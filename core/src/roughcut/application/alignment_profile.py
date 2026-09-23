"""Closed fixed-offset profile math, admission, grouping, and verification."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Protocol

from roughcut.adapters.audalign import (
    AUDALIGN_WORKER_MAX_OUTPUT_BYTES,
    run_audalign_recognize,
)
from roughcut.adapters.child_budget import ChildBudget
from roughcut.domain.alignment import (
    ALIGNMENT_CANDIDATE_GROUP_DIAMETER_TICKS,
    ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS,
    ALIGNMENT_VERIFICATION_WINDOW_TICKS,
    AUDALIGN_CORRELATION_PROBE_PERCENTS,
    AUDALIGN_CORRELATION_PROBE_TICKS,
    AUDALIGN_CORRELATION_PROFILE_NAME,
    AUDALIGN_CORRELATION_PROFILE_VERSION,
    AUDALIGN_CORRELATION_WRITER_PROFILE,
    AlignmentError,
    seconds_to_ticks,
)

ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


# These are deliberately two named constants rather than a profile registry.
# Profile 1 is retained solely for artifact/input readback compatibility;
# profile 2 is the only profile selected by new alignment operations.
PROFILE1_CANONICAL: dict[str, object] = {
    "name": "roughcut_audalign_fixed_offset",
    "version": 1,
    "audalign": {
        "package": "1.3.1",
        "upstream_commit": "d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
        "recognizer": "FingerprintRecognizer",
        "accuracy": 2,
        "num_processors": 1,
    },
    "roles": {"target": "auxiliary_complete_source", "against": "main_complete_source"},
    "mapping_model": "fixed_offset_equal_speed",
    "ticks_per_second": 120000,
    "candidate_group_diameter_ticks": 12000,
    "verification_window_ticks": 1440000,
    "verification_window_count": 3,
    "minimum_verified_overlap_ticks": 4320000,
    "maximum_local_error_ticks": 12000,
    "mono_first_lr_fallback": True,
}

PROFILE2_CANONICAL: dict[str, object] = {
    "name": "roughcut_audalign_fixed_offset",
    "version": 2,
    "audalign": {
        "package": "1.3.1",
        "upstream_commit": "d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
        "recognizer": "FingerprintRecognizer",
        "accuracy": 2,
        "num_processors": 1,
    },
    "roles": {"target": "auxiliary_excerpt", "against": "main_complete_source"},
    "mapping_model": "fixed_offset_equal_speed",
    "ticks_per_second": 120000,
    "recall_excerpt_length_ticks": 1800000,
    "recall_probe_start_algorithm": "[0, floor((D - L) / 2), D - L], deduplicated in that order",
    "recall_probe_order": ["opening", "middle", "ending"],
    "max_recall_probes_per_channel": 3,
    "max_recall_calls_per_source_pair": 9,
    "max_raw_candidates_per_probe": 512,
    "max_selected_candidates_per_probe": 1,
    "max_candidate_groups_per_channel": 3,
    "max_verification_calls_per_source_pair": 27,
    "max_audalign_calls_per_source_pair": 36,
    "channel_schedule": {
        "mono": "same_probe_schedule",
        "left_right_fallback": "same_probe_schedule",
    },
    "candidate_group_diameter_ticks": 12000,
    "verification_window_ticks": 1440000,
    "verification_window_count": 3,
    "maximum_local_error_ticks": 12000,
}

PROFILE1_VERSION = 1
PROFILE2_VERSION = 2
PROFILE2_RECALL_EXCERPT_LENGTH_TICKS = 1_800_000
PROFILE2_MAX_RECALL_PROBES_PER_CHANNEL = 3
PROFILE2_MAX_RECALL_CALLS_PER_SOURCE_PAIR = 9
PROFILE2_MAX_RAW_CANDIDATES_PER_PROBE = 512
PROFILE2_MAX_SELECTED_CANDIDATES_PER_PROBE = 1
PROFILE2_MAX_CANDIDATE_GROUPS_PER_CHANNEL = 3
PROFILE2_MAX_VERIFICATION_CALLS_PER_SOURCE_PAIR = 27
PROFILE2_MAX_AUDALIGN_CALLS_PER_SOURCE_PAIR = 36

# Correlation bounded probe (0.2.5 production): mono-only, 44.1k PCM16, 15 s, 20/50/80 %
CORRELATION_PROFILE_NAME = AUDALIGN_CORRELATION_PROFILE_NAME
CORRELATION_PROFILE_VERSION = AUDALIGN_CORRELATION_PROFILE_VERSION
# Compatibility name for callers that imported the old application constant.
# It is an alias, not a second literal profile identity.
CORRELATION_CANONICAL = AUDALIGN_CORRELATION_WRITER_PROFILE
CORRELATION_RECALL_EXCERPT_LENGTH_TICKS = AUDALIGN_CORRELATION_PROBE_TICKS
CORRELATION_PROBE_PERCENTS = tuple(AUDALIGN_CORRELATION_PROBE_PERCENTS)
CORRELATION_MAX_PROBES = len(AUDALIGN_CORRELATION_PROBE_PERCENTS)
CORRELATION_TOLERANCE_TICKS = ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS


def frozen_profile(version: int) -> dict[str, object]:
    """Return a detached copy of one of the two known frozen profiles."""
    if version == PROFILE1_VERSION:
        return deepcopy(PROFILE1_CANONICAL)
    if version == PROFILE2_VERSION:
        return deepcopy(PROFILE2_CANONICAL)
    raise AlignmentProfileError("unsupported frozen alignment profile")


class _RemainingProtocol(Protocol):
    def remaining(self) -> float: ...


class _WorkspaceBudgetProtocol(Protocol):
    def reserve_decode(self, *, duration_ticks: int) -> None: ...

    def reserve_bytes(self, *, size_bytes: int) -> None: ...

    def recheck(self) -> None: ...


def _recheck_workspace_after_child(
    workspace_budget: _WorkspaceBudgetProtocol | None,
    error: Exception | None = None,
) -> None:
    if workspace_budget is None:
        return
    try:
        workspace_budget.recheck()
    except AlignmentError as budget_error:
        if budget_error.code == "alignment_disk_budget_exceeded":
            if error is None:
                raise
            raise budget_error from error
        raise


class AlignmentProfileError(RuntimeError):
    """Raised when the frozen verification profile cannot be applied."""


@dataclass(frozen=True)
class CandidateOffset:
    b_ticks: int
    raw_seconds_text: str
    confidence: int
    upstream_index: int
    probe_index: int = 0


def source_relation_b(
    main_excerpt_start_ticks: int,
    auxiliary_excerpt_start_ticks: int,
    audalign_offset_seconds_text: str,
) -> int:
    """Convert one audalign Δ into the frozen source relation B.

    B = M0 - A0 + seconds_to_ticks(Δ)
    """
    return (
        main_excerpt_start_ticks
        - auxiliary_excerpt_start_ticks
        + seconds_to_ticks(audalign_offset_seconds_text)
    )


def group_candidates(
    candidates: tuple[CandidateOffset, ...],
) -> tuple[tuple[CandidateOffset, ...], ...]:
    """Group raw candidates by B with a 12000-tick diameter.

    Groups are built by B ascending, but each group is returned ordered by
    the candidate's original upstream index so verification follows
    audalign's return order inside every group.
    """
    ordered = tuple(sorted(candidates, key=lambda item: item.b_ticks))
    groups: list[list[CandidateOffset]] = []
    for candidate in ordered:
        if not groups:
            groups.append([candidate])
            continue
        current = groups[-1]
        if (
            candidate.b_ticks - current[0].b_ticks
            <= ALIGNMENT_CANDIDATE_GROUP_DIAMETER_TICKS
        ):
            current.append(candidate)
        else:
            groups.append([candidate])
    return tuple(
        tuple(sorted(group, key=lambda item: item.upstream_index))
        for group in groups
    )


def profile2_probe_starts(auxiliary_duration_ticks: int) -> tuple[int, ...]:
    """Return the closed opening/middle/ending source-local probe schedule."""
    if auxiliary_duration_ticks < PROFILE2_RECALL_EXCERPT_LENGTH_TICKS:
        return ()
    last = auxiliary_duration_ticks - PROFILE2_RECALL_EXCERPT_LENGTH_TICKS
    return tuple(dict.fromkeys((0, last // 2, last)))


def correlation_probe_starts(auxiliary_duration_ticks: int) -> tuple[int, ...]:
    """Return the frozen 20/50/80 % probe schedule or () if insufficient.

    * excerpt = 1_800_000 ticks (15 s at 120 000/s)
    * raw_start = floor(aux_duration * pct/100)  (integer ticks)
    * start = min(raw_start, aux_duration - excerpt)
    * end = start + excerpt
    * aux duration < excerpt => uncertain (no probes)
    * clamp producing duplicate starts => fail closed uncertain (caller treats () as insufficient)
    * probe windows are exactly [start, start+excerpt) at 44.1 kHz mono; target is the
      15 s auxiliary excerpt, against is the full main.
    """
    if auxiliary_duration_ticks < CORRELATION_RECALL_EXCERPT_LENGTH_TICKS:
        return ()
    last_start = auxiliary_duration_ticks - CORRELATION_RECALL_EXCERPT_LENGTH_TICKS
    starts: list[int] = []
    for pct in CORRELATION_PROBE_PERCENTS:
        raw = (auxiliary_duration_ticks * pct) // 100
        clamped = min(raw, last_start)
        clamped = max(clamped, 0)
        starts.append(clamped)
    # fail closed if clamped starts are not independent (duplicates) -> uncertain
    if len(set(starts)) != len(starts):
        return ()
    return tuple(starts)


def correlation_admission(
    probe_bs: tuple[int | None, ...],
    probe_starts: tuple[int, ...],
) -> tuple[str, int | None, int | None, tuple[int, ...]]:
    """Evaluate the 20/50/80 B cluster admission.

    Returns (code, representative_b, spread, support_probe_indices) where code is
    ``fixed_offset_verified`` on success or an uncertain code otherwise.
    The caller may map the code to evidence.  Only distinct probes count as
    support; duplicate B from distinct probes is still one support per probe,
    but duplicate start positions are already rejected as insufficient.
    Admission requires at least two distinct probes whose B values lie within
    12 000 ticks without chaining, and no ambiguous second qualifying cluster.
    Representative is the earliest probe in fixed order 20->50->80 that belongs
    to the winning cluster (not mean/median).
    """
    if (
        len(probe_bs) != len(probe_starts)
        or len(probe_starts) != CORRELATION_MAX_PROBES
        or len(set(probe_starts)) != len(probe_starts)
    ):
        return ("probe_inconsistent", None, None, ())
    # probe_bs is parallel to probe_starts in fixed order 20,50,80; None for failed probe
    valid: list[tuple[int, int, int]] = []  # (b, probe_index, percentage)
    for idx, (b, start) in enumerate(zip(probe_bs, probe_starts)):
        if b is not None:
            pct = CORRELATION_PROBE_PERCENTS[idx]
            valid.append((b, idx, pct))
    if len(valid) < 2:
        # fewer than 2 successful probes => no candidate
        return ("no_candidate", None, None, ())
    # exhaustive clustering for n=3 without chaining
    # enumerate all subsets size >=2 where diameter <= tolerance; take maximal ones
    candidates = valid
    qualifying: list[tuple[int, ...]] = []  # tuple of indices in candidates
    for r in (3, 2):
        for combo in combinations(range(len(candidates)), r):
            bs = [candidates[i][0] for i in combo]
            if max(bs) - min(bs) <= CORRELATION_TOLERANCE_TICKS:
                # check maximality: not subset of larger qualifying set
                is_subset = any(set(combo) < set(q) for q in qualifying)
                if not is_subset:
                    # remove any existing that is subset of new
                    qualifying = [q for q in qualifying if not set(q) < set(combo)]
                    qualifying.append(combo)
        if r == 3 and qualifying:
            # if 3 qualifies, it's the sole maximal cluster
            break
    # filter to those that are maximal and not subset; for n=3 this yields at most 2
    # Now check uniqueness: need exactly one qualifying cluster
    if not qualifying:
        return ("probe_inconsistent", None, None, ())
    if len(qualifying) != 1:
        return ("probe_inconsistent", None, None, ())
    winning = qualifying[0]
    winning_bs = [candidates[i][0] for i in winning]
    spread = max(winning_bs) - min(winning_bs)
    # representative: earliest probe in fixed order 20->50->80 among winning
    winning_probe_indices = [candidates[i][1] for i in winning]
    representative_probe = min(winning_probe_indices)  # since probe_index preserves 20<50<80 order
    representative_b = next(candidates[i][0] for i in winning if candidates[i][1] == representative_probe)
    support = tuple(sorted(winning_probe_indices))
    return ("fixed_offset_verified", representative_b, spread, support)


def group_profile2_hypotheses(
    candidates: tuple[CandidateOffset, ...],
) -> tuple[tuple[CandidateOffset, ...], ...]:
    """Group admitted profile-2 hypotheses with stable representatives.

    Group boundaries are calculated from B-sorted candidates without chaining;
    the representative inside each group is the first candidate in the fixed
    probe/upstream order.
    """
    ordered = tuple(
        sorted(candidates, key=lambda item: (item.probe_index, item.upstream_index))
    )
    by_b = tuple(sorted(ordered, key=lambda item: item.b_ticks))
    groups: list[list[CandidateOffset]] = []
    for candidate in by_b:
        if not groups or (
            candidate.b_ticks - groups[-1][0].b_ticks
            > ALIGNMENT_CANDIDATE_GROUP_DIAMETER_TICKS
        ):
            groups.append([candidate])
        else:
            groups[-1].append(candidate)
    stable_groups = tuple(
        tuple(sorted(group, key=lambda item: (item.probe_index, item.upstream_index)))
        for group in groups
    )
    return tuple(
        sorted(
            stable_groups,
            key=lambda group: (group[0].probe_index, group[0].upstream_index),
        )
    )


def profile2_call_plan(
    *,
    recall_calls: int,
    verification_calls: int,
) -> tuple[int, int]:
    """Validate and return the closed recall/verification/Audalign call plan."""
    if (
        recall_calls < 0
        or verification_calls < 0
        or recall_calls > PROFILE2_MAX_RECALL_CALLS_PER_SOURCE_PAIR
        or verification_calls > PROFILE2_MAX_VERIFICATION_CALLS_PER_SOURCE_PAIR
        or recall_calls + verification_calls
        > PROFILE2_MAX_AUDALIGN_CALLS_PER_SOURCE_PAIR
    ):
        raise AlignmentProfileError("profile 2 Audalign call plan exceeds its ceiling")
    return recall_calls, verification_calls


def three_verification_windows(
    overlap_start_ticks: int,
    overlap_end_ticks: int,
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None:
    """Return three non-overlapping 12 s windows over the effective overlap."""
    start = overlap_start_ticks
    end = overlap_end_ticks
    if end - start < 3 * ALIGNMENT_VERIFICATION_WINDOW_TICKS:
        return None
    first = (start, start + ALIGNMENT_VERIFICATION_WINDOW_TICKS)
    last_start = end - ALIGNMENT_VERIFICATION_WINDOW_TICKS
    last = (last_start, end)
    middle_start = (start + end - ALIGNMENT_VERIFICATION_WINDOW_TICKS) // 2
    middle = (middle_start, middle_start + ALIGNMENT_VERIFICATION_WINDOW_TICKS)
    if not (first[1] <= middle[0] and middle[1] <= last[0]):
        return None
    return first, middle, last


class FixedOffsetVerifier:
    """Verify one candidate relation with three independent audalign windows.

    Every FFmpeg extraction, audalign recognition, and version probe shares
    the operation deadline: the remaining seconds are re-read immediately
    before each child launch and used as its timeout, never the module
    defaults. When the coordinator supplies a ChildBudget, the exact same
    budget object (deadline + memory ceiling + TMPDIR constraint) is used,
    never a fresh budget that would lose the TMPDIR wiring.
    """

    def __init__(
        self,
        *,
        alignment_python: Path,
        ffmpeg_command: str,
        workspace: Path,
        deadline: _RemainingProtocol | None = None,
        child_budget: ChildBudget | None = None,
        workspace_budget: _WorkspaceBudgetProtocol | None = None,
        process_runner: ProcessRunner = subprocess.run,
    ) -> None:
        self.alignment_python = Path(alignment_python)
        self.ffmpeg_command = ffmpeg_command
        self.workspace = Path(workspace)
        self.deadline = deadline
        self.child_budget = child_budget
        self.workspace_budget = workspace_budget
        self.process_runner = process_runner

    def _timeout(self) -> float:
        if self.deadline is not None:
            return float(self.deadline.remaining())
        return 300.0

    def _budget(self) -> ChildBudget | None:
        if self.deadline is None:
            return None
        if self.child_budget is not None:
            return self.child_budget
        return ChildBudget(self.deadline, 0)

    def _recheck_after_child(self, error: Exception | None = None) -> None:
        _recheck_workspace_after_child(self.workspace_budget, error)

    def verify(
        self,
        main_wav: Path,
        auxiliary_wav: Path,
        *,
        candidate_b_ticks: int,
        main_overlap_start_ticks: int,
        main_overlap_end_ticks: int,
        auxiliary_overlap_start_ticks: int,
        auxiliary_overlap_end_ticks: int,
    ) -> dict[str, object]:
        """Run three 12 s window verifications for one candidate."""
        windows = three_verification_windows(
            main_overlap_start_ticks, main_overlap_end_ticks
        )
        if windows is None:
            return {"passed": False, "reason": "insufficient_overlap"}
        from roughcut.adapters.audalign.ffmpeg_audio import extract_wav_window

        budget = self._budget()
        local_errors: list[int] = []
        for index, (main_start, main_end) in enumerate(windows):
            main_window = self.workspace / f"main-window-{index}.wav"
            aux_start = auxiliary_overlap_start_ticks + (
                main_start - main_overlap_start_ticks
            )
            aux_end = aux_start + (main_end - main_start)
            if aux_end > auxiliary_overlap_end_ticks:
                return {"passed": False, "reason": "window_out_of_range"}
            auxiliary_window = self.workspace / f"aux-window-{index}.wav"
            window_duration_ticks = main_end - main_start
            for input_wav, output_wav, start_ticks, end_ticks in (
                (main_wav, main_window, main_start, main_end),
                (auxiliary_wav, auxiliary_window, aux_start, aux_end),
            ):
                if self.workspace_budget is not None:
                    self.workspace_budget.reserve_decode(
                        duration_ticks=window_duration_ticks
                    )
                try:
                    extract_wav_window(
                        input_wav,
                        output_wav,
                        start_ticks=start_ticks,
                        end_ticks=end_ticks,
                        ffmpeg_command=self.ffmpeg_command,
                        timeout_seconds=self._timeout(),
                        budget=budget,
                        process_runner=self.process_runner,
                    )
                except Exception as error:
                    self._recheck_after_child(error)
                    raise
                self._recheck_after_child()
            output = self.workspace / f"window-{index}.json"
            if self.workspace_budget is not None:
                self.workspace_budget.reserve_bytes(
                    size_bytes=AUDALIGN_WORKER_MAX_OUTPUT_BYTES
                )
            try:
                match = run_audalign_recognize(
                    self.alignment_python,
                    auxiliary_window,
                    main_window,
                    output,
                    timeout_seconds=self._timeout(),
                    budget=budget,
                    process_runner=self.process_runner,
                )
            except Exception as error:
                self._recheck_after_child(error)
                raise
            self._recheck_after_child()
            if not match.candidates:
                return {"passed": False, "reason": "no_window_candidate"}
            top = match.candidates[0]
            local_error = abs(top.offset_ticks)
            if local_error > ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS:
                return {
                    "passed": False,
                    "reason": "local_error_exceeded",
                    "local_error_ticks": local_error,
                }
            local_errors.append(local_error)
        return {
            "passed": True,
            "local_errors_ticks": local_errors,
            "max_local_offset_error_ticks": max(local_errors),
        }
