"""hf-archive CLI entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hf_archive.config import PROJECT_ROOT, load_config
from hf_archive import archive as archive_mod
from hf_archive import s3 as s3mod
from hf_archive import verify_restore as vr
from hf_archive.wishlist import load_wishlist


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="hf-archive", description="Archive Hugging Face repos to S3"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("ensure-bucket", help="Create bucket if missing")

    p_arch = sub.add_parser("archive", help="Archive one repo")
    p_arch.add_argument("repo_id")
    p_arch.add_argument("--force", action="store_true")
    p_arch.add_argument("--revision", default=None)

    p_all = sub.add_parser("archive-all", help="Archive all models.yaml entries")
    p_all.add_argument(
        "--force",
        action="store_true",
        help="Re-archive even if latest revision is already present (e.g. expanded patterns)",
    )
    sub.add_parser("list", help="List catalog")

    p_ver = sub.add_parser("verify", help="Verify archived repo")
    p_ver.add_argument("repo_id")

    p_res = sub.add_parser("restore", help="Restore archived repo")
    p_res.add_argument("repo_id")
    p_res.add_argument("--out", default=None)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return

    if args.command == "ensure-bucket":
        cfg = load_config()
        s3mod.ensure_bucket(cfg)
        print("bucket ready")
        return

    if args.command == "archive":
        cfg = load_config()
        result = archive_mod.archive_repo(
            cfg, args.repo_id, force=args.force, revision=args.revision
        )
        print(json.dumps(result))
        return

    if args.command == "archive-all":
        cfg = load_config()
        wishlist_path = PROJECT_ROOT / "models.yaml"
        items = load_wishlist(wishlist_path)
        summary = {"ok": 0, "skip": 0, "fail": 0, "results": []}
        for item in items:
            rid = item["repo_id"]
            try:
                result = archive_mod.archive_repo(
                    cfg,
                    rid,
                    force=args.force,
                    revision=item.get("revision"),
                    allow_patterns=item.get("allow_patterns"),
                    ignore_patterns=item.get("ignore_patterns"),
                )
                status = result.get("status")
                if status == "skipped":
                    summary["skip"] += 1
                else:
                    summary["ok"] += 1
                summary["results"].append(result)
                print(json.dumps(result))
            except Exception as e:
                summary["fail"] += 1
                err = {"repo_id": rid, "status": "fail", "error": str(e)}
                summary["results"].append(err)
                print(json.dumps(err), file=sys.stderr)
        print(
            json.dumps(
                {
                    "ok": summary["ok"],
                    "skip": summary["skip"],
                    "fail": summary["fail"],
                }
            )
        )
        if summary["fail"]:
            sys.exit(1)
        return

    if args.command == "list":
        cfg = load_config()
        for entry in archive_mod.list_catalog(cfg):
            print(
                f"{entry.get('repo_id')}\t{entry.get('revision')}\t{entry.get('archived_at', '')}"
            )
        return

    if args.command == "verify":
        cfg = load_config()
        result = vr.verify_repo(cfg, args.repo_id)
        print(json.dumps(result))
        if not result.get("ok"):
            sys.exit(1)
        return

    if args.command == "restore":
        cfg = load_config()
        owner, name = args.repo_id.split("/", 1)
        out = Path(args.out) if args.out else Path(f"./restored/{owner}__{name}")
        result = vr.restore_repo(cfg, args.repo_id, out)
        print(json.dumps(result))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
