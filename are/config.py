"""Configuration for app-reverse-engineer.

Everything lives under ~/.app-reverse-engineer so the tool is fully local and
offline-first. The config file is created by the guided setup and can be
rerun at any time with `are setup`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - yaml is optional but strongly recommended
    yaml = None

CONFIG_DIR = Path.home() / ".app-reverse-engineer"
HISTORY_DIR = CONFIG_DIR / "history"
EVIDENCE_DIR = CONFIG_DIR / "evidence"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
SETUP_LOG = CONFIG_DIR / "setup.log"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "llm": {
        "provider": "none",   # none | ollama | openai
        "model": "",
        "base_url": "http://localhost:11434",
        "api_key": "",
    },
    "safety": {
        "ask_before_install": True,
        "allow_network_analysis": False,   # keep analysis offline by default
        "allow_launching_apps": False,     # dynamic analysis needs explicit consent
    },
    "mcp": {
        "enabled": False,
        "transport": "stdio",
    },
}


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, HISTORY_DIR, EVIDENCE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    ensure_dirs()
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    if yaml is None:
        return dict(DEFAULT_CONFIG)
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    # merge with defaults so new keys are present
    merged = dict(DEFAULT_CONFIG)
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


def save_config(cfg: dict[str, Any]) -> None:
    ensure_dirs()
    if yaml is not None:
        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)
    else:
        # minimal fallback: write a simple ini-ish text is not ideal; just skip
        pass


def log_setup(message: str) -> None:
    ensure_dirs()
    with SETUP_LOG.open("a", encoding="utf-8") as f:
        f.write(message.rstrip("\n") + "\n")
