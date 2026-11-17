"""HTTP Integration API, WebSocket live telemetry, Demo Benchmark endpoints, and Web UI server."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from . import __version__
from .config import HyperDLConfig
from .engine.manager import DownloadManager
from .utils.importers import expand_wildcard_url, extract_urls_from_text, parse_curl_command

STATIC_DIR = Path(__file__).resolve().parent / "static"
EXT_DIR = Path(__file__).resolve().parent / "extension"


def _deterministic_demo_bytes(start: int, length: int) -> bytes:
    """Fast deterministic byte generator for Range-supporting benchmark endpoint."""
    # Repeat a 4096-byte hashed block pattern indexed by block number so Range requests are byte-exact!
    out = bytearray()
    pos = start
    rem = length
    while rem > 0:
        blk_idx = pos // 4096
        blk_off = pos % 4096
        seed = hashlib.sha256(f"hyperdl-demo-block-{blk_idx}".encode()).digest()
        block = (seed * 128)[:4096]
        take = min(rem, 4096 - blk_off)
        out.extend(block[blk_off : blk_off + take])
        pos += take
        rem -= take
    return bytes(out)


class HyperDLServer:
    def __init__(self, manager: DownloadManager) -> None:
        self.manager = manager
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._pending_updates: dict[str, dict[str, Any]] = {}
        self._flush_task: asyncio.Task[None] | None = None
        self.manager.add_listener(self._on_manager_event)

    def _on_manager_event(self, event: str, payload: Any) -> None:
        if event == "download_update" and isinstance(payload, dict) and "id" in payload:
            self._pending_updates[payload["id"]] = payload
        else:
            asyncio.create_task(self._broadcast({"event": event, "data": payload}))

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.2)
                if self._pending_updates and self._ws_clients:
                    batch = list(self._pending_updates.values())
                    self._pending_updates.clear()
                    await self._broadcast({"event": "downloads_batch_update", "data": batch})
                elif self._pending_updates:
                    self._pending_updates.clear()
        except asyncio.CancelledError:
            pass

    async def _broadcast(self, message: dict[str, Any]) -> None:
        if not self._ws_clients:
            return
        raw = json.dumps(message)
        dead: list[web.WebSocketResponse] = []
        for ws in list(self._ws_clients):
            try:
                await ws.send_str(raw)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)

    def _check_auth(self, request: web.Request) -> None:
        key = self.manager.config.api_key
        if not key:
            return
        provided = request.headers.get("X-API-Key") or request.query.get("api_key", "")
        if provided != key:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "Invalid or missing X-API-Key"}),
                content_type="application/json",
            )

    def create_app(self) -> web.Application:
        @web.middleware
        async def cors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
            if request.method == "OPTIONS":
                resp: web.StreamResponse = web.Response(status=204)
            else:
                try:
                    resp = await handler(request)
                except web.HTTPException as exc:
                    resp = exc
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            return resp

        app = web.Application(middlewares=[cors_middleware])
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)

        app.router.add_get("/", self.handle_index)
        app.router.add_get("/api/ping", self.handle_ping)
        app.router.add_get("/api/downloads", self.handle_list_downloads)
        app.router.add_post("/api/downloads/add", self.handle_add_download)
        app.router.add_post("/api/downloads/import-curl", self.handle_import_curl)
        app.router.add_post("/api/downloads/add-batch", self.handle_add_batch)
        app.router.add_get("/api/downloads/{id}", self.handle_get_download)
        app.router.add_post("/api/downloads/{id}/pause", self.handle_pause)
        app.router.add_post("/api/downloads/{id}/resume", self.handle_resume)
        app.router.add_delete("/api/downloads/{id}", self.handle_delete)
        app.router.add_post("/api/downloads/{id}/verify", self.handle_verify)
        app.router.add_get("/api/downloads/{id}/file", self.handle_serve_file)

        app.router.add_get("/api/queues", self.handle_list_queues)
        app.router.add_post("/api/queues", self.handle_create_queue)
        app.router.add_post("/api/queues/{id}/start", self.handle_start_queue)
        app.router.add_post("/api/queues/{id}/stop", self.handle_stop_queue)

        app.router.add_get("/api/hosts", self.handle_list_hosts)
        app.router.add_put("/api/hosts/{host}", self.handle_upsert_host)
        app.router.add_delete("/api/hosts/{host}", self.handle_delete_host)

        app.router.add_get("/api/settings", self.handle_get_settings)
        app.router.add_put("/api/settings", self.handle_put_settings)

        app.router.add_get("/api/ws", self.handle_ws)
        app.router.add_get("/api/extension.zip", self.handle_extension_zip)

        # Built-in Range & HLS Benchmark/Demo endpoints
        app.router.add_post("/api/demo/start", self.handle_demo_start)
        app.router.add_route("*", "/api/demo/file/{size_mb}", self.handle_demo_file)
        app.router.add_get("/api/demo/hls/master.m3u8", self.handle_demo_hls_master)
        app.router.add_get("/api/demo/hls/media.m3u8", self.handle_demo_hls_media)
        app.router.add_get("/api/demo/hls/seg_{idx}.ts", self.handle_demo_hls_segment)

        return app

    async def _on_startup(self, app: web.Application) -> None:
        await self.manager.start()
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def _on_cleanup(self, app: web.Application) -> None:
        if self._flush_task:
            self._flush_task.cancel()
            await asyncio.gather(self._flush_task, return_exceptions=True)
        for ws in list(self._ws_clients):
            await ws.close()
        await self.manager.stop()

    async def handle_index(self, request: web.Request) -> web.Response:
        html_path = STATIC_DIR / "index.html"
        return web.FileResponse(html_path)

    async def handle_ping(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "app": "HyperDL", "version": __version__})

    async def handle_list_downloads(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        status = request.query.get("status") or None
        category = request.query.get("category") or None
        queue_id = request.query.get("queue_id") or None
        include_segments = request.query.get("segments", "false").lower() == "true"
        items = self.manager.list_downloads(
            status=status, category=category, queue_id=queue_id, include_segments=include_segments
        )
        return web.json_response({"downloads": [d.to_dict(include_segments=include_segments) for d in items]})

    async def handle_get_download(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        dl_id = request.match_info["id"]
        rec = self.manager.get_download(dl_id, include_segments=True)
        if not rec:
            raise web.HTTPNotFound(text=json.dumps({"error": "Download not found"}), content_type="application/json")
        return web.json_response(rec.to_dict(include_segments=True))

    async def handle_add_download(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        data = await request.json()
        url = str(data.get("url", "")).strip()
        if not url:
            raise web.HTTPBadRequest(text=json.dumps({"error": "url is required"}), content_type="application/json")
        try:
            rec = await self.manager.add_download(
                url,
                filename=data.get("filename"),
                save_dir=data.get("save_dir"),
                queue_id=data.get("queue_id") or "main",
                max_connections=data.get("max_connections"),
                speed_limit_kbps=data.get("speed_limit_kbps"),
                headers=data.get("headers"),
                user_agent=data.get("user_agent"),
                download_type=data.get("download_type"),
                expected_checksum=data.get("expected_checksum", ""),
                checksum_algorithm=data.get("checksum_algorithm", "sha256"),
                auto_start=bool(data.get("auto_start", True)),
            )
            return web.json_response(rec.to_dict(include_segments=True), status=201)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(exc)}), content_type="application/json")

    async def handle_import_curl(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        data = await request.json()
        cmd = str(data.get("curl", "")).strip()
        try:
            parsed = parse_curl_command(cmd)
            rec = await self.manager.add_download(
                parsed.url,
                filename=data.get("filename") or parsed.suggested_name or None,
                queue_id=data.get("queue_id") or "main",
                max_connections=data.get("max_connections"),
                headers=parsed.headers,
                user_agent=parsed.user_agent or None,
                auto_start=bool(data.get("auto_start", True)),
            )
            return web.json_response(rec.to_dict(include_segments=True), status=201)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(exc)}), content_type="application/json")

    async def handle_add_batch(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        data = await request.json()
        pattern = str(data.get("pattern", "")).strip()
        queue_id = data.get("queue_id") or "batch"
        auto_start = bool(data.get("auto_start", True))
        urls: list[str] = []
        if "[" in pattern and ".." in pattern and "]" in pattern:
            urls = expand_wildcard_url(pattern)
        else:
            urls = extract_urls_from_text(pattern)
        if not urls:
            raise web.HTTPBadRequest(text=json.dumps({"error": "No valid URLs found"}), content_type="application/json")
        added = []
        for u in urls:
            rec = await self.manager.add_download(
                u,
                queue_id=queue_id,
                max_connections=data.get("max_connections"),
                auto_start=auto_start,
            )
            added.append(rec.to_dict(include_segments=False))
        return web.json_response({"count": len(added), "downloads": added}, status=201)

    async def handle_pause(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        rec = await self.manager.pause_download(request.match_info["id"])
        if not rec:
            raise web.HTTPNotFound(text=json.dumps({"error": "Download not found"}), content_type="application/json")
        return web.json_response(rec.to_dict(include_segments=True))

    async def handle_resume(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        rec = await self.manager.resume_download(request.match_info["id"])
        if not rec:
            raise web.HTTPNotFound(text=json.dumps({"error": "Download not found"}), content_type="application/json")
        return web.json_response(rec.to_dict(include_segments=True))

    async def handle_delete(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        delete_file = request.query.get("delete_file", "false").lower() == "true"
        ok = await self.manager.remove_download(request.match_info["id"], delete_file=delete_file)
        if not ok:
            raise web.HTTPNotFound(text=json.dumps({"error": "Download not found"}), content_type="application/json")
        return web.json_response({"deleted": True})

    async def handle_verify(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        data = await request.json() if request.can_read_body else {}
        try:
            res = await self.manager.verify_checksum(
                request.match_info["id"],
                algorithm=data.get("algorithm", "sha256"),
                expected=data.get("expected", ""),
            )
            return web.json_response(res)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(exc)}), content_type="application/json")

    async def handle_serve_file(self, request: web.Request) -> web.StreamResponse:
        self._check_auth(request)
        rec = self.manager.get_download(request.match_info["id"], include_segments=False)
        if not rec or not Path(rec.file_path).exists():
            raise web.HTTPNotFound()
        return web.FileResponse(
            rec.file_path,
            headers={"Content-Disposition": f'attachment; filename="{rec.filename}"'},
        )

    async def handle_list_queues(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        return web.json_response({"queues": [q.to_dict() for q in self.manager.db.list_queues()]})

    async def handle_create_queue(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        data = await request.json()
        name = str(data.get("name", "")).strip()
        if not name:
            raise web.HTTPBadRequest(text=json.dumps({"error": "name is required"}), content_type="application/json")
        qid = name.lower().replace(" ", "_")
        q = self.manager.db.create_queue(
            qid,
            name,
            max_concurrent=max(1, int(data.get("max_concurrent", 3))),
            is_running=bool(data.get("is_running", True)),
        )
        return web.json_response(q.to_dict(), status=201)

    async def handle_start_queue(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        q = await self.manager.start_queue(request.match_info["id"])
        if not q:
            raise web.HTTPNotFound()
        return web.json_response(q.to_dict())

    async def handle_stop_queue(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        q = await self.manager.stop_queue(request.match_info["id"])
        if not q:
            raise web.HTTPNotFound()
        return web.json_response(q.to_dict())

    async def handle_list_hosts(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        return web.json_response({"hosts": self.manager.db.list_host_settings()})

    async def handle_upsert_host(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        host = request.match_info["host"]
        data = await request.json()
        self.manager.db.upsert_host_settings(
            host,
            max_connections=int(data.get("max_connections", 8)),
            speed_limit_kbps=int(data.get("speed_limit_kbps", 0)),
            user_agent=str(data.get("user_agent", "")),
            headers=data.get("headers") or {},
        )
        return web.json_response(self.manager.db.get_host_settings(host))

    async def handle_delete_host(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        self.manager.db.delete_host_settings(request.match_info["host"])
        return web.json_response({"deleted": True})

    async def handle_get_settings(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        return web.json_response(self.manager.get_settings())

    async def handle_put_settings(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        data = await request.json()
        return web.json_response(self.manager.update_settings(data))

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        self._ws_clients.add(ws)
        try:
            init_payload = {
                "event": "snapshot",
                "data": {
                    "downloads": [
                        d.to_dict(include_segments=True)
                        for d in self.manager.list_downloads(include_segments=True)
                    ],
                    "queues": [q.to_dict() for q in self.manager.db.list_queues()],
                    "settings": self.manager.get_settings(),
                },
            }
            await ws.send_str(json.dumps(init_payload))
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._ws_clients.discard(ws)
        return ws

    async def handle_extension_zip(self, request: web.Request) -> web.Response:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(EXT_DIR.iterdir()):
                if p.is_file():
                    zf.write(p, arcname=f"hyperdl-extension/{p.name}")
        return web.Response(
            body=buf.getvalue(),
            content_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="hyperdl-browser-extension.zip"'},
        )

    # --- Built-in Range & HLS Benchmark Endpoints ---

    async def handle_demo_start(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        data = await request.json() if request.can_read_body else {}
        mode = str(data.get("mode", "http"))
        port = self.manager.config.port
        if mode == "hls":
            rec = await self.manager.add_download(
                f"http://127.0.0.1:{port}/api/demo/hls/master.m3u8",
                filename="demo_stream_1080p.ts",
                queue_id="video",
                max_connections=6,
            )
        elif mode == "github":
            rec = await self.manager.add_download(
                "https://codeload.github.com/psf/requests/zip/refs/tags/v2.32.3",
                filename="requests-2.32.3.zip",
                max_connections=4,
            )
        else:
            rec = await self.manager.add_download(
                f"http://127.0.0.1:{port}/api/demo/file/16?delay_ms=25&skew=1",
                max_connections=8,
            )
        return web.json_response(rec.to_dict(include_segments=True), status=201)

    async def handle_demo_file(self, request: web.Request) -> web.StreamResponse:
        size_mb = max(1, min(200, int(request.match_info["size_mb"])))
        total_size = size_mb * 1024 * 1024
        delay_ms = max(0, min(100, int(request.query.get("delay_ms", "8"))))
        skew = request.query.get("skew", "0") == "1"
        filename = f"HyperDL_Benchmark_{size_mb}MB.bin"
        etag = f'"hyperdl-demo-{size_mb}mb-v1"'
        base_headers = {
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Content-Disposition": f'attachment; filename="{filename}"',
        }

        if request.method == "HEAD":
            return web.Response(
                status=200,
                headers={**base_headers, "Content-Length": str(total_size), "Content-Type": "application/octet-stream"},
            )

        range_hdr = request.headers.get("Range", "")
        start, end = 0, total_size - 1
        status = 200
        if range_hdr.startswith("bytes="):
            spec = range_hdr[6:].split(",")[0].strip()
            s_part, _, e_part = spec.partition("-")
            if s_part.isdigit():
                start = int(s_part)
                end = int(e_part) if e_part.isdigit() else total_size - 1
                end = min(end, total_size - 1)
                status = 206

        length = max(0, end - start + 1)
        headers = {**base_headers, "Content-Length": str(length), "Content-Type": "application/octet-stream"}
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"

        resp = web.StreamResponse(status=status, headers=headers)
        await resp.prepare(request)
        cursor = start
        chunk_sz = 64 * 1024
        while cursor <= end:
            n = min(chunk_sz, end - cursor + 1)
            await resp.write(_deterministic_demo_bytes(cursor, n))
            cursor += n
            if delay_ms > 0:
                extra = delay_ms * 2 if (skew and start == 0) else delay_ms
                await asyncio.sleep(extra / 1000.0)
        await resp.write_eof()
        return resp

    async def handle_demo_hls_master(self, request: web.Request) -> web.Response:
        body = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=854x480\n"
            "media.m3u8?q=480p\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=4800000,RESOLUTION=1920x1080\n"
            "media.m3u8?q=1080p\n"
        )
        return web.Response(text=body, content_type="application/vnd.apple.mpegurl")

    async def handle_demo_hls_media(self, request: web.Request) -> web.Response:
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-TARGETDURATION:2",
            "#EXT-X-MEDIA-SEQUENCE:0",
        ]
        for i in range(16):
            lines.append("#EXTINF:2.0,")
            lines.append(f"seg_{i}.ts")
        lines.append("#EXT-X-ENDLIST")
        return web.Response(text="\n".join(lines) + "\n", content_type="application/vnd.apple.mpegurl")

    async def handle_demo_hls_segment(self, request: web.Request) -> web.Response:
        idx = int(request.match_info["idx"])
        await asyncio.sleep(0.08)
        # Synthetic MPEG-TS sync byte (0x47) packets of 188 bytes * 512 (~96 KB per segment)
        pkt = b"\x47" + hashlib.sha256(f"ts-seg-{idx}".encode()).digest() * 6
        pkt = pkt[:188]
        return web.Response(body=pkt * 512, content_type="video/mp2t")


def run_server(config: HyperDLConfig) -> None:
    mgr = DownloadManager(config)
    srv = HyperDLServer(mgr)
    web.run_app(srv.create_app(), host=config.host, port=config.port)
