"""Investigation history: save, list, and reload past investigations."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import HISTORY_DIR, EVIDENCE_DIR, ensure_dirs


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "investigation"


def new_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def save_investigation(
    inv_id: str | None,
    question: str,
    info: dict[str, Any],
    result,
    report_md: str,
    evidence_files: list[dict[str, str]] | None = None,
) -> str:
    """Save an investigation under HISTORY_DIR/<id>-<slug>/ and return the dir path."""
    ensure_dirs()
    inv_id = inv_id or new_id()
    slug = _slug(question)[:40]
    dir_name = f"{inv_id}-{slug}"
    inv_dir = HISTORY_DIR / dir_name
    inv_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "id": inv_id,
        "question": question,
        "target": info.get("path"),
        "app_type": result.kind,
        "file_type": info.get("file_type"),
        "created_at": datetime.now().isoformat(),
        "evidence_count": len(result.evidence),
        "note_count": len(result.notes),
        "evidence_files": evidence_files or [],
    }
    (inv_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (inv_dir / "report.md").write_text(report_md, encoding="utf-8")
    (inv_dir / "evidence.json").write_text(
        json.dumps([e.to_dict() for e in result.evidence], indent=2), encoding="utf-8"
    )
    (inv_dir / "info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return str(inv_dir)


def list_investigations() -> list[dict[str, Any]]:
    ensure_dirs()
    items: list[dict[str, Any]] = []
    for d in sorted(HISTORY_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        meta_f = d / "meta.json"
        if meta_f.exists():
            try:
                items.append(json.loads(meta_f.read_text(encoding="utf-8")))
            except Exception:
                items.append({"id": d.name, "question": "(unreadable)", "target": "", "created_at": ""})
    return items


def get_investigation(inv_id: str) -> dict[str, Any] | None:
    ensure_dirs()
    # match by id prefix or exact dir name
    for d in HISTORY_DIR.iterdir():
        if d.is_dir() and (d.name == inv_id or d.name.startswith(inv_id + "-") or d.name.startswith(inv_id)):
            meta_f = d / "meta.json"
            report_f = d / "report.md"
            ev_f = d / "evidence.json"
            data: dict[str, Any] = {}
            if meta_f.exists():
                data.update(json.loads(meta_f.read_text(encoding="utf-8")))
            if report_f.exists():
                data["report"] = report_f.read_text(encoding="utf-8")
            if ev_f.exists():
                data["evidence"] = json.loads(ev_f.read_text(encoding="utf-8"))
            data["dir"] = str(d)
            return data
    return None
