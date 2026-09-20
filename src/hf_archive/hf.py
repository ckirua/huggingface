"""Hugging Face Hub download helpers (disk-light, per-file, rate-limited)."""

from __future__ import annotations

from pathlib import Path

from huggingface_hub import hf_hub_download
from huggingface_hub.file_download import hf_hub_url, http_get
from huggingface_hub.utils import build_hf_headers, filter_repo_objects
from huggingface_hub import HfApi, list_repo_files

from hf_archive.rate_limit import RateLimiter, ThrottledWriter


def resolve_revision(
    repo_id: str, revision: str | None = None, token: str | None = None
) -> str:
    api = HfApi(token=token)
    info = api.repo_info(repo_id=repo_id, revision=revision)
    sha = getattr(info, "sha", None)
    if not sha:
        raise RuntimeError(f"Could not resolve revision for {repo_id}")
    return str(sha)


def list_matching_files(
    repo_id: str,
    revision: str,
    *,
    token: str | None = None,
    allow_patterns: list[str] | None = None,
    ignore_patterns: list[str] | None = None,
) -> list[str]:
    """List repo files at revision, filtered by allow/ignore patterns."""
    files = list_repo_files(repo_id=repo_id, revision=revision, token=token)
    matched = list(
        filter_repo_objects(
            files,
            allow_patterns=allow_patterns,
            ignore_patterns=ignore_patterns,
        )
    )
    matched.sort()
    return matched


def download_file(
    repo_id: str,
    filename: str,
    revision: str,
    dest_dir: Path,
    *,
    token: str | None = None,
    max_bytes_per_sec: float | None = None,
) -> Path:
    """Download one file into dest_dir (preserves relative path). Returns local path.

    When ``max_bytes_per_sec`` is set (from Config.max_bytes_per_sec / --max-mbps),
    stream via Hub URL + http_get into a ThrottledWriter so ingress is paced.
    When unset/None, use plain hf_hub_download (unlimited).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if max_bytes_per_sec is None:
        # Unlimited path — same as before rate limiting existed.
        path = hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            revision=revision,
            local_dir=str(dest_dir),
            token=token,
        )
        return Path(path)

    # Rate-limited path: resolve URL, stream chunks through RateLimiter.
    out = dest_dir / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    url = hf_hub_url(repo_id=repo_id, filename=filename, revision=revision)
    headers = build_hf_headers(token=token)
    limiter = RateLimiter(max_bytes_per_sec)
    with open(out, "wb") as raw:
        throttled = ThrottledWriter(raw, limiter)
        http_get(url, throttled, headers=headers)
    return out
