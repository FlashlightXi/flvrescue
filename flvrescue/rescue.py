"""Forward-only, fault-tolerant file rescue engine."""

from __future__ import annotations

import os
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Callable, Protocol

from .mapfile import BadRange, MapValidationError, RescueMap, load_map, save_map_atomic
from .progress import ProgressReporter
from .reader import FileReader, Reader


DEFAULT_BLOCK_SIZE = 8 * 1024 * 1024
DEFAULT_FALLBACK_SIZE = 64 * 1024
DEFAULT_SECTOR_SIZE = 4 * 1024
DEFAULT_CHECKPOINT_INTERVAL = 64 * 1024 * 1024


class ProgressSink(Protocol):
    def update(
        self,
        processed: int,
        *,
        unreadable: int = 0,
        bad_ranges: int = 0,
        force: bool = False,
    ) -> object: ...

    def close(self) -> object: ...


@dataclass(frozen=True)
class RescueResult:
    source: Path
    destination: Path
    map_path: Path
    source_size: int
    completed_until: int
    bad_ranges: tuple[BadRange, ...]

    @property
    def unreadable_bytes(self) -> int:
        return sum(item.length for item in self.bad_ranges)

    @property
    def recovered_bytes(self) -> int:
        return self.completed_until - self.unreadable_bytes


ReaderFactory = Callable[[Path], Reader]


def _canonical_path(path: str | PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _same_path(left: Path, right: Path) -> bool:
    if os.path.normcase(str(left)) == os.path.normcase(str(right)):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _validate_sizes(block_size: int, fallback_size: int, sector_size: int) -> None:
    values = {
        "block_size": block_size,
        "fallback_size": fallback_size,
        "sector_size": sector_size,
    }
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if block_size < fallback_size or fallback_size < sector_size:
        raise ValueError("block_size >= fallback_size >= sector_size is required")


def _read_exact(reader: Reader, offset: int, size: int) -> bytes | None:
    """Read once; return ``None`` for an I/O error or a short read.

    A short read below the known source size is handled exactly as a failed
    read.  It is important not to silently write shifted/truncated data.
    """

    try:
        data = reader.read_at(offset, size)
    except OSError:
        return None
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("Reader.read_at() must return bytes-like data")
    if len(data) != size:
        return None
    return bytes(data)


def _write_at(handle: object, offset: int, data: bytes) -> None:
    """Write all bytes at an explicit logical output offset."""

    # Binary file handles return the number of bytes written, although a short
    # write is rare for regular files.  Handle it instead of advancing the
    # resume point past data that was never handed to the operating system.
    handle.seek(offset)  # type: ignore[attr-defined]
    view = memoryview(data)
    while view:
        written = handle.write(view)  # type: ignore[attr-defined]
        if written is None:
            written = len(view)
        if not isinstance(written, int) or written <= 0:
            raise OSError("short write to destination")
        view = view[written:]


def _flush_destination(handle: object) -> None:
    handle.flush()  # type: ignore[attr-defined]
    os.fsync(handle.fileno())  # type: ignore[attr-defined]


def _default_map_path(destination: Path) -> Path:
    return destination.with_name(f"{destination.name}.rescue.json")


def _load_or_create_state(
    map_path: Path,
    source: Path,
    source_size: int,
    destination: Path,
) -> tuple[RescueMap, bool]:
    if not map_path.exists():
        return (
            RescueMap(
                source_path=str(source),
                source_size=source_size,
                destination_path=str(destination),
            ),
            False,
        )

    state = load_map(map_path)
    if state.source_path != str(source):
        raise MapValidationError("map source_path does not match the requested source")
    if state.destination_path != str(destination):
        raise MapValidationError("map destination_path does not match the requested destination")
    if state.source_size != source_size:
        raise MapValidationError("map source_size does not match the requested source")
    return state, True


def _make_progress(
    progress: ProgressSink | bool | None,
    source: Path,
    source_size: int,
) -> ProgressSink | None:
    if progress is True:
        return ProgressReporter(source, source_size)
    if progress is False or progress is None:
        return None
    if not hasattr(progress, "update"):
        raise TypeError("progress must be None, bool, or an object with update()")
    return progress


def rescue(
    source: str | PathLike[str],
    destination: str | PathLike[str],
    *,
    map_path: str | PathLike[str] | None = None,
    reader_factory: ReaderFactory | None = None,
    progress: ProgressSink | bool | None = None,
    block_size: int = DEFAULT_BLOCK_SIZE,
    fallback_size: int = DEFAULT_FALLBACK_SIZE,
    sector_size: int = DEFAULT_SECTOR_SIZE,
    checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL,
) -> RescueResult:
    """Rescue one file with a single forward-only read pass.

    The source is only opened read-only.  For a normal block, exactly one read
    is attempted; a failed normal read is localized with one pass of fallback
    reads and then one pass of sector reads.  Failed sector reads are zero-filled.

    ``KeyboardInterrupt`` is intentionally re-raised *after* the destination
    and map have been flushed.  The CLI converts that interruption into a calm,
    traceback-free completion message; library callers retain normal Python
    cancellation semantics.
    """

    _validate_sizes(block_size, fallback_size, sector_size)
    if isinstance(checkpoint_interval, bool) or not isinstance(checkpoint_interval, int):
        raise ValueError("checkpoint_interval must be a positive integer")
    if checkpoint_interval <= 0:
        raise ValueError("checkpoint_interval must be a positive integer")

    source_path = _canonical_path(source)
    destination_path = _canonical_path(destination)
    if _same_path(source_path, destination_path):
        raise ValueError("source and destination must be different files")
    resolved_map_path = _canonical_path(map_path) if map_path is not None else _default_map_path(destination_path)
    # A sidecar is written with os.replace.  It must never alias either data
    # file (including through an existing hard link), or a checkpoint could
    # replace that file with JSON.
    if _same_path(resolved_map_path, source_path):
        raise ValueError("map_path must be different from the source file")
    if _same_path(resolved_map_path, destination_path):
        raise ValueError("map_path must be different from the destination file")
    source_size = source_path.stat().st_size

    state, resuming = _load_or_create_state(
        resolved_map_path, source_path, source_size, destination_path
    )
    if resuming:
        if not destination_path.is_file():
            raise FileNotFoundError("destination from resume map does not exist")
        destination_size = destination_path.stat().st_size
        if destination_size < state.completed_until or destination_size > source_size:
            raise MapValidationError(
                "destination size is incompatible with the resume map and source_size"
            )
        destination_handle = destination_path.open("r+b")
    else:
        if destination_path.exists():
            raise FileExistsError(
                "destination already exists without a matching rescue map; refusing to overwrite it"
        )
        destination_handle = destination_path.open("x+b")
        # Create and durably publish an empty destination before its first map.
        # While interrupted output is only a verified contiguous prefix, the
        # completed final output reaches ``source_size`` through explicit writes.
        _flush_destination(destination_handle)
        save_map_atomic(resolved_map_path, state)

    reporter = _make_progress(progress, source_path, source_size)
    factory = reader_factory or FileReader
    reader: Reader | None = None
    frontier = state.completed_until

    def checkpoint() -> None:
        """Make the contiguous prefix durable before advertising it in the map."""

        nonlocal state
        if frontier < state.completed_until:
            raise RuntimeError("rescue progress moved backwards")
        _flush_destination(destination_handle)
        state.completed_until = frontier
        save_map_atomic(resolved_map_path, state)

    def report(*, force: bool = False) -> None:
        if reporter is not None:
            reporter.update(
                frontier,
                unreadable=state.unreadable_bytes,
                bad_ranges=len(state.bad_ranges),
                force=force,
            )

    def write_contiguous(offset: int, data: bytes) -> None:
        nonlocal frontier
        if offset != frontier:
            raise RuntimeError("rescue attempted a non-contiguous output write")
        _write_at(destination_handle, offset, data)
        frontier += len(data)

    try:
        reader = factory(source_path)
        report(force=True)
        while frontier < source_size:
            offset = frontier
            length = min(block_size, source_size - offset)
            normal_data = _read_exact(reader, offset, length)
            used_fallback = normal_data is None
            if normal_data is not None:
                write_contiguous(offset, normal_data)
            else:
                fallback_end = offset + length
                fallback_offset = offset
                while fallback_offset < fallback_end:
                    fallback_length = min(fallback_size, fallback_end - fallback_offset)
                    fallback_data = _read_exact(reader, fallback_offset, fallback_length)
                    if fallback_data is not None:
                        write_contiguous(fallback_offset, fallback_data)
                    else:
                        sector_end = fallback_offset + fallback_length
                        sector_offset = fallback_offset
                        while sector_offset < sector_end:
                            sector_length = min(sector_size, sector_end - sector_offset)
                            sector_data = _read_exact(reader, sector_offset, sector_length)
                            if sector_data is None:
                                write_contiguous(sector_offset, b"\x00" * sector_length)
                                state.add_bad_range(sector_offset, sector_length)
                            else:
                                write_contiguous(sector_offset, sector_data)
                            sector_offset += sector_length
                    fallback_offset += fallback_length

            # Fallback can have created new bad ranges, so record it promptly;
            # normal blocks are checkpointed at the lower-frequency interval.
            if used_fallback or frontier - state.completed_until >= checkpoint_interval:
                checkpoint()
            report()

        checkpoint()
        report(force=True)
        return RescueResult(
            source=source_path,
            destination=destination_path,
            map_path=resolved_map_path,
            source_size=source_size,
            completed_until=state.completed_until,
            bad_ranges=tuple(state.bad_ranges),
        )
    except KeyboardInterrupt:
        # ``frontier`` only advances after a complete explicit-offset write.
        # Commit that contiguous prefix so the next invocation need not reread it.
        checkpoint()
        report(force=True)
        raise
    finally:
        if reader is not None:
            close = getattr(reader, "close", None)
            if callable(close):
                close()
        destination_handle.close()
        if reporter is not None:
            close_reporter = getattr(reporter, "close", None)
            if callable(close_reporter):
                close_reporter()
