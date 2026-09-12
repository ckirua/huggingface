"""Config unit tests (no secrets printed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hf_archive import config as config_mod
from hf_archive.config import load_config


def _set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_URL", "https://example.invalid")
    monkeypatch.setenv("S3_REGION", "fsn1")
    monkeypatch.setenv("S3_ACCESS_KEY", "test-access")
    monkeypatch.setenv("S3_SECRET_KEY", "test-secret")


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    # Prevent ~/.env from filling deleted keys during tests.
    monkeypatch.setattr(config_mod, "_parse_env_file", lambda _path: None)


def test_default_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    cfg = load_config()
    assert cfg.bucket == "huggingface-models"


def test_custom_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("S3_BUCKET", "custom-bucket")
    cfg = load_config()
    assert cfg.bucket == "custom-bucket"


@pytest.mark.parametrize("missing", ["S3_URL", "S3_REGION", "S3_ACCESS_KEY", "S3_SECRET_KEY"])
def test_missing_required(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _set_required(monkeypatch)
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(ValueError) as excinfo:
        load_config()
    assert missing in str(excinfo.value)


def test_cache_dir_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("HF_ARCHIVE_CACHE", str(tmp_path / "c"))
    cfg = load_config()
    assert cfg.cache_dir == tmp_path / "c"


def test_cache_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.delenv("HF_ARCHIVE_CACHE", raising=False)
    cfg = load_config()
    assert cfg.cache_dir == config_mod.PROJECT_ROOT / ".cache"
