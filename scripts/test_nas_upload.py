"""
Quick test: upload video.mp4 to NAS using current .env config.
Run from project root: python scripts/test_nas_upload.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from services.nas import NasService, get_nas_config

config = get_nas_config()
print(f"Mode:      {config.mode}")
print(f"Server:    {config.server}")
print(f"Share:     {config.share}")
print(f"Root path: {config.root_path}")

video_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "video.mp4")
nas = NasService(config)
nas_path = nas.upload_video("19-05-2026", "test_upload", video_path)
print(f"\nUploaded → {nas_path}")
