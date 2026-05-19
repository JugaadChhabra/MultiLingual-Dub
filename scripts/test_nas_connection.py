"""
NAS connection smoke test.

Run from the project root:
    python scripts/test_nas_connection.py

Tests: connect → ensure_base_folders → write → read → list
"""
from __future__ import annotations

import sys
import os

# Allow running from repo root without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from services.nas import NasService, get_nas_config

TEST_CONTENT = b"nas_smoke_test_ok"
TEST_PATH = "videos/_smoke_test/test.mp4"


def run() -> None:
    config = get_nas_config()
    print(f"NAS mode:      {config.mode}")
    print(f"NAS root:      {config.root_path}")
    if config.mode == "smb":
        print(f"NAS server:    {config.server}")
        print(f"NAS share:     {config.share}")

    nas = NasService(config)

    print("\n[1] ensure_base_folders ...")
    nas.ensure_base_folders()
    print("    OK")

    print(f"[2] write_file → {TEST_PATH} ...")
    nas.write_file(TEST_PATH, TEST_CONTENT)
    print("    OK")

    print(f"[3] read_file ← {TEST_PATH} ...")
    data = nas.read_file(TEST_PATH)
    assert data == TEST_CONTENT, f"Content mismatch: got {data!r}"
    print("    OK")

    print("[4] list_files videos/_smoke_test ...")
    files = nas.list_files("videos/_smoke_test")
    print(f"    {files}")

    print("\nAll checks passed.")


if __name__ == "__main__":
    run()
