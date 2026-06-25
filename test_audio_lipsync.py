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

QUOTA_EXCEEDED_CODE = 401028

API_BASE = "https://api.heygen.com"
UPLOAD_BASE = "https://upload.heygen.com"

DEFAULT_MOTION_PROMPT = (
    "Professional news anchor seated at a broadcast desk, facing the camera "
    "with confident, composed posture. Natural, measured head movements and "
    "occasional subtle hand gestures to emphasize points. Maintains steady eye "
    "contact with the viewer and a calm, authoritative on-air presence."
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


def _oldest_group_id(api_key: str) -> str | None:
    """Find the oldest user-owned photo-avatar group, to free a slot if the
    per-account cap is hit."""
    headers = {"X-Api-Key": api_key}
    with httpx.Client(timeout=60.0) as c:
        groups = (
            c.get(f"{API_BASE}/v2/avatar_group.list", headers=headers)
            .raise_for_status()
            .json()
            .get("data", {})
            .get("avatar_group_list", [])
        )
    groups = [g for g in groups if g.get("id")]
    if not groups:
        return None
    groups.sort(key=lambda g: g.get("created_at") or 0)
    return str(groups[0]["id"])


def upload_talking_photo(api_key: str) -> str:
    """Upload news_anchor.png as a talking photo. If the account's photo-avatar
    cap is hit, delete the oldest group and retry once."""
    if not PHOTO_FILE.exists():
        sys.exit(f"Photo not found: {PHOTO_FILE}\nPut news_anchor.png in the repo root.")
    headers = {"X-Api-Key": api_key, "Content-Type": "image/png"}
    content = PHOTO_FILE.read_bytes()
    print(f"Uploading {PHOTO_FILE.name} ({len(content) / 1024:.0f} KB) as talking photo...")

    def _post() -> httpx.Response:
        with httpx.Client(timeout=httpx.Timeout(connect=60.0, read=300.0, write=300.0, pool=30.0)) as c:
            return c.post(f"{UPLOAD_BASE}/v1/talking_photo", headers=headers, content=content)

    resp = _post()
    if resp.status_code == 400:
        try:
            err = resp.json()
        except Exception:
            err = {}
        if err.get("code") == QUOTA_EXCEEDED_CODE:
            gid = _oldest_group_id(api_key)
            if not gid:
                sys.exit(f"Photo-avatar cap hit and no group to rotate: {err}")
            print(f"Photo-avatar cap hit; deleting oldest group {gid} to free a slot...")
            with httpx.Client(timeout=60.0) as c:
                c.delete(f"{API_BASE}/v2/avatar_group/{gid}", headers={"X-Api-Key": api_key}).raise_for_status()
            resp = _post()

    if resp.status_code >= 400:
        sys.exit(f"talking_photo upload failed: {resp.status_code} {resp.text}")
    data = resp.json().get("data") or resp.json()
    tp_id = data.get("talking_photo_id") or data.get("id")
    if not tp_id:
        sys.exit(f"talking_photo upload returned no id: {data}")
    print(f"talking_photo_id: {tp_id}")
    return str(tp_id)


def upload_audio(api_key: str) -> str:
    if not AUDIO_FILE.exists():
        sys.exit(f"Audio file not found: {AUDIO_FILE}\nPut news_anchor.mp3 in the repo root.")
    headers = {"X-Api-Key": api_key, "Content-Type": "audio/mpeg"}
    print(f"Uploading {AUDIO_FILE.name} ({AUDIO_FILE.stat().st_size / 1024:.0f} KB)...")
    with httpx.Client(timeout=httpx.Timeout(connect=60.0, read=300.0, write=300.0, pool=30.0)) as c:
        data = (
            c.post(f"{UPLOAD_BASE}/v1/asset", headers=headers, content=AUDIO_FILE.read_bytes())
            .raise_for_status()
            .json()
            .get("data", {})
        )
    asset_id = data.get("id") or data.get("asset_id")
    if not asset_id:
        sys.exit(f"Upload returned no asset id: {data}")
    print(f"Audio asset_id: {asset_id}")
    return str(asset_id)


def create_video(api_key: str, avatar_id: str, audio_asset_id: str, motion_prompt: str) -> str:
    body = {
        "video_title": "audio-lipsync-test",
        "video_inputs": [
            {
                "character": {
                    "type": "talking_photo",
                    "talking_photo_id": avatar_id,
                    "use_avatar_iv_model": True,
                    "motion_prompt": motion_prompt,
                },
                "voice": {"type": "audio", "audio_asset_id": audio_asset_id},
            }
        ],
        "caption": False,
    }
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    with httpx.Client(timeout=60.0) as c:
        resp = c.post(f"{API_BASE}/v2/video/generate", headers=headers, json=body)
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
    ap.add_argument("--avatar", help="existing talking_photo_id (default: upload news_anchor.png)")
    ap.add_argument("--motion", default=DEFAULT_MOTION_PROMPT, help="motion prompt for the avatar")
    args = ap.parse_args()

    api_key = load_api_key()
    avatar_id = args.avatar or upload_talking_photo(api_key)
    audio_asset_id = upload_audio(api_key)
    video_id = create_video(api_key, avatar_id, audio_asset_id, args.motion)
    url = poll(api_key, video_id)
    download(url, DOWNLOADS / f"heygen_audio_lipsync_{video_id}.mp4")


if __name__ == "__main__":
    main()
