"""Async token-bucket rate limiter (supports live speed limit changes)."""

from __future__ import annotations

import asyncio
import time


class TokenBucketLimiter:
    """Thread/async-safe token bucket limiter in bytes/sec (0 = unlimited)."""

    def __init__(self, rate_kbps: int = 0) -> None:
        self._lock = asyncio.Lock()
        self._rate_bps: float = 0.0
        self._tokens: float = 0.0
        self._capacity: float = 0.0
        self._last_refill: float = time.monotonic()
        self.set_rate_kbps(rate_kbps)

    @property
    def rate_kbps(self) -> int:
        return int(self._rate_bps / 1024) if self._rate_bps > 0 else 0

    def set_rate_kbps(self, rate_kbps: int) -> None:
        kbps = max(0, int(rate_kbps))
        self._rate_bps = float(kbps * 1024)
        self._capacity = max(self._rate_bps, 64 * 1024) if self._rate_bps > 0 else 0.0
        self._tokens = min(self._tokens, self._capacity) if self._rate_bps > 0 else 0.0
        self._last_refill = time.monotonic()

    async def acquire(self, num_bytes: int) -> None:
        if num_bytes <= 0 or self._rate_bps <= 0:
            return
        remaining = float(num_bytes)
        while remaining > 0:
            sleep_for = 0.0
            async with self._lock:
                if self._rate_bps <= 0:
                    return
                now = time.monotonic()
                elapsed = max(0.0, now - self._last_refill)
                self._last_refill = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_bps)

                take = min(remaining, self._capacity * 0.5)
                if self._tokens >= take:
                    self._tokens -= take
                    remaining -= take
                else:
                    deficit = take - self._tokens
                    sleep_for = max(0.005, deficit / self._rate_bps)
            if sleep_for > 0:
                await asyncio.sleep(min(sleep_for, 0.25))
