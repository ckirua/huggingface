"""Hugging Face Hub download helpers."""

from __future__ import annotations

from pathlib import Path

from huggingface_hub import HfApi, snapshot_download


def resolve_revision(
    repo_id: str, revision: str | None = None, token: str | None = None
) -> str:
    api = HfApi(token=token)
    info = api.repo_info(repo_id=repo_id, revision=revision)
    sha = getattr(info, "sha", None)
    if not sha:
        raise RuntimeError(f"Could not resolve revision for {repo_id}")
    return str(sha)


def download_snapshot(
    repo_id: str,
    revision: str,
    cache_root: Path,
    token: str | None = None,
    allow_patterns: list[str] | None = None,
    ignore_patterns: list[str] | None = None,
) -> Path:
    owner, name = repo_id.split("/", 1)
    dest_dir = Path(cache_root) / f"{owner}__{name}" / revision
    dest_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_dir=str(dest_dir),
        token=token,
        allow_patterns=allow_patterns,
        ignore_patterns=ignore_patterns,
    )
    return dest_dir
