from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ByteRange:
    offset: int
    length: int

    @property
    def end(self) -> int:
        return self.offset + self.length


class FaultInjectingReader:
    """In-memory Reader double that fails every read intersecting a bad range."""

    def __init__(
        self,
        data: bytes,
        bad_ranges: Iterable[tuple[int, int]] = (),
        *,
        interrupt_at_offset: int | None = None,
    ) -> None:
        self.data = data
        self.bad_ranges = tuple(ByteRange(offset, length) for offset, length in bad_ranges)
        self.interrupt_at_offset = interrupt_at_offset
        self.interrupted = False
        self.calls: list[tuple[int, int]] = []
        self.closed = False

    def read_at(self, offset: int, size: int) -> bytes:
        self.calls.append((offset, size))
        if (
            self.interrupt_at_offset is not None
            and offset >= self.interrupt_at_offset
            and not self.interrupted
        ):
            self.interrupted = True
            raise KeyboardInterrupt

        end = offset + size
        for bad_range in self.bad_ranges:
            if offset < bad_range.end and bad_range.offset < end:
                raise OSError("simulated unreadable range")
        return self.data[offset : min(end, len(self.data))]

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FaultInjectingReader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

