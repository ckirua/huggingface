"""S3 helpers for Hetzner Object Storage (uploads rate-limited via TransferConfig)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from hf_archive.config import Config


def client(cfg: Config) -> BaseClient:
    return boto3.client(
        "s3",
        endpoint_url=cfg.s3_url,
        region_name=cfg.region,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret_key,
    )


def _transfer_config(cfg: Config) -> TransferConfig | None:
    """Build boto3 TransferConfig with max_bandwidth from Config.max_mbps.

    boto3 expects bytes/sec. None means "use boto3 defaults" (unlimited).
    """
    bps = cfg.max_bytes_per_sec
    if bps is None:
        return None
    return TransferConfig(max_bandwidth=bps)


def ensure_bucket(cfg: Config) -> None:
    s3 = client(cfg)
    try:
        s3.head_bucket(Bucket=cfg.bucket)
        return
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        http = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code not in {"404", "NoSuchBucket", "NotFound"} and http not in {404, 403}:
            # 403 can mean missing on some providers; try create only on 404-like
            if code not in {"403", "AccessDenied"}:
                raise
        # Fall through to create when missing; re-raise unexpected errors from create
    try:
        # Hetzner / path-style: CreateBucket without LocationConstraint usually works
        s3.create_bucket(Bucket=cfg.bucket)
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        if code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            return
        raise


def put_json(cfg: Config, key: str, obj: Any) -> None:
    body = json.dumps(obj, indent=2, sort_keys=True).encode("utf-8")
    client(cfg).put_object(
        Bucket=cfg.bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
    )


def get_json(cfg: Config, key: str) -> Any | None:
    s3 = client(cfg)
    try:
        resp = s3.get_object(Bucket=cfg.bucket, Key=key)
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        http = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NotFound"} or http == 404:
            return None
        raise
    return json.loads(resp["Body"].read().decode("utf-8"))


def upload_file(cfg: Config, local_path: str | Path, key: str) -> None:
    """Upload one local file. Respects cfg.max_mbps via TransferConfig.max_bandwidth."""
    kwargs: dict[str, Any] = {}
    tcfg = _transfer_config(cfg)
    if tcfg is not None:
        kwargs["Config"] = tcfg
    client(cfg).upload_file(str(local_path), cfg.bucket, key, **kwargs)


def download_file(cfg: Config, key: str, local_path: str | Path) -> None:
    """Download one object. Also paced by max_mbps when set (restore path)."""
    path = Path(local_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {}
    tcfg = _transfer_config(cfg)
    if tcfg is not None:
        kwargs["Config"] = tcfg
    client(cfg).download_file(cfg.bucket, key, str(path), **kwargs)


def list_prefix(cfg: Config, prefix: str) -> list[str]:
    s3 = client(cfg)
    keys: list[str] = []
    token = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": cfg.bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for item in resp.get("Contents") or []:
            keys.append(item["Key"])
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return keys


def object_exists(cfg: Config, key: str) -> bool:
    try:
        client(cfg).head_object(Bucket=cfg.bucket, Key=key)
        return True
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        http = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NotFound", "404"} or http == 404:
            return False
        raise
