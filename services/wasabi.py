from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.client import BaseClient
from services.retry import retry_call
from services.runtime_config import RuntimeConfig, get_config_value


class S3ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class S3Config:
    endpoint: str | None
    access_key: str
    secret_key: str
    bucket: str
    region: str


def validate_s3_env() -> tuple[S3Config | None, str | None]:
    required = {
        "AWS_ACCESS_KEY": get_config_value("AWS_ACCESS_KEY"),
        "AWS_SECRET_KEY": get_config_value("AWS_SECRET_KEY"),
        "AWS_BUCKET": get_config_value("AWS_BUCKET"),
        "AWS_REGION": get_config_value("AWS_REGION"),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        return None, f"Missing AWS environment variables: {', '.join(sorted(missing))}"

    return (
        S3Config(
            access_key=required["AWS_ACCESS_KEY"],
            secret_key=required["AWS_SECRET_KEY"],
            bucket=required["AWS_BUCKET"],
            region=required["AWS_REGION"],
            endpoint=get_config_value("AWS_ENDPOINT_URL") or None,
        ),
        None,
    )


def get_s3_config(runtime_config: RuntimeConfig | None = None) -> S3Config:
    required = {
        "AWS_ACCESS_KEY": get_config_value("AWS_ACCESS_KEY", runtime_config=runtime_config),
        "AWS_SECRET_KEY": get_config_value("AWS_SECRET_KEY", runtime_config=runtime_config),
        "AWS_BUCKET": get_config_value("AWS_BUCKET", runtime_config=runtime_config),
        "AWS_REGION": get_config_value("AWS_REGION", runtime_config=runtime_config),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise S3ConfigError(f"Missing AWS environment variables: {', '.join(sorted(missing))}")

    config = S3Config(
        access_key=required["AWS_ACCESS_KEY"],
        secret_key=required["AWS_SECRET_KEY"],
        bucket=required["AWS_BUCKET"],
        region=required["AWS_REGION"],
        endpoint=get_config_value("AWS_ENDPOINT_URL", runtime_config=runtime_config) or None,
    )
    return config


class S3Client:
    def __init__(self, config: S3Config) -> None:
        self._config = config
        self._client: BaseClient = boto3.client(
            "s3",
            endpoint_url=config.endpoint,
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
        def _call() -> dict[str, str]:
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

        return retry_call(_call, operation="S3 upload file")

    def upload_bytes(self, audio_bytes: bytes, key: str) -> dict[str, str]:
        """Upload audio bytes directly to S3 without saving to disk."""
        def _call() -> dict[str, str]:
            self._ensure_folder_for_key(key)
            response = self._client.put_object(
                Bucket=self._config.bucket,
                Key=key,
                Body=audio_bytes,
                ContentType="audio/mpeg",
            )
            etag = str(response.get("ETag", "")).strip('"')
            return {"bucket": self._config.bucket, "key": key, "etag": etag}

        return retry_call(_call, operation="S3 upload bytes")

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
        def _call() -> dict[str, str]:
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

        return retry_call(_call, operation="S3 upload zip")
