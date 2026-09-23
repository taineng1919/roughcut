from __future__ import annotations

import pytest

from roughcut.domain.alignment import (
    AlignmentError,
    alignment_request_projection,
    hash_alignment_request,
)

_SCOPE = {
    "kind": "project",
    "project_id": "project_fixture",
    "project_root_hash": "a" * 64,
}


def _projection(auxiliary_cameras: list[dict[str, object]]) -> dict[str, object]:
    return alignment_request_projection(
        scope=_SCOPE,
        operation_id="op_00000000000040008000000000000100",
        alignment_id="aln_pairs",
        expected_revision=1,
        main_camera={
            "camera_id": "main",
            "ordered_source_ids": ["main_b", "main_a"],
        },
        auxiliary_cameras=auxiliary_cameras,
        main_audio_stable=True,
        max_temporary_disk_bytes=1,
        max_analysis_memory_bytes=1,
        max_runtime_seconds=1,
    )


def test_source_pairs_are_closed_sorted_and_hash_bound() -> None:
    first = _projection(
        [
            {
                "camera_id": "aux-1",
                "ordered_source_ids": ["aux_b", "aux_a"],
                "source_pairs": [
                    {
                        "main_source_id": "main_a",
                        "auxiliary_source_id": "aux_a",
                    },
                    {
                        "main_source_id": "main_b",
                        "auxiliary_source_id": "aux_b",
                    },
                ],
            }
        ]
    )
    reversed_order = _projection(
        [
            {
                "camera_id": "aux-1",
                "ordered_source_ids": ["aux_b", "aux_a"],
                "source_pairs": [
                    {
                        "main_source_id": "main_b",
                        "auxiliary_source_id": "aux_b",
                    },
                    {
                        "main_source_id": "main_a",
                        "auxiliary_source_id": "aux_a",
                    },
                ],
            }
        ]
    )
    assert first["auxiliary_cameras"] == [
        {
            "camera_id": "aux-1",
            "ordered_source_ids": ["aux_b", "aux_a"],
            "source_pairs": [
                {"main_source_id": "main_a", "auxiliary_source_id": "aux_a"},
                {"main_source_id": "main_b", "auxiliary_source_id": "aux_b"},
            ],
        }
    ]
    assert first == reversed_order
    assert hash_alignment_request(first) == hash_alignment_request(reversed_order)

    without_pairs = _projection(
        [{"camera_id": "aux-1", "ordered_source_ids": ["aux_a"]}]
    )
    assert hash_alignment_request(first) != hash_alignment_request(without_pairs)


def test_source_pairs_reject_duplicate_and_cross_group_ids() -> None:
    duplicate = {
        "camera_id": "aux-1",
        "ordered_source_ids": ["aux_a", "aux_b"],
        "source_pairs": [
            {"main_source_id": "main_a", "auxiliary_source_id": "aux_a"},
            {"main_source_id": "main_a", "auxiliary_source_id": "aux_a"},
        ],
    }
    with pytest.raises(AlignmentError):
        _projection([duplicate])

    unknown_main = {
        "camera_id": "aux-1",
        "ordered_source_ids": ["aux_a"],
        "source_pairs": [
            {"main_source_id": "main_unknown", "auxiliary_source_id": "aux_a"}
        ],
    }
    with pytest.raises(AlignmentError):
        _projection([unknown_main])

    cross_group_auxiliary = {
        "camera_id": "aux-1",
        "ordered_source_ids": ["aux_a"],
        "source_pairs": [
            {"main_source_id": "main_a", "auxiliary_source_id": "aux_other"}
        ],
    }
    with pytest.raises(AlignmentError):
        _projection([cross_group_auxiliary])


def test_source_pairs_reject_empty_lists() -> None:
    with pytest.raises(AlignmentError):
        _projection(
            [
                {
                    "camera_id": "aux-1",
                    "ordered_source_ids": ["aux_a"],
                    "source_pairs": [],
                }
            ]
        )


def test_source_pairs_are_closed_objects() -> None:
    with pytest.raises(AlignmentError):
        _projection(
            [
                {
                    "camera_id": "aux-1",
                    "ordered_source_ids": ["aux_a"],
                    "source_pairs": [
                        {
                            "main_source_id": "main_a",
                            "auxiliary_source_id": "aux_a",
                            "extra": "rejected",
                        }
                    ],
                }
            ]
        )


def test_writer_profile_produces_schema2_hash_and_legacy_stays_v1() -> None:
    """The waveform writer identity enters the request hash (schema 2) only
    as the exact canonical profile; the historical schema-1 projection
    without writer identity stays byte-stable for pre-existing succeeded
    operations."""
    from roughcut.domain.alignment import WAVEFORM_WRITER_PROFILE

    public = {
        "scope": _SCOPE,
        "operation_id": "op_00000000000040008000000000000101",
        "alignment_id": "aln_pairs",
        "expected_revision": 1,
        "main_camera": {"camera_id": "main", "ordered_source_ids": ["main_a"]},
        "auxiliary_cameras": [
            {"camera_id": "aux-1", "ordered_source_ids": ["aux_a"]}
        ],
        "main_audio_stable": True,
        "max_temporary_disk_bytes": 1,
        "max_analysis_memory_bytes": 1,
        "max_runtime_seconds": 1,
    }
    legacy = alignment_request_projection(**public)
    assert legacy["request_schema_version"] == 1
    assert "writer_profile" not in legacy
    current = alignment_request_projection(
        **public, writer_profile=WAVEFORM_WRITER_PROFILE
    )
    assert current["request_schema_version"] == 2
    assert current["writer_profile"] == WAVEFORM_WRITER_PROFILE
    assert hash_alignment_request(current) != hash_alignment_request(legacy)
    # the same identity is stable
    again = alignment_request_projection(
        **public, writer_profile=dict(WAVEFORM_WRITER_PROFILE)
    )
    assert hash_alignment_request(again) == hash_alignment_request(current)

    def _revalidated(value: dict[str, object]) -> dict[str, object]:
        from roughcut.domain.alignment import _validate_alignment_request

        return _validate_alignment_request(value)

    # both projections roundtrip their own closed validation
    assert hash_alignment_request(current) == hash_alignment_request(
        _revalidated(current)
    )
    assert hash_alignment_request(legacy) == hash_alignment_request(
        _revalidated(legacy)
    )

    # forged / extra / missing / non-dict writer profiles are all rejected
    forged = {**legacy, "request_schema_version": 2}
    with pytest.raises(AlignmentError):
        _revalidated(forged)  # missing writer_profile entirely
    partial = {
        **current,
        "writer_profile": {
            "name": "roughcut_waveform_fixed_offset",
            "version": 1,
        },
    }
    with pytest.raises(AlignmentError):
        _revalidated(partial)
    extra = {
        **current,
        "writer_profile": {
            **WAVEFORM_WRITER_PROFILE,
            "extra_field": True,
        },
    }
    with pytest.raises(AlignmentError):
        _revalidated(extra)
    altered = {
        **current,
        "writer_profile": {
            **WAVEFORM_WRITER_PROFILE,
            "coarse_decode_sample_rate_hz": 44_100,
        },
    }
    with pytest.raises(AlignmentError):
        _revalidated(altered)
    not_a_dict = {**current, "writer_profile": "roughcut_waveform_fixed_offset"}
    with pytest.raises(AlignmentError):
        _revalidated(not_a_dict)
    # an unknown projection version stays rejected
    with pytest.raises(AlignmentError):
        _revalidated({**legacy, "request_schema_version": 3})
    with pytest.raises(AlignmentError):
        _revalidated({**legacy, "writer_profile": WAVEFORM_WRITER_PROFILE})
