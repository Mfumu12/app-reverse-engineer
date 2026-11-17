"""Importers: cURL command parser, wildcard batch URL expander, and link extractor."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field


@dataclass
class ParsedCurl:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    user_agent: str = ""
    suggested_name: str = ""


def parse_curl_command(command: str) -> ParsedCurl:
    """Parse a curl command (e.g. copied from Chrome/Firefox DevTools) into URL + headers."""
    text = command.strip()
    # Handle multiline shell continuations (`\` or Windows `^`)
    text = re.sub(r"\\\r?\n", " ", text)
    text = re.sub(r"\^\r?\n", " ", text)
    # Windows cmd caret escapes like ^" or ^&
    if "^" in text and "curl" in text.lower():
        text = text.replace('^"', '"').replace("^&", "&").replace("^%", "%")

    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        tokens = text.split()

    if not tokens:
        raise ValueError("Empty cURL command")

    if tokens[0].lower() in ("curl", "curl.exe"):
        tokens = tokens[1:]

    url = ""
    headers: dict[str, str] = {}
    user_agent = ""
    suggested_name = ""

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-H", "--header") and i + 1 < len(tokens):
            hdr = tokens[i + 1]
            if ":" in hdr:
                k, _, v = hdr.partition(":")
                key = k.strip()
                val = v.strip()
                if key.lower() == "user-agent":
                    user_agent = val
                else:
                    headers[key] = val
            i += 2
        elif tok in ("-A", "--user-agent") and i + 1 < len(tokens):
            user_agent = tokens[i + 1]
            i += 2
        elif tok in ("-b", "--cookie") and i + 1 < len(tokens):
            headers["Cookie"] = tokens[i + 1]
            i += 2
        elif tok in ("-e", "--referer") and i + 1 < len(tokens):
            headers["Referer"] = tokens[i + 1]
            i += 2
        elif tok in ("-o", "--output") and i + 1 < len(tokens):
            suggested_name = tokens[i + 1]
            i += 2
        elif tok == "--url" and i + 1 < len(tokens):
            url = tokens[i + 1]
            i += 2
        elif tok in ("-X", "--request", "-d", "--data", "--data-raw", "--data-binary") and i + 1 < len(tokens):
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            if not url and (tok.startswith("http://") or tok.startswith("https://")):
                url = tok
            i += 1

    if not url:
        m = re.search(r"https?://[^\s'\"]+", text)
        if m:
            url = m.group(0)

    if not url:
        raise ValueError("No HTTP/HTTPS URL found in cURL command")

    return ParsedCurl(url=url, headers=headers, user_agent=user_agent, suggested_name=suggested_name)


NUM_RANGE_RE = re.compile(r"\[(\d+)\.\.(\d+)\]")
ALPHA_RANGE_RE = re.compile(r"\[([a-zA-Z])\.\.([a-zA-Z])\]")


def expand_wildcard_url(pattern: str, max_items: int = 500) -> list[str]:
    """Expand IDM/ABD-style batch wildcard patterns like https://host/part_[01..10].zip or [a..f]."""
    pattern = pattern.strip()
    if not pattern:
        return []

    m_num = NUM_RANGE_RE.search(pattern)
    if m_num:
        start_s, end_s = m_num.group(1), m_num.group(2)
        start, end = int(start_s), int(end_s)
        width = len(start_s) if start_s.startswith("0") and len(start_s) > 1 else 0
        step = 1 if end >= start else -1
        results: list[str] = []
        for n in range(start, end + step, step):
            rep = str(n).zfill(width) if width else str(n)
            expanded = pattern[: m_num.start()] + rep + pattern[m_num.end() :]
            results.extend(expand_wildcard_url(expanded, max_items=max_items - len(results)))
            if len(results) >= max_items:
                break
        return results[:max_items]

    m_alpha = ALPHA_RANGE_RE.search(pattern)
    if m_alpha:
        c1, c2 = m_alpha.group(1), m_alpha.group(2)
        if c1.isupper() == c2.isupper():
            step = 1 if ord(c2) >= ord(c1) else -1
            results = []
            for code in range(ord(c1), ord(c2) + step, step):
                expanded = pattern[: m_alpha.start()] + chr(code) + pattern[m_alpha.end() :]
                results.extend(expand_wildcard_url(expanded, max_items=max_items - len(results)))
                if len(results) >= max_items:
                    break
            return results[:max_items]

    return [pattern]


def extract_urls_from_text(text: str) -> list[str]:
    found = re.findall(r"https?://[^\s<>'\"`]+", text)
    seen: set[str] = set()
    unique: list[str] = []
    for u in found:
        u = u.rstrip(".,;)")
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique
