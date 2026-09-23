"""Bounded child-process runner: deadline, memory ceiling, tree kill, wait."""

from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class _RemainingProtocol(Protocol):
    def remaining(self) -> float: ...


class ChildProcessBudgetError(RuntimeError):
    """Raised when a child process exceeds the operation budget."""


class ChildProcessTimeBudgetError(ChildProcessBudgetError):
    """Raised when a child exceeds the alignment wall-time budget."""


class ChildProcessMemoryBudgetError(ChildProcessBudgetError):
    """Raised when a child exceeds the alignment memory budget."""


def _capture_base_exception(operation: Callable[[], object]) -> BaseException | None:
    """Capture every failure at an ownership/cleanup boundary."""
    try:
        operation()
    except BaseException as error:  # noqa: BLE001 - cleanup must cover all failures
        return error
    return None


def _capture_exception(operation: Callable[[], object]) -> Exception | None:
    """Capture every ordinary stream-reader failure without catching exits."""
    try:
        operation()
    except Exception as error:  # noqa: BLE001 - the reader boundary is an Exception envelope
        return error
    return None


def _controlled_environment(
    base: dict[str, str] | None,
    budget: ChildBudget,
) -> dict[str, str]:
    """One controlled environment copy for every alignment child.

    The caller may or may not pass an environment; both cases start from a
    controlled copy (PYTHON* stripped) and always apply the workspace TMPDIR
    constraint when it is configured.
    """
    environment = dict(base) if base is not None else dict(os.environ)
    environment = {
        name: value
        for name, value in environment.items()
        if not name.startswith("PYTHON")
    }
    if budget.apply_tmpdir is not None:
        environment = budget.apply_tmpdir(environment)
    return environment


def _macos_process_rusage_bytes(pid: int) -> int:
    """Current resident bytes of one macOS process via libproc."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    proc_pid_rusage = libc.proc_pid_rusage
    proc_pid_rusage.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    proc_pid_rusage.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(1024)
    # RUSAGE_INFO_V0: ri_resident_size sits at byte offset 64
    if proc_pid_rusage(pid, 1, buffer) != 0:
        return 0
    return max(0, int.from_bytes(buffer.raw[64:72], "little"))


def _macos_child_pids(pid: int) -> list[int]:
    """Direct children of one macOS process via proc_listchildpids."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    proc_listchildpids = libc.proc_listchildpids
    proc_listchildpids.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
    proc_listchildpids.restype = ctypes.c_int
    count = proc_listchildpids(pid, None, 0)
    if count <= 0:
        return []
    buffer = (ctypes.c_int * count)()
    written = proc_listchildpids(pid, buffer, count * ctypes.sizeof(ctypes.c_int))
    if written <= 0:
        return []
    return [int(item) for item in buffer[: min(written, count)]]


def _macos_tree_bytes(pid: int) -> int:
    """Recursive resident bytes of one macOS process and its descendants."""
    total = _macos_process_rusage_bytes(pid)
    for child in _macos_child_pids(pid):
        total += _macos_tree_bytes(child)
    return total


def _tree_bytes(pid: int) -> int:
    if sys.platform == "darwin":
        return _macos_tree_bytes(pid)
    return 0


class _WindowsBasicAccounting(ctypes.Structure):
    """JOBOBJECT_BASIC_ACCOUNTING_INFORMATION as a plain ctypes structure.

    Fields in exact ABI order: four c_int64 time fields, then the page-fault
    count and the three process counters. Reads go through byref(info) +
    sizeof(info); no hardcoded byte offsets are ever used.
    """

    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _WindowsExtendedLimit(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION as a plain ctypes structure.

    Hoisted to module level so the fault-injection tests can construct it
    without a real kernel32 handle.
    """

    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
        ("IoReadBytesLimit", ctypes.c_uint64),
        ("IoWriteBytesLimit", ctypes.c_uint64),
        ("IoOtherBytesLimit", ctypes.c_uint64),
        ("IoReadOperationLimit", ctypes.c_uint64),
        ("IoWriteOperationLimit", ctypes.c_uint64),
        ("IoOtherOperationLimit", ctypes.c_uint64),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsAssociateCompletionPort(ctypes.Structure):
    """JOBOBJECT_ASSOCIATE_COMPLETION_PORT for one private Job port."""

    _fields_ = [
        ("CompletionKey", ctypes.c_void_p),
        ("CompletionPort", ctypes.c_void_p),
    ]


class _WindowsThreadEntry(ctypes.Structure):
    """THREADENTRY32 with fixed-width Win32 field types."""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32),
        ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    ]


class _WindowsJob:
    """One per-child Job Object: job-wide frozen memory limit, kill-on-close.

    The child is created suspended, assigned before its unique initial thread
    is resumed, and then remains under the native job-wide memory ceiling.
    Every communicate round checks both completion-port and peak-memory
    evidence. Verified drain and kill-on-close containment use distinct Job
    and remaining-handle close operations.
    """

    _LIMIT_JOB_MEMORY = 0x00000200
    _LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _INFO_BASIC_ACCOUNTING = 1
    _INFO_ASSOCIATE_COMPLETION_PORT = 7
    _INFO_EXTENDED_LIMIT = 9
    _JOB_OBJECT_MSG_JOB_MEMORY_LIMIT = 10
    _ERROR_NO_MORE_FILES = 18
    _ERROR_TIMEOUT = 258
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(self, memory_limit: int) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self._kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        self._kernel32.CreateJobObjectW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self._kernel32.OpenProcess.restype = ctypes.c_void_p
        self._kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        self._kernel32.AssignProcessToJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = (
            ctypes.c_void_p,
            wintypes.UINT,
        )
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.SetInformationJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.QueryInformationJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel32.CreateIoCompletionPort.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            wintypes.DWORD,
        )
        self._kernel32.CreateIoCompletionPort.restype = ctypes.c_void_p
        self._kernel32.GetQueuedCompletionStatus.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
        )
        self._kernel32.GetQueuedCompletionStatus.restype = wintypes.BOOL
        self._kernel32.CreateToolhelp32Snapshot.argtypes = (
            wintypes.DWORD,
            wintypes.DWORD,
        )
        self._kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        self._kernel32.Thread32First.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_WindowsThreadEntry),
        )
        self._kernel32.Thread32First.restype = wintypes.BOOL
        self._kernel32.Thread32Next.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_WindowsThreadEntry),
        )
        self._kernel32.Thread32Next.restype = wintypes.BOOL
        self._kernel32.OpenThread.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        self._kernel32.OpenThread.restype = ctypes.c_void_p
        self._kernel32.ResumeThread.argtypes = (ctypes.c_void_p,)
        self._kernel32.ResumeThread.restype = wintypes.DWORD
        self._kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._thread: int | None = None
        self._completion_port: int | None = None
        self._completion_key: int | None = None
        self._assigned = False
        self._emergency_containment_occurred = False
        self._memory_limit_message_received = False
        job_handle = self._kernel32.CreateJobObjectW(None, None)
        self._job: int | None = int(job_handle) if job_handle else None
        if self._job is None:
            raise ChildProcessBudgetError("alignment child job could not be created")

        self._extended = _WindowsExtendedLimit
        info = self._extended()
        info.LimitFlags = self._LIMIT_JOB_MEMORY | self._LIMIT_KILL_ON_JOB_CLOSE
        info.JobMemoryLimit = memory_limit
        if not self._kernel32.SetInformationJobObject(
            self._job,
            self._INFO_EXTENDED_LIMIT,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = ChildProcessBudgetError(
                "alignment child job limits could not be set"
            )
            self._close_after_constructor_failure(error)
            raise error

        invalid_handle = ctypes.c_void_p(self._INVALID_HANDLE_VALUE)
        completion_port = self._kernel32.CreateIoCompletionPort(
            invalid_handle,
            None,
            0,
            1,
        )
        if not completion_port:
            error = ChildProcessBudgetError(
                "alignment child job completion port could not be created"
            )
            self._close_after_constructor_failure(error)
            raise error
        self._completion_port = int(completion_port)
        self._completion_key = int(self._job)
        association = _WindowsAssociateCompletionPort(
            ctypes.c_void_p(self._completion_key),
            ctypes.c_void_p(self._completion_port),
        )
        if not self._kernel32.SetInformationJobObject(
            self._job,
            self._INFO_ASSOCIATE_COMPLETION_PORT,
            ctypes.byref(association),
            ctypes.sizeof(association),
        ):
            error = ChildProcessBudgetError(
                "alignment child job completion port could not be associated"
            )
            self._close_after_constructor_failure(error)
            raise error

    @property
    def assigned(self) -> bool:
        return self._assigned

    @property
    def emergency_containment_occurred(self) -> bool:
        return self._emergency_containment_occurred

    def _last_error(self) -> int:
        return int(self._ctypes.get_last_error())  # type: ignore[attr-defined]

    def _close_after_constructor_failure(self, primary_error: BaseException) -> None:
        close_error = _capture_base_exception(self.close)
        if close_error is not None:
            raise close_error from primary_error

    def assign(self, pid: int) -> None:
        handle = self._kernel32.OpenProcess(0x0100 | 0x0001, False, pid)
        if not handle:
            raise ChildProcessBudgetError(
                "alignment child could not be opened for its job"
            )

        def assign_process() -> None:
            if not self._kernel32.AssignProcessToJobObject(self._job, handle):
                raise ChildProcessBudgetError(
                    "alignment child could not join its job"
                )
            self._assigned = True

        primary_error = _capture_base_exception(assign_process)

        def close_process_handle() -> None:
            if not self._kernel32.CloseHandle(handle):
                raise ChildProcessBudgetError(
                    "alignment child process handle could not be closed"
                )

        close_error = _capture_base_exception(close_process_handle)
        if close_error is not None:
            if primary_error is not None:
                raise close_error from primary_error
            raise close_error
        if primary_error is not None:
            raise primary_error

    def resume_initial_thread(self, pid: int) -> None:
        """Resume the one CREATE_SUSPENDED root thread found via Toolhelp."""
        snapshot = self._kernel32.CreateToolhelp32Snapshot(
            self._TH32CS_SNAPTHREAD,
            0,
        )
        if not snapshot or int(snapshot) == self._INVALID_HANDLE_VALUE:
            raise ChildProcessBudgetError(
                "alignment child initial thread could not be enumerated"
            )
        thread_ids: list[int] = []

        def enumerate_threads() -> None:
            entry = _WindowsThreadEntry()
            entry.dwSize = self._ctypes.sizeof(entry)
            if not self._kernel32.Thread32First(snapshot, self._ctypes.byref(entry)):
                raise ChildProcessBudgetError(
                    "alignment child initial thread could not be enumerated"
                )
            while True:
                if int(entry.th32OwnerProcessID) == pid:
                    thread_ids.append(int(entry.th32ThreadID))
                entry.dwSize = self._ctypes.sizeof(entry)
                if self._kernel32.Thread32Next(snapshot, self._ctypes.byref(entry)):
                    continue
                if self._last_error() != self._ERROR_NO_MORE_FILES:
                    raise ChildProcessBudgetError(
                        "alignment child initial thread could not be enumerated"
                    )
                break

        primary_error = _capture_base_exception(enumerate_threads)

        def close_snapshot() -> None:
            if not self._kernel32.CloseHandle(snapshot):
                raise ChildProcessBudgetError(
                    "alignment child thread snapshot could not be closed"
                )

        close_error = _capture_base_exception(close_snapshot)
        if close_error is not None:
            if primary_error is not None:
                raise close_error from primary_error
            raise close_error
        if primary_error is not None:
            raise primary_error
        if len(thread_ids) != 1:
            raise ChildProcessBudgetError(
                "alignment child initial thread identity was not unique"
            )
        thread = self._kernel32.OpenThread(
            self._THREAD_SUSPEND_RESUME,
            False,
            thread_ids[0],
        )
        if not thread:
            raise ChildProcessBudgetError(
                "alignment child initial thread could not be opened"
            )
        self._thread = int(thread)
        previous_suspend_count = int(self._kernel32.ResumeThread(thread))
        if previous_suspend_count != 1:
            raise ChildProcessBudgetError(
                "alignment child initial thread could not be resumed"
            )

    def active_processes(self) -> int:
        from ctypes import wintypes

        info = _WindowsBasicAccounting()
        returned = wintypes.DWORD()
        job_handle = self._require_job_handle()
        if not self._kernel32.QueryInformationJobObject(
            job_handle,
            self._INFO_BASIC_ACCOUNTING,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
            self._ctypes.byref(returned),
        ):
            # a query failure must never masquerade as "no active processes"
            raise ChildProcessBudgetError(
                "alignment child job accounting could not be queried"
            )
        return int(info.ActiveProcesses)

    def memory_limit_exceeded(self, limit: int) -> bool:
        """Return only positive Job message or exact peak memory evidence."""
        message_received = False
        peak: int | None = None
        query_errors: list[ChildProcessBudgetError] = []
        try:
            message_received = self.drain_notifications()
        except ChildProcessBudgetError as error:
            query_errors.append(error)
        try:
            peak = self.peak_job_memory_used()
        except ChildProcessBudgetError as error:
            query_errors.append(error)

        if message_received or (peak is not None and peak >= limit):
            return True
        if query_errors:
            primary_error = query_errors[0]
            for additional_error in query_errors[1:]:
                primary_error.add_note(
                    f"additional memory evidence query failure: {additional_error}"
                )
            raise primary_error
        return False

    def drain_notifications(self) -> bool:
        """Non-blockingly drain this Job's completion packets."""
        from ctypes import wintypes

        if self._completion_port is None or self._completion_key is None:
            raise ChildProcessBudgetError(
                "alignment child job completion port is unavailable"
            )
        while True:
            message = wintypes.DWORD()
            key = ctypes.c_size_t()
            overlapped = ctypes.c_void_p()
            if not self._kernel32.GetQueuedCompletionStatus(
                self._completion_port,
                self._ctypes.byref(message),
                self._ctypes.byref(key),
                self._ctypes.byref(overlapped),
                0,
            ):
                if self._last_error() == self._ERROR_TIMEOUT and not overlapped.value:
                    break
                raise ChildProcessBudgetError(
                    "alignment child job completion port could not be queried"
                )
            if int(key.value) != self._completion_key:
                raise ChildProcessBudgetError(
                    "alignment child job completion packet was invalid"
                )
            if int(message.value) == self._JOB_OBJECT_MSG_JOB_MEMORY_LIMIT:
                self._memory_limit_message_received = True
        return self._memory_limit_message_received

    def terminate(self) -> None:
        if not self._kernel32.TerminateJobObject(self._require_job_handle(), 1):
            raise ChildProcessBudgetError(
                "alignment child job could not be terminated"
            )

    def verify_empty(self) -> None:
        """Require one exact class-1 observation after the root was waited."""
        if self.active_processes() != 0:
            raise ChildProcessBudgetError(
                "alignment child job still had active processes after root wait"
            )

    def peak_job_memory_used(self) -> int:
        from ctypes import wintypes

        info = self._extended()
        returned = wintypes.DWORD()
        job_handle = self._require_job_handle()
        if not self._kernel32.QueryInformationJobObject(
            job_handle,
            self._INFO_EXTENDED_LIMIT,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
            self._ctypes.byref(returned),
        ):
            raise ChildProcessBudgetError(
                "alignment child job memory could not be queried"
            )
        return int(info.PeakJobMemoryUsed)

    def peak_process_memory_used(self) -> int:
        from ctypes import wintypes

        info = self._extended()
        returned = wintypes.DWORD()
        job_handle = self._require_job_handle()
        if not self._kernel32.QueryInformationJobObject(
            job_handle,
            self._INFO_EXTENDED_LIMIT,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
            self._ctypes.byref(returned),
        ):
            raise ChildProcessBudgetError(
                "alignment child job memory could not be queried"
            )
        return int(info.PeakProcessMemoryUsed)

    def _require_job_handle(self) -> int:
        if self._job is None:
            raise ChildProcessBudgetError("alignment child job handle is unavailable")
        return self._job

    def _close_owned_handle(self, attribute: str, evidence: str) -> None:
        handle = getattr(self, attribute, None)
        if handle is None:
            return
        try:
            if not self._kernel32.CloseHandle(handle):
                raise ChildProcessBudgetError(
                    f"alignment child {evidence} handle could not be closed"
                )
        finally:
            # A failed CloseHandle leaves ownership indeterminate. Do not
            # retry or risk a later double-close; surface the failure instead.
            setattr(self, attribute, None)

    @staticmethod
    def _raise_close_errors(errors: list[BaseException]) -> None:
        if not errors:
            return
        first_error = errors[0]
        for later_error in errors[1:]:
            first_error.add_note(f"additional handle close failure: {later_error}")
        raise first_error

    def close_job_handle(self) -> None:
        """Close only the owned Job handle and check the kernel result."""
        self._close_owned_handle("_job", "job")

    def close_job_for_emergency(self) -> None:
        """Trigger kill-on-close and retain that containment fact."""
        self.close_job_handle()
        self._emergency_containment_occurred = True

    def close_remaining_handles(self) -> None:
        """Attempt every non-Job long-lived close in frozen order."""
        errors: list[BaseException] = []
        for attribute, evidence in (
            ("_completion_port", "completion port"),
            ("_thread", "initial thread"),
        ):
            def close_handle(attribute: str = attribute, evidence: str = evidence) -> None:
                self._close_owned_handle(attribute, evidence)

            error = _capture_base_exception(close_handle)
            if error is not None:
                errors.append(error)
        self._raise_close_errors(errors)

    def close(self) -> None:
        """Close an unassigned/constructor Job, then all remaining handles."""
        errors: list[BaseException] = []
        error = _capture_base_exception(self.close_job_handle)
        if error is not None:
            errors.append(error)
        error = _capture_base_exception(self.close_remaining_handles)
        if error is not None:
            errors.append(error)
        self._raise_close_errors(errors)


@dataclass(frozen=True)
class ChildBudget:
    """One shared deadline and memory ceiling for alignment children."""

    deadline: _RemainingProtocol
    max_memory_bytes: int
    apply_tmpdir: Callable[[dict[str, str]], dict[str, str]] | None = None

    def timeout_seconds(self) -> float:
        return float(self.deadline.remaining())

    def memory_over(self, pid: int) -> bool:
        # Windows enforces the ceiling natively through the Job Object
        if self.max_memory_bytes <= 0 or sys.platform == "win32":
            return False
        return _tree_bytes(pid) > self.max_memory_bytes


def run_bounded_child(
    command: Sequence[str],
    *,
    budget: ChildBudget,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run one child under the shared deadline and memory ceiling.

    The child runs in its own session/process group (POSIX) or inside a
    per-child Job Object (Windows). Each loop round re-reads the remaining
    deadline and the full process-tree resident memory while the child is
    alive, with a 50 ms communicate cycle; it never blocks to completion
    before checking the budget. On timeout, memory overrun, SystemExit,
    KeyboardInterrupt, or a deadline exception, the runner completes either
    verified Job drain or emergency kill-on-close containment before return,
    so the caller cannot release its writer lock while cleanup is in flight.
    """
    from roughcut.adapters.media_operation_store import media_child_process_kwargs

    kwargs = dict(kwargs)
    kwargs.pop("timeout", None)
    kwargs.pop("check", None)
    kwargs.pop("capture_output", None)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    kwargs.update(media_child_process_kwargs())
    environment = _controlled_environment(kwargs.pop("env", None), budget)
    popen_kwargs: dict[str, Any] = dict(kwargs)
    popen_kwargs["env"] = environment
    if sys.platform == "win32":
        creation_flags = int(popen_kwargs.pop("creationflags", 0))
        # CREATE_SUSPENDED preserves subprocess command/cwd/env/pipe semantics
        # while preventing the target from executing before Job assignment.
        popen_kwargs["creationflags"] = creation_flags | 0x00000004
    else:
        popen_kwargs.setdefault("start_new_session", True)
    job: _WindowsJob | None = None
    try:
        if sys.platform == "win32":
            job = _WindowsJob(budget.max_memory_bytes)
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
    except (OSError, ValueError) as error:
        if job is not None:
            try:
                job.close()
            except BaseException as close_error:
                raise close_error from error
        raise ChildProcessBudgetError("alignment child could not start") from error
    if job is not None:
        try:
            job.assign(process.pid)
            job.resume_initial_thread(process.pid)
        except BaseException as error:
            _cleanup_child(process, job, primary_error=error)
            raise

    collected_out: list[str] = []
    collected_err: list[str] = []
    try:
        while True:
            remaining = budget.timeout_seconds()
            if remaining <= 0:
                raise ChildProcessTimeBudgetError(
                    "alignment child time budget exceeded"
                )
            if sys.platform != "win32" and budget.memory_over(process.pid):
                raise ChildProcessMemoryBudgetError(
                    "alignment child memory budget exceeded"
                )
            if sys.platform == "win32" and job is not None:
                if job.memory_limit_exceeded(budget.max_memory_bytes):
                    raise ChildProcessMemoryBudgetError(
                        "alignment child memory budget exceeded"
                    )
            try:
                chunk_out, chunk_err = process.communicate(
                    timeout=min(remaining, 0.05)
                )
            except subprocess.TimeoutExpired:
                continue
            collected_out.append(chunk_out)
            collected_err.append(chunk_err)
            break
        if sys.platform == "win32" and job is not None:
            # A final post-exit drain+peak query closes the race between the
            # last communicate timeout and root termination. Missing messages
            # remain neutral; only an observed memory packet or exact peak is
            # positive memory evidence.
            if job.memory_limit_exceeded(budget.max_memory_bytes):
                raise ChildProcessMemoryBudgetError(
                    "alignment child memory budget exceeded"
                )
    except BaseException as error:
        _cleanup_child(process, job, primary_error=error)
        raise
    if job is not None:
        _cleanup_child(process, job)
    return subprocess.CompletedProcess(
        process.args,
        process.returncode,
        "".join(collected_out),
        "".join(collected_err),
    )


def run_bounded_child_stream(
    command: Sequence[str],
    *,
    budget: ChildBudget,
    on_stdout: Callable[[bytes], None],
    chunk_size: int = 65_536,
) -> subprocess.CompletedProcess[bytes]:
    """Run one bounded child while consuming binary stdout incrementally.

    The reader thread is deliberately queue-bounded: a long low-rate FFmpeg
    stream cannot turn into an unbounded parent-side buffer. The same process
    tree, deadline, memory ceiling, Job Object and cleanup paths as
    ``run_bounded_child`` are used.
    """
    from roughcut.adapters.media_operation_store import media_child_process_kwargs

    if chunk_size <= 0:
        raise ChildProcessBudgetError("alignment stream chunk size is invalid")
    kwargs: dict[str, Any] = dict(media_child_process_kwargs())
    environment = _controlled_environment(None, budget)
    kwargs["env"] = environment
    kwargs["stdin"] = subprocess.DEVNULL
    kwargs["stdout"] = subprocess.PIPE
    kwargs["stderr"] = subprocess.DEVNULL
    if sys.platform == "win32":
        kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | 0x00000004
    else:
        kwargs.setdefault("start_new_session", True)
    job: _WindowsJob | None = None
    try:
        if sys.platform == "win32":
            job = _WindowsJob(budget.max_memory_bytes)
        process = subprocess.Popen(list(command), **kwargs)
    except (OSError, ValueError) as error:
        if job is not None:
            try:
                job.close()
            except BaseException as close_error:
                raise close_error from error
        raise ChildProcessBudgetError("alignment child could not start") from error
    if job is not None:
        try:
            job.assign(process.pid)
            job.resume_initial_thread(process.pid)
        except BaseException as error:
            _cleanup_child(process, job, primary_error=error)
            raise

    events: queue.Queue[tuple[str, bytes | BaseException | None]] = queue.Queue(maxsize=4)
    stdout = process.stdout
    if stdout is None:
        _cleanup_child(
            process,
            job,
            primary_error=ChildProcessBudgetError("alignment child stdout is unavailable"),
        )
        raise ChildProcessBudgetError("alignment child stdout is unavailable")

    def read_stdout() -> None:
        def read_chunks() -> None:
            while True:
                chunk = stdout.read(chunk_size)
                if not chunk:
                    events.put(("eof", None))
                    return
                events.put(("chunk", chunk))
        error = _capture_exception(read_chunks)
        if error is not None:
            events.put(("error", error))

    reader = threading.Thread(target=read_stdout, name="roughcut-alignment-stream")
    reader.daemon = True
    reader.start()
    primary_error: BaseException | None = None
    try:
        eof = False
        while not eof:
            remaining = budget.timeout_seconds()
            if sys.platform != "win32" and budget.memory_over(process.pid):
                raise ChildProcessMemoryBudgetError(
                    "alignment child memory budget exceeded"
                )
            if sys.platform == "win32" and job is not None:
                if job.memory_limit_exceeded(budget.max_memory_bytes):
                    raise ChildProcessMemoryBudgetError(
                        "alignment child memory budget exceeded"
                    )
            try:
                kind, payload = events.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if kind == "chunk":
                if not isinstance(payload, bytes):
                    raise ChildProcessBudgetError("alignment stream event is invalid")
                on_stdout(payload)
            elif kind == "error":
                if isinstance(payload, BaseException):
                    raise ChildProcessBudgetError(
                        "alignment child stream could not be read"
                    ) from payload
                raise ChildProcessBudgetError("alignment child stream failed")
            else:
                eof = True
        try:
            process.wait(timeout=budget.timeout_seconds())
        except subprocess.TimeoutExpired as error:
            raise ChildProcessTimeBudgetError(
                "alignment child time budget exceeded"
            ) from error
        if sys.platform == "win32" and job is not None:
            if job.memory_limit_exceeded(budget.max_memory_bytes):
                raise ChildProcessMemoryBudgetError(
                    "alignment child memory budget exceeded"
                )
        return_code = process.returncode
        if job is not None:
            _cleanup_child(process, job)
        else:
            _cleanup_child(process, None)
        return subprocess.CompletedProcess(process.args, return_code, b"", b"")
    except BaseException as error:
        primary_error = error
        _cleanup_child(process, job, primary_error=error)
        raise
    finally:
        try:
            stdout.close()
        except OSError:
            pass
        # Draining the bounded queue lets a reader blocked on queue.put finish
        # after the process tree has been closed.
        while reader.is_alive():
            try:
                events.get(timeout=0.05)
            except queue.Empty:
                pass
        reader.join(timeout=1.0)
        if primary_error is None and reader.is_alive():
            raise ChildProcessBudgetError("alignment stream reader did not clean up")


def _cleanup_child(
    process: subprocess.Popen[str],
    job: _WindowsJob | None,
    *,
    primary_error: BaseException | None = None,
) -> None:
    """Finish child ownership before the caller can release its writer lock."""
    if job is not None:
        _cleanup_windows_child(process, job, primary_error=primary_error)
        return

    cleanup_error: BaseException | None = None
    error = _capture_base_exception(lambda: _terminate_tree(process.pid))
    if error is not None:
        cleanup_error = error
        _capture_base_exception(process.kill)
    wait_error = _capture_base_exception(lambda: process.wait())
    if cleanup_error is None and wait_error is not None:
        cleanup_error = wait_error
    if primary_error is None and cleanup_error is not None:
        raise cleanup_error


def _cleanup_windows_child(
    process: subprocess.Popen[str],
    job: _WindowsJob,
    *,
    primary_error: BaseException | None,
) -> None:
    """Use verified drain, or kill-on-close when kernel control fails."""
    if not job.assigned:
        _cleanup_unassigned_windows_child(
            process,
            job,
            primary_error=primary_error,
        )
        return

    active_processes_holder: list[int] = []

    def read_active_processes() -> None:
        active_processes_holder.append(job.active_processes())

    control_error = _capture_base_exception(read_active_processes)
    if control_error is not None:
        _emergency_contain_windows_child(
            process,
            job,
            primary_error=primary_error,
            control_error=control_error,
        )
        return
    active_processes = active_processes_holder[0]

    root_is_active = process.poll() is None
    if root_is_active or active_processes > 0:
        control_error = _capture_base_exception(job.terminate)
        if control_error is not None:
            _emergency_contain_windows_child(
                process,
                job,
                primary_error=primary_error,
                control_error=control_error,
            )
            return

    wait_error = _capture_base_exception(lambda: process.wait())
    if wait_error is not None:
        _emergency_contain_windows_child(
            process,
            job,
            primary_error=primary_error,
            control_error=wait_error,
        )
        return

    control_error = _capture_base_exception(job.verify_empty)
    if control_error is not None:
        _emergency_contain_windows_child(
            process,
            job,
            primary_error=primary_error,
            control_error=control_error,
        )
        return

    close_errors: list[BaseException] = []
    close_error = _capture_base_exception(job.close_job_handle)
    if close_error is not None:
        close_errors.append(close_error)
    close_error = _capture_base_exception(job.close_remaining_handles)
    if close_error is not None:
        close_errors.append(close_error)
    _raise_cleanup_errors(close_errors, primary_error=primary_error)


def _cleanup_unassigned_windows_child(
    process: subprocess.Popen[str],
    job: _WindowsJob,
    *,
    primary_error: BaseException | None,
) -> None:
    """The suspended target never joined the Job; clean only owned handles."""
    cleanup_errors: list[BaseException] = []
    error = _capture_base_exception(process.kill)
    if error is not None:
        cleanup_errors.append(error)
    error = _capture_base_exception(lambda: process.wait())
    if error is not None:
        cleanup_errors.append(error)
    error = _capture_base_exception(job.close_job_handle)
    if error is not None:
        cleanup_errors.append(error)
    error = _capture_base_exception(job.close_remaining_handles)
    if error is not None:
        cleanup_errors.append(error)
    _raise_cleanup_errors(cleanup_errors, primary_error=primary_error)


def _emergency_contain_windows_child(
    process: subprocess.Popen[str],
    job: _WindowsJob,
    *,
    primary_error: BaseException | None,
    control_error: BaseException,
) -> None:
    """Close the kill-on-close Job first, then wait and close the rest."""
    cleanup_errors: list[BaseException] = []
    job_close_error = _capture_base_exception(job.close_job_for_emergency)
    if job_close_error is not None:
        cleanup_errors.append(job_close_error)
        # Containment was not established. A root-only kill is best effort and
        # is never reported as Job-tree containment.
        kill_error = _capture_base_exception(process.kill)
        if kill_error is not None:
            cleanup_errors.append(kill_error)
    wait_error = _capture_base_exception(lambda: process.wait())
    if wait_error is not None:
        cleanup_errors.append(wait_error)
    close_error = _capture_base_exception(job.close_remaining_handles)
    if close_error is not None:
        cleanup_errors.append(close_error)
    _raise_cleanup_errors(
        cleanup_errors,
        primary_error=primary_error or control_error,
    )
    if primary_error is None:
        raise control_error


def _raise_cleanup_errors(
    errors: list[BaseException],
    *,
    primary_error: BaseException | None,
) -> None:
    if not errors:
        return
    first_error = errors[0]
    for later_error in errors[1:]:
        first_error.add_note(f"additional cleanup failure: {later_error}")
    if primary_error is not None:
        raise first_error from primary_error
    raise first_error


def _terminate_tree(pid: int, job: _WindowsJob | None = None) -> None:
    if job is not None:
        job.terminate()
        return
    if sys.platform != "win32":
        # the child lives in its own process group; kill the whole group
        for signal in (getattr(os, "SIGTERM", 15), getattr(os, "SIGKILL", 9)):
            try:
                os.killpg(pid, signal)
            except (ProcessLookupError, OSError):
                pass
        _confirm_no_descendants(pid)
        return
    try:
        os.kill(pid, getattr(os, "SIGTERM", 15))
    except (ProcessLookupError, OSError):
        pass
    try:
        os.kill(pid, getattr(os, "SIGKILL", 9))
    except (ProcessLookupError, OSError):
        pass


def _confirm_no_descendants(pid: int) -> None:
    if sys.platform != "darwin":
        return
    deadline = time.time() + 2.0
    while time.time() < deadline:
        children = _macos_child_pids(pid)
        if not children:
            return
        for child in children:
            try:
                os.kill(child, getattr(os, "SIGKILL", 9))
            except (ProcessLookupError, OSError):
                pass
        time.sleep(0.02)
