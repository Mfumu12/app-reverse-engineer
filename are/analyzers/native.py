"""Static analysis for native binaries (ELF/PE/Mach-O)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .base import AnalysisResult, Evidence, Analyzer

KEYWORDS_DEFAULT = ["search", "query", "api", "http", "connect", "socket", "open", "read", "write"]


def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out: {' '.join(cmd)}"


def _strings(path: Path, min_len: int = 6) -> str:
    rc, out, err = _run(["strings", f"-n{min_len}", str(path)])
    return out if rc == 0 else err


def _keywords_in_text(text: str, keywords: list[str]) -> list[str]:
    found = []
    low = text.lower()
    for kw in keywords:
        if kw.lower() in low:
            found.append(kw)
    return found


def _extract_context(text: str, keyword: str, width: int = 120) -> str:
    low = text.lower()
    idx = low.find(keyword.lower())
    if idx == -1:
        return ""
    start = max(0, idx - width)
    end = min(len(text), idx + len(keyword) + width)
    snippet = text[start:end]
    return snippet.replace("\n", " ").strip()


class NativeAnalyzer(Analyzer):
    name = "native"

    def supports(self, info: dict[str, Any]) -> bool:
        ft = (info.get("file_type") or "").lower()
        return info.get("kind") == "native" or any(t in ft for t in ("elf", "pe32", "mach-o"))

    def run(self, info: dict[str, Any], question: str, safety: dict) -> AnalysisResult:
        res = AnalysisResult(kind="native", info=info)
        path = Path(info["path"])
        keywords = _keywords_from_question(question) or KEYWORDS_DEFAULT

        # file type already in info
        res.notes.append(f"Binary type: {info.get('file_type')}")

        # strings + keyword hits
        strings_out = _strings(path)
        if strings_out:
            hits = _keywords_in_text(strings_out, keywords)
            res.notes.append(f"strings keyword hits: {', '.join(hits) if hits else 'none'}")
            for kw in hits[:8]:
                ctx = _extract_context(strings_out, kw)
                res.add(Evidence(
                    kind="string",
                    title=f"string contains '{kw}'",
                    detail=ctx or strings_out[:200],
                    file=str(path),
                    source="strings",
                    score=0.6,
                ))
            # save a sample of strings as evidence
            sample = "\n".join(strings_out.splitlines()[:50])
            res.add(Evidence(
                kind="note",
                title="strings sample (first 50 lines)",
                detail=sample,
                file=str(path),
                source="strings",
                score=0.3,
            ))

        # dynamic symbols
        for cmd, label in (
            (["nm", "-D", str(path)], "dynamic symbols (nm -D)"),
            (["objdump", "-T", str(path)], "dynamic symbols (objdump -T)"),
        ):
            rc, out, err = _run(cmd)
            if rc == 0 and out.strip():
                res.notes.append(label + f": {len(out.splitlines())} lines")
                # look for interesting imports
                interesting = [l for l in out.splitlines() if any(
                    s in l.lower() for s in ("search", "query", "http", "socket", "connect", "sqlite", "json", "curl")
                )]
                for l in interesting[:10]:
                    res.add(Evidence(
                        kind="symbol",
                        title=label,
                        detail=l.strip(),
                        file=str(path),
                        source=cmd[0],
                        score=0.7,
                    ))
                break  # use whichever works first

        # shared libraries
        rc, out, err = _run(["ldd", str(path)])
        if rc == 0 and out.strip():
            libs = [l.split()[0] for l in out.splitlines() if "=>" in l or "\t" in l]
            res.notes.append(f"shared libs: {', '.join(libs[:10]) if libs else out.strip()[:120]}")
            res.add(Evidence(
                kind="import",
                title="shared libraries (ldd)",
                detail=out.strip()[:1500],
                file=str(path),
                source="ldd",
                score=0.4,
            ))

        # sections
        rc, out, err = _run(["readelf", "-S", str(path)])
        if rc == 0 and out.strip():
            res.add(Evidence(
                kind="section",
                title="sections (readelf -S)",
                detail=out.strip()[:1500],
                file=str(path),
                source="readelf",
                score=0.3,
            ))

        return res


def _keywords_from_question(question: str) -> list[str]:
    # naive extraction of quoted words + common nouns
    words = re.findall(r"[A-Za-z]{4,}", question)
    stop = {"what", "how", "does", "work", "this", "app", "feature", "with", "from", "have", "the"}
    return [w for w in words if w.lower() not in stop][:8]
