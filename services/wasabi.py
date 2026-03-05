from __future__ import annotations

import os
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

    def upload_file(self, file_path: str | Path, key: str) -> dict[str, str]:
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
