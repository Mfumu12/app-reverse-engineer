"""HLS (.m3u8) Multi-Connection Stream Downloader & Segment Assembler."""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import aiohttp

from ..config import HyperDLConfig
from ..db import Database
from ..models import DownloadRecord, DownloadStatus, SegmentRecord, SegmentStatus
from ..utils.checksum import compute_checksum
from .throttle import TokenBucketLimiter

ATTR_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)")
ATTR_URI_RE = re.compile(r'URI="([^"]+)"')


def parse_m3u8(text: str, base_url: str) -> dict[str, object]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("Invalid M3U8 playlist (missing #EXTM3U header)")

    variants: list[tuple[int, str]] = []
    segments: list[str] = []
    init_url: str = ""
    encrypted = False

    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("#EXT-X-STREAM-INF:"):
            bw_m = ATTR_BANDWIDTH_RE.search(ln)
            bw = int(bw_m.group(1)) if bw_m else 0
            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                variants.append((bw, urljoin(base_url, lines[i + 1])))
                i += 2
                continue
        elif ln.startswith("#EXT-X-KEY:"):
            if "METHOD=NONE" not in ln.upper():
                encrypted = True
        elif ln.startswith("#EXT-X-MAP:"):
            um = ATTR_URI_RE.search(ln)
            if um:
                init_url = urljoin(base_url, um.group(1))
        elif not ln.startswith("#"):
            segments.append(urljoin(base_url, ln))
        i += 1

    return {
        "is_master": bool(variants),
        "variants": sorted(variants, key=lambda x: x[0], reverse=True),
        "init_url": init_url,
        "segments": segments,
        "encrypted": encrypted,
    }


class HlsDownloadJob:
    def __init__(
        self,
        record: DownloadRecord,
        db: Database,
        config: HyperDLConfig,
        session: aiohttp.ClientSession,
        global_limiter: TokenBucketLimiter,
        on_update: Callable[[DownloadRecord], None] | None = None,
    ) -> None:
        self.record = record
        self.db = db
        self.config = config
        self.session = session
        self.global_limiter = global_limiter
        self.local_limiter = TokenBucketLimiter(record.speed_limit_kbps)
        self.on_update = on_update

        self._stop_event = asyncio.Event()
        self._stop_reason: str = ""
        self._seg_lock = asyncio.Lock()
        self._segments: list[SegmentRecord] = []
        self._speed_window: deque[tuple[float, int]] = deque()
        self._last_db_sync = 0.0

    def set_speed_limit_kbps(self, kbps: int) -> None:
        self.record.speed_limit_kbps = max(0, int(kbps))
        self.local_limiter.set_rate_kbps(self.record.speed_limit_kbps)

    def request_pause(self) -> None:
        self._stop_reason = DownloadStatus.PAUSED.value
        self._stop_event.set()

    def request_cancel(self) -> None:
        self._stop_reason = DownloadStatus.CANCELLED.value
        self._stop_event.set()

    def _emit(self) -> None:
        if self.on_update:
            self.on_update(self.record)

    def _parts_dir(self) -> Path:
        p = Path(self.record.file_path)
        return p.parent / f".{p.name}.hls_parts"

    def _build_headers(self) -> dict[str, str]:
        hdrs = {"User-Agent": self.record.user_agent or self.config.user_agent}
        hdrs.update(self.record.headers or {})
        return hdrs

    async def _fetch_text(self, url: str) -> tuple[str, str]:
        async with self.session.get(url, headers=self._build_headers(), allow_redirects=True) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status} fetching playlist: {url}")
            return str(resp.url), await resp.text()

    def _recompute_metrics(self) -> None:
        now = time.monotonic()
        cutoff = now - 1.5
        while self._speed_window and self._speed_window[0][0] < cutoff:
            self._speed_window.popleft()
        window_bytes = sum(b for _, b in self._speed_window)
        span = (now - self._speed_window[0][0]) if len(self._speed_window) >= 2 else 1.0
        speed = window_bytes / max(0.25, span)

        done_segs = sum(1 for s in self._segments if s.status == SegmentStatus.COMPLETED.value)
        total_segs = len(self._segments)
        downloaded_bytes = sum(s.downloaded_bytes for s in self._segments)
        # Estimate total size dynamically from completed segments average
        if done_segs > 0 and total_segs > 0:
            avg_seg = downloaded_bytes / max(1, done_segs)
            if done_segs == total_segs:
                self.record.total_size = downloaded_bytes
            else:
                self.record.total_size = max(downloaded_bytes, int(avg_seg * total_segs))
        self.record.downloaded_bytes = downloaded_bytes
        self.record.active_connections = sum(
            1 for s in self._segments if s.status == SegmentStatus.DOWNLOADING.value
        )
        self.record.speed_bps = speed
        if self.record.total_size > downloaded_bytes and speed > 1:
            self.record.eta_seconds = (self.record.total_size - downloaded_bytes) / speed
        else:
            self.record.eta_seconds = None
        self.record.segments = list(self._segments)

    def _maybe_sync_db(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or (now - self._last_db_sync >= 0.5):
            self._last_db_sync = now
            self.db.sync_segments(self.record.id, self._segments)
            self.db.update_download(self.record)

    async def _download_hls_segment(self, seg: SegmentRecord, parts_dir: Path) -> None:
        out_part = parts_dir / f"seg_{seg.segment_index:06d}.part"
        retries = 0
        while not self._stop_event.is_set():
            try:
                async with self.session.get(
                    seg.segment_url, headers=self._build_headers(), allow_redirects=True
                ) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"HTTP {resp.status} for segment {seg.segment_index}")
                    tmp_path = out_part.with_suffix(".tmp")
                    written = 0
                    with open(tmp_path, "wb") as f:
                        while not self._stop_event.is_set():
                            chunk = await resp.content.read(self.config.chunk_read_size)
                            if not chunk:
                                break
                            await self.global_limiter.acquire(len(chunk))
                            await self.local_limiter.acquire(len(chunk))
                            f.write(chunk)
                            written += len(chunk)
                            now = time.monotonic()
                            async with self._seg_lock:
                                seg.downloaded_bytes = written
                                self._speed_window.append((now, len(chunk)))
                            self._recompute_metrics()
                            self._maybe_sync_db()
                            self._emit()

                    if self._stop_event.is_set():
                        tmp_path.unlink(missing_ok=True)
                        async with self._seg_lock:
                            seg.status = SegmentStatus.PAUSED.value
                            seg.downloaded_bytes = 0
                        return

                    tmp_path.replace(out_part)
                    async with self._seg_lock:
                        seg.start_byte = 0
                        seg.end_byte = max(0, written - 1)
                        seg.downloaded_bytes = written
                        seg.status = SegmentStatus.COMPLETED.value
                    self._recompute_metrics()
                    self._maybe_sync_db()
                    self._emit()
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retries += 1
                if self._stop_event.is_set():
                    async with self._seg_lock:
                        seg.status = SegmentStatus.PAUSED.value
                    return
                if retries > self.config.max_retries:
                    async with self._seg_lock:
                        seg.status = SegmentStatus.ERROR.value
                        seg.error_message = str(exc)
                    raise
                await asyncio.sleep(min(3.0, 0.3 * (2 ** (retries - 1))))

    async def run(self) -> DownloadRecord:
        try:
            self.record.status = DownloadStatus.PROBING.value
            self.record.error_message = ""
            self.db.update_download(self.record)
            self._emit()

            parts_dir = self._parts_dir()
            parts_dir.mkdir(parents=True, exist_ok=True)

            existing = self.db.get_segments(self.record.id)
            if existing:
                self._segments = existing
                for seg in self._segments:
                    part = parts_dir / f"seg_{seg.segment_index:06d}.part"
                    if seg.status == SegmentStatus.COMPLETED.value and part.exists():
                        continue
                    seg.status = SegmentStatus.PENDING.value
                    seg.downloaded_bytes = 0
            else:
                final_url, text = await self._fetch_text(self.record.url)
                parsed = parse_m3u8(text, final_url)
                if parsed["is_master"]:
                    variants = parsed["variants"]  # type: ignore[assignment]
                    best_url = variants[0][1]
                    final_url, text = await self._fetch_text(best_url)
                    parsed = parse_m3u8(text, final_url)

                if parsed["encrypted"]:
                    raise RuntimeError("Encrypted HLS streams (#EXT-X-KEY) are not supported without a decryption key")

                urls: list[str] = []
                if parsed["init_url"]:
                    urls.append(str(parsed["init_url"]))
                urls.extend(parsed["segments"])  # type: ignore[arg-type]
                if not urls:
                    raise RuntimeError("No media segments found in HLS playlist")

                self.record.final_url = final_url
                self.record.supports_range = True
                self._segments = [
                    SegmentRecord(
                        id=0,
                        download_id=self.record.id,
                        segment_index=idx,
                        start_byte=0,
                        end_byte=0,
                        downloaded_bytes=0,
                        status=SegmentStatus.PENDING.value,
                        segment_url=seg_url,
                    )
                    for idx, seg_url in enumerate(urls)
                ]
                self.db.replace_segments(self.record.id, self._segments)

            self.record.status = DownloadStatus.DOWNLOADING.value
            self._recompute_metrics()
            self.db.update_download(self.record)
            self._emit()

            queue: asyncio.Queue[SegmentRecord] = asyncio.Queue()
            for seg in self._segments:
                if seg.status != SegmentStatus.COMPLETED.value:
                    await queue.put(seg)

            errors: list[Exception] = []
            max_workers = max(1, min(self.record.max_connections, self.config.max_connections_cap))

            async def worker() -> None:
                while not self._stop_event.is_set():
                    try:
                        seg = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        async with self._seg_lock:
                            seg.status = SegmentStatus.DOWNLOADING.value
                        await self._download_hls_segment(seg, parts_dir)
                    except Exception as exc:
                        errors.append(exc)
                        return
                    finally:
                        queue.task_done()

            workers = [asyncio.create_task(worker()) for _ in range(max_workers)]
            await asyncio.gather(*workers, return_exceptions=False)

            if self._stop_event.is_set():
                self.record.status = self._stop_reason or DownloadStatus.PAUSED.value
                self.record.speed_bps = 0.0
                self.record.active_connections = 0
                if self._stop_reason == DownloadStatus.CANCELLED.value:
                    shutil.rmtree(parts_dir, ignore_errors=True)
                self._recompute_metrics()
                self._maybe_sync_db(force=True)
                self._emit()
                return self.record

            if errors:
                raise errors[0]

            self.record.status = DownloadStatus.ASSEMBLING.value
            self._emit()

            target = Path(self.record.file_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_out = target.with_name(f"{target.name}.assembling")

            def assemble_sync() -> int:
                total = 0
                with open(tmp_out, "wb") as out_f:
                    for seg in sorted(self._segments, key=lambda s: s.segment_index):
                        p = parts_dir / f"seg_{seg.segment_index:06d}.part"
                        with open(p, "rb") as in_f:
                            while True:
                                buf = in_f.read(1024 * 1024)
                                if not buf:
                                    break
                                out_f.write(buf)
                                total += len(buf)
                tmp_out.replace(target)
                shutil.rmtree(parts_dir, ignore_errors=True)
                return total

            total_bytes = await asyncio.to_thread(assemble_sync)
            self.record.total_size = total_bytes
            self.record.downloaded_bytes = total_bytes

            if self.record.expected_checksum:
                self.record.actual_checksum = await compute_checksum(
                    self.record.file_path, self.record.checksum_algorithm or "sha256"
                )
                if self.record.actual_checksum.lower() != self.record.expected_checksum.lower():
                    raise RuntimeError("Checksum mismatch on assembled HLS stream")

            self.record.status = DownloadStatus.COMPLETED.value
            self.record.speed_bps = 0.0
            self.record.eta_seconds = 0.0
            self.record.active_connections = 0
            self.record.completed_at = time.time()
            self._maybe_sync_db(force=True)
            self._emit()
            return self.record

        except Exception as exc:
            self.record.status = DownloadStatus.ERROR.value
            self.record.error_message = str(exc)
            self.record.speed_bps = 0.0
            self.record.active_connections = 0
            self._maybe_sync_db(force=True)
            self._emit()
            return self.record
