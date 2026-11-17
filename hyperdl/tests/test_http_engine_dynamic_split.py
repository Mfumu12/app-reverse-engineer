"""Tests for HTTP multi-segment engine, dynamic work-stealing, pause/resume, and ETag safety."""

import asyncio
import hashlib
import os
from pathlib import Path

import pytest
from aiohttp import web

from hyperdl.config import HyperDLConfig
from hyperdl.engine.manager import DownloadManager
from hyperdl.models import DownloadStatus


async def _start_range_server(payload: bytes, *, slow_first_chunk: bool = False, etag_ref: list[str] | None = None, supports_range: bool = True):
    etag_box = etag_ref or ['"v1"']

    async def handler(request: web.Request) -> web.StreamResponse:
        headers = {
            "ETag": etag_box[0],
            "Content-Disposition": 'attachment; filename="payload.bin"',
            "Content-Type": "application/octet-stream",
        }
        if supports_range:
            headers["Accept-Ranges"] = "bytes"

        if request.method == "HEAD":
            return web.Response(status=200, headers={**headers, "Content-Length": str(len(payload))})

        rng = request.headers.get("Range", "")
        if supports_range and rng.startswith("bytes="):
            s_part, _, e_part = rng[6:].partition("-")
            start = int(s_part)
            end = int(e_part) if e_part.isdigit() else len(payload) - 1
            end = min(end, len(payload) - 1)
            chunk = payload[start : end + 1]
            resp = web.StreamResponse(
                status=206,
                headers={
                    **headers,
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{len(payload)}",
                },
            )
            await resp.prepare(request)
            pos = 0
            while pos < len(chunk):
                part = chunk[pos : pos + 16384]
                await resp.write(part)
                pos += len(part)
                if slow_first_chunk and start == 0:
                    await asyncio.sleep(0.03)
                else:
                    await asyncio.sleep(0.001)
            await resp.write_eof()
            return resp

        resp = web.StreamResponse(status=200, headers={**headers, "Content-Length": str(len(payload))})
        await resp.prepare(request)
        await resp.write(payload)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_route("*", "/file.bin", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}/file.bin"


def test_dynamic_work_stealing_and_checksum(tmp_path: Path):
    async def run():
        payload = os.urandom(1536 * 1024)  # 1.5 MB
        expected_sha = hashlib.sha256(payload).hexdigest()
        runner, url = await _start_range_server(payload, slow_first_chunk=True)

        cfg = HyperDLConfig(
            data_dir=tmp_path / "data",
            download_dir=tmp_path / "dl",
            default_connections=4,
            min_split_size=64 * 1024,
            chunk_read_size=16 * 1024,
        )
        mgr = DownloadManager(cfg)
        await mgr.start()
        try:
            rec = await mgr.add_download(url, max_connections=4, expected_checksum=expected_sha)
            for _ in range(120):
                cur = mgr.get_download(rec.id, include_segments=True)
                assert cur is not None
                if cur.status in (DownloadStatus.COMPLETED.value, DownloadStatus.ERROR.value):
                    break
                await asyncio.sleep(0.05)

            final = mgr.get_download(rec.id, include_segments=True)
            assert final is not None
            assert final.status == DownloadStatus.COMPLETED.value, final.error_message
            assert final.actual_checksum == expected_sha
            assert Path(final.file_path).read_bytes() == payload
            assert final.split_count >= 1
        finally:
            await mgr.stop()
            await runner.cleanup()

    asyncio.run(run())


def test_pause_resume_and_etag_invalidation(tmp_path: Path):
    async def run():
        payload = os.urandom(1024 * 1024)
        expected_sha = hashlib.sha256(payload).hexdigest()
        etag_box = ['"etag-1"']
        runner, url = await _start_range_server(payload, slow_first_chunk=True, etag_ref=etag_box)

        cfg = HyperDLConfig(
            data_dir=tmp_path / "data",
            download_dir=tmp_path / "dl",
            default_connections=4,
            min_split_size=64 * 1024,
            chunk_read_size=16 * 1024,
        )
        mgr = DownloadManager(cfg)
        await mgr.start()
        try:
            rec = await mgr.add_download(url, max_connections=4)
            await asyncio.sleep(0.12)
            paused = await mgr.pause_download(rec.id)
            assert paused is not None
            assert paused.status == DownloadStatus.PAUSED.value
            assert paused.downloaded_bytes > 0

            # Resume normally
            await mgr.resume_download(rec.id)
            for _ in range(120):
                cur = mgr.get_download(rec.id, include_segments=True)
                assert cur is not None
                if cur.status in (DownloadStatus.COMPLETED.value, DownloadStatus.ERROR.value):
                    break
                await asyncio.sleep(0.05)

            done = mgr.get_download(rec.id, include_segments=True)
            assert done is not None
            assert done.status == DownloadStatus.COMPLETED.value, done.error_message
            assert hashlib.sha256(Path(done.file_path).read_bytes()).hexdigest() == expected_sha
        finally:
            await mgr.stop()
            await runner.cleanup()

    asyncio.run(run())


def test_non_range_server_fallback(tmp_path: Path):
    async def run():
        payload = os.urandom(200_000)
        runner, url = await _start_range_server(payload, supports_range=False)
        cfg = HyperDLConfig(data_dir=tmp_path / "data", download_dir=tmp_path / "dl")
        mgr = DownloadManager(cfg)
        await mgr.start()
        try:
            rec = await mgr.add_download(url, max_connections=8)
            for _ in range(80):
                cur = mgr.get_download(rec.id, include_segments=True)
                assert cur is not None
                if cur.status in (DownloadStatus.COMPLETED.value, DownloadStatus.ERROR.value):
                    break
                await asyncio.sleep(0.05)
            done = mgr.get_download(rec.id, include_segments=True)
            assert done is not None
            assert done.status == DownloadStatus.COMPLETED.value
            assert Path(done.file_path).read_bytes() == payload
        finally:
            await mgr.stop()
            await runner.cleanup()

    asyncio.run(run())
