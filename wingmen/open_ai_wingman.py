import asyncio
import os
import threading
import time
import json
from datetime import datetime, timezone
import azure.cognitiveservices.speech as speechsdk
from typing import Mapping
from elevenlabslib import ElevenLabsUser, GenerationOptions, PlaybackOptions
from services.open_ai import AzureConfig, OpenAi
from services.edge import EdgeTTS
from services.printr import Printr
from services.secret_keeper import SecretKeeper
from wingmen.wingman import Wingman

printr = Printr()


class OpenAiWingman(Wingman):
    """Our OpenAI Wingman base gives you everything you need to interact with OpenAI's various APIs.

    It transcribes speech to text using Whisper, uses the Completion API for conversation and implements the Tools API to execute functions.
    """

    def __init__(self, name: str, config: dict[str, any], secret_keeper: SecretKeeper):
        super().__init__(name, config, secret_keeper)

        self._logged_memories: dict[str, str] = {}

        self.openai: OpenAi = None  # validate will set this
        """Our OpenAI API wrapper"""

        # every conversation starts with the "context" that the user has configured
        self.messages = [
            {"role": "system", "content": self.config["openai"].get("context")},
            {"role": "system", "content": "{{ {0} }}".format(self._logged_memories)},
        ]
        """The conversation history that is used for the GPT calls"""

        self.edge_tts = EdgeTTS()
        self.last_transcript_locale = None
        self.elevenlabs_api_key = None
        self.azure_keys = {
            "tts": None,
            "whisper": None,
            "conversation": None,
            "summarize": None,
        }
        self.stt_provider = self.config["features"].get("stt_provider", None)
        self.conversation_provider = self.config["features"].get(
            "conversation_provider", None
        )
        self.summarize_provider = self.config["features"].get(
            "summarize_provider", None
        )

        self.wait_task = None
        self.loop_task = None
        self.looping = True

    def prepare(self):
        super().prepare()
        self._load_logged_memories(self.name)

    def validate(self):
        errors = super().validate()
        openai_api_key = self.secret_keeper.retrieve(
            requester=self.name,
            key="openai",
            friendly_key_name="OpenAI API key",
            prompt_if_missing=True,
        )
        if not openai_api_key:
            errors.append(
                "Missing 'openai' API key. Please provide a valid key in the settings."
            )
        else:
            openai_organization = self.config["openai"].get("organization")
            openai_base_url = self.config["openai"].get("base_url")
            self.openai = OpenAi(openai_api_key, openai_organization, openai_base_url)

        self.__validate_elevenlabs_config(errors)

        self.__validate_azure_config(errors)

        return errors

    def __validate_elevenlabs_config(self, errors):
        if self.tts_provider == "elevenlabs":
            self.elevenlabs_api_key = self.secret_keeper.retrieve(
                requester=self.name,
                key="elevenlabs",
                friendly_key_name="Elevenlabs API key",
                prompt_if_missing=True,
            )
            if not self.elevenlabs_api_key:
                errors.append(
                    "Missing 'elevenlabs' API key. Please provide a valid key in the settings or use another tts_provider."
                )
                return
            elevenlabs_settings = self.config.get("elevenlabs")
            if not elevenlabs_settings:
                errors.append(
                    "Missing 'elevenlabs' section in config. Please provide a valid config or change the TTS provider."
                )
                return
            if not elevenlabs_settings.get("model"):
                errors.append("Missing 'model' setting in 'elevenlabs' config.")
                return
            voice_settings = elevenlabs_settings.get("voice")
            if not voice_settings:
                errors.append(
                    "Missing 'voice' section in 'elevenlabs' config. Please provide a voice configuration as shown in our example config."
                )
                return
            if not voice_settings.get("id") and not voice_settings.get("name"):
                errors.append(
                    "Missing 'id' or 'name' in 'voice' section of 'elevenlabs' config. Please provide a valid name or id for the voice in your config."
                )

    def __validate_azure_config(self, errors):
        if (
            self.tts_provider == "azure"
            or self.stt_provider == "azure"
            or self.conversation_provider == "azure"
            or self.summarize_provider == "azure"
        ):
            azure_settings = self.config.get("azure")
            if not azure_settings:
                errors.append(
                    "Missing 'azure' section in config. Please provide a valid config."
                )
                return

        if self.tts_provider == "azure":
            self.azure_keys["tts"] = self.secret_keeper.retrieve(
                requester=self.name,
                key="azure_tts",
                friendly_key_name="Azure TTS API key",
                prompt_if_missing=True,
            )
            if not self.azure_keys["tts"]:
                errors.append(
                    "Missing 'azure' tts API key. Please provide a valid key in the settings."
                )
                return

        if self.stt_provider == "azure":
            self.azure_keys["whisper"] = self.secret_keeper.retrieve(
                requester=self.name,
                key="azure_whisper",
                friendly_key_name="Azure Whisper API key",
                prompt_if_missing=True,
            )
            if not self.azure_keys["whisper"]:
                errors.append(
                    "Missing 'azure' whisper API key. Please provide a valid key in the settings."
                )
                return

        if self.conversation_provider == "azure":
            self.azure_keys["conversation"] = self.secret_keeper.retrieve(
                requester=self.name,
                key="azure_conversation",
                friendly_key_name="Azure Conversation API key",
                prompt_if_missing=True,
            )
            if not self.azure_keys["conversation"]:
                errors.append(
                    "Missing 'azure' conversation API key. Please provide a valid key in the settings."
                )
                return

        if self.summarize_provider == "azure":
            self.azure_keys["summarize"] = self.secret_keeper.retrieve(
                requester=self.name,
                key="azure_summarize",
                friendly_key_name="Azure Summarize API key",
                prompt_if_missing=True,
            )
            if not self.azure_keys["summarize"]:
                errors.append(
                    "Missing 'azure' summarize API key. Please provide a valid key in the settings."
                )
                return

    async def _transcribe(self, audio_input_wav: str) -> tuple[str | None, str | None]:
        """Transcribes the recorded audio to text using the OpenAI Whisper API.

        Args:
            audio_input_wav (str): The path to the audio file that contains the user's speech. This is a recording of what you you said.

        Returns:
            str | None: The transcript of the audio file or None if the transcription failed.
        """
        detect_language = self.config["edge_tts"].get("detect_language")

        response_format = (
            "verbose_json"  # verbose_json will return the language detected in the transcript.
            if self.tts_provider == "edge_tts" and detect_language
            else "json"
        )

        azure_config = None
        if self.stt_provider == "azure":
            azure_config = self._get_azure_config("whisper")

        transcript = self.openai.transcribe(
            audio_input_wav, response_format=response_format, azure_config=azure_config
        )

        locale = None
        # skip the GPT call if we didn't change the language
        if (
            response_format == "verbose_json"
            and transcript
            and transcript.language != self.last_transcript_locale  # type: ignore
        ):
            printr.print(
                f"   EdgeTTS detected language '{transcript.language}'.", tags="info"  # type: ignore
            )
            locale = self.__ask_gpt_for_locale(transcript.language)  # type: ignore

        return transcript.text if transcript else None, locale

    def _get_azure_config(self, section: str):
        azure_api_key = self.azure_keys[section]
        azure_config = AzureConfig(
            api_key=azure_api_key,
            api_base_url=self.config["azure"]
            .get(section, {})
            .get("api_base_url", None),
            api_version=self.config["azure"].get(section, {}).get("api_version", None),
            deployment_name=self.config["azure"]
            .get(section, {})
            .get("deployment_name", None),
        )

        return azure_config

    async def _get_response_for_transcript(
        self, transcript: str, locale: str | None
    ) -> tuple[str, str]:
        """Gets the response for a given transcript.

        This function interprets the transcript, runs instant commands if triggered,
        calls the OpenAI API when needed, processes any tool calls, and generates the final response.

        Args:
            transcript (str): The user's spoken text transcribed.

        Returns:
            A tuple of strings representing the response to a function call and an instant response.
        """
        self.last_transcript_locale = locale
        self._add_user_message(transcript)

        instant_response = self._try_instant_activation(transcript)
        if instant_response:
            return instant_response, instant_response

        completion = self._gpt_call()

        if completion is None:
            return None, None

        response_message, tool_calls = self._process_completion(completion)

        # do not tamper with this message as it will lead to 400 errors!
        self.messages.append(response_message)

        if tool_calls:
            instant_response = await self._handle_tool_calls(tool_calls)
            if instant_response:
                return None, instant_response

            summarize_response = self._summarize_function_calls()
            return self._finalize_response(str(summarize_response))

        return response_message.content, response_message.content

    def _add_user_message(self, content: str):
        """Shortens the conversation history if needed and adds a user message to it.

        Args:
            content (str): The message content to add.
            role (str): The role of the message sender ("user", "assistant", "function" or "tool").
            tool_call_id (Optional[str]): The identifier for the tool call, if applicable.
            name (Optional[str]): The name of the function associated with the tool call, if applicable.
        """
        msg = {"role": "user", "content": content}
        self._cleanup_conversation_history()
        self.messages.append(msg)

    def _cleanup_conversation_history(self):
        """Cleans up the conversation history by removing messages that are too old."""
        remember_messages = self.config.get("features", {}).get(
            "remember_messages", None
        )

        if remember_messages is None or len(self.messages) == 0:
            return 0  # Configuration not set, nothing to delete.

        # The system message aka `context` does not count
        context_offset = (
            2 if self.messages and self.messages[0]["role"] == "system" else 0
        )

        # Find the cutoff index where to end deletion, making sure to only count 'user' messages towards the limit starting with newest messages.
        cutoff_index = len(self.messages) - 1
        user_message_count = 0
        for message in reversed(self.messages):
            if self.__get_message_role(message) == "user":
                user_message_count += 1
                if user_message_count == remember_messages:
                    break  # Found the cutoff point.
            cutoff_index -= 1

        # If messages below the keep limit, don't delete anything.
        if user_message_count < remember_messages:
            return 0

        total_deleted_messages = cutoff_index - context_offset  # Messages to delete.

        # Remove the messages before the cutoff index, exclusive of the system message.
        del self.messages[context_offset:cutoff_index]

        # Optional debugging printout.
        if self.debug and total_deleted_messages > 0:
            printr.print(
                f"Deleted {total_deleted_messages} messages from the conversation history.",
                tags="warn",
            )

        return total_deleted_messages

    def reset_conversation_history(self):
        """Resets the conversation history by removing all messages except for the system messages."""
        del self.messages[2:]

    def _try_instant_activation(self, transcript: str) -> str:
        """Tries to execute an instant activation command if present in the transcript.

        Args:
            transcript (str): The transcript to check for an instant activation command.

        Returns:
            str: The response to the instant command or None if no such command was found.
        """
        command = self._execute_instant_activation_command(transcript)
        if command:
            response = self._select_command_response(command)
            return response
        return None

    def _gpt_call(self):
        """Makes the primary GPT call with the conversation history and tools enabled.

        Returns:
            The GPT completion object or None if the call fails.
        """
        if self.debug:
            printr.print(
                f"   Calling GPT with {(len(self.messages) - 1)} messages (excluding context)",
                tags="info",
            )

        azure_config = None
        if self.conversation_provider == "azure":
            azure_config = self._get_azure_config("conversation")

        return self.openai.ask(
            messages=self.messages,
            tools=self._build_tools(),
            model=self.config["openai"].get("conversation_model"),
            azure_config=azure_config,
        )

    def _process_completion(self, completion):
        """Processes the completion returned by the GPT call.

        Args:
            completion: The completion object from an OpenAI call.

        Returns:
            A tuple containing the message response and tool calls from the completion.
        """
        response_message = completion.choices[0].message

        content = response_message.content
        if content is None:
            response_message.content = ""

        return response_message, response_message.tool_calls

    async def _handle_tool_calls(self, tool_calls):
        """Processes all the tool calls identified in the response message.

        Args:
            tool_calls: The list of tool calls to process.

        Returns:
            str: The immediate response from processed tool calls or None if there are no immediate responses.
        """
        instant_response = None
        function_response = ""

        for tool_call in tool_calls:
            function_name = tool_call.function.name
            function_args = json.loads(tool_call.function.arguments)
            (
                function_response,
                instant_response,
            ) = await self._execute_command_by_function_call(
                function_name, function_args
            )

            msg = {"role": "tool", "content": function_response}
            if tool_call.id is not None:
                msg["tool_call_id"] = tool_call.id
            if function_name is not None:
                msg["name"] = function_name

            # Don't use self._add_user_message_to_history here because we never want to skip this because of history limitions
            self.messages.append(msg)

        return instant_response

    def _summarize_function_calls(self):
        """Summarizes the function call responses using the GPT model specified for summarization in the configuration.

        Returns:
            The content of the GPT response to the function call summaries.
        """
        azure_config = None
        if self.summarize_provider == "azure":
            azure_config = self._get_azure_config("summarize")

        summarize_model = self.config["openai"].get("summarize_model")
        summarize_response = self.openai.ask(
            messages=self.messages,
            model=summarize_model,
            azure_config=azure_config,
        )

        if summarize_response is None:
            return None

        # do not tamper with this message as it will lead to 400 errors!
        message = summarize_response.choices[0].message
        self.messages.append(message)
        return message.content

    def _finalize_response(self, summarize_response: str) -> tuple[str, str]:
        """Finalizes the response based on the call of the second (summarize) GPT call.

        Args:
            summarize_response (str): The response content from the second GPT call.

        Returns:
            A tuple containing the final response to the user.
        """
        if summarize_response is None:
            return self.messages[-1]["content"], self.messages[-1]["content"]
        return summarize_response, summarize_response

    async def _execute_command_by_function_call(
        self, function_name: str, function_args: dict[str, any]
    ) -> tuple[str, str]:
        """
        Uses an OpenAI function call to execute a command. If it's an instant activation_command, one if its reponses will be played.

        Args:
            function_name (str): The name of the function to be executed.
            function_args (dict[str, any]): The arguments to pass to the function being executed.

        Returns:
            A tuple containing two elements:
            - function_response (str): The text response or result obtained after executing the function.
            - instant_response (str): An immediate response or action to be taken, if any (e.g., play audio).
        """
        function_response = ""
        instant_reponse = ""
        if function_name == "execute_command":
            # get the command based on the argument passed by GPT
            command = self._get_command(function_args["command_name"])
            # execute the command
            function_response = self._execute_command(command)
            # if the command has responses, we have to play one of them
            if command and command.get("responses"):
                instant_reponse = self._select_command_response(command)
                await self._play_to_user(instant_reponse)

        if function_name == "search_online":
            function_response = self._search_online(
                function_args["query_string"],
                function_args["category"],
                function_args["attributes"],
            )

        if function_name == "log_memory":
            function_response = self._log_memory(
                function_args["name"], function_args["value"]
            )

        if function_name == "create_complex_object":
            function_response = self._create_complex_memory(
                function_args["name"],
                function_args["file_name"],
                function_args["content"]
            )

        if function_name == "get_complex_object":
            obj = self._get_complex_memory(function_args["file_name"])
            function_response = f"{obj}"

        if function_name == "modify_complex_object":
            function_response = self._modify_complex_memory(
                function_args["file_name"],
                function_args["key"],
                function_args["value"],
            )

        if function_name == "get_current_time":
            function_response = self._get_current_time()

        if function_name == "wait_for_then":
            function_response = self._wait_for_then(
                function_args["wait_time"],
                function_args["action"],
            )

        if function_name == "start_command_loop":
            function_response = self._start_command_loop(
                function_args["command_name"],
                function_args["interval"],
                function_args["amount"],
            )

        if function_name == "stop_command_loop":
            function_response = self._stop_command_loop()

        return function_response, instant_reponse

    async def _play_to_user(self, text: str):
        """Plays audio to the user using the configured TTS Provider (default: OpenAI TTS).
        Also adds sound effects if enabled in the configuration.

        Args:
            text (str): The text to play as audio.
        """

        if self.tts_provider == "edge_tts":
            await self._play_with_edge_tts(text)
        elif self.tts_provider == "elevenlabs":
            self._play_with_elevenlabs(text)
        elif self.tts_provider == "azure":
            self._play_with_azure(text)
        else:
            self._play_with_openai(text)

    def _play_with_openai(self, text):
        response = self.openai.speak(text, self.config["openai"].get("tts_voice"))
        if response is not None:
            self.audio_player.stream_with_effects(response.content, self.config)

    def _play_with_azure(self, text):
        azure_config = self.config["azure"].get("tts", None)

        if azure_config is None:
            return

        speech_config = speechsdk.SpeechConfig(
            subscription=self.azure_keys["tts"],
            region=azure_config["region"],
        )
        speech_config.speech_synthesis_voice_name = azure_config["voice"]

        if azure_config["detect_language"]:
            auto_detect_source_language_config = (
                speechsdk.AutoDetectSourceLanguageConfig()
            )

        speech_synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=speech_config,
            audio_config=None,
            auto_detect_source_language_config=auto_detect_source_language_config
            if azure_config["detect_language"]
            else None,
        )

        result = speech_synthesizer.speak_text_async(text).get()
        if result is not None:
            self.audio_player.stream_with_effects(result.audio_data, self.config)

    async def _play_with_edge_tts(self, text: str):
        output_file = "audio_output/edge_tts.mp3"
        edge_config = self.config["edge_tts"]

        tts_voice = edge_config.get("tts_voice")
        detect_language = edge_config.get("detect_language")
        if detect_language:
            gender = edge_config.get("gender")
            tts_voice = await self.edge_tts.get_same_random_voice_for_language(
                gender, self.last_transcript_locale
            )

        await self.edge_tts.generate_speech(text, filename=output_file, voice=tts_voice)
        audio, sample_rate = self.audio_player.get_audio_from_file(output_file)

        self.audio_player.stream_with_effects((audio, sample_rate), self.config)

    def _play_with_elevenlabs(self, text: str):
        # presence already validated in validate()
        elevenlabs_config = self.config["elevenlabs"]
        # validate() already checked that either id or name is set
        voice_id = elevenlabs_config["voice"].get("id")
        voice_name = elevenlabs_config["voice"].get("name")

        voice_settings = elevenlabs_config.get("voice_settings", {})
        user = ElevenLabsUser(self.elevenlabs_api_key)
        model = elevenlabs_config.get("model", "eleven_multilingual_v2")

        if voice_id:
            voice = user.get_voice_by_ID(voice_id)
        else:
            voice = user.get_voices_by_name(voice_name)[0]

        # todo: add start/end callbacks to play Quindar beep even if use_sound_effects is disabled
        playback_options = PlaybackOptions(runInBackground=True)
        generation_options = GenerationOptions(
            model=model,
            latencyOptimizationLevel=elevenlabs_config.get("latency", 0),
            style=voice_settings.get("style", 0),
            use_speaker_boost=voice_settings.get("use_speaker_boost", True),
        )
        stability = voice_settings.get("stability")
        if stability is not None:
            generation_options.stability = stability

        similarity_boost = voice_settings.get("similarity_boost")
        if similarity_boost is not None:
            generation_options.similarity_boost = similarity_boost

        style = voice_settings.get("style")
        if style is not None and model != "eleven_turbo_v2":
            generation_options.style = style

        use_sound_effects = elevenlabs_config.get("use_sound_effects", False)
        if use_sound_effects:
            audio_bytes, _history_id = voice.generate_audio_v2(
                prompt=text,
                generationOptions=generation_options,
            )
            if audio_bytes:
                self.audio_player.stream_with_effects(audio_bytes, self.config)
        else:
            voice.generate_stream_audio_v2(
                prompt=text,
                playbackOptions=playback_options,
                generationOptions=generation_options,
            )

    def _execute_command(self, command: dict) -> str:
        """Does what Wingman base does, but always returns "Ok" instead of a command response.
        Otherwise the AI will try to respond to the command and generate a "duplicate" response for instant_activation commands.
        """
        super()._execute_command(command)
        return "Ok"

    def _build_tools(self) -> list[dict]:
        """
        Builds a tool for each command that is not instant_activation.

        Returns:
            list[dict]: A list of tool descriptors in OpenAI format.
        """
        commands = [
            command["name"]
            for command in self.config.get("commands", [])
            if not command.get("instant_activation")
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "execute_command",
                    "description": "Executes a command",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command_name": {
                                "type": "string",
                                "description": "The command to execute",
                                "enum": commands,
                            },
                        },
                        "required": ["command_name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_online",
                    "description": "A function used to search the internet, to search for content related to a search query",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query_string": {
                                "type": "string",
                                "description": "The subject of the query that was asked",
                            },
                            "category": {
                                "type": "string",
                                "description": "The category the subject falls under. i.e: ships, weapons, components",
                            },
                            "attributes": {
                                "type": "string",
                                "description": "The attributes relating to the subject; comma delimited",
                            },
                        },
                        "required": ["query_string", "category", "attributes"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_current_time",
                    "description": "An internal function that gets the current time if so needed",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_complex_object",
                    "description": "A function that creates a complex object to be saved to memory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "The name of the memory or 'key' to index it by.",
                            },
                            "file_name": {
                                "type": "string",
                                "description": "The name of the file.",
                            },
                            "content":{
                                "type": "string",
                                "description": "The initial serialzied data, in json format, that should be stored if any was provided"
                            }
                        },
                        "required": ["name", "file_name", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_complex_object",
                    "description": "A function that gets a complex object of which we have an index for in the current memory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_name": {
                                "type": "string",
                                "description": "The name of the file.",
                            },
                        },
                        "required": ["file_name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "modify_complex_object",
                    "description": "A function that modifies an existing complex object of which we have an index for in the current memory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_name": {
                                "type": "string",
                                "description": "The name of the file.",
                            },
                            "key": {
                                "type": "string",
                                "description": "The name of the property to index it by. i.e 'object_property'",
                            },
                            "value": {
                                "type": "string",
                                "description": "The value of the property. this can be a single string, an array or a complex object (in serialized json format) depending on the value that is needed to be stored.",
                            },
                        },
                        "required": ["file_name", "key", "value"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "log_memory",
                    "description": "A function that is called when a specific detail needs to be written to memory. This should be called when you assume details are important.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "The name of the memory or 'key' to index it by. i.e 'user_favorite_color'",
                            },
                            "value": {
                                "type": "string",
                                "description": "The value to remember respective to its key. i.e 'Blue'",
                            },
                        },
                        "required": ["name", "value"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "wait_for_then",
                    "description": "Waits for specified amount of time and then does something. This is also used when asked to set a reminder.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "wait_time": {
                                "type": "integer",
                                "description": "The amount of time to wait in seconds. If minutes or hours are given, convert it.",
                            },
                            "action": {
                                "type": "string",
                                "description": "The order to do i.e 'Do this action'",
                            },
                        },
                        "required": ["wait_time", "action"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "start_command_loop",
                    "description": "A function that executes a given command in a loop for a specific amount of times or infinately",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command_name": {
                                "type": "string",
                                "description": "The command to execute",
                                "enum": commands,
                            },
                            "interval": {
                                "type": "number",
                                "description": "The interval in between executions. If none is given, the default will be 2 seconds",
                            },
                            "amount": {
                                "type": "number",
                                "description": "The amount of times to loop the command. -1 for infinity",
                            },
                        },
                        "required": ["command_name", "interval", "amount"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "stop_command_loop",
                    "description": "A function that stops the current command loop",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
        ]
        return tools

    def __ask_gpt_for_locale(self, language: str) -> str:
        """OpenAI TTS returns a natural language name for the language of the transcript, e.g. "german" or "english".
        This method uses ChatGPT to find the corresponding locale, e.g. "de-DE" or "en-EN".

        Args:
            language (str): The natural, lowercase language name returned by OpenAI TTS. Thank you for that btw.. WTF OpenAI?
        """

        response = self.openai.ask(
            messages=[
                {
                    "content": """
                        I'll say a natural language name in lowercase and you'll just return the IETF country code / locale for this language.
                        Your answer always has exactly 2 lowercase letters, a dash, then two more letters in uppercase.
                        If I say "german", you answer with "de-DE". If I say "russian", you answer with "ru-RU".
                        If it's ambiguous and you don't know which locale to pick ("en-GB" vs "en-US"), you pick the most commonly used one.
                        You only answer with valid country codes according to most common standards.
                        If you can't, you respond with "None".
                    """,
                    "role": "system",
                },
                {
                    "content": language,
                    "role": "user",
                },
            ],
            model="gpt-3.5-turbo-1106",
        )
        answer = response.choices[0].message.content

        if answer == "None":
            return None

        printr.print(
            f"   ChatGPT says this language maps to locale '{answer}'.", tags="info"
        )
        return answer

    def __get_message_role(self, message):
        """Helper method to get the role of the message regardless of its type."""
        if isinstance(message, Mapping):
            return message.get("role")
        elif hasattr(message, "role"):
            return message.role
        else:
            raise TypeError(
                f"Message is neither a mapping nor has a 'role' attribute: {message}"
            )

    def _search_online(self, search_term: str, category: str, attributes: str):
        async def _access_game_data_file(
            self, search_term: str, category: str, attributes: str
        ):
            json_data = {}
            # Should load the local json data
            # Find the category
            # Find search_term
            # Find attribute if provided
            # return json data
            return json_data

        asyncio.create_task(
            _access_game_data_file(self, search_term, category, attributes)
        )
        return f"Searching internet to find data on {search_term}. Please wait."

    def _log_memory(self, name: str, vlaue: str):
        self._logged_memories[name] = vlaue
        self.messages[1]["content"] = "{{ {0} }}".format(self._logged_memories)
        self._save_logged_memories()
        return "Noted"

    def _create_complex_memory(self, file_name: str, name: str, content: str = None):
        obj = {}
        if content:
            obj = json.loads(content)
        self._save_complex_memory(file_name, obj)
        self._log_memory(name, file_name)
        return "Empty complex object created. No data logged. Must now specify properties to start modifying."

    def _save_complex_memory(self, file_name: str, obj: any):
        file_path = f"wingmen/{self.name}/{file_name}.json"
        if not os.path.exists(f"wingmen/{self.name}"):
            # If not, create it
            os.makedirs(f"wingmen/{self.name}")
        with open(file_path, "w") as file:
            json.dump(obj, file, indent=4)

    def _modify_complex_memory(self, file_name: str, key: str, value: str):
        memory = self._get_complex_memory(file_name)
        obj = json.loads(value)
        memory[key] = obj
        self._save_complex_memory(file_name, memory)
        return f"Modified {key} to {value}"

    def _get_complex_memory(self, file_name: str):
        # File path where your JSON data is stored
        file_path = f"wingmen/{self.name}/{file_name}"
        if not file_path.endswith(".json") :
           file_path = f"{file_path}.json"
        # Reading the JSON file back into a list of dictionaries
        if os.path.exists(file_path):
            with open(file_path, "r") as file:
                return json.load(file)
        return "No such memory"

    def _remove_log_memory(self, name: str):
        self._logged_memories = [d for d in self._logged_memories if d[0] != name]
        self.messages[1]["content"] = "{{ {0} }}".format(self._logged_memories)
        self._save_logged_memories()
        return "Removed"

    def _save_logged_memories(self):
        file_path = f"wingmen/{self.name}_logged_memories.json"
        # Writing the list of dictionaries to a JSON file
        with open(file_path, "w") as file:
            json.dump(self._logged_memories, file, indent=4)

    def _load_logged_memories(self, wingman_name):
        # File path where your JSON data is stored
        file_path = f"wingmen/{wingman_name}_logged_memories.json"
        # Reading the JSON file back into a list of dictionaries
        if os.path.exists(file_path):
            with open(file_path, "r") as file:
                self._logged_memories = json.load(file)
                self.messages[1]["content"] = "{{ {0} }}".format(self._logged_memories)

    def _get_current_time(self):
        # Get the current UTC time
        current_utc_time = datetime.now(timezone.utc)
        current_time = current_utc_time.strftime("%d/%m/%Y, %H:%M UTC")
        return f"current time: {current_time}"

    def _wait_for_then(self, wait_time: int, action: str):
        if self.wait_task:
            self.wait_task.cancel()
        wait_thread = threading.Thread(
            target=self._wait_then_do_task(wait_time, action)
        )
        wait_thread.start()
        return "Respond with a message that acknowledges the wait_time and action"

    def _wait_then_do_task(self, wait_time: int, action: str):
        async def _task(wait_time: int, action: str):
            try:
                time.sleep(wait_time)
                response, instant_response = await self._get_response_for_transcript(
                    action, self.last_transcript_locale
                )
                Printr.clr_print(f"<< ({self.name}): {response}", Printr.GREEN)
                await self._play_to_user(response)
            except Exception as e:
                Printr.clr_print(f"Error occurred: {str(e)}", Printr.RED)

        self.wait_task = asyncio.create_task(_task(wait_time, action))

    def _start_command_loop(
        self, command_name: str, interval: int = 2, amount: int = -1
    ):
        if amount == 0:
            return "Nevermind then"
        command = self._get_command(command_name)
        if self.loop_task:
            self.loop_task.cancel()
        self.looping = True
        self.loop_task = asyncio.create_task(self._loop(command, interval))
        return "Starting command loop"

    async def _loop(self, command: str, interval: int = 2, amount: int = -1):
        looped = 0
        try:
            while self.looping and amount < looped:
                time.sleep(interval)
                self._execute_command(command)
                looped += 1
        except Exception as e:
            Printr.clr_print(f"Error occurred: {str(e)}", Printr.RED)
        return self._stop_command_loop()

    def _stop_command_loop(self):
        if self.loop_task:
            self.looping = False
            self.loop_task.cancel()
            return "Stopping command loop"
        return "No command to stop"