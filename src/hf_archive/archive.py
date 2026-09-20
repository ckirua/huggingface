"""Archive one HF repo into the S3 layout (disk-light streaming)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from hf_archive.config import Config
from hf_archive import hf as hfmod
from hf_archive import manifest as man
from hf_archive import s3 as s3mod


def _parts(repo_id: str) -> tuple[str, str]:
    if "/" not in repo_id:
        raise ValueError(f"repo_id must be owner/name, got {repo_id!r}")
    owner, name = repo_id.split("/", 1)
    return owner, name


def _prefix(owner: str, name: str) -> str:
    return f"repos/{owner}/{name}"


def _staging_dir(cache_root: Path, owner: str, name: str, revision: str) -> Path:
    # Small scratch only — one file at a time, then unlink.
    return Path(cache_root) / "_stream" / f"{owner}__{name}" / revision


def archive_repo(
    cfg: Config,
    repo_id: str,
    *,
    force: bool = False,
    revision: str | None = None,
    allow_patterns: list[str] | None = None,
    ignore_patterns: list[str] | None = None,
) -> dict[str, Any]:
    owner, name = _parts(repo_id)
    base = _prefix(owner, name)
    sha = hfmod.resolve_revision(repo_id, revision=revision, token=cfg.hf_token)

    latest_key = f"{base}/latest.json"
    latest_obj = s3mod.get_json(cfg, latest_key)
    if (
        not force
        and latest_obj
        and latest_obj.get("revision") == sha
        and s3mod.object_exists(cfg, f"{base}/manifest.json")
    ):
        return {
            "repo_id": repo_id,
            "revision": sha,
            "status": "skipped",
            "files": 0,
            "total_size": 0,
        }

    filenames = hfmod.list_matching_files(
        repo_id,
        sha,
        token=cfg.hf_token,
        allow_patterns=allow_patterns,
        ignore_patterns=ignore_patterns,
    )
    if not filenames:
        raise RuntimeError(
            f"No files matched for {repo_id}@{sha} "
            f"(allow_patterns={allow_patterns!r}, ignore_patterns={ignore_patterns!r})"
        )

    staging = _staging_dir(cfg.cache_dir, owner, name, sha)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    files_meta: list[dict[str, Any]] = []
    total_size = 0
    try:
        # max_bytes_per_sec comes from --max-mbps / HF_ARCHIVE_MAX_MBPS / default 8.
        # Download and upload run one after another, so each sees the same cap.
        bps = cfg.max_bytes_per_sec
        for relpath in filenames:
            local = hfmod.download_file(
                repo_id,
                relpath,
                sha,
                staging,
                token=cfg.hf_token,
                max_bytes_per_sec=bps,
            )
            try:
                entry = man.file_entry(local, relpath)
                key = f"{base}/revisions/{sha}/{relpath}"
                s3mod.upload_file(cfg, local, key)
                files_meta.append(entry)
                total_size += entry["size"]
            finally:
                # Disk-light: drop local bytes as soon as upload finishes.
                try:
                    local.unlink(missing_ok=True)
                except OSError:
                    pass
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    files_meta.sort(key=lambda e: e["path"])
    built = {
        "files": files_meta,
        "file_count": len(files_meta),
        "total_size": total_size,
    }

    s3mod.put_json(cfg, f"{base}/manifest.json", {**built, "repo_id": repo_id, "revision": sha})
    s3mod.put_json(cfg, latest_key, man.latest_pointer(sha))

    catalog_key = "index/catalog.json"
    catalog = s3mod.get_json(cfg, catalog_key) or {"repos": []}
    catalog = man.merge_catalog(
        catalog,
        man.catalog_entry(
            repo_id,
            sha,
            file_count=built["file_count"],
            total_size=built["total_size"],
        ),
    )
    s3mod.put_json(cfg, catalog_key, catalog)

    return {
        "repo_id": repo_id,
        "revision": sha,
        "status": "archived",
        "files": built["file_count"],
        "total_size": built["total_size"],
    }


def list_catalog(cfg: Config) -> list[dict[str, Any]]:
    catalog = s3mod.get_json(cfg, "index/catalog.json") or {"repos": []}
    return list(catalog.get("repos") or [])
