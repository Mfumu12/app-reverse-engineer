"""Filename extraction, sanitization, and collision-safe path resolution."""

from __future__ import annotations

import email.header
import mimetypes
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def decode_mime_words(value: str) -> str:
    if "=?" not in value:
        return value
    try:
        parts = email.header.decode_header(value)
        decoded = []
        for raw, enc in parts:
            if isinstance(raw, bytes):
                decoded.append(raw.decode(enc or "utf-8", errors="replace"))
            else:
                decoded.append(raw)
        return "".join(decoded)
    except Exception:
        return value


def sanitize_filename(name: str, fallback: str = "download.bin") -> str:
    name = decode_mime_words(name).strip().strip(".")
    name = INVALID_CHARS_RE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name or name in (".", ".."):
        return fallback
    # Keep filename within conservative OS limits (200 chars)
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 16:
            name = f"{stem[:200 - len(ext) - 1]}.{ext}"
        else:
            name = name[:200]
    return name


def parse_content_disposition(header_value: str) -> str | None:
    if not header_value:
        return None
    # Prefer RFC 5987 filename*=charset'lang'value
    m_ext = re.search(r"filename\*\s*=\s*([^;]+)", header_value, re.IGNORECASE)
    if m_ext:
        raw = m_ext.group(1).strip().strip('"').strip("'")
        if "''" in raw:
            charset, _, encoded = raw.partition("''")
            try:
                return unquote(encoded, encoding=charset or "utf-8")
            except Exception:
                return unquote(encoded)
        return unquote(raw)

    m_std = re.search(r'filename\s*=\s*"([^"]+)"', header_value, re.IGNORECASE)
    if m_std:
        return decode_mime_words(m_std.group(1))

    m_unquoted = re.search(r"filename\s*=\s*([^;\s]+)", header_value, re.IGNORECASE)
    if m_unquoted:
        return decode_mime_words(m_unquoted.group(1).strip('"'))

    return None


def extract_filename(
    url: str,
    headers: dict[str, str] | None = None,
    suggested_name: str | None = None,
    mime_type: str = "",
) -> str:
    if suggested_name and suggested_name.strip():
        return sanitize_filename(suggested_name)

    if headers:
        lowered = {k.lower(): v for k, v in headers.items()}
        cd = lowered.get("content-disposition", "")
        cd_name = parse_content_disposition(cd)
        if cd_name:
            return sanitize_filename(cd_name)

    parsed = urlparse(url)
    path_name = unquote(Path(parsed.path).name)
    if path_name and path_name != "/":
        clean = sanitize_filename(path_name)
        if "." not in clean and mime_type:
            ext = mimetypes.guess_extension(mime_type.split(";")[0].strip())
            if ext:
                clean = f"{clean}{ext}"
        return clean

    ext = ""
    if mime_type:
        ext = mimetypes.guess_extension(mime_type.split(";")[0].strip()) or ""
    host = parsed.hostname or "download"
    return sanitize_filename(f"{host}_file{ext or '.bin'}")


def part_file_path(target_path: Path | str) -> Path:
    p = Path(target_path)
    return p.with_name(f"{p.name}.hyperdl.part")


def resolve_unique_path(directory: Path | str, filename: str) -> Path:
    base_dir = Path(directory)
    clean_name = sanitize_filename(filename)
    candidate = base_dir / clean_name
    if not candidate.exists() and not part_file_path(candidate).exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1
    while True:
        alt = base_dir / f"{stem} ({counter}){suffix}"
        if not alt.exists() and not part_file_path(alt).exists():
            return alt
        counter += 1
