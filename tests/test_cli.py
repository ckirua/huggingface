"""CLI smoke tests — mocked S3, no HF downloads."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hf_archive.cli import main


def test_help(capfd) -> None:
    main([])
    out = capfd.readouterr().out
    assert "ensure-bucket" in out
    assert "archive" in out


@patch("hf_archive.cli.load_config")
@patch("hf_archive.cli.s3mod.ensure_bucket")
def test_ensure_bucket(mock_ensure_bucket, mock_load_config, capfd) -> None:
    mock_load_config.return_value = MagicMock()
    main(["ensure-bucket"])
    mock_ensure_bucket.assert_called_once()
    assert "bucket ready" in capfd.readouterr().out


@patch("hf_archive.cli.load_config")
@patch("hf_archive.cli.archive_mod.list_catalog")
def test_list(mock_list_catalog, mock_load_config, capfd) -> None:
    mock_load_config.return_value = MagicMock()
    mock_list_catalog.return_value = [
        {"repo_id": "owner/demo", "revision": "abc", "archived_at": "2024-01-01T00:00:00Z"}
    ]
    main(["list"])
    assert "owner/demo" in capfd.readouterr().out


@patch("hf_archive.cli.load_config")
@patch("hf_archive.cli.archive_mod.archive_repo")
def test_archive_passes_max_mbps(mock_archive, mock_load_config) -> None:
    cfg = MagicMock()
    cfg.with_max_mbps.return_value = cfg
    mock_load_config.return_value = cfg
    mock_archive.return_value = {"repo_id": "o/r", "status": "archived", "files": 1}
    main(["archive", "o/r", "--max-mbps", "4"])
    cfg.with_max_mbps.assert_called_once_with(4.0)
    mock_archive.assert_called_once()


@patch("hf_archive.cli.load_config")
@patch("hf_archive.cli.load_wishlist")
@patch("hf_archive.cli.archive_mod.archive_repo")
def test_archive_all_passes_max_mbps(mock_archive, mock_wishlist, mock_load_config) -> None:
    cfg = MagicMock()
    cfg.with_max_mbps.return_value = cfg
    mock_load_config.return_value = cfg
    mock_wishlist.return_value = [{"repo_id": "o/r"}]
    mock_archive.return_value = {"repo_id": "o/r", "status": "skipped", "files": 0}
    main(["archive-all", "--force", "--max-mbps", "8"])
    cfg.with_max_mbps.assert_called_once_with(8.0)
