"""YouTube & video-platform probing and multi-format download engine powered by yt-dlp + FFmpeg."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..config import HyperDLConfig
from ..db import Database
from ..models import Category, DownloadRecord, DownloadStatus, SegmentRecord, SegmentStatus
from ..utils.filename import sanitize_filename
from .throttle import TokenBucketLimiter

YOUTUBE_HOST_RE = re.compile(
    r"(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com|music\.youtube\.com)$",
    re.IGNORECASE,
)


def is_youtube_url(url: str) -> bool:
    try:
        host = (urlparse(url.strip()).hostname or "").lower()
        return bool(YOUTUBE_HOST_RE.search(host))
    except Exception:
        return False


def get_ffmpeg_path() -> str | None:
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return None


def _fmt_duration(seconds: Any) -> str:
    if not seconds:
        return ""
    s = int(float(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def build_ydl_format_spec(
    *,
    media_mode: str = "video",
    quality: str = "best",
    container: str = "mp4",
    audio_bitrate: str = "320",
    has_ffmpeg: bool = True,
) -> dict[str, Any]:
    """Return yt-dlp format string, merge_output_format, and postprocessors."""
    mode = (media_mode or "video").lower()
    cont = (container or ("mp3" if mode == "audio" else "mp4")).lower()
    q = str(quality or "best").lower().rstrip("p")

    if mode == "audio":
        if cont not in ("mp3", "m4a", "opus", "webm", "wav", "flac"):
            cont = "mp3"
        postprocessors: list[dict[str, Any]] = []
        if has_ffmpeg:
            codec = "opus" if cont == "webm" else cont
            pp: dict[str, Any] = {
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec,
            }
            if codec in ("mp3", "m4a", "opus") and str(audio_bitrate).isdigit():
                pp["preferredquality"] = str(audio_bitrate)
            postprocessors.append(pp)
            fmt = "bestaudio/best"
        else:
            fmt = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
        return {
            "format": fmt,
            "merge_output_format": None,
            "postprocessors": postprocessors,
            "target_ext": cont,
        }

    # Video + Audio mode
    if cont not in ("mp4", "mkv", "webm"):
        cont = "mp4"
    height_filter = f"[height<={int(q)}]" if q.isdigit() else ""

    if has_ffmpeg:
        if cont == "mp4":
            fmt = (
                f"bestvideo{height_filter}[ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo{height_filter}+bestaudio/"
                f"best{height_filter}[ext=mp4]/best{height_filter}"
            )
        elif cont == "webm":
            fmt = (
                f"bestvideo{height_filter}[ext=webm]+bestaudio[ext=webm]/"
                f"bestvideo{height_filter}+bestaudio/"
                f"best{height_filter}[ext=webm]/best{height_filter}"
            )
        else:
            fmt = f"bestvideo{height_filter}+bestaudio/best{height_filter}"
        return {
            "format": fmt,
            "merge_output_format": cont,
            "postprocessors": [],
            "target_ext": cont,
        }

    # Without FFmpeg fallback: progressive single stream
    fmt = f"best{height_filter}[ext={cont}]/best{height_filter}/best"
    return {
        "format": fmt,
        "merge_output_format": None,
        "postprocessors": [],
        "target_ext": cont,
    }


def _extract_probe_info(info: dict[str, Any], ffmpeg_path: str | None) -> dict[str, Any]:
    """Normalize yt-dlp extract_info dict into clean UI options."""
    formats = info.get("formats") or []
    best_audio_size = 0
    heights: dict[int, dict[str, Any]] = {}

    for f in formats:
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        size = int(f.get("filesize") or f.get("filesize_approx") or 0)
        if vcodec == "none" and acodec != "none":
            if size > best_audio_size:
                best_audio_size = size
            continue
        height = f.get("height")
        if vcodec != "none" and height:
            h = int(height)
            prev = heights.get(h)
            ext = f.get("ext") or ""
            # Prefer larger known filesize or mp4 estimate
            if not prev or (size > prev["video_size"]) or (ext == "mp4" and prev["ext"] != "mp4"):
                heights[h] = {
                    "height": h,
                    "ext": ext,
                    "video_size": size,
                    "fps": f.get("fps") or 0,
                    "has_audio": acodec != "none",
                }

    video_qualities: list[dict[str, Any]] = [
        {"value": "best", "label": "Melhor disponível", "estimated_size": 0}
    ]
    labels_map = {
        2160: "2160p (4K UHD)",
        1440: "1440p (2K QHD)",
        1080: "1080p (Full HD)",
        720: "720p (HD)",
        480: "480p (SD)",
        360: "360p",
        240: "240p",
    }
    for h in sorted(heights.keys(), reverse=True):
        if h < 144:
            continue
        item = heights[h]
        est = item["video_size"] + (0 if item["has_audio"] else best_audio_size)
        fps_txt = f" {int(item['fps'])}fps" if item.get("fps", 0) and int(item["fps"]) > 30 else ""
        base_lbl = labels_map.get(h, f"{h}p")
        video_qualities.append({
            "value": str(h),
            "label": f"{base_lbl}{fps_txt}",
            "estimated_size": est,
        })

    if len(video_qualities) == 1:
        for h in (1080, 720, 480, 360):
            video_qualities.append({"value": str(h), "label": labels_map[h], "estimated_size": 0})

    return {
        "title": info.get("title") or "YouTube Video",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": int(info.get("duration") or 0),
        "duration_text": _fmt_duration(info.get("duration")),
        "thumbnail": info.get("thumbnail") or "",
        "webpage_url": info.get("webpage_url") or "",
        "has_ffmpeg": bool(ffmpeg_path),
        "video_qualities": video_qualities,
        "video_formats": [
            {"value": "mp4", "label": "MP4 (Universal)"},
            {"value": "mkv", "label": "MKV (Matroska)"},
            {"value": "webm", "label": "WebM (VP9/AV1)"},
        ],
        "audio_formats": [
            {"value": "mp3", "label": "MP3 (Áudio universal)"},
            {"value": "m4a", "label": "M4A (AAC alta qualidade)"},
            {"value": "opus", "label": "Opus (Original / Leve)"},
        ],
        "audio_qualities": [
            {"value": "320", "label": "320 kbps (Alta)"},
            {"value": "192", "label": "192 kbps (Média)"},
            {"value": "128", "label": "128 kbps (Compacta)"},
        ],
    }


def probe_youtube_sync(url: str) -> dict[str, Any]:
    import yt_dlp  # type: ignore[import-not-found]

    ffmpeg_path = get_ffmpeg_path()
    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise ValueError("Não foi possível obter informações do vídeo.")
    return _extract_probe_info(info, ffmpeg_path)


async def probe_youtube(url: str) -> dict[str, Any]:
    return await asyncio.to_thread(probe_youtube_sync, url)


class _PauseRequested(Exception):
    pass


class _CancelRequested(Exception):
    pass


class YoutubeDownloadJob:
    """Downloads YouTube/platform video or audio-only via yt-dlp + FFmpeg."""

    def __init__(
        self,
        record: DownloadRecord,
        db: Database,
        config: HyperDLConfig,
        session: Any,
        global_limiter: TokenBucketLimiter,
        on_update: Callable[[DownloadRecord], None] | None = None,
    ) -> None:
        self.record = record
        self.db = db
        self.config = config
        self.session = session
        self.global_limiter = global_limiter
        self.on_update = on_update
        self._pause_requested = False
        self._cancel_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_emit = 0.0

    def request_pause(self) -> None:
        self._pause_requested = True

    def request_cancel(self) -> None:
        self._cancel_requested = True

    def _emit_now(self) -> None:
        if self.on_update:
            self.on_update(self.record)

    def _schedule_emit(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_emit) < 0.15:
            return
        self._last_emit = now
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._emit_now)

    def _update_segment(self, status: str, total: int = 0, downloaded: int = 0, speed: float = 0.0) -> None:
        if not self.record.segments:
            self.record.segments = [
                SegmentRecord(
                    id=0,
                    download_id=self.record.id,
                    segment_index=0,
                    start_byte=0,
                    end_byte=max(0, total - 1),
                    downloaded_bytes=downloaded,
                    status=status,
                    speed_bps=speed,
                )
            ]
        else:
            seg = self.record.segments[0]
            if total > 0:
                seg.end_byte = total - 1
            seg.downloaded_bytes = downloaded
            seg.status = status
            seg.speed_bps = speed

    def _run_sync(self) -> DownloadRecord:
        import yt_dlp  # type: ignore[import-not-found]

        opts = dict(self.record.media_options or {})
        media_mode = str(opts.get("media_mode", "video")).lower()
        quality = str(opts.get("quality", "best"))
        container = str(opts.get("container", "mp3" if media_mode == "audio" else "mp4"))
        audio_bitrate = str(opts.get("audio_bitrate", "320"))
        ffmpeg_path = get_ffmpeg_path()

        spec = build_ydl_format_spec(
            media_mode=media_mode,
            quality=quality,
            container=container,
            audio_bitrate=audio_bitrate,
            has_ffmpeg=bool(ffmpeg_path),
        )

        save_dir = Path(self.record.save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(save_dir / f"%(title).120B [{self.record.id[:6]}].%(ext)s")

        # Keep track of the output filepath reported by yt-dlp
        final_paths: list[str] = []

        def progress_hook(d: dict[str, Any]) -> None:
            if self._cancel_requested:
                raise _CancelRequested()
            if self._pause_requested:
                raise _PauseRequested()

            st = d.get("status")
            if st == "downloading":
                downloaded = int(d.get("downloaded_bytes") or 0)
                total = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
                speed = float(d.get("speed") or 0.0)
                eta = d.get("eta")

                self.record.status = DownloadStatus.DOWNLOADING.value
                self.record.downloaded_bytes = downloaded
                if total > 0:
                    self.record.total_size = total
                self.record.speed_bps = speed
                self.record.eta_seconds = float(eta) if eta is not None else None
                self.record.active_connections = 1

                fn = d.get("filename")
                if fn:
                    base_name = sanitize_filename(Path(fn).name)
                    if base_name:
                        self.record.filename = base_name

                self._update_segment(SegmentStatus.DOWNLOADING.value, total=total, downloaded=downloaded, speed=speed)
                self._schedule_emit()
            elif st == "finished":
                fn = d.get("filename")
                if fn:
                    final_paths.append(fn)
                total = int(d.get("total_bytes") or self.record.total_size or 0)
                self.record.status = DownloadStatus.ASSEMBLING.value
                if total > 0:
                    self.record.total_size = total
                    self.record.downloaded_bytes = total
                self.record.speed_bps = 0.0
                self._update_segment(SegmentStatus.COMPLETED.value, total=total, downloaded=total, speed=0.0)
                self._schedule_emit(force=True)

        def postprocessor_hook(d: dict[str, Any]) -> None:
            if d.get("status") == "finished":
                fp = (d.get("info_dict") or {}).get("filepath")
                if fp:
                    final_paths.append(fp)

        ydl_opts: dict[str, Any] = {
            "format": spec["format"],
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "continuedl": True,
            "retries": self.config.max_retries,
            "concurrent_fragment_downloads": max(1, min(4, self.record.max_connections)),
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
        }
        if ffmpeg_path:
            ydl_opts["ffmpeg_location"] = ffmpeg_path
        if spec["merge_output_format"]:
            ydl_opts["merge_output_format"] = spec["merge_output_format"]
        if spec["postprocessors"]:
            ydl_opts["postprocessors"] = spec["postprocessors"]
        if self.record.speed_limit_kbps > 0:
            ydl_opts["ratelimit"] = self.record.speed_limit_kbps * 1024
        elif self.config.global_speed_limit_kbps > 0:
            ydl_opts["ratelimit"] = self.config.global_speed_limit_kbps * 1024

        self.record.status = DownloadStatus.PROBING.value
        self._schedule_emit(force=True)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(self.record.url, download=True)
            resolved_path: Path | None = None
            if isinstance(info, dict):
                reqs = info.get("requested_downloads") or []
                if reqs and reqs[0].get("filepath"):
                    resolved_path = Path(reqs[0]["filepath"])
                elif info.get("_filename"):
                    resolved_path = Path(info["_filename"])
                if not resolved_path or not resolved_path.exists():
                    # Check post-processed filepath candidate
                    if final_paths:
                        cand = Path(final_paths[-1])
                        if cand.exists():
                            resolved_path = cand
                        else:
                            alt = cand.with_suffix(f".{spec['target_ext']}")
                            if alt.exists():
                                resolved_path = alt

            if resolved_path and resolved_path.exists():
                sz = resolved_path.stat().st_size
                self.record.file_path = str(resolved_path)
                self.record.filename = resolved_path.name
                self.record.total_size = sz
                self.record.downloaded_bytes = sz

        self.record.status = DownloadStatus.COMPLETED.value
        self.record.category = Category.MUSIC.value if media_mode == "audio" else Category.VIDEO.value
        self.record.active_connections = 0
        self.record.speed_bps = 0.0
        self.record.eta_seconds = 0.0
        self.record.completed_at = time.time()
        self._update_segment(
            SegmentStatus.COMPLETED.value,
            total=max(0, self.record.total_size),
            downloaded=max(0, self.record.downloaded_bytes),
            speed=0.0,
        )
        self.db.update_download(self.record)
        self._schedule_emit(force=True)
        return self.record

    async def run(self) -> DownloadRecord:
        self._loop = asyncio.get_running_loop()
        try:
            return await asyncio.to_thread(self._run_sync)
        except _PauseRequested:
            self.record.status = DownloadStatus.PAUSED.value
            self.record.active_connections = 0
            self.record.speed_bps = 0.0
            self.db.update_download(self.record)
            self._emit_now()
            return self.record
        except _CancelRequested:
            self.record.status = DownloadStatus.CANCELLED.value
            self.record.active_connections = 0
            self.record.speed_bps = 0.0
            self.db.update_download(self.record)
            self._emit_now()
            return self.record
        except Exception as exc:
            self.record.status = DownloadStatus.ERROR.value
            self.record.error_message = str(exc)
            self.record.active_connections = 0
            self.record.speed_bps = 0.0
            self.db.update_download(self.record)
            self._emit_now()
            return self.record
