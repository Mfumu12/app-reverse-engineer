"""Tests for cURL parser, wildcard expander, and filename utilities."""

from pathlib import Path

from hyperdl.utils.checksum import compute_checksum_sync
from hyperdl.utils.filename import extract_filename, resolve_unique_path, sanitize_filename
from hyperdl.utils.importers import expand_wildcard_url, extract_urls_from_text, parse_curl_command


def test_parse_curl_command():
    cmd = (
        "curl 'https://cdn.example.com/archive.zip?token=123' "
        "-H 'Cookie: session=abc; theme=dark' "
        "-H 'Authorization: Bearer xyz' "
        "-A 'CustomAgent/2.0' "
        "-o my_archive.zip"
    )
    parsed = parse_curl_command(cmd)
    assert parsed.url == "https://cdn.example.com/archive.zip?token=123"
    assert parsed.headers["Cookie"] == "session=abc; theme=dark"
    assert parsed.headers["Authorization"] == "Bearer xyz"
    assert parsed.user_agent == "CustomAgent/2.0"
    assert parsed.suggested_name == "my_archive.zip"


def test_expand_wildcard_numeric_and_alpha():
    nums = expand_wildcard_url("https://host.test/part_[01..04].zip")
    assert nums == [
        "https://host.test/part_01.zip",
        "https://host.test/part_02.zip",
        "https://host.test/part_03.zip",
        "https://host.test/part_04.zip",
    ]
    chars = expand_wildcard_url("https://host.test/vol_[a..c].bin")
    assert chars == [
        "https://host.test/vol_a.bin",
        "https://host.test/vol_b.bin",
        "https://host.test/vol_c.bin",
    ]


def test_extract_urls_from_text():
    urls = extract_urls_from_text("Check https://a.com/1.zip, and http://b.org/2.tar.gz.")
    assert urls == ["https://a.com/1.zip", "http://b.org/2.tar.gz"]


def test_extract_filename_rfc5987_and_mime():
    name = extract_filename(
        "https://example.com/dl?id=5",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''relat%C3%B3rio%20final.pdf"},
    )
    assert name == "relatório final.pdf"

    mime_name = extract_filename(
        "https://example.com/dl",
        headers={"Content-Disposition": 'attachment; filename="=?UTF-8?B?dGVzdGVfZmlsZS56aXA=?="'},
    )
    assert mime_name == "teste_file.zip"


def test_sanitize_and_unique_path(tmp_path: Path):
    assert sanitize_filename('bad<>:"/\\|?*name.exe') == "bad_________name.exe"
    first = tmp_path / "sample.bin"
    first.write_bytes(b"abc")
    unique = resolve_unique_path(tmp_path, "sample.bin")
    assert unique.name == "sample (1).bin"
    assert len(compute_checksum_sync(first, "sha256")) == 64
