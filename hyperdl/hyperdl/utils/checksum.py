"""Async file checksum verification (MD5, SHA-1, SHA-256, SHA-512)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

SUPPORTED_ALGORITHMS = ("md5", "sha1", "sha256", "sha512")


def compute_checksum_sync(file_path: Path | str, algorithm: str = "sha256") -> str:
    algo = algorithm.lower().strip()
    if algo not in SUPPORTED_ALGORITHMS:
        raise ValueError(f"Unsupported checksum algorithm: {algorithm}")
    h = hashlib.new(algo)
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


async def compute_checksum(file_path: Path | str, algorithm: str = "sha256") -> str:
    return await asyncio.to_thread(compute_checksum_sync, file_path, algorithm)
