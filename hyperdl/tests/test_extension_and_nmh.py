import json
import urllib.request
from pathlib import Path

import pytest

from hyperdl import browser_installer, native_messaging


def test_register_native_messaging_host_includes_fixed_and_extra_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_installer, "_local_app_base", lambda: tmp_path)
    ext_dir = tmp_path / "extension"
    ext_dir.mkdir(parents=True)

    res = browser_installer.register_native_messaging_host(
        ext_dir,
        extra_extension_ids=["bcdefghijklmnopabcdefghijklmnop"],  # 31 chars ignored
    )
    res = browser_installer.register_native_messaging_host(
        ext_dir,
        extra_extension_ids=["bcdefghijklmnopabcdefghijklmnopa"],  # 32 chars accepted
    )
    manifest_data = json.loads(Path(res["manifest"]).read_text(encoding="utf-8"))
    origins = manifest_data["allowed_origins"]
    assert f"chrome-extension://{browser_installer.FIXED_EXTENSION_ID}/" in origins
    assert "chrome-extension://bcdefghijklmnopabcdefghijklmnopa/" in origins
    assert (tmp_path / "host_config.json").exists()


def test_ensure_server_running_auto_launches_when_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"ping": 0, "launch": 0}

    def fake_ping(url: str, timeout: float = 0.6) -> bool:
        calls["ping"] += 1
        return calls["ping"] >= 3

    def fake_launch(hidden: bool = True) -> bool:
        calls["launch"] += 1
        assert hidden is True
        return True

    monkeypatch.setattr(native_messaging, "_ping_server", fake_ping)
    monkeypatch.setattr(native_messaging, "_launch_gennius_dm", fake_launch)

    ok, launched = native_messaging.ensure_server_running("http://127.0.0.1:8787", timeout=2.0)
    assert ok is True
    assert launched is True
    assert calls["launch"] == 1
