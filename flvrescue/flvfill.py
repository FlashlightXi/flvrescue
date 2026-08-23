"""Write skippable FLV script tags over holes so a demuxer can keep walking."""

from __future__ import annotations

from collections.abc import Sequence
from typing import BinaryIO, Protocol

FLV_SCRIPT_TAG = 18
MAX_TAG_DATA = 0xFFFFFF
MIN_TAG_SIZE = 15


class RangeLike(Protocol):
    offset: int
    length: int
    status: str


def pack_u24(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFF:
        raise ValueError("uint24 out of range")
    return bytes((value >> 16, (value >> 8) & 0xFF, value & 0xFF))


def overlay_flv_placeholders(handle: BinaryIO, offset: int, length: int) -> int:
    """Overlay script-tag headers and footers on an already-zero hole.

    Payload bytes stay zero. A parser that honors ``dataSize`` can skip the
    hole instead of treating raw zeros as a broken tag stream.
    """

    if length <= 0:
        return 0
    covered = 0
    position = offset
    remaining = length
    while remaining >= MIN_TAG_SIZE:
        data_size = min(remaining - MIN_TAG_SIZE, MAX_TAG_DATA)
        header = bytes([FLV_SCRIPT_TAG]) + pack_u24(data_size) + bytes(7)
        handle.seek(position)
        handle.write(header)
        handle.seek(position + 11 + data_size)
        handle.write((11 + data_size).to_bytes(4, "big"))
        step = MIN_TAG_SIZE + data_size
        position += step
        remaining -= step
        covered += step
    return covered


def overlay_map_holes(
    handle: BinaryIO,
    ranges: Sequence[RangeLike],
    *,
    statuses: frozenset[str] = frozenset({"skipped", "unreadable"}),
) -> int:
    covered = 0
    for item in ranges:
        if item.status not in statuses:
            continue
        covered += overlay_flv_placeholders(handle, item.offset, item.length)
    return covered
