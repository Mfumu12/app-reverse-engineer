"""Guided, safe setup. Checks what is available and asks before changing anything."""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from typing import Any

from .config import (
    CONFIG_FILE, DEFAULT_CONFIG, save_config, load_config, log_setup,
    ensure_dirs,
)

SYSTEM_TOOLS = {
    "python3": "python3",
    "node": "node (Node.js)",
    "npm": "npm",
    "git": "git",
    "strings": "binutils (strings)",
    "objdump": "binutils (objdump)",
    "readelf": "binutils (readelf)",
    "nm": "binutils (nm)",
    "file": "file",
    "ldd": "ldd",
}

PY_PACKAGES = {
    "click": "click",
    "yaml": "PyYAML",
    "typer": "typer",
    "requests": "requests",
    "mcp": "mcp  (needed for the full MCP server)",
}


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _have_python(mod: str) -> bool:
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


def doctor_report(cfg: dict[str, Any]) -> dict[str, Any]:
    safety = cfg.get("safety", {})
    report: dict[str, Any] = {
        "config_file": str(CONFIG_FILE),
        "config_exists": CONFIG_FILE.exists(),
        "system_tools": {},
        "python_packages": {},
        "safety": safety,
        "missing_system": [],
        "missing_python": [],
    }
    for cmd, label in SYSTEM_TOOLS.items():
        ok = _have(cmd)
        report["system_tools"][label] = ok
        if not ok:
            report["missing_system"].append(label)
    for mod, label in PY_PACKAGES.items():
        ok = _have_python(mod)
        report["python_packages"][label] = ok
        if not ok:
            report["missing_python"].append(label)
    report["llm_configured"] = bool(cfg.get("llm", {}).get("provider") in ("ollama", "openai"))
    report["ready"] = not report["missing_system"] and not report["missing_python"]
    return report


def _prompt(prompt_text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    ans = input(f"{prompt_text}{suffix}: ").strip()
    if not ans and default is not None:
        return default
    return ans


def _confirm(prompt_text: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    ans = input(f"{prompt_text} ({suffix}): ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def run_setup(cfg: dict[str, Any] | None = None, non_interactive: bool = False) -> dict[str, Any]:
    cfg = cfg or load_config()
    ensure_dirs()
    report = doctor_report(cfg)
    print("=== app-reverse-engineer setup ===")
    print(f"Config dir: {CONFIG_FILE.parent}")
    print()

    # Show status
    print("System tools:")
    for label, ok in report["system_tools"].items():
        print(f"  [{'OK ' if ok else 'MISS'}] {label}")
    print()
    print("Python packages:")
    for label, ok in report["python_packages"].items():
        print(f"  [{'OK ' if ok else 'MISS'}] {label}")
    print()

    missing_sys = report["missing_system"]
    missing_py = report["missing_python"]

    if missing_sys:
        print("Missing system tools. Install them yourself (this tool will not run sudo/apt):")
        for label in missing_sys:
            print(f"  - {label}")
        print()

    if missing_py:
        print("Missing Python packages:")
        for label in missing_py:
            print(f"  - {label}")
        if not non_interactive and _confirm("Install missing Python packages with pip now?", default=True):
            pkgs = []
            for label in missing_py:
                if label.startswith("mcp"):
                    pkgs.append("mcp")
                else:
                    pkgs.append(label)
            cmd = [sys.executable, "-m", "pip", "install", "--user"] + pkgs
            print("Running:", " ".join(cmd))
            try:
                subprocess.run(cmd, check=True)
                log_setup("pip installed: " + " ".join(pkgs))
                print("Installed. Re-run `are setup` to verify.")
            except subprocess.CalledProcessError as exc:
                print(f"pip install failed: {exc}")
        else:
            print("Skipped Python package install. The MCP server needs `mcp` to run fully.")

    # LLM config
    print()
    print("Optional LLM (for polishing reports). Current provider:",
          cfg.get("llm", {}).get("provider", "none"))
    if not non_interactive and _confirm("Configure an LLM provider now?", default=False):
        provider = _prompt("Provider (none | ollama | openai)", cfg.get("llm", {}).get("provider", "none"))
        cfg.setdefault("llm", {})["provider"] = provider
        if provider == "ollama":
            cfg["llm"]["model"] = _prompt("Ollama model", cfg.get("llm", {}).get("model", "llama3.2"))
            cfg["llm"]["base_url"] = _prompt("Ollama base URL", cfg.get("llm", {}).get("base_url", "http://localhost:11434"))
        elif provider == "openai":
            cfg["llm"]["model"] = _prompt("OpenAI model", cfg.get("llm", {}).get("model", "gpt-4o-mini"))
            cfg["llm"]["api_key"] = _prompt("OpenAI API key", cfg.get("llm", {}).get("api_key", ""))
        save_config(cfg)
        log_setup(f"llm configured: provider={provider}")

    # Safety settings
    print()
    print("Safety settings (analysis stays offline/local):")
    safety = cfg.setdefault("safety", {})
    if not non_interactive:
        safety["ask_before_install"] = _confirm("Ask before installing anything?", default=safety.get("ask_before_install", True))
        safety["allow_network_analysis"] = _confirm("Allow network-based analysis?", default=safety.get("allow_network_analysis", False))
        safety["allow_launching_apps"] = _confirm("Allow launching apps for dynamic analysis?", default=safety.get("allow_launching_apps", False))
    save_config(cfg)
    log_setup("setup complete")
    print()
    print("Setup complete. Run `are doctor` anytime to re-check, or `are setup` to rerun.")
    return cfg
