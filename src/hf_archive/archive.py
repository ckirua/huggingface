"""Archive one HF repo into the S3 layout."""

from __future__ import annotations

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
        }

    dest_dir = hfmod.download_snapshot(
        repo_id,
        sha,
        cfg.cache_dir,
        token=cfg.hf_token,
        allow_patterns=allow_patterns,
        ignore_patterns=ignore_patterns,
    )

    built = man.build_manifest(dest_dir)
    for entry in built["files"]:
        relpath = entry["path"]
        local = dest_dir / relpath
        key = f"{base}/revisions/{sha}/{relpath}"
        s3mod.upload_file(cfg, local, key)

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
    }


def list_catalog(cfg: Config) -> list[dict[str, Any]]:
    catalog = s3mod.get_json(cfg, "index/catalog.json") or {"repos": []}
    return list(catalog.get("repos") or [])
