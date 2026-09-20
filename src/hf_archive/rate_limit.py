"""Simple sleep-based bandwidth limiter for HF downloads and S3 uploads.

How the CLI flag reaches here
-----------------------------
1. User passes ``--max-mbps 8`` on ``archive`` / ``archive-all``
   (or sets env ``HF_ARCHIVE_MAX_MBPS``; else default 8).
2. ``cli.py`` calls ``cfg.with_max_mbps(args.max_mbps)`` so Config.max_mbps is set.
3. ``archive.py`` passes ``cfg.max_bytes_per_sec`` into HF download and S3 upload.
4. Those call ``RateLimiter.acquire(nbytes)`` after each chunk (or boto3
   TransferConfig.max_bandwidth for uploads).

Set max_mbps to 0 (or negative) to disable limiting.
"""

from __future__ import annotations

import threading
import time


def mbps_to_bytes_per_sec(max_mbps: float) -> float | None:
    """Convert MiB/s to bytes/s. Returns None when limiting is disabled (<= 0)."""
    if max_mbps is None or max_mbps <= 0:
        return None
    return float(max_mbps) * 1024 * 1024


class RateLimiter:
    """Token-bucket limiter: sleep until we have enough tokens for ``nbytes``.

    One limiter instance should be shared for a single transfer direction at a
    time. Archive downloads then uploads sequentially, so a fresh limiter per
    transfer (or one shared at the same Mbps) is fine.
    """

    def __init__(self, bytes_per_sec: float) -> None:
        if bytes_per_sec <= 0:
            raise ValueError("bytes_per_sec must be positive; use None to disable")
        self.bytes_per_sec = float(bytes_per_sec)
        # Start with one second of burst so tiny files don't stall.
        self._tokens = self.bytes_per_sec
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, nbytes: int) -> None:
        """Block until ``nbytes`` may be transferred under the rate cap."""
        if nbytes <= 0:
            return
        remaining = float(nbytes)
        with self._lock:
            while remaining > 0:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(
                    self.bytes_per_sec,
                    self._tokens + elapsed * self.bytes_per_sec,
                )
                if self._tokens < 1.0 and remaining > 0:
                    # Wait for at least one byte's worth of tokens.
                    need = max(1.0, min(remaining, self.bytes_per_sec) - self._tokens)
                    time.sleep(need / self.bytes_per_sec)
                    continue
                take = min(remaining, self._tokens)
                self._tokens -= take
                remaining -= take


class ThrottledWriter:
    """File-like wrapper: every ``write()`` sleeps via RateLimiter first.

    Pass this as the temp file to huggingface_hub ``http_get`` so Hub downloads
    are paced while bytes are written to disk.
    """

    def __init__(self, raw, limiter: RateLimiter) -> None:
        self._raw = raw
        self._limiter = limiter

    def write(self, data: bytes) -> int:
        self._limiter.acquire(len(data))
        return self._raw.write(data)

    def flush(self) -> None:
        self._raw.flush()

    def tell(self) -> int:
        return self._raw.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._raw.seek(offset, whence)

    def close(self) -> None:
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()
