"""Shared synthetic-media builders for real audalign Phase 2 vector tests.

Each builder produces real WAV files with rich spectral content so audalign
1.3.1 can actually match them across different processing chains (independent
PCM generation, per-chain gain/DC/bandpass variations).
"""

from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 44_100
TICKS_PER_SECOND = 120_000


def _samples_per_tick(ticks: int) -> int:
    return int(ticks * SAMPLE_RATE / TICKS_PER_SECOND)


def _render_sequence(
    *,
    seed: int,
    duration_ticks: int,
    gain: float = 1.0,
    dc_offset: float = 0.0,
    bandpass: bool = False,
    lr_channel: str | None = None,
    stereo: bool = False,
) -> list[float]:
    """One deterministic rich-tone sequence with per-chain processing."""
    rng = random.Random(seed)
    samples: list[float] = []
    target = _samples_per_tick(duration_ticks)
    t = 0.0
    while len(samples) < target:
        freq = 180 + rng.uniform(0, 1) * 1700
        seg_ticks = rng.uniform(0.15, 0.5)
        seg_samples = _samples_per_tick(int(seg_ticks * TICKS_PER_SECOND))
        seg_samples = max(seg_samples, 1)
        for i in range(seg_samples):
            if len(samples) >= target:
                break
            env = math.sin(math.pi * i / seg_samples) ** 0.5
            value = env * 0.6 * math.sin(2 * math.pi * freq * (t + i / SAMPLE_RATE))
            if bandpass:
                # crude low-pass to differentiate the processing chain
                value = value * 0.85 + 0.15 * math.sin(
                    2 * math.pi * (freq * 0.5) * (t + i / SAMPLE_RATE)
                )
            samples.append(value * gain + dc_offset)
        t += seg_ticks
    while len(samples) < target:
        samples.append(dc_offset)
    if stereo:
        stereo_samples: list[float] = []
        for index, value in enumerate(samples):
            if lr_channel == "left":
                stereo_samples.append(value)
                stereo_samples.append(0.0)
            elif lr_channel == "right":
                stereo_samples.append(0.0)
                stereo_samples.append(value)
            else:
                stereo_samples.extend((value * 0.7, value * 0.7))
        return stereo_samples
    return samples


def write_mono_wav(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(
            b"".join(
                struct.pack(
                    "<h",
                    max(-32767, min(32767, int(value * 32000))),
                )
                for value in samples
            )
        )


def write_stereo_wav(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(
            b"".join(
                struct.pack(
                    "<h",
                    max(-32767, min(32767, int(value * 32000))),
                )
                for value in samples
            )
        )


def make_offset_pair(
    directory: Path,
    *,
    main_duration_ticks: int,
    aux_offset_ticks: int,
    seed: int = 42,
    aux_gain: float = 1.0,
    aux_bandpass: bool = False,
    aux_dc: float = 0.0,
    aux_duration_ticks: int | None = None,
    aux_tail_ticks: int = 0,
) -> tuple[Path, Path]:
    """Write main/aux WAVs where aux starts `aux_offset_ticks` later.

    Returns (main_path, aux_path). The two chains always differ (aux gain and
    optional bandpass/DC), so a naive byte-copy match is impossible. When
    `aux_tail_ticks` is given, aux extends that many ticks beyond the main
    duration so the main timeline is fully covered.
    """
    main_samples = _render_sequence(seed=seed, duration_ticks=main_duration_ticks)
    aux_content = _render_sequence(
        seed=seed,
        duration_ticks=main_duration_ticks + aux_tail_ticks,
        gain=aux_gain,
        bandpass=aux_bandpass,
        dc_offset=aux_dc,
    )
    offset_samples = _samples_per_tick(aux_offset_ticks)
    if aux_duration_ticks is None:
        aux_duration_ticks = main_duration_ticks + aux_tail_ticks
    aux_samples = (
        [0.0] * offset_samples
        + aux_content[: _samples_per_tick(aux_duration_ticks)]
    )
    main_path = directory / "main.wav"
    aux_path = directory / "aux.wav"
    write_mono_wav(main_path, main_samples)
    write_mono_wav(aux_path, aux_samples)
    return main_path, aux_path


def make_shared_event_pair(
    directory: Path,
    *,
    main_duration_ticks: int,
    aux_duration_ticks: int,
    shared_start_ticks: int,
    shared_duration_ticks: int,
    seed: int = 99,
) -> tuple[Path, Path]:
    """Write main/aux WAVs sharing one short event (for low-evidence cases)."""
    event = _render_sequence(seed=seed, duration_ticks=shared_duration_ticks)
    main = [0.0] * _samples_per_tick(main_duration_ticks)
    main_start = _samples_per_tick(shared_start_ticks)
    for index, value in enumerate(event):
        if main_start + index < len(main):
            main[main_start + index] = value
    aux = [0.0] * _samples_per_tick(aux_duration_ticks)
    for index, value in enumerate(event):
        if index < len(aux):
            aux[index] = value
    main_path = directory / "main.wav"
    aux_path = directory / "aux.wav"
    write_mono_wav(main_path, main)
    write_mono_wav(aux_path, aux)
    return main_path, aux_path


def make_stereo_cancellation_pair(
    directory: Path,
    *,
    main_duration_ticks: int,
    aux_offset_ticks: int,
    seed: int = 7,
) -> tuple[Path, Path]:
    """Stereo pair where L = program and R = -program.

    The default mono mix (L+R) cancels the program to silence, so the
    mono-first attempt cannot form a safe candidate; each channel alone still
    carries the full program, so the L/R fallback must recover the mapping.
    """
    program = _render_sequence(seed=seed, duration_ticks=main_duration_ticks)
    main_stereo: list[float] = []
    for value in program:
        main_stereo.extend((value, -value))
    aux_content = _render_sequence(
        seed=seed, duration_ticks=main_duration_ticks, gain=1.05
    )
    aux_stereo: list[float] = []
    for value in aux_content:
        aux_stereo.extend((value, -value))
    offset = _samples_per_tick(aux_offset_ticks)
    aux_samples = [0.0] * (offset * 2) + aux_stereo[: len(aux_stereo) - offset * 2]
    main_path = directory / "main.wav"
    aux_path = directory / "aux.wav"
    write_stereo_wav(main_path, main_stereo)
    write_stereo_wav(aux_path, aux_samples)
    return main_path, aux_path


def make_stereo_distant_pair(
    directory: Path,
    *,
    main_duration_ticks: int,
    aux_offset_ticks: int,
    seed: int = 7,
) -> tuple[Path, Path]:
    """Stereo pair where L and R support relations > 12000 ticks apart.

    main-L = program A, main-R = independent program B. aux-L inserts
    `aux_offset_ticks` of silence before program A; aux-R inserts
    `aux_offset_ticks + 240000` of silence before program B, so the two
    channels' relations differ by 240000 ticks (well over the 12000-tick
    group diameter). Both channels keep at least 36 s of effective overlap.
    The default mono mix carries both programs, so mono cannot form a unique
    safe mapping, and the L/R fallback finds two inconsistent verified B
    relations which must close as conflict, never mapped.
    """
    program_a = _render_sequence(seed=seed, duration_ticks=main_duration_ticks)
    program_b = _render_sequence(seed=seed + 500, duration_ticks=main_duration_ticks)
    main_stereo: list[float] = []
    for a, b in zip(program_a, program_b):
        main_stereo.extend((a, b))
    aux_content_a = _render_sequence(
        seed=seed, duration_ticks=main_duration_ticks, gain=1.04
    )
    aux_content_b = _render_sequence(
        seed=seed + 500, duration_ticks=main_duration_ticks, gain=0.97
    )
    left_offset = _samples_per_tick(aux_offset_ticks)
    right_offset = _samples_per_tick(aux_offset_ticks + 240_000)
    # the two channels' offsets differ by 240000 ticks > 12000 ticks
    assert abs(
        _samples_per_tick(aux_offset_ticks + 240_000)
        - _samples_per_tick(aux_offset_ticks)
    ) > _samples_per_tick(12_000)
    # both channels keep at least 36 s of effective overlap
    assert main_duration_ticks - (aux_offset_ticks + 240_000) >= 3 * 1_440_000
    aux_samples: list[float] = []
    for index in range(max(len(aux_content_a), len(aux_content_b))):
        if index < left_offset:
            left = 0.0
        elif index - left_offset < len(aux_content_a):
            left = aux_content_a[index - left_offset]
        else:
            left = 0.0
        if index < right_offset:
            right = 0.0
        elif index - right_offset < len(aux_content_b):
            right = aux_content_b[index - right_offset]
        else:
            right = 0.0
        aux_samples.extend((left, right))
    main_path = directory / "main.wav"
    aux_path = directory / "aux.wav"
    write_stereo_wav(main_path, main_stereo)
    write_stereo_wav(aux_path, aux_samples)
    return main_path, aux_path


def make_stereo_pair(
    directory: Path,
    *,
    main_duration_ticks: int,
    aux_offset_ticks: int,
    seed: int = 7,
) -> tuple[Path, Path]:
    """Stereo pair where only the LEFT channel carries the shared program."""
    main_stereo = _render_sequence(
        seed=seed,
        duration_ticks=main_duration_ticks,
        stereo=True,
        lr_channel="left",
    )
    aux_content = _render_sequence(
        seed=seed,
        duration_ticks=main_duration_ticks,
        stereo=True,
        lr_channel="left",
        gain=1.05,
    )
    offset = _samples_per_tick(aux_offset_ticks)
    aux_stereo = [0.0] * (offset * 2) + aux_content[: len(aux_content) - offset * 2]
    main_path = directory / "main.wav"
    aux_path = directory / "aux.wav"
    write_stereo_wav(main_path, main_stereo)
    write_stereo_wav(aux_path, aux_stereo)
    return main_path, aux_path
