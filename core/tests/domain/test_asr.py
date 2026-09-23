from __future__ import annotations

import pytest

from roughcut.domain.asr import (
    ASR_CLOUD_TAG,
    LOCAL_FUNASR_ROUTE,
    QWEN_FILETRANS_ROUTE,
    merge_cloud_route_tag,
    resolve_asr_route,
    source_filename_declares_cloud,
)


@pytest.mark.parametrize(
    ("filename", "declares_cloud"),
    (
        ("采访02__方言.mov", True),
        ("方言采访.mov", False),
        ("采访_方言版.mov", False),
        ("abc__方言_01.mov", False),
        ("abc__方言.mov.bak", False),
        ("abc__方言.mov", True),
    ),
)
def test_source_filename_marker_uses_exact_stem_suffix(
    filename: str, declares_cloud: bool
) -> None:
    assert source_filename_declares_cloud(filename) is declares_cloud


def test_cloud_route_tag_merge_is_idempotent_and_explicitly_removable() -> None:
    tags = ("role:main", "audio:good")
    marked = merge_cloud_route_tag(tags, enabled=True)
    assert marked == ("role:main", "audio:good", ASR_CLOUD_TAG)
    assert merge_cloud_route_tag(marked, enabled=True) == marked
    assert merge_cloud_route_tag(marked, enabled=False) == tags


def test_asr_route_resolver_is_fixed_to_marker_presence() -> None:
    assert resolve_asr_route(("role:main",)) == LOCAL_FUNASR_ROUTE
    assert resolve_asr_route(("role:main", ASR_CLOUD_TAG)) == QWEN_FILETRANS_ROUTE
