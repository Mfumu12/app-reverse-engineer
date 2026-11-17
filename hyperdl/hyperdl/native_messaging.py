"""Chrome/Edge/Firefox Native Messaging Host bridge (modeled after ABDownloadManagerNativeMessagingHost)."""

from __future__ import annotations

import json
import struct
import sys
import urllib.request
from typing import Any


def read_native_message() -> dict[str, Any] | None:
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
    encoded = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def run_native_messaging_host(server_url: str = "http://127.0.0.1:8787") -> int:
    while True:
        msg = read_native_message()
        if msg is None:
            break
        action = msg.get("action", "add")
        try:
            if action == "ping":
                with urllib.request.urlopen(f"{server_url.rstrip('/')}/api/ping", timeout=5) as r:
                    write_native_message({"ok": True, "data": json.loads(r.read().decode("utf-8"))})
            elif action == "add":
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
