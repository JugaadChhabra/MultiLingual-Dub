from __future__ import annotations

import io
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.client import BaseClient


class WasabiConfigError(ValueError):
    pass


@dataclass(frozen=True)
class WasabiConfig:
    access_key: str
    secret_key: str
    bucket: str
    region: str
    endpoint_url: str


def validate_wasabi_env() -> tuple[WasabiConfig | None, str | None]:
    required = {
        "WASABI_ACCESS_KEY": os.getenv("WASABI_ACCESS_KEY", "").strip(),
        "WASABI_SECRET_KEY": os.getenv("WASABI_SECRET_KEY", "").strip(),
        "WASABI_BUCKET": os.getenv("WASABI_BUCKET", "").strip(),
        "WASABI_REGION": os.getenv("WASABI_REGION", "").strip(),
        "WASABI_ENDPOINT_URL": os.getenv("WASABI_ENDPOINT_URL", "").strip(),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        return None, f"Missing Wasabi environment variables: {', '.join(sorted(missing))}"

    return (
        WasabiConfig(
            access_key=required["WASABI_ACCESS_KEY"],
            secret_key=required["WASABI_SECRET_KEY"],
            bucket=required["WASABI_BUCKET"],
            region=required["WASABI_REGION"],
            endpoint_url=required["WASABI_ENDPOINT_URL"],
        ),
        None,
    )


def get_wasabi_config() -> WasabiConfig:
    config, error = validate_wasabi_env()
    if error or config is None:
        raise WasabiConfigError(error or "Wasabi config validation failed")
    return config


class WasabiClient:
    def __init__(self, config: WasabiConfig) -> None:
        self._config = config
        self._client: BaseClient = boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region,
        )

    def _ensure_folder(self, folder_key: str) -> None:
        """Create folder if it doesn't exist by checking and creating folder marker."""
        try:
            self._client.head_object(Bucket=self._config.bucket, Key=folder_key)
        except Exception:
            # Folder doesn't exist, create it
            self._client.put_object(Bucket=self._config.bucket, Key=folder_key)

    def _ensure_folder_for_key(self, key: str) -> None:
        """Ensure all parent folders for the given key exist."""
        # Extract folder path (everything before the last /)
        parts = key.rsplit("/", 1)
        if len(parts) > 1:
            folder_key = parts[0] + "/"
            self._ensure_folder(folder_key)

    def upload_file(self, file_path: str | Path, key: str) -> dict[str, str]:
        """Upload file from disk to S3."""
        self._ensure_folder_for_key(key)
        path = Path(file_path)
        with path.open("rb") as handle:
            response = self._client.put_object(
                Bucket=self._config.bucket,
                Key=key,
                Body=handle,
                ContentType="audio/mpeg",
            )
        etag = str(response.get("ETag", "")).strip('"')
        return {"bucket": self._config.bucket, "key": key, "etag": etag}

    def upload_bytes(self, audio_bytes: bytes, key: str) -> dict[str, str]:
        """Upload audio bytes directly to S3 without saving to disk."""
        self._ensure_folder_for_key(key)
        response = self._client.put_object(
            Bucket=self._config.bucket,
            Key=key,
            Body=audio_bytes,
            ContentType="audio/mpeg",
        )
        etag = str(response.get("ETag", "")).strip('"')
        return {"bucket": self._config.bucket, "key": key, "etag": etag}

    def upload_language_zip(
        self, language: str, audio_files: dict[str, bytes], folder_name: str
    ) -> dict[str, str]:
        """
        Create and upload a zip file containing all audio files for a language.
        
        :param language: Language code (e.g., "hi-IN", "en-IN")
        :param audio_files: Dict of {filename: audio_bytes}
        :param folder_name: Folder path in S3 (e.g., "batch/job_id")
        :return: Dict with bucket, key, and etag
        """
        # Create zip file in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename, audio_bytes in audio_files.items():
                zf.writestr(filename, audio_bytes)
        
        zip_buffer.seek(0)
        zip_filename = f"{language}.zip"
        s3_key = f"{folder_name}/{zip_filename}"
        
        # Ensure folder exists
        self._ensure_folder_for_key(s3_key)
        
        # Upload zip file
        response = self._client.put_object(
            Bucket=self._config.bucket,
            Key=s3_key,
            Body=zip_buffer.getvalue(),
            ContentType="application/zip",
        )
        etag = str(response.get("ETag", "")).strip('"')
        return {"bucket": self._config.bucket, "key": s3_key, "etag": etag}
