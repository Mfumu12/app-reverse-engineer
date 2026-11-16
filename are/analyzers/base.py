"""Base analyzer interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Evidence:
    """A single piece of evidence found during an investigation."""
    kind: str  # string | symbol | source-snippet | config | import | section | screenshot | note
    title: str
    detail: str
    file: str | None = None
    line: int | None = None
    source: str | None = None   # which tool produced it (strings, objdump, grep, ...)
    score: float = 0.0          # relevance to the question (0..1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "file": self.file,
            "line": self.line,
            "source": self.source,
            "score": self.score,
        }

    def summary_line(self) -> str:
        loc = ""
        if self.file:
            loc = f"{self.file}" + (f":{self.line}" if self.line is not None else "")
        return f"- [{self.kind}] {self.title}" + (f"  ({loc})" if loc else "") + f"\n    {self.detail[:200]}"


@dataclass
class AnalysisResult:
    kind: str  # native | javascript | electron | unknown
    info: dict[str, Any]
    evidence: list[Evidence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, ev: Evidence) -> None:
        self.evidence.append(ev)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "info": self.info,
            "evidence": [e.to_dict() for e in self.evidence],
            "notes": self.notes,
            "errors": self.errors,
        }


class Analyzer:
    name = "base"

    def supports(self, info: dict[str, Any]) -> bool:
        return True

    def run(self, info: dict[str, Any], question: str, safety: dict) -> AnalysisResult:
        raise NotImplementedError
