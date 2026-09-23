"""Release guard for the M2.7 public multicam surface."""

from __future__ import annotations

import platform

from roughcut.domain.alignment import AlignmentError
from roughcut.domain.multicam_parallel import ParallelRenderError

_WINDOWS_ERROR_CODES = {
    "align_multicam": "alignment_runtime_unavailable",
    "multicam_parallel_render_prepare": "parallel_render_runtime_unavailable",
    "multicam_parallel_render_start": "parallel_render_runtime_unavailable",
}


def require_m2_7_public_capability(entry_name: str) -> None:
    """Reject the deferred M2.7 public surface on Windows."""
    try:
        error_code = _WINDOWS_ERROR_CODES[entry_name]
    except KeyError as error:
        raise ValueError(f"unknown M2.7 public entry: {entry_name}") from error
    if platform.system() != "Windows":
        return
    if entry_name == "align_multicam":
        raise AlignmentError(error_code, "M2.7 alignment is not released on Windows")
    raise ParallelRenderError(error_code, "M2.7 parallel render is not released on Windows")
