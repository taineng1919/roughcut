"""Normalize Qwen Filetrans recognition JSON into timed transcript models.

The provider reports integer milliseconds; Roughcut time is
``TICKS_PER_SECOND = 120000``, so the frozen conversion is ``1 ms = 120 ticks``.
Provider sentences map onto the existing ``TranscriptSegment`` and provider
words onto the existing ``FineUnit``; no Qwen-specific transcript schema exists.

Every timing decision fails closed.  The S0 evidence does not establish a
provider timestamp quantum, so this normalizer applies no final-boundary
tolerance, no interpolation and no clamping.  The WP4 Cloud evidence does show
that a provider ``word`` can carry legal timing while its lexical text is
whitespace only: such a timing-only artifact publishes no ``FineUnit``, but its
timing is still validated and stays part of the monotonic sequence.  No provider
timing is ever silently dropped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from roughcut import __version__ as ROUGHCUT_CORE_VERSION
from roughcut.adapters.qwen import (
    BACKEND,
    CHANNEL_ID,
    CLOUD_AUDIO_PROFILE,
    LANGUAGE_HINTS,
    MODEL_NAME,
    REGION,
    TRANSPORT_MODE,
)
from roughcut.domain.time import TICKS_PER_SECOND
from roughcut.domain.transcript import (
    FineUnit,
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)

QWEN_TICKS_PER_MILLISECOND = TICKS_PER_SECOND // 1000
QWEN_LANGUAGE = "zh-CN"
PUNCTUATION_MAX_CHARS = 8
# The existing project-relative raw evidence boundary; provenance may name
# nothing else.
EVIDENCE_ROOT = "raw-asr"
_EVIDENCE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_REJECTED_LABEL = "<rejected>"


class QwenNormalizationError(ValueError):
    def __init__(self, message: str, raw_result_path: str) -> None:
        super().__init__(message)
        self.raw_result_path = raw_result_path


def normalize_qwen(
    raw: object,
    *,
    source_id: str,
    transcript_version_id: str,
    raw_result_path: str,
    source_duration_ticks: int,
    started_at: str = "unknown",
    completed_at: str = "unknown",
    exit_status: int = 0,
) -> TimedTranscript:
    """Normalize provider recognition JSON into the existing schema 1 transcript.

    Provenance is a closed mapping derived from the fixed Qwen Cloud identity
    plus this run's own validated facts.  No caller-supplied mapping is copied
    into the published transcript, so a credential, signed URL, ``oss://``
    locator, task locator or transport envelope cannot reach
    ``TimedTranscript.provenance`` through this function.
    """

    evidence_path = _evidence_path(raw_result_path, source_id)
    started = _run_timestamp(started_at, "started_at", evidence_path)
    completed = _run_timestamp(completed_at, "completed_at", evidence_path)
    run_status = _run_exit_status(exit_status, evidence_path)
    transcripts = _transcripts(raw, evidence_path)
    segments: list[TranscriptSegment] = []
    previous_end_ticks = 0
    for index, sentence in enumerate(
        _sentences(transcripts[0], raw_result_path), start=1
    ):
        start_ticks = _milliseconds(
            sentence.get("begin_time"), "sentence begin_time", raw_result_path
        )
        end_ticks = _milliseconds(
            sentence.get("end_time"), "sentence end_time", raw_result_path
        )
        if start_ticks < 0 or end_ticks <= start_ticks:
            raise QwenNormalizationError(
                "Qwen sentence has invalid bounds", raw_result_path
            )
        if start_ticks < previous_end_ticks:
            raise QwenNormalizationError(
                "Qwen sentences are not monotonic", raw_result_path
            )
        if end_ticks > source_duration_ticks:
            raise QwenNormalizationError(
                "Qwen sentence exceeds source duration", raw_result_path
            )
        text = _recognized_text(sentence.get("text"))
        if text is None:
            raise QwenNormalizationError("Qwen sentence has no text", raw_result_path)
        segments.append(
            TranscriptSegment(
                segment_id=f"seg_{index:06d}",
                start_ticks=start_ticks,
                end_ticks=end_ticks,
                original_text=text,
                corrected_text=None,
                local_speaker_id=_speaker(sentence.get("speaker_id"), raw_result_path),
                person_id=None,
                confidence=None,
                fine_units=_fine_units(
                    sentence,
                    segment_start=start_ticks,
                    segment_end=end_ticks,
                    raw_result_path=raw_result_path,
                ),
                editorial_mark="unmarked",
            )
        )
        previous_end_ticks = end_ticks

    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_version_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend=BACKEND,
            package_version=ROUGHCUT_CORE_VERSION,
            models={"asr": MODEL_NAME},
            parameters=_cloud_parameters(),
            raw_result_path=evidence_path,
            started_at=started,
            completed_at=completed,
            exit_status=run_status,
        ),
        language=QWEN_LANGUAGE,
        segments=tuple(segments),
    )


def _cloud_parameters() -> dict[str, object]:
    """The fixed, non-secret Cloud facts this adapter can evidence."""

    return {
        "transport": TRANSPORT_MODE,
        "region": REGION,
        "audio_profile": CLOUD_AUDIO_PROFILE,
        "language_hints": list(LANGUAGE_HINTS),
        "channel_id": [CHANNEL_ID],
    }


def _evidence_path(value: object, source_id: str) -> str:
    """Bind provenance to the canonical project-relative evidence identity.

    Mirroring the existing local ASR boundary, the only accepted evidence locator
    is ``raw-asr/<source_id>/<run_id>.json``; every component is re-derived from
    validated identifiers and returned canonically, so a caller cannot pass a
    locator or an arbitrary path through this field.
    """

    def rejected() -> QwenNormalizationError:
        # Never echo an unvalidated value: it may itself be a locator or secret.
        return QwenNormalizationError(
            "Qwen raw_result_path is not the canonical raw evidence path",
            _REJECTED_LABEL,
        )

    if not isinstance(source_id, str) or not _EVIDENCE_ID.fullmatch(source_id):
        raise rejected()
    if not isinstance(value, str) or not value or len(value) > 256:
        raise rejected()
    parts = PurePosixPath(value).parts
    if len(parts) != 3 or parts[0] != EVIDENCE_ROOT or parts[1] != source_id:
        raise rejected()
    run_id = parts[2][: -len(".json")] if parts[2].endswith(".json") else ""
    if not _EVIDENCE_ID.fullmatch(run_id):
        raise rejected()
    return f"{EVIDENCE_ROOT}/{source_id}/{run_id}.json"


def _run_timestamp(value: object, label: str, raw_result_path: str) -> str:
    """Accept an offset-aware ISO-8601 run timestamp, or the literal unknown."""

    if value == "unknown":
        return "unknown"
    if not isinstance(value, str):
        raise QwenNormalizationError(f"Qwen {label} is malformed", raw_result_path)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise QwenNormalizationError(
            f"Qwen {label} is malformed", raw_result_path
        ) from None
    if parsed.tzinfo is None:
        raise QwenNormalizationError(f"Qwen {label} is malformed", raw_result_path)
    return value


def _run_exit_status(value: object, raw_result_path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise QwenNormalizationError("Qwen exit_status is malformed", raw_result_path)
    return value


def _transcripts(raw: object, raw_result_path: str) -> list[Mapping[str, Any]]:
    if not isinstance(raw, Mapping):
        raise QwenNormalizationError("Qwen result must be an object", raw_result_path)
    transcripts = raw.get("transcripts")
    if not isinstance(transcripts, list) or not transcripts:
        raise QwenNormalizationError("Qwen result has no transcripts", raw_result_path)
    if len(transcripts) != 1:
        raise QwenNormalizationError(
            "Qwen result must contain exactly one transcript", raw_result_path
        )
    validated = [item for item in transcripts if isinstance(item, Mapping)]
    if len(validated) != len(transcripts):
        raise QwenNormalizationError("Qwen transcript is malformed", raw_result_path)
    return validated


def _sentences(
    transcript: Mapping[str, Any], raw_result_path: str
) -> list[Mapping[str, Any]]:
    sentences = transcript.get("sentences")
    if not isinstance(sentences, list) or not sentences:
        raise QwenNormalizationError(
            "Qwen transcript has no sentences", raw_result_path
        )
    validated = [item for item in sentences if isinstance(item, Mapping)]
    if len(validated) != len(sentences):
        raise QwenNormalizationError("Qwen sentence is malformed", raw_result_path)
    return validated


def _fine_units(
    sentence: Mapping[str, Any],
    *,
    segment_start: int,
    segment_end: int,
    raw_result_path: str,
) -> tuple[FineUnit, ...]:
    words = sentence.get("words")
    if not isinstance(words, list) or not words:
        raise QwenNormalizationError("Qwen sentence has no words", raw_result_path)
    units: list[FineUnit] = []
    previous_end_ticks = segment_start
    for word in words:
        if not isinstance(word, Mapping):
            raise QwenNormalizationError("Qwen word is malformed", raw_result_path)
        start_ticks = _milliseconds(
            word.get("begin_time"), "word begin_time", raw_result_path
        )
        end_ticks = _milliseconds(
            word.get("end_time"), "word end_time", raw_result_path
        )
        if end_ticks <= start_ticks:
            raise QwenNormalizationError("Qwen word has invalid bounds", raw_result_path)
        if start_ticks < previous_end_ticks:
            raise QwenNormalizationError(
                "Qwen words are not monotonic", raw_result_path
            )
        if start_ticks < segment_start or end_ticks > segment_end:
            raise QwenNormalizationError(
                "Qwen word is not contained in its sentence", raw_result_path
            )
        # Timing is fully validated now, so this word constrains the sequence
        # whether or not it ends up publishing lexical text.
        previous_end_ticks = end_ticks
        text = _word_text(word, raw_result_path)
        if text is None:
            # A real WP4 Cloud response reported words with legal integer
            # milliseconds but whitespace-only lexical text.  Such an artifact
            # has no publishable spoken evidence, so it yields no FineUnit; its
            # timing is provider evidence and stays in the monotonic sequence
            # above instead of being dropped.
            continue
        units.append(FineUnit("word", text, start_ticks, end_ticks, None))
    return tuple(units)


def _word_text(word: Mapping[str, Any], raw_result_path: str) -> str | None:
    """Return the publishable lexical text, or ``None`` for a timing-only artifact.

    The WP4 Cloud evidence shows a provider ``word`` whose ``text`` is whitespace
    only while its timing is legal.  Roughcut publishes spoken lexical evidence,
    so such an artifact yields no FineUnit, while its punctuation is still
    validated exactly as for a lexical word.  Anything that is not a ``str`` text
    remains malformed input that fails closed.
    """

    text = word.get("text")
    if not isinstance(text, str):
        raise QwenNormalizationError("Qwen word has no text", raw_result_path)
    punctuation = word.get("punctuation")
    if punctuation is None or punctuation == "":
        punctuation = ""
    elif not isinstance(punctuation, str) or len(punctuation) > PUNCTUATION_MAX_CHARS:
        raise QwenNormalizationError(
            "Qwen word punctuation is malformed", raw_result_path
        )
    elif any(character.isalnum() for character in punctuation):
        # Punctuation is appended to the word text, so a locator or credential
        # must never be able to reach a published FineUnit through this field.
        raise QwenNormalizationError(
            "Qwen word punctuation is malformed", raw_result_path
        )
    if not text.strip():
        # A non-lexical artifact has no spoken evidence to publish, and its
        # validated punctuation never becomes a unit of its own.
        return None
    return text + punctuation


def _recognized_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _milliseconds(value: object, label: str, raw_result_path: str) -> int:
    """Require the S0-frozen integer-millisecond provider timestamp.

    The evidence freezes integer milliseconds only, so a float or a numeric
    string is an unevidenced representation.  Coercing it would be guessing, so
    every other representation fails closed.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise QwenNormalizationError(f"Qwen {label} is malformed", raw_result_path)
    return value * QWEN_TICKS_PER_MILLISECOND


def _speaker(value: object, raw_result_path: str) -> str | None:
    """Map a legal provider diarization index; never guess an identity.

    S0 observed no Qwen ``speaker_id``, so only the index shape is accepted: it
    is the one shape that cannot also be a credential or locator.
    """

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QwenNormalizationError("Qwen speaker_id is malformed", raw_result_path)
    return f"spk_{value}"
