# hf-archive

Archive Hugging Face repos into a Hetzner Object Storage bucket (`huggingface-models`) with manifests for inventory, verify, and restore.

## Setup

```bash
uv sync
```

Credentials load from process env, then `~/.env`, then optional `huggingface.env` in this directory:

- `S3_URL`, `S3_REGION`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` (required)
- `S3_BUCKET` (default `huggingface-models`)
- `HF_TOKEN` (optional, gated repos)
- `HF_ARCHIVE_CACHE` (default `./.cache`)
- `HF_ARCHIVE_MAX_MBPS` (optional fallback MiB/s for download+upload; default `8` if unset; `0` = unlimited). Prefer the CLI flag below when picking a speed for a run.

See `huggingface.env.example`. Never commit real secrets.

### Bandwidth throttle

`archive` / `archive-all` accept `--max-mbps` (primary). That caps Hugging Face download and S3 upload roughly to the given MiB/s so overnight jobs do not saturate the LAN.

```bash
# Slow overnight (4 MiB/s)
uv run hf-archive archive-all --force --max-mbps 4

# Default-safe explicit cap (8 MiB/s)
uv run hf-archive archive-all --force --max-mbps 8

# One repo
uv run hf-archive archive owner/name --force --max-mbps 8
```

Priority: `--max-mbps` → env `HF_ARCHIVE_MAX_MBPS` → default `8`. Pass `--max-mbps 0` for unlimited.

## Tests

Unit tests mock S3 (no bartowski / no real uploads):

```bash
uv sync --group dev
uv run pytest
```

Foreman can drive this suite: `python3 scripts/foreman.py --todo T11`.

## Commands

```bash
uv run hf-archive ensure-bucket
uv run hf-archive archive <owner/name> [--revision SHA] [--force] [--max-mbps N]
uv run hf-archive archive-all [--force] [--max-mbps N]
uv run hf-archive list
uv run hf-archive verify <owner/name>
uv run hf-archive restore <owner/name> [--out DIR]
```

## Storage usage snapshots

Measure Hetzner S3 + local disk (no secrets in output). Writes under `snapshots/`.

```bash
cd /home/kirua/app/huggingface

# All buckets (can take a long time — millions of objects)
uv run python scripts/measure_storage.py

# Fast: only huggingface-models + disk
uv run python scripts/measure_storage.py --bucket huggingface-models

# Skip S3 or disk
uv run python scripts/measure_storage.py --skip-s3
uv run python scripts/measure_storage.py --bucket huggingface-models --skip-disk
```

Notes: systemd stub DNS on this host sometimes fails for `*.your-objectstorage.com`; the script falls back to UDP DNS against `1.1.1.1`. Per-repo breakdown defaults to `huggingface-models` (`--detail-bucket`). Snapshot JSON/Markdown under `snapshots/` is gitignored (local only).

Wishlist for `archive-all` is `models.yaml` (`models:` list of `repo_id`, optional `revision` / `allow_patterns` / `ignore_patterns`). Prefer `archive-all` so YAML patterns apply (`archive` alone does not read the wishlist). Use `--force` after expanding `allow_patterns` for a revision already in S3.

## S3 layout

- `repos/{owner}/{name}/revisions/{sha}/...` — immutable file tree
- `repos/{owner}/{name}/latest.json` — current pointer
- `repos/{owner}/{name}/manifest.json` — path / size / sha256
- `index/catalog.json` — flat inventory

## Disk-light archive path

`archive` / `archive-all` do **not** snapshot a full repo to disk. For each matched file they:

1. `list_repo_files` + pattern filter (`allow_patterns` / `ignore_patterns`)
2. Rate-limited Hub download one file into a small `.cache/_stream/...` staging dir (`--max-mbps`)
3. multipart `upload_file` to S3 (boto3 `TransferConfig.max_bandwidth` from the same cap)
4. `unlink` the local file immediately

Peak disk use is roughly the largest single file (plus a tiny staging tree), not the whole repo. Manifests still record path / size / sha256; `verify` / `restore` are unchanged.

Legacy full-repo dirs under `.cache/{owner}__{name}/` from older runs are unused by the new path and may be deleted after `verify` confirms the repo is in S3.

## Cost warning

Full GGUF repos (e.g. bartowski multi-quant dumps) contain **many** quantizations. A full mirror is intentional delete-insurance, but storage GB adds up. The wishlist intentionally selects a few quants by size class (see `models.yaml` comments) — not every quant in each bartowski dump.

### Wishlist inventory (selected quants)

Ceiling is **2x RTX 3090 (~48GB, llama.cpp layer-split)**. Single 3090 (24GB) and 2070 (8GB) models are included. `models.yaml` is a 2026 black-swan archive list: official instruct GGUFs that load usefully in this VRAM range, with **intentional quants** (not a full bartowski multi-quant dump). VL repos always add `*mmproj*`.

`archive-all` walks YAML in order. Wave 0 is the original seed (already-started repos first). New waves go cheap/small → mid → largest new 70–80B so overnight sessions make progress.

| Class | Patterns (YAML anchors) | Examples |
|-------|-------------------------|----------|
| ≤14B / 9B GGUF | `Q4_K_M` / `Q5_K_M` / `Q6_K` (`q14`) | Qwen2.5/3 0.5B–14B, Llama-3.2/3.1-8B, Phi-4, Ministral, Granite 3.3–4.2 small, Hunyuan ≤7B |
| ~24–35B / MoE ~30B-A3B | `Q3_K_M` / `Q4_K_M` / `Q5_K_M` (`q32`) | Qwen3 30B-A3B / Coder-30B, Mistral-Small 3.1/3.2, Devstral/Magistral, gpt-oss-20b, Nemotron 30B-A3B, Seed-OSS-36B |
| VL (any size) | size-class quants **+ `mmproj`** (`vl14` / `vl32` / `vl70`) | Qwen2.5/3-VL, Qwen3.5–3.8 (native VL), Gemma 3/4, InternVL3.5, MiniCPM-V, GLM-4.6V-Flash, Voxtral |
| Gemma 27B / 31B | `Q4_K_M` / `Q5_K_M` only (`gemma27` / `vl_gemma27`) | Gemma-2-27B, Gemma-3-27B (+QAT), Gemma-4-31B |
| ~70–80B / 80B-class MoE | `Q3_K_M` / `IQ3_M` / `Q4_K_M` (`q70`) | Llama-3.3-70B, Qwen3-Next-80B-A3B, Qwen3-Coder-Next, R1-Llama-70B, Qwen2.5-VL-72B |
| Embed / rerank / ASR | `Q8_0`+`f16` / full small repo | Qwen3-Embedding, nomic-embed-text v1.5/v2-moe/code, granite-embedding, whisper-large-v3 |
| Image / Gemlite (3090 CUDA) | full small repo (no patterns) | Bonsai Image ternary/binary 4B Gemlite; **Qwen-Image-2.1** (+ PE-T2I / PE-I2I) Diffusers T2I+edit — in-scope for 3090/2x3090; not llama.cpp |

**Skipped (too big for 2x3090 even at Q3/IQ3):** Kimi-K2 ~1T; DeepSeek-V3/R1 671B; Qwen3-Coder-480B; Qwen3-235B; Qwen3.5-122B/397B; gpt-oss-120b; Mistral-Large-675B / Small-4-119B / Devstral-2-123B; Llama 4 Maverick; full GLM-4.5/4.6/4.7 (not Flash); GLM-4.5-Air (Q3 ~52GB); Qwen3.8-Flash-Next (Q3 ~86GB); Command-A 111B; Command-R+ 104B; Mixtral-8x22B if Q4 >48GB. Also skipped: abliterated/heretic/Fallen/RP/NSFW finetunes; EXL2; dual unsloth dumps when bartowski exists. Llama-4-Scout is included at IQ3/Q3 only (Q4 ~63GB).

## License note

This tool is for **personal backup**. Respect each model's license and Hugging Face terms; do not redistribute weights you are not allowed to share.

## Foreman mode (local LLM build)

Cursor `/loop` cannot drive the local GGUF as a tool-using agent. Use the foreman instead:

```bash
python3 scripts/foreman.py          # run next pending T1–T9,T11 until fail/done
python3 scripts/foreman.py --once   # one todo only
python3 scripts/foreman.py --todo T3 --skip-gen  # re-VERIFY only
python3 scripts/foreman.py --todo T11            # pytest suite
```

Progress lives in `loop-hf.md` (outside this repo). Do not run T10 / bartowski until T1–T9 pass.

