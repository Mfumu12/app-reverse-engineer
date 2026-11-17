"""Dynamic Multi-Segment HTTP/HTTPS Download Engine (IDM work-stealing + ABD validation)."""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from pathlib import Path
from typing import Callable

import aiohttp

from ..config import HyperDLConfig
from ..db import Database
from ..models import DownloadRecord, DownloadStatus, SegmentRecord, SegmentStatus, detect_category
from ..utils.checksum import compute_checksum
from ..utils.filename import extract_filename, part_file_path, resolve_unique_path
from .storage import SegmentedFileWriter
from .throttle import TokenBucketLimiter

CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


class HttpDownloadJob:
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
        self._stop_reason: str = ""  # "paused" or "cancelled"
        self._seg_lock = asyncio.Lock()
        self._segments: list[SegmentRecord] = []
        self._speed_window: deque[tuple[float, int]] = deque()
        self._writer: SegmentedFileWriter | None = None
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

    def _build_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        hdrs = {"User-Agent": self.record.user_agent or self.config.user_agent}
        for k, v in (self.record.headers or {}).items():
            hdrs[k] = v
        if extra:
            hdrs.update(extra)
        return hdrs

    async def _probe(self) -> dict[str, object]:
        headers = self._build_headers()
        final_url = self.record.url
        total_size = -1
        supports_range = False
        etag = ""
        last_modified = ""
        mime_type = ""
        resp_headers: dict[str, str] = {}

        # 1. Try HEAD first
        try:
            async with self.session.head(
                self.record.url, headers=headers, allow_redirects=True
            ) as resp:
                if resp.status < 400:
                    final_url = str(resp.url)
                    resp_headers = dict(resp.headers)
                    cl = resp.headers.get("Content-Length")
                    if cl and cl.isdigit():
                        total_size = int(cl)
                    ar = resp.headers.get("Accept-Ranges", "")
                    supports_range = ar.lower() == "bytes"
                    etag = resp.headers.get("ETag", "")
                    last_modified = resp.headers.get("Last-Modified", "")
                    mime_type = resp.headers.get("Content-Type", "")
        except Exception:
            pass

        # 2. Fallback/verify with Range: bytes=0-0 if needed
        if not supports_range or total_size <= 0:
            probe_hdrs = self._build_headers({"Range": "bytes=0-0"})
            async with self.session.get(
                final_url, headers=probe_hdrs, allow_redirects=True
            ) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}: {resp.reason}")
                final_url = str(resp.url)
                resp_headers = dict(resp.headers)
                etag = resp.headers.get("ETag", etag)
                last_modified = resp.headers.get("Last-Modified", last_modified)
                mime_type = resp.headers.get("Content-Type", mime_type)

                cr = resp.headers.get("Content-Range", "")
                m = CONTENT_RANGE_RE.search(cr)
                if resp.status == 206 and m:
                    supports_range = True
                    if m.group(3) != "*":
                        total_size = int(m.group(3))
                else:
                    cl = resp.headers.get("Content-Length")
                    if cl and cl.isdigit():
                        total_size = int(cl)
                    ar = resp.headers.get("Accept-Ranges", "")
                    if ar.lower() == "bytes" and total_size > 0:
                        supports_range = True

        return {
            "final_url": final_url,
            "total_size": total_size,
            "supports_range": supports_range,
            "etag": etag,
            "last_modified": last_modified,
            "mime_type": mime_type,
            "resp_headers": resp_headers,
        }

    def _create_initial_segments(self, total_size: int, supports_range: bool) -> list[SegmentRecord]:
        if total_size <= 0 or not supports_range:
            end = total_size - 1 if total_size > 0 else -1
            return [
                SegmentRecord(
                    id=0,
                    download_id=self.record.id,
                    segment_index=0,
                    start_byte=0,
                    end_byte=end,
                    downloaded_bytes=0,
                    status=SegmentStatus.PENDING.value,
                )
            ]

        max_conns = max(1, min(self.record.max_connections, self.config.max_connections_cap))
        possible_parts = max(1, total_size // self.config.min_split_size)
        num_parts = min(max_conns, possible_parts)

        segments: list[SegmentRecord] = []
        chunk_size = total_size // num_parts
        offset = 0
        for idx in range(num_parts):
            end = (total_size - 1) if idx == num_parts - 1 else (offset + chunk_size - 1)
            segments.append(
                SegmentRecord(
                    id=0,
                    download_id=self.record.id,
                    segment_index=idx,
                    start_byte=offset,
                    end_byte=end,
                    downloaded_bytes=0,
                    status=SegmentStatus.PENDING.value,
                )
            )
            offset = end + 1
        return segments

    def _recompute_metrics(self) -> None:
        now = time.monotonic()
        cutoff = now - 1.5
        while self._speed_window and self._speed_window[0][0] < cutoff:
            self._speed_window.popleft()
        window_bytes = sum(b for _, b in self._speed_window)
        span = (now - self._speed_window[0][0]) if len(self._speed_window) >= 2 else 1.0
        speed = window_bytes / max(0.25, span)

        self.record.downloaded_bytes = sum(s.downloaded_bytes for s in self._segments)
        self.record.active_connections = sum(
            1 for s in self._segments if s.status == SegmentStatus.DOWNLOADING.value
        )
        self.record.speed_bps = speed
        if self.record.total_size > 0 and speed > 1:
            rem = max(0, self.record.total_size - self.record.downloaded_bytes)
            self.record.eta_seconds = rem / speed
        else:
            self.record.eta_seconds = None
        self.record.segments = list(self._segments)

    async def _claim_or_steal_segment(self) -> SegmentRecord | None:
        async with self._seg_lock:
            # 1. Claim any pending/paused/errored incomplete segment
            for seg in self._segments:
                if seg.status in (SegmentStatus.PENDING.value, SegmentStatus.PAUSED.value, SegmentStatus.ERROR.value):
                    if seg.end_byte >= 0 and seg.downloaded_bytes >= seg.total_bytes:
                        seg.status = SegmentStatus.COMPLETED.value
                        continue
                    seg.status = SegmentStatus.DOWNLOADING.value
                    seg.error_message = ""
                    return seg

            # 2. IDM-style Dynamic Work-Stealing: split largest active segment in half
            if not self.record.supports_range or self.record.total_size <= 0:
                return None

            best_seg: SegmentRecord | None = None
            best_remaining = 0
            for seg in self._segments:
                if seg.status == SegmentStatus.DOWNLOADING.value and seg.end_byte >= 0:
                    cursor = seg.start_byte + seg.downloaded_bytes
                    rem = seg.end_byte - cursor + 1
                    if rem > best_remaining:
                        best_remaining = rem
                        best_seg = seg

            if best_seg is None or best_remaining < self.config.min_split_size * 2:
                return None

            cursor = best_seg.start_byte + best_seg.downloaded_bytes
            midpoint = cursor + (best_remaining // 2)
            old_end = best_seg.end_byte
            best_seg.end_byte = midpoint - 1

            new_seg = SegmentRecord(
                id=0,
                download_id=self.record.id,
                segment_index=len(self._segments),
                start_byte=midpoint,
                end_byte=old_end,
                downloaded_bytes=0,
                status=SegmentStatus.DOWNLOADING.value,
            )
            self.db.insert_segment(new_seg)
            self.db.sync_segments(self.record.id, [best_seg])
            self._segments.append(new_seg)
            self.record.split_count += 1
            return new_seg

    async def _download_segment(self, seg: SegmentRecord) -> None:
        assert self._writer is not None
        retries = 0
        while not self._stop_event.is_set():
            async with self._seg_lock:
                cursor = seg.start_byte + seg.downloaded_bytes
                end_byte = seg.end_byte
                if end_byte >= 0 and cursor > end_byte:
                    seg.status = SegmentStatus.COMPLETED.value
                    seg.speed_bps = 0.0
                    return

            req_headers = self._build_headers()
            if self.record.supports_range and end_byte >= 0:
                req_headers["Range"] = f"bytes={cursor}-{end_byte}"
            elif cursor > 0 and self.record.supports_range:
                req_headers["Range"] = f"bytes={cursor}-"

            last_t = time.monotonic()
            bytes_since_t = 0
            try:
                async with self.session.get(
                    self.record.final_url or self.record.url,
                    headers=req_headers,
                    allow_redirects=True,
                ) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"HTTP {resp.status}: {resp.reason}")
                    if cursor > 0 and self.record.supports_range and resp.status != 206:
                        # Server ignored Range on resume; cannot safely seek-write
                        raise RuntimeError("Server ignored Range header during segmented/resumed request")

                    while not self._stop_event.is_set():
                        chunk = await resp.content.read(self.config.chunk_read_size)
                        if not chunk:
                            break

                        await self.global_limiter.acquire(len(chunk))
                        await self.local_limiter.acquire(len(chunk))

                        async with self._seg_lock:
                            cursor = seg.start_byte + seg.downloaded_bytes
                            if seg.end_byte >= 0:
                                allowed = seg.end_byte - cursor + 1
                                if allowed <= 0:
                                    seg.status = SegmentStatus.COMPLETED.value
                                    seg.speed_bps = 0.0
                                    return
                                if len(chunk) > allowed:
                                    chunk = chunk[:allowed]

                        await self._writer.write_at(cursor, chunk)

                        now = time.monotonic()
                        bytes_since_t += len(chunk)
                        async with self._seg_lock:
                            seg.downloaded_bytes += len(chunk)
                            self._speed_window.append((now, len(chunk)))
                            dt = now - last_t
                            if dt >= 0.3:
                                seg.speed_bps = bytes_since_t / dt
                                bytes_since_t = 0
                                last_t = now
                            if seg.end_byte >= 0 and (seg.start_byte + seg.downloaded_bytes) > seg.end_byte:
                                seg.status = SegmentStatus.COMPLETED.value
                                seg.speed_bps = 0.0
                                self._recompute_metrics()
                                self._maybe_sync_db()
                                self._emit()
                                return

                        self._recompute_metrics()
                        self._maybe_sync_db()
                        self._emit()

                # Stream reached EOF cleanly
                async with self._seg_lock:
                    if self._stop_event.is_set():
                        seg.status = SegmentStatus.PAUSED.value
                        seg.speed_bps = 0.0
                        return
                    if seg.end_byte < 0 or (seg.start_byte + seg.downloaded_bytes) > seg.end_byte:
                        seg.status = SegmentStatus.COMPLETED.value
                        seg.speed_bps = 0.0
                        return
                    # Incomplete EOF before end_byte: retry remaining bytes
                    raise RuntimeError("Unexpected EOF before segment end")

            except asyncio.CancelledError:
                async with self._seg_lock:
                    seg.status = SegmentStatus.PAUSED.value
                    seg.speed_bps = 0.0
                raise
            except Exception as exc:
                retries += 1
                if self._stop_event.is_set():
                    async with self._seg_lock:
                        seg.status = SegmentStatus.PAUSED.value
                        seg.speed_bps = 0.0
                    return
                if retries > self.config.max_retries:
                    async with self._seg_lock:
                        seg.status = SegmentStatus.ERROR.value
                        seg.error_message = str(exc)
                        seg.speed_bps = 0.0
                    raise
                await asyncio.sleep(min(4.0, 0.4 * (2 ** (retries - 1))))

    def _maybe_sync_db(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or (now - self._last_db_sync >= 0.5):
            self._last_db_sync = now
            self.db.sync_segments(self.record.id, self._segments)
            self.db.update_download(self.record)

    async def _worker(self, errors: list[Exception]) -> None:
        try:
            while not self._stop_event.is_set():
                seg = await self._claim_or_steal_segment()
                if seg is None:
                    return
                await self._download_segment(seg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append(exc)

    async def run(self) -> DownloadRecord:
        try:
            self.record.status = DownloadStatus.PROBING.value
            self.record.error_message = ""
            self.db.update_download(self.record)
            self._emit()

            probe = await self._probe()
            final_url = str(probe["final_url"])
            total_size = int(probe["total_size"])  # type: ignore[arg-type]
            supports_range = bool(probe["supports_range"])
            etag = str(probe["etag"])
            last_modified = str(probe["last_modified"])
            mime_type = str(probe["mime_type"])
            resp_headers = dict(probe["resp_headers"])  # type: ignore[arg-type]

            existing_segments = self.db.get_segments(self.record.id)
            part_exists = part_file_path(self.record.file_path).exists()

            # Check if existing download can resume safely
            can_resume = (
                bool(existing_segments)
                and part_exists
                and self.record.supports_range
                and supports_range
                and (self.record.total_size == total_size)
                and (not self.record.etag or not etag or self.record.etag == etag)
            )

            self.record.final_url = final_url
            self.record.total_size = total_size
            self.record.supports_range = supports_range
            self.record.etag = etag
            self.record.last_modified = last_modified
            self.record.mime_type = mime_type

            if not existing_segments:
                # First probe: resolve final filename & collision-safe path
                resolved_name = extract_filename(
                    final_url,
                    headers=resp_headers,
                    suggested_name=self.record.filename if self.record.filename != "pending" else None,
                    mime_type=mime_type,
                )
                target = resolve_unique_path(Path(self.record.save_dir), resolved_name)
                self.record.filename = target.name
                self.record.file_path = str(target)
                self.record.category = detect_category(target.name, mime_type, self.record.download_type)

            if can_resume:
                self._segments = existing_segments
                for seg in self._segments:
                    if seg.status != SegmentStatus.COMPLETED.value:
                        seg.status = SegmentStatus.PENDING.value
            else:
                # Fresh start (or server invalidated ETag/size/Range support)
                part = part_file_path(self.record.file_path)
                if part.exists():
                    part.unlink(missing_ok=True)
                self._segments = self._create_initial_segments(total_size, supports_range)
                self.db.replace_segments(self.record.id, self._segments)
                self.record.downloaded_bytes = 0

            self._writer = SegmentedFileWriter(Path(self.record.file_path), total_size=total_size)
            self._writer.open()

            self.record.status = DownloadStatus.DOWNLOADING.value
            self._recompute_metrics()
            self.db.update_download(self.record)
            self._emit()

            max_workers = max(1, min(self.record.max_connections, self.config.max_connections_cap))
            if not supports_range or total_size <= 0:
                max_workers = 1

            errors: list[Exception] = []
            workers = [asyncio.create_task(self._worker(errors)) for _ in range(max_workers)]
            await asyncio.gather(*workers, return_exceptions=False)

            if self._stop_event.is_set():
                self._writer.flush_and_close()
                self.record.status = self._stop_reason or DownloadStatus.PAUSED.value
                self.record.speed_bps = 0.0
                self.record.eta_seconds = None
                self.record.active_connections = 0
                if self._stop_reason == DownloadStatus.CANCELLED.value:
                    part_file_path(self.record.file_path).unlink(missing_ok=True)
                self._recompute_metrics()
                self._maybe_sync_db(force=True)
                self._emit()
                return self.record

            if errors:
                raise errors[0]

            # Verify all segments completed
            incomplete = [s for s in self._segments if s.status != SegmentStatus.COMPLETED.value]
            if incomplete:
                raise RuntimeError(f"{len(incomplete)} segments did not complete")

            self.record.status = DownloadStatus.ASSEMBLING.value
            self._emit()

            actual_size = self.record.total_size if self.record.total_size > 0 else self.record.downloaded_bytes
            await self._writer.finalize(actual_size=actual_size)

            if self.record.expected_checksum:
                self.record.actual_checksum = await compute_checksum(
                    self.record.file_path, self.record.checksum_algorithm or "sha256"
                )
                if self.record.actual_checksum.lower() != self.record.expected_checksum.lower():
                    raise RuntimeError(
                        f"Checksum mismatch ({self.record.checksum_algorithm}): "
                        f"expected {self.record.expected_checksum}, got {self.record.actual_checksum}"
                    )

            self.record.status = DownloadStatus.COMPLETED.value
            self.record.speed_bps = 0.0
            self.record.eta_seconds = 0.0
            self.record.active_connections = 0
            self.record.completed_at = time.time()
            self._recompute_metrics()
            self._maybe_sync_db(force=True)
            self._emit()
            return self.record

        except Exception as exc:
            if self._writer:
                self._writer.flush_and_close()
            self.record.status = DownloadStatus.ERROR.value
            self.record.error_message = str(exc)
            self.record.speed_bps = 0.0
            self.record.eta_seconds = None
            self.record.active_connections = 0
            self._maybe_sync_db(force=True)
            self._emit()
            return self.record
