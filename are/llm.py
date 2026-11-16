"""Optional LLM integration for polishing plain-English reports.

Offline-first: returns None unless a provider is explicitly configured.
Supported: ollama (local) and openai (API). Never sends data unless configured.
"""

from __future__ import annotations

from typing import Any


def is_enabled(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("llm", {}).get("provider") in ("ollama", "openai"))


def polish(cfg: dict[str, Any], question: str, report_md: str) -> str | None:
    """Try to rewrite/polish the report into plain English. Returns None on any failure."""
    provider = (cfg.get("llm", {}) or {}).get("provider", "none")
    if provider == "ollama":
        return _ollama(cfg, question, report_md)
    if provider == "openai":
        return _openai(cfg, question, report_md)
    return None


def _ollama(cfg: dict[str, Any], question: str, report_md: str) -> str | None:
    try:
        import ollama  # type: ignore
    except Exception:
        return None
    model = (cfg.get("llm", {}) or {}).get("model") or "llama3.2"
    base_url = (cfg.get("llm", {}) or {}).get("base_url") or "http://localhost:11434"
    prompt = (
        "Rewrite the following reverse-engineering findings into plain English. "
        "Explain how the feature works, list the evidence used, and give a short "
        "code skeleton to recreate a similar feature. Keep it concise and beginner-friendly.\n\n"
        f"QUESTION: {question}\n\nFINDINGS:\n{report_md}"
    )
    try:
        resp = ollama.chat(model=model, messages=[{"role": "user", "content": prompt}])
        return resp["message"]["content"]
    except Exception:
        return None


def _openai(cfg: dict[str, Any], question: str, report_md: str) -> str | None:
    try:
        import requests  # type: ignore
    except Exception:
        return None
    api_key = (cfg.get("llm", {}) or {}).get("api_key", "")
    model = (cfg.get("llm", {}) or {}).get("model") or "gpt-4o-mini"
    if not api_key:
        return None
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "You explain reverse-engineering findings in plain English."},
                    {"role": "user", "content": (
                        "Rewrite the following findings into plain English: how the feature works, "
                        "the evidence used, and a short code skeleton to recreate a similar feature. "
                        f"QUESTION: {question}\n\nFINDINGS:\n{report_md}"
                    )},
                ],
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None
