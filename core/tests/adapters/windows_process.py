"""Small real-process helpers for Windows adapter behavior tests.

This is a subprocess fixture, not a second adapter test suite.  The child
commands intentionally import and execute the production adapters directly.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path

_HELPER_PATH = Path(__file__).resolve()
_CORE_ROOT = _HELPER_PATH.parents[2]


class WindowsHelperProcess:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._lines: queue.Queue[str] = queue.Queue()
        self._reader = threading.Thread(target=self._read_one_line, daemon=True)
        self._reader.start()

    def _read_one_line(self) -> None:
        stdout = self.process.stdout
        if stdout is None:
            self._lines.put("")
            return
        self._lines.put(stdout.readline().rstrip("\r\n"))

    def poll_line(self, timeout: float) -> str | None:
        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def require_line(self, timeout: float = 15.0) -> str:
        line = self.poll_line(timeout)
        if line is None:
            stderr = ""
            if self.process.poll() is not None and self.process.stderr is not None:
                stderr = self.process.stderr.read()
            raise AssertionError(
                f"Windows helper did not report a line within {timeout}s; "
                f"returncode={self.process.poll()} stderr={stderr!r}"
            )
        return line

    def release(self) -> None:
        stdin = self.process.stdin
        if stdin is None:
            raise AssertionError("Windows helper stdin is unavailable")
        stdin.write("\n")
        stdin.flush()
        stdin.close()

    def finish(self, timeout: float = 15.0, *, check: bool = True) -> str:
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            self.process.terminate()
            self.process.wait(timeout=5)
            raise AssertionError(
                f"Windows helper did not exit within {timeout}s"
            ) from error
        self._reader.join(timeout=1)
        stdout = self.process.stdout.read() if self.process.stdout is not None else ""
        stderr = self.process.stderr.read() if self.process.stderr is not None else ""
        if check and self.process.returncode != 0:
            raise AssertionError(
                f"Windows helper failed with returncode={self.process.returncode}; "
                f"stdout_tail={stdout!r} stderr={stderr!r}"
            )
        return stderr

    def terminate_and_wait(self, timeout: float = 15.0) -> str:
        self.process.terminate()
        return self.finish(timeout=timeout, check=False)


def start_windows_helper(command: str, *arguments: str) -> WindowsHelperProcess:
    process = subprocess.Popen(
        [sys.executable, str(_HELPER_PATH), command, *arguments],
        cwd=_CORE_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    return WindowsHelperProcess(process)


def _hold_project_write_lock(root_value: str) -> None:
    from roughcut.adapters.project_lock import project_write_lock

    with project_write_lock(Path(root_value)):
        print("READY", flush=True)
        sys.stdin.readline()


def _enter_project_write_lock(root_value: str) -> None:
    from roughcut.adapters.project_lock import project_write_lock

    with project_write_lock(Path(root_value)):
        print("ENTERED", flush=True)


def _hold_export_claim(root_value: str) -> None:
    from roughcut.adapters.project_lock import project_export_claim

    with project_export_claim(Path(root_value)):
        print("READY", flush=True)
        sys.stdin.readline()


def _try_export_claim(root_value: str) -> None:
    from roughcut.adapters.project_lock import project_export_claim
    from roughcut.domain.errors import WorkflowError

    try:
        with project_export_claim(Path(root_value)):
            print("ACQUIRED", flush=True)
    except WorkflowError as error:
        print(f"ERROR:{error.code}", flush=True)


def _store_writer(
    store_kind: str,
    root_value: str,
    identifier: str,
    operation_id: str,
    create: bool,
    hold: bool,
) -> None:
    if store_kind == "media":
        from roughcut.adapters.media_operation_store import MediaOperationStore

        writer = MediaOperationStore(Path(root_value), identifier).writer(
            operation_id, create=create
        )
    elif store_kind == "installation":
        from roughcut.adapters.installation_operation_store import (
            InstallationOperationStore,
        )

        writer = InstallationOperationStore(Path(root_value)).writer(
            operation_id, create=create
        )
    else:
        raise ValueError(f"unknown store kind: {store_kind}")

    with writer as acquired:
        print("ACQUIRED" if acquired else "NOT_ACQUIRED", flush=True)
        if acquired and hold:
            sys.stdin.readline()


def _main() -> None:
    command = sys.argv[1]
    arguments = sys.argv[2:]
    if command == "project-hold":
        _hold_project_write_lock(arguments[0])
    elif command == "project-enter":
        _enter_project_write_lock(arguments[0])
    elif command == "export-hold":
        _hold_export_claim(arguments[0])
    elif command == "export-try":
        _try_export_claim(arguments[0])
    elif command == "store-hold":
        _store_writer(
            arguments[0],
            arguments[1],
            arguments[2],
            arguments[3],
            arguments[4] == "create",
            True,
        )
    elif command == "store-try":
        _store_writer(
            arguments[0],
            arguments[1],
            arguments[2],
            arguments[3],
            False,
            False,
        )
    else:
        raise ValueError(f"unknown helper command: {command}")


if __name__ == "__main__":
    _main()
