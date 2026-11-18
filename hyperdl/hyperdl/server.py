"""HTTP Integration API, WebSocket live telemetry, Demo Benchmark endpoints, and Web UI server."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import threading
import time
import webbrowser
import zipfile
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from . import __version__
from .browser_installer import detect_browsers, get_installed_extension_dir, install_extension_for_browser, sync_extension_files
from .config import HyperDLConfig
from .engine.manager import DownloadManager
from .engine.youtube_engine import expand_youtube_playlist, extract_playlist_id, probe_youtube
from .utils.filename import sanitize_filename
from .utils.importers import expand_wildcard_url, extract_urls_from_text, parse_curl_command


def _bundle_root() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "hyperdl"
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parent


STATIC_DIR = _bundle_root() / "static"
EXT_DIR = _bundle_root() / "extension"


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
        app.router.add_static("/static", STATIC_DIR)
        app.router.add_get("/api/ping", self.handle_ping)
        app.router.add_get("/api/downloads", self.handle_list_downloads)
        app.router.add_post("/api/downloads/add", self.handle_add_download)
        app.router.add_post("/api/downloads/import-curl", self.handle_import_curl)
        app.router.add_post("/api/downloads/add-batch", self.handle_add_batch)
        app.router.add_post("/api/downloads/pause-all", self.handle_pause_all)
        app.router.add_post("/api/downloads/resume-all", self.handle_resume_all)
        app.router.add_post("/api/downloads/clear-completed", self.handle_clear_completed)
        app.router.add_get("/api/downloads/{id}", self.handle_get_download)
        app.router.add_post("/api/downloads/{id}/pause", self.handle_pause)
        app.router.add_post("/api/downloads/{id}/resume", self.handle_resume)
        app.router.add_post("/api/downloads/{id}/open-file", self.handle_open_file)
        app.router.add_post("/api/downloads/{id}/reveal", self.handle_reveal_file)
        app.router.add_post("/api/downloads/{id}/refresh-url", self.handle_refresh_url)
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
        app.router.add_get("/api/extension/status", self.handle_extension_status)
        app.router.add_post("/api/extension/install", self.handle_extension_install)
        app.router.add_post("/api/extension/register-id", self.handle_extension_register_id)
        app.router.add_post("/api/youtube/probe", self.handle_youtube_probe)
        app.router.add_post("/api/desktop/focus", self.handle_desktop_focus)
        app.router.add_post("/api/system/open-downloads", self.handle_open_downloads)
        app.router.add_post("/api/system/pick-folder", self.handle_pick_folder)
        app.router.add_get("/api/system/clipboard", self.handle_clipboard)
        app.router.add_get("/api/system/ytdlp-status", self.handle_ytdlp_status)
        app.router.add_post("/api/system/ytdlp-update", self.handle_ytdlp_update)

        # Built-in Range & HLS Benchmark/Demo endpoints
        app.router.add_post("/api/demo/start", self.handle_demo_start)
        app.router.add_route("*", "/api/demo/file/{size_mb}", self.handle_demo_file)
        app.router.add_get("/api/demo/hls/master.m3u8", self.handle_demo_hls_master)
        app.router.add_get("/api/demo/hls/media.m3u8", self.handle_demo_hls_media)
        app.router.add_get("/api/demo/hls/seg_{idx}.ts", self.handle_demo_hls_segment)

        return app

    async def _on_startup(self, app: web.Application) -> None:
        loop = asyncio.get_running_loop()
        prev_handler = loop.get_exception_handler()

        def _quiet_win_reset_handler(lp: asyncio.AbstractEventLoop, ctx: dict[str, Any]) -> None:
            exc = ctx.get("exception")
            msg = str(ctx.get("message", ""))
            if isinstance(exc, ConnectionResetError) and (
                "10054" in str(exc) or "_ProactorBasePipeTransport" in msg or "call_connection_lost" in msg
            ):
                return
            if prev_handler is not None:
                prev_handler(lp, ctx)
            else:
                lp.default_exception_handler(ctx)

        loop.set_exception_handler(_quiet_win_reset_handler)
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
        return web.json_response({"status": "ok", "app": "HyperDL", "name": "Gennius DM", "version": __version__})

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
            media_options = data.get("media_options")
            if not media_options and any(k in data for k in ("media_mode", "quality", "container", "audio_bitrate", "subtitles", "embed_metadata")):
                media_options = {
                    "media_mode": data.get("media_mode", "video"),
                    "quality": data.get("quality", "best"),
                    "container": data.get("container", "mp4"),
                    "audio_bitrate": data.get("audio_bitrate", "320"),
                    "subtitles": bool(data.get("subtitles", False)),
                    "embed_metadata": bool(data.get("embed_metadata", True)),
                }

            if bool(data.get("download_playlist", False)) and extract_playlist_id(url):
                pl = await expand_youtube_playlist(url, limit=int(data.get("playlist_limit", 100)))
                entries = pl.get("entries") or []
                if not entries:
                    raise ValueError("Não foram encontrados vídeos nesta playlist.")
                pl_folder = sanitize_filename(pl.get("playlist_title") or "Playlist YouTube")
                target_dir = str(self.manager.config.download_dir / pl_folder)
                added = []
                for entry in entries:
                    rec = await self.manager.add_download(
                        entry["url"],
                        save_dir=target_dir,
                        queue_id=data.get("queue_id") or ("video" if (media_options or {}).get("media_mode") != "audio" else "main"),
                        max_connections=data.get("max_connections"),
                        speed_limit_kbps=data.get("speed_limit_kbps"),
                        download_type="youtube",
                        media_options=media_options,
                        auto_start=bool(data.get("auto_start", True)),
                    )
                    added.append(rec.to_dict(include_segments=True))
                return web.json_response({"playlist_title": pl.get("playlist_title"), "count": len(added), "downloads": added}, status=201)

            rec = await self.manager.add_download(
                url,
                filename=data.get("filename"),
                save_dir=data.get("save_dir"),
                save_subdir=data.get("save_subdir"),
                queue_id=data.get("queue_id") or "main",
                max_connections=data.get("max_connections"),
                speed_limit_kbps=data.get("speed_limit_kbps"),
                headers=data.get("headers"),
                user_agent=data.get("user_agent"),
                download_type=data.get("download_type"),
                expected_checksum=data.get("expected_checksum", ""),
                checksum_algorithm=data.get("checksum_algorithm", "sha256"),
                media_options=media_options,
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
                    zf.write(p, arcname=f"gennius-dm-extension/{p.name}")
        return web.Response(
            body=buf.getvalue(),
            content_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="gennius-dm-extension.zip"'},
        )

    async def handle_extension_status(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        ext_dir = sync_extension_files(EXT_DIR)
        return web.json_response({
            "extension_dir": str(ext_dir),
            "browsers": detect_browsers(),
        })

    async def handle_extension_install(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        data = await request.json() if request.can_read_body else {}
        browser_id = str(data.get("browser", "auto"))
        mode = str(data.get("mode", "launch"))
        result = install_extension_for_browser(EXT_DIR, browser_id=browser_id, mode=mode)
        return web.json_response(result)

    async def handle_youtube_probe(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        data = await request.json() if request.can_read_body else {}
        url = str(data.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            raise web.HTTPBadRequest(text=json.dumps({"error": "URL inválida"}), content_type="application/json")
        try:
            info = await probe_youtube(url)
            return web.json_response(info)
        except Exception as exc:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(exc)}), content_type="application/json")

    async def handle_desktop_focus(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        from .desktop import focus_desktop_window

        focused = focus_desktop_window()
        return web.json_response({"focused": focused})

    async def handle_extension_register_id(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        data = await request.json() if request.can_read_body else {}
        ext_id = str(data.get("extension_id", "")).strip()
        if not ext_id:
            return web.json_response({"ok": False}, status=400)
        from .browser_installer import register_native_messaging_host, sync_extension_files

        ext_dir = sync_extension_files(EXT_DIR)
        res = register_native_messaging_host(ext_dir, extra_extension_ids=[ext_id])
        return web.json_response({"ok": True, "native_messaging": res})

    async def handle_pause_all(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        count = await self.manager.pause_all()
        return web.json_response({"paused": count})

    async def handle_resume_all(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        count = await self.manager.resume_all()
        return web.json_response({"resumed": count})

    async def handle_clear_completed(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        count = await self.manager.clear_completed()
        return web.json_response({"cleared": count})

    async def handle_open_file(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        import os
        import subprocess

        rec = self.manager.get_download(request.match_info["id"], include_segments=False)
        if not rec or not Path(rec.file_path).exists():
            raise web.HTTPNotFound(text=json.dumps({"error": "Ficheiro não encontrado no disco"}), content_type="application/json")
        try:
            if sys.platform == "win32":
                os.startfile(rec.file_path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", rec.file_path])
            else:
                subprocess.Popen(["xdg-open", rec.file_path])
            return web.json_response({"opened": True})
        except Exception as exc:
            return web.json_response({"opened": False, "error": str(exc)}, status=500)

    async def handle_reveal_file(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        import subprocess

        rec = self.manager.get_download(request.match_info["id"], include_segments=False)
        if not rec:
            raise web.HTTPNotFound(text=json.dumps({"error": "Download não encontrado"}), content_type="application/json")
        fp = Path(rec.file_path)
        target_dir = str(fp.parent if fp.parent.exists() else self.manager.config.download_dir)
        try:
            if sys.platform == "win32":
                if fp.exists():
                    subprocess.Popen(["explorer.exe", f"/select,{fp}"])
                else:
                    subprocess.Popen(["explorer.exe", target_dir])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(fp)] if fp.exists() else ["open", target_dir])
            else:
                subprocess.Popen(["xdg-open", target_dir])
            return web.json_response({"revealed": True})
        except Exception as exc:
            return web.json_response({"revealed": False, "error": str(exc)}, status=500)

    async def handle_refresh_url(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        data = await request.json()
        new_url = str(data.get("url", "")).strip()
        try:
            rec = await self.manager.refresh_download_url(
                request.match_info["id"],
                new_url,
                headers=data.get("headers"),
                resume=bool(data.get("resume", True)),
            )
            if not rec:
                raise web.HTTPNotFound(text=json.dumps({"error": "Download não encontrado"}), content_type="application/json")
            return web.json_response(rec.to_dict(include_segments=True))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(exc)}), content_type="application/json")

    async def handle_pick_folder(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        from .desktop import pick_folder_dialog

        chosen = await asyncio.to_thread(pick_folder_dialog, str(self.manager.config.download_dir))
        return web.json_response({"path": chosen})

    async def handle_clipboard(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        text = ""
        if sys.platform == "win32":
            try:
                import ctypes

                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32
                if user32.OpenClipboard(0):
                    try:
                        h = user32.GetClipboardData(13)  # CF_UNICODETEXT
                        if h:
                            ptr = kernel32.GlobalLock(h)
                            if ptr:
                                try:
                                    text = ctypes.wstring_at(ptr)
                                finally:
                                    kernel32.GlobalUnlock(h)
                    finally:
                        user32.CloseClipboard()
            except Exception:
                text = ""
        return web.json_response({"text": text})

    async def handle_open_downloads(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        import os
        import subprocess

        folder = str(self.manager.config.download_dir)
        Path(folder).mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
            return web.json_response({"opened": True, "path": folder})
        except Exception as exc:
            return web.json_response({"opened": False, "path": folder, "error": str(exc)})

    async def handle_ytdlp_status(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        import urllib.request
        from .engine.youtube_engine import _import_ytdlp, get_ffmpeg_path

        local_ver = "—"
        try:
            yt_dlp = _import_ytdlp()
            local_ver = str(getattr(yt_dlp.version, "__version__", "unknown"))
        except Exception:
            pass

        latest_ver = ""
        try:
            def _fetch_pypi() -> str:
                with urllib.request.urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=3.0) as r:
                    return str(json.loads(r.read().decode("utf-8")).get("info", {}).get("version", ""))

            latest_ver = await asyncio.to_thread(_fetch_pypi)
        except Exception:
            pass

        return web.json_response({
            "yt_dlp_version": local_ver,
            "latest_version": latest_ver,
            "update_available": bool(latest_ver and local_ver != "—" and latest_ver != local_ver),
            "has_ffmpeg": bool(get_ffmpeg_path()),
        })

    async def handle_ytdlp_update(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        from .engine.youtube_engine import download_ytdlp_override_sync

        try:
            res = await asyncio.to_thread(download_ytdlp_override_sync)
            return web.json_response(res)
        except Exception as exc:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(exc)}), content_type="application/json")

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
        try:
            while cursor <= end:
                n = min(chunk_sz, end - cursor + 1)
                await resp.write(_deterministic_demo_bytes(cursor, n))
                cursor += n
                if delay_ms > 0:
                    extra = delay_ms * 2 if (skew and start == 0) else delay_ms
                    await asyncio.sleep(extra / 1000.0)
            await resp.write_eof()
        except (ConnectionResetError, asyncio.CancelledError, OSError):
            # Expected when client closes early during dynamic segment splitting or pause
            pass
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


def run_server(config: HyperDLConfig, open_browser: bool = False) -> None:
    mgr = DownloadManager(config)
    srv = HyperDLServer(mgr)
    if open_browser:
        browser_host = "127.0.0.1" if config.host in ("0.0.0.0", "::", "") else config.host
        url = f"http://{browser_host}:{config.port}"
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()
    web.run_app(srv.create_app(), host=config.host, port=config.port)
