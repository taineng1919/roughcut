from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from roughcut.adapters.qwen.normalize import (
    QWEN_TICKS_PER_MILLISECOND,
    QwenNormalizationError,
    normalize_qwen,
)
from roughcut.domain.time import TICKS_PER_SECOND
from roughcut.domain.transcript import TimedTranscript

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "fixtures" / "asr"
FIXTURE_NAME = "qwen_filetrans_recognition.json"


def load_fixture() -> dict[str, Any]:
    payload = json.loads((FIXTURES / FIXTURE_NAME).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def sentences(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return raw["transcripts"][0]["sentences"]


def words(raw: dict[str, Any], sentence_index: int) -> list[dict[str, Any]]:
    return sentences(raw)[sentence_index]["words"]


def normalize_synthetic(
    raw: object,
    *,
    source_duration_ticks: int = 480_000,
    raw_result_path: str = "raw-asr/src_fixture/run.json",
    started_at: str = "unknown",
    completed_at: str = "unknown",
    exit_status: int = 0,
):
    return normalize_qwen(
        raw,
        source_id="src_fixture",
        transcript_version_id="tr_fixture",
        raw_result_path=raw_result_path,
        source_duration_ticks=source_duration_ticks,
        started_at=started_at,
        completed_at=completed_at,
        exit_status=exit_status,
    )


def test_sentence_and_word_mapping_uses_the_existing_schema() -> None:
    transcript = normalize_synthetic(load_fixture())

    assert transcript.schema_version == 1
    assert transcript.source_id == "src_fixture"
    assert transcript.transcript_version_id == "tr_fixture"
    assert transcript.parent_version_id is None
    assert transcript.language == "zh-CN"
    assert isinstance(transcript, TimedTranscript)

    assert [segment.segment_id for segment in transcript.segments] == [
        "seg_000001",
        "seg_000002",
    ]
    assert [
        (segment.start_ticks, segment.end_ticks) for segment in transcript.segments
    ] == [(12_000, 240_000), (288_000, 432_000)]
    assert [segment.original_text for segment in transcript.segments] == [
        "你好，世界。",
        "第二句",
    ]
    assert [segment.local_speaker_id for segment in transcript.segments] == [
        "spk_0",
        "spk_1",
    ]
    assert all(segment.corrected_text is None for segment in transcript.segments)
    assert all(segment.person_id is None for segment in transcript.segments)
    assert all(segment.editorial_mark == "unmarked" for segment in transcript.segments)


def test_one_millisecond_is_exactly_120_ticks() -> None:
    transcript = normalize_synthetic(load_fixture())

    assert TICKS_PER_SECOND == 120_000
    assert QWEN_TICKS_PER_MILLISECOND == 120
    first = transcript.segments[0]
    assert first.start_ticks == 100 * 120
    assert first.end_ticks == 2000 * 120
    assert transcript.segments[1].start_ticks == 2400 * 120


def test_words_become_fine_units_with_attached_punctuation() -> None:
    transcript = normalize_synthetic(load_fixture())

    first_units = transcript.segments[0].fine_units
    assert [unit.kind for unit in first_units] == ["word", "word"]
    assert [unit.text for unit in first_units] == ["你好，", "世界。"]
    assert [(unit.start_ticks, unit.end_ticks) for unit in first_units] == [
        (12_000, 84_000),
        (84_000, 168_000),
    ]
    # Provider punctuation has no independent timing, so it never produces an
    # extra, punctuation-only or zero-duration unit.
    fixture_words = words(load_fixture(), 0)
    assert len(first_units) == len(fixture_words)
    assert all(unit.end_ticks > unit.start_ticks for unit in first_units)
    assert transcript.segments[1].fine_units[1].text == "句"


# Real WP4 Cloud evidence: run `run_3fc0432744f54278b5140eac03cf7adb` of
# `src_ff0f2b8226e241a89ba33317483719e3` contained 4 word objects out of 212
# whose lexical `text` was a single ASCII space while `begin_time` / `end_time`
# were legal integer milliseconds, `punctuation` was the empty string, and each
# artifact sat contiguously between two lexical words.
WP4_ARTIFACT_TEXT = " "


def raw_with_word_artifact(
    *,
    artifact_begin: int = 700,
    artifact_end: int = 800,
    next_word_begin: int = 800,
) -> dict[str, Any]:
    """Sentence 0 rebuilt as lexical word / whitespace artifact / lexical word."""

    raw = load_fixture()
    first, second = words(raw, 0)
    second["begin_time"] = next_word_begin
    sentences(raw)[0]["words"] = [
        first,
        {
            "begin_time": artifact_begin,
            "end_time": artifact_end,
            "text": WP4_ARTIFACT_TEXT,
            "punctuation": "",
        },
        second,
    ]
    return raw


@pytest.mark.parametrize("artifact_text", (WP4_ARTIFACT_TEXT, ""))
def test_real_whitespace_word_artifact_is_not_published_as_a_fine_unit(
    artifact_text: str,
) -> None:
    raw = raw_with_word_artifact()
    words(raw, 0)[1]["text"] = artifact_text

    transcript = normalize_synthetic(raw)

    units = transcript.segments[0].fine_units
    assert len(units) == 2
    assert [unit.text for unit in units] == ["你好，", "世界。"]
    # The artifact does not shift or shrink the lexical neighbours it sat between.
    assert [(unit.start_ticks, unit.end_ticks) for unit in units] == [
        (12_000, 84_000),
        (96_000, 168_000),
    ]


@pytest.mark.parametrize(
    ("label", "artifact_begin", "artifact_end", "next_word_begin"),
    (
        ("artifact overlaps the preceding lexical word", 600, 800, 800),
        ("following lexical word overlaps the artifact", 700, 900, 700),
    ),
)
def test_whitespace_word_artifact_timing_still_constrains_the_sequence(
    label: str, artifact_begin: int, artifact_end: int, next_word_begin: int
) -> None:
    raw = raw_with_word_artifact(
        artifact_begin=artifact_begin,
        artifact_end=artifact_end,
        next_word_begin=next_word_begin,
    )

    with pytest.raises(QwenNormalizationError):
        normalize_synthetic(raw)


@pytest.mark.parametrize(
    ("label", "mutate"),
    (
        (
            "blank artifact with numeric string timing",
            lambda raw: words(raw, 0)[1].update({"begin_time": "700"}),
        ),
        (
            "blank artifact with integral float timing",
            lambda raw: words(raw, 0)[1].update({"begin_time": 700.0}),
        ),
        (
            "blank artifact with boolean timing",
            lambda raw: words(raw, 0)[1].update({"begin_time": True}),
        ),
        (
            "blank artifact with missing begin",
            lambda raw: words(raw, 0)[1].pop("begin_time"),
        ),
        (
            "blank artifact with missing end",
            lambda raw: words(raw, 0)[1].pop("end_time"),
        ),
        (
            "blank artifact with zero duration",
            lambda raw: words(raw, 0)[1].update({"end_time": 700}),
        ),
        (
            "blank artifact with reversed bounds",
            lambda raw: words(raw, 0)[1].update({"end_time": 600}),
        ),
        (
            "blank artifact ending after its sentence",
            lambda raw: words(raw, 0)[1].update({"end_time": 2001}),
        ),
    ),
)
def test_blank_word_artifact_does_not_mask_malformed_timing(
    label: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    raw = raw_with_word_artifact()
    mutate(raw)

    with pytest.raises(QwenNormalizationError):
        normalize_synthetic(raw)


@pytest.mark.parametrize(
    ("label", "mutate"),
    (
        (
            "blank artifact without text",
            lambda raw: words(raw, 0)[1].pop("text"),
        ),
        (
            "blank artifact with null text",
            lambda raw: words(raw, 0)[1].update({"text": None}),
        ),
        (
            "blank artifact with integer text",
            lambda raw: words(raw, 0)[1].update({"text": 123}),
        ),
        (
            "blank artifact with object text",
            lambda raw: words(raw, 0)[1].update({"text": {}}),
        ),
        (
            "blank artifact with list text",
            lambda raw: words(raw, 0)[1].update({"text": [WP4_ARTIFACT_TEXT]}),
        ),
        (
            "blank artifact with boolean text",
            lambda raw: words(raw, 0)[1].update({"text": True}),
        ),
    ),
)
def test_non_string_word_text_is_still_malformed(
    label: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    raw = raw_with_word_artifact()
    mutate(raw)

    with pytest.raises(QwenNormalizationError):
        normalize_synthetic(raw)


@pytest.mark.parametrize(
    ("label", "mutate"),
    (
        (
            "blank artifact with integer punctuation",
            lambda raw: words(raw, 0)[1].update({"punctuation": 5}),
        ),
        (
            "blank artifact with overlong punctuation",
            lambda raw: words(raw, 0)[1].update({"punctuation": "!!!!!!!!!"}),
        ),
        (
            "blank artifact with locator shaped punctuation",
            lambda raw: words(raw, 0)[1].update(
                {"punctuation": "https://signed.invalid/?Signature=SECRET"}
            ),
        ),
        (
            "blank artifact with alphanumeric punctuation",
            lambda raw: words(raw, 0)[1].update(
                {"punctuation": "fake-signature-canary"}
            ),
        ),
    ),
)
def test_blank_word_artifact_does_not_mask_malformed_punctuation(
    label: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    raw = raw_with_word_artifact()
    mutate(raw)

    with pytest.raises(QwenNormalizationError):
        normalize_synthetic(raw)


def test_blank_word_artifact_with_valid_punctuation_publishes_nothing() -> None:
    """Validated punctuation on an artifact never becomes its own unit."""

    raw = raw_with_word_artifact()
    words(raw, 0)[1]["punctuation"] = "，"

    transcript = normalize_synthetic(raw)

    units = transcript.segments[0].fine_units
    assert [unit.text for unit in units] == ["你好，", "世界。"]
    assert all(unit.text.strip() for unit in units)


def test_missing_provider_confidence_is_not_guessed() -> None:
    raw = load_fixture()
    sentences(raw)[0]["confidence"] = 0.98
    words(raw, 0)[0]["score"] = 0.5
    raw["transcripts"][0]["confidence"] = 0.99

    transcript = normalize_synthetic(raw)

    assert transcript.segments[0].confidence is None
    assert transcript.segments[1].confidence is None
    assert all(
        unit.confidence is None
        for segment in transcript.segments
        for unit in segment.fine_units
    )


def test_missing_speaker_is_none_and_speaker_count_is_not_a_success_condition() -> None:
    raw = load_fixture()
    raw["transcripts"][0]["speaker_count"] = 3
    for sentence in sentences(raw):
        sentence.pop("speaker_id")

    transcript = normalize_synthetic(raw)

    assert [segment.local_speaker_id for segment in transcript.segments] == [None, None]
    assert len(transcript.segments) == 2


def test_speaker_id_is_mapped_without_identity_inference() -> None:
    transcript = normalize_synthetic(load_fixture())

    assert [segment.local_speaker_id for segment in transcript.segments] == [
        "spk_0",
        "spk_1",
    ]
    assert all(segment.person_id is None for segment in transcript.segments)


def test_timing_exactly_at_source_duration_is_accepted_without_tolerance() -> None:
    raw = load_fixture()
    sentences(raw)[1]["end_time"] = 4000
    words(raw, 1)[1]["end_time"] = 4000

    transcript = normalize_synthetic(raw, source_duration_ticks=4000 * 120)

    assert transcript.segments[1].end_ticks == 480_000


def test_timing_beyond_source_duration_is_not_clamped_or_shifted() -> None:
    raw = load_fixture()
    sentences(raw)[1]["end_time"] = 4001
    words(raw, 1)[1]["end_time"] = 4001

    with pytest.raises(QwenNormalizationError):
        normalize_synthetic(raw, source_duration_ticks=4000 * 120)


def test_result_round_trips_through_the_unchanged_transcript_schema() -> None:
    transcript = normalize_synthetic(load_fixture())

    restored = TimedTranscript.from_dict(transcript.to_dict())

    assert restored == transcript
    assert restored.to_dict() == transcript.to_dict()
    assert restored.provenance.backend == "qwen_filetrans"
    assert restored.provenance.raw_result_path == "raw-asr/src_fixture/run.json"
    assert restored.provenance.models == {
        "asr": "qwen-audio-3.0-asr-flash-filetrans"
    }


def test_provenance_records_no_secret_or_locator() -> None:
    serialized = json.dumps(
        normalize_synthetic(load_fixture()).provenance.to_dict(), ensure_ascii=False
    )

    for forbidden in ("oss://", "Signature", "Authorization", "task_id"):
        assert forbidden not in serialized


def test_published_provenance_is_a_closed_derived_mapping() -> None:
    """Provenance is derived from fixed Qwen identity, not caller mappings."""

    provenance = normalize_synthetic(load_fixture()).provenance

    assert provenance.backend == "qwen_filetrans"
    assert provenance.models == {"asr": "qwen-audio-3.0-asr-flash-filetrans"}
    assert provenance.parameters == {
        "transport": "temporary_upload",
        "region": "cn-beijing",
        "audio_profile": "16_khz_mono_flac",
        "language_hints": ["zh", "en"],
        "channel_id": [0],
    }
    assert provenance.raw_result_path == "raw-asr/src_fixture/run.json"

    serialized = json.dumps(
        normalize_synthetic(load_fixture()).to_dict(), ensure_ascii=False
    )
    for canary in (
        "fake-api-key-canary-0001",
        "fakeworkspace01",
        "oss://",
        "Signature=",
        "fake-task-id-0001",
        "fake-upload-policy-token-canary",
        "https://",
    ):
        assert canary not in serialized


def test_normalizer_does_not_accept_caller_supplied_provenance() -> None:
    """The open-ended provenance inputs are gone from the signature."""

    for field, value in (
        ("models", {"asr": "fake-api-key-canary-0001"}),
        ("parameters", {"region": "oss://bucket/object"}),
        ("package_version", "fake-api-key-canary-0001"),
    ):
        with pytest.raises(TypeError):
            normalize_qwen(
                load_fixture(),
                source_id="src_fixture",
                transcript_version_id="tr_fixture",
                raw_result_path="raw-asr/src_fixture/run.json",
                source_duration_ticks=480_000,
                **{field: value},  # type: ignore[arg-type]
            )


@pytest.mark.parametrize(
    "raw_result_path",
    (
        "https://signed.invalid/run.json?Signature=SECRET",
        "oss://bucket/object",
        "/absolute/raw-asr/run.json",
        "raw-asr/../../etc/passwd",
        "C:\\\\media\\\\raw-asr\\\\run.json",
        "transcripts/src_fixture/run.json",
        "raw-asr/other_source/run.json",
        "raw-asr/src_fixture/run.json?Signature=SECRET",
        "",
    ),
)
def test_normalizer_rejects_a_non_evidence_result_path(raw_result_path: str) -> None:
    with pytest.raises(QwenNormalizationError) as error:
        normalize_synthetic(load_fixture(), raw_result_path=raw_result_path)

    assert error.value.raw_result_path == "<rejected>"
    assert "SECRET" not in str(error.value)
    assert "oss://" not in str(error.value)


def test_published_evidence_path_is_canonical() -> None:
    transcript = normalize_synthetic(load_fixture())

    assert transcript.provenance.raw_result_path == "raw-asr/src_fixture/run.json"


def test_normalizer_rejects_a_non_evidence_source_id() -> None:
    for source_id in ("fake-api-key-canary-0001/../x", "src fixture", "oss://bucket", ""):
        with pytest.raises(QwenNormalizationError) as error:
            normalize_qwen(
                load_fixture(),
                source_id=source_id,
                transcript_version_id="tr_fixture",
                raw_result_path="raw-asr/src_fixture/run.json",
                source_duration_ticks=480_000,
            )
        assert error.value.raw_result_path == "<rejected>"
        if source_id:
            assert source_id not in str(error.value)


@pytest.mark.parametrize(
    "timestamp",
    (
        "https://signed.invalid/run.json?Signature=SECRET",
        "fake-api-key-canary-0001",
        "2026-09-11T15:44:36.414000",
        "",
        5,
    ),
)
def test_normalizer_rejects_non_timestamp_run_facts(timestamp: object) -> None:
    for field in ("started_at", "completed_at"):
        with pytest.raises(QwenNormalizationError):
            normalize_synthetic(load_fixture(), **{field: timestamp})


def test_normalizer_accepts_the_adapter_run_facts() -> None:
    transcript = normalize_synthetic(
        load_fixture(),
        started_at="2026-09-11T15:44:36.414000+00:00",
        completed_at="2026-09-11T15:46:12.000000+00:00",
        exit_status=0,
    )

    assert transcript.provenance.started_at == "2026-09-11T15:44:36.414000+00:00"
    assert transcript.provenance.exit_status == 0


def test_normalizer_rejects_a_non_integer_exit_status() -> None:
    for exit_status in (True, "0", 1.0):
        with pytest.raises(QwenNormalizationError):
            normalize_synthetic(load_fixture(), exit_status=exit_status)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("label", "mutate"),
    (
        ("sentence zero duration", lambda raw: sentences(raw)[0].update({"end_time": 100})),
        ("sentence reversed bounds", lambda raw: sentences(raw)[0].update({"end_time": 50})),
        ("sentence negative begin", lambda raw: sentences(raw)[0].update({"begin_time": -1})),
        (
            "sentence non-monotonic",
            lambda raw: sentences(raw)[1].update({"begin_time": 1000}),
        ),
        (
            "sentence missing begin",
            lambda raw: sentences(raw)[0].pop("begin_time"),
        ),
        ("sentence missing end", lambda raw: sentences(raw)[0].pop("end_time")),
        (
            "sentence boolean timing",
            lambda raw: sentences(raw)[0].update({"begin_time": True}),
        ),
        (
            "sentence fractional timing",
            lambda raw: sentences(raw)[0].update({"begin_time": 100.5}),
        ),
        (
            "sentence text timing",
            lambda raw: sentences(raw)[0].update({"begin_time": "abc"}),
        ),
        (
            "sentence integral float timing",
            lambda raw: sentences(raw)[0].update({"begin_time": 100.0}),
        ),
        (
            "sentence numeric string timing",
            lambda raw: sentences(raw)[0].update({"begin_time": "100"}),
        ),
        (
            "sentence missing text",
            lambda raw: sentences(raw)[0].pop("text"),
        ),
        (
            "word zero duration",
            lambda raw: words(raw, 0)[0].update({"end_time": 100}),
        ),
        (
            "word reversed bounds",
            lambda raw: words(raw, 0)[0].update({"end_time": 50}),
        ),
        (
            "word non-monotonic",
            lambda raw: words(raw, 0)[1].update({"begin_time": 300}),
        ),
        (
            "word starts before its sentence",
            lambda raw: words(raw, 0)[0].update({"begin_time": 0}),
        ),
        (
            "word ends after its sentence",
            lambda raw: words(raw, 0)[1].update({"end_time": 2100}),
        ),
        ("word missing begin", lambda raw: words(raw, 0)[0].pop("begin_time")),
        ("word missing end", lambda raw: words(raw, 0)[1].pop("end_time")),
        (
            "word integral float timing",
            lambda raw: words(raw, 0)[0].update({"end_time": 700.0}),
        ),
        (
            "word numeric string timing",
            lambda raw: words(raw, 0)[1].update({"begin_time": "3000"}),
        ),
        ("word missing text", lambda raw: words(raw, 0)[0].pop("text")),
        # A blank ``text`` with legal timing is a timing-only artifact, not
        # malformed input: see
        # test_real_whitespace_word_artifact_is_not_published_as_a_fine_unit and
        # test_blank_word_artifact_does_not_mask_malformed_timing below.
        (
            "word malformed punctuation",
            lambda raw: words(raw, 0)[0].update({"punctuation": 5}),
        ),
        (
            "word overlong punctuation",
            lambda raw: words(raw, 0)[0].update({"punctuation": "!!!!!!!!!"}),
        ),
        ("sentence without words", lambda raw: sentences(raw)[0].update({"words": []})),
        (
            "word not an object",
            lambda raw: sentences(raw)[0].update({"words": ["你好"]}),
        ),
        (
            "sentence not an object",
            lambda raw: raw["transcripts"][0].update({"sentences": [1]}),
        ),
        ("transcripts not a list", lambda raw: raw.update({"transcripts": {}})),
        ("empty transcripts", lambda raw: raw.update({"transcripts": []})),
        (
            "two transcripts",
            lambda raw: raw.update(
                {"transcripts": [raw["transcripts"][0], deepcopy(raw["transcripts"][0])]}
            ),
        ),
        (
            "transcript without sentences",
            lambda raw: raw["transcripts"][0].update({"sentences": []}),
        ),
        (
            "illegal speaker id",
            lambda raw: sentences(raw)[0].update({"speaker_id": -1}),
        ),
        (
            "string speaker id",
            lambda raw: sentences(raw)[0].update({"speaker_id": "abc"}),
        ),
        (
            "credential shaped speaker id",
            lambda raw: sentences(raw)[0].update({"speaker_id": "fake-api-key-canary-0001"}),
        ),
        (
            "locator shaped punctuation",
            lambda raw: words(raw, 0)[0].update(
                {"punctuation": "https://signed.invalid/?Signature=SECRET"}
            ),
        ),
        (
            "alphanumeric punctuation",
            lambda raw: words(raw, 0)[0].update({"punctuation": "fake-signature-canary"}),
        ),
        (
            "boolean speaker id",
            lambda raw: sentences(raw)[0].update({"speaker_id": True}),
        ),
        (
            "malformed speaker id",
            lambda raw: sentences(raw)[0].update({"speaker_id": ["a"]}),
        ),
    ),
)
def test_normalizer_fails_closed(
    label: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    raw = load_fixture()
    mutate(raw)

    with pytest.raises(QwenNormalizationError):
        normalize_synthetic(raw)


def test_non_object_result_fails_closed() -> None:
    for raw in ([], "text", 5, None):
        with pytest.raises(QwenNormalizationError):
            normalize_synthetic(raw)


def test_malformed_sentence_keeps_the_evidence_path() -> None:
    raw = load_fixture()
    sentences(raw)[0].update({"end_time": 100})

    with pytest.raises(QwenNormalizationError) as error:
        normalize_synthetic(raw)

    assert error.value.raw_result_path == "raw-asr/src_fixture/run.json"
