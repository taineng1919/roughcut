from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from windows.w4_support import CORE_ROOT, child_environment, parse_json_bytes
from windows.w5_review_process import (
    ReviewCliChild,
    assert_port_closed,
    review_request,
    session_cookie,
)
from windows.w5_review_support import seed_artifact_review_project

from roughcut.adapters.project_store import ProjectStore
from roughcut.domain.project import ImportMode, ProjectError
from roughcut.review.server import start_review_server

_ReviewCliChild = ReviewCliChild
_request = review_request
_cookie = session_cookie
_assert_port_closed = assert_port_closed


def _session_headers(cookie: str, *, origin: str | None = None) -> dict[str, str]:
    headers = {"Cookie": cookie}
    if origin is not None:
        headers.update({"Content-Type": "application/json", "Origin": origin})
    return headers


def test_w5_real_cli_review_server_covers_http_security_media_and_static_contract(
    tmp_path: Path,
) -> None:
    project, _proposal_id, media = seed_artifact_review_project(
        tmp_path / "中文 Review project with spaces"
    )
    child = _ReviewCliChild.start(project)
    try:
        parsed = urlsplit(child.url)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port == child.port
        if os.name == "nt":
            assert project.drive
            assert ":" in str(project)

        status, index_headers, index = _request(
            child.port,
            "GET",
            f"/?token={child.token}",
            headers={"Host": f"localhost:{child.port}"},
        )
        assert status == 200
        assert index_headers["content-type"] == "text/html"
        assert int(index_headers["content-length"]) == len(index)
        cookie = _cookie(index_headers)
        assert cookie == f"roughcut_session={child.token}"

        script_match = re.search(rb'src="(/assets/[^\"]+\.js)"', index)
        style_match = re.search(rb'href="(/assets/[^\"]+\.css)"', index)
        assert script_match is not None
        assert style_match is not None
        for asset_path in (
            script_match.group(1).decode("ascii"),
            style_match.group(1).decode("ascii"),
        ):
            status, asset_headers, asset = _request(
                child.port,
                "GET",
                asset_path,
                headers=_session_headers(cookie),
            )
            assert status == 200
            assert int(asset_headers["content-length"]) == len(asset)
            assert len(asset) > 100
            status, head_headers, head = _request(
                child.port,
                "HEAD",
                asset_path,
                headers=_session_headers(cookie),
            )
            assert status == 200
            assert head == b""
            assert head_headers["content-length"] == asset_headers["content-length"]

        status, _headers, body = _request(
            child.port,
            "GET",
            "/api/review",
            headers=_session_headers(cookie),
        )
        assert status == 200
        payload = json.loads(body)
        serialized = json.dumps(payload, ensure_ascii=False)
        assert str(project) not in serialized
        assert str(media) not in serialized
        assert "locator" not in serialized
        assert child.token not in serialized
        assert payload["basis"]["type"] == "proposal"

        assert _request(child.port, "GET", "/api/review")[0] == 403
        assert (
            _request(
                child.port,
                "GET",
                "/api/review",
                headers={"Cookie": cookie, "Host": "evil.test"},
            )[0]
            == 403
        )
        assert (
            _request(
                child.port,
                "GET",
                "/api/review",
                headers={"Cookie": "roughcut_session=wrong"},
            )[0]
            == 403
        )
        assert (
            _request(
                child.port,
                "GET",
                "/media/%2e%2e/project.json",
                headers=_session_headers(cookie),
            )[0]
            == 404
        )
        assert (
            _request(
                child.port,
                "GET",
                "/media/source_unknown",
                headers=_session_headers(cookie),
            )[0]
            == 404
        )

        status, media_headers, body = _request(
            child.port,
            "GET",
            "/media/src_w5",
            headers=_session_headers(cookie),
        )
        assert status == 200
        assert body == media.read_bytes()
        assert media_headers["accept-ranges"] == "bytes"
        assert media_headers["content-length"] == str(media.stat().st_size)
        assert media_headers["content-type"] == "video/mp4"

        status, range_headers, body = _request(
            child.port,
            "GET",
            "/media/src_w5",
            headers={**_session_headers(cookie), "Range": "bytes=3-17"},
        )
        assert status == 206
        assert body == media.read_bytes()[3:18]
        assert range_headers["content-range"] == f"bytes 3-17/{media.stat().st_size}"
        assert range_headers["content-length"] == "15"
        status, head_headers, head = _request(
            child.port,
            "HEAD",
            "/media/src_w5",
            headers=_session_headers(cookie),
        )
        assert status == 200
        assert head == b""
        assert head_headers["content-length"] == str(media.stat().st_size)
        status, _headers, body = _request(
            child.port,
            "GET",
            "/media/src_w5",
            headers={**_session_headers(cookie), "Range": "bytes=999999-"},
        )
        assert status == 416
        assert body == b""

        mutation_headers = _session_headers(
            cookie,
            origin=f"http://localhost:{child.port}",
        )
        status, body_headers, body = _request(
            child.port,
            "POST",
            "/api/proposals",
            headers=mutation_headers,
            body=b"{}",
        )
        assert status == 409
        assert json.loads(body)["error"]["code"] == "workflow_transition_not_allowed"
        assert body_headers["content-type"].startswith("application/json")
        status, _headers, body = _request(
            child.port,
            "POST",
            "/api/proposals",
            headers={
                **_session_headers(cookie),
                "Origin": "https://evil.test",
                "Content-Type": "application/json",
            },
            body=b"{}",
        )
        assert status == 403
        assert json.loads(body)["error"]["code"] == "forbidden"

        escaped_media = project.parent / "escaped.mp4"
        escaped_media.write_bytes(b"outside project")
        current = ProjectStore(project).load()
        escaped = replace(
            current.sources[0],
            import_mode=ImportMode.COPIED,
            locator={"project_relative_path": "../escaped.mp4"},
        )
        ProjectStore(project).save(
            replace(current, sources=(escaped,)),
            expected_revision=current.revision,
        )
        assert _request(
            child.port,
            "GET",
            "/media/src_w5",
            headers=_session_headers(cookie),
        )[0] == 403
        assert media.exists()
    finally:
        child.stop()
    _assert_port_closed(child.port)


def test_w5_missing_registered_media_returns_a_sanitized_fail_closed_response(
    tmp_path: Path,
) -> None:
    project, _proposal_id, media = seed_artifact_review_project(
        tmp_path / "missing media 中文 project with spaces"
    )
    child = _ReviewCliChild.start(project)
    try:
        media.unlink()
        status, _headers, body = _request(
            child.port,
            "GET",
            "/media/src_w5",
            headers={"X-Roughcut-Token": child.token},
        )
        assert status == 403
        payload = json.loads(body)
        assert payload["error"] == {
            "code": "forbidden_media",
            "message": "registered media is unavailable",
        }
        serialized = json.dumps(payload, ensure_ascii=False)
        assert str(project) not in serialized
        assert str(media) not in serialized
    finally:
        child.stop()
    _assert_port_closed(child.port)


def test_w5_review_server_repeated_start_stop_expiry_and_failed_start_cleanup(
    tmp_path: Path,
) -> None:
    project, proposal_id, _media = seed_artifact_review_project(
        tmp_path / "重复启动 中文 project with spaces"
    )
    for _ in range(3):
        with start_review_server(project, proposal_id=proposal_id) as review:
            assert review.token not in repr(review)
            assert _request(review.port, "GET", "/api/review", headers={"X-Roughcut-Token": review.token})[0] == 200
            port = review.port
        assert review.thread.is_alive() is False
        _assert_port_closed(port)

    with start_review_server(
        project,
        proposal_id=proposal_id,
        token_ttl_seconds=1,
    ) as expiring:
        time.sleep(1.1)
        assert _request(
            expiring.port,
            "GET",
            "/api/review",
            headers={"X-Roughcut-Token": expiring.token},
        )[0] == 403

    missing_static = tmp_path / "missing static"
    with pytest.raises((OSError, ProjectError)):
        start_review_server(project, proposal_id=proposal_id, static_root=missing_static)
    assert not any(
        thread.name.startswith("roughcut-review-")
        for thread in threading.enumerate()
    )

    failed = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "review",
            str(project),
            "--run-id",
            "missing_run",
            "--json",
        ],
        cwd=CORE_ROOT,
        env=child_environment(),
        check=False,
        capture_output=True,
    )
    assert failed.returncode == 2
    assert failed.stderr == b""
    assert parse_json_bytes(failed.stdout.splitlines()[0])["error"] == {
        "code": "review_run_not_found"
    }


def test_w5_review_cli_uses_a_native_windows_drive_path_when_available(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("requires a real Windows drive-letter path")
    project, _proposal_id, _media = seed_artifact_review_project(
        tmp_path / "中文 native drive project with spaces"
    )
    assert project.drive
    assert ":" in str(project)
    child = _ReviewCliChild.start(project)
    try:
        status, _headers, body = _request(
            child.port,
            "GET",
            "/api/review",
            headers={"X-Roughcut-Token": child.token},
        )
        assert status == 200
        assert json.loads(body)["project"]["name"] == "Windows 中文 Review"
    finally:
        child.stop()
    _assert_port_closed(child.port)
