from elevenlabs.client import ElevenLabs
from elevenlabs.types import VoiceSettings
from dotenv import load_dotenv
import os
from pydantic.v1.datetime_parse import parse_date as parse_date

load_dotenv()

bhojpuri_ai_studio = "S1JBcZECEJJlf7lEDTbN"
dv_nisha = "dffT29nmBclERTsFHmHg"

my_api_key = os.getenv("ELEVEN_LABS")
from elevenlabs.client import ElevenLabs

client = ElevenLabs(api_key=my_api_key)

audio_data = client.text_to_speech.convert(
    
    voice_id=bhojpuri_ai_studio,
    model_id="eleven_v3",
    text="[excitement] নমস্কার, [loudly laugh] এটি অভিব্যক্তিমূলক বক্তৃতা।",
    voice_settings=VoiceSettings(
        stability=0.5,
        similarity_boost=0.75,
        style=0.0,
        use_speaker_boost=True
    )
)

with open("output.mp3", "wb") as f:
    for chunk in audio_data:
        f.write(chunk)