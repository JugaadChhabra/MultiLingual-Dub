from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.client import BaseClient
from services.retry import retry_call
from services.runtime_config import (
    MissingSettingError,
    RuntimeConfig,
    read_setting,
    require,
)


class S3ConfigError(MissingSettingError):
    """Kept as its own type because callers catch it specifically to report a
    cloud-upload problem rather than a generic config one."""

    def __init__(self, keys: list[str]) -> None:
        super().__init__(keys)


@dataclass(frozen=True)
class S3Config:
    endpoint: str | None
    access_key: str
    secret_key: str
    bucket: str
    region: str

    REQUIRED = ("AWS_ACCESS_KEY", "AWS_SECRET_KEY", "AWS_BUCKET", "AWS_REGION")

    @classmethod
    def resolve(cls, session: RuntimeConfig | None = None) -> S3Config:
        try:
            values = require(cls.REQUIRED, session)
        except MissingSettingError as exc:
            raise S3ConfigError(exc.keys) from exc
        return cls(
            access_key=values["AWS_ACCESS_KEY"],
            secret_key=values["AWS_SECRET_KEY"],
            bucket=values["AWS_BUCKET"],
            region=values["AWS_REGION"],
            endpoint=read_setting("AWS_ENDPOINT_URL", session) or None,
        )


def validate_s3_env() -> tuple[S3Config | None, str | None]:
    """Startup probe: report whether the process env alone can reach S3."""
    try:
        return S3Config.resolve(), None
    except S3ConfigError as exc:
        return None, str(exc)


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

    def _put_object(self, body, key: str, content_type: str = "audio/mpeg", operation: str = "S3 upload") -> dict[str, str]:
        def _call() -> dict[str, str]:
            self._ensure_folder_for_key(key)
            response = self._client.put_object(
                Bucket=self._config.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
            )
            etag = str(response.get("ETag", "")).strip('"')
            return {"bucket": self._config.bucket, "key": key, "etag": etag}

        return retry_call(_call, operation=operation)

    def upload_file(self, file_path: str | Path, key: str) -> dict[str, str]:
        with Path(file_path).open("rb") as handle:
            return self._put_object(handle, key, operation="S3 upload file")

    def upload_bytes(self, audio_bytes: bytes, key: str) -> dict[str, str]:
        return self._put_object(audio_bytes, key, operation="S3 upload bytes")

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

    def list_zip_filenames(self, folder_name: str, language: str) -> set[str]:
        """Return the set of filenames inside {folder_name}/{language}.zip on S3.

        Returns an empty set if the zip does not exist.
        """
        s3_key = f"{folder_name}/{language}.zip"

        def _call() -> set[str]:
            try:
                response = self._client.get_object(Bucket=self._config.bucket, Key=s3_key)
            except self._client.exceptions.NoSuchKey:
                return set()
            except Exception as exc:
                if "NoSuchKey" in type(exc).__name__:
                    return set()
                raise

            existing_bytes = response["Body"].read()
            with zipfile.ZipFile(io.BytesIO(existing_bytes)) as zf:
                return {info.filename for info in zf.infolist() if not info.is_dir()}

        return retry_call(_call, operation="S3 list zip filenames")

    def folder_exists(self, folder_name: str) -> bool:
        """True if any object exists under the given prefix."""
        prefix = folder_name.rstrip("/") + "/"

        def _call() -> bool:
            response = self._client.list_objects_v2(
                Bucket=self._config.bucket,
                Prefix=prefix,
                MaxKeys=1,
            )
            return response.get("KeyCount", 0) > 0 or bool(response.get("Contents"))

        return retry_call(_call, operation="S3 folder exists check")

    def append_to_language_zip(
        self, language: str, new_files: dict[str, bytes], folder_name: str
    ) -> dict[str, str]:
        """
        Merge new audio files into an existing language zip on S3 and re-upload.

        If the zip does not exist yet, behaves like upload_language_zip. On
        filename collisions inside the zip, the new file overwrites the old.
        """
        s3_key = f"{folder_name}/{language}.zip"

        def _call() -> dict[str, str]:
            existing_files: dict[str, bytes] = {}
            try:
                response = self._client.get_object(Bucket=self._config.bucket, Key=s3_key)
                existing_bytes = response["Body"].read()
                with zipfile.ZipFile(io.BytesIO(existing_bytes)) as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        existing_files[info.filename] = zf.read(info.filename)
            except self._client.exceptions.NoSuchKey:
                existing_files = {}
            except Exception as exc:
                if "NoSuchKey" not in type(exc).__name__:
                    raise

            merged = {**existing_files, **new_files}

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for filename, audio_bytes in merged.items():
                    zf.writestr(filename, audio_bytes)

            self._ensure_folder_for_key(s3_key)
            response = self._client.put_object(
                Bucket=self._config.bucket,
                Key=s3_key,
                Body=zip_buffer.getvalue(),
                ContentType="application/zip",
            )
            etag = str(response.get("ETag", "")).strip('"')
            return {
                "bucket": self._config.bucket,
                "key": s3_key,
                "etag": etag,
                "added_files": str(len(new_files)),
                "total_files": str(len(merged)),
                "overwritten_files": str(len(set(existing_files) & set(new_files))),
            }

        return retry_call(_call, operation="S3 append zip")
