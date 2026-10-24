"""Bounded, read-only extraction of a standard QuickTime tmcd source origin.

Only a single, unshifted timecode sample is supported. Private camera metadata
(e.g. RTMD), formatted timecode tags and container timestamps are not origins.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO

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


def read_quicktime_source_origin(path: Path, source_rate: RationalRate) -> int:
    """Return exact origin ticks, or zero for a container without standard tmcd.

    The caller validates the source fingerprint before calling. Metadata reads
    are bounded; media payload is skipped except for the four-byte tmcd sample.
    Unsupported/ambiguous standard timecode fails closed rather than guessing.
    """
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            reader = _Reader(stream, size)
            if size < 8:
                return 0
            if reader.read(4, 4) not in {b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip"}:
                return 0  # Other media containers have no standard QuickTime tmcd.
            top = reader.atoms(0, size)
            movies = [atom for atom in top if atom.kind == b"moov"]
            if not movies:
                raise _invalid("QuickTime movie metadata is missing")
            movie = _one(top, b"moov")
            tracks = []
            for track in reader.children(movie):
                if track.kind != b"trak":
                    continue
                children = reader.children(track)
                media = reader.children(_one(children, b"mdia"))
                handler = reader.body(_one(media, b"hdlr"))
                if len(handler) < 12:
                    raise _invalid("truncated media handler")
                if handler[8:12] == b"tmcd":
                    tracks.append((children, media))
            if not tracks:
                return 0
            if len(tracks) != 1:
                raise _invalid("multiple timecode tracks")
            timecode_track, media = tracks[0]
            _validate_edits(reader, timecode_track)
            table = reader.children(_one(reader.children(_one(media, b"minf")), b"stbl"))
            return _sample_origin(reader, table, top, source_rate)
    except OSError as error:
        raise _invalid("source is unreadable") from error


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
