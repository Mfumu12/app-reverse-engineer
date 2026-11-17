"""Tests for SQLite persistence, queue concurrency, and per-host settings."""

import asyncio
from pathlib import Path

from hyperdl.config import HyperDLConfig
from hyperdl.engine.manager import DownloadManager
from hyperdl.models import DownloadStatus


def test_queues_and_per_host_settings(tmp_path: Path):
    async def run():
        cfg = HyperDLConfig(data_dir=tmp_path / "data", download_dir=tmp_path / "dl")
        mgr = DownloadManager(cfg)
        await mgr.start()
        try:
            mgr.db.upsert_host_settings(
                "files.example.org",
                max_connections=16,
                speed_limit_kbps=500,
                user_agent="HostAgent/1.0",
                headers={"X-Host": "yes"},
            )
            rec = await mgr.add_download(
                "https://files.example.org/big.iso",
                queue_id="batch",
                auto_start=False,
            )
            assert rec.status == DownloadStatus.PAUSED.value
            assert rec.max_connections == 16
            assert rec.speed_limit_kbps == 500
            assert rec.user_agent == "HostAgent/1.0"
            assert rec.headers["X-Host"] == "yes"
            assert rec.category == "compressed"

            # Verify persisted on re-read
            from_db = mgr.db.get_download(rec.id)
            assert from_db is not None
            assert from_db.filename == "big.iso"
        finally:
            await mgr.stop()

    asyncio.run(run())
