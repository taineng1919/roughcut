"""Bounded real-process and HTTP helpers for Windows Review tests."""

from __future__ import annotations

import http.client
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from windows.w4_support import CORE_ROOT, child_environment, parse_json_bytes
from windows.w5_review_support import RUN_ID


class ReviewCliChild:
    """A real CLI Review child whose startup URL is published by production code."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        url: str,
        token: str,
        port: int,
        reader: threading.Thread,
    ) -> None:
        self.process = process
        self.url = url
        self.token = token
        self.port = port
        self._reader = reader

    @classmethod
    def start(cls, project: Path, *, run_id: str = RUN_ID) -> ReviewCliChild:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "roughcut.cli",
                "review",
                str(project),
                "--run-id",
                run_id,
                "--json",
            ],
            cwd=CORE_ROOT,
            env=child_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
        )
        lines: queue.Queue[bytes | None] = queue.Queue()

        def read_stdout() -> None:
            stdout = process.stdout
            if stdout is None:
                lines.put(None)
                return
            line = stdout.readline()
            lines.put(line if line else None)

        reader = threading.Thread(target=read_stdout, name="w5-review-cli-reader", daemon=True)
        reader.start()
        try:
            line = lines.get(timeout=15)
        except queue.Empty as error:
            _stop_process(process)
            raise AssertionError("Review CLI did not publish startup JSON") from error
        if line is None:
            _stop_process(process)
            raise AssertionError("Review CLI exited before publishing startup JSON")
        payload = parse_json_bytes(line.rstrip(b"\r\n"))
        review = payload.get("review")
        if not isinstance(review, dict):
            _stop_process(process)
            raise TypeError("Review CLI startup envelope omitted review metadata")
        url = review.get("url")
        if not isinstance(url, str):
            _stop_process(process)
            raise TypeError("Review CLI startup envelope omitted review URL")
        parsed = urlsplit(url)
        if parsed.hostname != "127.0.0.1" or parsed.port is None:
            _stop_process(process)
            raise AssertionError("Review CLI did not publish a loopback URL")
        tokens = parse_qs(parsed.query).get("token", [])
        if len(tokens) != 1:
            _stop_process(process)
            raise AssertionError("Review CLI startup URL did not contain one token")
        return cls(
            process,
            url=url,
            token=tokens[0],
            port=parsed.port,
            reader=reader,
        )

    def stop(self) -> None:
        _stop_process(self.process)
        self._reader.join(timeout=2)
        assert self.process.poll() is not None
        if self.process.stderr is not None:
            self.process.stderr.read()


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait(timeout=5)
        raise AssertionError("Review CLI process did not stop") from error


def review_request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return (
            response.status,
            {key.lower(): value for key, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()


def session_cookie(headers: dict[str, str]) -> str:
    value = headers.get("set-cookie")
    if value is None:
        raise AssertionError("Review index did not set a session cookie")
    return value.split(";", 1)[0]


def assert_port_closed(port: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.05)
    raise AssertionError("Review server listener remained open after shutdown")
