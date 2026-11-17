"""SQLite persistence layer (WAL mode, indexed queries, crash-safe segment tracking)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import DownloadRecord, DownloadStatus, QueueRecord, SegmentRecord


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._init_schema()
            self._seed_defaults()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS queues (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                max_concurrent INTEGER NOT NULL DEFAULT 3,
                is_running INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS downloads (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                final_url TEXT NOT NULL DEFAULT '',
                filename TEXT NOT NULL,
                save_dir TEXT NOT NULL,
                file_path TEXT NOT NULL,
                total_size INTEGER NOT NULL DEFAULT -1,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued',
                download_type TEXT NOT NULL DEFAULT 'http',
                category TEXT NOT NULL DEFAULT 'other',
                queue_id TEXT NOT NULL DEFAULT 'main',
                max_connections INTEGER NOT NULL DEFAULT 8,
                speed_limit_kbps INTEGER NOT NULL DEFAULT 0,
                supports_range INTEGER NOT NULL DEFAULT 0,
                etag TEXT NOT NULL DEFAULT '',
                last_modified TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                headers_json TEXT NOT NULL DEFAULT '{}',
                user_agent TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                expected_checksum TEXT NOT NULL DEFAULT '',
                checksum_algorithm TEXT NOT NULL DEFAULT 'sha256',
                actual_checksum TEXT NOT NULL DEFAULT '',
                media_options_json TEXT NOT NULL DEFAULT '{}',
                split_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
            CREATE INDEX IF NOT EXISTS idx_downloads_queue ON downloads(queue_id);
            CREATE INDEX IF NOT EXISTS idx_downloads_cat ON downloads(category);
            CREATE INDEX IF NOT EXISTS idx_downloads_created ON downloads(created_at DESC);

            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                download_id TEXT NOT NULL,
                segment_index INTEGER NOT NULL,
                start_byte INTEGER NOT NULL,
                end_byte INTEGER NOT NULL,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                segment_url TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (download_id) REFERENCES downloads(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_segments_dl ON segments(download_id, segment_index);

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS per_host_settings (
                host TEXT PRIMARY KEY,
                max_connections INTEGER NOT NULL DEFAULT 8,
                speed_limit_kbps INTEGER NOT NULL DEFAULT 0,
                user_agent TEXT NOT NULL DEFAULT '',
                headers_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(downloads)").fetchall()}
        if "media_options_json" not in cols:
            self._conn.execute("ALTER TABLE downloads ADD COLUMN media_options_json TEXT NOT NULL DEFAULT '{}'")
        self._conn.commit()

    def _seed_defaults(self) -> None:
        now = time.time()
        defaults = [
            ("main", "Main Queue", 3, 1, now),
            ("video", "Video & Streams", 2, 1, now),
            ("batch", "Batch Queue", 4, 0, now),
        ]
        for qid, name, mc, running, ts in defaults:
            self._conn.execute(
                "INSERT OR IGNORE INTO queues (id, name, max_concurrent, is_running, created_at) VALUES (?, ?, ?, ?, ?)",
                (qid, name, mc, running, ts),
            )
        # Recover any downloads left in 'downloading'/'probing'/'assembling' after an abrupt crash
        self._conn.execute(
            """
            UPDATE downloads
            SET status = 'paused'
            WHERE status IN ('downloading', 'probing', 'assembling')
            """
        )
        self._conn.execute(
            """
            UPDATE segments
            SET status = 'paused'
            WHERE status = 'downloading'
            """
        )
        self._conn.commit()

    # --- Settings ---

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            self._conn.commit()

    def get_all_settings(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
            return {r["key"]: r["value"] for r in rows}

    # --- Per-host settings ---

    def get_host_settings(self, host: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM per_host_settings WHERE host = ?", (host.lower(),)
            ).fetchone()
            if not row:
                return None
            return {
                "host": row["host"],
                "max_connections": row["max_connections"],
                "speed_limit_kbps": row["speed_limit_kbps"],
                "user_agent": row["user_agent"],
                "headers": json.loads(row["headers_json"] or "{}"),
            }

    def list_host_settings(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM per_host_settings ORDER BY host").fetchall()
            return [
                {
                    "host": r["host"],
                    "max_connections": r["max_connections"],
                    "speed_limit_kbps": r["speed_limit_kbps"],
                    "user_agent": r["user_agent"],
                    "headers": json.loads(r["headers_json"] or "{}"),
                }
                for r in rows
            ]

    def upsert_host_settings(
        self,
        host: str,
        max_connections: int = 8,
        speed_limit_kbps: int = 0,
        user_agent: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO per_host_settings (host, max_connections, speed_limit_kbps, user_agent, headers_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(host) DO UPDATE SET
                    max_connections = excluded.max_connections,
                    speed_limit_kbps = excluded.speed_limit_kbps,
                    user_agent = excluded.user_agent,
                    headers_json = excluded.headers_json
                """,
                (host.lower(), max_connections, speed_limit_kbps, user_agent, json.dumps(headers or {})),
            )
            self._conn.commit()

    def delete_host_settings(self, host: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM per_host_settings WHERE host = ?", (host.lower(),))
            self._conn.commit()

    # --- Queues ---

    def list_queues(self) -> list[QueueRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM queues ORDER BY created_at ASC").fetchall()
            return [
                QueueRecord(
                    id=r["id"],
                    name=r["name"],
                    max_concurrent=r["max_concurrent"],
                    is_running=bool(r["is_running"]),
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    def get_queue(self, queue_id: str) -> QueueRecord | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM queues WHERE id = ?", (queue_id,)).fetchone()
            if not r:
                return None
            return QueueRecord(
                id=r["id"],
                name=r["name"],
                max_concurrent=r["max_concurrent"],
                is_running=bool(r["is_running"]),
                created_at=r["created_at"],
            )

    def create_queue(self, queue_id: str, name: str, max_concurrent: int = 3, is_running: bool = True) -> QueueRecord:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO queues (id, name, max_concurrent, is_running, created_at) VALUES (?, ?, ?, ?, ?)",
                (queue_id, name, max_concurrent, 1 if is_running else 0, now),
            )
            self._conn.commit()
        return QueueRecord(id=queue_id, name=name, max_concurrent=max_concurrent, is_running=is_running, created_at=now)

    def update_queue(self, queue_id: str, *, max_concurrent: int | None = None, is_running: bool | None = None) -> None:
        with self._lock:
            if max_concurrent is not None:
                self._conn.execute("UPDATE queues SET max_concurrent = ? WHERE id = ?", (max_concurrent, queue_id))
            if is_running is not None:
                self._conn.execute("UPDATE queues SET is_running = ? WHERE id = ?", (1 if is_running else 0, queue_id))
            self._conn.commit()

    # --- Downloads ---

    def _row_to_download(self, r: sqlite3.Row, include_segments: bool = False) -> DownloadRecord:
        rec = DownloadRecord(
            id=r["id"],
            url=r["url"],
            final_url=r["final_url"],
            filename=r["filename"],
            save_dir=r["save_dir"],
            file_path=r["file_path"],
            total_size=r["total_size"],
            downloaded_bytes=r["downloaded_bytes"],
            status=r["status"],
            download_type=r["download_type"],
            category=r["category"],
            queue_id=r["queue_id"],
            max_connections=r["max_connections"],
            speed_limit_kbps=r["speed_limit_kbps"],
            supports_range=bool(r["supports_range"]),
            etag=r["etag"],
            last_modified=r["last_modified"],
            mime_type=r["mime_type"],
            headers=json.loads(r["headers_json"] or "{}"),
            user_agent=r["user_agent"],
            error_message=r["error_message"],
            expected_checksum=r["expected_checksum"],
            checksum_algorithm=r["checksum_algorithm"],
            actual_checksum=r["actual_checksum"],
            media_options=json.loads(r["media_options_json"] or "{}") if "media_options_json" in r.keys() else {},
            split_count=r["split_count"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            completed_at=r["completed_at"],
        )
        if include_segments:
            rec.segments = self.get_segments(rec.id)
        return rec

    def insert_download(self, dl: DownloadRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO downloads (
                    id, url, final_url, filename, save_dir, file_path, total_size, downloaded_bytes,
                    status, download_type, category, queue_id, max_connections, speed_limit_kbps,
                    supports_range, etag, last_modified, mime_type, headers_json, user_agent,
                    error_message, expected_checksum, checksum_algorithm, actual_checksum, media_options_json, split_count,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dl.id,
                    dl.url,
                    dl.final_url,
                    dl.filename,
                    dl.save_dir,
                    dl.file_path,
                    dl.total_size,
                    dl.downloaded_bytes,
                    dl.status,
                    dl.download_type,
                    dl.category,
                    dl.queue_id,
                    dl.max_connections,
                    dl.speed_limit_kbps,
                    1 if dl.supports_range else 0,
                    dl.etag,
                    dl.last_modified,
                    dl.mime_type,
                    json.dumps(dl.headers),
                    dl.user_agent,
                    dl.error_message,
                    dl.expected_checksum,
                    dl.checksum_algorithm,
                    dl.actual_checksum,
                    json.dumps(dl.media_options),
                    dl.split_count,
                    dl.created_at,
                    dl.updated_at,
                    dl.completed_at,
                ),
            )
            self._conn.commit()

    def update_download(self, dl: DownloadRecord) -> None:
        dl.updated_at = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE downloads SET
                    url = ?, final_url = ?, filename = ?, save_dir = ?, file_path = ?,
                    total_size = ?, downloaded_bytes = ?, status = ?, download_type = ?,
                    category = ?, queue_id = ?, max_connections = ?, speed_limit_kbps = ?,
                    supports_range = ?, etag = ?, last_modified = ?, mime_type = ?,
                    headers_json = ?, user_agent = ?, error_message = ?,
                    expected_checksum = ?, checksum_algorithm = ?, actual_checksum = ?, media_options_json = ?, split_count = ?,
                    updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    dl.url,
                    dl.final_url,
                    dl.filename,
                    dl.save_dir,
                    dl.file_path,
                    dl.total_size,
                    dl.downloaded_bytes,
                    dl.status,
                    dl.download_type,
                    dl.category,
                    dl.queue_id,
                    dl.max_connections,
                    dl.speed_limit_kbps,
                    1 if dl.supports_range else 0,
                    dl.etag,
                    dl.last_modified,
                    dl.mime_type,
                    json.dumps(dl.headers),
                    dl.user_agent,
                    dl.error_message,
                    dl.expected_checksum,
                    dl.checksum_algorithm,
                    dl.actual_checksum,
                    json.dumps(dl.media_options),
                    dl.split_count,
                    dl.updated_at,
                    dl.completed_at,
                    dl.id,
                ),
            )
            self._conn.commit()

    def get_download(self, download_id: str, include_segments: bool = False) -> DownloadRecord | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
            if not r:
                return None
            return self._row_to_download(r, include_segments=include_segments)

    def list_downloads(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        queue_id: str | None = None,
        include_segments: bool = False,
    ) -> list[DownloadRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if queue_id:
            clauses.append("queue_id = ?")
            params.append(queue_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM downloads {where} ORDER BY created_at DESC", params
            ).fetchall()
            return [self._row_to_download(r, include_segments=include_segments) for r in rows]

    def delete_download(self, download_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
            self._conn.commit()

    # --- Segments ---

    def replace_segments(self, download_id: str, segments: list[SegmentRecord]) -> list[SegmentRecord]:
        with self._lock:
            self._conn.execute("DELETE FROM segments WHERE download_id = ?", (download_id,))
            for seg in segments:
                cur = self._conn.execute(
                    """
                    INSERT INTO segments (
                        download_id, segment_index, start_byte, end_byte,
                        downloaded_bytes, status, segment_url, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        download_id,
                        seg.segment_index,
                        seg.start_byte,
                        seg.end_byte,
                        seg.downloaded_bytes,
                        seg.status,
                        seg.segment_url,
                        seg.error_message,
                    ),
                )
                seg.id = cur.lastrowid or 0
            self._conn.commit()
        return segments

    def insert_segment(self, seg: SegmentRecord) -> SegmentRecord:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO segments (
                    download_id, segment_index, start_byte, end_byte,
                    downloaded_bytes, status, segment_url, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    seg.download_id,
                    seg.segment_index,
                    seg.start_byte,
                    seg.end_byte,
                    seg.downloaded_bytes,
                    seg.status,
                    seg.segment_url,
                    seg.error_message,
                ),
            )
            seg.id = cur.lastrowid or 0
            self._conn.commit()
        return seg

    def sync_segments(self, download_id: str, segments: list[SegmentRecord]) -> None:
        with self._lock:
            for seg in segments:
                if seg.id:
                    self._conn.execute(
                        """
                        UPDATE segments
                        SET segment_index = ?, start_byte = ?, end_byte = ?,
                            downloaded_bytes = ?, status = ?, error_message = ?
                        WHERE id = ?
                        """,
                        (
                            seg.segment_index,
                            seg.start_byte,
                            seg.end_byte,
                            seg.downloaded_bytes,
                            seg.status,
                            seg.error_message,
                            seg.id,
                        ),
                    )
            self._conn.commit()

    def get_segments(self, download_id: str) -> list[SegmentRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM segments WHERE download_id = ? ORDER BY segment_index ASC",
                (download_id,),
            ).fetchall()
            return [
                SegmentRecord(
                    id=r["id"],
                    download_id=r["download_id"],
                    segment_index=r["segment_index"],
                    start_byte=r["start_byte"],
                    end_byte=r["end_byte"],
                    downloaded_bytes=r["downloaded_bytes"],
                    status=r["status"],
                    segment_url=r["segment_url"],
                    error_message=r["error_message"],
                )
                for r in rows
            ]
