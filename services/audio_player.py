import io
import os
from os import path
from enum import Enum
from pydub import AudioSegment
from pydub.playback import play
from scipy.io import wavfile
from pedalboard import Pedalboard, Chorus, PitchShift, Reverb, Delay, Gain, Resample, Compressor, HighpassFilter, LowpassFilter
import soundfile as sf
import scipy.signal
import numpy

class PedalBoards(Enum):
    ROBOT = Pedalboard([
        PitchShift(semitones=-1),
        Delay(delay_seconds=0.01,feedback=0.5,mix=0.2),
        Chorus(rate_hz=0.5, depth=0.8, mix=0.5, centre_delay_ms=2, feedback=0.3),
        Reverb(room_size=0.05, dry_level=0.5, wet_level=0.5, freeze_mode=0.5, width=0.3),
        Gain(gain_db=3)
    ])
    RADIO = Pedalboard([
        HighpassFilter(1000),
        LowpassFilter(5000),
        Resample(10000),
        Gain(gain_db=3),
        Compressor(threshold_db=-21,ratio=3.5,attack_ms=1,release_ms=50),
        Delay(delay_seconds=0.1,mix=0.9),
        Reverb(room_size=0.01, dry_level=0.6, wet_level=0.4, width=0.3)
    ])

class AudioPlayer:
    def stream(self, stream: bytes):
        audio = self.get_audio_from_stream(stream)
        play(audio)

    def stream_with_effects(
        self,
        stream: bytes,
        play_beep: bool = False,
        play_noise: bool = False,
        robot_effect: bool = False,
    ):
        audio = self.get_audio_from_stream(stream)

        if not os.path.exists("audio_output"):
            os.makedirs("audio_output")

        audio_path = "audio_output/output_generated.wav"
        audio.export(audio_path, format="wav")

        if play_beep | play_noise:
            audio = self.add_radio_effect_with_beep(
                audio_path,
                play_beep,
                play_noise,
                delete_source=True,
            )
            audio.export(audio_path, format="wav")

        if robot_effect:
            self.effect_audio(audio_path, PedalBoards.ROBOT)
            audio = AudioSegment.from_wav(audio_path)

        play(audio)

    def effect_audio(self, audio_file_path, board: PedalBoards):
        # Load the audio file
        audio, sample_rate = sf.read(audio_file_path)
        # Process the audio with the effects
        processed_audio = board.value(audio, sample_rate)
        # Save the processed audio to a new file
        sf.write(audio_file_path, processed_audio, sample_rate)

    def get_audio_from_stream(self, stream: bytes) -> AudioSegment:
        byte_stream = io.BytesIO(stream)
        audio = AudioSegment.from_file(byte_stream, format="mp3")
        return audio

    def play(self, filename: str):
        audio = None
        if filename.endswith(".wav"):
            audio = AudioSegment.from_wav(filename)
        elif filename.endswith(".mp3"):
            audio = AudioSegment.from_mp3(filename)

        if audio:
            play(audio)

    def add_radio_effect_with_beep(
        self,
        filename: str,
        play_beep: bool = False,
        play_noise: bool = False,
        delete_source: bool = False,
    ):
        bundle_dir = path.abspath(path.dirname(__file__))

        file, extension = os.path.splitext(filename)
        wav_file = file + ".wav"

        if extension == ".mp3":
            sound = AudioSegment.from_mp3(filename)
            sound.export(wav_file, format="wav")

        samplerate, data = wavfile.read(wav_file)
        nyquist = 0.5 * samplerate
        low, high = 500 / nyquist, 5000 / nyquist

        filtered_sound = AudioSegment.from_wav(wav_file)

        if play_noise:
            b, a = scipy.signal.butter(5, [low, high], btype="band")
            filtered = scipy.signal.lfilter(b, a, data)
            filtered_wav = file + "_filtered.wav"
            wavfile.write(filtered_wav, samplerate, filtered.astype(numpy.int16))
            filtered_sound = AudioSegment.from_wav(filtered_wav)
            filtered_sound = filtered_sound + 10

            noise = AudioSegment.from_mp3(
                path.join(bundle_dir, "../audio_samples/noise.wav")
            )
            noise_sound = noise - 30

            # Calculate the durations
            main_duration = len(filtered_sound)

            # Loop the noise until it matches or exceeds the duration of the main audio
            looped_noise = noise_sound
            while len(looped_noise) < main_duration:
                looped_noise += noise_sound

            # If looped noise is longer than the main audio, cut it down to the correct length
            if len(looped_noise) > main_duration:
                looped_noise = looped_noise[:main_duration]

            filtered_sound = filtered_sound.overlay(looped_noise)

        intro_audio = AudioSegment.empty()
        outro_audio = AudioSegment.empty()
        if play_beep:
            # Load the audio to be added at the beginning and end
            intro_audio = AudioSegment.from_mp3(
                path.join(bundle_dir, "../audio_samples/beep.wav")
            )
            intro_audio = intro_audio + 3

            outro_audio = AudioSegment.from_mp3(
                path.join(bundle_dir, "../audio_samples/beep.wav")
            )

        # Concatenate the audio
        final_audio = intro_audio + filtered_sound + outro_audio

        """ os.remove(filtered_wav)
        if delete_source:
            os.remove(filename) """

        return final_audio
