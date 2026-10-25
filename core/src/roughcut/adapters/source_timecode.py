"""Bounded FCPXML source-origin evidence, selected by structure, never filename.

No subprocess or runtime tool is needed. MXF timecode and private metadata need
an independently validated Final Cut mapping; ffprobe tags cannot supply it.
"""

from __future__ import annotations

import struct
from pathlib import Path

from roughcut.adapters.quicktime_timecode import (
    SourceTimecodeResolution,
    _inspect_quicktime,
    _invalid,
    _Reader,
)
from roughcut.domain.nle_handoff import NleHandoffError
from roughcut.domain.time import RationalRate


def inspect_source_timecode(
    path: Path,
    source_rate: RationalRate | None,
    expected_media_types: tuple[str, ...] | None = None,
) -> SourceTimecodeResolution:
    """Resolve one source with one open and bounded metadata reads.

    Both audio and video originals must resolve. Generated placeholder media is
    owned by the application and explicitly retains a zero origin there.
    """
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            reader = _Reader(stream, stream.tell())
            if reader.size < 8:
                return SourceTimecodeResolution("unknown", None, "source header is too short")
            header = reader.read(0, min(12, reader.size))
            if header.startswith(b"\x06\x0e\x2b\x34"):
                return SourceTimecodeResolution(
                    "unsupported",
                    None,
                    "MXF/KLV has no validated Final Cut source-origin adapter",
                )
            if header[:4] in {b"RF64", b"BW64"}:
                return SourceTimecodeResolution("unsupported", None, "RF64/BW64 is not supported")
            if header[:4] == b"RIFF":
                if expected_media_types is not None and set(expected_media_types) != {"audio"}:
                    raise _invalid("WAVE audio differs from verified source media types")
                return _wave_origin(reader, header)
            if header[4:8] in {b"moof", b"mfra", b"styp"}:
                raise _invalid("fragmented QuickTime/ISO BMFF is not supported")
            if header[4:8] in {b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip"}:
                return _inspect_quicktime(reader, source_rate, expected_media_types)
            return SourceTimecodeResolution(
                "unknown", None, "unrecognized source container; no verified origin adapter"
            )
    except NleHandoffError as error:
        return SourceTimecodeResolution(
            "unsupported", None, str(error).removeprefix("Roughcut NLE source timecode: ")
        )
    except OSError:
        return SourceTimecodeResolution("unsupported", None, "source is unreadable")


def _wave_origin(reader: _Reader, header: bytes) -> SourceTimecodeResolution:
    if len(header) != 12 or header[8:] != b"WAVE":
        raise _invalid("RIFF form is not a supported WAVE source")
    if int.from_bytes(header[4:8], "little") + 8 != reader.size:
        raise _invalid("invalid WAVE RIFF extent")
    offset = 12
    formats = 0
    data_chunks = 0
    while offset < reader.size:
        reader.atom_count += 1
        if reader.atom_count > 10_000 or reader.size - offset < 8:
            raise _invalid("invalid or excessive WAVE chunk headers")
        kind, length = struct.unpack("<4sI", reader.read(offset, 8))
        body = offset + 8
        end = body + length
        offset = end + (length % 2)
        if offset > reader.size:
            raise _invalid("WAVE chunk exceeds RIFF extent")
        if kind == b"fmt ":
            formats += 1
            if length not in {16, 18}:
                raise _invalid("unsupported WAVE format description")
            fmt = reader.read(body, length)
            tag, channels, rate, byte_rate, block, bits = struct.unpack("<HHIIHH", fmt[:16])
            if (
                tag not in {1, 3}
                or not channels
                or not rate
                or not bits
                or bits % 8
                or block != channels * (bits // 8)
                or byte_rate != rate * block
                or (length == 18 and fmt[16:] != bytes(2))
                or (tag == 3 and bits not in {32, 64})
            ):
                raise _invalid("unsupported or invalid PCM/float WAVE format")
        elif kind == b"data":
            data_chunks += 1
            if not length:
                raise _invalid("empty WAVE audio data")
        elif kind == b"LIST":
            if length < 4 or reader.read(body, 4) != b"INFO":
                raise _invalid("unsupported WAVE LIST metadata")
        elif kind not in {b"JUNK", b"PAD ", b"fact"}:
            raise _invalid(f"unsupported WAVE metadata chunk {kind!r}; origin is unverified")
    if formats != 1 or data_chunks != 1:
        raise _invalid("WAVE requires one format and one audio data chunk")
    return SourceTimecodeResolution("confirmed_absent", 0, "plain PCM/float WAVE without timecode")
