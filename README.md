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

See `huggingface.env.example`. Never commit real secrets.

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
uv run hf-archive archive <owner/name> [--revision SHA] [--force]
uv run hf-archive archive-all [--force]
uv run hf-archive list
uv run hf-archive verify <owner/name>
uv run hf-archive restore <owner/name> [--out DIR]
```

Wishlist for `archive-all` is `models.yaml` (`models:` list of `repo_id`, optional `revision` / `allow_patterns` / `ignore_patterns`). Prefer `archive-all` so YAML patterns apply (`archive` alone does not read the wishlist). Use `--force` after expanding `allow_patterns` for a revision already in S3.

## S3 layout

- `repos/{owner}/{name}/revisions/{sha}/...` — immutable file tree
- `repos/{owner}/{name}/latest.json` — current pointer
- `repos/{owner}/{name}/manifest.json` — path / size / sha256
- `index/catalog.json` — flat inventory

Local staging cache: `.cache/` (gitignored).

## Cost warning

Full GGUF repos (e.g. bartowski multi-quant dumps) contain **many** quantizations. A full mirror is intentional delete-insurance, but storage GB adds up. The wishlist intentionally selects a few quants (for the Qwen 7B Instruct GGUFs: `Q4_K_M`, `Q5_K_M`, and `Q6_K` — not every quant in the repo). See comments in `models.yaml`.

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

