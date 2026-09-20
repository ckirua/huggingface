"""Unit tests for the sleep-based RateLimiter."""

from __future__ import annotations

import time

from hf_archive.rate_limit import RateLimiter, mbps_to_bytes_per_sec


def test_mbps_to_bytes_per_sec() -> None:
    assert mbps_to_bytes_per_sec(1) == 1024 * 1024
    assert mbps_to_bytes_per_sec(8) == 8 * 1024 * 1024
    assert mbps_to_bytes_per_sec(0) is None
    assert mbps_to_bytes_per_sec(-1) is None


def test_rate_limiter_paces_transfer() -> None:
    # 100 KB/s — transferring ~50 KB should take roughly half a second+.
    limiter = RateLimiter(100_000)
    # Burn the initial 1s burst so the next acquire must sleep.
    limiter.acquire(100_000)
    t0 = time.monotonic()
    limiter.acquire(50_000)
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.35  # allow some scheduler slack below 0.5s
