"""YouTube & video-platform probing and multi-format download engine powered by yt-dlp + FFmpeg."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

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


def extract_playlist_id(url: str) -> str:
    try:
        qs = parse_qs(urlparse(url.strip()).query)
        vals = qs.get("list") or []
        return vals[0].strip() if vals else ""
    except Exception:
        return ""


def is_pure_playlist_url(url: str) -> bool:
    try:
        u = urlparse(url.strip())
        return is_youtube_url(url) and u.path.rstrip("/").endswith("/playlist") and bool(extract_playlist_id(url))
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
    subtitles: bool = False,
    embed_metadata: bool = True,
) -> dict[str, Any]:
    """Return yt-dlp format string, merge_output_format, and postprocessors."""
    mode = (media_mode or "video").lower()
    cont = (container or ("mp3" if mode == "audio" else "mp4")).lower()
    q = str(quality or "best").lower().rstrip("p")

    if mode == "audio":
        if cont not in ("mp3", "m4a", "opus", "webm", "wav", "flac"):
            cont = "mp3"
        postprocessors: list[dict[str, Any]] = []
        write_thumb = False
        if has_ffmpeg:
            codec = "opus" if cont == "webm" else cont
            pp: dict[str, Any] = {
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec,
            }
            if codec in ("mp3", "m4a", "opus") and str(audio_bitrate).isdigit():
                pp["preferredquality"] = str(audio_bitrate)
            postprocessors.append(pp)
            if embed_metadata:
                postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
                if codec in ("mp3", "m4a", "opus"):
                    postprocessors.append({"key": "EmbedThumbnail"})
                    write_thumb = True
            fmt = "bestaudio/best"
        else:
            fmt = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
        return {
            "format": fmt,
            "merge_output_format": None,
            "postprocessors": postprocessors,
            "target_ext": cont,
            "writethumbnail": write_thumb,
            "writesubtitles": False,
        }

    # Video + Audio mode
    if cont not in ("mp4", "mkv", "webm"):
        cont = "mp4"
    height_filter = f"[height<={int(q)}]" if q.isdigit() else ""
    video_pps: list[dict[str, Any]] = []
    if has_ffmpeg:
        if embed_metadata:
            video_pps.append({"key": "FFmpegMetadata", "add_metadata": True})
        if subtitles:
            video_pps.append({"key": "FFmpegEmbedSubtitle"})
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
            "postprocessors": video_pps,
            "target_ext": cont,
            "writethumbnail": False,
            "writesubtitles": bool(subtitles),
        }

    # Without FFmpeg fallback: progressive single stream
    fmt = f"best{height_filter}[ext={cont}]/best{height_filter}/best"
    return {
        "format": fmt,
        "merge_output_format": None,
        "postprocessors": [],
        "target_ext": cont,
        "writethumbnail": False,
        "writesubtitles": bool(subtitles),
    }


def _extract_probe_info(info: dict[str, Any], ffmpeg_path: str | None) -> dict[str, Any]:
    """Normalize yt-dlp extract_info dict into clean UI options."""
    formats = info.get("formats") or []
    duration_s = float(info.get("duration") or 0)
    best_audio_size = 0
    heights: dict[int, dict[str, Any]] = {}

    for f in formats:
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        size = int(f.get("filesize") or f.get("filesize_approx") or 0)
        if size <= 0 and duration_s > 0 and f.get("tbr"):
            size = int(float(f["tbr"]) * 1000.0 / 8.0 * duration_s)
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


def _default_options_payload(ffmpeg_path: str | None) -> dict[str, Any]:
    return {
        "has_ffmpeg": bool(ffmpeg_path),
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


def _extract_playlist_sync(url: str, limit: int = 100) -> dict[str, Any]:
    import yt_dlp  # type: ignore[import-not-found]

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "playlistend": limit,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        return {"playlist_title": "", "entries": []}
    raw_entries = info.get("entries") or []
    entries: list[dict[str, Any]] = []
    first_thumb = ""
    for e in raw_entries:
        if not isinstance(e, dict):
            continue
        vid = e.get("id") or ""
        entry_url = e.get("url") or e.get("webpage_url") or ""
        if entry_url and not entry_url.startswith("http"):
            entry_url = f"https://www.youtube.com/watch?v={entry_url}"
        elif not entry_url and vid:
            entry_url = f"https://www.youtube.com/watch?v={vid}"
        if not entry_url:
            continue
        thumbs = e.get("thumbnails") or []
        if not first_thumb and thumbs and isinstance(thumbs, list):
            first_thumb = thumbs[-1].get("url") or ""
        entries.append({
            "id": vid,
            "url": entry_url,
            "title": e.get("title") or vid or "Vídeo",
            "duration_text": _fmt_duration(e.get("duration")),
        })
    if not first_thumb:
        p_thumbs = info.get("thumbnails") or []
        if p_thumbs and isinstance(p_thumbs, list):
            first_thumb = p_thumbs[-1].get("url") or ""
    return {
        "playlist_title": info.get("title") or "Playlist YouTube",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "thumbnail": first_thumb,
        "entries": entries,
    }


async def expand_youtube_playlist(url: str, limit: int = 100) -> dict[str, Any]:
    return await asyncio.to_thread(_extract_playlist_sync, url, limit)


def probe_youtube_sync(url: str) -> dict[str, Any]:
    import yt_dlp  # type: ignore[import-not-found]

    ffmpeg_path = get_ffmpeg_path()
    list_id = extract_playlist_id(url)

    if is_pure_playlist_url(url):
        pl = _extract_playlist_sync(url, limit=100)
        labels = [
            {"value": "best", "label": "Melhor disponível", "estimated_size": 0},
            {"value": "2160", "label": "2160p (4K UHD)", "estimated_size": 0},
            {"value": "1440", "label": "1440p (2K QHD)", "estimated_size": 0},
            {"value": "1080", "label": "1080p (Full HD)", "estimated_size": 0},
            {"value": "720", "label": "720p (HD)", "estimated_size": 0},
            {"value": "480", "label": "480p (SD)", "estimated_size": 0},
            {"value": "360", "label": "360p", "estimated_size": 0},
        ]
        return {
            "title": pl["playlist_title"] or "Playlist YouTube",
            "uploader": pl.get("uploader") or "YouTube Playlist",
            "duration": 0,
            "duration_text": f"{len(pl['entries'])} itens",
            "thumbnail": pl.get("thumbnail") or "",
            "webpage_url": url,
            "is_playlist": True,
            "has_playlist": True,
            "playlist_id": list_id,
            "playlist_title": pl["playlist_title"],
            "playlist_count": len(pl["entries"]),
            "video_qualities": labels,
            **_default_options_payload(ffmpeg_path),
        }

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
    res = _extract_probe_info(info, ffmpeg_path)
    res["is_playlist"] = False
    res["has_playlist"] = bool(list_id)
    res["playlist_id"] = list_id
    res["playlist_title"] = ""
    res["playlist_count"] = 0
    if list_id:
        try:
            pl = _extract_playlist_sync(url, limit=100)
            if pl["entries"]:
                res["playlist_title"] = pl["playlist_title"]
                res["playlist_count"] = len(pl["entries"])
        except Exception:
            pass
    return res


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
        subtitles = bool(opts.get("subtitles", False))
        embed_metadata = bool(opts.get("embed_metadata", True))
        ffmpeg_path = get_ffmpeg_path()

        spec = build_ydl_format_spec(
            media_mode=media_mode,
            quality=quality,
            container=container,
            audio_bitrate=audio_bitrate,
            has_ffmpeg=bool(ffmpeg_path),
            subtitles=subtitles,
            embed_metadata=embed_metadata,
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
        if spec.get("writethumbnail"):
            ydl_opts["writethumbnail"] = True
        if spec.get("writesubtitles"):
            ydl_opts["writesubtitles"] = True
            ydl_opts["writeautomaticsub"] = True
            ydl_opts["subtitleslangs"] = ["pt", "pt-PT", "pt-BR", "en"]
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
