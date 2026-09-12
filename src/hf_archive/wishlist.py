"""Parse models.yaml wishlist."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_wishlist(path: Path | str) -> list[dict[str, Any]]:
    path = Path(path)
    data = yaml.safe_load(path.read_text())
    if data is None:
        return []
    if isinstance(data, dict) and "models" in data:
        items = data["models"]
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("models.yaml must be a list or a mapping with key 'models'")
    if not isinstance(items, list):
        raise ValueError("wishlist entries must be a list")
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or "repo_id" not in item:
            raise ValueError("Each wishlist item must be a mapping with repo_id")
        entry = {"repo_id": str(item["repo_id"])}
        if item.get("revision"):
            entry["revision"] = str(item["revision"])
        if item.get("allow_patterns") is not None:
            entry["allow_patterns"] = list(item["allow_patterns"])
        if item.get("ignore_patterns") is not None:
            entry["ignore_patterns"] = list(item["ignore_patterns"])
        out.append(entry)
    return out
