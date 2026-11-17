"""Data models for HyperDL."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DownloadStatus(str, Enum):
    QUEUED = "queued"
    PROBING = "probing"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    ASSEMBLING = "assembling"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


class SegmentStatus(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


class DownloadType(str, Enum):
    HTTP = "http"
    HLS = "hls"
    YOUTUBE = "youtube"


class Category(str, Enum):
    COMPRESSED = "compressed"
    DOCUMENTS = "documents"
    MUSIC = "music"
    VIDEO = "video"
    PROGRAMS = "programs"
    OTHER = "other"


CATEGORY_EXTENSIONS: dict[Category, set[str]] = {
    Category.COMPRESSED: {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst", "iso", "cab"},
    Category.DOCUMENTS: {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv", "epub", "odt"},
    Category.MUSIC: {"mp3", "flac", "wav", "aac", "ogg", "opus", "m4a", "wma"},
    Category.VIDEO: {"mp4", "mkv", "webm", "avi", "mov", "flv", "wmv", "ts", "m4v", "m3u8"},
    Category.PROGRAMS: {"exe", "msi", "dmg", "pkg", "deb", "rpm", "appimage", "apk", "jar", "bin"},
}


def detect_category(filename: str, mime_type: str = "", download_type: str = "http") -> str:
    if download_type == DownloadType.HLS.value:
        return Category.VIDEO.value
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    for cat, exts in CATEGORY_EXTENSIONS.items():
        if ext in exts:
            return cat.value
    mime = (mime_type or "").lower()
    if mime.startswith("video/"):
        return Category.VIDEO.value
    if mime.startswith("audio/"):
        return Category.MUSIC.value
    if mime in ("application/zip", "application/x-rar-compressed", "application/x-7z-compressed", "application/gzip"):
        return Category.COMPRESSED.value
    if mime in ("application/pdf", "text/plain", "text/csv"):
        return Category.DOCUMENTS.value
    if mime in ("application/x-msdownload", "application/x-dosexec", "application/vnd.android.package-archive"):
        return Category.PROGRAMS.value
    return Category.OTHER.value


@dataclass
class SegmentRecord:
    id: int
    download_id: str
    segment_index: int
    start_byte: int
    end_byte: int  # inclusive for HTTP byte-range, or segment duration*1000 / index for HLS
    downloaded_bytes: int = 0
    status: str = SegmentStatus.PENDING.value
    segment_url: str = ""  # used by HLS segments
    speed_bps: float = 0.0
    error_message: str = ""

    @property
    def total_bytes(self) -> int:
        if self.end_byte < self.start_byte:
            return 0
        return self.end_byte - self.start_byte + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "download_id": self.download_id,
            "segment_index": self.segment_index,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "total_bytes": self.total_bytes,
            "downloaded_bytes": self.downloaded_bytes,
            "status": self.status,
            "segment_url": self.segment_url,
            "speed_bps": round(self.speed_bps, 1),
            "error_message": self.error_message,
        }


@dataclass
class DownloadRecord:
    id: str
    url: str
    final_url: str
    filename: str
    save_dir: str
    file_path: str
    total_size: int  # -1 if unknown
    downloaded_bytes: int
    status: str
    download_type: str
    category: str
    queue_id: str
    max_connections: int
    speed_limit_kbps: int
    supports_range: bool
    etag: str
    last_modified: str
    mime_type: str
    headers: dict[str, str] = field(default_factory=dict)
    user_agent: str = ""
    error_message: str = ""
    expected_checksum: str = ""
    checksum_algorithm: str = "sha256"
    actual_checksum: str = ""
    media_options: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0
    # Runtime telemetry (not necessarily persisted)
    speed_bps: float = 0.0
    eta_seconds: float | None = None
    active_connections: int = 0
    split_count: int = 0
    segments: list[SegmentRecord] = field(default_factory=list)

    def to_dict(self, include_segments: bool = False) -> dict[str, Any]:
        progress = 0.0
        if self.total_size > 0:
            progress = min(100.0, (self.downloaded_bytes / self.total_size) * 100.0)
        elif self.status == DownloadStatus.COMPLETED.value:
            progress = 100.0
        d: dict[str, Any] = {
            "id": self.id,
            "url": self.url,
            "final_url": self.final_url,
            "filename": self.filename,
            "save_dir": self.save_dir,
            "file_path": self.file_path,
            "total_size": self.total_size,
            "downloaded_bytes": self.downloaded_bytes,
            "progress": round(progress, 2),
            "status": self.status,
            "download_type": self.download_type,
            "category": self.category,
            "queue_id": self.queue_id,
            "max_connections": self.max_connections,
            "speed_limit_kbps": self.speed_limit_kbps,
            "supports_range": self.supports_range,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "mime_type": self.mime_type,
            "headers": self.headers,
            "user_agent": self.user_agent,
            "error_message": self.error_message,
            "expected_checksum": self.expected_checksum,
            "checksum_algorithm": self.checksum_algorithm,
            "actual_checksum": self.actual_checksum,
            "media_options": self.media_options,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "speed_bps": round(self.speed_bps, 1),
            "eta_seconds": round(self.eta_seconds, 1) if self.eta_seconds is not None else None,
            "active_connections": self.active_connections,
            "split_count": self.split_count,
        }
        if include_segments:
            d["segments"] = [s.to_dict() for s in self.segments]
        return d


@dataclass
class QueueRecord:
    id: str
    name: str
    max_concurrent: int = 3
    is_running: bool = True
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "max_concurrent": self.max_concurrent,
            "is_running": self.is_running,
            "created_at": self.created_at,
        }
