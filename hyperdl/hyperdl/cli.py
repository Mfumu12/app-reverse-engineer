"""Command-line interface for HyperDL (modeled after ABDownloadManagerCli)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

from . import __version__
from .config import HyperDLConfig
from .engine.manager import DownloadManager
from .native_messaging import run_native_messaging_host
from .server import run_server
from .utils.checksum import compute_checksum_sync
from .utils.importers import parse_curl_command


def _fmt_size(n: int) -> str:
    if n < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024  # type: ignore[assignment]
    return f"{n} B"


async def _run_direct_add(args: argparse.Namespace) -> int:
    cfg = HyperDLConfig()
    if args.output_dir:
        cfg.download_dir = Path(args.output_dir)
    mgr = DownloadManager(cfg)
    await mgr.start()
    try:
        rec = await mgr.add_download(
            args.url,
            filename=args.filename,
            max_connections=args.connections,
            speed_limit_kbps=args.limit,
            expected_checksum=args.checksum or "",
        )
        print(f"[HyperDL] Started {rec.id}: {rec.url} -> {rec.file_path} (connections={rec.max_connections})")
        while True:
            cur = mgr.get_download(rec.id, include_segments=True)
            if not cur:
                break
            pct = (cur.downloaded_bytes / cur.total_size * 100) if cur.total_size > 0 else 0.0
            spd = _fmt_size(int(cur.speed_bps)) + "/s"
            sys.stdout.write(
                f"\r[{cur.status.upper()}] {pct:5.1f}%  {_fmt_size(cur.downloaded_bytes)}/{_fmt_size(cur.total_size)}  "
                f"{spd}  conns={cur.active_connections} splits={cur.split_count}   "
            )
            sys.stdout.flush()
            if cur.status in ("completed", "error", "cancelled", "paused"):
                print()
                if cur.status == "error":
                    print(f"Error: {cur.error_message}", file=sys.stderr)
                    return 1
                print(f"Saved: {cur.file_path}")
                return 0
            await asyncio.sleep(0.25)
        return 0
    finally:
        await mgr.stop()


def _api_request(server_url: str, path: str, method: str = "GET", body: dict | None = None, api_key: str = "") -> dict:
    url = server_url.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hyperdl", description="HyperDL — Hybrid Multi-Segment Download Accelerator")
    parser.add_argument("--version", action="version", version=f"HyperDL {__version__}")
    sub = parser.add_subparsers(dest="command", required=False)

    p_desktop = sub.add_parser("desktop", help="Launch Gennius DM native desktop window")
    p_desktop.add_argument("--port", type=int, default=8787)
    p_desktop.add_argument("--download-dir", default="")
    p_desktop.add_argument("--hidden", action="store_true", help="Start hidden in background ready for browser capture")

    p_serve = sub.add_parser("serve", help="Start HyperDL Web UI, REST API & WebSocket server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.add_argument("--download-dir", default="")
    p_serve.add_argument("--connections", type=int, default=8)
    p_serve.add_argument("--api-key", default="")
    p_serve.add_argument("--open", action="store_true", help="Open Web UI in default browser")

    p_add = sub.add_parser("add", help="Download a URL directly with multi-segment acceleration")
    p_add.add_argument("url")
    p_add.add_argument("-o", "--output-dir", default="")
    p_add.add_argument("-n", "--filename", default=None)
    p_add.add_argument("-c", "--connections", type=int, default=8)
    p_add.add_argument("-l", "--limit", type=int, default=0, help="Speed limit in KB/s (0=unlimited)")
    p_add.add_argument("--checksum", default="", help="Expected SHA-256 checksum")

    p_curl = sub.add_parser("import-curl", help="Send a cURL command to a running HyperDL server")
    p_curl.add_argument("curl_command")
    p_curl.add_argument("--server", default="http://127.0.0.1:8787")
    p_curl.add_argument("--api-key", default="")

    p_list = sub.add_parser("list", help="List downloads from a running HyperDL server")
    p_list.add_argument("--server", default="http://127.0.0.1:8787")
    p_list.add_argument("--api-key", default="")

    p_pause = sub.add_parser("pause", help="Pause a download via server API")
    p_pause.add_argument("id")
    p_pause.add_argument("--server", default="http://127.0.0.1:8787")

    p_resume = sub.add_parser("resume", help="Resume a download via server API")
    p_resume.add_argument("id")
    p_resume.add_argument("--server", default="http://127.0.0.1:8787")

    p_verify = sub.add_parser("verify", help="Verify local file checksum")
    p_verify.add_argument("file")
    p_verify.add_argument("--algo", default="sha256", choices=["md5", "sha1", "sha256", "sha512"])
    p_verify.add_argument("--expected", default="")

    p_nmh = sub.add_parser("native-messaging", help="Run Chrome/Edge/Firefox Native Messaging stdio bridge")
    p_nmh.add_argument("--server", default="http://127.0.0.1:8787")

    args = parser.parse_args(argv)

    if args.command is None:
        from .desktop import run_desktop_app

        cfg = HyperDLConfig(host="127.0.0.1", port=8787)
        return run_desktop_app(cfg)

    if args.command == "desktop":
        from .desktop import run_desktop_app

        cfg = HyperDLConfig(host="127.0.0.1", port=args.port)
        if args.download_dir:
            cfg.download_dir = Path(args.download_dir)
        return run_desktop_app(cfg, hidden=bool(args.hidden))

    if args.command == "serve":
        cfg = HyperDLConfig(host=args.host, port=args.port, default_connections=args.connections, api_key=args.api_key)
        if args.download_dir:
            cfg.download_dir = Path(args.download_dir)
        run_server(cfg, open_browser=args.open)
        return 0
    if args.command == "add":
        return asyncio.run(_run_direct_add(args))
    if args.command == "import-curl":
        parsed = parse_curl_command(args.curl_command)
        res = _api_request(
            args.server,
            "/api/downloads/add",
            method="POST",
            body={"url": parsed.url, "headers": parsed.headers, "user_agent": parsed.user_agent or None},
            api_key=args.api_key,
        )
        print(json.dumps(res, indent=2))
        return 0
    if args.command == "list":
        res = _api_request(args.server, "/api/downloads", api_key=args.api_key)
        for d in res.get("downloads", []):
            print(f"{d['id']}  {d['status']:11s}  {d['progress']:6.2f}%  {d['filename']}")
        return 0
    if args.command == "pause":
        print(json.dumps(_api_request(args.server, f"/api/downloads/{args.id}/pause", method="POST"), indent=2))
        return 0
    if args.command == "resume":
        print(json.dumps(_api_request(args.server, f"/api/downloads/{args.id}/resume", method="POST"), indent=2))
        return 0
    if args.command == "verify":
        digest = compute_checksum_sync(args.file, args.algo)
        print(f"{args.algo.upper()}: {digest}")
        if args.expected:
            ok = digest.lower() == args.expected.strip().lower()
            print("MATCH" if ok else "MISMATCH")
            return 0 if ok else 2
        return 0
    if args.command == "native-messaging":
        return run_native_messaging_host(args.server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
