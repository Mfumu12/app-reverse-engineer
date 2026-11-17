"""Configuration and paths for HyperDL."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def default_data_dir() -> Path:
    env = os.environ.get("HYPERDL_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".hyperdl"


def default_download_dir() -> Path:
    env = os.environ.get("HYPERDL_DOWNLOAD_DIR")
    if env:
        return Path(env)
    return Path.cwd() / "hyperdl_downloads"


@dataclass
class HyperDLConfig:
    data_dir: Path = field(default_factory=default_data_dir)
    download_dir: Path = field(default_factory=default_download_dir)
    host: str = "0.0.0.0"
    port: int = 8787
    api_key: str = ""
    default_connections: int = 8
    max_connections_cap: int = 32
    min_split_size: int = 256 * 1024  # 256 KiB minimum byte span to split dynamically
    chunk_read_size: int = 64 * 1024  # 64 KiB socket read buffer
    global_speed_limit_kbps: int = 0  # 0 = unlimited
    max_retries: int = 5
    connect_timeout_s: float = 20.0
    read_timeout_s: float = 45.0
    user_agent: str = "HyperDL/1.0 (Multi-Segment Accelerator)"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hyperdl.db"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
