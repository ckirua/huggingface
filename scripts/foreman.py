#!/usr/bin/env python3
"""Local Foreman: drive HF→S3 archive build via local llama-server.

Reads Progress from ~/loop-hf.md, asks the local OpenAI-compatible API for
one-file JSON patches, applies them under app/huggingface, runs VERIFY, and
updates the Progress table. Max 2 repairs per todo on FAIL. Stops before T10.

Default :8080 uses the live GGUF alias (not gpt-4o-mini). Point
FOREMAN_API_URL at :8090 and FOREMAN_MODEL=gpt-4o-mini to use the Cursor
catalog on the context router.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path("/home/kirua/app/huggingface")
PLAYBOOK = Path("/home/kirua/loop-hf.md")
API_URL = os.environ.get(
    "FOREMAN_API_URL", "http://127.0.0.1:8080/v1/chat/completions"
)


def _resolve_model(api_url: str, configured: str) -> str:
    """8090 keeps gpt-4o-mini*; 8080 uses the live llama-server --alias."""
    if ":8090" in api_url:
        return configured or "gpt-4o-mini"
    if configured and not configured.startswith("gpt-4o-mini"):
        return configured
    base = api_url
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    try:
        with urllib.request.urlopen(base + "/models", timeout=5) as resp:
            data = json.loads(resp.read().decode())
        mid = (data.get("data") or [{}])[0].get("id") or ""
        if mid:
            return str(mid)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, KeyError, IndexError):
        pass
    return configured or "Qwen2.5-Coder-7B-Instruct"


MODEL = _resolve_model(API_URL, os.environ.get("FOREMAN_MODEL", ""))
MAX_REPAIRS = 2
MAX_TODOS = ("T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "T11")  # never T10

# ---------------------------------------------------------------------------
# Micro-prompt file specs (one file per LLM call where needed)
# ---------------------------------------------------------------------------

TODO_FILES: dict[str, list[dict[str, Any]]] = {
    "T1": [
        {
            "path": "pyproject.toml",
            "spec": """Create pyproject.toml for package hf_archive.
Requirements:
- build-system hatchling
- project name hf-archive, version 0.1.0, requires-python >=3.11
- dependencies: huggingface_hub, boto3, PyYAML
- [project.scripts] hf-archive = "hf_archive.cli:main"
- [tool.hatch.build.targets.wheel] packages = ["src/hf_archive"]
- Use src layout: [tool.hatch.build.targets.wheel] packages under src
Prefer:
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "hf-archive"
version = "0.1.0"
description = "Archive Hugging Face repos to Hetzner S3"
requires-python = ">=3.11"
dependencies = [
  "huggingface_hub",
  "boto3",
  "PyYAML",
]

[project.scripts]
hf-archive = "hf_archive.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/hf_archive"]
""",
        },
        {
            "path": "src/hf_archive/__init__.py",
            "spec": '__version__ = "0.1.0"\n',
        },
        {
            "path": "src/hf_archive/cli.py",
            "spec": """Stub CLI so `hf-archive --help` works.
Use argparse. Subcommands (stubs that print 'not implemented' for now except --help):
ensure-bucket, archive, archive-all, list, verify, restore.
def main() -> None: parse args and dispatch; exit 0 on --help.
Keep under ~80 lines. No S3/HF logic yet.
""",
        },
        {
            "path": ".gitignore",
            "spec": """.cache/
__pycache__/
*.py[cod]
.venv/
dist/
*.egg-info/
huggingface.env
.env
*.env
!.env.example
!huggingface.env.example
.python-version
""",
        },
        {
            "path": "huggingface.env.example",
            "spec": """# Copy to huggingface.env or load from ~/.env
S3_URL=
S3_REGION=
S3_ACCESS_KEY=
S3_SECRET_KEY=
S3_BUCKET=huggingface-models
# Optional
# HF_TOKEN=
# HF_ARCHIVE_CACHE=/home/kirua/app/huggingface/.cache
""",
        },
        {
            "path": "models.yaml",
            "spec": """YAML wishlist with two seed repos:
- repo_id: bartowski/Qwen2.5-Coder-7B-Instruct-GGUF
- repo_id: bartowski/Qwen2.5-7B-Instruct-GGUF
Use a top-level key `models:` as a list of mappings with repo_id.
""",
        },
    ],
    "T2": [
        {
            "path": "src/hf_archive/config.py",
            "spec": """Implement load_config() returning a simple dataclass/namespace Config with:
- s3_url, region, access_key, secret_key (from env S3_*)
- bucket: S3_BUCKET or default "huggingface-models"
- hf_token: optional HF_TOKEN
- cache_dir: HF_ARCHIVE_CACHE or <package-root>/.cache (Path)

Load order: keep existing os.environ; fill missing keys from ~/.env then
<project>/huggingface.env by parsing KEY=VALUE (no python-dotenv — stdlib only).
Skip blanks/comments; do not print secrets. Expand ~ in paths.
Project root = Path(__file__).resolve().parents[2]
Default cache = PROJECT_ROOT / .cache
Raise ValueError if required S3_URL/REGION/ACCESS_KEY/SECRET_KEY missing.

Export: Config dataclass + load_config() -> Config
""",
            "attach": [],
        },
    ],
    "T3": [
        {
            "path": "src/hf_archive/s3.py",
            "spec": """boto3 S3 helpers for Hetzner Object Storage.
Functions taking Config (from hf_archive.config):
- client(cfg) -> boto3 S3 client with endpoint_url=cfg.s3_url, region_name=cfg.region,
  aws_access_key_id, aws_secret_access_key
- ensure_bucket(cfg): create bucket if not exists (HeadBucket / CreateBucket)
- put_json(cfg, key, obj): upload JSON bytes
- get_json(cfg, key): download JSON or None if missing
- upload_file(cfg, local_path, key): multipart-capable upload_file
- download_file(cfg, key, local_path): download
- list_prefix(cfg, prefix): list object keys under prefix
- object_exists(cfg, key): bool
Use botocore ClientError for 404. Never log secrets.
""",
            "attach": ["src/hf_archive/config.py"],
        },
        {
            "path": "src/hf_archive/cli.py",
            "spec": """Update CLI: wire ensure-bucket to call load_config + s3.ensure_bucket.
Other subcommands may stay stubs. Print 'ok' or 'bucket ready' on success for ensure-bucket.
Keep argparse structure for all commands: ensure-bucket, archive <repo_id>, archive-all,
list, verify <repo_id>, restore <repo_id> [--out DIR], and --force flag for later.
""",
            "attach": ["src/hf_archive/cli.py", "src/hf_archive/s3.py", "src/hf_archive/config.py"],
        },
    ],
    "T4": [
        {
            "path": "src/hf_archive/manifest.py",
            "spec": """Manifest helpers:
- sha256_file(path) -> hex digest
- file_entry(path, relpath) -> dict with path, size, sha256
- build_manifest(root_dir) -> dict { "files": [entries...], "file_count", "total_size" }
- latest_pointer(revision: str) -> {"revision": revision, "archived_at": iso8601 utc}
- catalog_entry(repo_id, revision, **extra) -> dict
- merge_catalog(catalog: dict|None, entry) -> updated catalog with repos list/dict
Pure local filesystem/hash/json helpers; no S3/HF calls.
""",
        },
    ],
    "T5": [
        {
            "path": "src/hf_archive/hf.py",
            "spec": """Hugging Face download helpers using huggingface_hub:
- resolve_revision(repo_id, revision=None, token=None) -> commit sha string
- download_snapshot(repo_id, revision, cache_root: Path, token=None,
  allow_patterns=None, ignore_patterns=None) -> Path
  Destination: cache_root / f"{owner}__{name}" / sha
  Use snapshot_download with local_dir set to that path (or revision subdir).
Use HfApi().repo_info or model_info for sha. Support allow/ignore patterns.
""",
            "attach": ["src/hf_archive/config.py"],
        },
    ],
    "T6": [
        {
            "path": "src/hf_archive/archive.py",
            "spec": """Archive one repo:
def archive_repo(cfg, repo_id, *, force=False, revision=None,
                 allow_patterns=None, ignore_patterns=None) -> dict summary:
1. resolve revision sha via hf.resolve_revision
2. If not force and latest.json on S3 already same revision and objects exist → skip
3. download_snapshot to cfg.cache_dir
4. For each file: upload to repos/{owner}/{name}/revisions/{sha}/{relpath}
5. Write manifest.json (via manifest.build_manifest) and latest.json
6. Update index/catalog.json
Return {"repo_id", "revision", "status": "archived"|"skipped", "files": n}
Use s3 helpers. owner/name from repo_id.split('/').
""",
            "attach": [
                "src/hf_archive/config.py",
                "src/hf_archive/s3.py",
                "src/hf_archive/hf.py",
                "src/hf_archive/manifest.py",
            ],
        },
        {
            "path": "src/hf_archive/cli.py",
            "spec": """Wire archive and list:
- archive <repo_id> [--force] [--revision REV]: call archive.archive_repo
- list: load index/catalog.json from S3 and print repo_ids / revisions
Keep ensure-bucket. Stubs ok for archive-all/verify/restore if not ready.
""",
            "attach": ["src/hf_archive/cli.py", "src/hf_archive/archive.py"],
        },
    ],
    "T7": [
        {
            "path": "src/hf_archive/wishlist.py",
            "spec": """Parse models.yaml:
def load_wishlist(path: Path) -> list[dict]
Each item: repo_id required; optional revision, allow_patterns, ignore_patterns.
""",
        },
        {
            "path": "src/hf_archive/cli.py",
            "spec": """Wire archive-all: load models.yaml from project root (next to pyproject.toml),
loop archive_repo for each; continue on failure; print summary of ok/fail/skip.
""",
            "attach": ["src/hf_archive/cli.py", "src/hf_archive/wishlist.py", "src/hf_archive/archive.py"],
        },
    ],
    "T8": [
        {
            "path": "src/hf_archive/verify_restore.py",
            "spec": """verify_repo(cfg, repo_id): load manifest from S3; for each file check object_exists
and optionally ContentLength vs size; return ok/fail list.
restore_repo(cfg, repo_id, out_dir: Path): download all revision files from latest
revision to out_dir preserving relative paths.
""",
            "attach": ["src/hf_archive/s3.py", "src/hf_archive/manifest.py"],
        },
        {
            "path": "src/hf_archive/cli.py",
            "spec": """Wire verify <repo_id> and restore <repo_id> [--out DIR].
Default --out to ./restored/{owner}__{name} if omitted.
Exit non-zero if verify fails.
""",
            "attach": ["src/hf_archive/cli.py", "src/hf_archive/verify_restore.py"],
        },
    ],
    "T9": [
        {
            "path": "README.md",
            "spec": """Ops README for hf-archive. Cover:
- Setup: uv sync, ~/.env S3_* vars, S3_BUCKET=huggingface-models
- Commands: ensure-bucket, archive, archive-all, list, verify, restore
- Cache under .cache/; models.yaml wishlist
- Warning: full GGUF repos have many quants → storage cost
- License note: personal backup; respect model licenses
- Foreman: run scripts/foreman.py for local LLM-driven build
Match real CLI names. Keep concise (~80-120 lines).
""",
            "attach": ["src/hf_archive/cli.py", "models.yaml", "huggingface.env.example"],
        },
    ],
    "T11": [
        {
            "path": "pyproject.toml",
            "spec": """Update pyproject.toml: keep existing project deps and scripts.
Add uv dependency group for tests:

[dependency-groups]
dev = [
  "pytest>=8",
]

Keep hatchling build, src layout packages = ["src/hf_archive"].
Do not remove huggingface_hub/boto3/PyYAML.
""",
            "attach": ["pyproject.toml"],
        },
        {
            "path": "tests/test_config.py",
            "spec": """pytest tests for hf_archive.config.load_config.
- Use monkeypatch.setenv for S3_URL, S3_REGION, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET
- Assert bucket default huggingface-models when S3_BUCKET unset
- Assert custom bucket when set
- Assert ValueError when a required S3_* key missing
- Assert cache_dir from HF_ARCHIVE_CACHE
- NEVER print or assert full secret values in failure messages; do not echo secrets
Keep under ~80 lines. Import from hf_archive.config.
""",
            "attach": ["src/hf_archive/config.py"],
        },
        {
            "path": "tests/test_manifest.py",
            "spec": """pytest for hf_archive.manifest:
- tmp_path file → sha256_file returns 64-char hex
- file_entry has path/size/sha256
- build_manifest counts files and total_size
- latest_pointer has revision + archived_at
- catalog_entry + merge_catalog replaces same repo_id
No network. Keep under ~90 lines.
""",
            "attach": ["src/hf_archive/manifest.py"],
        },
        {
            "path": "tests/test_cli.py",
            "spec": """CLI smoke tests with mocks (no real S3, no bartowski, no HF download):
- Import main from hf_archive.cli
- test --help / no-command exits 0 and prints help (capfd or argparse)
- test ensure-bucket: monkeypatch load_config to return a dummy Config AND
  monkeypatch hf_archive.s3.ensure_bucket to a no-op; call main(['ensure-bucket']);
  assert 'bucket ready' in stdout
- test list: monkeypatch load_config + archive.list_catalog to return one fake entry;
  main(['list']); assert repo_id appears in stdout
Do not call real boto3. Do not print secrets. Keep under ~100 lines.
""",
            "attach": ["src/hf_archive/cli.py", "src/hf_archive/config.py"],
        },
        {
            "path": "README.md",
            "spec": """Update README: keep all existing sections. Add a short ## Tests section:

```bash
uv sync --group dev
uv run pytest
```

Note: unit tests mock S3; no bartowski / no real uploads required.
Also mention foreman can run T11. Keep concise; do not remove Setup/Commands/Foreman.
""",
            "attach": ["README.md"],
        },
        {
            "path": ".gitignore",
            "spec": """Ensure .gitignore still ignores .cache/, .venv/, __pycache__, huggingface.env, etc.
Also add: .pytest_cache/, .coverage, htmlcov/
Keep other existing entries.
""",
            "attach": [".gitignore"],
        },
    ],
}

VERIFY: dict[str, list[str]] = {
    "T1": [
        "uv sync",
        "uv run hf-archive --help",
    ],
    "T2": [
        'uv run python -c "from hf_archive.config import load_config; c=load_config(); print(c.bucket, bool(c.s3_url), bool(c.access_key))"',
    ],
    "T3": [
        "uv run hf-archive ensure-bucket",
    ],
    "T4": [
        'uv run python -c "from pathlib import Path; import tempfile; from hf_archive.manifest import sha256_file, build_manifest, latest_pointer; d=Path(tempfile.mkdtemp()); (d/\'a.txt\').write_text(\'hi\'); h=sha256_file(d/\'a.txt\'); m=build_manifest(d); p=latest_pointer(\'abc\'); assert h and m[\'files\'] and p[\'revision\']==\'abc\'; print(\'ok\', len(h))"',
    ],
    "T5": [
        'uv run python -c "from pathlib import Path; from hf_archive.config import load_config; from hf_archive.hf import resolve_revision, download_snapshot; c=load_config(); rid=\'hf-internal-testing/tiny-random-gpt2\'; sha=resolve_revision(rid, token=c.hf_token); p=download_snapshot(rid, sha, c.cache_dir, token=c.hf_token); print(sha, p.exists(), any(p.rglob(\'*\')))"',
    ],
    "T6": [
        "uv run hf-archive archive hf-internal-testing/tiny-random-gpt2",
        "uv run hf-archive list",
    ],
    "T7": [
        "uv run python -c \""
        "from pathlib import Path; "
        "from hf_archive.wishlist import load_wishlist; "
        "p=Path('models.yaml'); bak=p.read_text(); "
        "p.write_text('models:\\n  - repo_id: hf-internal-testing/tiny-random-gpt2\\n'); "
        "print('wishlist', load_wishlist(p)); "
        "import subprocess, sys; "
        "r=subprocess.run(['uv','run','hf-archive','archive-all'], cwd='.'); "
        "p.write_text(bak); "
        "sys.exit(r.returncode)"
        "\"",
    ],
    "T8": [
        "uv run hf-archive verify hf-internal-testing/tiny-random-gpt2",
        "uv run hf-archive restore hf-internal-testing/tiny-random-gpt2 --out /tmp/hf-restore-test",
        'uv run python -c "from pathlib import Path; p=Path(\'/tmp/hf-restore-test\'); assert p.exists() and any(p.rglob(\'*\')), p; print(\'restored\', sum(1 for _ in p.rglob(\'*\')))"',
    ],
    "T9": [
        "uv run hf-archive --help",
        'uv run python -c "from pathlib import Path; t=Path(\'README.md\').read_text(); assert \'ensure-bucket\' in t and \'archive-all\' in t and \'restore\' in t; print(\'readme-ok\')"',
    ],
    "T11": [
        "uv sync --group dev",
        "uv run pytest -q",
        'uv run python -c "from pathlib import Path; t=Path(\'README.md\').read_text(); assert \'pytest\' in t.lower() or \'uv run pytest\' in t; print(\'readme-tests-ok\')"',
    ],
}


@dataclass
class Progress:
    statuses: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    last_completed: str = "_(none)_"
    current: str = "T1"


def load_dotenv_silent() -> None:
    """Load ~/.env and project huggingface.env into os.environ without printing."""
    for path in (Path.home() / ".env", ROOT / "huggingface.env"):
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def parse_progress(text: str) -> Progress:
    prog = Progress()
    for m in re.finditer(
        r"\|\s*(T\d+)\s+[^|]*\|\s*(pending|in_progress|pass|fail)\s*\|\s*([^|]*)\|",
        text,
    ):
        tid, status, notes = m.group(1), m.group(2), m.group(3).strip()
        prog.statuses[tid] = status
        prog.notes[tid] = notes
    m = re.search(r"Last completed:\s*(.+)", text)
    if m:
        prog.last_completed = m.group(1).strip()
    m = re.search(r"Current:\s*(T\d+|none|_none_|\(none\))", text, re.I)
    if m:
        prog.current = m.group(1)
    return prog


def next_todo(prog: Progress) -> str | None:
    for tid in MAX_TODOS:
        st = prog.statuses.get(tid, "pending")
        if st in ("pending", "fail", "in_progress"):
            return tid
    return None


def update_progress_file(
    tid: str, status: str, notes: str = "", *, last: bool = False
) -> None:
    text = PLAYBOOK.read_text()
    # Update table row
    def repl_row(m: re.Match[str]) -> str:
        name = m.group(1)
        rest_name = m.group(2)
        if name != tid:
            return m.group(0)
        note = notes if notes else m.group(4).strip()
        return f"| {name}{rest_name}| {status} | {note} |"

    text = re.sub(
        r"\|\s*(T\d+)(\s+[^|]*)\|\s*(?:pending|in_progress|pass|fail)\s*\|\s*([^|]*)\|",
        lambda m: (
            f"| {m.group(1)}{m.group(2)}| {status} | {(notes or m.group(3).strip())} |"
            if m.group(1) == tid
            else m.group(0)
        ),
        text,
    )
    if last and status == "pass":
        text = re.sub(
            r"Last completed:\s*.*",
            f"Last completed: {tid}",
            text,
            count=1,
        )
        # set Current to next pending
        nxt = None
        prog = parse_progress(text)
        for cand in MAX_TODOS + ("T10",):
            if prog.statuses.get(cand) in ("pending", "fail", "in_progress") and cand != tid:
                # after update, tid is pass — re-parse
                pass
        # Re-read statuses from updated text
        prog2 = parse_progress(text)
        prog2.statuses[tid] = "pass"
        nxt = None
        for cand in list(MAX_TODOS) + ["T10"]:
            if cand == tid:
                continue
            if prog2.statuses.get(cand, "pending") != "pass":
                nxt = cand
                break
        # Prefer T11 before T10 when both pending
        if nxt == "T10" and prog2.statuses.get("T11", "pending") != "pass":
            nxt = "T11"
        text = re.sub(
            r"Current:\s*.*",
            f"Current: {nxt or 'done'}",
            text,
            count=1,
        )
    else:
        text = re.sub(r"Current:\s*.*", f"Current: {tid}", text, count=1)
    PLAYBOOK.write_text(text)


def chat(messages: list[dict[str, str]], *, max_tokens: int = 4096) -> str:
    body = json.dumps(
        {
            "model": MODEL,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
    ).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM API error: {e}") from e
    return data["choices"][0]["message"]["content"]


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    # Strip a single outer markdown fence only (content may contain ```).
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text, count=1)
        text = re.sub(r"\n?```\s*$", "", text, count=1)
        text = text.strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON object in model response")
    blob = text[start:]
    try:
        obj, _ = json.JSONDecoder().raw_decode(blob)
        if isinstance(obj, dict):
            return obj
        raise ValueError("JSON root must be object")
    except json.JSONDecodeError:
        end = text.rfind("}")
        if end < start:
            raise ValueError("No JSON object in model response")
        cleaned = text[start : end + 1]
        cleaned = re.sub(r",\s*}", "}", cleaned)
        cleaned = re.sub(r",\s*]", "]", cleaned)
        return json.loads(cleaned)


def read_attach(rel: str, limit: int = 12000) -> str:
    path = ROOT / rel
    if not path.is_file():
        return f"(missing: {rel})"
    data = path.read_text(errors="replace")
    if len(data) > limit:
        return data[:limit] + "\n... [truncated] ..."
    return data


def build_gen_prompt(tid: str, file_spec: dict[str, Any]) -> list[dict[str, str]]:
    path = file_spec["path"]
    spec = file_spec["spec"]
    attach = file_spec.get("attach") or []
    parts = [
        f"Todo: {tid}",
        f"Write ONE file only: {path}",
        "Project root: /home/kirua/app/huggingface",
        "Stack: Python uv package hf_archive, console script hf-archive.",
        "Deps only: huggingface_hub, boto3, PyYAML + stdlib.",
        "Never invent secrets or hardcode S3 keys.",
        "",
        "SPEC:",
        spec.strip(),
        "",
        "Respond with STRICT JSON only, no markdown, no commentary:",
        '{"files":[{"path":"' + path + '","content":"...full file content..."}],"notes":"short"}',
        "Escape JSON properly (newlines as \\n).",
    ]
    if attach:
        parts.append("\nEXISTING FILES:")
        for a in attach:
            parts.append(f"\n--- {a} ---\n{read_attach(a)}")
    existing = ROOT / path
    if existing.is_file() and path not in attach:
        parts.append(f"\nCURRENT {path}:\n{read_attach(path)}")
    return [
        {
            "role": "system",
            "content": "You are a careful coding model. Output valid JSON only with complete file contents.",
        },
        {"role": "user", "content": "\n".join(parts)},
    ]


def build_repair_prompt(
    tid: str,
    verify_cmd: str,
    stderr: str,
    files: list[str],
) -> list[dict[str, str]]:
    excerpts = []
    for f in files[:6]:
        excerpts.append(f"--- {f} ---\n{read_attach(f, 8000)}")
    user = "\n".join(
        [
            f"Todo {tid} VERIFY FAILED.",
            f"Command: {verify_cmd}",
            f"Stderr/stdout (truncated, secrets redacted):\n{redact(stderr)[:4000]}",
            "",
            "Fix the minimal set of files. Output STRICT JSON only:",
            '{"files":[{"path":"relative/path","content":"..."}],"notes":"what you fixed"}',
            "",
            "Relevant files:",
            *excerpts,
        ]
    )
    return [
        {
            "role": "system",
            "content": "You fix Python project bugs. Output valid JSON file patches only.",
        },
        {"role": "user", "content": user},
    ]


def redact(s: str) -> str:
    s = re.sub(r"(?i)(secret|password|token|key|access)[=:\s]+\S+", r"\1=***", s)
    for envk in ("S3_SECRET_KEY", "S3_ACCESS_KEY", "HF_TOKEN"):
        val = os.environ.get(envk)
        if val and len(val) > 4:
            s = s.replace(val, "***")
    return s


def apply_files(payload: dict[str, Any]) -> list[str]:
    written: list[str] = []
    files = payload.get("files") or []
    if not isinstance(files, list):
        raise ValueError("files must be a list")
    for item in files:
        rel = item["path"].lstrip("/")
        if rel.startswith("..") or rel.startswith("home/"):
            raise ValueError(f"refusing path: {rel}")
        # normalize away accidental absolute under root
        if rel.startswith("app/huggingface/"):
            rel = rel[len("app/huggingface/") :]
        content = item["content"]
        if not isinstance(content, str):
            raise ValueError("content must be string")
        # If model double-escaped, try unescape once when looks wrong
        dest = ROOT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        written.append(rel)
        print(f"  wrote {rel} ({len(content)} bytes)")
    return written


def run_verify(tid: str) -> tuple[bool, str, str]:
    cmds = VERIFY[tid]
    logs: list[str] = []
    for cmd in cmds:
        print(f"  VERIFY: {cmd}")
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        logs.append(f"$ {cmd}\n{out}\nexit={proc.returncode}")
        if proc.returncode != 0:
            return False, cmd, "\n".join(logs)
    return True, cmds[-1], "\n".join(logs)


def ensure_hatch_src_layout() -> None:
    """If hatch needs tool.hatch.build packages fix for src layout."""
    pyproject = ROOT / "pyproject.toml"
    if not pyproject.is_file():
        return
    text = pyproject.read_text()
    # hatchling src layout often needs:
    # [tool.hatch.build.targets.wheel]
    # packages = ["src/hf_archive"]
    # OR use [tool.hatch.build] and proper structure
    if "hf_archive" in text and "[tool.hatch.build]" not in text:
        # also add hatch config for src
        if "packages = [\"src/hf_archive\"]" in text:
            # hatchling may need:
            pass


def run_file_gen(tid: str, file_spec: dict[str, Any]) -> list[str]:
    messages = build_gen_prompt(tid, file_spec)
    print(f"  LLM generate: {file_spec['path']}")
    raw = chat(messages, max_tokens=8192)
    try:
        payload = extract_json(raw)
    except Exception as e:
        print(f"  JSON parse failed: {e}; raw head={redact(raw)[:300]!r}")
        # retry once with stricter nudge
        messages.append({"role": "assistant", "content": raw})
        messages.append(
            {
                "role": "user",
                "content": "Your reply was not valid JSON. Reply again with ONLY a JSON object "
                f'{{"files":[{{"path":"{file_spec["path"]}","content":"..."}}],"notes":"..."}}',
            }
        )
        raw = chat(messages, max_tokens=8192)
        payload = extract_json(raw)
    # Force path if model drifted
    if payload.get("files"):
        for f in payload["files"]:
            if "path" not in f or not f["path"]:
                f["path"] = file_spec["path"]
    else:
        # maybe content at top level
        if "content" in payload:
            payload = {
                "files": [{"path": file_spec["path"], "content": payload["content"]}],
                "notes": payload.get("notes", ""),
            }
        else:
            raise ValueError("no files in payload")
    return apply_files(payload)


def run_todo(tid: str) -> bool:
    print(f"\n=== {tid} ===")
    update_progress_file(tid, "in_progress", "foreman running")
    file_specs = TODO_FILES[tid]
    touched: list[str] = []
    for fs in file_specs:
        try:
            touched.extend(run_file_gen(tid, fs))
        except Exception as e:
            print(f"  GEN FAIL: {e}")
            update_progress_file(tid, "fail", f"gen error: {e}"[:80])
            return False

    ok, cmd, log = run_verify(tid)
    repairs = 0
    while not ok and repairs < MAX_REPAIRS:
        repairs += 1
        print(f"  REPAIR {repairs}/{MAX_REPAIRS}")
        # files to attach: todo files + recently written
        paths = [fs["path"] for fs in file_specs]
        for t in touched:
            if t not in paths:
                paths.append(t)
        try:
            raw = chat(build_repair_prompt(tid, cmd, log, paths), max_tokens=8192)
            payload = extract_json(raw)
            touched.extend(apply_files(payload))
        except Exception as e:
            print(f"  REPAIR GEN FAIL: {e}")
            break
        ok, cmd, log = run_verify(tid)

    if ok:
        update_progress_file(tid, "pass", "VERIFY ok", last=True)
        print(f"=== {tid} PASS ===")
        return True
    update_progress_file(tid, "fail", f"VERIFY fail after {repairs} repairs"[:80])
    print(f"=== {tid} FAIL ===")
    print(redact(log)[-2000:])
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="HF archive local foreman")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run only the next pending todo",
    )
    parser.add_argument(
        "--todo",
        type=str,
        default="",
        help="Force a specific todo id (e.g. T3)",
    )
    parser.add_argument(
        "--skip-gen",
        action="store_true",
        help="Only run VERIFY for current/forced todo (no LLM)",
    )
    args = parser.parse_args()

    load_dotenv_silent()
    if not PLAYBOOK.is_file():
        print(f"missing playbook {PLAYBOOK}", file=sys.stderr)
        return 2

    while True:
        text = PLAYBOOK.read_text()
        prog = parse_progress(text)
        tid = args.todo.upper() if args.todo else next_todo(prog)
        if not tid:
            print("All T1–T9,T11 complete.")
            return 0
        if tid == "T10" or tid not in TODO_FILES:
            print(f"Refusing {tid}: foreman only runs T1–T9 and T11 (never T10).")
            return 0

        if args.skip_gen:
            update_progress_file(tid, "in_progress")
            ok, _, log = run_verify(tid)
            if ok:
                update_progress_file(tid, "pass", "VERIFY ok", last=True)
                print(f"{tid} PASS")
            else:
                update_progress_file(tid, "fail", "VERIFY fail")
                print(redact(log)[-1500:])
                return 1
        else:
            ok = run_todo(tid)
            if not ok:
                print(f"Stopped on {tid} failure (max repairs exhausted).")
                return 1

        if args.once or args.todo:
            return 0 if ok else 1
        # continue to next immediately
        args.todo = ""  # clear force after one


if __name__ == "__main__":
    raise SystemExit(main())
