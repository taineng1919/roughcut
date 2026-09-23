from __future__ import annotations

import shutil
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import pytest

from roughcut.adapters import component_download


@pytest.fixture
def synthetic_media_runtime(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Opt in to local FFmpeg only for synthetic-media tests."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.fail("Roughcut 测试 fixture 未解析到 ffmpeg/ffprobe")
    monkeypatch.setenv("ROUGHCUT_FFMPEG_COMMAND", ffmpeg)
    monkeypatch.setenv("ROUGHCUT_FFPROBE_COMMAND", ffprobe)
    return ffmpeg, ffprobe


@pytest.fixture(autouse=True)
def reject_non_loopback_component_artifact_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_urlopen: Callable[..., Any] = component_download.urllib.request.urlopen

    def loopback_only_urlopen(request: object, *args: object, **kwargs: object) -> Any:
        url = getattr(request, "full_url", request)
        hostname = urlsplit(str(url)).hostname
        if hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise AssertionError(
                "test component artifact network must use a loopback server"
            )
        return real_urlopen(request, *args, **kwargs)

    monkeypatch.setattr(
        component_download.urllib.request,
        "urlopen",
        loopback_only_urlopen,
    )
