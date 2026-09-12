"""Manifest helper unit tests (local only)."""

from __future__ import annotations

from pathlib import Path

from hf_archive.manifest import (
    build_manifest,
    catalog_entry,
    file_entry,
    latest_pointer,
    merge_catalog,
    sha256_file,
)


def test_sha256_file(tmp_path: Path) -> None:
    p = tmp_path / "test_file.txt"
    p.write_text("test content")
    digest = sha256_file(p)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_file_entry(tmp_path: Path) -> None:
    p = tmp_path / "test_file.txt"
    p.write_text("test content")
    entry = file_entry(p, "test_file.txt")
    assert entry["path"] == "test_file.txt"
    assert entry["size"] == 12
    assert entry["sha256"] == sha256_file(p)


def test_build_manifest(tmp_path: Path) -> None:
    (tmp_path / "test_file.txt").write_text("test content")
    manifest = build_manifest(tmp_path)
    assert manifest["file_count"] == 1
    assert manifest["total_size"] == 12
    assert manifest["files"][0]["path"] == "test_file.txt"
    assert manifest["files"][0]["sha256"] == sha256_file(tmp_path / "test_file.txt")


def test_latest_pointer() -> None:
    pointer = latest_pointer("v1.0")
    assert pointer["revision"] == "v1.0"
    assert "archived_at" in pointer


def test_catalog_entry() -> None:
    entry = catalog_entry("owner/name", "abc123")
    assert entry["repo_id"] == "owner/name"
    assert entry["revision"] == "abc123"
    assert "archived_at" in entry


def test_merge_catalog_replaces_same_repo() -> None:
    catalog = {
        "repos": [
            {
                "repo_id": "owner/name",
                "revision": "old",
                "archived_at": "2023-01-01T00:00:00Z",
            }
        ]
    }
    entry = catalog_entry("owner/name", "new")
    merged = merge_catalog(catalog, entry)
    assert len(merged["repos"]) == 1
    assert merged["repos"][0]["revision"] == "new"
