# from elevenlabs.client import ElevenLabs
# from elevenlabs.types import VoiceSettings
# from dotenv import load_dotenv
# import os

# if __name__ == "__main__":
#     load_dotenv()

#     bhojpuri_ai_studio = "S1JBcZECEJJlf7lEDTbN"
#     dv_nisha = "dffT29nmBclERTsFHmHg"

#     my_api_key = os.getenv("ELEVEN_LABS")
#     client = ElevenLabs(api_key=my_api_key)

#     audio_data = client.text_to_speech.convert(
#         voice_id=bhojpuri_ai_studio,
#         model_id="eleven_v3",
#         text="[excitement] নমস্কার, [loudly laugh] এটি অভিব্যক্তিমূলক বক্তৃতা।",
#         voice_settings=VoiceSettings(
#             stability=0.5,
#             similarity_boost=0.75,
#             style=0.0,
#             use_speaker_boost=True,
#         ),
#     )

#     with open("output.mp3", "wb") as f:
#         for chunk in audio_data:
#             f.write(chunk)

import os
from dotenv import load_dotenv
import google.genai as genai


load_dotenv()

def get_gemini_api_key() -> str:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY environment variable")
    return api_key

api_key = get_gemini_api_key()
client = genai.Client(api_key=api_key)

for m in client.models.list():
    print(m.name)

# response = client.models.generate_content(
#     model="gemini-3-flash-preview",
#     contents="Hello"
# )

# print(response)