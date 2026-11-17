"""Automatic browser extension extractor, Native Messaging registrar, and browser launcher."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any


def _local_app_base() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "GenniusDM"
        return Path.home() / "AppData" / "Local" / "GenniusDM"
    return Path.home() / ".genniusdm"


def get_installed_extension_dir() -> Path:
    return _local_app_base() / "extension"


def sync_extension_files(source_dir: Path) -> Path:
    """Copy the bundled MV3 extension into a stable local folder for browsers."""
    target = get_installed_extension_dir()
    target.mkdir(parents=True, exist_ok=True)
    if source_dir.exists():
        for item in source_dir.iterdir():
            if item.is_file():
                shutil.copy2(item, target / item.name)
    return target


def register_native_messaging_host(ext_dir: Path) -> dict[str, Any]:
    """Write Native Messaging Host manifest and register it in HKCU on Windows."""
    host_name = "com.genniusdm.host"
    manifest_path = _local_app_base() / f"{host_name}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    if getattr(sys, "frozen", False):
        exe_path = str(Path(sys.executable).resolve())
        bat_path = _local_app_base() / "genniusdm_nmh.bat"
        bat_path.write_text(f'@echo off\r\n"{exe_path}" native-messaging %*\r\n', encoding="utf-8")
        host_cmd = str(bat_path)
    else:
        host_cmd = str(Path(sys.executable).resolve())

    manifest = {
        "name": host_name,
        "description": "Gennius DM Native Messaging Host",
        "path": host_cmd,
        "type": "stdio",
        "allowed_origins": [
            "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    registered: list[str] = []
    if sys.platform == "win32":
        try:
            import winreg  # type: ignore[import-not-found]

            reg_paths = [
                rf"Software\Google\Chrome\NativeMessagingHosts\{host_name}",
                rf"Software\Microsoft\Edge\NativeMessagingHosts\{host_name}",
                rf"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\{host_name}",
            ]
            for rp in reg_paths:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rp) as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest_path))
                registered.append(rp)
        except Exception:
            pass
    return {"manifest": str(manifest_path), "registry_keys": registered, "extension_dir": str(ext_dir)}


def detect_browsers() -> list[dict[str, str]]:
    """Find installed Chromium-based browsers on the host."""
    found: list[dict[str, str]] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        candidates = [
            ("edge", "Microsoft Edge", [
                os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
            ]),
            ("chrome", "Google Chrome", [
                os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
            ]),
            ("brave", "Brave Browser", [
                os.path.join(pf, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                os.path.join(pf86, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                os.path.join(local, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            ]),
        ]
        for bid, name, paths in candidates:
            for p in paths:
                if p and os.path.isfile(p):
                    found.append({"id": bid, "name": name, "path": p})
                    break
    else:
        for bid, name, bins in [
            ("chrome", "Google Chrome", ["google-chrome", "chromium", "chromium-browser"]),
            ("edge", "Microsoft Edge", ["microsoft-edge"]),
            ("brave", "Brave Browser", ["brave-browser"]),
        ]:
            for b in bins:
                exe = shutil.which(b)
                if exe:
                    found.append({"id": bid, "name": name, "path": exe})
                    break
    return found


def _copy_to_clipboard(text: str) -> bool:
    if sys.platform == "win32":
        try:
            subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)
            return True
        except Exception:
            try:
                subprocess.run(["cmd", "/c", f"echo {text}|clip"], check=False)
                return True
            except Exception:
                return False
    return False


def create_desktop_browser_shortcut(browser_name: str, browser_exe: str, ext_dir: Path) -> str | None:
    """Create a Windows Desktop shortcut that opens the browser with Gennius DM preloaded."""
    if sys.platform != "win32":
        return None
    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        return None
    profile_dir = _local_app_base() / f"profile-{browser_name.lower().replace(' ', '_')}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    lnk_path = desktop / f"{browser_name} (Gennius DM).lnk"
    args = f'--user-data-dir="{profile_dir}" --load-extension="{ext_dir}" --no-first-run'
    ico = ext_dir.parent / "extension" / "icon128.png"
    ps = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$s = $ws.CreateShortcut("{lnk_path}"); '
        f'$s.TargetPath = "{browser_exe}"; '
        f'$s.Arguments = \'{args}\'; '
        f'$s.Save()'
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return str(lnk_path)
    except Exception:
        return None


def install_extension_for_browser(
    source_ext_dir: Path,
    browser_id: str = "auto",
    mode: str = "launch",
) -> dict[str, Any]:
    """Extract extension, register host, and install/launch it in Chrome/Edge/Brave."""
    ext_dir = sync_extension_files(source_ext_dir)
    nmh = register_native_messaging_host(ext_dir)
    browsers = detect_browsers()

    chosen = None
    if browser_id != "auto":
        chosen = next((b for b in browsers if b["id"] == browser_id), None)
    if not chosen and browsers:
        chosen = browsers[0]

    copied = _copy_to_clipboard(str(ext_dir))

    if mode == "launch" and chosen:
        profile_dir = _local_app_base() / f"profile-{chosen['id']}"
        profile_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            chosen["path"],
            f"--user-data-dir={profile_dir}",
            f"--load-extension={ext_dir}",
            "--no-first-run",
            "http://127.0.0.1:8787",
        ]
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(cmd, creationflags=creationflags)
        shortcut = create_desktop_browser_shortcut(chosen["name"], chosen["path"], ext_dir)
        return {
            "status": "launched",
            "browser": chosen["name"],
            "extension_dir": str(ext_dir),
            "shortcut": shortcut,
            "clipboard_copied": copied,
            "native_messaging": nmh,
            "message": f"{chosen['name']} iniciado com a extensão Gennius DM ativa automaticamente.",
        }

    # Developer / main-profile assistant mode
    scheme = "edge" if (chosen and chosen["id"] == "edge") else "chrome"
    ext_url = f"{scheme}://extensions"
    if chosen:
        try:
            subprocess.Popen([chosen["path"], ext_url])
        except Exception:
            webbrowser.open(ext_url)
    else:
        webbrowser.open(ext_url)

    if sys.platform == "win32":
        try:
            subprocess.Popen(["explorer.exe", str(ext_dir)])
        except Exception:
            pass

    return {
        "status": "ready",
        "browser": chosen["name"] if chosen else "Navegador",
        "extension_dir": str(ext_dir),
        "clipboard_copied": copied,
        "native_messaging": nmh,
        "message": (
            f"Pasta da extensão extraída em {ext_dir} (copiada para a área de transferência). "
            f"Em {ext_url}, ativa o Modo de programador e cola em Carregar expandida."
        ),
    }
