
from dotenv import load_dotenv
import argparse
from pathlib import Path

from stt import transcribe_audio
from translate import translate_text
from tts import text_to_speech


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="STT -> Translate -> TTS pipeline")
	parser.add_argument(
		"--audio",
		nargs="+",
		required=True,
		help="One or more audio file paths to transcribe",
	)
	parser.add_argument(
		"--target-language",
		required=True,
		help="Target language code for translation and TTS (e.g., hi-IN)",
	)
	parser.add_argument(
		"--source-language",
		default="en-IN",
		help="Source language code for translation",
	)
	parser.add_argument(
		"--output-dir",
		default="./output",
		help="Directory for TTS outputs",
	)
	parser.add_argument(
		"--speaker",
		default="shubh",
		help="TTS speaker ID",
	)
	parser.add_argument(
		"--stt-language",
		default="unknown",
		help="Language code for STT job",
	)
	return parser.parse_args()


def main() -> None:
	load_dotenv()
	args = parse_args()

	transcripts = transcribe_audio(
		audio_paths=args.audio,
		output_dir=args.output_dir,
		language_code=args.stt_language,
	)

	output_dir = Path(args.output_dir)
	for file_name, transcript in transcripts.items():
		translated = translate_text(
			transcript,
			target_language_code=args.target_language,
			source_language_code=args.source_language,
		)

		stem = Path(file_name).stem
		output_path = output_dir / f"{stem}.{args.target_language}.mp3"
		tts_result = text_to_speech(
			translated,
			target_language_code=args.target_language,
			output_path=str(output_path),
			speaker=args.speaker,
		)

		print(f"Input: {file_name}")
		print(f"Translated: {translated}")
		print(f"TTS output: {tts_result}\n")


if __name__ == "__main__":
	main()
