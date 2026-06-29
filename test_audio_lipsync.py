#!/usr/bin/env python3
"""One-off test: generate a HeyGen avatar video lip-synced to a pre-recorded
audio file (instead of a TTS script), and save the result to ~/Downloads.

Audio bypasses TTS entirely — HeyGen animates the avatar's mouth to match
news_anchor.mp3 directly.

Usage:
    python test_audio_lipsync.py [--avatar TALKING_PHOTO_ID]

If no avatar is given, the first photo avatar in your account is used.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
AUDIO_FILE = ROOT / "news_anchor.mp3"
PHOTO_FILE = ROOT / "news_anchor.png"
DOWNLOADS = Path.home() / "Downloads"

API_BASE = "https://api.heygen.com"
UPLOAD_BASE = "https://upload.heygen.com"

# Keep this SHORT and SMILE-FREE. The motion prompt is a GLOBAL, persistent
# instruction, so any "smile" bleeds across the whole clip — including somber
# lines (e.g. Arvind bhai's death) — which is what made the avatar grin during
# the sad part. Negatives like "no teeth" are weakly obeyed; instead describe a
# closed, resting mouth positively. Per-line emotion comes from the audio's
# vocal tone, not this prompt — keep it composed and let the voice drive it.
DEFAULT_MOTION_PROMPT = (
    "Sincere, composed storyteller with a calm, serious, respectful expression. "
    "Lips relaxed and closed at rest; subtle head movements and soft hand gestures "
    "synced to the voice."
)


def load_api_key() -> str:
    # Prefer real env, fall back to parsing .env (one-off convenience).
    key = os.environ.get("HEYGEN_ISHWARI")
    if not key:
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("HEYGEN_ISHWARI=") and not line.startswith("#"):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not key:
        sys.exit("Missing HEYGEN_ISHWARI (env var or .env)")
    return key


def upload_asset(api_key: str, path: Path, content_type: str, label: str) -> str:
    """Upload a file to HeyGen's generic asset endpoint and return its asset_id.
    Works for both images and audio."""
    if not path.exists():
        sys.exit(f"{label} file not found: {path}\nPut {path.name} in the repo root.")
    headers = {"X-Api-Key": api_key, "Content-Type": content_type}
    print(f"Uploading {path.name} ({path.stat().st_size / 1024:.0f} KB) as {label}...")
    with httpx.Client(timeout=httpx.Timeout(connect=60.0, read=300.0, write=300.0, pool=30.0)) as c:
        data = (
            c.post(f"{UPLOAD_BASE}/v1/asset", headers=headers, content=path.read_bytes())
            .raise_for_status()
            .json()
            .get("data", {})
        )
    asset_id = data.get("id") or data.get("asset_id")
    if not asset_id:
        sys.exit(f"{label} upload returned no asset id: {data}")
    print(f"{label} asset_id: {asset_id}")
    return str(asset_id)


def upload_image(api_key: str) -> str:
    return upload_asset(api_key, PHOTO_FILE, "image/png", "image")


def upload_audio(api_key: str) -> str:
    return upload_asset(api_key, AUDIO_FILE, "audio/mpeg", "audio")


def create_video(api_key: str, image_asset_id: str, audio_asset_id: str, motion_prompt: str) -> str:
    """Create an Avatar IV image-to-video render: a single photo lip-synced to
    uploaded audio, with a natural-language motion prompt. Uses the v3 endpoint,
    which natively supports image asset + audio + motion_prompt (the v2
    talking_photo + use_avatar_iv_model + uploaded-audio combo errors out)."""
    body = {
        "type": "image",
        "image": {"type": "asset_id", "asset_id": image_asset_id},
        "audio_asset_id": audio_asset_id,
        "title": "audio-lipsync-test",
        "resolution": "1080p",
        "aspect_ratio": "auto",
        "motion_prompt": motion_prompt,
    }
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    with httpx.Client(timeout=60.0) as c:
        resp = c.post(f"{API_BASE}/v3/videos", headers=headers, json=body)
        if resp.status_code >= 400:
            sys.exit(f"generate failed: {resp.status_code} {resp.text}")
        data = resp.json().get("data") or resp.json()
    video_id = data.get("video_id") or data.get("id")
    if not video_id:
        sys.exit(f"No video_id in response: {data}")
    print(f"Rendering... video_id: {video_id}")
    return str(video_id)


def poll(api_key: str, video_id: str, timeout_s: float = 1500.0) -> str:
    headers = {"X-Api-Key": api_key}
    elapsed = 0.0
    while elapsed < timeout_s:
        with httpx.Client(timeout=60.0) as c:
            data = (
                c.get(f"{API_BASE}/v1/video_status.get", headers=headers, params={"video_id": video_id})
                .raise_for_status()
                .json()
                .get("data", {})
            )
        status = data.get("status")
        print(f"  [{elapsed:>4.0f}s] status={status}")
        if status == "completed":
            url = data.get("video_url")
            if not url:
                sys.exit(f"completed but no video_url: {data}")
            return url
        if status == "failed":
            sys.exit(f"render failed: {data.get('error')}")
        time.sleep(6.0)
        elapsed += 6.0
    sys.exit(f"timed out after {timeout_s}s")


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as c:
        with c.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes():
                    if chunk:
                        f.write(chunk)
                        total += len(chunk)
    print(f"Saved {total / 1024 / 1024:.1f} MB -> {dest}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", help="existing image asset_id (default: upload news_anchor.png)")
    ap.add_argument("--motion", default=DEFAULT_MOTION_PROMPT, help="motion prompt for the avatar")
    args = ap.parse_args()

    api_key = load_api_key()
    image_asset_id = args.image or upload_image(api_key)
    audio_asset_id = upload_audio(api_key)
    video_id = create_video(api_key, image_asset_id, audio_asset_id, args.motion)
    url = poll(api_key, video_id)
    download(url, DOWNLOADS / f"heygen_audio_lipsync_{video_id}.mp4")


if __name__ == "__main__":
    main()
