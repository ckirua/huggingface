"""Local manifest / catalog helpers."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SKIP_DIR_NAMES = {".git", ".cache", "__pycache__", ".huggingface"}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_entry(path: str | Path, relpath: str) -> dict[str, Any]:
    p = Path(path)
    return {
        "path": relpath.replace("\\", "/"),
        "size": p.stat().st_size,
        "sha256": sha256_file(p),
    }


def build_manifest(root_dir: str | Path) -> dict[str, Any]:
    root = Path(root_dir)
    files: list[dict[str, Any]] = []
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith(".")]
        for filename in filenames:
            if filename.startswith("."):
                continue
            full = Path(dirpath) / filename
            rel = full.relative_to(root).as_posix()
            entry = file_entry(full, rel)
            files.append(entry)
            total_size += entry["size"]
    files.sort(key=lambda e: e["path"])
    return {
        "files": files,
        "file_count": len(files),
        "total_size": total_size,
    }


def latest_pointer(revision: str) -> dict[str, str]:
    return {
        "revision": revision,
        "archived_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def catalog_entry(repo_id: str, revision: str, **extra: Any) -> dict[str, Any]:
    entry = {
        "repo_id": repo_id,
        "revision": revision,
        "archived_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    entry.update(extra)
    return entry


def merge_catalog(catalog: dict[str, Any] | None, entry: dict[str, Any]) -> dict[str, Any]:
    catalog = dict(catalog or {})
    repos = list(catalog.get("repos") or [])
    # Replace existing repo_id entry
    repos = [r for r in repos if r.get("repo_id") != entry.get("repo_id")]
    repos.append(entry)
    repos.sort(key=lambda r: r.get("repo_id") or "")
    catalog["repos"] = repos
    return catalog
