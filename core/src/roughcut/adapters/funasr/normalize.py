"""Normalize known FunASR result shapes into timed transcript models."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from itertools import pairwise
from typing import Any

from roughcut.domain.time import TICKS_PER_SECOND
from roughcut.domain.transcript import (
    FineUnit,
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)

MAX_FALLBACK_SEGMENT_MILLISECONDS = Decimal(8000)
MAX_FALLBACK_SEGMENT_TOKENS = 30
FUNASR_TIMESTAMP_QUANTUM_TICKS = TICKS_PER_SECOND // 100
FUNASR_ADJACENT_OVERLAP_TOLERANCE_TICKS = 2 * FUNASR_TIMESTAMP_QUANTUM_TICKS
RECOGNITION_TOKEN = re.compile(r"[\u4e00-\u9fff]|[\w-]+", re.UNICODE)


class TranscriptNormalizationError(ValueError):
    def __init__(self, message: str, raw_result_path: str) -> None:
        super().__init__(message)
        self.raw_result_path = raw_result_path


class _SentenceInfoError(ValueError):
    pass


def normalize_funasr(
    raw: object,
    *,
    source_id: str,
    transcript_version_id: str,
    raw_result_path: str,
    source_duration_ticks: int,
    package_version: str,
    models: dict[str, str],
    parameters: dict[str, object],
    started_at: str = "unknown",
    completed_at: str = "unknown",
    exit_status: int = 0,
) -> TimedTranscript:
    try:
        entries = _timed_entries(raw, require_speaker=_speaker_enabled(parameters))
    except _SentenceInfoError as error:
        raise TranscriptNormalizationError(str(error), raw_result_path) from error
    if not entries:
        raise TranscriptNormalizationError("FunASR result has no timed segments", raw_result_path)

    prepared: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        text = _recognized_text(entry)
        start = _milliseconds(entry.get("start"))
        end = _milliseconds(entry.get("end"))
        if not isinstance(text, str) or not text.strip() or start is None or end is None:
            raise TranscriptNormalizationError("FunASR timed segment is malformed", raw_result_path)
        start_ticks = _milliseconds_to_ticks(start)
        raw_end_ticks = _milliseconds_to_ticks(end)
        if start_ticks < 0 or raw_end_ticks <= start_ticks:
            raise TranscriptNormalizationError("FunASR segment has invalid bounds", raw_result_path)
        end_ticks = raw_end_ticks
        clamped_final_end = False
        if end_ticks > source_duration_ticks:
            overrun_ticks = end_ticks - source_duration_ticks
            if (
                index == len(entries)
                and start_ticks < source_duration_ticks
                and overrun_ticks <= FUNASR_TIMESTAMP_QUANTUM_TICKS
            ):
                end_ticks = source_duration_ticks
                clamped_final_end = True
            else:
                raise TranscriptNormalizationError(
                    "FunASR segment exceeds source duration", raw_result_path
                )

        prepared.append(
            {
                "entry": entry,
                "text": text.strip(),
                "start_ticks": start_ticks,
                "raw_end_ticks": raw_end_ticks,
                "end_ticks": end_ticks,
                "clamped_final_end": clamped_final_end,
            }
        )

    overlap_indexes: list[int] = []
    for index in range(1, len(prepared)):
        left = prepared[index - 1]
        right = prepared[index]
        overlap_ticks = left["raw_end_ticks"] - right["start_ticks"]
        if overlap_ticks <= 0:
            continue
        if overlap_ticks > FUNASR_ADJACENT_OVERLAP_TOLERANCE_TICKS:
            raise TranscriptNormalizationError(
                "FunASR segments are not monotonic", raw_result_path
            )
        overlap_indexes.append(index - 1)

    if any(
        right_index - left_index == 1
        for left_index, right_index in pairwise(overlap_indexes)
    ):
        raise TranscriptNormalizationError("FunASR segments are not monotonic", raw_result_path)

    for left_index in overlap_indexes:
        left = prepared[left_index]
        right = prepared[left_index + 1]
        boundary = right["start_ticks"]
        token_boundary = _overlap_token_boundary(
            left["entry"],
            right["entry"],
            left_start=left["start_ticks"],
            left_end=left["raw_end_ticks"],
            right_start=right["start_ticks"],
            right_end=right["raw_end_ticks"],
        )
        if token_boundary is not None:
            boundary = token_boundary
            right["start_ticks"] = boundary
        if boundary <= left["start_ticks"] or boundary >= right["raw_end_ticks"]:
            raise TranscriptNormalizationError("FunASR segments are not monotonic", raw_result_path)
        left["end_ticks"] = boundary

    segments: list[TranscriptSegment] = []
    for index, item in enumerate(prepared, start=1):
        entry = item["entry"]
        start_ticks = item["start_ticks"]
        end_ticks = item["end_ticks"]

        segments.append(
            TranscriptSegment(
                segment_id=f"seg_{index:06d}",
                start_ticks=start_ticks,
                end_ticks=end_ticks,
                original_text=item["text"],
                corrected_text=None,
                local_speaker_id=_speaker(entry.get("spk")),
                person_id=None,
                confidence=_confidence(entry.get("confidence")),
                fine_units=_fine_units(
                    entry,
                    start_ticks,
                    end_ticks,
                    original_segment_end=item["raw_end_ticks"],
                    clamp_final_end=item["clamped_final_end"],
                    is_last_segment=index == len(entries),
                    source_duration_ticks=source_duration_ticks,
                ),
                editorial_mark="unmarked",
            )
        )

    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_version_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="funasr",
            package_version=package_version,
            models=models,
            parameters=parameters,
            raw_result_path=raw_result_path,
            started_at=started_at,
            completed_at=completed_at,
            exit_status=exit_status,
        ),
        language="zh-CN",
        segments=tuple(segments),
    )


def _overlap_token_boundary(
    left_entry: dict[str, Any],
    right_entry: dict[str, Any],
    *,
    left_start: int,
    left_end: int,
    right_start: int,
    right_end: int,
) -> int | None:
    left_units = _reliable_fine_timestamps(left_entry, left_start, left_end)
    right_units = _reliable_fine_timestamps(right_entry, right_start, right_end)
    if left_units is None or right_units is None:
        return None

    left_last_end = left_units[-1][1]
    right_first_start = right_units[0][0]
    if left_last_end > right_first_start:
        return None

    boundary = max(right_start, left_last_end)
    if boundary > min(left_end, right_first_start):
        return None
    if boundary <= left_start or boundary >= right_end:
        return None
    return boundary


def _reliable_fine_timestamps(
    entry: dict[str, Any], segment_start: int, segment_end: int
) -> list[tuple[int, int]] | None:
    timestamps = _timestamp_pairs(entry.get("timestamp"))
    tokens = _alignment_tokens(entry)
    if not tokens or len(tokens) != len(timestamps):
        return None

    units: list[tuple[int, int]] = []
    previous_end = segment_start
    for start, end in timestamps:
        start_ticks = _milliseconds_to_ticks(start)
        end_ticks = _milliseconds_to_ticks(end)
        if (
            start_ticks < segment_start
            or end_ticks > segment_end
            or start_ticks < previous_end
            or end_ticks <= start_ticks
        ):
            return None
        units.append((start_ticks, end_ticks))
        previous_end = end_ticks
    return units


def _timed_entries(raw: object, *, require_speaker: bool) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for result in raw:
        if not isinstance(result, dict):
            continue
        sentence_info = result.get("sentence_info")
        sentence_entries: list[dict[str, Any]] = []
        if sentence_info is not None and sentence_info != []:
            if not isinstance(sentence_info, list):
                raise _SentenceInfoError("FunASR sentence_info has an invalid structure")
            for item in sentence_info:
                if not isinstance(item, dict):
                    raise _SentenceInfoError("FunASR sentence_info contains a malformed item")
                preserved = _preserve_sentence_entry(item)
                if preserved is None:
                    raise _SentenceInfoError("FunASR sentence_info contains a malformed item")
                if require_speaker and _speaker(preserved.get("spk")) is None:
                    raise _SentenceInfoError("FunASR sentence_info has a missing speaker")
                sentence_entries.append(preserved)
        if sentence_entries:
            entries.extend(sentence_entries)
        else:
            fallback = _expand_fallback_entry(result)
            if require_speaker and any(_speaker(item.get("spk")) is None for item in fallback):
                raise _SentenceInfoError("FunASR result has a missing speaker")
            entries.extend(fallback)
    return entries


def _has_boundaries(entry: dict[str, Any]) -> bool:
    return _milliseconds(entry.get("start")) is not None and _milliseconds(entry.get("end")) is not None


def _preserve_sentence_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    text = _recognized_text(entry)
    if not isinstance(text, str) or not text.strip():
        return None
    start = _milliseconds(entry.get("start"))
    end = _milliseconds(entry.get("end"))
    timestamps = _timestamp_pairs(entry.get("timestamp"))
    synthesized_boundaries = start is None and end is None and bool(timestamps)
    if start is None and end is None and timestamps:
        start, end = timestamps[0][0], timestamps[-1][1]
    elif start is None or end is None:
        return None
    if start < 0 or end <= start:
        return None
    timestamp_value = entry.get("timestamp")
    if timestamp_value not in (None, []) and not timestamps:
        return None
    if synthesized_boundaries:
        return {**entry, "start": str(start), "end": str(end)}
    return entry


def _expand_fallback_entry(entry: dict[str, Any]) -> list[dict[str, Any]]:
    timestamps = _timestamp_pairs(entry.get("timestamp"))
    has_boundaries = _has_boundaries(entry)
    if not timestamps:
        return [entry] if has_boundaries else []

    bounded = {
        **entry,
        "start": entry.get("start", timestamps[0][0]),
        "end": entry.get("end", timestamps[-1][1]),
    }
    tokens = _alignment_tokens(entry)
    if len(tokens) != len(timestamps):
        return [] if not has_boundaries else [bounded]

    chunks: list[dict[str, Any]] = []
    start_index = 0
    for index in range(len(tokens)):
        duration = timestamps[index][1] - timestamps[start_index][0]
        token_count = index - start_index + 1
        should_split = index > start_index and (
            duration >= MAX_FALLBACK_SEGMENT_MILLISECONDS
            or token_count >= MAX_FALLBACK_SEGMENT_TOKENS
        )
        if should_split:
            chunks.append(_timestamp_chunk(bounded, tokens, timestamps, start_index, index + 1))
            start_index = index + 1
    if not chunks:
        return [bounded]
    if start_index < len(tokens):
        chunks.append(_timestamp_chunk(bounded, tokens, timestamps, start_index, len(tokens)))
    return chunks


def _alignment_tokens(entry: dict[str, Any]) -> list[str]:
    raw_text = entry.get("raw_text")
    if isinstance(raw_text, str) and raw_text.split():
        spaced_tokens = raw_text.split()
        if len(spaced_tokens) > 1:
            return spaced_tokens
        recognized_tokens = RECOGNITION_TOKEN.findall(raw_text)
        return recognized_tokens or spaced_tokens
    text = _recognized_text(entry)
    return RECOGNITION_TOKEN.findall(text) if isinstance(text, str) else []


def _timestamp_chunk(
    entry: dict[str, Any],
    tokens: list[str],
    timestamps: list[tuple[Decimal, Decimal]],
    start: int,
    end: int,
) -> dict[str, Any]:
    selected_tokens = tokens[start:end]
    selected_timestamps = timestamps[start:end]
    return {
        **entry,
        "text": _join_tokens(selected_tokens),
        "raw_text": " ".join(selected_tokens),
        "timestamp": [[str(begin), str(finish)] for begin, finish in selected_timestamps],
        "start": str(selected_timestamps[0][0]),
        "end": str(selected_timestamps[-1][1]),
    }


def _join_tokens(tokens: list[str]) -> str:
    text = ""
    previous = ""
    for token in tokens:
        if text and _is_word(previous) and _is_word(token):
            text += " "
        text += token
        previous = token
    return text


def _is_word(token: str) -> bool:
    return re.fullmatch(r"[\w-]+", token, re.UNICODE) is not None and re.fullmatch(
        r"[\u4e00-\u9fff]", token
    ) is None


def _recognized_text(entry: dict[str, Any]) -> object:
    for key in ("text", "text_tn", "sentence"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _fine_units(
    entry: dict[str, Any],
    segment_start: int,
    segment_end: int,
    *,
    original_segment_end: int,
    clamp_final_end: bool,
    is_last_segment: bool,
    source_duration_ticks: int,
) -> tuple[FineUnit, ...]:
    timestamps = _timestamp_pairs(entry.get("timestamp"))
    tokens = _alignment_tokens(entry)
    if not tokens or not timestamps:
        return ()
    if len(tokens) != len(timestamps):
        return ()
    units: list[FineUnit] = []
    previous_end = segment_start
    for index, (token, (start, end)) in enumerate(
        zip(tokens, timestamps, strict=True)
    ):
        start_ticks = _milliseconds_to_ticks(start)
        end_ticks = _milliseconds_to_ticks(end)
        if start_ticks < previous_end or end_ticks <= start_ticks:
            return ()
        if start_ticks < segment_start:
            return ()
        if end_ticks > segment_end:
            is_last_fine = index == len(timestamps) - 1
            # Independent fine-unit terminal tolerance: last segment already at source_duration,
            # only last fine unit overruns by <=10ms, start still before terminal end.
            is_terminal_fine_clamp = (
                is_last_segment
                and segment_end == source_duration_ticks
                and is_last_fine
                and start_ticks < segment_end
                and 0 < end_ticks - segment_end <= FUNASR_TIMESTAMP_QUANTUM_TICKS
            )
            if is_terminal_fine_clamp:
                if clamp_final_end and end_ticks > original_segment_end:
                    return ()
                end_ticks = segment_end
                if end_ticks <= start_ticks:
                    return ()
            else:
                is_safe_final_clamp = (
                    clamp_final_end
                    and is_last_fine
                    and end_ticks <= original_segment_end
                    and end_ticks - segment_end <= FUNASR_TIMESTAMP_QUANTUM_TICKS
                    and start_ticks < segment_end
                )
                if not is_safe_final_clamp:
                    return ()
                end_ticks = segment_end
                if end_ticks <= start_ticks:
                    return ()
        units.append(FineUnit("token", token, start_ticks, end_ticks, None))
        previous_end = end_ticks
    return tuple(units)


def _timestamp_pairs(value: object) -> list[tuple[Decimal, Decimal]]:
    if not isinstance(value, list):
        return []
    pairs: list[tuple[Decimal, Decimal]] = []
    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2:
            return []
        start = _milliseconds(pair[0])
        end = _milliseconds(pair[1])
        if start is None or end is None:
            return []
        pairs.append((start, end))
    return pairs


def _milliseconds(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _milliseconds_to_ticks(value: Decimal) -> int:
    ticks = value * TICKS_PER_SECOND / 1000
    return int(ticks.to_integral_value(rounding=ROUND_HALF_UP))


def _speaker(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return f"spk_{value}"
    if isinstance(value, str) and value and re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return f"spk_{value}"
    return None


def _speaker_enabled(parameters: dict[str, object]) -> bool:
    value = parameters.get("speaker_diarization")
    return isinstance(value, dict) and value.get("enabled") is True


def _confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
