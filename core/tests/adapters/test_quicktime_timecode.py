from __future__ import annotations

import struct
from pathlib import Path

import pytest

from roughcut.adapters.quicktime_timecode import read_quicktime_source_origin
from roughcut.domain.nle_handoff import NleHandoffError
from roughcut.domain.time import TICKS_PER_SECOND, RationalRate


def atom(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I4s", len(body) + 8, kind) + body


def local_dinf(kind: bytes = b"url ") -> bytes:
    return atom(b"dinf", atom(b"dref", struct.pack(">II", 0, 1) + atom(kind, struct.pack(">I", 1))))


def media_track(handler: bytes = b"vide", sample: bytes = b"avc1") -> bytes:
    padding = 70 if handler == b"vide" else 20 if handler == b"soun" else 0
    description = atom(sample, bytes(6) + b"\x00\x01" + bytes(padding))
    stsd = atom(b"stsd", struct.pack(">II", 0, 1) + description)
    return atom(
        b"trak",
        atom(
            b"mdia",
            atom(b"hdlr", bytes(8) + handler) + atom(b"minf", local_dinf() + atom(b"stbl", stsd)),
        ),
    )


def timecode_movie(
    *,
    counter: int = 2_297_222,
    rate: int = 50,
    co64: bool = False,
    edit_time: int = 0,
    duplicate: bool = False,
    audio_only: bool = False,
) -> bytes:
    """Synthetic standard tmcd metadata and a four-byte media sample; no video."""
    ftyp = atom(b"ftyp", b"isom\x00\x00\x00\x00isom")
    sample_offset = len(ftyp) + 8
    mdat = atom(b"mdat", struct.pack(">i", counter))
    description = atom(
        b"tmcd", bytes(6) + struct.pack(">HIIIIB", 1, 0, 2, rate * 1000, 1000, rate) + bytes(3)
    )
    table = atom(b"stsd", struct.pack(">II", 0, 1) + description)
    table += atom(b"stts", struct.pack(">IIII", 0, 1, 1, 5_000_000))
    table += atom(b"stsc", struct.pack(">IIIII", 0, 1, 1, 1, 1))
    table += atom(b"stsz", struct.pack(">III", 0, 4, 1))
    table += atom(
        b"co64" if co64 else b"stco",
        struct.pack(">II", 0, 1) + sample_offset.to_bytes(8 if co64 else 4, "big"),
    )
    handler = atom(b"hdlr", struct.pack(">II4s", 0, 0, b"tmcd"))
    media = atom(b"mdia", handler + atom(b"minf", local_dinf() + atom(b"stbl", table)))
    edits = atom(b"edts", atom(b"elst", struct.pack(">IIIiI", 0, 1, 100, edit_time, 0x10000)))
    track = atom(b"trak", edits + media)
    av = media_track(b"soun", b"mp4a") if audio_only else media_track()
    return ftyp + mdat + atom(b"moov", av + track + (track if duplicate else b""))


@pytest.mark.parametrize("co64", [False, True])
def test_standard_50fps_timecode_origin_is_exact(tmp_path: Path, co64: bool) -> None:
    path = tmp_path / "arbitrary-name.mp4"
    path.write_bytes(timecode_movie(co64=co64))
    before = path.read_bytes()
    assert read_quicktime_source_origin(path, RationalRate(50, 1)) == 5_513_332_800
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        atom(b"moov", media_track()),
        atom(b"moov", media_track() + media_track(b"meta", b"rtmd")),
    ],
    ids=["no-timecode-track", "private-rtmd-only"],
)
def test_absent_standard_timecode_is_zero(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "source.mov"
    path.write_bytes(payload)
    assert read_quicktime_source_origin(path, RationalRate(50, 1)) == 0


@pytest.mark.parametrize(
    "payload",
    [
        timecode_movie(counter=-1),
        timecode_movie(edit_time=1),
        timecode_movie(duplicate=True),
        timecode_movie()[:-1],
        timecode_movie().replace(
            struct.pack(">IIIII", 0, 1, 1, 1, 1), struct.pack(">IIIII", 0, 1, 1, 2, 1)
        ),
        timecode_movie().replace(
            b"tmcd\x00\x00\x00\x00\x00\x00\x00\x01", b"tmcd\x00\x00\x00\x00\x00\x00\x00\x02"
        ),
    ],
    ids=[
        "negative-counter",
        "shifted-edit",
        "duplicate-timecode-track",
        "truncated-movie",
        "multiple-samples-per-chunk",
        "external-sample-reference",
    ],
)
def test_invalid_or_ambiguous_timecode_fails_closed(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "source.mp4"
    path.write_bytes(payload)
    with pytest.raises(NleHandoffError) as failure:
        read_quicktime_source_origin(path, RationalRate(50, 1))
    assert failure.value.code == "nle_export_source_timecode_unsupported"


def test_timecode_rate_mismatch_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "source.mov"
    path.write_bytes(timecode_movie())
    with pytest.raises(NleHandoffError, match="rate differs"):
        read_quicktime_source_origin(path, RationalRate(25, 1))


def test_25fps_timecode_has_no_filename_dependency(tmp_path: Path) -> None:
    path = tmp_path / "unrelated.bin"
    path.write_bytes(timecode_movie(counter=25, rate=25))
    assert read_quicktime_source_origin(path, RationalRate(25, 1)) == TICKS_PER_SECOND


@pytest.mark.parametrize(
    "payload",
    [
        atom(b"ftyp", b"isom")
        + atom(b"moov", atom(b"trak", atom(b"mdia", atom(b"hdlr", bytes(4_000_001))))),
        atom(b"ftyp", b"isom") + atom(b"free", b"") * 10_001,
        timecode_movie().replace(struct.pack(">III", 0, 4, 1), struct.pack(">III", 0, 4, 2)),
        timecode_movie().replace(
            struct.pack(">IIII", 0, 1, 1, 5_000_000), struct.pack(">IIII", 0, 1, 2, 5_000_000)
        ),
    ],
    ids=[
        "metadata-read-limit",
        "atom-count-limit",
        "multiple-sample-sizes",
        "multiple-sample-times",
    ],
)
def test_excessive_metadata_and_multiple_samples_fail_closed(
    tmp_path: Path, payload: bytes
) -> None:
    path = tmp_path / "source.mp4"
    path.write_bytes(payload)
    with pytest.raises(NleHandoffError):
        read_quicktime_source_origin(path, RationalRate(50, 1))


def test_extended_atom_is_supported(tmp_path: Path) -> None:
    payload = timecode_movie()
    movie_start = payload.index(b"moov") - 4
    movie_body = payload[movie_start + 8 :]
    extended_movie = struct.pack(">I4sQ", 1, b"moov", 16 + len(movie_body)) + movie_body
    path = tmp_path / "source.mp4"
    path.write_bytes(payload[:movie_start] + extended_movie)
    assert read_quicktime_source_origin(path, RationalRate(50, 1)) == 5_513_332_800
