"""Tests for YouTube URL detection, format/quality selection, probe normalization, and persistence."""

import asyncio
from pathlib import Path

from hyperdl.config import HyperDLConfig
from hyperdl.engine.manager import DownloadManager
from hyperdl.engine.youtube_engine import (
    _extract_probe_info,
    build_ydl_format_spec,
    is_youtube_url,
)
from hyperdl.models import Category, DownloadType


def test_is_youtube_url():
    assert is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert is_youtube_url("https://youtu.be/dQw4w9WgXcQ")
    assert is_youtube_url("https://music.youtube.com/watch?v=abc")
    assert is_youtube_url("https://youtube.com/shorts/xyz")
    assert not is_youtube_url("https://github.com/psf/requests")
    assert not is_youtube_url("https://example.com/video.mp4")


def test_build_ydl_format_spec_video_and_audio():
    v_mp4 = build_ydl_format_spec(media_mode="video", quality="1080", container="mp4", has_ffmpeg=True)
    assert "[height<=1080]" in v_mp4["format"]
    assert v_mp4["merge_output_format"] == "mp4"
    assert v_mp4["postprocessors"] == []

    v_webm = build_ydl_format_spec(media_mode="video", quality="720", container="webm", has_ffmpeg=True)
    assert "[height<=720]" in v_webm["format"]
    assert v_webm["merge_output_format"] == "webm"

    a_mp3 = build_ydl_format_spec(media_mode="audio", container="mp3", audio_bitrate="320", has_ffmpeg=True)
    assert a_mp3["format"] == "bestaudio/best"
    assert a_mp3["postprocessors"][0]["preferredcodec"] == "mp3"
    assert a_mp3["postprocessors"][0]["preferredquality"] == "320"

    a_m4a = build_ydl_format_spec(media_mode="audio", container="m4a", audio_bitrate="192", has_ffmpeg=True)
    assert a_m4a["postprocessors"][0]["preferredcodec"] == "m4a"
    assert a_m4a["postprocessors"][0]["preferredquality"] == "192"


def test_extract_probe_info_normalizes_qualities():
    sample_info = {
        "title": "Demo Video",
        "uploader": "Channel Name",
        "duration": 185,
        "thumbnail": "https://i.ytimg.com/vi/abc/hqdefault.jpg",
        "webpage_url": "https://www.youtube.com/watch?v=abc",
        "formats": [
            {"vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "filesize": 3_000_000},
            {"vcodec": "avc1.640028", "acodec": "none", "height": 1080, "fps": 60, "ext": "mp4", "filesize": 40_000_000},
            {"vcodec": "avc1.4d401f", "acodec": "none", "height": 720, "fps": 30, "ext": "mp4", "filesize": 18_000_000},
            {"vcodec": "avc1.42001e", "acodec": "mp4a.40.2", "height": 360, "fps": 30, "ext": "mp4", "filesize": 8_000_000},
        ],
    }
    res = _extract_probe_info(sample_info, ffmpeg_path="/usr/bin/ffmpeg")
    assert res["title"] == "Demo Video"
    assert res["duration_text"] == "3:05"
    assert res["has_ffmpeg"] is True
    vals = [q["value"] for q in res["video_qualities"]]
    assert vals == ["best", "1080", "720", "360"]
    assert res["video_qualities"][1]["estimated_size"] == 43_000_000
    assert res["video_qualities"][3]["estimated_size"] == 8_000_000


def test_youtube_download_record_persistence(tmp_path: Path):
    async def run():
        cfg = HyperDLConfig(data_dir=tmp_path / "data", download_dir=tmp_path / "dl")
        mgr = DownloadManager(cfg)
        rec = await mgr.add_download(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            media_options={"media_mode": "audio", "container": "mp3", "audio_bitrate": "320"},
            auto_start=False,
        )
        assert rec.download_type == DownloadType.YOUTUBE.value
        assert rec.category == Category.MUSIC.value
        loaded = mgr.db.get_download(rec.id)
        assert loaded is not None
        assert loaded.media_options == {"media_mode": "audio", "container": "mp3", "audio_bitrate": "320"}
        mgr.db.close()

    asyncio.run(run())
