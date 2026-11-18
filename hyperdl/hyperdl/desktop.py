"""Native desktop window launcher (pywebview + aiohttp background server)."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from aiohttp import web

from . import __version__
from .browser_installer import (
    register_native_messaging_host,
    register_windows_autostart,
    sync_extension_files,
)
from .config import HyperDLConfig
from .engine.manager import DownloadManager
from .server import EXT_DIR, STATIC_DIR, HyperDLServer


_active_window: Any = None


def focus_desktop_window() -> bool:
    win = _active_window
    if win is None:
        return False
    try:
        win.restore()
        win.show()
        try:
            win.on_top = True
            win.on_top = False
        except Exception:
            pass
        return True
    except Exception:
        return False


def _ensure_stdio_streams(data_dir: Path) -> None:
    """When frozen with console=False on Windows, stdout/stderr are None."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "genniusdm.log"
    fh = open(log_path, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = fh
    if sys.stderr is None:
        sys.stderr = fh


def _server_alive(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/ping", timeout=1.0) as r:
            return r.status == 200
    except Exception:
        return False


def _start_background_server(config: HyperDLConfig) -> tuple[threading.Thread, dict[str, Any]]:
    state: dict[str, Any] = {"loop": None, "runner": None, "error": None, "ready": threading.Event()}

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        state["loop"] = loop

        async def _boot() -> None:
            mgr = DownloadManager(config)
            srv = HyperDLServer(mgr)
            runner = web.AppRunner(srv.create_app())
            await runner.setup()
            site = web.TCPSite(runner, config.host, config.port)
            await site.start()
            state["runner"] = runner
            state["ready"].set()

        try:
            loop.run_until_complete(_boot())
            loop.run_forever()
        except Exception as exc:
            state["error"] = exc
            state["ready"].set()
        finally:
            runner = state.get("runner")
            if runner is not None:
                try:
                    loop.run_until_complete(runner.cleanup())
                except Exception:
                    pass
            loop.close()

    t = threading.Thread(target=_run, name="GenniusDMServer", daemon=True)
    t.start()
    return t, state


def _focus_existing_instance(url: str) -> bool:
    try:
        req = urllib.request.Request(f"{url}/api/desktop/focus", data=b"{}", method="POST")
        with urllib.request.urlopen(req, timeout=1.5) as r:
            import json

            data = json.loads(r.read().decode("utf-8"))
            return bool(data.get("focused"))
    except Exception:
        return False


def run_desktop_app(config: HyperDLConfig | None = None, *, hidden: bool = False) -> int:
    cfg = config or HyperDLConfig(host="127.0.0.1", port=8787)
    _ensure_stdio_streams(cfg.data_dir)

    # Always extract the MV3 extension, register Native Messaging Host, and enable Windows background startup
    try:
        ext_dir = sync_extension_files(EXT_DIR)
        register_native_messaging_host(ext_dir)
        register_windows_autostart(True)
    except Exception as exc:
        print(f"[Gennius DM] Aviso ao preparar extensao: {exc}")

    url = f"http://127.0.0.1:{cfg.port}"
    already_running = _server_alive(url)
    if already_running:
        if hidden:
            return 0
        if _focus_existing_instance(url):
            return 0

    srv_thread: threading.Thread | None = None
    srv_state: dict[str, Any] | None = None

    if not already_running:
        srv_thread, srv_state = _start_background_server(cfg)
        srv_state["ready"].wait(timeout=10.0)
        if srv_state.get("error"):
            print(f"[Gennius DM] Erro ao iniciar servidor: {srv_state['error']}")
            return 1

    # Open native desktop window via pywebview (Edge WebView2 on Windows)
    tray_icon: Any = None
    try:
        import webview  # type: ignore[import-not-found]
        global _active_window

        icon_path = STATIC_DIR / "genniusdm.ico"
        win = webview.create_window(
            title=f"Gennius DM {__version__}",
            url=url,
            width=1240,
            height=780,
            min_size=(920, 620),
            background_color="#0b0d11",
            hidden=hidden,
        )
        _active_window = win
        force_quit = {"value": False}

        def _post_action(path: str) -> None:
            try:
                req = urllib.request.Request(f"{url}{path}", data=b"{}", method="POST")
                urllib.request.urlopen(req, timeout=2.0).close()
            except Exception:
                pass

        try:
            import pystray  # type: ignore[import-not-found]
            from PIL import Image  # type: ignore[import-not-found]

            if icon_path.exists():
                tray_img = Image.open(icon_path)
            else:
                tray_img = Image.new("RGBA", (64, 64), (37, 99, 235, 255))

            def _on_tray_open(icon: Any, item: Any) -> None:
                focus_desktop_window()

            def _on_tray_pause_all(icon: Any, item: Any) -> None:
                threading.Thread(target=_post_action, args=("/api/downloads/pause-all",), daemon=True).start()

            def _on_tray_quit(icon: Any, item: Any) -> None:
                force_quit["value"] = True
                try:
                    icon.stop()
                except Exception:
                    pass
                try:
                    win.destroy()
                except Exception:
                    pass

            tray_menu = pystray.Menu(
                pystray.MenuItem("Abrir Gennius DM", _on_tray_open, default=True),
                pystray.MenuItem("Pausar todos os downloads", _on_tray_pause_all),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Sair do Gennius DM", _on_tray_quit),
            )
            tray_icon = pystray.Icon("GenniusDM", tray_img, f"Gennius DM {__version__}", tray_menu)
            tray_icon.run_detached()

            def _on_closing() -> bool:
                if force_quit["value"]:
                    return True
                try:
                    win.hide()
                    return False
                except Exception:
                    return True

            win.events.closing += _on_closing
        except Exception as tray_exc:
            print(f"[Gennius DM] Tray indisponivel ({tray_exc})")

        start_kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            start_kwargs["gui"] = "edgechromium"
        if icon_path.exists():
            start_kwargs["icon"] = str(icon_path)
        try:
            webview.start(**start_kwargs)
        except Exception:
            # Fallback if edgechromium or icon kwarg isn't supported on platform
            webview.start()
    except Exception as exc:
        print(f"[Gennius DM] Modo navegador de reserva ({exc})")
        webbrowser.open(url)
        if not already_running and srv_thread is not None:
            try:
                while srv_thread.is_alive():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
    finally:
        if tray_icon is not None:
            try:
                tray_icon.stop()
            except Exception:
                pass
        if srv_state and srv_state.get("loop") is not None:
            loop = srv_state["loop"]
            if loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
        if srv_thread is not None:
            srv_thread.join(timeout=3.0)
    return 0
