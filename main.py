# Install the assemblyai package by executing the command "pip install assemblyai"
# from moviepy.editor import *
import assemblyai as aai

aai.settings.api_key = "1d7a922389144d1aba29077899b40e6f"

# audio_file = "./local_file.mp3"
audio_file = "example.mp3"

config = aai.TranscriptionConfig(speech_model=aai.SpeechModel.best)

transcript = aai.Transcriber(config=config).transcribe(audio_file)

if transcript.status == "error":
  raise RuntimeError(f"Transcription failed: {transcript.error}")

print(transcript.text)

# video = VideoFileClip("SampleVideo.mp4")

# video.audio.write_audiofile("example.mp3")
