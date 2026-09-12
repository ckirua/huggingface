"""Verify and restore archived repos from S3."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hf_archive.config import Config
from hf_archive import s3 as s3mod


def _parts(repo_id: str) -> tuple[str, str]:
    owner, name = repo_id.split("/", 1)
    return owner, name


def verify_repo(cfg: Config, repo_id: str) -> dict[str, Any]:
    owner, name = _parts(repo_id)
    base = f"repos/{owner}/{name}"
    latest = s3mod.get_json(cfg, f"{base}/latest.json")
    if not latest or not latest.get("revision"):
        return {"ok": False, "repo_id": repo_id, "error": "missing latest.json"}
    sha = latest["revision"]
    manifest = s3mod.get_json(cfg, f"{base}/manifest.json")
    if not manifest or "files" not in manifest:
        return {"ok": False, "repo_id": repo_id, "error": "missing manifest.json"}

    missing: list[str] = []
    size_mismatch: list[str] = []
    for entry in manifest["files"]:
        rel = entry["path"]
        key = f"{base}/revisions/{sha}/{rel}"
        if not s3mod.object_exists(cfg, key):
            missing.append(rel)
            continue
        expected = entry.get("size")
        if expected is None:
            continue
        try:
            head = s3mod.client(cfg).head_object(Bucket=cfg.bucket, Key=key)
            actual = int(head.get("ContentLength", -1))
            if actual != int(expected):
                size_mismatch.append(rel)
        except Exception:
            size_mismatch.append(rel)

    ok = not missing and not size_mismatch
    return {
        "ok": ok,
        "repo_id": repo_id,
        "revision": sha,
        "checked": len(manifest["files"]),
        "missing": missing,
        "size_mismatch": size_mismatch,
    }


def restore_repo(cfg: Config, repo_id: str, out_dir: Path) -> dict[str, Any]:
    owner, name = _parts(repo_id)
    base = f"repos/{owner}/{name}"
    latest = s3mod.get_json(cfg, f"{base}/latest.json")
    if not latest or not latest.get("revision"):
        raise FileNotFoundError(f"No latest.json for {repo_id}")
    sha = latest["revision"]
    manifest = s3mod.get_json(cfg, f"{base}/manifest.json")
    if not manifest or "files" not in manifest:
        raise FileNotFoundError(f"No manifest.json for {repo_id}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in manifest["files"]:
        rel = entry["path"]
        key = f"{base}/revisions/{sha}/{rel}"
        dest = out_dir / rel
        s3mod.download_file(cfg, key, dest)
        count += 1
    return {"repo_id": repo_id, "revision": sha, "files": count, "out": str(out_dir)}
