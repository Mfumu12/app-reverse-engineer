"""Chrome/Edge/Brave Native Messaging Host bridge with automatic GenniusDM.exe launcher."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from .browser_installer import _local_app_base


def _ensure_binary_stdio() -> None:
    if sys.platform == "win32":
        try:
            import msvcrt

            if sys.stdin is not None:
                msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            if sys.stdout is not None:
                msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        except Exception:
            pass


def read_native_message() -> dict[str, Any] | None:
    if sys.stdin is None:
        return None
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        return None
    msg_len = struct.unpack("<I", raw_len)[0]
    if msg_len == 0 or msg_len > 10 * 1024 * 1024:
        return None
    payload = sys.stdin.buffer.read(msg_len)
    if len(payload) < msg_len:
        return None
    return json.loads(payload.decode("utf-8"))


def write_native_message(obj: dict[str, Any]) -> None:
    if sys.stdout is None:
        return
    encoded = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _ping_server(server_url: str, timeout: float = 0.6) -> bool:
    try:
        with urllib.request.urlopen(f"{server_url.rstrip('/')}/api/ping", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _launch_gennius_dm(hidden: bool = True) -> bool:
    cmd: list[str] | None = None
    if getattr(sys, "frozen", False):
        cmd = [str(Path(sys.executable).resolve()), "desktop"]
    else:
        cfg_file = _local_app_base() / "host_config.json"
        if cfg_file.exists():
            try:
                cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                exe = cfg.get("exe_path")
                if exe and Path(exe).exists():
                    if cfg.get("frozen"):
                        cmd = [exe, "desktop"]
                    else:
                        cmd = [exe, "-m", "hyperdl", "desktop"]
            except Exception:
                pass
        if cmd is None:
            cmd = [sys.executable, "-m", "hyperdl", "desktop"]

    if hidden:
        cmd.append("--hidden")

    creationflags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
        return True
    except Exception:
        return False


def ensure_server_running(server_url: str = "http://127.0.0.1:8787", timeout: float = 12.0) -> tuple[bool, bool]:
    """Return (is_running, was_launched)."""
    if _ping_server(server_url, timeout=0.6):
        return True, False

    if not _launch_gennius_dm(hidden=True):
        return False, False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _ping_server(server_url, timeout=0.5):
            return True, True
        time.sleep(0.2)
    return False, True


def run_native_messaging_host(server_url: str = "http://127.0.0.1:8787") -> int:
    _ensure_binary_stdio()
    while True:
        msg = read_native_message()
        if msg is None:
            break
        action = msg.get("action", "add")
        try:
            if action == "ensure_running":
                ok, launched = ensure_server_running(server_url)
                write_native_message({"ok": ok, "launched": launched})
            elif action == "ping":
                with urllib.request.urlopen(f"{server_url.rstrip('/')}/api/ping", timeout=5) as r:
                    write_native_message({"ok": True, "data": json.loads(r.read().decode("utf-8"))})
            elif action == "add":
                ok, _ = ensure_server_running(server_url)
                if not ok:
                    write_native_message({"ok": False, "error": "Gennius DM não respondeu a tempo"})
                    continue
                body = json.dumps(
                    {
                        "url": msg.get("url", ""),
                        "filename": msg.get("filename"),
                        "headers": msg.get("headers") or {},
                        "user_agent": msg.get("user_agent"),
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"{server_url.rstrip('/')}/api/downloads/add",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    write_native_message({"ok": True, "data": json.loads(r.read().decode("utf-8"))})
            else:
                write_native_message({"ok": False, "error": f"Unknown action: {action}"})
        except Exception as exc:
            write_native_message({"ok": False, "error": str(exc)})
    return 0
