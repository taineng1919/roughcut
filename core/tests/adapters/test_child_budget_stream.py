"""Targeted run_bounded_child_stream semantics: success chunking, timeout,
memory ceiling, consumer error, and process-tree cleanup."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from roughcut.adapters.child_budget import (
    ChildBudget,
    ChildProcessBudgetError,
    ChildProcessMemoryBudgetError,
    ChildProcessTimeBudgetError,
    run_bounded_child_stream,
)


class _FixedDeadline:
    """Deadline protocol double with a configurable remaining budget."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def remaining(self) -> float:
        return self.seconds


class _CountdownDeadline:
    """Real-clock deadline that aborts like the shared producer deadline."""

    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        left = self.end - time.monotonic()
        if left <= 0:
            raise ChildProcessTimeBudgetError("synthetic stream deadline")
        return left


def _budget(seconds: float = 30.0, memory_bytes: int = 512 * 1024 * 1024):
    return ChildBudget(_FixedDeadline(seconds), memory_bytes)


def _python(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def test_stream_success_consumes_all_chunks_in_order(tmp_path: Path) -> None:
    script = (
        "import sys\n"
        "for round in range(4):\n"
        "    sys.stdout.buffer.write(bytes([round]) * 100_000)\n"
        "sys.stdout.flush()\n"
    )
    chunks: list[bytes] = []
    result = run_bounded_child_stream(
        _python(script),
        budget=_budget(),
        on_stdout=chunks.append,
        chunk_size=8_192,
    )
    assert result.returncode == 0
    assert b"".join(chunks) == (
        bytes([0]) * 100_000
        + bytes([1]) * 100_000
        + bytes([2]) * 100_000
        + bytes([3]) * 100_000
    )
    # every chunk respects the requested bound
    assert all(len(chunk) <= 8_192 for chunk in chunks)


def test_stream_deadline_abort_terminates_process_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    grandchild_file = tmp_path / "grandchild.pid"
    script = (
        "import os, sys, time\n"
        f"pid = {str(pid_file)!r}\n"
        f"gc = {str(grandchild_file)!r}\n"
        "open(pid, 'w').write(str(os.getpid()))\n"
        "child = os.fork() if hasattr(os, 'fork') else None\n"
        "if child == 0:\n"
        "    open(gc, 'w').write(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "    os._exit(0)\n"
        "sys.stdout.buffer.write(b'x' * 64)\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    chunks: list[bytes] = []
    with pytest.raises(ChildProcessTimeBudgetError):
        run_bounded_child_stream(
            _python(script),
            budget=ChildBudget(_CountdownDeadline(0.5), 512 * 1024 * 1024),
            on_stdout=chunks.append,
        )
    for record in (pid_file, grandchild_file):
        deadline = time.monotonic() + 5.0
        gone = False
        while time.monotonic() < deadline:
            if not record.exists():
                time.sleep(0.05)
                continue
            pid = int(record.read_text())
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                gone = True
                break
            time.sleep(0.05)
        assert gone, f"process from {record.name} survived the cleanup"


def test_stream_wait_timeout_after_eof_raises_time_budget(tmp_path: Path) -> None:
    script = (
        "import os, sys, time\n"
        "sys.stdout.buffer.write(b'y' * 32)\n"
        "sys.stdout.flush()\n"
        "os.close(1)\n"
        "time.sleep(30)\n"
    )
    chunks: list[bytes] = []
    with pytest.raises(ChildProcessTimeBudgetError):
        run_bounded_child_stream(
            _python(script),
            budget=_budget(seconds=0.5),
            on_stdout=chunks.append,
        )
    assert chunks == [b"y" * 32]


def test_stream_memory_ceiling_raises_and_cleans_up(tmp_path: Path) -> None:
    script = "import time\nfor _ in range(600):\n    time.sleep(0.1)\n"
    chunks: list[bytes] = []
    with pytest.raises(ChildProcessMemoryBudgetError):
        run_bounded_child_stream(
            _python(script),
            budget=_budget(seconds=30.0, memory_bytes=1),
            on_stdout=chunks.append,
        )


def test_stream_consumer_error_stops_child_and_propagates(tmp_path: Path) -> None:
    script = (
        "import sys, time\n"
        "for _ in range(600):\n"
        "    sys.stdout.buffer.write(b'x' * 1024)\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.05)\n"
    )
    consumed = 0

    def consumer(chunk: bytes) -> None:
        nonlocal consumed
        consumed += len(chunk)
        if consumed >= 2048:
            raise RuntimeError("synthetic consumer fault")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="synthetic consumer fault"):
        run_bounded_child_stream(
            _python(script),
            budget=_budget(),
            on_stdout=consumer,
        )
    assert time.monotonic() - started < 20.0
    assert consumed >= 2048


def test_stream_invalid_chunk_size_fails_closed() -> None:
    with pytest.raises(ChildProcessBudgetError):
        run_bounded_child_stream(
            _python("pass"),
            budget=_budget(),
            on_stdout=lambda chunk: None,
            chunk_size=0,
        )


def test_stream_missing_binary_fails_closed_without_orphan() -> None:
    with pytest.raises(ChildProcessBudgetError):
        run_bounded_child_stream(
            ["roughcut-definitely-missing-binary"],
            budget=_budget(),
            on_stdout=lambda chunk: None,
        )


def test_unlisted_cleanup_exception_does_not_replace_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roughcut.adapters import child_budget

    events: list[str] = []
    primary = RuntimeError("primary operation failure")

    class _Process:
        pid = 901

        def kill(self) -> None:
            events.append("kill")

        def wait(self) -> int:
            events.append("wait")
            return 1

    def fail_terminate(_pid: int) -> None:
        raise LookupError("unlisted cleanup failure")

    monkeypatch.setattr(child_budget, "_terminate_tree", fail_terminate)
    process = _Process()
    try:
        try:
            raise primary
        except BaseException as error:
            child_budget._cleanup_child(process, None, primary_error=error)  # type: ignore[arg-type]
            raise
    except RuntimeError as error:
        assert error is primary
    assert events == ["kill", "wait"]


def test_stream_reader_unlisted_exception_uses_reader_error_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roughcut.adapters import child_budget

    events: list[str] = []

    class _Stdout:
        def read(self, _size: int) -> bytes:
            raise LookupError("unlisted reader failure")

        def close(self) -> None:
            events.append("close")

    class _Process:
        pid = 902
        args = ("fixture-child",)

        def __init__(self) -> None:
            self.stdout = _Stdout()
            self.returncode = 1

        def wait(self) -> int:
            events.append("wait")
            return self.returncode

    process = _Process()

    def fake_popen(*_args: object, **_kwargs: object) -> _Process:
        return process

    monkeypatch.setattr(child_budget.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(child_budget, "_terminate_tree", lambda _pid: None)
    with pytest.raises(ChildProcessBudgetError) as error:
        run_bounded_child_stream(
            _python("pass"),
            budget=_budget(),
            on_stdout=lambda _chunk: None,
        )
    assert isinstance(error.value.__cause__, LookupError)
    assert events == ["wait", "close"]
