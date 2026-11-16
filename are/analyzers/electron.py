"""Analysis for Electron apps (builds on JavaScriptAnalyzer)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import AnalysisResult, Evidence, Analyzer
from .javascript import JavaScriptAnalyzer


class ElectronAnalyzer(Analyzer):
    name = "electron"

    def supports(self, info: dict[str, Any]) -> bool:
        return info.get("kind") == "electron"

    def run(self, info: dict[str, Any], question: str, safety: dict) -> AnalysisResult:
        # Reuse the JS analysis first
        res = JavaScriptAnalyzer().run(info, question, safety)
        res.kind = "electron"
        root = Path(info["path"])

        # main process
        main_file = info.get("main_file")
        if main_file and Path(main_file).exists():
            text = Path(main_file).read_text(encoding="utf-8", errors="replace")
            low = text.lower()
            for token in ("browserwindow", "webpreferences", "ipcmain", "ipcrenderer", "contextbridge", "preload"):
                if token in low:
                    idx = low.find(token)
                    s = max(0, idx - 80)
                    e = min(len(text), idx + 220)
                    res.add(Evidence(
                        kind="source-snippet",
                        title=f"main process uses {token}",
                        detail=text[s:e].replace("\n", " ").strip(),
                        file=main_file,
                        source="grep",
                        score=0.7,
                    ))
                    break

            if "ipcmain" in low:
                res.notes.append("main process exposes IPC handlers - search feature may go through ipcMain")
            if "contextbridge" in low or "ipcrenderer" in low:
                res.notes.append("preload exposes APIs to renderer via contextBridge/ipcRenderer")

        # preload
        preload = info.get("preload_file")
        if preload and Path(preload).exists():
            text = Path(preload).read_text(encoding="utf-8", errors="replace")
            if "exposeInMainWorld" in text:
                res.add(Evidence(
                    kind="source-snippet",
                    title="preload exposes API to renderer (contextBridge)",
                    detail=text[:1200].replace("\n", " ").strip(),
                    file=preload,
                    source="grep",
                    score=0.75,
                ))
                res.notes.append("renderer can call preload-exposed functions - good place to find search logic")

        # resources/app layout
        resources_app = root / "resources" / "app"
        if resources_app.exists():
            res.notes.append(f"Electron resources/app present at {resources_app}")

        return res
