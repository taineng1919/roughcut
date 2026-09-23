"""Closed Multicam Alignment domain: schema 1 artifact, classification, and math."""

from __future__ import annotations

import math
import re
from copy import deepcopy
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import combinations
from typing import Any, Literal, cast

from roughcut.domain.errors import WorkflowError
from roughcut.domain.workflow import (
    canonical_sha256_v1,
    validate_safe_id,
    validate_sha256,
    validate_timestamp,
)

ALIGNMENT_SCHEMA_VERSION = 1
ALIGNMENT_TICKS_PER_SECOND = 120_000
ALIGNMENT_CANDIDATE_GROUP_DIAMETER_TICKS = 12_000
ALIGNMENT_VERIFICATION_WINDOW_TICKS = 1_440_000
ALIGNMENT_VERIFICATION_WINDOW_COUNT = 3
ALIGNMENT_MINIMUM_VERIFIED_OVERLAP_TICKS = 4_320_000
ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS = 12_000
ALIGNMENT_PROFILE_NAME = "roughcut_audalign_fixed_offset"
ALIGNMENT_PROFILE_VERSION = 2
ALIGNMENT_SUPPORTED_PROFILE_VERSIONS = frozenset({1, 2})
ALIGNMENT_MAPPING_MODEL = "fixed_offset_equal_speed"
AUDALIGN_VERSION = "1.3.1"
AUDALIGN_UPSTREAM_COMMIT = "d5955ae8a85b1cd480dadd005c3f88986f4ebbef"
AUDALIGN_ACCURACY = 2
AUDALIGN_NUM_PROCESSORS = 1
# New correlation fixed-offset family (production in 0.2.5): mono-only,
# three bounded 15 s probes at 20/50/80 % vs full main, serial, no L/R fallback.
AUDALIGN_CORRELATION_PROFILE_NAME = "roughcut_audalign_correlation_fixed_offset"
AUDALIGN_CORRELATION_PROFILE_VERSION = 1
AUDALIGN_CORRELATION_ALGORITHM_NAME = "audalign_correlation"
AUDALIGN_CORRELATION_ALGORITHM_VERSION = "1.3.1"
AUDALIGN_CORRELATION_UPSTREAM_COMMIT = AUDALIGN_UPSTREAM_COMMIT
AUDALIGN_CORRELATION_RECOGNIZER = "CorrelationRecognizer"
AUDALIGN_CORRELATION_EVIDENCE_FAMILY = "roughcut_audalign_correlation_evidence_v1"
AUDALIGN_CORRELATION_SAMPLE_RATE_HZ = 8000
AUDALIGN_CORRELATION_FFT_WINDOW_SIZE = 4096
AUDALIGN_CORRELATION_FILTER_MATCHES = 0.0
AUDALIGN_CORRELATION_FREQ_THRESHOLD = 200
AUDALIGN_CORRELATION_NORMALIZE = True
AUDALIGN_CORRELATION_PROBE_TICKS = 1_800_000
AUDALIGN_CORRELATION_PROBE_PERCENTS = (20, 50, 80)
AUDALIGN_CORRELATION_MAX_RAW_CANDIDATES_PER_PROBE = 512
WAVEFORM_ALGORITHM_NAME = "roughcut_waveform_cross_correlation"
WAVEFORM_ALGORITHM_VERSION = "1.0.0"
# Not a VCS commit: the waveform path ships inside Roughcut core itself. The
# literal marks "no external upstream"; it is never an Audalign commit.
WAVEFORM_ALGORITHM_UPSTREAM = "roughcut-core"
WAVEFORM_PROFILE_NAME = "roughcut_waveform_fixed_offset"
WAVEFORM_PROFILE_VERSION = 1
WAVEFORM_EVIDENCE_FAMILY = "roughcut_waveform_evidence_v1"
# Frozen coarse-envelope decode rule: FFmpeg streams full-speech-band stereo
# PCM at this rate; one RMS bin covers exactly coarse_bin_ticks of audio.
WAVEFORM_COARSE_DECODE_SAMPLE_RATE_HZ = 8_000
WAVEFORM_COARSE_BIN_TICKS = ALIGNMENT_TICKS_PER_SECOND
WAVEFORM_COARSE_RUNNER_UP_EXCLUSION_BINS = 2
WAVEFORM_COARSE_MINIMUM_OVERLAP_BINS = 4
WAVEFORM_COARSE_MINIMUM_SCORE = 0.20
WAVEFORM_COARSE_MINIMUM_SEPARATION = 0.05
# Short windows are decoded full-band at their own decode rate and folded
# into block-RMS values; the derived sequence rate is exact by construction.
WAVEFORM_SHORT_WINDOW_DECODE_SAMPLE_RATE_HZ = 8_000
WAVEFORM_SHORT_WINDOW_BLOCK_SAMPLES = 32
WAVEFORM_SHORT_WINDOW_DERIVED_SAMPLE_RATE_HZ = (
    WAVEFORM_SHORT_WINDOW_DECODE_SAMPLE_RATE_HZ
    // WAVEFORM_SHORT_WINDOW_BLOCK_SAMPLES
)
WAVEFORM_WINDOW_TICKS = 1_200_000
WAVEFORM_MAX_REFINEMENT_LAG_TICKS = 360_000
WAVEFORM_MINIMUM_CORRELATION = 0.35
WAVEFORM_WINDOW_COUNT = 3
BBC_ALGORITHM_NAME = "bbc_audio_offset_finder_roughcut_correlation"
BBC_ALGORITHM_VERSION = "0.5.5+roughcut.1"
BBC_ALGORITHM_UPSTREAM = "audio-offset-finder:0.5.5"
BBC_PROFILE_NAME = "roughcut_bbc_offset_refine_verify"
BBC_PROFILE_VERSION = 1
BBC_EVIDENCE_FAMILY = "roughcut_bbc_offset_evidence_v1"

# The single canonical waveform writer identity. A schema-2 alignment request
# hash is only defined over exactly this object; any other value is rejected
# before hashing.
WAVEFORM_WRITER_PROFILE: dict[str, object] = {
    "name": WAVEFORM_PROFILE_NAME,
    "version": WAVEFORM_PROFILE_VERSION,
    "algorithm": {
        "name": WAVEFORM_ALGORITHM_NAME,
        "version": WAVEFORM_ALGORITHM_VERSION,
        "upstream": WAVEFORM_ALGORITHM_UPSTREAM,
        "evidence_family": WAVEFORM_EVIDENCE_FAMILY,
    },
    "roles": {
        "main": "selected_main_source",
        "auxiliary": "selected_auxiliary_source",
    },
    "mapping_model": ALIGNMENT_MAPPING_MODEL,
    "ticks_per_second": ALIGNMENT_TICKS_PER_SECOND,
    "coarse_decode_sample_rate_hz": WAVEFORM_COARSE_DECODE_SAMPLE_RATE_HZ,
    "coarse_bin_ticks": WAVEFORM_COARSE_BIN_TICKS,
    "coarse_candidate_support_bins": (
        "at_least_half_of_the_smaller_envelope_side"
    ),
    "coarse_runner_up_exclusion_bins": WAVEFORM_COARSE_RUNNER_UP_EXCLUSION_BINS,
    "coarse_minimum_overlap_bins": WAVEFORM_COARSE_MINIMUM_OVERLAP_BINS,
    # canonical JSON forbids floating-point numbers; thresholds are frozen as
    # their exact decimal literals
    "coarse_minimum_score": "0.20",
    "coarse_minimum_separation": "0.05",
    "short_window_derived_values_per_second": (
        WAVEFORM_SHORT_WINDOW_DERIVED_SAMPLE_RATE_HZ
    ),
    "short_window_decode_sample_rate_hz": (
        WAVEFORM_SHORT_WINDOW_DECODE_SAMPLE_RATE_HZ
    ),
    "short_window_block_samples": WAVEFORM_SHORT_WINDOW_BLOCK_SAMPLES,
    "window_ticks": WAVEFORM_WINDOW_TICKS,
    "window_count": WAVEFORM_WINDOW_COUNT,
    "maximum_refinement_lag_ticks": WAVEFORM_MAX_REFINEMENT_LAG_TICKS,
    "minimum_correlation": "0.35",
    "maximum_local_error_ticks": ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS,
    "channel_schedule": {
        "coarse": "one_stereo_stream_shared_by_mono_left_right",
        "short_windows": "mono_then_left_then_right_only_after_failure",
    },
}

BBC_WRITER_PROFILE: dict[str, object] = {
    "name": BBC_PROFILE_NAME,
    "version": BBC_PROFILE_VERSION,
    "algorithm": {
        "name": BBC_ALGORITHM_NAME,
        "version": BBC_ALGORITHM_VERSION,
        "upstream": BBC_ALGORITHM_UPSTREAM,
        "evidence_family": BBC_EVIDENCE_FAMILY,
    },
    "provider": "bbc_audio_offset_finder",
    "provider_version": "0.5.5",
    "finder_parameters": "upstream_defaults_no_overrides",
    "roles": {
        "main": "selected_main_source",
        "auxiliary": "selected_auxiliary_source",
    },
    "mapping_model": ALIGNMENT_MAPPING_MODEL,
    "ticks_per_second": ALIGNMENT_TICKS_PER_SECOND,
    "native_offset_semantics": "main_tick_equals_auxiliary_tick_plus_b",
    "seconds_to_ticks": "decimal_nearest_ties_away_from_zero",
    "short_window_derived_values_per_second": (
        WAVEFORM_SHORT_WINDOW_DERIVED_SAMPLE_RATE_HZ
    ),
    "short_window_decode_sample_rate_hz": (
        WAVEFORM_SHORT_WINDOW_DECODE_SAMPLE_RATE_HZ
    ),
    "short_window_block_samples": WAVEFORM_SHORT_WINDOW_BLOCK_SAMPLES,
    "window_ticks": WAVEFORM_WINDOW_TICKS,
    "window_count": WAVEFORM_WINDOW_COUNT,
    "maximum_refinement_lag_ticks": WAVEFORM_MAX_REFINEMENT_LAG_TICKS,
    "minimum_correlation": "0.35",
    "maximum_local_error_ticks": ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS,
    "minimum_verified_overlap_ticks": ALIGNMENT_MINIMUM_VERIFIED_OVERLAP_TICKS,
    "verification_geometry": "recomputed_from_refined_b",
    "channel_schedule": "mono_then_left_then_right_only_after_failure",
}

AUDALIGN_CORRELATION_WRITER_PROFILE: dict[str, object] = {
    "name": AUDALIGN_CORRELATION_PROFILE_NAME,
    "version": AUDALIGN_CORRELATION_PROFILE_VERSION,
    "algorithm": {
        "name": AUDALIGN_CORRELATION_ALGORITHM_NAME,
        "version": AUDALIGN_CORRELATION_ALGORITHM_VERSION,
        "upstream": AUDALIGN_CORRELATION_UPSTREAM_COMMIT,
        "evidence_family": AUDALIGN_CORRELATION_EVIDENCE_FAMILY,
    },
    "provider": "audalign",
    "provider_version": AUDALIGN_VERSION,
    "upstream_commit": AUDALIGN_CORRELATION_UPSTREAM_COMMIT,
    "recognizer": AUDALIGN_CORRELATION_RECOGNIZER,
    "recognizer_config": {
        "sample_rate": AUDALIGN_CORRELATION_SAMPLE_RATE_HZ,
        "fft_window_size": AUDALIGN_CORRELATION_FFT_WINDOW_SIZE,
        "filter_matches": "0.0",
        "freq_threshold": AUDALIGN_CORRELATION_FREQ_THRESHOLD,
        "normalize": AUDALIGN_CORRELATION_NORMALIZE,
        "locality": None,
        "max_lags": None,
        "match_len_filter": None,
        "close_seconds_filter": None,
        "locality_filter_prop": None,
        "plot": False,
        "multiprocessing": True,
        "num_processors": None,
        "start_end": None,
        "start_end_against": None,
        "passthrough_args": {},
        "fail_on_decode_error": True,
        "cant_read_extensions": [".txt", ".md", ".pkf", ".py", ".pyc"],
        "cant_write_extensions": [".mov", ".mp4", ".m4a"],
        "DEFAULT_OVERLAP_RATIO": "0.5",
        "SCALING_16_BIT": 65536,
        "LOCALITY_OVERLAP_RATIO": "0.5",
        "DEFAULT_LOCALITY_FILTER_PROP": "0.6",
    },
    "roles": {
        "main": "selected_main_source",
        "auxiliary": "selected_auxiliary_source",
    },
    "mapping_model": ALIGNMENT_MAPPING_MODEL,
    "ticks_per_second": ALIGNMENT_TICKS_PER_SECOND,
    "decode": {
        "sample_rate_hz": 44100,
        "channels": 1,
        "sample_format": "pcm_s16le",
        "audio_stream": "0:a:0",
    },
    "probe": {
        "excerpt_ticks": AUDALIGN_CORRELATION_PROBE_TICKS,
        "percentages": list(AUDALIGN_CORRELATION_PROBE_PERCENTS),
        "max_raw_candidates": AUDALIGN_CORRELATION_MAX_RAW_CANDIDATES_PER_PROBE,
        "schedule": "floor(aux_duration_ticks*percentage/100) clamp to aux_duration_ticks-excerpt",
        "order": "20->50->80",
    },
    "source_relation_b": "main_excerpt_start(0) - auxiliary_excerpt_start + seconds_to_ticks(delta)",
    "admission": {
        "required_unique_probes": 2,
        "max_cluster_spread_ticks": ALIGNMENT_CANDIDATE_GROUP_DIAMETER_TICKS,
        "non_chaining": True,
        "representative": "first_member_in_fixed_probe_order_20_50_80",
    },
    "execution": "serial",
    "channel_schedule": "mono_only_no_fallback",
}


AUDALIGN_CORRELATION_WRITER_PROFILE_SHA256 = canonical_sha256_v1(
    AUDALIGN_CORRELATION_WRITER_PROFILE
)


def audalign_correlation_typed_config() -> dict[str, object]:
    """Return the recognizer config with the Python types used by Audalign.

    The writer profile remains the only literal identity.  The isolated worker
    receives this derived value from the parent and compares it with the
    constructed ``CorrelationRecognizer`` config before recognizing media.
    """
    raw = AUDALIGN_CORRELATION_WRITER_PROFILE["recognizer_config"]
    if not isinstance(raw, dict):
        raise TypeError("Audalign Correlation recognizer config is not an object")
    config = deepcopy(raw)
    for key in (
        "filter_matches",
        "DEFAULT_OVERLAP_RATIO",
        "LOCALITY_OVERLAP_RATIO",
        "DEFAULT_LOCALITY_FILTER_PROP",
    ):
        value = config.get(key)
        if not isinstance(value, str):
            raise TypeError(f"Audalign Correlation config {key} is not decimal text")
        config[key] = float(value)
    return config

INTERVAL_CLASSIFICATIONS = frozenset(
    {"mapped", "missing", "uncertain", "conflict"}
)
CAMERA_STATUSES = frozenset(
    {"complete", "partial", "omitted", "failed"}
)
ALIGNMENT_PER_CAMERA_ERROR_CODES = frozenset(
    {
        "auxiliary_probe_failed",
        "auxiliary_decode_failed",
        "auxiliary_recognition_failed",
        "auxiliary_verification_failed",
        "auxiliary_audio_stream_unsupported",
    }
)
SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

IntervalClassification = Literal["mapped", "missing", "uncertain", "conflict"]
CameraStatus = Literal["complete", "partial", "omitted", "failed"]


class AlignmentError(RuntimeError):
    """Closed alignment-domain failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _error(code: str, evidence: str) -> AlignmentError:
    return AlignmentError(
        code,
        f"Roughcut multicam alignment {evidence}",
    )


def seconds_to_ticks(seconds_text: str) -> int:
    """Convert an external decimal seconds string to ticks at 120000/s.

    Uses a single Decimal conversion with nearest, ties away from zero:
    positives floor(x + 0.5), negatives ceil(x - 0.5). No binary float
    intermediate rounding is allowed.
    """
    try:
        value = Decimal(seconds_text)
    except Exception as error:
        raise _error(
            "alignment_integrity_error",
            "rejected a non-decimal seconds value",
        ) from error
    scaled = value * Decimal(ALIGNMENT_TICKS_PER_SECOND)
    if scaled.is_nan() or not scaled.is_finite():
        raise _error(
            "alignment_integrity_error",
            "rejected a non-finite seconds value",
        )
    if scaled >= 0:
        return int(
            (scaled + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR)
        )
    return int(
        (scaled - Decimal("0.5")).to_integral_value(rounding=ROUND_CEILING)
    )


def _safe_id(value: object, *, field: str) -> str:
    try:
        return validate_safe_id(value, field=field)
    except WorkflowError as error:
        raise _error("alignment_integrity_error", f"rejected invalid {field}") from error


def _sha256(value: object, *, field: str) -> str:
    try:
        return validate_sha256(value, field=field)
    except WorkflowError as error:
        raise _error("alignment_integrity_error", f"rejected invalid {field}") from error


def _timestamp(value: object, *, field: str) -> str:
    try:
        return validate_timestamp(value, field=field)
    except WorkflowError as error:
        raise _error("alignment_integrity_error", f"rejected invalid {field}") from error


def _closed(
    value: object, fields: set[str], *, description: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise _error(
            "alignment_integrity_error",
            f"rejected non-closed {description}",
        )
    return cast(dict[str, Any], value)


def _string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error(
            "alignment_integrity_error",
            f"rejected invalid {field}",
        )
    return value


def _integer(value: object, *, field: str, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > 9_223_372_036_854_775_807
    ):
        raise _error(
            "alignment_integrity_error",
            f"rejected invalid {field}",
        )
    return value


def _boolean(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise _error(
            "alignment_integrity_error",
            f"rejected invalid {field}",
        )
    return value


def _signed_integer(value: object, *, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or abs(value) > 9_223_372_036_854_775_807
    ):
        raise _error(
            "alignment_integrity_error",
            f"rejected invalid {field}",
        )
    return value


def _number(value: object, *, field: str) -> float:
    if not isinstance(value, str) or not value:
        raise _error(
            "alignment_integrity_error",
            f"rejected invalid {field}",
        )
    try:
        number = float(value)
    except ValueError as error:
        raise _error(
            "alignment_integrity_error",
            f"rejected invalid {field}",
        ) from error
    if not math.isfinite(number):
        raise _error(
            "alignment_integrity_error",
            f"rejected non-finite {field}",
        )
    return number


@dataclass(frozen=True)
class AlignmentVerificationProfile:
    name: str
    version: int

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        supported = (
            self.name == ALIGNMENT_PROFILE_NAME
            and self.version in ALIGNMENT_SUPPORTED_PROFILE_VERSIONS
        ) or (
            self.name == WAVEFORM_PROFILE_NAME
            and self.version == WAVEFORM_PROFILE_VERSION
        ) or (
            self.name == BBC_PROFILE_NAME
            and self.version == BBC_PROFILE_VERSION
        ) or (
            self.name == AUDALIGN_CORRELATION_PROFILE_NAME
            and self.version == AUDALIGN_CORRELATION_PROFILE_VERSION
        )
        if not supported:
            raise _error(
                "alignment_integrity_error",
                "rejected an unsupported verification profile",
            )

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "version": self.version}

    @classmethod
    def from_dict(cls, value: object) -> AlignmentVerificationProfile:
        data = _closed(
            value,
            {"name", "version"},
            description="verification profile",
        )
        return cls(
            name=_string(data["name"], field="profile.name"),
            version=_integer(
                data["version"], field="profile.version", minimum=1
            ),
        )


@dataclass(frozen=True)
class AlignmentAlgorithm:
    """Closed three-family algorithm identity union.

    The historical Audalign fingerprint family serializes accuracy/num_processors
    with their real Audalign semantics. The waveform/BBC families and the new
    Audalign correlation family carry no ``accuracy``/``num_processors`` fields
    at all instead of reusing those Audalign-only semantics with fake values.
    """

    name: str
    version: str
    upstream_commit: str
    accuracy: int | None
    num_processors: int | None
    mapping_model: str
    ticks_per_second: int
    verification_profile: AlignmentVerificationProfile

    def __post_init__(self) -> None:
        self.validate()

    @property
    def _is_core_algorithm(self) -> bool:
        return self.name in {WAVEFORM_ALGORITHM_NAME, BBC_ALGORITHM_NAME}

    @property
    def _is_correlation_algorithm(self) -> bool:
        return self.name == AUDALIGN_CORRELATION_ALGORITHM_NAME

    @property
    def _is_fingerprint_algorithm(self) -> bool:
        return self.name == "audalign_fingerprint"

    def validate(self) -> None:
        if self._is_core_algorithm:
            expected_identity = (
                (WAVEFORM_ALGORITHM_VERSION, WAVEFORM_ALGORITHM_UPSTREAM)
                if self.name == WAVEFORM_ALGORITHM_NAME
                else (BBC_ALGORITHM_VERSION, BBC_ALGORITHM_UPSTREAM)
            )
            core_identity = (
                (self.version, self.upstream_commit) == expected_identity
                and self.accuracy is None
                and self.num_processors is None
            )
            if not core_identity:
                raise _error(
                    "alignment_integrity_error",
                    "rejected an unsupported core alignment algorithm",
                )
        elif self._is_correlation_algorithm:
            correlation_identity = (
                self.name == AUDALIGN_CORRELATION_ALGORITHM_NAME
                and self.version == AUDALIGN_CORRELATION_ALGORITHM_VERSION
                and self.upstream_commit == AUDALIGN_CORRELATION_UPSTREAM_COMMIT
                and self.accuracy is None
                and self.num_processors is None
            )
            if not correlation_identity:
                raise _error(
                    "alignment_integrity_error",
                    "rejected an unsupported Audalign correlation algorithm",
                )
        else:
            audalign_identity = (
                self.name == "audalign_fingerprint"
                and self.version == AUDALIGN_VERSION
                and self.upstream_commit == AUDALIGN_UPSTREAM_COMMIT
                and self.accuracy == AUDALIGN_ACCURACY
                and self.num_processors == AUDALIGN_NUM_PROCESSORS
            )
            if not audalign_identity:
                raise _error(
                    "alignment_integrity_error",
                    "rejected an unsupported alignment algorithm",
                )
        if (
            self.mapping_model != ALIGNMENT_MAPPING_MODEL
            or self.ticks_per_second != ALIGNMENT_TICKS_PER_SECOND
        ):
            raise _error(
                "alignment_integrity_error",
                "rejected an unsupported alignment mapping identity",
            )
        self.verification_profile.validate()
        if self._is_fingerprint_algorithm and self.verification_profile.name != ALIGNMENT_PROFILE_NAME:
            raise _error(
                "alignment_integrity_error",
                "rejected an Audalign algorithm with waveform evidence",
            )
        if self._is_correlation_algorithm and self.verification_profile.name != AUDALIGN_CORRELATION_PROFILE_NAME:
            raise _error(
                "alignment_integrity_error",
                "rejected an Audalign correlation algorithm with a non-correlation profile",
            )
        expected_profile = (
            WAVEFORM_PROFILE_NAME
            if self.name == WAVEFORM_ALGORITHM_NAME
            else BBC_PROFILE_NAME if self.name == BBC_ALGORITHM_NAME else None
        )
        if self._is_core_algorithm and self.verification_profile.name != expected_profile:
            raise _error(
                "alignment_integrity_error",
                "rejected a waveform algorithm with Audalign evidence",
            )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "version": self.version,
            "upstream_commit": self.upstream_commit,
            "mapping_model": self.mapping_model,
            "ticks_per_second": self.ticks_per_second,
            "verification_profile": self.verification_profile.to_dict(),
        }
        if self._is_fingerprint_algorithm:
            result["accuracy"] = self.accuracy
            result["num_processors"] = self.num_processors
        return result

    @classmethod
    def from_dict(cls, value: object) -> AlignmentAlgorithm:
        if not isinstance(value, dict):
            raise _error(
                "alignment_integrity_error",
                "rejected invalid alignment algorithm",
            )
        name = value.get("name")
        if not isinstance(name, str):
            raise _error(
                "alignment_integrity_error",
                "rejected invalid algorithm.name",
            )
        if name in {
            WAVEFORM_ALGORITHM_NAME,
            BBC_ALGORITHM_NAME,
            AUDALIGN_CORRELATION_ALGORITHM_NAME,
        }:
            data = _closed(
                value,
                {
                    "name",
                    "version",
                    "upstream_commit",
                    "mapping_model",
                    "ticks_per_second",
                    "verification_profile",
                },
                description="waveform alignment algorithm",
            )
            return cls(
                name=_string(data["name"], field="algorithm.name"),
                version=_string(data["version"], field="algorithm.version"),
                upstream_commit=_string(
                    data["upstream_commit"], field="algorithm.upstream_commit"
                ),
                accuracy=None,
                num_processors=None,
                mapping_model=_string(
                    data["mapping_model"], field="algorithm.mapping_model"
                ),
                ticks_per_second=_integer(
                    data["ticks_per_second"],
                    field="algorithm.ticks_per_second",
                    minimum=1,
                ),
                verification_profile=AlignmentVerificationProfile.from_dict(
                    data["verification_profile"]
                ),
            )
        data = _closed(
            value,
            {
                "name",
                "version",
                "upstream_commit",
                "accuracy",
                "num_processors",
                "mapping_model",
                "ticks_per_second",
                "verification_profile",
            },
            description="alignment algorithm",
        )
        return cls(
            name=_string(data["name"], field="algorithm.name"),
            version=_string(data["version"], field="algorithm.version"),
            upstream_commit=_string(
                data["upstream_commit"], field="algorithm.upstream_commit"
            ),
            accuracy=_integer(
                data["accuracy"], field="algorithm.accuracy", minimum=1
            ),
            num_processors=_integer(
                data["num_processors"],
                field="algorithm.num_processors",
                minimum=1,
            ),
            mapping_model=_string(
                data["mapping_model"], field="algorithm.mapping_model"
            ),
            ticks_per_second=_integer(
                data["ticks_per_second"],
                field="algorithm.ticks_per_second",
                minimum=1,
            ),
            verification_profile=AlignmentVerificationProfile.from_dict(
                data["verification_profile"]
            ),
        )


@dataclass(frozen=True)
class AlignmentCameraGroup:
    camera_id: str
    ordered_source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _safe_id(self.camera_id, field="camera_id")
        if not self.ordered_source_ids:
            raise _error(
                "alignment_integrity_error",
                "rejected an empty camera source group",
            )
        if len(self.ordered_source_ids) != len(set(self.ordered_source_ids)):
            raise _error(
                "alignment_integrity_error",
                "rejected duplicate source IDs in a camera group",
            )
        for source_id in self.ordered_source_ids:
            _safe_id(source_id, field="source_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "camera_id": self.camera_id,
            "ordered_source_ids": list(self.ordered_source_ids),
        }

    @classmethod
    def from_dict(cls, value: object) -> AlignmentCameraGroup:
        data = _closed(
            value,
            {"camera_id", "ordered_source_ids"},
            description="camera source group",
        )
        ordered = data["ordered_source_ids"]
        if not isinstance(ordered, list):
            raise _error(
                "alignment_integrity_error",
                "rejected invalid ordered_source_ids",
            )
        return cls(
            camera_id=_safe_id(data["camera_id"], field="camera_id"),
            ordered_source_ids=tuple(
                _safe_id(item, field="ordered_source_ids") for item in ordered
            ),
        )


@dataclass(frozen=True)
class AlignmentSourcePair:
    """One caller-declared exact main/auxiliary source relationship."""

    main_source_id: str
    auxiliary_source_id: str

    def __post_init__(self) -> None:
        _safe_id(self.main_source_id, field="source_pairs.main_source_id")
        _safe_id(
            self.auxiliary_source_id,
            field="source_pairs.auxiliary_source_id",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "main_source_id": self.main_source_id,
            "auxiliary_source_id": self.auxiliary_source_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> AlignmentSourcePair:
        data = _closed(
            value,
            {"main_source_id", "auxiliary_source_id"},
            description="source pair",
        )
        return cls(
            main_source_id=_safe_id(
                data["main_source_id"], field="source_pairs.main_source_id"
            ),
            auxiliary_source_id=_safe_id(
                data["auxiliary_source_id"],
                field="source_pairs.auxiliary_source_id",
            ),
        )


@dataclass(frozen=True)
class AlignmentRequestAuxiliaryGroup:
    """One normalized auxiliary request group and its optional exact pairs."""

    camera: AlignmentCameraGroup
    source_pairs: tuple[AlignmentSourcePair, ...] | None

    def to_dict(self) -> dict[str, object]:
        result = self.camera.to_dict()
        if self.source_pairs is not None:
            result["source_pairs"] = [pair.to_dict() for pair in self.source_pairs]
        return result


def parse_alignment_request_groups(
    main_camera: object,
    auxiliary_cameras: object,
) -> tuple[AlignmentCameraGroup, tuple[AlignmentRequestAuxiliaryGroup, ...]]:
    """Validate and normalize the closed request camera groups.

    `source_pairs` is intentionally request-only data. The artifact keeps the
    selected source groups, while the complete group declaration and exact
    pairs remain in the request hash.
    """
    main = AlignmentCameraGroup.from_dict(main_camera)
    if main.camera_id != "main":
        raise _error(
            "alignment_integrity_error",
            "rejected a main camera with a non-main ID",
        )
    if not isinstance(auxiliary_cameras, list):
        raise _error(
            "alignment_integrity_error",
            "rejected invalid auxiliary camera groups",
        )
    parsed: list[AlignmentRequestAuxiliaryGroup] = []
    seen_camera_ids: set[str] = set()
    main_ids = set(main.ordered_source_ids)
    for raw in auxiliary_cameras:
        if not isinstance(raw, dict):
            raise _error(
                "alignment_integrity_error",
                "rejected a non-object auxiliary camera group",
            )
        fields = set(raw)
        base_fields = {"camera_id", "ordered_source_ids"}
        if fields != base_fields and fields != base_fields | {"source_pairs"}:
            raise _error(
                "alignment_integrity_error",
                "rejected a non-closed auxiliary camera request group",
            )
        camera = AlignmentCameraGroup.from_dict(
            {
                "camera_id": raw["camera_id"],
                "ordered_source_ids": raw["ordered_source_ids"],
            }
        )
        if camera.camera_id == "main":
            raise _error(
                "alignment_integrity_error",
                "rejected an auxiliary camera named main",
            )
        if camera.camera_id in seen_camera_ids:
            raise _error(
                "alignment_integrity_error",
                "rejected duplicate auxiliary camera IDs",
            )
        seen_camera_ids.add(camera.camera_id)
        source_pairs: tuple[AlignmentSourcePair, ...] | None = None
        if "source_pairs" in raw:
            pairs_raw = raw["source_pairs"]
            if not isinstance(pairs_raw, list) or not pairs_raw:
                raise _error(
                    "alignment_integrity_error",
                    "rejected an empty or invalid source_pairs list",
                )
            parsed_pairs = tuple(
                AlignmentSourcePair.from_dict(item) for item in pairs_raw
            )
            pair_keys = [
                (pair.main_source_id, pair.auxiliary_source_id)
                for pair in parsed_pairs
            ]
            if len(pair_keys) != len(set(pair_keys)):
                raise _error(
                    "alignment_integrity_error",
                    "rejected duplicate source pairs",
                )
            auxiliary_ids = set(camera.ordered_source_ids)
            for pair in parsed_pairs:
                if pair.main_source_id not in main_ids:
                    raise _error(
                        "alignment_integrity_error",
                        "rejected a source pair with a main source outside the group",
                    )
                if pair.auxiliary_source_id not in auxiliary_ids:
                    raise _error(
                        "alignment_integrity_error",
                        "rejected a source pair with an auxiliary source outside the group",
                    )
            source_pairs = tuple(
                sorted(
                    parsed_pairs,
                    key=lambda pair: (
                        pair.main_source_id,
                        pair.auxiliary_source_id,
                    ),
                )
            )
        parsed.append(
            AlignmentRequestAuxiliaryGroup(
                camera=camera,
                source_pairs=source_pairs,
            )
        )
    if not parsed:
        raise _error(
            "alignment_integrity_error",
            "rejected a request without auxiliary cameras",
        )
    return main, tuple(parsed)


@dataclass(frozen=True)
class AlignmentSourceFingerprint:
    size: int
    mtime_ns: int
    sha256_head_tail: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _integer(self.size, field="fingerprint.size", minimum=0)
        _integer(self.mtime_ns, field="fingerprint.mtime_ns", minimum=0)
        if (
            not isinstance(self.sha256_head_tail, str)
            or len(self.sha256_head_tail) != 64
            or any(c not in "0123456789abcdef" for c in self.sha256_head_tail)
        ):
            raise _error(
                "alignment_integrity_error",
                "rejected invalid fingerprint.sha256_head_tail",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256_head_tail": self.sha256_head_tail,
        }

    @classmethod
    def from_dict(cls, value: object) -> AlignmentSourceFingerprint:
        data = _closed(
            value,
            {"size", "mtime_ns", "sha256_head_tail"},
            description="source fingerprint",
        )
        return cls(
            size=_integer(data["size"], field="fingerprint.size", minimum=0),
            mtime_ns=_integer(
                data["mtime_ns"], field="fingerprint.mtime_ns", minimum=0
            ),
            sha256_head_tail=_string(
                data["sha256_head_tail"],
                field="fingerprint.sha256_head_tail",
            ),
        )


@dataclass(frozen=True)
class AlignmentSourceBasis:
    camera_id: str
    source_id: str
    fingerprint: AlignmentSourceFingerprint
    duration_ticks: int

    def __post_init__(self) -> None:
        _safe_id(self.camera_id, field="camera_id")
        _safe_id(self.source_id, field="source_id")
        _integer(self.duration_ticks, field="duration_ticks", minimum=1)
        self.fingerprint.validate()

    def to_dict(self) -> dict[str, object]:
        return {
            "camera_id": self.camera_id,
            "source_id": self.source_id,
            "fingerprint": self.fingerprint.to_dict(),
            "duration_ticks": self.duration_ticks,
        }

    @classmethod
    def from_dict(cls, value: object) -> AlignmentSourceBasis:
        data = _closed(
            value,
            {"camera_id", "source_id", "fingerprint", "duration_ticks"},
            description="source basis",
        )
        return cls(
            camera_id=_safe_id(data["camera_id"], field="camera_id"),
            source_id=_safe_id(data["source_id"], field="source_id"),
            fingerprint=AlignmentSourceFingerprint.from_dict(
                data["fingerprint"]
            ),
            duration_ticks=_integer(
                data["duration_ticks"], field="duration_ticks", minimum=1
            ),
        )


@dataclass(frozen=True)
class AlignmentCamera:
    """One auxiliary camera group with its merged status/ticks/errors."""

    camera_id: str
    ordered_source_ids: tuple[str, ...]
    status: CameraStatus
    mapped_ticks: int
    missing_ticks: int
    uncertain_ticks: int
    conflict_ticks: int
    errors: tuple[AlignmentPerCameraError, ...]

    def __post_init__(self) -> None:
        _safe_id(self.camera_id, field="camera_id")
        if not self.ordered_source_ids:
            raise _error(
                "alignment_integrity_error",
                "rejected an empty camera source group",
            )
        if len(self.ordered_source_ids) != len(set(self.ordered_source_ids)):
            raise _error(
                "alignment_integrity_error",
                "rejected duplicate source IDs in a camera group",
            )
        for source_id in self.ordered_source_ids:
            _safe_id(source_id, field="source_id")
        if self.status not in CAMERA_STATUSES:
            raise _error(
                "alignment_integrity_error",
                "rejected an unknown camera status",
            )
        for field in (
            "mapped_ticks",
            "missing_ticks",
            "uncertain_ticks",
            "conflict_ticks",
        ):
            _integer(getattr(self, field), field=field, minimum=0)
        # a non-null error source_id must belong to this camera's own group
        owned = set(self.ordered_source_ids)
        for error in self.errors:
            if error.source_id is not None and error.source_id not in owned:
                raise _error(
                    "alignment_integrity_error",
                    "rejected a per-camera error for a foreign source",
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "camera_id": self.camera_id,
            "ordered_source_ids": list(self.ordered_source_ids),
            "status": self.status,
            "mapped_ticks": self.mapped_ticks,
            "missing_ticks": self.missing_ticks,
            "uncertain_ticks": self.uncertain_ticks,
            "conflict_ticks": self.conflict_ticks,
            "errors": [error.to_dict() for error in self.errors],
        }

    @classmethod
    def from_dict(cls, value: object) -> AlignmentCamera:
        data = _closed(
            value,
            {
                "camera_id",
                "ordered_source_ids",
                "status",
                "mapped_ticks",
                "missing_ticks",
                "uncertain_ticks",
                "conflict_ticks",
                "errors",
            },
            description="auxiliary camera",
        )
        ordered = data["ordered_source_ids"]
        if not isinstance(ordered, list):
            raise _error(
                "alignment_integrity_error",
                "rejected invalid ordered_source_ids",
            )
        errors_raw = data["errors"]
        if not isinstance(errors_raw, list):
            raise _error(
                "alignment_integrity_error",
                "rejected invalid camera errors",
            )
        return cls(
            camera_id=_safe_id(data["camera_id"], field="camera_id"),
            ordered_source_ids=tuple(
                _safe_id(item, field="ordered_source_ids") for item in ordered
            ),
            status=cast(CameraStatus, _string(data["status"], field="status")),
            mapped_ticks=_integer(
                data["mapped_ticks"], field="mapped_ticks", minimum=0
            ),
            missing_ticks=_integer(
                data["missing_ticks"], field="missing_ticks", minimum=0
            ),
            uncertain_ticks=_integer(
                data["uncertain_ticks"], field="uncertain_ticks", minimum=0
            ),
            conflict_ticks=_integer(
                data["conflict_ticks"], field="conflict_ticks", minimum=0
            ),
            errors=tuple(
                AlignmentPerCameraError.from_dict(item) for item in errors_raw
            ),
        )


@dataclass(frozen=True)
class AlignmentPerCameraError:
    """One stable per-camera error; source_id is string|null, never ""."""

    code: str
    source_id: str | None

    def __post_init__(self) -> None:
        if self.code not in ALIGNMENT_PER_CAMERA_ERROR_CODES:
            raise _error(
                "alignment_integrity_error",
                "rejected an unknown per-camera error code",
            )
        if self.source_id is not None:
            _safe_id(self.source_id, field="error.source_id")

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "source_id": self.source_id}

    @classmethod
    def from_dict(cls, value: object) -> AlignmentPerCameraError:
        data = _closed(
            value,
            {"code", "source_id"},
            description="per-camera error",
        )
        source_id = data["source_id"]
        if source_id is not None and not isinstance(source_id, str):
            raise _error(
                "alignment_integrity_error",
                "rejected an invalid per-camera error source",
            )
        return cls(
            code=_string(data["code"], field="error.code"),
            source_id=(
                _safe_id(source_id, field="error.source_id")
                if isinstance(source_id, str)
                else None
            ),
        )


_AUDALIGN_EVIDENCE_FIELDS = {
    "code",
    "raw_candidate_count",
    "matching_fingerprint_counts",
    "verification_window_count",
    "verification_profile",
    "max_local_offset_error_ticks",
}
_WAVEFORM_EVIDENCE_FIELDS = {
    "code",
    "coarse_peak",
    "coarse_runner_up",
    "coarse_peak_runner_up_separation",
    "refined_offset_ticks",
    "refined_correlation",
    "verification_windows",
    "verification_window_count",
    "verification_profile",
    "max_local_offset_error_ticks",
    "conflicting_b_ticks",
}
_BBC_EVIDENCE_FIELDS = {
    "code",
    "provider",
    "provider_version",
    "channel",
    "native_offset_seconds",
    "initial_b_ticks",
    "standard_score",
    "refined_b_ticks",
    "refined_correlation",
    "refined_geometry",
    "verification_windows",
    "verification_window_count",
    "verification_profile",
    "max_local_offset_error_ticks",
    "conflicting_b_ticks",
}
_BBC_EVIDENCE_CODES = frozenset(
    {
        "fixed_offset_verified",
        "fixed_offset_conflict",
        "finder_failed",
        "no_offset",
        "finder_import_failed",
        "finder_ffmpeg_unavailable",
        "finder_decode_failed",
        "finder_provider_failed",
        "finder_result_invalid",
        "finder_insufficient_audio",
        "initial_overlap_insufficient",
        "refinement_failed",
        "refined_overlap_insufficient",
        "verification_failed",
        "decode_failed",
    }
)
_WAVEFORM_EVIDENCE_CODES = frozenset(
    {
        "fixed_offset_verified",
        "fixed_offset_conflict",
        "coarse_ambiguous",
        "refinement_failed",
        "verification_failed",
        "no_candidate",
        "failed",
    }
)
_AUDALIGN_CORRELATION_EVIDENCE_FIELDS = {
    "code",
    "provider",
    "provider_version",
    "recognizer",
    "probe_records",
    "support_probe_count",
    "cluster_spread_ticks",
    "representative_b_ticks",
    "conflicting_b_ticks",
    "verification_profile",
}
_AUDALIGN_CORRELATION_EVIDENCE_CODES = frozenset(
    {
        "fixed_offset_verified",
        "fixed_offset_conflict",
        "insufficient_probes",
        "no_candidate",
        "probe_inconsistent",
        "worker_failed",
        "auxiliary_decode_failed",
    }
)


def _validate_audalign_evidence(
    evidence: dict[str, object],
    classification: IntervalClassification,
) -> None:
    if evidence["code"] not in {"fixed_offset_verified", "no_candidate", "failed"}:
        raise _error(
            "alignment_integrity_error",
            "rejected an unknown Audalign evidence code",
        )
    raw_count = _integer(
        evidence["raw_candidate_count"],
        field="evidence.raw_candidate_count",
        minimum=0,
    )
    counts = evidence["matching_fingerprint_counts"]
    if not isinstance(counts, list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0
        for item in counts
    ):
        raise _error(
            "alignment_integrity_error",
            "rejected invalid matching fingerprint counts",
        )
    window_count = _integer(
        evidence["verification_window_count"],
        field="evidence.verification_window_count",
        minimum=0,
    )
    if window_count not in {0, 3}:
        raise _error(
            "alignment_integrity_error",
            "rejected an unsupported Audalign verification window count",
        )
    AlignmentVerificationProfile.from_dict(evidence["verification_profile"])
    error_ticks = evidence["max_local_offset_error_ticks"]
    if window_count == 3:
        _integer(
            error_ticks,
            field="evidence.max_local_offset_error_ticks",
            minimum=0,
        )
    elif error_ticks is not None:
        raise _error(
            "alignment_integrity_error",
            "rejected a local error without three Audalign windows",
        )
    if raw_count != len(counts):
        raise _error(
            "alignment_integrity_error",
            "rejected evidence whose raw count differs from its matches",
        )
    if classification == "mapped":
        if (
            evidence["code"] != "fixed_offset_verified"
            or raw_count <= 0
            or window_count != 3
            or error_ticks is None
        ):
            raise _error(
                "alignment_integrity_error",
                "rejected incomplete Audalign mapped evidence",
            )
        mapped_error = _integer(
            error_ticks,
            field="evidence.max_local_offset_error_ticks",
            minimum=0,
        )
        if mapped_error > ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS:
            raise _error(
                "alignment_integrity_error",
                "rejected Audalign mapped evidence beyond the local error bound",
            )
    elif classification == "missing" or classification == "uncertain":
        if (
            evidence["code"] != "no_candidate"
            or raw_count != 0
            or counts
            or window_count != 0
            or error_ticks is not None
        ):
            raise _error(
                "alignment_integrity_error",
                "rejected non-mapped Audalign evidence that is not empty",
            )
    else:
        if (
            evidence["code"] == "no_candidate"
            or raw_count < 2
            or len(counts) < 2
            or window_count != 3
            or error_ticks is None
        ):
            raise _error(
                "alignment_integrity_error",
                "rejected Audalign conflict evidence without two candidates",
            )


def _validate_audalign_correlation_probe_record(value: object) -> dict[str, object]:
    data = _closed(
        value,
        {
            "percentage",
            "auxiliary_start_ticks",
            "auxiliary_end_ticks",
            "native_offset_seconds",
            "derived_b_ticks",
        },
        description="correlation probe record",
    )
    percentage = _integer(data["percentage"], field="probe.percentage", minimum=0)
    if percentage not in {20, 50, 80}:
        raise _error("alignment_integrity_error", "rejected invalid correlation probe percentage")
    aux_start = _integer(data["auxiliary_start_ticks"], field="probe.auxiliary_start_ticks", minimum=0)
    aux_end = _integer(data["auxiliary_end_ticks"], field="probe.auxiliary_end_ticks", minimum=0)
    if aux_end - aux_start != AUDALIGN_CORRELATION_PROBE_TICKS:
        raise _error("alignment_integrity_error", "rejected correlation probe window length")
    native = data["native_offset_seconds"]
    derived = data["derived_b_ticks"]
    if native is None:
        if derived is not None:
            raise _error("alignment_integrity_error", "rejected partial correlation probe B")
    else:
        native_text = _string(native, field="probe.native_offset_seconds") if isinstance(native, str) else None
        if native_text is None:
            raise _error("alignment_integrity_error", "rejected invalid correlation native offset")
        _number(native_text, field="probe.native_offset_seconds")
        if not isinstance(derived, int) or isinstance(derived, bool):
            raise _error("alignment_integrity_error", "rejected invalid correlation derived B")
        _signed_integer(derived, field="probe.derived_b_ticks")
        expected = -aux_start + seconds_to_ticks(native_text)
        if derived != expected:
            raise _error("alignment_integrity_error", "rejected inverted correlation B")
    return {
        "percentage": percentage,
        "auxiliary_start_ticks": aux_start,
        "auxiliary_end_ticks": aux_end,
        "native_offset_seconds": native,
        "derived_b_ticks": derived,
    }


def _validate_audalign_correlation_evidence(
    evidence: dict[str, object],
    classification: IntervalClassification,
) -> None:
    code = evidence["code"]
    if code not in _AUDALIGN_CORRELATION_EVIDENCE_CODES:
        raise _error("alignment_integrity_error", "rejected an unknown correlation evidence code")
    if evidence["provider"] != "audalign" or evidence["provider_version"] != AUDALIGN_VERSION:
        raise _error("alignment_integrity_error", "rejected correlation provider identity")
    if evidence["recognizer"] != AUDALIGN_CORRELATION_RECOGNIZER:
        raise _error("alignment_integrity_error", "rejected correlation recognizer identity")
    profile = AlignmentVerificationProfile.from_dict(evidence["verification_profile"])
    if profile.name != AUDALIGN_CORRELATION_PROFILE_NAME:
        raise _error("alignment_integrity_error", "rejected correlation evidence with a non-correlation profile")
    probe_records_raw = evidence["probe_records"]
    if not isinstance(probe_records_raw, list):
        raise _error("alignment_integrity_error", "rejected invalid correlation probe records")
    probe_records = [_validate_audalign_correlation_probe_record(item) for item in probe_records_raw]
    percentages = [cast(int, item["percentage"]) for item in probe_records]
    if len(percentages) != len(set(percentages)):
        raise _error("alignment_integrity_error", "rejected duplicate correlation probe percentages")
    if any(p not in {20, 50, 80} for p in percentages):
        raise _error("alignment_integrity_error", "rejected invalid correlation probe percentage")
    # Probe records must be in fixed order 20->50->80 prefix/subsequence
    order = {20: 0, 50: 1, 80: 2}
    sorted_pcts = sorted(percentages, key=lambda p: order[p])
    if percentages != sorted_pcts:
        raise _error("alignment_integrity_error", "rejected out-of-order correlation probe records")
    # Start positions must be unique for different percentages
    starts = [item["auxiliary_start_ticks"] for item in probe_records]
    if len(starts) != len(set(starts)):
        raise _error("alignment_integrity_error", "rejected duplicate correlation probe starts")
    # at most 3 probes
    if len(probe_records) > 3:
        raise _error("alignment_integrity_error", "rejected too many correlation probe records")
    # no matching_fingerprint_counts masquerade
    if "matching_fingerprint_counts" in evidence or "raw_candidate_count" in evidence:
        raise _error("alignment_integrity_error", "rejected correlation evidence masquerading as fingerprint")
    support = _integer(
        evidence["support_probe_count"],
        field="evidence.support_probe_count",
        minimum=0,
    )
    if support > 3:
        raise _error("alignment_integrity_error", "rejected excessive correlation support")
    spread_raw = evidence["cluster_spread_ticks"]
    representative = evidence["representative_b_ticks"]
    conflicts_raw = evidence["conflicting_b_ticks"]
    if not isinstance(conflicts_raw, list):
        raise _error("alignment_integrity_error", "rejected invalid correlation conflict offsets")
    conflicts = [_signed_integer(item, field="evidence.conflicting_b_ticks") for item in conflicts_raw]
    if len(conflicts) != len(set(conflicts)):
        raise _error("alignment_integrity_error", "rejected duplicate correlation conflict offsets")
    indexed_b_values = [
        (index, cast(int, rec["derived_b_ticks"]))
        for index, rec in enumerate(probe_records)
        if rec["derived_b_ticks"] is not None
    ]
    derived_b_values = [value for _index, value in indexed_b_values]

    qualifying_clusters: list[tuple[int, ...]] = []
    for size in range(len(indexed_b_values), 1, -1):
        for cluster in combinations(range(len(indexed_b_values)), size):
            values = [indexed_b_values[index][1] for index in cluster]
            if max(values) - min(values) > ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS:
                continue
            if any(set(cluster) < set(existing) for existing in qualifying_clusters):
                continue
            qualifying_clusters = [
                existing
                for existing in qualifying_clusters
                if not set(existing) < set(cluster)
            ]
            qualifying_clusters.append(cluster)
    # spread validation
    if spread_raw is None:
        spread = None
    else:
        spread = _integer(spread_raw, field="evidence.cluster_spread_ticks", minimum=0)
        if spread > ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS:
            raise _error("alignment_integrity_error", "rejected correlation spread beyond tolerance")
    # representative validation
    if representative is None:
        rep = None
    else:
        rep = _signed_integer(representative, field="evidence.representative_b_ticks")
        if rep not in derived_b_values:
            raise _error("alignment_integrity_error", "rejected correlation representative not in probe cluster")
    # per-code shape constraints
    if code in {"fixed_offset_verified", "fixed_offset_conflict"} and (
        percentages != [20, 50, 80]
    ):
        raise _error(
            "alignment_integrity_error",
            "rejected mapped or conflict correlation evidence without all probes",
        )
    if code == "fixed_offset_verified":
        if classification != "mapped":
            raise _error("alignment_integrity_error", "rejected verified correlation evidence on non-mapped interval")
        if support < 2 or support > 3:
            raise _error("alignment_integrity_error", "rejected correlation mapped evidence without two supports")
        if spread is None or spread > ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS:
            raise _error("alignment_integrity_error", "rejected correlation mapped evidence beyond spread")
        if rep is None or conflicts:
            raise _error("alignment_integrity_error", "rejected correlation mapped evidence with missing representative or conflicts")
        if len(qualifying_clusters) != 1:
            raise _error("alignment_integrity_error", "rejected ambiguous or chaining correlation cluster")
        winning = qualifying_clusters[0]
        cluster_bs = [indexed_b_values[index][1] for index in winning]
        if len(cluster_bs) != support or max(cluster_bs) - min(cluster_bs) != spread:
            raise _error("alignment_integrity_error", "rejected incorrect correlation cluster spread")
        # earliest fixed-order representative: the first probe in 20->50->80 that belongs to cluster
        earliest_b = indexed_b_values[winning[0]][1]
        if earliest_b != rep:
            raise _error("alignment_integrity_error", "rejected non-earliest correlation representative")
    elif code == "fixed_offset_conflict":
        if classification != "conflict":
            raise _error("alignment_integrity_error", "rejected correlation conflict evidence on non-conflict interval")
        if support < 2 or support > 3:
            raise _error("alignment_integrity_error", "rejected correlation conflict without two supports")
        if (
            len(conflicts) < 2
            or max(conflicts) - min(conflicts) <= ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS
        ):
            raise _error("alignment_integrity_error", "rejected correlation conflict without divergent B")
        if spread is None or len(qualifying_clusters) != 1:
            raise _error("alignment_integrity_error", "rejected incomplete correlation conflict cluster")
        winning = qualifying_clusters[0]
        cluster_bs = [indexed_b_values[index][1] for index in winning]
        if len(cluster_bs) != support or max(cluster_bs) - min(cluster_bs) != spread:
            raise _error("alignment_integrity_error", "rejected incorrect correlation conflict cluster spread")
        if rep is None or rep not in conflicts or rep != cluster_bs[0]:
            raise _error("alignment_integrity_error", "rejected correlation conflict without representative in conflicts")
    else:
        # uncertain codes
        if classification in {"mapped", "conflict"}:
            raise _error("alignment_integrity_error", "rejected uncertain correlation evidence on mapped/conflict interval")
        if code in {"fixed_offset_verified", "fixed_offset_conflict"}:
            raise _error("alignment_integrity_error", "rejected verified correlation evidence on uncertain interval")
        if spread is not None or rep is not None:
            raise _error("alignment_integrity_error", "rejected uncertain correlation evidence with representative")
        if conflicts:
            raise _error("alignment_integrity_error", "rejected uncertain correlation evidence with conflicts")
        if support != 0:
            raise _error("alignment_integrity_error", "rejected uncertain correlation evidence with support")
        if code == "insufficient_probes" and probe_records:
            raise _error("alignment_integrity_error", "rejected insufficient probes with records")
        if code == "probe_inconsistent" and (
            len(derived_b_values) < 2 or len(qualifying_clusters) == 1
        ):
            raise _error("alignment_integrity_error", "rejected invalid inconsistent correlation probes")
        if code == "worker_failed" and not any(
            rec["derived_b_ticks"] is None for rec in probe_records
        ):
            raise _error("alignment_integrity_error", "rejected insufficient probes with support")
        if code == "auxiliary_decode_failed" and not any(
            rec["native_offset_seconds"] is None and rec["derived_b_ticks"] is None
            for rec in probe_records
        ):
            raise _error(
                "alignment_integrity_error",
                "rejected auxiliary decode failure without a failed probe",
            )
    if classification == "missing" and code != "no_candidate":
        raise _error("alignment_integrity_error", "rejected missing correlation evidence with non-no_candidate code")


def _validate_waveform_point(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"lag_ticks", "score"}:
        raise _error(
            "alignment_integrity_error",
            f"rejected non-closed {field}",
        )
    score = _number(value["score"], field=f"{field}.score")
    # both coarse scores are normalized correlations on the shared envelope
    if not -1.0 <= score <= 1.0:
        raise _error(
            "alignment_integrity_error",
            f"rejected an out-of-range {field}.score",
        )
    return {
        "lag_ticks": _signed_integer(value["lag_ticks"], field=f"{field}.lag_ticks"),
        "score": value["score"],
    }


def _validate_waveform_window(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "b_ticks",
        "local_error_ticks",
        "correlation",
    }:
        raise _error(
            "alignment_integrity_error",
            f"rejected non-closed {field}",
        )
    correlation = _number(value["correlation"], field=f"{field}.correlation")
    if not -1.0 <= correlation <= 1.0:
        raise _error(
            "alignment_integrity_error",
            f"rejected an out-of-range {field}.correlation",
        )
    return {
        "b_ticks": _signed_integer(value["b_ticks"], field=f"{field}.b_ticks"),
        "local_error_ticks": _integer(
            value["local_error_ticks"],
            field=f"{field}.local_error_ticks",
            minimum=0,
        ),
        "correlation": value["correlation"],
    }


def _validate_waveform_evidence(
    evidence: dict[str, object],
    classification: IntervalClassification,
) -> None:
    code = evidence["code"]
    if code not in _WAVEFORM_EVIDENCE_CODES:
        raise _error(
            "alignment_integrity_error",
            "rejected an unknown waveform evidence code",
        )
    profile = AlignmentVerificationProfile.from_dict(evidence["verification_profile"])
    if profile.name != WAVEFORM_PROFILE_NAME:
        raise _error(
            "alignment_integrity_error",
            "rejected waveform evidence with an Audalign profile",
        )
    peak = evidence["coarse_peak"]
    runner_up = evidence["coarse_runner_up"]
    separation = evidence["coarse_peak_runner_up_separation"]
    refined_offset = evidence["refined_offset_ticks"]
    refined_correlation = evidence["refined_correlation"]
    windows_raw = evidence["verification_windows"]
    conflicts = evidence["conflicting_b_ticks"]
    window_count = _integer(
        evidence["verification_window_count"],
        field="evidence.verification_window_count",
        minimum=0,
    )
    if window_count > 3 or not isinstance(windows_raw, list):
        raise _error(
            "alignment_integrity_error",
            "rejected invalid waveform verification windows",
        )
    windows = [
        _validate_waveform_window(item, field="evidence.verification_windows")
        for item in windows_raw
    ]
    if len(windows) > 2:
        raise _error(
            "alignment_integrity_error",
            "rejected too many waveform verification windows",
        )
    if not isinstance(conflicts, list):
        raise _error(
            "alignment_integrity_error",
            "rejected invalid waveform conflict offsets",
        )
    conflict_ticks = [
        _signed_integer(item, field="evidence.conflicting_b_ticks")
        for item in conflicts
    ]
    if len(conflict_ticks) != len(set(conflict_ticks)):
        raise _error(
            "alignment_integrity_error",
            "rejected duplicate waveform conflict offsets",
        )
    if peak is None:
        if runner_up is not None or separation is not None:
            raise _error(
                "alignment_integrity_error",
                "rejected incomplete empty waveform coarse evidence",
            )
    else:
        _validate_waveform_point(peak, field="evidence.coarse_peak")
        if separation is None:
            raise _error(
                "alignment_integrity_error",
                "rejected waveform coarse evidence without a separation",
            )
        separation_number = _number(
            separation,
            field="evidence.coarse_peak_runner_up_separation",
        )
        if not 0.0 <= separation_number <= 2.0:
            raise _error(
                "alignment_integrity_error",
                "rejected an out-of-range waveform coarse separation",
            )
        if runner_up is None and separation_number != 0.0:
            raise _error(
                "alignment_integrity_error",
                "rejected a waveform separation without a runner-up candidate",
            )
        if runner_up is not None:
            _validate_waveform_point(runner_up, field="evidence.coarse_runner_up")
    if refined_offset is None:
        if refined_correlation is not None:
            raise _error(
                "alignment_integrity_error",
                "rejected a waveform correlation without an offset",
            )
    else:
        _signed_integer(refined_offset, field="evidence.refined_offset_ticks")
        if refined_correlation is None:
            raise _error(
                "alignment_integrity_error",
                "rejected a waveform offset without a correlation",
            )
        refined_number = _number(
            refined_correlation,
            field="evidence.refined_correlation",
        )
        if not -1.0 <= refined_number <= 1.0:
            raise _error(
                "alignment_integrity_error",
                "rejected an out-of-range waveform refined correlation",
            )
    expected_count = len(windows) + (1 if refined_offset is not None else 0)
    if window_count != expected_count:
        raise _error(
            "alignment_integrity_error",
            "rejected a waveform window count that differs from its evidence",
        )
    error_ticks = evidence["max_local_offset_error_ticks"]
    if windows:
        if error_ticks is None:
            raise _error(
                "alignment_integrity_error",
                "rejected waveform windows without a maximum local error",
            )
        max_error = _integer(
            error_ticks,
            field="evidence.max_local_offset_error_ticks",
            minimum=0,
        )
        window_errors = [
            cast(int, item["local_error_ticks"]) for item in windows
        ]
        if max_error != max(window_errors):
            raise _error(
                "alignment_integrity_error",
                "rejected an incorrect waveform maximum local error",
            )
    elif error_ticks is not None:
        raise _error(
            "alignment_integrity_error",
            "rejected a waveform local error without verification windows",
        )
    # closed per-code shape union: every non-terminal code carries exactly the
    # fields its producer stage can know, so a contradictory artifact such as
    # "no_candidate with a refined offset" cannot roundtrip
    if code in {"no_candidate", "failed"}:
        if (
            peak is not None
            or refined_offset is not None
            or windows
            or window_count != 0
            or error_ticks is not None
            or conflict_ticks
        ):
            raise _error(
                "alignment_integrity_error",
                f"rejected non-empty waveform evidence for code {code}",
            )
    elif code == "coarse_ambiguous":
        if (
            peak is None
            or refined_offset is not None
            or windows
            or window_count != 0
            or error_ticks is not None
            or conflict_ticks
        ):
            raise _error(
                "alignment_integrity_error",
                "rejected coarse_ambiguous waveform evidence with refinement facts",
            )
    elif code == "refinement_failed":
        if (
            peak is None
            or conflict_ticks
            or windows
            or window_count > 1
            or (window_count == 1) != (refined_offset is not None)
            or error_ticks is not None
        ):
            raise _error(
                "alignment_integrity_error",
                "rejected inconsistent refinement_failed waveform evidence",
            )
    elif code == "verification_failed" and (
        peak is None
        or refined_offset is None
        or conflict_ticks
        or window_count < 1
    ):
        raise _error(
            "alignment_integrity_error",
            "rejected incomplete verification_failed waveform evidence",
        )
    if classification == "mapped":
        if (
            code != "fixed_offset_verified"
            or peak is None
            or refined_offset is None
            or len(windows) != 2
            or window_count != 3
            or conflicts
        ):
            raise _error(
                "alignment_integrity_error",
                "rejected incomplete waveform mapped evidence",
            )
        if error_ticks is None:
            raise _error(
                "alignment_integrity_error",
                "rejected waveform mapped evidence without a local error",
            )
        mapped_max_error = _integer(
            error_ticks,
            field="evidence.max_local_offset_error_ticks",
            minimum=0,
        )
        if mapped_max_error > ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS:
            raise _error(
                "alignment_integrity_error",
                "rejected waveform mapped evidence beyond the local error bound",
            )
    elif classification == "conflict":
        if (
            code != "fixed_offset_conflict"
            or peak is None
            or refined_offset is None
            or len(windows) != 2
            or window_count != 3
            or len(conflict_ticks) < 2
            or not any(
                abs(item - conflict_ticks[0]) > ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS
                for item in conflict_ticks[1:]
            )
        ):
            raise _error(
                "alignment_integrity_error",
                "rejected incomplete waveform conflict evidence",
            )
    elif classification == "missing":
        if code != "no_candidate" or peak is not None or conflicts:
            raise _error(
                "alignment_integrity_error",
                "rejected waveform missing evidence that is not empty",
            )
    else:
        if code in {"fixed_offset_verified", "fixed_offset_conflict"}:
            raise _error(
                "alignment_integrity_error",
                "rejected verified waveform evidence on an uncertain interval",
            )


def _validate_bbc_geometry(value: object) -> dict[str, object]:
    data = _closed(
        value,
        {
            "main_start_ticks",
            "main_end_ticks",
            "auxiliary_start_ticks",
            "auxiliary_end_ticks",
        },
        description="BBC refined geometry",
    )
    normalized: dict[str, int] = {
        key: _integer(data[key], field=f"evidence.refined_geometry.{key}")
        for key in data
    }
    if (
        normalized["main_end_ticks"] <= normalized["main_start_ticks"]
        or normalized["auxiliary_end_ticks"]
        <= normalized["auxiliary_start_ticks"]
        or normalized["main_end_ticks"] - normalized["main_start_ticks"]
        != normalized["auxiliary_end_ticks"]
        - normalized["auxiliary_start_ticks"]
    ):
        raise _error(
            "alignment_integrity_error", "rejected invalid BBC refined geometry"
        )
    return cast(dict[str, object], normalized)


def _validate_bbc_window(value: object) -> dict[str, object]:
    data = _closed(
        value,
        {
            "label",
            "main_start_ticks",
            "main_end_ticks",
            "auxiliary_start_ticks",
            "auxiliary_end_ticks",
            "b_ticks",
            "local_error_ticks",
            "correlation",
        },
        description="BBC verification window",
    )
    label = _string(data["label"], field="evidence.window.label")
    if label not in {"opening", "middle", "ending"}:
        raise _error(
            "alignment_integrity_error", "rejected unknown BBC window label"
        )
    result: dict[str, object] = {
        "label": label,
        "main_start_ticks": _integer(
            data["main_start_ticks"], field="evidence.window.main_start_ticks"
        ),
        "main_end_ticks": _integer(
            data["main_end_ticks"], field="evidence.window.main_end_ticks"
        ),
        "auxiliary_start_ticks": _integer(
            data["auxiliary_start_ticks"], field="evidence.window.auxiliary_start_ticks"
        ),
        "auxiliary_end_ticks": _integer(
            data["auxiliary_end_ticks"], field="evidence.window.auxiliary_end_ticks"
        ),
        "b_ticks": _signed_integer(data["b_ticks"], field="evidence.window.b_ticks"),
        "local_error_ticks": _integer(
            data["local_error_ticks"], field="evidence.window.local_error_ticks"
        ),
        "correlation": data["correlation"],
    }
    correlation = _number(data["correlation"], field="evidence.window.correlation")
    if not -1.0 <= correlation <= 1.0:
        raise _error(
            "alignment_integrity_error", "rejected BBC window correlation"
        )
    if (
        cast(int, result["main_end_ticks"])
        - cast(int, result["main_start_ticks"])
        != WAVEFORM_WINDOW_TICKS
        or cast(int, result["auxiliary_end_ticks"])
        - cast(int, result["auxiliary_start_ticks"])
        != WAVEFORM_WINDOW_TICKS
    ):
        raise _error(
            "alignment_integrity_error", "rejected invalid BBC window geometry"
        )
    return result


def _bbc_geometry_windows(
    geometry: dict[str, object],
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None:
    start = cast(int, geometry["main_start_ticks"])
    end = cast(int, geometry["main_end_ticks"])
    if end - start < max(
        ALIGNMENT_MINIMUM_VERIFIED_OVERLAP_TICKS,
        WAVEFORM_WINDOW_TICKS * WAVEFORM_WINDOW_COUNT,
    ):
        return None
    opening = (start, start + WAVEFORM_WINDOW_TICKS)
    ending = (end - WAVEFORM_WINDOW_TICKS, end)
    middle_start = (start + end - WAVEFORM_WINDOW_TICKS) // 2
    middle = (middle_start, middle_start + WAVEFORM_WINDOW_TICKS)
    if not (opening[1] <= middle[0] and middle[1] <= ending[0]):
        return None
    return opening, middle, ending


def _validate_bbc_evidence(
    evidence: dict[str, object], classification: IntervalClassification
) -> None:
    code = evidence["code"]
    if code not in _BBC_EVIDENCE_CODES:
        raise _error("alignment_integrity_error", "rejected BBC evidence code")
    if (
        evidence["provider"] != "bbc_audio_offset_finder"
        or evidence["provider_version"] != "0.5.5"
    ):
        raise _error("alignment_integrity_error", "rejected BBC provider identity")
    profile = AlignmentVerificationProfile.from_dict(evidence["verification_profile"])
    if profile.name != BBC_PROFILE_NAME:
        raise _error("alignment_integrity_error", "rejected BBC evidence profile")
    channel = evidence["channel"]
    if channel is not None and (
        not isinstance(channel, str) or channel not in {"mono", "left", "right"}
    ):
        raise _error("alignment_integrity_error", "rejected BBC evidence channel")
    native = evidence["native_offset_seconds"]
    initial = evidence["initial_b_ticks"]
    score = evidence["standard_score"]
    if native is None:
        if initial is not None or score is not None:
            raise _error("alignment_integrity_error", "rejected partial BBC finder evidence")
    else:
        _number(native, field="evidence.native_offset_seconds")
        if seconds_to_ticks(cast(str, native)) != _signed_integer(
            initial, field="evidence.initial_b_ticks"
        ):
            raise _error("alignment_integrity_error", "rejected inverted BBC native B")
        _number(score, field="evidence.standard_score")
    refined = evidence["refined_b_ticks"]
    refined_correlation = evidence["refined_correlation"]
    geometry = evidence["refined_geometry"]
    if refined is None:
        if refined_correlation is not None or geometry is not None:
            raise _error("alignment_integrity_error", "rejected partial BBC refinement evidence")
    else:
        _signed_integer(refined, field="evidence.refined_b_ticks")
        correlation = _number(refined_correlation, field="evidence.refined_correlation")
        if not -1.0 <= correlation <= 1.0:
            raise _error("alignment_integrity_error", "rejected BBC refined correlation")
    normalized_geometry = None if geometry is None else _validate_bbc_geometry(geometry)
    windows_raw = evidence["verification_windows"]
    if not isinstance(windows_raw, list):
        raise _error("alignment_integrity_error", "rejected BBC verification windows")
    windows = [_validate_bbc_window(item) for item in windows_raw]
    count = _integer(
        evidence["verification_window_count"], field="evidence.verification_window_count"
    )
    if count != len(windows) or count > 3:
        raise _error("alignment_integrity_error", "rejected BBC verification window count")
    error_ticks = evidence["max_local_offset_error_ticks"]
    conflicts_raw = evidence["conflicting_b_ticks"]
    if not isinstance(conflicts_raw, list):
        raise _error("alignment_integrity_error", "rejected BBC conflict offsets")
    conflicts = [
        _signed_integer(item, field="evidence.conflicting_b_ticks")
        for item in conflicts_raw
    ]
    if windows:
        if normalized_geometry is None:
            raise _error(
                "alignment_integrity_error",
                "rejected BBC windows without refined geometry",
            )
        maximum = _integer(error_ticks, field="evidence.max_local_offset_error_ticks")
        if maximum != max(cast(int, item["local_error_ticks"]) for item in windows):
            raise _error("alignment_integrity_error", "rejected BBC maximum local error")
    elif error_ticks is not None:
        raise _error("alignment_integrity_error", "rejected BBC local error without windows")
    if normalized_geometry is not None and refined is not None:
        refined_ticks = cast(int, refined)
        if (
            cast(int, normalized_geometry["main_start_ticks"])
            - cast(int, normalized_geometry["auxiliary_start_ticks"])
            != refined_ticks
            or cast(int, normalized_geometry["main_end_ticks"])
            - cast(int, normalized_geometry["auxiliary_end_ticks"])
            != refined_ticks
        ):
            raise _error("alignment_integrity_error", "rejected BBC geometry for another B")
        expected_windows = _bbc_geometry_windows(normalized_geometry)
        if expected_windows is None:
            raise _error("alignment_integrity_error", "rejected infeasible BBC geometry")
        if [item["label"] for item in windows] != [
            "opening",
            "middle",
            "ending",
        ][: len(windows)]:
            raise _error(
                "alignment_integrity_error",
                "rejected non-prefix BBC verification windows",
            )
        for item, expected in zip(windows, expected_windows):
            if (
                cast(int, item["main_start_ticks"]) != expected[0]
                or cast(int, item["main_end_ticks"]) != expected[1]
                or cast(int, item["main_start_ticks"])
                - cast(int, item["auxiliary_start_ticks"])
                != refined_ticks
                or cast(int, item["local_error_ticks"])
                != abs(cast(int, item["b_ticks"]) - refined_ticks)
            ):
                raise _error(
                    "alignment_integrity_error",
                    "rejected BBC window outside refined geometry",
                )
    finder_complete = native is not None and initial is not None and score is not None
    no_refinement = (
        refined is None
        and refined_correlation is None
        and normalized_geometry is None
        and not windows
        and error_ticks is None
        and not conflicts
    )
    correlation_value = (
        None
        if refined_correlation is None
        else _number(refined_correlation, field="evidence.refined_correlation")
    )
    window_passes = [
        _number(item["correlation"], field="evidence.window.correlation")
        >= WAVEFORM_MINIMUM_CORRELATION
        and cast(int, item["local_error_ticks"])
        <= ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS
        for item in windows
    ]
    if code in {
        "finder_failed",
        "no_offset",
        "finder_import_failed",
        "finder_ffmpeg_unavailable",
        "finder_decode_failed",
        "finder_provider_failed",
        "finder_result_invalid",
        "finder_insufficient_audio",
    }:
        valid_shape = (
            native is None
            and initial is None
            and score is None
            and channel is None
            and no_refinement
        )
    elif code == "initial_overlap_insufficient":
        valid_shape = finder_complete and channel is None and no_refinement
    elif code == "refinement_failed":
        valid_shape = (
            finder_complete
            and channel is not None
            and normalized_geometry is None
            and not windows
            and error_ticks is None
            and not conflicts
            and refined is not None
            and correlation_value is not None
            and correlation_value < WAVEFORM_MINIMUM_CORRELATION
        )
    elif code == "refined_overlap_insufficient":
        valid_shape = (
            finder_complete
            and channel is not None
            and refined is not None
            and correlation_value is not None
            and correlation_value >= WAVEFORM_MINIMUM_CORRELATION
            and normalized_geometry is None
            and not windows
            and error_ticks is None
            and not conflicts
        )
    elif code == "decode_failed":
        valid_shape = (
            finder_complete
            and channel is not None
            and not conflicts
            and (
                no_refinement
                or (
                    refined is not None
                    and correlation_value is not None
                    and correlation_value >= WAVEFORM_MINIMUM_CORRELATION
                    and normalized_geometry is not None
                    and len(windows) <= 2
                    and all(window_passes)
                )
            )
        )
    elif code == "verification_failed":
        valid_shape = (
            finder_complete
            and channel is not None
            and refined is not None
            and correlation_value is not None
            and correlation_value >= WAVEFORM_MINIMUM_CORRELATION
            and normalized_geometry is not None
            and 1 <= len(windows) <= 3
            and not conflicts
            and all(window_passes[:-1])
            and not window_passes[-1]
        )
    else:
        valid_shape = True
    if not valid_shape:
        raise _error(
            "alignment_integrity_error", "rejected BBC evidence stage shape"
        )
    if classification == "mapped":
        if (
            code != "fixed_offset_verified"
            or not finder_complete
            or channel is None
            or refined is None
            or correlation_value is None
            or correlation_value < WAVEFORM_MINIMUM_CORRELATION
            or normalized_geometry is None
            or [item["label"] for item in windows] != ["opening", "middle", "ending"]
            or error_ticks is None
            or cast(int, error_ticks) > ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS
            or any(
                _number(item["correlation"], field="evidence.window.correlation")
                < WAVEFORM_MINIMUM_CORRELATION
                for item in windows
            )
            or conflicts
        ):
            raise _error("alignment_integrity_error", "rejected incomplete BBC mapped evidence")
    elif classification == "conflict":
        if (
            code != "fixed_offset_conflict"
            or not finder_complete
            or channel is None
            or refined is None
            or correlation_value is None
            or correlation_value < WAVEFORM_MINIMUM_CORRELATION
            or normalized_geometry is None
            or [item["label"] for item in windows]
            != ["opening", "middle", "ending"]
            or error_ticks is None
            or cast(int, error_ticks) > ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS
            or any(
                _number(item["correlation"], field="evidence.window.correlation")
                < WAVEFORM_MINIMUM_CORRELATION
                for item in windows
            )
            or len(conflicts) < 2
            or len(set(conflicts)) < 2
            or cast(int, refined) not in conflicts
            or max(conflicts) - min(conflicts)
            <= ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS
        ):
            raise _error("alignment_integrity_error", "rejected incomplete BBC conflict evidence")
    elif classification == "missing" and code != "no_offset":
        raise _error(
            "alignment_integrity_error", "rejected BBC evidence on missing interval"
        )
    elif classification == "uncertain" and code in {
        "fixed_offset_verified",
        "fixed_offset_conflict",
    }:
        raise _error("alignment_integrity_error", "rejected mapped BBC evidence on uncertain interval")


def _normalize_interval_evidence(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _error(
            "alignment_integrity_error",
            "rejected invalid interval evidence",
        )
    profile_raw = value.get("verification_profile")
    profile = AlignmentVerificationProfile.from_dict(profile_raw)
    fields = (
        _AUDALIGN_EVIDENCE_FIELDS
        if profile.name == ALIGNMENT_PROFILE_NAME
        else _AUDALIGN_CORRELATION_EVIDENCE_FIELDS
        if profile.name == AUDALIGN_CORRELATION_PROFILE_NAME
        else _BBC_EVIDENCE_FIELDS
        if profile.name == BBC_PROFILE_NAME
        else _WAVEFORM_EVIDENCE_FIELDS
    )
    data = _closed(value, fields, description="interval evidence")
    data["code"] = _string(data["code"], field="evidence.code")
    data["verification_profile"] = profile.to_dict()
    if profile.name == AUDALIGN_CORRELATION_PROFILE_NAME:
        data["provider"] = _string(data["provider"], field="evidence.provider")
        data["provider_version"] = _string(data["provider_version"], field="evidence.provider_version")
        data["recognizer"] = _string(data["recognizer"], field="evidence.recognizer")
        if not isinstance(data["probe_records"], list):
            raise _error("alignment_integrity_error", "rejected invalid correlation probe records")
        data["probe_records"] = [
            _validate_audalign_correlation_probe_record(item) for item in data["probe_records"]
        ]
        data["support_probe_count"] = _integer(
            data["support_probe_count"], field="evidence.support_probe_count", minimum=0
        )
        if data["cluster_spread_ticks"] is not None:
            data["cluster_spread_ticks"] = _integer(
                data["cluster_spread_ticks"], field="evidence.cluster_spread_ticks", minimum=0
            )
        if data["representative_b_ticks"] is not None:
            data["representative_b_ticks"] = _signed_integer(
                data["representative_b_ticks"], field="evidence.representative_b_ticks"
            )
        if not isinstance(data["conflicting_b_ticks"], list):
            raise _error("alignment_integrity_error", "rejected invalid correlation conflict offsets")
        data["conflicting_b_ticks"] = [
            _signed_integer(item, field="evidence.conflicting_b_ticks") for item in data["conflicting_b_ticks"]
        ]
    elif profile.name == ALIGNMENT_PROFILE_NAME:
        data["raw_candidate_count"] = _integer(
            data["raw_candidate_count"],
            field="evidence.raw_candidate_count",
            minimum=0,
        )
        counts = data["matching_fingerprint_counts"]
        if not isinstance(counts, list):
            raise _error(
                "alignment_integrity_error",
                "rejected invalid matching fingerprint counts",
            )
        data["matching_fingerprint_counts"] = [
            _integer(item, field="evidence.matching_fingerprint_counts", minimum=0)
            for item in counts
        ]
        data["verification_window_count"] = _integer(
            data["verification_window_count"],
            field="evidence.verification_window_count",
            minimum=0,
        )
        if data["max_local_offset_error_ticks"] is not None:
            data["max_local_offset_error_ticks"] = _integer(
                data["max_local_offset_error_ticks"],
                field="evidence.max_local_offset_error_ticks",
                minimum=0,
            )
    elif profile.name == WAVEFORM_PROFILE_NAME:
        if data["coarse_peak"] is not None:
            data["coarse_peak"] = _validate_waveform_point(
                data["coarse_peak"], field="evidence.coarse_peak"
            )
        if data["coarse_runner_up"] is not None:
            data["coarse_runner_up"] = _validate_waveform_point(
                data["coarse_runner_up"], field="evidence.coarse_runner_up"
            )
        if data["coarse_peak_runner_up_separation"] is not None:
            _number(
                data["coarse_peak_runner_up_separation"],
                field="evidence.coarse_peak_runner_up_separation",
            )
        if data["refined_offset_ticks"] is not None:
            data["refined_offset_ticks"] = _signed_integer(
                data["refined_offset_ticks"],
                field="evidence.refined_offset_ticks",
            )
        if data["refined_correlation"] is not None:
            _number(
                data["refined_correlation"],
                field="evidence.refined_correlation",
            )
        if not isinstance(data["verification_windows"], list):
            raise _error(
                "alignment_integrity_error",
                "rejected invalid waveform verification windows",
            )
        data["verification_windows"] = [
            _validate_waveform_window(item, field="evidence.verification_windows")
            for item in data["verification_windows"]
        ]
        data["verification_window_count"] = _integer(
            data["verification_window_count"],
            field="evidence.verification_window_count",
            minimum=0,
        )
        if data["max_local_offset_error_ticks"] is not None:
            data["max_local_offset_error_ticks"] = _integer(
                data["max_local_offset_error_ticks"],
                field="evidence.max_local_offset_error_ticks",
                minimum=0,
            )
        if not isinstance(data["conflicting_b_ticks"], list):
            raise _error(
                "alignment_integrity_error",
                "rejected invalid waveform conflict offsets",
            )
        data["conflicting_b_ticks"] = [
            _signed_integer(item, field="evidence.conflicting_b_ticks")
            for item in data["conflicting_b_ticks"]
        ]
    else:
        for field in ("native_offset_seconds", "standard_score", "refined_correlation"):
            if data[field] is not None:
                _number(data[field], field=f"evidence.{field}")
        for field in ("initial_b_ticks", "refined_b_ticks"):
            if data[field] is not None:
                data[field] = _signed_integer(data[field], field=f"evidence.{field}")
        if data["refined_geometry"] is not None:
            data["refined_geometry"] = _validate_bbc_geometry(
                data["refined_geometry"]
            )
        if not isinstance(data["verification_windows"], list):
            raise _error(
                "alignment_integrity_error", "rejected BBC verification windows"
            )
        data["verification_windows"] = [
            _validate_bbc_window(item) for item in data["verification_windows"]
        ]
        data["verification_window_count"] = _integer(
            data["verification_window_count"],
            field="evidence.verification_window_count",
        )
        if data["max_local_offset_error_ticks"] is not None:
            data["max_local_offset_error_ticks"] = _integer(
                data["max_local_offset_error_ticks"],
                field="evidence.max_local_offset_error_ticks",
            )
        if not isinstance(data["conflicting_b_ticks"], list):
            raise _error("alignment_integrity_error", "rejected BBC conflict offsets")
        data["conflicting_b_ticks"] = [
            _signed_integer(item, field="evidence.conflicting_b_ticks")
            for item in data["conflicting_b_ticks"]
        ]
    return data


@dataclass(frozen=True)
class AlignmentInterval:
    interval_id: str
    auxiliary_camera_id: str
    classification: IntervalClassification
    main: dict[str, object]
    auxiliary: dict[str, object] | None
    evidence: dict[str, object]

    def __post_init__(self) -> None:
        _safe_id(self.interval_id, field="interval_id")
        _safe_id(self.auxiliary_camera_id, field="auxiliary_camera_id")
        if self.classification not in INTERVAL_CLASSIFICATIONS:
            raise _error(
                "alignment_integrity_error",
                "rejected an unknown interval classification",
            )
        main = _closed(
            self.main,
            {"source_id", "start_ticks", "end_ticks"},
            description="main interval",
        )
        start = _integer(main["start_ticks"], field="main.start_ticks", minimum=0)
        end = _integer(main["end_ticks"], field="main.end_ticks", minimum=0)
        if end <= start:
            raise _error(
                "alignment_integrity_error",
                "rejected a main interval with no duration",
            )
        if self.classification == "mapped":
            if self.auxiliary is None:
                raise _error(
                    "alignment_integrity_error",
                    "rejected a mapped interval without an auxiliary side",
                )
            auxiliary = _closed(
                self.auxiliary,
                {"source_id", "start_ticks", "end_ticks"},
                description="auxiliary interval",
            )
            aux_start = _integer(
                auxiliary["start_ticks"],
                field="auxiliary.start_ticks",
                minimum=0,
            )
            aux_end = _integer(
                auxiliary["end_ticks"],
                field="auxiliary.end_ticks",
                minimum=0,
            )
            if aux_end <= aux_start:
                raise _error(
                    "alignment_integrity_error",
                    "rejected an auxiliary interval with no duration",
                )
            if (aux_end - aux_start) != (end - start):
                raise _error(
                    "alignment_integrity_error",
                    "rejected a mapped interval with unequal durations",
                )
        elif self.auxiliary is not None:
            raise _error(
                "alignment_integrity_error",
                "rejected an auxiliary side on a non-mapped interval",
            )
        evidence = _normalize_interval_evidence(self.evidence)
        profile = AlignmentVerificationProfile.from_dict(
            evidence["verification_profile"]
        )
        if profile.name == ALIGNMENT_PROFILE_NAME:
            _validate_audalign_evidence(evidence, self.classification)
        elif profile.name == AUDALIGN_CORRELATION_PROFILE_NAME:
            _validate_audalign_correlation_evidence(evidence, self.classification)
        elif profile.name == WAVEFORM_PROFILE_NAME:
            _validate_waveform_evidence(evidence, self.classification)
        else:
            _validate_bbc_evidence(evidence, self.classification)

    def to_dict(self) -> dict[str, object]:
        return {
            "interval_id": self.interval_id,
            "auxiliary_camera_id": self.auxiliary_camera_id,
            "classification": self.classification,
            "main": dict(self.main),
            "auxiliary": (
                None if self.auxiliary is None else dict(self.auxiliary)
            ),
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, value: object) -> AlignmentInterval:
        data = _closed(
            value,
            {
                "interval_id",
                "auxiliary_camera_id",
                "classification",
                "main",
                "auxiliary",
                "evidence",
            },
            description="alignment interval",
        )
        main_raw = data["main"]
        auxiliary_raw = data["auxiliary"]
        if not isinstance(main_raw, dict):
            raise _error(
                "alignment_integrity_error",
                "rejected invalid main interval",
            )
        parsed_main = _closed(
            main_raw,
            {"source_id", "start_ticks", "end_ticks"},
            description="main interval",
        )
        parsed_main["source_id"] = _safe_id(
            parsed_main["source_id"], field="main.source_id"
        )
        parsed_main["start_ticks"] = _integer(
            parsed_main["start_ticks"], field="main.start_ticks", minimum=0
        )
        parsed_main["end_ticks"] = _integer(
            parsed_main["end_ticks"], field="main.end_ticks", minimum=0
        )
        parsed_aux: dict[str, object] | None = None
        if auxiliary_raw is not None:
            if not isinstance(auxiliary_raw, dict):
                raise _error(
                    "alignment_integrity_error",
                    "rejected invalid auxiliary interval",
                )
            parsed_aux = _closed(
                auxiliary_raw,
                {"source_id", "start_ticks", "end_ticks"},
                description="auxiliary interval",
            )
            parsed_aux["source_id"] = _safe_id(
                parsed_aux["source_id"], field="auxiliary.source_id"
            )
            parsed_aux["start_ticks"] = _integer(
                parsed_aux["start_ticks"],
                field="auxiliary.start_ticks",
                minimum=0,
            )
            parsed_aux["end_ticks"] = _integer(
                parsed_aux["end_ticks"],
                field="auxiliary.end_ticks",
                minimum=0,
            )
        evidence_raw = data["evidence"]
        if not isinstance(evidence_raw, dict):
            raise _error(
                "alignment_integrity_error",
                "rejected invalid interval evidence",
            )
        evidence = _normalize_interval_evidence(evidence_raw)
        return cls(
            interval_id=_safe_id(data["interval_id"], field="interval_id"),
            auxiliary_camera_id=_safe_id(
                data["auxiliary_camera_id"], field="auxiliary_camera_id"
            ),
            classification=cast(
                IntervalClassification,
                _string(data["classification"], field="classification"),
            ),
            main=parsed_main,
            auxiliary=parsed_aux,
            evidence=evidence,
        )


@dataclass(frozen=True)
class AlignmentProjectionSegment:
    """One exact alignment interval projected into a Decision clip.

    This primitive deliberately contains no renderer quotas, output filenames,
    codec settings, or generated media. Both NLE handoff and the existing
    parallel-camera output consume this same interval mapping.
    """

    interval_id: str
    camera_id: str
    classification: IntervalClassification
    main_source_id: str
    main_start_ticks: int
    main_end_ticks: int
    auxiliary_source_id: str | None
    auxiliary_start_ticks: int | None
    auxiliary_end_ticks: int | None
    timeline_start_ticks: int
    timeline_end_ticks: int

    def __post_init__(self) -> None:
        _safe_id(self.interval_id, field="projection.interval_id")
        _safe_id(self.camera_id, field="projection.camera_id")
        _safe_id(self.main_source_id, field="projection.main_source_id")
        if self.classification not in INTERVAL_CLASSIFICATIONS:
            raise _error(
                "alignment_integrity_error",
                "rejected an unknown projected interval classification",
            )
        for name, value in (
            ("main_start_ticks", self.main_start_ticks),
            ("timeline_start_ticks", self.timeline_start_ticks),
        ):
            _integer(value, field=f"projection.{name}", minimum=0)
        for name, value in (
            ("main_end_ticks", self.main_end_ticks),
            ("timeline_end_ticks", self.timeline_end_ticks),
        ):
            _integer(value, field=f"projection.{name}", minimum=1)
        if self.main_end_ticks <= self.main_start_ticks:
            raise _error("alignment_integrity_error", "projected main interval is empty")
        if self.timeline_end_ticks <= self.timeline_start_ticks:
            raise _error("alignment_integrity_error", "projected timeline interval is empty")
        if self.main_end_ticks - self.main_start_ticks != self.timeline_end_ticks - self.timeline_start_ticks:
            raise _error("alignment_integrity_error", "projected interval durations differ")
        if self.classification == "mapped":
            if (
                self.auxiliary_source_id is None
                or self.auxiliary_start_ticks is None
                or self.auxiliary_end_ticks is None
            ):
                raise _error("alignment_integrity_error", "mapped projection has no auxiliary range")
            _safe_id(self.auxiliary_source_id, field="projection.auxiliary_source_id")
            _integer(self.auxiliary_start_ticks, field="projection.auxiliary_start_ticks", minimum=0)
            _integer(self.auxiliary_end_ticks, field="projection.auxiliary_end_ticks", minimum=1)
            if self.auxiliary_end_ticks <= self.auxiliary_start_ticks:
                raise _error("alignment_integrity_error", "projected auxiliary interval is empty")
            if self.auxiliary_end_ticks - self.auxiliary_start_ticks != self.main_end_ticks - self.main_start_ticks:
                raise _error("alignment_integrity_error", "projected auxiliary duration differs")
        elif any(
            value is not None
            for value in (
                self.auxiliary_source_id,
                self.auxiliary_start_ticks,
                self.auxiliary_end_ticks,
            )
        ):
            raise _error("alignment_integrity_error", "unmapped projection has an auxiliary range")


def project_alignment_intervals(
    *,
    camera_id: str,
    intervals: tuple[AlignmentInterval, ...],
    main_source_id: str,
    main_start_ticks: int,
    main_end_ticks: int,
    timeline_start_ticks: int,
) -> tuple[AlignmentProjectionSegment, ...]:
    """Project one Decision source range using only exact artifact intervals."""

    _safe_id(camera_id, field="projection.camera_id")
    _safe_id(main_source_id, field="projection.main_source_id")
    _integer(main_start_ticks, field="projection.main_start_ticks", minimum=0)
    _integer(main_end_ticks, field="projection.main_end_ticks", minimum=1)
    _integer(timeline_start_ticks, field="projection.timeline_start_ticks", minimum=0)
    if main_end_ticks <= main_start_ticks:
        raise _error("alignment_integrity_error", "projection source range is empty")
    relevant = sorted(
        (
            interval
            for interval in intervals
            if interval.auxiliary_camera_id == camera_id
            and cast(str, interval.main["source_id"]) == main_source_id
            and cast(int, interval.main["end_ticks"]) > main_start_ticks
            and cast(int, interval.main["start_ticks"]) < main_end_ticks
        ),
        key=lambda interval: (
            cast(int, interval.main["start_ticks"]),
            cast(int, interval.main["end_ticks"]),
            interval.interval_id,
        ),
    )
    position = main_start_ticks
    result: list[AlignmentProjectionSegment] = []
    for interval in relevant:
        interval_start = cast(int, interval.main["start_ticks"])
        interval_end = cast(int, interval.main["end_ticks"])
        start = max(position, main_start_ticks, interval_start)
        end = min(main_end_ticks, interval_end)
        if end <= start:
            continue
        if start != position:
            raise _error(
                "alignment_integrity_error",
                "projection interval partition has a gap or overlap",
            )
        timeline_start = timeline_start_ticks + start - main_start_ticks
        timeline_end = timeline_start_ticks + end - main_start_ticks
        auxiliary_source_id: str | None = None
        auxiliary_start: int | None = None
        auxiliary_end: int | None = None
        if interval.classification == "mapped":
            raw_auxiliary = interval.auxiliary
            if raw_auxiliary is None:
                raise _error("alignment_integrity_error", "mapped interval lacks auxiliary data")
            auxiliary_source_id = cast(str, raw_auxiliary["source_id"])
            auxiliary_start = cast(int, raw_auxiliary["start_ticks"]) + start - interval_start
            auxiliary_end = auxiliary_start + end - start
        result.append(
            AlignmentProjectionSegment(
                interval_id=interval.interval_id,
                camera_id=camera_id,
                classification=interval.classification,
                main_source_id=main_source_id,
                main_start_ticks=start,
                main_end_ticks=end,
                auxiliary_source_id=auxiliary_source_id,
                auxiliary_start_ticks=auxiliary_start,
                auxiliary_end_ticks=auxiliary_end,
                timeline_start_ticks=timeline_start,
                timeline_end_ticks=timeline_end,
            )
        )
        position = end
    if position != main_end_ticks:
        raise _error(
            "alignment_integrity_error",
            "projection interval partition does not cover the Decision range",
        )
    return tuple(result)


@dataclass(frozen=True)
class AlignmentSummary:
    total_main_ticks: int
    camera_count: int
    mapped_ticks: int
    missing_ticks: int
    uncertain_ticks: int
    conflict_ticks: int

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for field in (
            "total_main_ticks",
            "camera_count",
            "mapped_ticks",
            "missing_ticks",
            "uncertain_ticks",
            "conflict_ticks",
        ):
            _integer(getattr(self, field), field=field, minimum=0)
        if (
            self.mapped_ticks
            + self.missing_ticks
            + self.uncertain_ticks
            + self.conflict_ticks
            != self.total_main_ticks * self.camera_count
        ):
            raise _error(
                "alignment_integrity_error",
                "rejected a summary that violates the camera-ticks equation",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "total_main_ticks": self.total_main_ticks,
            "camera_count": self.camera_count,
            "mapped_ticks": self.mapped_ticks,
            "missing_ticks": self.missing_ticks,
            "uncertain_ticks": self.uncertain_ticks,
            "conflict_ticks": self.conflict_ticks,
        }

    @classmethod
    def from_dict(cls, value: object) -> AlignmentSummary:
        data = _closed(
            value,
            {
                "total_main_ticks",
                "camera_count",
                "mapped_ticks",
                "missing_ticks",
                "uncertain_ticks",
                "conflict_ticks",
            },
            description="alignment summary",
        )
        return cls(
            total_main_ticks=_integer(
                data["total_main_ticks"],
                field="total_main_ticks",
                minimum=0,
            ),
            camera_count=_integer(
                data["camera_count"], field="camera_count", minimum=0
            ),
            mapped_ticks=_integer(
                data["mapped_ticks"], field="mapped_ticks", minimum=0
            ),
            missing_ticks=_integer(
                data["missing_ticks"], field="missing_ticks", minimum=0
            ),
            uncertain_ticks=_integer(
                data["uncertain_ticks"], field="uncertain_ticks", minimum=0
            ),
            conflict_ticks=_integer(
                data["conflict_ticks"], field="conflict_ticks", minimum=0
            ),
        )


@dataclass(frozen=True)
class MulticamAlignmentArtifact:
    alignment_id: str
    project_id: str
    producer_operation_id: str
    created_at: str
    request_hash: str
    input_hash: str
    algorithm: AlignmentAlgorithm
    main_camera: AlignmentCameraGroup
    auxiliary_cameras: tuple[AlignmentCamera, ...]
    source_basis: tuple[AlignmentSourceBasis, ...]
    intervals: tuple[AlignmentInterval, ...]
    summary: AlignmentSummary
    schema_version: int = ALIGNMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ALIGNMENT_SCHEMA_VERSION:
            raise _error(
                "alignment_integrity_error",
                "rejected an unsupported alignment artifact schema",
            )
        _safe_id(self.alignment_id, field="alignment_id")
        _safe_id(self.project_id, field="project_id")
        _safe_id(self.producer_operation_id, field="producer_operation_id")
        _sha256(self.request_hash, field="request_hash")
        _sha256(self.input_hash, field="input_hash")
        _timestamp(self.created_at, field="created_at")
        self.algorithm.validate()
        if not self.main_camera.ordered_source_ids:
            raise _error(
                "alignment_integrity_error",
                "rejected an empty main camera group",
            )
        if not self.auxiliary_cameras:
            raise _error(
                "alignment_integrity_error",
                "rejected an artifact without auxiliary cameras",
            )
        algorithm_profile = self.algorithm.verification_profile.to_dict()
        for interval in self.intervals:
            evidence_profile = AlignmentVerificationProfile.from_dict(
                interval.evidence["verification_profile"]
            ).to_dict()
            if evidence_profile != algorithm_profile:
                raise _error(
                    "alignment_integrity_error",
                    "rejected interval evidence with a mismatched verification profile",
                )
        camera_ids = [
            self.main_camera.camera_id,
            *(camera.camera_id for camera in self.auxiliary_cameras),
        ]
        if len(camera_ids) != len(set(camera_ids)):
            raise _error(
                "alignment_integrity_error",
                "rejected duplicate camera IDs",
            )
        # no source_id may appear in any two camera groups: the check covers
        # every pair of cameras, not just the main/auxiliary intersection
        seen_sources: set[str] = set()
        for camera in self.auxiliary_cameras:
            for source_id in camera.ordered_source_ids:
                if source_id in seen_sources:
                    raise _error(
                        "alignment_integrity_error",
                        "rejected a source shared by two camera groups",
                    )
                seen_sources.add(source_id)
        for source_id in self.main_camera.ordered_source_ids:
            if source_id in seen_sources:
                raise _error(
                    "alignment_integrity_error",
                    "rejected a source shared by two camera groups",
                )
            seen_sources.add(source_id)
        # source_basis must be exactly sorted by (camera_id, source_id) with no
        # extra entries beyond the requested groups
        expected_basis_keys: list[tuple[str, str]] = [
            (self.main_camera.camera_id, source_id)
            for source_id in self.main_camera.ordered_source_ids
        ]
        for group_camera in self.auxiliary_cameras:
            for source_id in group_camera.ordered_source_ids:
                expected_basis_keys.append(
                    (group_camera.camera_id, source_id)
                )
        expected_basis_keys.sort()
        basis_keys = [
            (basis.camera_id, basis.source_id) for basis in self.source_basis
        ]
        if basis_keys != expected_basis_keys:
            raise _error(
                "alignment_integrity_error",
                "rejected unsorted or extra source basis entries",
            )
        main_duration_by_source: dict[str, int] = {}
        aux_duration_by_source: dict[str, int] = {}
        for basis in self.source_basis:
            if basis.camera_id == self.main_camera.camera_id:
                main_duration_by_source[basis.source_id] = basis.duration_ticks
            else:
                aux_duration_by_source[basis.source_id] = basis.duration_ticks
        expected_camera_ids = {camera.camera_id for camera in self.auxiliary_cameras}
        interval_ids = [interval.interval_id for interval in self.intervals]
        if len(interval_ids) != len(set(interval_ids)):
            raise _error(
                "alignment_integrity_error",
                "rejected duplicate interval IDs",
            )
        for interval in self.intervals:
            if interval.auxiliary_camera_id not in expected_camera_ids:
                raise _error(
                    "alignment_integrity_error",
                    "rejected an interval for an unknown camera",
                )
            main = interval.main
            main_source = cast(str, main["source_id"])
            main_end = cast(int, main["end_ticks"])
            if main_source not in main_duration_by_source:
                raise _error(
                    "alignment_integrity_error",
                    "rejected an interval for an unknown main source",
                )
            if main_end > main_duration_by_source[main_source]:
                raise _error(
                    "alignment_integrity_error",
                    "rejected an interval beyond the main source duration",
                )
            if interval.auxiliary is not None:
                aux = interval.auxiliary
                aux_source = cast(str, aux["source_id"])
                aux_end = cast(int, aux["end_ticks"])
                if aux_source not in aux_duration_by_source:
                    raise _error(
                        "alignment_integrity_error",
                        "rejected an interval for an unknown auxiliary source",
                    )
                if aux_end > aux_duration_by_source[aux_source]:
                    raise _error(
                        "alignment_integrity_error",
                        "rejected an interval beyond the auxiliary source duration",
                    )
                camera = next(
                    camera
                    for camera in self.auxiliary_cameras
                    if camera.camera_id == interval.auxiliary_camera_id
                )
                if aux_source not in camera.ordered_source_ids:
                    raise _error(
                        "alignment_integrity_error",
                        "rejected a mapped interval for a foreign auxiliary source",
                    )
        # strict partition: every auxiliary camera x every main source must be
        # a gapless cover of [0, duration); the persisted interval order itself
        # must already be sorted by start_ticks with no gaps or overlaps, so a
        # serialized payload with reversed order is rejected, never repaired
        for camera in self.auxiliary_cameras:
            for main_source_id, main_duration in main_duration_by_source.items():
                position = 0
                for interval in self.intervals:
                    if (
                        interval.auxiliary_camera_id != camera.camera_id
                        or cast(str, interval.main["source_id"]) != main_source_id
                    ):
                        continue
                    start = cast(int, interval.main["start_ticks"])
                    end = cast(int, interval.main["end_ticks"])
                    if start != position:
                        raise _error(
                            "alignment_integrity_error",
                            "rejected an unsorted, gap, or overlapped partition",
                        )
                    position = end
                if position != main_duration:
                    raise _error(
                        "alignment_integrity_error",
                        "rejected an incomplete partition",
                    )
        # recompute per-camera ticks and status from the actual intervals
        for camera in self.auxiliary_cameras:
            computed = _compute_camera_ticks(camera, self.intervals)
            if computed != (
                camera.mapped_ticks,
                camera.missing_ticks,
                camera.uncertain_ticks,
                camera.conflict_ticks,
            ):
                raise _error(
                    "alignment_integrity_error",
                    "rejected per-camera ticks that differ from the intervals",
                )
            derived_status = _derive_camera_status(
                camera.mapped_ticks,
                camera.missing_ticks,
                camera.uncertain_ticks,
                camera.conflict_ticks,
                bool(camera.errors),
                self.main_camera,
                self.source_basis,
            )
            if camera.status != derived_status:
                raise _error(
                    "alignment_integrity_error",
                    "rejected a camera status that differs from its ticks",
                )
        main_duration_total = sum(main_duration_by_source.values())
        for camera in self.auxiliary_cameras:
            total = (
                camera.mapped_ticks
                + camera.missing_ticks
                + camera.uncertain_ticks
                + camera.conflict_ticks
            )
            if total != main_duration_total:
                raise _error(
                    "alignment_integrity_error",
                    "rejected a camera that does not cover the main timeline",
                )
        # the top-level summary must be recomputed exactly from the intervals
        expected_summary = _compute_summary(self.auxiliary_cameras, main_duration_total)
        if (
            expected_summary.total_main_ticks != self.summary.total_main_ticks
            or expected_summary.camera_count != self.summary.camera_count
            or expected_summary.mapped_ticks != self.summary.mapped_ticks
            or expected_summary.missing_ticks != self.summary.missing_ticks
            or expected_summary.uncertain_ticks != self.summary.uncertain_ticks
            or expected_summary.conflict_ticks != self.summary.conflict_ticks
        ):
            raise _error(
                "alignment_integrity_error",
                "rejected a summary that differs from the intervals",
            )
        self.summary.validate()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "alignment_id": self.alignment_id,
            "project_id": self.project_id,
            "producer_operation_id": self.producer_operation_id,
            "created_at": self.created_at,
            "request_hash": self.request_hash,
            "input_hash": self.input_hash,
            "algorithm": self.algorithm.to_dict(),
            "main_camera": self.main_camera.to_dict(),
            "auxiliary_cameras": [
                camera.to_dict() for camera in self.auxiliary_cameras
            ],
            "source_basis": [basis.to_dict() for basis in self.source_basis],
            "intervals": [interval.to_dict() for interval in self.intervals],
            "summary": self.summary.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> MulticamAlignmentArtifact:
        data = _closed(
            value,
            {
                "schema_version",
                "alignment_id",
                "project_id",
                "producer_operation_id",
                "created_at",
                "request_hash",
                "input_hash",
                "algorithm",
                "main_camera",
                "auxiliary_cameras",
                "source_basis",
                "intervals",
                "summary",
            },
            description="multicam alignment artifact",
        )
        auxiliary_raw = data["auxiliary_cameras"]
        if not isinstance(auxiliary_raw, list):
            raise _error(
                "alignment_integrity_error",
                "rejected invalid auxiliary cameras",
            )
        basis_raw = data["source_basis"]
        if not isinstance(basis_raw, list):
            raise _error(
                "alignment_integrity_error",
                "rejected invalid source basis",
            )
        intervals_raw = data["intervals"]
        if not isinstance(intervals_raw, list):
            raise _error(
                "alignment_integrity_error",
                "rejected invalid intervals",
            )
        return cls(
            schema_version=_integer(
                data["schema_version"], field="schema_version", minimum=1
            ),
            alignment_id=_safe_id(data["alignment_id"], field="alignment_id"),
            project_id=_safe_id(data["project_id"], field="project_id"),
            producer_operation_id=_safe_id(
                data["producer_operation_id"], field="producer_operation_id"
            ),
            created_at=_timestamp(data["created_at"], field="created_at"),
            request_hash=_sha256(data["request_hash"], field="request_hash"),
            input_hash=_sha256(data["input_hash"], field="input_hash"),
            algorithm=AlignmentAlgorithm.from_dict(data["algorithm"]),
            main_camera=AlignmentCameraGroup.from_dict(data["main_camera"]),
            auxiliary_cameras=tuple(
                AlignmentCamera.from_dict(item) for item in auxiliary_raw
            ),
            source_basis=tuple(
                AlignmentSourceBasis.from_dict(item) for item in basis_raw
            ),
            intervals=tuple(
                AlignmentInterval.from_dict(item) for item in intervals_raw
            ),
            summary=AlignmentSummary.from_dict(data["summary"]),
        )

    @property
    def content_hash(self) -> str:
        return canonical_sha256_v1(self.to_dict())


def _compute_camera_ticks(
    camera: AlignmentCamera,
    intervals: tuple[AlignmentInterval, ...],
) -> tuple[int, int, int, int]:
    """Recompute one camera's four tick counts from its actual intervals."""
    mapped = 0
    missing = 0
    uncertain = 0
    conflict = 0
    for interval in intervals:
        if interval.auxiliary_camera_id != camera.camera_id:
            continue
        duration = cast(int, interval.main["end_ticks"]) - cast(
            int, interval.main["start_ticks"]
        )
        if interval.classification == "mapped":
            mapped += duration
        elif interval.classification == "missing":
            missing += duration
        elif interval.classification == "uncertain":
            uncertain += duration
        elif interval.classification == "conflict":
            conflict += duration
    return mapped, missing, uncertain, conflict


def _derive_camera_status(
    mapped_ticks: int,
    missing_ticks: int,
    uncertain_ticks: int,
    conflict_ticks: int,
    has_errors: bool,
    main_camera: AlignmentCameraGroup,
    source_basis: tuple[AlignmentSourceBasis, ...],
) -> str:
    """Derive the closed camera status from ticks and errors."""
    total = (
        mapped_ticks + missing_ticks + uncertain_ticks + conflict_ticks
    )
    main_duration_total = sum(
        basis.duration_ticks
        for basis in source_basis
        if basis.camera_id == main_camera.camera_id
    )
    if mapped_ticks == 0:
        if has_errors:
            return "failed"
        if total == 0:
            return "failed"
        return "omitted"
    if mapped_ticks == main_duration_total and not has_errors:
        return "complete"
    return "partial"


def _compute_summary(
    auxiliary_cameras: tuple[AlignmentCamera, ...],
    main_duration_total: int,
) -> AlignmentSummary:
    """Recompute the top-level summary exactly from the camera items."""
    return AlignmentSummary(
        total_main_ticks=main_duration_total,
        camera_count=len(auxiliary_cameras),
        mapped_ticks=sum(camera.mapped_ticks for camera in auxiliary_cameras),
        missing_ticks=sum(camera.missing_ticks for camera in auxiliary_cameras),
        uncertain_ticks=sum(
            camera.uncertain_ticks for camera in auxiliary_cameras
        ),
        conflict_ticks=sum(
            camera.conflict_ticks for camera in auxiliary_cameras
        ),
    )


def alignment_request_projection(
    *,
    scope: dict[str, object],
    operation_id: str,
    alignment_id: str,
    expected_revision: int,
    main_camera: dict[str, object],
    auxiliary_cameras: list[dict[str, object]],
    main_audio_stable: bool,
    max_temporary_disk_bytes: int,
    max_analysis_memory_bytes: int,
    max_runtime_seconds: int,
    writer_profile: dict[str, object] | None = None,
) -> dict[str, object]:
    """Closed stable request projection for one align_multicam operation.

    ``writer_profile=None`` reproduces the historical schema-1 Audalign
    projection byte-for-byte so existing succeeded operations keep their
    request hash. The waveform writer passes its frozen profile identity and
    produces a schema-2 projection whose hash binds that identity.
    """
    from roughcut.domain.media_operation import (
        ProjectOperationScope,
    )

    ProjectOperationScope.from_dict(scope)
    _safe_id(operation_id, field="operation_id")
    _safe_id(alignment_id, field="alignment_id")
    _integer(expected_revision, field="expected_revision", minimum=1)
    main, parsed_groups = parse_alignment_request_groups(
        main_camera,
        auxiliary_cameras,
    )
    parsed_auxiliary = [group.to_dict() for group in parsed_groups]
    _boolean(main_audio_stable, field="main_audio_stable")
    for field in (
        "max_temporary_disk_bytes",
        "max_analysis_memory_bytes",
        "max_runtime_seconds",
    ):
        _integer(locals()[field], field=field, minimum=1)
    projection: dict[str, object] = {
        "operation_type": "align_multicam",
        "scope": dict(scope),
        "operation_id": operation_id,
        "alignment_id": alignment_id,
        "expected_revision": expected_revision,
        "main_camera": main.to_dict(),
        "auxiliary_cameras": parsed_auxiliary,
        "main_audio_stable": main_audio_stable,
        "max_temporary_disk_bytes": max_temporary_disk_bytes,
        "max_analysis_memory_bytes": max_analysis_memory_bytes,
        "max_runtime_seconds": max_runtime_seconds,
    }
    if writer_profile is None:
        projection["request_schema_version"] = 1
        return projection
    projection["request_schema_version"] = 2
    projection["writer_profile"] = dict(writer_profile)
    return projection


def hash_alignment_request(value: object) -> str:
    return canonical_sha256_v1(_validate_alignment_request(value))


_ALIGNMENT_REQUEST_V1_FIELDS = {
    "request_schema_version",
    "operation_type",
    "scope",
    "operation_id",
    "alignment_id",
    "expected_revision",
    "main_camera",
    "auxiliary_cameras",
    "main_audio_stable",
    "max_temporary_disk_bytes",
    "max_analysis_memory_bytes",
    "max_runtime_seconds",
}
_ALIGNMENT_REQUEST_V2_FIELDS = _ALIGNMENT_REQUEST_V1_FIELDS | {"writer_profile"}


def _validate_alignment_request(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(
            "alignment_integrity_error",
            "rejected invalid alignment request",
        )
    version = value.get("request_schema_version")
    fields = (
        _ALIGNMENT_REQUEST_V1_FIELDS
        if version == 1
        else _ALIGNMENT_REQUEST_V2_FIELDS
        if version == 2
        else None
    )
    if fields is None:
        raise _error(
            "alignment_integrity_error",
            "rejected unknown projection schema or operation type",
        )
    data = _closed(
        value,
        fields,
        description="alignment request",
    )
    if data["operation_type"] != "align_multicam":
        raise _error(
            "alignment_integrity_error",
            "rejected unknown projection schema or operation type",
        )
    if version == 2 and not (
        data["writer_profile"] == WAVEFORM_WRITER_PROFILE
        or data["writer_profile"] == BBC_WRITER_PROFILE
        or data["writer_profile"] == AUDALIGN_CORRELATION_WRITER_PROFILE
    ):
        raise _error(
            "alignment_integrity_error",
            "rejected a non-canonical alignment writer profile",
        )
    return alignment_request_projection(
        scope=data["scope"],
        operation_id=data["operation_id"],
        alignment_id=data["alignment_id"],
        expected_revision=data["expected_revision"],
        main_camera=data["main_camera"],
        auxiliary_cameras=data["auxiliary_cameras"],
        main_audio_stable=data["main_audio_stable"],
        max_temporary_disk_bytes=data["max_temporary_disk_bytes"],
        max_analysis_memory_bytes=data["max_analysis_memory_bytes"],
        max_runtime_seconds=data["max_runtime_seconds"],
        writer_profile=(
            data["writer_profile"] if version == 2 else None
        ),
    )
