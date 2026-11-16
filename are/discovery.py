"""App/binary discovery: figure out what we are looking at."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AppInfo:
    path: str
    is_dir: bool
    is_file: bool
    size_bytes: int | None = None
    file_type: str | None = None
    kind: str = "unknown"  # unknown | native | electron | javascript | web
    name: str = ""
    package_json: dict[str, Any] | None = None
    main_file: str | None = None
    preload_file: str | None = None
    source_files: list[str] = field(default_factory=list)
    clues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "is_dir": self.is_dir,
            "is_file": self.is_file,
            "size_bytes": self.size_bytes,
            "file_type": self.file_type,
            "kind": self.kind,
            "name": self.name,
            "package_json": self.package_json,
            "main_file": self.main_file,
            "preload_file": self.preload_file,
            "source_files": self.source_files[:50],
            "clues": self.clues,
        }


def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"command timed out: {' '.join(cmd)}"


def _file_type(path: Path) -> str | None:
    rc, out, err = _run(["file", "-b", str(path)])
    if rc == 0 and out.strip():
        return out.strip()
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def discover(path: str) -> AppInfo:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"path not found: {p}")

    info = AppInfo(path=str(p), is_dir=p.is_dir(), is_file=p.is_file())
    try:
        info.size_bytes = p.stat().st_size
    except OSError:
        pass

    info.name = p.name
    info.file_type = _file_type(p) if p.is_file() else "directory"

    # --- Node / JS project -------------------------------------------------
    pkg = p / "package.json" if p.is_dir() else None
    if pkg and pkg.exists():
        pj = _read_json(pkg)
        if pj:
            info.package_json = pj
            info.kind = "javascript"
            deps = {**pj.get("dependencies", {}), **pj.get("devDependencies", {})}
            if "electron" in deps:
                info.kind = "electron"
            main = pj.get("main")
            if main:
                info.main_file = str(p / main)
            # preload hint
            for cand in ("preload.js", "preload.ts", "src/preload.js"):
                cp = p / cand
                if cp.exists():
                    info.preload_file = str(cp)
                    break
            info.clues.append(f"package.json: name={pj.get('name')} version={pj.get('version')}")
            info.clues.append(f"main={main} scripts={list(pj.get('scripts', {}).keys())}")

    # --- Native binary -----------------------------------------------------
    if p.is_file() and info.file_type:
        ft = info.file_type.lower()
        if any(tok in ft for tok in ("elf", "pe32", "mach-o", "binary", "executable")):
            if info.kind == "unknown":
                info.kind = "native"
            info.clues.append(f"file type: {info.file_type}")

    # --- Collect source files (JS/TS) --------------------------------------
    if p.is_dir() and info.kind in ("javascript", "electron"):
        exts = {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"}
        seen: set[str] = set()
        for root, dirs, files in os_walk_skip(p, skip={"node_modules", ".git", "dist", "build", ".next"}):
            for fn in files:
                if Path(fn).suffix.lower() in exts:
                    fp = Path(root) / fn
                    rel = str(fp.relative_to(p))
                    seen.add(rel)
        info.source_files = sorted(seen)[:200]
        info.clues.append(f"source files found: {len(info.source_files)}")

    return info


def os_walk_skip(top: Path, skip: set[str]):
    """os.walk that skips given directory names."""
    import os

    for root, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d not in skip]
        yield root, dirs, files
