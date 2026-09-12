"""Load S3 / HF archive configuration from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Config:
    s3_url: str
    region: str
    access_key: str
    secret_key: str
    bucket: str
    hf_token: str | None
    cache_dir: Path


def _parse_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def load_config() -> Config:
    # Prefer already-set process env; fill gaps from ~/.env then project file.
    _parse_env_file(Path.home() / ".env")
    _parse_env_file(PROJECT_ROOT / "huggingface.env")

    s3_url = os.environ.get("S3_URL", "").strip()
    region = os.environ.get("S3_REGION", "").strip()
    access_key = os.environ.get("S3_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("S3_SECRET_KEY", "").strip()
    bucket = os.environ.get("S3_BUCKET", "huggingface-models").strip() or "huggingface-models"
    hf_token = os.environ.get("HF_TOKEN") or None
    if hf_token:
        hf_token = hf_token.strip() or None

    cache_raw = os.environ.get("HF_ARCHIVE_CACHE", "").strip()
    cache_dir = Path(cache_raw).expanduser() if cache_raw else (PROJECT_ROOT / ".cache")

    missing = [
        name
        for name, val in (
            ("S3_URL", s3_url),
            ("S3_REGION", region),
            ("S3_ACCESS_KEY", access_key),
            ("S3_SECRET_KEY", secret_key),
        )
        if not val
    ]
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")

    return Config(
        s3_url=s3_url,
        region=region,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        hf_token=hf_token,
        cache_dir=cache_dir,
    )
