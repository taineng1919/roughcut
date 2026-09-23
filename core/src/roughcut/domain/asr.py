"""Deterministic source-level ASR routing primitives."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

ASR_CLOUD_TAG = "asr:cloud"
QWEN_FILETRANS_ROUTE = "qwen_filetrans"
LOCAL_FUNASR_ROUTE = "funasr"


def source_filename_declares_cloud(filename: str) -> bool:
    """Return whether an original filename has the exact cloud suffix marker."""

    return Path(filename).stem.endswith("__方言")


def merge_cloud_route_tag(tags: Iterable[str], *, enabled: bool) -> tuple[str, ...]:
    """Merge or explicitly remove the canonical cloud route tag."""

    merged = list(tags)
    if enabled:
        if ASR_CLOUD_TAG not in merged:
            merged.append(ASR_CLOUD_TAG)
    else:
        merged = [tag for tag in merged if tag != ASR_CLOUD_TAG]
    return tuple(merged)


def resolve_asr_route(tags: Iterable[str]) -> str:
    """Resolve the fixed route from canonical Source tags without I/O."""

    return QWEN_FILETRANS_ROUTE if ASR_CLOUD_TAG in tags else LOCAL_FUNASR_ROUTE
