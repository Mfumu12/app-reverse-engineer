"""End-to-end tests for HyperDL HTTP Integration API, Demo Range endpoint, and Extension zip."""

import asyncio
from pathlib import Path

import aiohttp
from aiohttp import web

from hyperdl.config import HyperDLConfig
from hyperdl.engine.manager import DownloadManager
from hyperdl.models import DownloadStatus
from hyperdl.server import HyperDLServer


def test_server_api_and_demo_download(tmp_path: Path):
    async def run():
        cfg = HyperDLConfig(
            data_dir=tmp_path / "data",
            download_dir=tmp_path / "dl",
            min_split_size=64 * 1024,
        )
        mgr = DownloadManager(cfg)
        srv = HyperDLServer(mgr)
        app = srv.create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        base = f"http://127.0.0.1:{port}"

        async with aiohttp.ClientSession() as client:
            async with client.get(f"{base}/api/ping") as r:
                assert r.status == 200
                j = await r.json()
                assert j["app"] == "HyperDL"

            async with client.get(f"{base}/api/extension.zip") as r:
                assert r.status == 200
                assert len(await r.read()) > 200

            async with client.get(f"{base}/api/extension/status") as r:
                assert r.status == 200
                st = await r.json()
                assert "extension_dir" in st

            async with client.post(
                f"{base}/api/downloads/add",
                json={"url": f"{base}/api/demo/file/1?delay_ms=2", "max_connections": 4},
            ) as r:
                assert r.status == 201
                dl = await r.json()
                dl_id = dl["id"]

            for _ in range(100):
                async with client.get(f"{base}/api/downloads/{dl_id}") as r:
                    cur = await r.json()
                if cur["status"] in (DownloadStatus.COMPLETED.value, DownloadStatus.ERROR.value):
                    break
                await asyncio.sleep(0.05)

            assert cur["status"] == DownloadStatus.COMPLETED.value
            assert cur["downloaded_bytes"] == 1024 * 1024

            async with client.post(
                f"{base}/api/downloads/{dl_id}/verify", json={"algorithm": "sha256"}
            ) as r:
                assert r.status == 200
                v = await r.json()
                assert len(v["digest"]) == 64

            # Settings update including scheduler & clipboard monitor
            async with client.put(
                f"{base}/api/settings",
                json={
                    "auto_categorize": True,
                    "clipboard_monitor": False,
                    "schedule_enabled": True,
                    "schedule_start": "02:00",
                    "schedule_stop": "06:30",
                },
            ) as r:
                assert r.status == 200
                s = await r.json()
                assert s["auto_categorize"] is True
                assert s["clipboard_monitor"] is False
                assert s["schedule_enabled"] is True
                assert s["schedule_start"] == "02:00"
                assert s["schedule_stop"] == "06:30"

            # Refresh URL on paused download
            async with client.post(
                f"{base}/api/downloads/add",
                json={"url": "https://example.com/old.zip", "auto_start": False},
            ) as r:
                assert r.status == 201
                paused_id = (await r.json())["id"]

            async with client.post(
                f"{base}/api/downloads/{paused_id}/refresh-url",
                json={"url": "https://example.com/new.zip", "resume": False},
            ) as r:
                assert r.status == 200
                refreshed = await r.json()
                assert refreshed["url"] == "https://example.com/new.zip"

            # Clear completed keeps file on disk and removes completed row
            async with client.post(f"{base}/api/downloads/clear-completed") as r:
                assert r.status == 200
                cl = await r.json()
                assert cl["cleared"] >= 1
            assert Path(cur["file_path"]).exists()

        await runner.cleanup()

    asyncio.run(run())
