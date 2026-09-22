#!/usr/bin/env python3
"""Measure Hetzner S3 + local disk usage. Never prints secrets."""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import socket
import struct
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'").strip('"')
        if k and k not in os.environ:
            os.environ[k] = v


# --- DNS patch (systemd stub resolver sometimes fails on this host) ---
_DNS_CACHE: dict[str, list[str]] = {}
_ORIG_GETADDRINFO = socket.getaddrinfo


def dns_a(name: str, server: str = "1.1.1.1", timeout: float = 3.0) -> list[str]:
    if name in _DNS_CACHE:
        return _DNS_CACHE[name]
    tid = random.randint(0, 65535)
    header = struct.pack("!HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    q = b"".join(bytes([len(l)]) + l.encode() for l in name.split("."))
    q += b"\x00" + struct.pack("!HH", 1, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(header + q, (server, 53))
        data, _ = sock.recvfrom(512)
    finally:
        sock.close()
    ancount = struct.unpack("!H", data[6:8])[0]
    i = 12
    while data[i] != 0:
        i += 1 + data[i]
    i += 5
    addrs: list[str] = []
    for _ in range(ancount):
        if data[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while data[i] != 0:
                i += 1 + data[i]
            i += 1
        typ, _clas, _ttl, rdlen = struct.unpack("!HHIH", data[i : i + 10])
        i += 10
        rdata = data[i : i + rdlen]
        i += rdlen
        if typ == 1 and rdlen == 4:
            addrs.append(".".join(map(str, rdata)))
    _DNS_CACHE[name] = addrs
    return addrs


def patch_dns() -> None:
    def _patched(host, port, family=0, type=0, proto=0, flags=0):
        if isinstance(host, str) and host and not host.replace(".", "").isdigit():
            try:
                addrs = dns_a(host)
                if addrs:
                    out = []
                    for a in addrs:
                        out.extend(_ORIG_GETADDRINFO(a, port, family, type, proto, flags))
                    return out
            except Exception:
                pass
        return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)

    socket.getaddrinfo = _patched  # type: ignore[assignment]


def measure_disk(paths: list[str]) -> dict:
    disk: dict = {}
    for p in paths:
        try:
            u = shutil.disk_usage(p)
            disk[p] = {
                "total_gib": round(u.total / 1024**3, 2),
                "used_gib": round(u.used / 1024**3, 2),
                "free_gib": round(u.free / 1024**3, 2),
                "used_pct": round(100 * u.used / u.total, 1),
            }
        except OSError as e:
            disk[p] = {"error": str(e)}
    return disk


def dir_size_bytes(path: Path) -> int | None:
    if not path.exists():
        return None
    total = 0
    try:
        for f in path.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        return None
    return total


def repo_key(key: str) -> str:
    parts = key.split("/")
    if len(parts) >= 3 and parts[0] == "repos":
        return f"{parts[1]}/{parts[2]}"
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return parts[0] if parts else "(root)"


def scan_bucket(client, bucket: str, detail_repos: bool, progress_every: int = 50) -> dict:
    total = 0
    count = 0
    by_repo: dict[str, dict] = defaultdict(lambda: {"bytes": 0, "objects": 0})
    by_top: dict[str, dict] = defaultdict(lambda: {"bytes": 0, "objects": 0})
    token = None
    pages = 0
    t0 = time.time()
    while True:
        kwargs: dict = {"Bucket": bucket, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        pages += 1
        for item in resp.get("Contents") or []:
            size = int(item.get("Size") or 0)
            key = item["Key"]
            total += size
            count += 1
            top = key.split("/", 1)[0] if key else ""
            by_top[top]["bytes"] += size
            by_top[top]["objects"] += 1
            if detail_repos:
                rk = repo_key(key)
                by_repo[rk]["bytes"] += size
                by_repo[rk]["objects"] += 1
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
        if progress_every and pages % progress_every == 0:
            print(
                f"  [{bucket}] pages={pages} objects={count} gib={total / 1024**3:.3f}",
                flush=True,
            )

    out: dict = {
        "bytes": total,
        "objects": count,
        "gib": round(total / 1024**3, 3),
        "pages": pages,
        "elapsed_sec": round(time.time() - t0, 1),
        "top_prefixes": [
            {
                "prefix": k,
                "bytes": v["bytes"],
                "gib": round(v["bytes"] / 1024**3, 3),
                "objects": v["objects"],
            }
            for k, v in sorted(by_top.items(), key=lambda x: -x[1]["bytes"])[:30]
        ],
    }
    if detail_repos:
        repos = sorted(by_repo.items(), key=lambda x: -x[1]["bytes"])
        out["repo_count"] = len(repos)
        out["repos"] = [
            {
                "repo": k,
                "bytes": v["bytes"],
                "gib": round(v["bytes"] / 1024**3, 3),
                "objects": v["objects"],
            }
            for k, v in repos
        ]
    return out


def measure_s3(
    buckets: list[str] | None,
    detail_buckets: set[str],
) -> dict:
    import boto3
    from botocore.client import Config as BotoConfig

    url = os.environ["S3_URL"].strip()
    region = os.environ["S3_REGION"].strip()
    ak = os.environ["S3_ACCESS_KEY"].strip()
    sk = os.environ["S3_SECRET_KEY"].strip()
    default_bucket = (
        os.environ.get("S3_BUCKET", "huggingface-models").strip() or "huggingface-models"
    )
    host = urlparse(url).netloc
    # Never emit keys
    print(f"endpoint_host={host} region={region}", flush=True)

    patch_dns()
    client = boto3.client(
        "s3",
        endpoint_url=url,
        region_name=region,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        config=BotoConfig(s3={"addressing_style": "path"}),
    )

    listed: list[str] = []
    list_err = None
    try:
        listed = [b["Name"] for b in client.list_buckets().get("Buckets", [])]
        print(f"buckets_found={len(listed)}", flush=True)
    except Exception as e:
        list_err = f"{type(e).__name__}: {e}"
        print(f"list_buckets_error={list_err}", flush=True)
        listed = [default_bucket]

    if buckets:
        target = buckets
    else:
        target = listed
        if default_bucket not in target:
            target = [default_bucket] + target

    results: dict = {
        "endpoint_host": host,
        "region": region,
        "source": "live_list_objects_v2",
        "list_buckets_error": list_err,
        "buckets_listed": listed,
        "buckets": {},
    }
    grand_b = 0
    grand_n = 0
    for bucket in target:
        detail = bucket in detail_buckets or bucket == default_bucket
        print(f"scanning_bucket={bucket} detail_repos={detail}", flush=True)
        info = scan_bucket(client, bucket, detail_repos=detail)
        results["buckets"][bucket] = info
        grand_b += info["bytes"]
        grand_n += info["objects"]
        print(
            f"bucket={bucket} objects={info['objects']} gib={info['gib']} pages={info['pages']}",
            flush=True,
        )

    results["total_bytes"] = grand_b
    results["total_objects"] = grand_n
    results["total_gib"] = round(grand_b / 1024**3, 3)
    return results


def to_markdown(snap: dict) -> str:
    lines = [
        f"# Storage usage snapshot — {snap.get('measured_at', '')}",
        "",
        f"**S3 total:** {snap.get('s3', {}).get('total_gib', '?')} GiB "
        f"({snap.get('s3', {}).get('total_objects', '?')} objects)",
        "",
    ]
    disk = snap.get("disk") or {}
    if disk:
        lines.append("## Disk")
        lines.append("")
        for path, info in disk.items():
            if "error" in info:
                lines.append(f"- `{path}`: error {info['error']}")
            else:
                lines.append(
                    f"- `{path}`: {info['used_gib']} / {info['total_gib']} GiB used "
                    f"({info['used_pct']}%), {info['free_gib']} GiB free"
                )
        lines.append("")
    dirs = snap.get("local_dirs") or {}
    if dirs:
        lines.append("## Local directories")
        lines.append("")
        for path, info in dirs.items():
            if info.get("bytes") is None:
                lines.append(f"- `{path}`: missing")
            else:
                lines.append(
                    f"- `{path}`: {info.get('gib')} GiB ({info.get('bytes')} bytes)"
                )
        lines.append("")

    buckets = (snap.get("s3") or {}).get("buckets") or {}
    if buckets:
        lines.append("## Buckets")
        lines.append("")
        lines.append("| Bucket | GiB | Objects |")
        lines.append("| --- | ---: | ---: |")
        for name, info in sorted(buckets.items(), key=lambda x: -x[1].get("bytes", 0)):
            lines.append(f"| `{name}` | {info.get('gib')} | {info.get('objects')} |")
        lines.append("")

    # huggingface repo breakdown if present
    hf = buckets.get("huggingface-models") or {}
    repos = hf.get("repos") or []
    if repos:
        lines.append("## huggingface-models by repo")
        lines.append("")
        lines.append("| Repo | GiB | Objects |")
        lines.append("| --- | ---: | ---: |")
        for r in repos:
            lines.append(f"| `{r['repo']}` | {r['gib']} | {r['objects']} |")
        lines.append("")

    lines.append("_No secrets included in this snapshot._")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--env-file",
        default=str(Path.home() / ".env"),
        help="Path to env file with S3_* (default: ~/.env)",
    )
    ap.add_argument(
        "--bucket",
        action="append",
        dest="buckets",
        help="Only scan this bucket (repeatable). Default: all listed buckets.",
    )
    ap.add_argument(
        "--detail-bucket",
        action="append",
        default=["huggingface-models"],
        help="Buckets to include per-repo breakdown (default: huggingface-models)",
    )
    ap.add_argument(
        "--out-dir",
        default=str(REPO_ROOT / "snapshots"),
        help="Directory for JSON/Markdown snapshots",
    )
    ap.add_argument("--skip-s3", action="store_true")
    ap.add_argument("--skip-disk", action="store_true")
    args = ap.parse_args()

    load_dotenv(Path(args.env_file))
    # Optional project-local env (same as hf-archive)
    load_dotenv(REPO_ROOT / "huggingface.env")

    measured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap: dict = {"measured_at": measured_at}

    if not args.skip_disk:
        snap["disk"] = measure_disk(["/home", "/"])
        local_dirs = {}
        for p in [
            REPO_ROOT / ".cache",
            REPO_ROOT / ".cache" / "_stream",
        ]:
            b = dir_size_bytes(p)
            local_dirs[str(p)] = (
                None
                if b is None
                else {"bytes": b, "gib": round(b / 1024**3, 3)}
            )
        snap["local_dirs"] = local_dirs

    if not args.skip_s3:
        for req in ("S3_URL", "S3_REGION", "S3_ACCESS_KEY", "S3_SECRET_KEY"):
            if not os.environ.get(req, "").strip():
                print(f"missing_env={req}", file=sys.stderr)
                return 2
        snap["s3"] = measure_s3(
            buckets=args.buckets,
            detail_buckets=set(args.detail_bucket or []),
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"snapshot-{stamp}.json"
    md_path = out_dir / f"snapshot-{stamp}.md"
    latest_json = out_dir / "latest.json"
    latest_md = out_dir / "latest.md"

    text = json.dumps(snap, indent=2)
    json_path.write_text(text + "\n")
    latest_json.write_text(text + "\n")
    md = to_markdown(snap)
    md_path.write_text(md)
    latest_md.write_text(md)

    # Safe summary only
    s3 = snap.get("s3") or {}
    print(
        json.dumps(
            {
                "measured_at": measured_at,
                "total_gib": s3.get("total_gib"),
                "total_objects": s3.get("total_objects"),
                "json": str(json_path),
                "md": str(md_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
