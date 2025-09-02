from moviepy.editor import *
import assemblyai as aai

def transcribe_audio(filepath):
  aai.settings.api_key = "1d7a922389144d1aba29077899b40e6f"
  
  config = aai.TranscriptionConfig(speech_model=aai.SpeechModel.best)

  transcript = aai.Transcriber(config=config).transcribe(filepath)
  if transcript.status == "error":
    raise RuntimeError(f"Transcription failed: {transcript.error}")

  print(transcript.text)

def mp4_to_mp3(filename):
  video = VideoFileClip(filename)
  video.audio.write_audiofile("example.mp3")

# def translate_text_gemini()

def main():
  mp4_to_mp3("SampleVideo.mp4")
  transcribe_audio("example.mp3")

if __name__ == "__main__":
  main()