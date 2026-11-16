"""Static analysis for JavaScript / Node projects."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import AnalysisResult, Evidence, Analyzer


class JavaScriptAnalyzer(Analyzer):
    name = "javascript"

    def supports(self, info: dict[str, Any]) -> bool:
        return info.get("kind") in ("javascript", "electron")

    def run(self, info: dict[str, Any], question: str, safety: dict) -> AnalysisResult:
        res = AnalysisResult(kind="javascript", info=info)
        root = Path(info["path"])
        keywords = _keywords_from_question(question)

        pj = info.get("package_json") or {}
        if pj:
            res.add(Evidence(
                kind="config",
                title="package.json",
                detail=(
                    f"name={pj.get('name')} version={pj.get('version')} "
                    f"main={pj.get('main')} "
                    f"deps={list((pj.get('dependencies') or {}).keys())[:12]}"
                ),
                file=str(root / "package.json"),
                source="package.json",
                score=0.5,
            ))
            deps = {**pj.get("dependencies", {}), **pj.get("devDependencies", {})}
            interesting = [d for d in deps if d.lower() in
                           ("electron", "axios", "node-fetch", "express", "sqlite3", "better-sqlite3", "request")]
            if interesting:
                res.notes.append(f"interesting deps: {', '.join(interesting)}")

        # scan source files for keywords and network calls
        src_files = info.get("source_files") or []
        hits = 0
        for rel in src_files[:120]:
            fp = root / rel
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            low = text.lower()
            for kw in keywords:
                if kw.lower() in low:
                    hits += 1
                    ctx = _context(text, kw)
                    res.add(Evidence(
                        kind="source-snippet",
                        title=f"source mentions '{kw}'",
                        detail=ctx,
                        file=str(fp),
                        source="grep",
                        score=0.65,
                    ))
                    break  # one snippet per file to keep it tidy
            # network / fetch hints
            if any(tok in low for tok in ("fetch(", "axios", "xhr", "xmlhttprequest", "req.", "app.get", "app.post")):
                for m in re.finditer(r"(fetch|axios|\.get\(|\.post\(|xmlhttprequest)", low):
                    s = max(0, m.start() - 60)
                    e = min(len(text), m.end() + 160)
                    snippet = text[s:e].replace("\n", " ").strip()
                    res.add(Evidence(
                        kind="source-snippet",
                        title="network call found",
                        detail=snippet,
                        file=str(fp),
                        source="grep",
                        score=0.7,
                    ))
                    break

        res.notes.append(f"scanned {len(src_files)} source files, {hits} keyword hits")
        return res


def _context(text: str, keyword: str, width: int = 150) -> str:
    low = text.lower()
    idx = low.find(keyword.lower())
    if idx == -1:
        return text[:200]
    s = max(0, idx - width)
    e = min(len(text), idx + len(keyword) + width)
    return text[s:e].replace("\n", " ").strip()


def _keywords_from_question(question: str) -> list[str]:
    words = re.findall(r"[A-Za-z]{4,}", question)
    stop = {"what", "how", "does", "work", "this", "app", "feature", "with", "from", "have", "the", "like", "make"}
    return [w for w in words if w.lower() not in stop][:8]
