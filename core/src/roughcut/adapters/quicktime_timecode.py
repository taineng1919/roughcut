"""Bounded, read-only extraction of a standard QuickTime tmcd source origin.

Only a single, unshifted timecode sample is supported. Private camera metadata
(e.g. RTMD), formatted timecode tags and container timestamps are not origins.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO, Literal

from roughcut.domain.nle_handoff import NleHandoffError
from roughcut.domain.time import TICKS_PER_SECOND, RationalRate


@dataclass(frozen=True)
class _Atom:
    kind: bytes
    payload: int
    end: int


class _Reader:
    def __init__(self, stream: BinaryIO, size: int) -> None:
        self.stream = stream
        self.size = size
        self.atom_count = 0
        self.bytes_read = 0

    def read(self, offset: int, length: int) -> bytes:
        self.bytes_read += length
        if offset < 0 or length < 0 or offset + length > self.size or self.bytes_read > 4_000_000:
            raise _invalid("out-of-bounds or excessive metadata")
        self.stream.seek(offset)
        data = self.stream.read(length)
        if len(data) != length:
            raise _invalid("truncated data")
        return data

    def atoms(self, start: int, end: int) -> list[_Atom]:
        result = []
        while start < end:
            self.atom_count += 1
            if self.atom_count > 10_000 or end - start < 8:
                raise _invalid("invalid or excessive atom headers")
            size, kind = struct.unpack(">I4s", self.read(start, 8))
            header = 8
            if size == 1:
                if end - start < 16:
                    raise _invalid("truncated extended atom")
                size = struct.unpack(">Q", self.read(start + 8, 8))[0]
                header = 16
            elif size == 0:
                size = end - start
            if size < header or start + size > end:
                raise _invalid("invalid atom size")
            result.append(_Atom(kind, start + header, start + size))
            start += size
        return result

    def children(self, atom: _Atom) -> list[_Atom]:
        return self.atoms(atom.payload, atom.end)

    def body(self, atom: _Atom) -> bytes:
        return self.read(atom.payload, atom.end - atom.payload)


def _invalid(reason: str) -> NleHandoffError:
    return NleHandoffError(
        "nle_export_source_timecode_unsupported", f"Roughcut NLE source timecode: {reason}"
    )


def _one(atoms: list[_Atom], kind: bytes) -> _Atom:
    matches = [atom for atom in atoms if atom.kind == kind]
    if len(matches) != 1:
        raise _invalid(f"expected one {kind.decode('ascii')} atom")
    return matches[0]


@dataclass(frozen=True)
class SourceTimecodeResolution:
    """Transient evidence; an unresolved origin must never become an implicit zero."""

    status: Literal["known_origin", "confirmed_absent", "unknown", "unsupported"]
    origin_ticks: int | None
    reason: str

    def __post_init__(self) -> None:
        if self.status not in {"known_origin", "confirmed_absent", "unknown", "unsupported"}:
            raise ValueError("Invalid source timecode resolution status")
        known = self.status in {"known_origin", "confirmed_absent"}
        if known != (self.origin_ticks is not None):
            raise ValueError("Only resolved timecode evidence may contain origin ticks")
        if self.origin_ticks is not None and (
            type(self.origin_ticks) is not int or self.origin_ticks < 0
        ):
            raise ValueError("Source timecode origin must be nonnegative")
        if self.status == "confirmed_absent" and self.origin_ticks != 0:
            raise ValueError("Absent timecode must have origin zero")

    def require_origin(self) -> int:
        if self.origin_ticks is None:
            raise NleHandoffError(
                f"nle_export_source_timecode_{self.status}",
                f"Roughcut NLE source timecode: {self.reason}",
            )
        return self.origin_ticks


def read_quicktime_source_origin(path: Path, source_rate: RationalRate) -> int:
    """Compatibility wrapper; unknown and unsupported sources fail closed."""
    return inspect_quicktime_source_origin(path, source_rate).require_origin()


def inspect_quicktime_source_origin(
    path: Path, source_rate: RationalRate | None
) -> SourceTimecodeResolution:
    """Read bounded standard tmcd evidence without guessing from tags or names."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            reader = _Reader(stream, size)
            if size < 8:
                return SourceTimecodeResolution("unknown", None, "source header is too short")
            if reader.read(4, 4) not in {b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip"}:
                return SourceTimecodeResolution(
                    "unknown", None, "unrecognized source container; no verified origin adapter"
                )
            return _inspect_quicktime(reader, source_rate)
    except NleHandoffError as error:
        return SourceTimecodeResolution(
            "unsupported", None, str(error).removeprefix("Roughcut NLE source timecode: ")
        )
    except OSError:
        return SourceTimecodeResolution("unsupported", None, "source is unreadable")


def _inspect_quicktime(
    reader: _Reader,
    source_rate: RationalRate | None,
    expected_media_types: tuple[str, ...] | None = None,
) -> SourceTimecodeResolution:
    top = reader.atoms(0, reader.size)
    movies = [atom for atom in top if atom.kind == b"moov"]
    if not movies:
        raise _invalid("QuickTime movie metadata is missing")
    movie = _one(top, b"moov")
    movie_children = reader.children(movie)
    if any(atom.kind in {b"moof", b"mfra"} for atom in top) or any(
        atom.kind == b"mvex" for atom in movie_children
    ):
        raise _invalid("fragmented QuickTime/ISO BMFF is not supported")
    tracks = []
    media_types: set[str] = set()
    for track in movie_children:
        if track.kind != b"trak":
            continue
        children = reader.children(track)
        media = reader.children(_one(children, b"mdia"))
        handler = reader.body(_one(media, b"hdlr"))
        if len(handler) < 12:
            raise _invalid("truncated media handler")
        kind = handler[8:12]
        if kind not in {b"vide", b"soun", b"tmcd", b"meta", b"mdta"}:
            raise _invalid(f"unsupported media handler {kind!r}")
        if kind == b"tmcd":
            tracks.append((children, media))
        else:
            _validate_non_timecode_descriptions(reader, media, kind)
            if kind in {b"vide", b"soun"}:
                media_types.add("video" if kind == b"vide" else "audio")
    if not media_types:
        raise _invalid("movie has no verified audio/video tracks")
    if expected_media_types is not None and media_types != set(expected_media_types):
        raise _invalid("container audio/video tracks differ from verified source media types")
    if not tracks:
        return SourceTimecodeResolution(
            "confirmed_absent", 0, "QuickTime/ISO BMFF has no standard timecode track"
        )
    if len(tracks) != 1:
        raise _invalid("multiple timecode tracks")
    timecode_track, media = tracks[0]
    _validate_edits(reader, timecode_track)
    table = _local_sample_table(reader, media)
    if source_rate is None:
        raise _invalid("standard timecode requires a verified nominal source frame rate")
    return SourceTimecodeResolution(
        "known_origin",
        _sample_origin(reader, table, top, source_rate),
        "standard QuickTime tmcd sample",
    )


# Supported standard audio/video sample entries. Unknown descriptions are not
# evidence that timecode is absent. This is container inspection, not a decoder.
_VIDEO_ENTRIES = {
    b"avc1",
    b"avc3",
    b"hvc1",
    b"hev1",
    b"mp4v",
    b"apch",
    b"apcn",
    b"apcs",
    b"apco",
    b"ap4h",
    b"ap4x",
    b"jpeg",
    b"mjpg",
    b"mjpa",
    b"mjpb",
    b"raw ",
    b"v210",
    b"v308",
    b"v408",
    b"v410",
    b"2vuy",
    b"yuv2",
    b"dvc ",
    b"dvcp",
    b"dvhp",
    b"av01",
}
_AUDIO_ENTRIES = {
    b"mp4a",
    b"lpcm",
    b"sowt",
    b"twos",
    b"fl32",
    b"fl64",
    b"in24",
    b"in32",
    b"alac",
    b"ac-3",
    b"ec-3",
    b".mp3",
    b"Opus",
    b"ulaw",
    b"alaw",
    b"raw ",
    b"NONE",
}


def _local_sample_table(reader: _Reader, media: list[_Atom]) -> list[_Atom]:
    """Require the sample's index 1 to resolve to this file, not just exist.

    Only one version-0 self-contained URL/QuickTime alias entry is supported.
    An external offset can coincidentally point into our mdat; it is never local
    evidence without its actual data-reference record.
    """
    info = reader.children(_one(media, b"minf"))
    references = _one(reader.children(_one(info, b"dinf")), b"dref")
    if references.end - references.payload < 8:
        raise _invalid("truncated data reference table")
    if reader.read(references.payload, 8) != struct.pack(">II", 0, 1):
        raise _invalid("unsupported data reference version, flags or entry count")
    entries = reader.atoms(references.payload + 8, references.end)
    if len(entries) != 1:
        raise _invalid("expected one local data reference entry")
    entry = entries[0]
    if entry.kind not in {b"url ", b"alis"}:
        raise _invalid("unsupported data reference entry type")
    if entry.end - entry.payload != 4 or reader.body(entry) != struct.pack(">I", 1):
        raise _invalid("external or unsupported data reference; expected self-contained flag 1")
    return reader.children(_one(info, b"stbl"))


def _validate_non_timecode_descriptions(reader: _Reader, media: list[_Atom], kind: bytes) -> None:
    table = _local_sample_table(reader, media)
    stsd = _one(table, b"stsd")
    if stsd.end - stsd.payload < 8:
        raise _invalid("truncated sample descriptions")
    version, count = struct.unpack(">II", reader.read(stsd.payload, 8))
    entries = reader.atoms(stsd.payload + 8, stsd.end)
    if version or not count or len(entries) != count:
        raise _invalid("invalid sample description count or version")
    for entry in entries:
        if entry.kind in {b"tmcd", b"tc64"}:
            raise _invalid("timecode sample description has a non-timecode handler")
        supported = _VIDEO_ENTRIES if kind == b"vide" else _AUDIO_ENTRIES
        if kind in {b"vide", b"soun"} and entry.kind not in supported:
            raise _invalid(f"unverified audio/video sample description {entry.kind!r}")
        minimum = 78 if kind == b"vide" else 28 if kind == b"soun" else 8
        if entry.end - entry.payload < minimum:
            raise _invalid("truncated audio/video or metadata sample description")
        if reader.read(entry.payload, 8) != bytes(6) + b"\x00\x01":
            raise _invalid("invalid or external sample description reference")


def _validate_edits(reader: _Reader, track: list[_Atom]) -> None:
    edits = [atom for atom in track if atom.kind == b"edts"]
    if not edits:
        return
    edit = _one(reader.children(_one(track, b"edts")), b"elst")
    data = reader.body(edit)
    if len(data) == 20 and data[:8] == struct.pack(">II", 0, 1):
        media_time, rate = struct.unpack(">iI", data[12:20])
    elif len(data) == 28 and data[:8] == struct.pack(">II", 1 << 24, 1):
        media_time, rate = struct.unpack(">qI", data[16:28])
    else:
        raise _invalid("unsupported timecode edit list")
    if media_time != 0 or rate != 0x10000:
        raise _invalid("shifted timecode edit list")


def _sample_origin(
    reader: _Reader, table: list[_Atom], top: list[_Atom], source_rate: RationalRate
) -> int:
    stsd = reader.body(_one(table, b"stsd"))
    if len(stsd) < 42 or stsd[:8] != struct.pack(">II", 0, 1):
        raise _invalid("unsupported timecode sample description")
    entry_size = int.from_bytes(stsd[8:12], "big")
    if entry_size != len(stsd) - 8 or stsd[12:16] != b"tmcd":
        raise _invalid("invalid timecode sample entry")
    if stsd[16:22] != bytes(6) or stsd[22:24] != b"\x00\x01":
        raise _invalid("external timecode sample reference")
    reserved, flags, scale, duration = struct.unpack(">IIII", stsd[24:40])
    frames = stsd[40]
    if reserved or flags & ~0x7 or not scale or not duration or not frames:
        raise _invalid("unsupported timecode format flags or rate")
    rate = Fraction(scale, duration)
    if rate != Fraction(source_rate.numerator, source_rate.denominator):
        raise _invalid("timecode rate differs from source rate")
    if frames != (rate.numerator + rate.denominator - 1) // rate.denominator:
        raise _invalid("inconsistent timecode frame count")
    stts = reader.body(_one(table, b"stts"))
    if len(stts) != 16 or stts[:12] != struct.pack(">III", 0, 1, 1):
        raise _invalid("multiple or invalid timecode samples")
    if int.from_bytes(stts[12:], "big") == 0:
        raise _invalid("zero timecode sample duration")
    stsc = reader.body(_one(table, b"stsc"))
    if stsc != struct.pack(">IIIII", 0, 1, 1, 1, 1):
        raise _invalid("unsupported timecode sample-to-chunk mapping")
    stsz = reader.body(_one(table, b"stsz"))
    if stsz not in {struct.pack(">III", 0, 4, 1), struct.pack(">IIII", 0, 0, 1, 4)}:
        raise _invalid("unsupported timecode sample size")
    chunks = [atom for atom in table if atom.kind in {b"stco", b"co64"}]
    if len(chunks) != 1:
        raise _invalid("ambiguous timecode chunk offsets")
    chunk = chunks[0]
    data = reader.body(chunk)
    width = 4 if chunk.kind == b"stco" else 8
    if len(data) != 8 + width or data[:8] != struct.pack(">II", 0, 1):
        raise _invalid("unsupported timecode chunk count")
    offset = int.from_bytes(data[8:], "big")
    if not any(
        atom.kind == b"mdat" and atom.payload <= offset and offset + 4 <= atom.end for atom in top
    ):
        raise _invalid("timecode sample is outside media data")
    counter = int.from_bytes(reader.read(offset, 4), "big", signed=True)
    if counter < 0:
        raise _invalid("negative timecode sample")
    ticks = Fraction(counter * duration * TICKS_PER_SECOND, scale)
    if ticks.denominator != 1:
        raise _invalid("timecode origin is not representable in ticks")
    return ticks.numerator
