from __future__ import annotations

import struct
from pathlib import Path

import pytest
from test_quicktime_timecode import atom, local_dinf, media_track, timecode_movie

from roughcut.adapters.quicktime_timecode import SourceTimecodeResolution
from roughcut.adapters.source_timecode import inspect_source_timecode
from roughcut.domain.nle_handoff import NleHandoffError
from roughcut.domain.time import RationalRate


def chunk(kind: bytes, body: bytes) -> bytes:
    return kind + struct.pack("<I", len(body)) + body + bytes(len(body) % 2)


def wave(*extra: bytes, tag: int = 1, bits: int = 16) -> bytes:
    block = bits // 8
    fmt = struct.pack("<HHIIHH", tag, 1, 48_000, 48_000 * block, block, bits)
    body = b"WAVE" + chunk(b"fmt ", fmt) + b"".join(extra) + chunk(b"data", bytes(16))
    return b"RIFF" + struct.pack("<I", len(body)) + body


def movie(handler: bytes = b"vide") -> bytes:
    sample = b"mp4a" if handler == b"soun" else b"avc1"
    return atom(b"moov", media_track(handler, sample))


@pytest.mark.parametrize(
    "payload,rate,status,ticks",
    [
        (timecode_movie(counter=0), RationalRate(50, 1), "known_origin", 0),
        (timecode_movie(counter=50), RationalRate(50, 1), "known_origin", 120_000),
        (movie(), RationalRate(25, 1), "confirmed_absent", 0),
        (movie(b"soun"), None, "confirmed_absent", 0),
        (
            atom(b"moov", media_track() + media_track(b"meta", b"rtmd")),
            RationalRate(25, 1),
            "confirmed_absent",
            0,
        ),
        (wave(), None, "confirmed_absent", 0),
        (wave(tag=3, bits=32), None, "confirmed_absent", 0),
        (wave(chunk(b"JUNK", b"odd"), chunk(b"LIST", b"INFO")), None, "confirmed_absent", 0),
        (b"", None, "unknown", None),
        (b"other media", RationalRate(25, 1), "unknown", None),
        (b"\x06\x0e\x2b\x34" + bytes(24), RationalRate(25, 1), "unsupported", None),
        (b"RF64" + bytes(12), None, "unsupported", None),
        (b"BW64" + bytes(12), None, "unsupported", None),
        (wave(chunk(b"bext", bytes(602))), None, "unsupported", None),
        (wave(chunk(b"iXML", b"<TIMECODE>12:00:00</TIMECODE>")), None, "unsupported", None),
        (wave(chunk(b"axml", b"metadata")), None, "unsupported", None),
        (wave(tag=6), None, "unsupported", None),
        (wave()[:-1], None, "unsupported", None),
        (atom(b"moov", b""), None, "unsupported", None),
        (movie(b"tc64"), RationalRate(25, 1), "unsupported", None),
        (movie() + atom(b"moof", b""), RationalRate(25, 1), "unsupported", None),
        (timecode_movie(), None, "unsupported", None),
        (timecode_movie()[:-1], RationalRate(50, 1), "unsupported", None),
    ],
    ids=[
        "known-zero",
        "known-nonzero",
        "ordinary-movie",
        "audio-only-movie",
        "private-rtmd",
        "pcm-wave",
        "float-wave",
        "wave-padding-info",
        "empty",
        "unrecognized",
        "mxf-klv",
        "rf64",
        "bw64",
        "bwf-time-reference",
        "ixml",
        "axml",
        "wave-alaw",
        "truncated-wave",
        "empty-movie",
        "tc64",
        "fragmented-movie",
        "tmcd-without-rate",
        "truncated-tmcd",
    ],
)
def test_source_origin_support_matrix(
    tmp_path: Path, payload: bytes, rate: RationalRate | None, status: str, ticks: int | None
) -> None:
    # A misleading extension must never select a container or a brand policy.
    path = tmp_path / "unrelated.mxf"
    path.write_bytes(payload)
    result = inspect_source_timecode(path, rate)
    assert (result.status, result.origin_ticks) == (status, ticks)
    assert result.reason
    if ticks is None:
        with pytest.raises(NleHandoffError) as failure:
            result.require_origin()
        assert failure.value.code == f"nle_export_source_timecode_{status}"
    else:
        assert result.require_origin() == ticks
    assert path.read_bytes() == payload


def test_unreadable_source_is_explicitly_unsupported(tmp_path: Path) -> None:
    result = inspect_source_timecode(tmp_path / "absent.mp4", RationalRate(25, 1))
    assert result.status == "unsupported"
    assert result.origin_ticks is None
    assert "unreadable" in result.reason


def test_one_source_is_opened_once_and_payload_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "plain.wave"
    # The data chunk can exceed the metadata budget: it must be skipped, not read.
    payload = wave()
    data_start = payload.index(b"data")
    body = payload[8:data_start] + chunk(b"data", bytes(4_000_002))
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    original_open = Path.open
    calls: list[Path] = []

    def counted_open(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append(self)
        return original_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", counted_open)
    assert inspect_source_timecode(path, None).require_origin() == 0
    assert calls == [path]


@pytest.mark.parametrize(
    "status,ticks",
    [
        ("unknown", 0),
        ("unsupported", 0),
        ("known_origin", None),
        ("confirmed_absent", 120_000),
        ("known_origin", -1),
    ],
)
def test_resolution_cannot_disguise_unknown_as_zero(status: str, ticks: int | None) -> None:
    with pytest.raises(ValueError):
        SourceTimecodeResolution(status, ticks, "fixture")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "payload,expected,reason",
    [
        (movie(), ("audio",), "media types"),
        (wave(), ("video",), "media types"),
        (movie(b"zzzz"), ("video",), "handler"),
        (atom(b"moov", media_track(b"meta", b"rtmd")), None, "audio/video"),
        (atom(b"moov", media_track(b"vide", b"tc64")), None, "timecode sample"),
        (atom(b"moov", media_track(b"soun", b"tmcd")), None, "timecode sample"),
        (atom(b"moov", media_track(b"vide", b"zzzz")), None, "sample description"),
        (timecode_movie().replace(b"tmcd", b"tc64"), None, "handler"),
        (
            timecode_movie().replace(
                b"tmcd" + bytes(6) + b"\x00\x01", b"tc64" + bytes(6) + b"\x00\x01"
            ),
            None,
            "sample entry",
        ),
        (movie().replace(struct.pack(">II", 0, 1), struct.pack(">II", 0, 2)), None, "count"),
        (atom(b"moof", b""), None, "fragmented"),
        (atom(b"moov", media_track() + atom(b"mvex", b"")), None, "fragmented"),
        (b"RIFF" + struct.pack("<I", 4) + wave()[8:], None, "RIFF extent"),
        (wave(chunk(b"data", bytes(4))), None, "one audio data"),
        (wave(chunk(b"LIST", b"adtl")), None, "LIST"),
    ],
    ids=[
        "movie-as-audio",
        "wave-as-video",
        "unknown-handler",
        "private-only",
        "video-tc64",
        "audio-tmcd",
        "unknown-codec",
        "tc64-handler",
        "tc64-description",
        "bad-description-count",
        "fragment-first",
        "movie-fragments",
        "riff-declared-size",
        "multiple-wave-data",
        "wave-adtl",
    ],
)
def test_unverified_structure_never_becomes_confirmed_absence(
    tmp_path: Path,
    payload: bytes,
    expected: tuple[str, ...] | None,
    reason: str,
) -> None:
    path = tmp_path / "ordinary.mp4"
    path.write_bytes(payload)
    result = inspect_source_timecode(path, RationalRate(50, 1), expected)
    assert result.status == "unsupported"
    assert result.origin_ticks is None
    assert reason in result.reason


def test_audio_only_timecode_track_requires_verified_rate(tmp_path: Path) -> None:
    path = tmp_path / "audio.m4a"
    path.write_bytes(timecode_movie(audio_only=True))
    result = inspect_source_timecode(path, None, ("audio",))
    assert result.status == "unsupported"
    assert "nominal source frame rate" in result.reason


def test_wave_chunk_count_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "many.wav"
    path.write_bytes(wave(chunk(b"JUNK", b"") * 10_001))
    result = inspect_source_timecode(path, None)
    assert result.status == "unsupported"
    assert "excessive WAVE chunk" in result.reason


@pytest.mark.parametrize("sample", [b"abcd", b"xyzz", b"rtmd", b"djmd", b"dbgi"])
def test_ordinary_metadata_tracks_are_not_brand_specific(tmp_path: Path, sample: bytes) -> None:
    path = tmp_path / "generic.bin"
    path.write_bytes(atom(b"moov", media_track() + media_track(b"meta", sample)))
    result = inspect_source_timecode(path, RationalRate(25, 1), ("video",))
    assert result.status == "confirmed_absent"
    assert result.require_origin() == 0


@pytest.mark.parametrize("sample", [b"tmcd", b"tc64"])
def test_timecode_description_in_metadata_track_is_not_ignored(
    tmp_path: Path, sample: bytes
) -> None:
    path = tmp_path / "metadata.mp4"
    path.write_bytes(atom(b"moov", media_track() + media_track(b"meta", sample)))
    result = inspect_source_timecode(path, RationalRate(25, 1), ("video",))
    assert result.status == "unsupported"
    assert result.origin_ticks is None


@pytest.mark.parametrize("handler,sample", [(b"vide", b"avc1"), (b"soun", b"mp4a")])
def test_truncated_av_description_is_not_evidence_of_absence(
    tmp_path: Path,
    handler: bytes,
    sample: bytes,
) -> None:
    stsd = atom(b"stsd", struct.pack(">II", 0, 1) + atom(sample, bytes(6) + b"\x00\x01"))
    track = atom(
        b"trak",
        atom(
            b"mdia",
            atom(b"hdlr", bytes(8) + handler) + atom(b"minf", local_dinf() + atom(b"stbl", stsd)),
        ),
    )
    path = tmp_path / "short.mp4"
    path.write_bytes(atom(b"moov", track))
    result = inspect_source_timecode(path, RationalRate(25, 1))
    assert result.status == "unsupported"
    assert "truncated" in result.reason


def _replace_track_dinf(
    payload: bytes,
    handler: bytes,
    replacement: bytes,
    *,
    selected: bool = False,
) -> bytes:
    """Rebuild synthetic atom extents; leave mdat sample offsets unchanged."""
    output = b""
    offset = 0
    while offset < len(payload):
        length, kind = struct.unpack(">I4s", payload[offset : offset + 8])
        body = payload[offset + 8 : offset + length]
        if kind == b"dinf" and selected:
            output += replacement
        else:
            if kind in {b"moov", b"trak", b"mdia", b"minf"}:
                matches = atom(b"hdlr", bytes(8) + handler) in body if kind == b"mdia" else selected
                body = _replace_track_dinf(body, handler, replacement, selected=matches)
            output += atom(kind, body)
        offset += length
    return output


def _dinf(*entries: bytes, count: int | None = None, version_flags: int = 0) -> bytes:
    return atom(
        b"dinf",
        atom(
            b"dref",
            struct.pack(">II", version_flags, len(entries) if count is None else count)
            + b"".join(entries),
        ),
    )


@pytest.mark.parametrize(
    "replacement",
    [
        b"",
        _dinf(atom(b"url ", bytes(4) + b"file:///external-timecode.mov\x00")),
        _dinf(atom(b"alis", bytes(4) + b"external alias data")),
        _dinf(atom(b"url ", struct.pack(">I", 1)), atom(b"url ", struct.pack(">I", 1))),
        _dinf(),
        _dinf(atom(b"url ", struct.pack(">I", 1)), count=2),
        _dinf(atom(b"url ", struct.pack(">I", 3))),
        _dinf(atom(b"url ", struct.pack(">I", 1)), version_flags=1 << 24),
        _dinf(atom(b"zzzz", struct.pack(">I", 1))),
        _dinf(atom(b"url ", struct.pack(">I", 1) + b"extra")),
        _dinf(atom(b"url ", struct.pack(">I", (1 << 24) | 1))),
    ],
    ids=[
        "missing",
        "external-url-index-one",
        "external-alias-index-one",
        "multiple-refs",
        "no-refs",
        "count-mismatch",
        "unknown-entry-flags",
        "dref-version",
        "unknown-entry-type",
        "self-contained-with-extra-data",
        "entry-version",
    ],
)
@pytest.mark.parametrize(
    "handler", [b"vide", b"meta", b"tmcd"], ids=["video", "metadata", "timecode"]
)
def test_actual_data_reference_must_prove_locality(
    tmp_path: Path,
    replacement: bytes,
    handler: bytes,
) -> None:
    payload = (
        timecode_movie()
        if handler == b"tmcd"
        else atom(b"moov", media_track() + media_track(b"meta", b"abcd"))
    )
    path = tmp_path / "references.mov"
    path.write_bytes(_replace_track_dinf(payload, handler, replacement))
    # The tmcd fixture still contains the correct four-byte counter in local
    # mdat. It must not be read as origin when its index 1 is actually external.
    result = inspect_source_timecode(path, RationalRate(50, 1))
    assert result.status == "unsupported"
    assert result.origin_ticks is None
    assert "reference" in result.reason or "dinf" in result.reason


@pytest.mark.parametrize(
    "kind", [b"url ", b"alis"], ids=["self-contained-url", "self-contained-alias"]
)
@pytest.mark.parametrize(
    "handler", [b"vide", b"meta", b"tmcd"], ids=["video", "metadata", "timecode"]
)
def test_supported_self_contained_data_reference_forms(
    tmp_path: Path,
    kind: bytes,
    handler: bytes,
) -> None:
    payload = (
        timecode_movie()
        if handler == b"tmcd"
        else atom(b"moov", media_track() + media_track(b"meta", b"abcd"))
    )
    path = tmp_path / "local.mov"
    path.write_bytes(_replace_track_dinf(payload, handler, local_dinf(kind)))
    result = inspect_source_timecode(path, RationalRate(50, 1))
    assert result.status == ("known_origin" if handler == b"tmcd" else "confirmed_absent")
    assert result.require_origin() == (5_513_332_800 if handler == b"tmcd" else 0)
