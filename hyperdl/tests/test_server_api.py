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

        await runner.cleanup()

    asyncio.run(run())
