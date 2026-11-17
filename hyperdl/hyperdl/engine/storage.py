"""Random-access .part file writer with sparse pre-allocation and atomic finalize."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

from ..utils.filename import part_file_path


class SegmentedFileWriter:
    """Thread-safe positioned writer to `<target>.hyperdl.part`."""

    def __init__(self, target_path: Path, total_size: int = -1) -> None:
        self.target_path = Path(target_path)
        self.part_path = part_file_path(self.target_path)
        self.total_size = total_size
        self._io_lock = threading.Lock()
        self._fd: int | None = None
        self._has_pwrite = hasattr(os, "pwrite")

    def open(self) -> None:
        self.target_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY  # Windows binary mode
        self._fd = os.open(str(self.part_path), flags, 0o644)
        if self.total_size > 0:
            cur_size = os.fstat(self._fd).st_size
            if cur_size < self.total_size:
                try:
                    os.ftruncate(self._fd, self.total_size)
                except OSError:
                    pass

    def write_at_sync(self, offset: int, data: bytes) -> None:
        if self._fd is None:
            raise RuntimeError("SegmentedFileWriter is not open")
        if not data:
            return
        if self._has_pwrite:
            view = memoryview(data)
            written = 0
            while written < len(data):
                n = os.pwrite(self._fd, view[written:], offset + written)
                if n <= 0:
                    raise OSError("os.pwrite returned 0 bytes")
                written += n
        else:
            with self._io_lock:
                os.lseek(self._fd, offset, os.SEEK_SET)
                view = memoryview(data)
                written = 0
                while written < len(data):
                    n = os.write(self._fd, view[written:])
                    if n <= 0:
                        raise OSError("os.write returned 0 bytes")
                    written += n

    async def write_at(self, offset: int, data: bytes) -> None:
        await asyncio.to_thread(self.write_at_sync, offset, data)

    def flush_and_close(self) -> None:
        if self._fd is not None:
            try:
                os.fsync(self._fd)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None

    def finalize_sync(self, actual_size: int | None = None) -> Path:
        self.flush_and_close()
        if actual_size is not None and actual_size >= 0 and self.part_path.exists():
            try:
                os.truncate(str(self.part_path), actual_size)
            except OSError:
                pass
        if self.part_path.exists():
            os.replace(str(self.part_path), str(self.target_path))
        return self.target_path

    async def finalize(self, actual_size: int | None = None) -> Path:
        return await asyncio.to_thread(self.finalize_sync, actual_size)
