import json
import os
import time
import requests
from elevenlabs import ElevenLabs

API_KEY = ""
BASE = "https://api.magnific.com"

payload = {
    "prompt": "The character is speaking directly to the camera in calm, divine, and confident manner, delivering a spiritual message as if guided by Shri Krishna. Voiceover / Spoken Dialogue (exact lip-sync required):श्री कृष्ण कहते हैं, (pause) जब हम आभार महसूस करते हैं, तो हमारी दुनिया सुंदर हो जाती है। Natural human-like head movements while speaking seeng left and right: gentle nods, smile and charm in face, subtle head tilts, organic pauses between sentences. Extremely accurate Hindi lip-sync with realistic mouth shapes, jaw movement, breath timing, and natural speech flow. Soft eye contact with the camera, natural blinking, slight eye movements to avoid stiffness.",
    "image": "https://ctixankxxpkworlbihfp.supabase.co/storage/v1/object/sign/images%20for%20test/magnific_2850346686.png?token=eyJraWQiOiJzdG9yYWdlLXVybC1zaWduaW5nLWtleV8zMjhhMDNmMS0yNTVkLTQ5OTQtOWI2NS04MTNmZGNiNGIzOGQiLCJhbGciOiJIUzI1NiJ9.eyJ1cmwiOiJpbWFnZXMgZm9yIHRlc3QvbWFnbmlmaWNfMjg1MDM0NjY4Ni5wbmciLCJpYXQiOjE3Nzc0NTk0MjAsImV4cCI6MTgwODk5NTQyMH0.YwnYJbEJUowUrJHRJT9RtRLiKdw3PJplya2A91Y1y5Y=",
    "duration": 10,
    "generate_audio": True,
    "camera_fixed": True,
    "aspect_ratio": "widescreen_16_9",
    "seed": -1,
}

headers = {
    "x-magnific-api-key": API_KEY,
    "Content-Type": "application/json",
}

# 1) Create task
print("generation about to start")
resp = requests.post(f"{BASE}/v1/ai/video/seedance-1-5-pro-1080p", json=payload, headers=headers)
resp.raise_for_status()
task_id = resp.json()["data"]["task_id"]

# 2) Poll status
while True:
    print("started generation")
    status_resp = requests.get(
        f"{BASE}/v1/ai/video/seedance-1-5-pro-1080p/{task_id}",
        headers={"x-magnific-api-key": API_KEY},
    )
    status_resp.raise_for_status()
    status_payload = status_resp.json()
    data = status_payload["data"]

    if data["status"] == "COMPLETED":
        with open("seedance_task_response.txt", "w", encoding="utf-8") as f:
            json.dump(status_payload, f, indent=2, ensure_ascii=True)
        video_url = data["generated"][0]
        # 3) Download
        with requests.get(video_url, stream=True) as r:
            r.raise_for_status()
            with open("seedance_1080p.mp4", "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        break

    if data["status"] == "FAILED":
        with open("seedance_task_response.txt", "w", encoding="utf-8") as f:
            json.dump(status_payload, f, indent=2, ensure_ascii=True)
        raise RuntimeError("Video generation failed")

    time.sleep(5)


client = ElevenLabs(
    api_key="",
)

audio_data = client.text_to_speech.convert(
    voice_id="zeOSoTv4slb5gv8XJfVx",
    output_format="mp3_44100_128",
    text="श्री कृष्ण कहते हैं, (pause) जब हम आभार महसूस करते हैं, तो हमारी दुनिया सुंदर हो जाती है।",
    model_id="eleven_v3",
)

with open("seedance_1080p.mp3", "wb") as f:
    for chunk in audio_data:
        f.write(chunk)
