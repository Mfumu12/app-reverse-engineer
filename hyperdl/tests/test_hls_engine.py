"""Tests for HLS (.m3u8) playlist parser and concurrent segment assembler."""

import asyncio
from pathlib import Path

from aiohttp import web

from hyperdl.config import HyperDLConfig
from hyperdl.engine.hls_engine import parse_m3u8
from hyperdl.engine.manager import DownloadManager
from hyperdl.models import DownloadStatus


def test_parse_m3u8_master_and_media():
    master = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=500000\n"
        "low/index.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2500000\n"
        "high/index.m3u8\n"
    )
    res = parse_m3u8(master, "https://cdn.example.com/live/master.m3u8")
    assert res["is_master"] is True
    variants = res["variants"]
    assert variants[0] == (2500000, "https://cdn.example.com/live/high/index.m3u8")


def test_hls_download_and_assemble(tmp_path: Path):
    async def run():
        segs = [f"SEGMENT-{i}-DATA-".encode() * 500 for i in range(6)]
        expected = b"".join(segs)

        app = web.Application()

        async def master_h(req: web.Request) -> web.Response:
            return web.Response(
                text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=4000000\nhigh.m3u8\n"
            )

        async def high_h(req: web.Request) -> web.Response:
            body = "#EXTM3U\n" + "".join(f"#EXTINF:2.0,\nseg_{i}.ts\n" for i in range(len(segs))) + "#EXT-X-ENDLIST\n"
            return web.Response(text=body)

        async def seg_h(req: web.Request) -> web.Response:
            idx = int(req.match_info["idx"])
            return web.Response(body=segs[idx])

        app.router.add_get("/master.m3u8", master_h)
        app.router.add_get("/high.m3u8", high_h)
        app.router.add_get("/seg_{idx}.ts", seg_h)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

        cfg = HyperDLConfig(data_dir=tmp_path / "data", download_dir=tmp_path / "dl")
        mgr = DownloadManager(cfg)
        await mgr.start()
        try:
            rec = await mgr.add_download(
                f"http://127.0.0.1:{port}/master.m3u8",
                filename="video_stream.ts",
                max_connections=4,
            )
            for _ in range(80):
                cur = mgr.get_download(rec.id, include_segments=True)
                assert cur is not None
                if cur.status in (DownloadStatus.COMPLETED.value, DownloadStatus.ERROR.value):
                    break
                await asyncio.sleep(0.05)

            done = mgr.get_download(rec.id, include_segments=True)
            assert done is not None
            assert done.status == DownloadStatus.COMPLETED.value, done.error_message
            assert Path(done.file_path).read_bytes() == expected
        finally:
            await mgr.stop()
            await runner.cleanup()

    asyncio.run(run())
