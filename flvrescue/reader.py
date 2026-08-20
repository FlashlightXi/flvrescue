"""Offset-based readers used by the rescue engine.

The rescue loop intentionally never relies on the implicit file position after a
read error.  ``FileReader`` therefore seeks immediately before every read.
"""

from __future__ import annotations

from os import PathLike
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Reader(Protocol):
    """The small reader contract that makes I/O failure simulation possible."""

    def read_at(self, offset: int, size: int) -> bytes:
        """Return exactly ``size`` bytes starting at ``offset`` or raise ``OSError``."""


class FileReader:
    """Portable file-backed :class:`Reader`.

    ``os.pread`` would also suit this use case, but is not universally available
    on supported Windows Python builds.  This implementation is deliberately
    simple and is used by one sequential rescue loop only.
    """

    def __init__(self, path: str | PathLike[str]) -> None:
        self.path = Path(path)
        self._file = self.path.open("rb", buffering=0)

    def read_at(self, offset: int, size: int) -> bytes:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if size < 0:
            raise ValueError("size must be non-negative")

        # A failed read can leave the OS/Python file position unspecified.  Do
        # not try to recover it: establish the requested absolute position for
        # every attempt instead.
        self._file.seek(offset)
        return self._file.read(size)

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "FileReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
