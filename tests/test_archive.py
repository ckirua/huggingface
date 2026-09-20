"""Archive streaming unit tests (mocked HF + S3)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from hf_archive.archive import archive_repo
from hf_archive.config import Config


def _cfg(tmp_path: Path) -> Config:
    return Config(
        s3_url="https://example.invalid",
        region="fsn1",
        access_key="ak",
        secret_key="sk",
        bucket="huggingface-models",
        hf_token=None,
        cache_dir=tmp_path / "cache",
    )


@patch("hf_archive.archive.s3mod.put_json")
@patch("hf_archive.archive.s3mod.upload_file")
@patch("hf_archive.archive.s3mod.object_exists", return_value=False)
@patch("hf_archive.archive.s3mod.get_json", return_value=None)
@patch("hf_archive.archive.hfmod.download_file")
@patch("hf_archive.archive.hfmod.list_matching_files")
@patch("hf_archive.archive.hfmod.resolve_revision", return_value="abc123")
def test_archive_streams_and_deletes_local(
    _resolve,
    mock_list,
    mock_download,
    _get_json,
    _exists,
    mock_upload,
    mock_put_json,
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    mock_list.return_value = ["a.bin", "subdir/b.bin"]

    def _dl(repo_id, filename, revision, dest_dir, token=None, max_bytes_per_sec=None):
        path = Path(dest_dir) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data-" + filename.encode())
        return path

    mock_download.side_effect = _dl

    result = archive_repo(cfg, "owner/demo", allow_patterns=["*.bin"])
    assert result["status"] == "archived"
    assert result["files"] == 2
    assert mock_upload.call_count == 2
    # Staging tree removed after success
    staging = cfg.cache_dir / "_stream" / "owner__demo" / "abc123"
    assert not staging.exists()
    # Manifest + latest + catalog
    assert mock_put_json.call_count == 3


@patch("hf_archive.archive.s3mod.object_exists", return_value=True)
@patch("hf_archive.archive.s3mod.get_json")
@patch("hf_archive.archive.hfmod.resolve_revision", return_value="abc123")
@patch("hf_archive.archive.hfmod.list_matching_files")
def test_archive_skips_when_latest_matches(
    mock_list, _resolve, mock_get_json, _exists, tmp_path: Path
) -> None:
    cfg = _cfg(tmp_path)
    mock_get_json.return_value = {"revision": "abc123"}
    result = archive_repo(cfg, "owner/demo")
    assert result["status"] == "skipped"
    mock_list.assert_not_called()


def test_list_matching_files_filters() -> None:
    from hf_archive import hf as hfmod

    with patch("hf_archive.hf.list_repo_files", return_value=["a.Q4_K_M.gguf", "a.Q8_0.gguf", "README.md"]):
        got = hfmod.list_matching_files(
            "o/r",
            "sha",
            allow_patterns=["*Q4_K_M*"],
        )
    assert got == ["a.Q4_K_M.gguf"]
