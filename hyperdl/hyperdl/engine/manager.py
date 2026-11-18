"""DownloadManager: queue scheduler, active job registry, per-host rules, and telemetry."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import aiohttp

from ..config import HyperDLConfig
from ..db import Database
from ..models import Category, DownloadRecord, DownloadStatus, DownloadType, QueueRecord, detect_category
from ..utils.checksum import compute_checksum
from ..utils.filename import extract_filename, part_file_path, resolve_unique_path, sanitize_filename
from .hls_engine import HlsDownloadJob
from .http_engine import HttpDownloadJob
from .throttle import TokenBucketLimiter
from .youtube_engine import YoutubeDownloadJob, is_youtube_url

ActiveJob = HttpDownloadJob | HlsDownloadJob | YoutubeDownloadJob

CATEGORY_FOLDERS = {
    Category.VIDEO.value: "Vídeos",
    Category.MUSIC.value: "Músicas",
    Category.PROGRAMS.value: "Programas",
    Category.DOCUMENTS.value: "Documentos",
    Category.COMPRESSED.value: "Compactados",
    Category.OTHER.value: "Outros",
}


class DownloadManager:
    def __init__(self, config: HyperDLConfig) -> None:
        self.config = config
        self.config.ensure_dirs()
        self.db = Database(self.config.db_path)

        # Load persisted settings if any
        saved_limit = self.db.get_setting("global_speed_limit_kbps")
        if saved_limit.isdigit():
            self.config.global_speed_limit_kbps = int(saved_limit)
        saved_conns = self.db.get_setting("default_connections")
        if saved_conns.isdigit():
            self.config.default_connections = max(1, min(int(saved_conns), self.config.max_connections_cap))
        saved_dir = self.db.get_setting("download_dir")
        if saved_dir:
            self.config.download_dir = Path(saved_dir)
            self.config.download_dir.mkdir(parents=True, exist_ok=True)
        self.auto_categorize = self.db.get_setting("auto_categorize", "0") == "1"
        self.post_complete_action = self.db.get_setting("post_complete_action", "none") or "none"
        self.clipboard_monitor = self.db.get_setting("clipboard_monitor", "1") == "1"
        self.schedule_enabled = self.db.get_setting("schedule_enabled", "0") == "1"
        self.schedule_start = self.db.get_setting("schedule_start", "01:00") or "01:00"
        self.schedule_stop = self.db.get_setting("schedule_stop", "07:00") or "07:00"
        self._last_sched_minute = ""

        self.global_limiter = TokenBucketLimiter(self.config.global_speed_limit_kbps)
        self._session: aiohttp.ClientSession | None = None
        self._jobs: dict[str, ActiveJob] = {}
        self._tasks: dict[str, asyncio.Task[DownloadRecord]] = {}
        self._listeners: list[Callable[[str, Any], None]] = []
        self._schedule_lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=None,
                connect=self.config.connect_timeout_s,
                sock_read=self.config.read_timeout_s,
            )
            connector = aiohttp.TCPConnector(limit=128, limit_per_host=32, enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        await self.schedule_queues()

    async def stop(self) -> None:
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
            self._scheduler_task = None
        for job in list(self._jobs.values()):
            job.request_pause()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._jobs.clear()
        self._tasks.clear()
        if self._session and not self._session.closed:
            await self._session.close()
        self.db.close()

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(15.0)
                if not self.schedule_enabled:
                    continue
                now_hm = time.strftime("%H:%M")
                if now_hm == self._last_sched_minute:
                    continue
                self._last_sched_minute = now_hm
                if now_hm == self.schedule_start:
                    await self.resume_all()
                elif now_hm == self.schedule_stop:
                    await self.pause_all()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def add_listener(self, fn: Callable[[str, Any], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[str, Any], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _notify(self, event: str, payload: Any) -> None:
        for fn in list(self._listeners):
            try:
                fn(event, payload)
            except Exception:
                pass

    def _on_job_update(self, rec: DownloadRecord) -> None:
        self._notify("download_update", rec.to_dict(include_segments=True))

    def set_global_speed_limit_kbps(self, kbps: int) -> None:
        self.config.global_speed_limit_kbps = max(0, int(kbps))
        self.global_limiter.set_rate_kbps(self.config.global_speed_limit_kbps)
        self.db.set_setting("global_speed_limit_kbps", self.config.global_speed_limit_kbps)
        self._notify("settings_update", self.get_settings())

    def update_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        if "global_speed_limit_kbps" in data:
            self.set_global_speed_limit_kbps(int(data["global_speed_limit_kbps"]))
        if "default_connections" in data:
            val = max(1, min(int(data["default_connections"]), self.config.max_connections_cap))
            self.config.default_connections = val
            self.db.set_setting("default_connections", val)
        if "download_dir" in data and str(data["download_dir"]).strip():
            p = Path(str(data["download_dir"]).strip())
            p.mkdir(parents=True, exist_ok=True)
            self.config.download_dir = p
            self.db.set_setting("download_dir", str(p))
        if "api_key" in data:
            self.config.api_key = str(data["api_key"])
            self.db.set_setting("api_key", self.config.api_key)
        if "auto_categorize" in data:
            self.auto_categorize = bool(data["auto_categorize"])
            self.db.set_setting("auto_categorize", "1" if self.auto_categorize else "0")
        if "post_complete_action" in data:
            act = str(data["post_complete_action"]).strip().lower()
            if act in ("none", "open_folder", "sleep", "shutdown"):
                self.post_complete_action = act
                self.db.set_setting("post_complete_action", act)
        if "clipboard_monitor" in data:
            self.clipboard_monitor = bool(data["clipboard_monitor"])
            self.db.set_setting("clipboard_monitor", "1" if self.clipboard_monitor else "0")
        if "schedule_enabled" in data:
            self.schedule_enabled = bool(data["schedule_enabled"])
            self.db.set_setting("schedule_enabled", "1" if self.schedule_enabled else "0")
        if "schedule_start" in data and len(str(data["schedule_start"]).strip()) == 5:
            self.schedule_start = str(data["schedule_start"]).strip()
            self.db.set_setting("schedule_start", self.schedule_start)
        if "schedule_stop" in data and len(str(data["schedule_stop"]).strip()) == 5:
            self.schedule_stop = str(data["schedule_stop"]).strip()
            self.db.set_setting("schedule_stop", self.schedule_stop)
        self._notify("settings_update", self.get_settings())
        return self.get_settings()

    def get_settings(self) -> dict[str, Any]:
        return {
            "download_dir": str(self.config.download_dir),
            "default_connections": self.config.default_connections,
            "max_connections_cap": self.config.max_connections_cap,
            "global_speed_limit_kbps": self.config.global_speed_limit_kbps,
            "min_split_size": self.config.min_split_size,
            "api_key_enabled": bool(self.config.api_key),
            "auto_categorize": self.auto_categorize,
            "post_complete_action": self.post_complete_action,
            "clipboard_monitor": self.clipboard_monitor,
            "schedule_enabled": self.schedule_enabled,
            "schedule_start": self.schedule_start,
            "schedule_stop": self.schedule_stop,
        }

    async def add_download(
        self,
        url: str,
        *,
        filename: str | None = None,
        save_dir: str | None = None,
        save_subdir: str | None = None,
        queue_id: str = "main",
        max_connections: int | None = None,
        speed_limit_kbps: int | None = None,
        headers: dict[str, str] | None = None,
        user_agent: str | None = None,
        download_type: str | None = None,
        expected_checksum: str = "",
        checksum_algorithm: str = "sha256",
        media_options: dict[str, Any] | None = None,
        auto_start: bool = True,
    ) -> DownloadRecord:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")

        dl_type = download_type
        if not dl_type:
            path_part = urlparse(url).path.lower()
            if is_youtube_url(url) or (media_options and media_options.get("media_mode")):
                dl_type = DownloadType.YOUTUBE.value
            elif path_part.endswith(".m3u8"):
                dl_type = DownloadType.HLS.value
            else:
                dl_type = DownloadType.HTTP.value

        host = (urlparse(url).hostname or "").lower()
        host_cfg = self.db.get_host_settings(host) if host else None

        effective_conns = max_connections or (host_cfg["max_connections"] if host_cfg else self.config.default_connections)
        effective_conns = max(1, min(int(effective_conns), self.config.max_connections_cap))
        effective_limit = speed_limit_kbps if speed_limit_kbps is not None else (host_cfg["speed_limit_kbps"] if host_cfg else 0)
        effective_ua = user_agent or (host_cfg["user_agent"] if host_cfg else "")
        merged_headers: dict[str, str] = {}
        if host_cfg and host_cfg.get("headers"):
            merged_headers.update(host_cfg["headers"])
        if headers:
            merged_headers.update(headers)

        initial_name = extract_filename(url, suggested_name=filename)
        if dl_type == DownloadType.HLS.value and initial_name.lower().endswith(".m3u8"):
            initial_name = initial_name[:-5] + ".ts"
        if dl_type == DownloadType.YOUTUBE.value and not filename:
            yt_opts = media_options or {}
            ext = str(yt_opts.get("container") or ("mp3" if yt_opts.get("media_mode") == "audio" else "mp4")).lower()
            initial_name = f"youtube_media.{ext}"

        yt_cat = detect_category(initial_name, "", dl_type)
        if dl_type == DownloadType.YOUTUBE.value:
            yt_cat = Category.MUSIC.value if (media_options or {}).get("media_mode") == "audio" else Category.VIDEO.value

        target_dir = Path(save_dir) if save_dir else self.config.download_dir
        if not save_dir:
            sub = (save_subdir or "").strip()
            if sub == "auto" or (not sub and self.auto_categorize):
                target_dir = target_dir / CATEGORY_FOLDERS.get(yt_cat, "Outros")
            elif sub and sub not in (".", "/", "\\"):
                clean_parts = [sanitize_filename(p) for p in Path(sub).parts if p not in ("", ".", "..", "/", "\\")]
                if clean_parts:
                    target_dir = target_dir.joinpath(*clean_parts)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = resolve_unique_path(target_dir, initial_name)

        if not self.db.get_queue(queue_id):
            queue_id = "main"

        now = time.time()

        rec = DownloadRecord(
            id=uuid.uuid4().hex[:12],
            url=url,
            final_url=url,
            filename=target_path.name,
            save_dir=str(target_dir),
            file_path=str(target_path),
            total_size=-1,
            downloaded_bytes=0,
            status=DownloadStatus.QUEUED.value if auto_start else DownloadStatus.PAUSED.value,
            download_type=dl_type,
            category=yt_cat,
            queue_id=queue_id,
            max_connections=effective_conns,
            speed_limit_kbps=max(0, int(effective_limit)),
            supports_range=False,
            etag="",
            last_modified="",
            mime_type="",
            headers=merged_headers,
            user_agent=effective_ua,
            expected_checksum=expected_checksum.strip(),
            checksum_algorithm=checksum_algorithm.strip().lower() or "sha256",
            media_options=dict(media_options or {}),
            created_at=now,
            updated_at=now,
        )
        self.db.insert_download(rec)
        self._notify("download_added", rec.to_dict(include_segments=True))
        if auto_start:
            await self.schedule_queues()
        return self.get_download(rec.id, include_segments=True) or rec

    async def schedule_queues(self) -> None:
        if self._session is None or self._session.closed:
            return
        async with self._schedule_lock:
            queues = self.db.list_queues()
            for q in queues:
                if not q.is_running:
                    continue
                active_in_q = sum(
                    1
                    for job in self._jobs.values()
                    if job.record.queue_id == q.id
                    and job.record.status in (DownloadStatus.PROBING.value, DownloadStatus.DOWNLOADING.value, DownloadStatus.ASSEMBLING.value)
                )
                slots = max(0, q.max_concurrent - active_in_q)
                if slots <= 0:
                    continue
                queued = [
                    d
                    for d in self.db.list_downloads(status=DownloadStatus.QUEUED.value, queue_id=q.id)
                    if d.id not in self._tasks
                ]
                # FIFO order
                for rec in sorted(queued, key=lambda x: x.created_at)[:slots]:
                    self._launch_job(rec)

    def _launch_job(self, rec: DownloadRecord) -> None:
        assert self._session is not None
        if rec.id in self._tasks:
            return
        if rec.download_type == DownloadType.YOUTUBE.value:
            job: ActiveJob = YoutubeDownloadJob(
                rec, self.db, self.config, self._session, self.global_limiter, on_update=self._on_job_update
            )
        elif rec.download_type == DownloadType.HLS.value:
            job = HlsDownloadJob(
                rec, self.db, self.config, self._session, self.global_limiter, on_update=self._on_job_update
            )
        else:
            job = HttpDownloadJob(
                rec, self.db, self.config, self._session, self.global_limiter, on_update=self._on_job_update
            )
        self._jobs[rec.id] = job
        task = asyncio.create_task(self._run_job(rec.id, job))
        self._tasks[rec.id] = task

    async def _run_job(self, download_id: str, job: ActiveJob) -> DownloadRecord:
        try:
            res = await job.run()
            return res
        finally:
            self._jobs.pop(download_id, None)
            self._tasks.pop(download_id, None)
            await self.schedule_queues()
            if not self._jobs and self.post_complete_action != "none":
                queued_left = self.db.list_downloads(status=DownloadStatus.QUEUED.value)
                if not queued_left:
                    self._trigger_post_complete_action()

    def _trigger_post_complete_action(self) -> None:
        action = self.post_complete_action
        if action == "none":
            return
        # Reset one-shot power actions after triggering so next run won't surprise user
        if action in ("sleep", "shutdown"):
            self.post_complete_action = "none"
            self.db.set_setting("post_complete_action", "none")
            self._notify("settings_update", self.get_settings())
        try:
            if action == "open_folder":
                if sys.platform == "win32":
                    os.startfile(str(self.config.download_dir))  # type: ignore[attr-defined]
            elif action == "sleep" and sys.platform == "win32":
                subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
            elif action == "shutdown" and sys.platform == "win32":
                subprocess.Popen(["shutdown", "/s", "/t", "60"])
        except Exception:
            pass

    async def pause_all(self) -> int:
        count = 0
        for dl_id in list(self._jobs.keys()):
            await self.pause_download(dl_id)
            count += 1
        for rec in self.db.list_downloads(status=DownloadStatus.QUEUED.value):
            rec.status = DownloadStatus.PAUSED.value
            self.db.update_download(rec)
            self._notify("download_update", rec.to_dict(include_segments=True))
            count += 1
        return count

    async def resume_all(self) -> int:
        count = 0
        for rec in self.db.list_downloads(status=DownloadStatus.PAUSED.value):
            await self.resume_download(rec.id)
            count += 1
        return count

    async def clear_completed(self) -> int:
        count = 0
        for st in (DownloadStatus.COMPLETED.value, DownloadStatus.CANCELLED.value):
            for rec in self.db.list_downloads(status=st):
                self.db.delete_download(rec.id)
                self._notify("download_removed", {"id": rec.id})
                count += 1
        return count

    async def refresh_download_url(
        self,
        download_id: str,
        new_url: str,
        *,
        headers: dict[str, str] | None = None,
        resume: bool = True,
    ) -> DownloadRecord | None:
        clean_url = new_url.strip()
        if not clean_url.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        if download_id in self._jobs:
            await self.pause_download(download_id)
        rec = self.db.get_download(download_id, include_segments=True)
        if not rec:
            return None
        rec.url = clean_url
        rec.final_url = clean_url
        rec.error_message = ""
        if headers:
            rec.headers.update(headers)
        rec.updated_at = time.time()
        self.db.update_download(rec)
        self._notify("download_update", rec.to_dict(include_segments=True))
        if resume:
            return await self.resume_download(download_id)
        return rec

    def get_download(self, download_id: str, include_segments: bool = True) -> DownloadRecord | None:
        job = self._jobs.get(download_id)
        if job:
            return job.record
        return self.db.get_download(download_id, include_segments=include_segments)

    def list_downloads(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        queue_id: str | None = None,
        include_segments: bool = False,
    ) -> list[DownloadRecord]:
        rows = self.db.list_downloads(
            status=status, category=category, queue_id=queue_id, include_segments=include_segments
        )
        out: list[DownloadRecord] = []
        for r in rows:
            if r.id in self._jobs:
                live = self._jobs[r.id].record
                out.append(live)
            else:
                out.append(r)
        return out

    async def pause_download(self, download_id: str) -> DownloadRecord | None:
        job = self._jobs.get(download_id)
        if job:
            job.request_pause()
            task = self._tasks.get(download_id)
            if task:
                await asyncio.gather(task, return_exceptions=True)
            return self.get_download(download_id, include_segments=True)

        rec = self.db.get_download(download_id, include_segments=True)
        if rec and rec.status == DownloadStatus.QUEUED.value:
            rec.status = DownloadStatus.PAUSED.value
            self.db.update_download(rec)
            self._notify("download_update", rec.to_dict(include_segments=True))
        return rec

    async def resume_download(self, download_id: str) -> DownloadRecord | None:
        if download_id in self._jobs:
            return self._jobs[download_id].record
        rec = self.db.get_download(download_id, include_segments=True)
        if not rec:
            return None
        if rec.status == DownloadStatus.COMPLETED.value:
            return rec
        rec.status = DownloadStatus.QUEUED.value
        rec.error_message = ""
        self.db.update_download(rec)
        # Ensure target queue is running when user explicitly resumes
        self.db.update_queue(rec.queue_id, is_running=True)
        self._notify("download_update", rec.to_dict(include_segments=True))
        await self.schedule_queues()
        return self.get_download(download_id, include_segments=True)

    async def remove_download(self, download_id: str, delete_file: bool = False) -> bool:
        job = self._jobs.get(download_id)
        if job:
            job.request_cancel()
            task = self._tasks.get(download_id)
            if task:
                await asyncio.gather(task, return_exceptions=True)

        rec = self.db.get_download(download_id)
        if not rec:
            return False
        part_file_path(rec.file_path).unlink(missing_ok=True)
        if delete_file:
            Path(rec.file_path).unlink(missing_ok=True)
        self.db.delete_download(download_id)
        self._notify("download_removed", {"id": download_id})
        return True

    async def verify_checksum(self, download_id: str, algorithm: str = "sha256", expected: str = "") -> dict[str, Any]:
        rec = self.get_download(download_id, include_segments=False)
        if not rec:
            raise ValueError("Download not found")
        if rec.status != DownloadStatus.COMPLETED.value or not Path(rec.file_path).exists():
            raise ValueError("File is not completed or missing on disk")
        digest = await compute_checksum(rec.file_path, algorithm)
        rec.actual_checksum = digest
        rec.checksum_algorithm = algorithm.lower()
        if expected:
            rec.expected_checksum = expected.strip()
        self.db.update_download(rec)
        self._notify("download_update", rec.to_dict(include_segments=True))
        matches = (digest.lower() == rec.expected_checksum.lower()) if rec.expected_checksum else None
        return {"id": rec.id, "algorithm": rec.checksum_algorithm, "digest": digest, "expected": rec.expected_checksum, "matches": matches}

    async def start_queue(self, queue_id: str) -> QueueRecord | None:
        q = self.db.get_queue(queue_id)
        if not q:
            return None
        self.db.update_queue(queue_id, is_running=True)
        await self.schedule_queues()
        return self.db.get_queue(queue_id)

    async def stop_queue(self, queue_id: str) -> QueueRecord | None:
        q = self.db.get_queue(queue_id)
        if not q:
            return None
        self.db.update_queue(queue_id, is_running=False)
        for dl_id, job in list(self._jobs.items()):
            if job.record.queue_id == queue_id:
                await self.pause_download(dl_id)
        return self.db.get_queue(queue_id)
