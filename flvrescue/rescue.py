"""Range-based, multi-pass rescue engine."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Callable, Protocol

from .mapfile import MapValidationError, RecoveryRange, RescueMap, load_map, save_map_atomic
from .progress import ProgressReporter
from .reader import FileReader, Reader


DEFAULT_BLOCK_SIZE = 8 * 1024 * 1024
DEFAULT_FALLBACK_SIZE = 64 * 1024
DEFAULT_SECTOR_SIZE = 4 * 1024
DEFAULT_CHECKPOINT_INTERVAL = 64 * 1024 * 1024
DEFAULT_SLOW_THRESHOLD = 2.0
DEFAULT_SKIP_START = 8 * 1024 * 1024
DEFAULT_SKIP_MAX = 1024 * 1024 * 1024
DEFAULT_MAX_PASS = 2


class ProgressSink(Protocol):
    def update(self, **values: object) -> object: ...

    def begin_read(self, offset: int, size: int, *, status: str = "reading") -> object: ...

    def end_read(self, *, status: str) -> object: ...

    def event(self, kind: str, message: str) -> object: ...

    def close(self) -> object: ...


@dataclass(frozen=True)
class RescueResult:
    source: Path
    destination: Path
    map_path: Path
    source_size: int
    current_pass: int
    ranges: tuple[RecoveryRange, ...]

    @property
    def recovered_bytes(self) -> int:
        return sum(item.length for item in self.ranges if item.status == "recovered")

    @property
    def skipped_bytes(self) -> int:
        return sum(item.length for item in self.ranges if item.status == "skipped")

    @property
    def unreadable_bytes(self) -> int:
        return sum(item.length for item in self.ranges if item.status == "unreadable")

    @property
    def unprocessed_bytes(self) -> int:
        return sum(item.length for item in self.ranges if item.status == "unprocessed")

    @property
    def bad_ranges(self) -> tuple[RecoveryRange, ...]:
        return tuple(item for item in self.ranges if item.status == "unreadable")

    @property
    def completed_until(self) -> int:
        frontier = 0
        for item in self.ranges:
            if item.status == "unprocessed":
                break
            frontier = item.end
        return frontier


@dataclass(frozen=True)
class ReadOutcome:
    data: bytes | None
    elapsed: float
    failure: str | None

    @property
    def succeeded(self) -> bool:
        return self.data is not None


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


def _write_at(handle: object, offset: int, data: bytes) -> None:
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
    source_size: int,
) -> ProgressSink | None:
    if progress is True:
        return ProgressReporter(source_size)
    if progress is False or progress is None:
        return None
    required = ("update", "begin_read", "end_read", "event")
    if not all(hasattr(progress, name) for name in required):
        raise TypeError("custom progress objects must implement update/read/event methods")
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
    slow_threshold: float = DEFAULT_SLOW_THRESHOLD,
    skip_start: int = DEFAULT_SKIP_START,
    skip_max: int = DEFAULT_SKIP_MAX,
    max_pass: int = DEFAULT_MAX_PASS,
    clock: Callable[[], float] = time.monotonic,
) -> RescueResult:
    """Recover easy ranges first, then refine skipped and unreadable ranges."""

    _validate_sizes(block_size, fallback_size, sector_size)
    for name, value in {
        "checkpoint_interval": checkpoint_interval,
        "skip_start": skip_start,
        "skip_max": skip_max,
    }.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if skip_start > skip_max:
        raise ValueError("skip_start must not exceed skip_max")
    if isinstance(slow_threshold, bool) or not isinstance(slow_threshold, (int, float)):
        raise ValueError("slow_threshold must be a positive number")
    if slow_threshold <= 0:
        raise ValueError("slow_threshold must be a positive number")
    if isinstance(max_pass, bool) or not isinstance(max_pass, int) or max_pass not in (1, 2, 3):
        raise ValueError("max_pass must be 1, 2, or 3")

    source_path = _canonical_path(source)
    destination_path = _canonical_path(destination)
    if _same_path(source_path, destination_path):
        raise ValueError("source and destination must be different files")
    resolved_map_path = (
        _canonical_path(map_path) if map_path is not None else _default_map_path(destination_path)
    )
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
        required_end = max(
            (item.end for item in state.ranges if item.status == "recovered"),
            default=0,
        )
        if destination_size < required_end or destination_size > source_size:
            raise MapValidationError(
                "destination size is incompatible with committed recovered ranges"
            )
        destination_handle = destination_path.open("r+b")
        if destination_size < source_size:
            destination_handle.truncate(source_size)
            _flush_destination(destination_handle)
    else:
        if destination_path.exists():
            raise FileExistsError(
                "destination already exists without a matching rescue map; refusing to overwrite it"
            )
        destination_handle = destination_path.open("x+b")
        destination_handle.truncate(source_size)
        _flush_destination(destination_handle)
        save_map_atomic(resolved_map_path, state)

    reporter = _make_progress(progress, source_size)
    factory = reader_factory or FileReader
    reader: Reader | None = None
    dirty_bytes = 0

    def checkpoint() -> None:
        nonlocal dirty_bytes
        _flush_destination(destination_handle)
        save_map_atomic(resolved_map_path, state)
        dirty_bytes = 0

    def event(kind: str, message: str) -> None:
        if reporter is not None:
            reporter.event(kind, message)

    def report(status: str, *, force: bool = False) -> None:
        if reporter is not None:
            reporter.update(
                current_pass=min(state.current_pass, 3),
                pass_cursor=state.pass_cursor,
                recovered=state.recovered_bytes,
                skipped=state.skipped_bytes,
                slow=state.slow_bytes,
                unreadable=state.unreadable_bytes,
                status=status,
                force=force,
            )

    def read_once(offset: int, length: int, *, status: str) -> ReadOutcome:
        if reporter is not None:
            reporter.begin_read(offset, length, status=status)
        started = clock()
        try:
            try:
                raw = reader.read_at(offset, length)  # type: ignore[union-attr]
            except OSError:
                elapsed = max(0.0, clock() - started)
                if reporter is not None:
                    reporter.end_read(status="error")
                return ReadOutcome(None, elapsed, "read error")
            if not isinstance(raw, (bytes, bytearray, memoryview)):
                raise TypeError("Reader.read_at() must return bytes-like data")
            elapsed = max(0.0, clock() - started)
            if len(raw) != length:
                if reporter is not None:
                    reporter.end_read(status="error")
                return ReadOutcome(None, elapsed, "short read")
            data = bytes(raw)
            if reporter is not None:
                reporter.end_read(status="slow" if elapsed >= slow_threshold else "normal")
            return ReadOutcome(data, elapsed, None)
        except BaseException:
            if reporter is not None:
                reporter.end_read(status="error")
            raise

    def write_recovered(offset: int, data: bytes, *, slow: bool) -> None:
        nonlocal dirty_bytes
        _write_at(destination_handle, offset, data)
        state.replace_range(offset, len(data), "recovered", "slow" if slow else None)
        dirty_bytes += len(data)

    def zero_unreadable(offset: int, length: int) -> None:
        nonlocal dirty_bytes
        _write_at(destination_handle, offset, b"\0" * length)
        state.replace_range(offset, length, "unreadable", "read_error")
        dirty_bytes += length

    def maybe_checkpoint(*, force: bool = False) -> None:
        if force or dirty_bytes >= checkpoint_interval:
            checkpoint()

    def apply_adaptive_skip(cause: str) -> bool:
        current = state.range_at(state.pass_cursor)
        if current is None or current.status != "unprocessed":
            return False
        width = state.adaptive_skip or skip_start
        length = min(width, current.end - state.pass_cursor)
        offset = state.pass_cursor
        state.replace_range(offset, length, "skipped", cause)
        state.pass_cursor += length
        state.adaptive_skip = min(skip_max, max(skip_start, width * 2))
        event("skip", f"Skip {length} bytes at offset {offset}; next width {state.adaptive_skip}")
        return True

    def run_pass1() -> None:
        state.pass_cursor = min(state.pass_cursor, source_size)
        report("normal", force=True)
        while state.pass_cursor < source_size:
            item = state.range_at(state.pass_cursor)
            if item is None:
                break
            if item.status != "unprocessed":
                state.pass_cursor = item.end
                continue
            offset = state.pass_cursor
            length = min(block_size, item.end - offset)
            outcome = read_once(offset, length, status="reading")
            slow = outcome.succeeded and outcome.elapsed >= slow_threshold
            if outcome.data is not None:
                write_recovered(offset, outcome.data, slow=slow)
                state.pass_cursor += length
                if slow:
                    event("slow", f"Slow read at offset {offset}: {outcome.elapsed:.2f}s")
                    state.adaptive_skip = state.adaptive_skip or skip_start
                    apply_adaptive_skip("slow")
                    maybe_checkpoint(force=True)
                    report("skip")
                    continue
                if state.adaptive_skip:
                    event("normal", f"Normal read speed rediscovered at offset {offset}")
                    state.adaptive_skip = 0
            else:
                zero_unreadable(offset, length)
                state.pass_cursor += length
                event("error", f"Block read failure at offset {offset}: {outcome.failure}")
                state.adaptive_skip = state.adaptive_skip or skip_start
                apply_adaptive_skip("read_error")
                maybe_checkpoint(force=True)
                report("skip")
                continue
            maybe_checkpoint()
            report("normal")
        state.current_pass = 2
        state.pass_cursor = 0
        state.adaptive_skip = 0
        checkpoint()
        event("normal", "Pass 1 complete; switching to Pass 2")
        report("normal", force=True)

    def apply_pass2_jump() -> bool:
        current = state.range_at(state.pass_cursor)
        if current is None or current.status != "skipped":
            return False
        width = state.adaptive_skip or skip_start
        length = min(width, current.end - state.pass_cursor)
        offset = state.pass_cursor
        state.pass_cursor += length
        state.adaptive_skip = min(skip_max, max(skip_start, width * 2))
        event("skip", f"Pass 2 probe jump {length} bytes from offset {offset}")
        return True

    def run_pass2() -> None:
        report("normal", force=True)
        while state.pass_cursor < source_size:
            item = state.range_at(state.pass_cursor)
            if item is None:
                break
            if item.status != "skipped":
                state.pass_cursor = item.end
                continue
            offset = state.pass_cursor
            length = min(fallback_size, item.end - offset)
            outcome = read_once(offset, length, status="reading")
            slow = outcome.succeeded and outcome.elapsed >= slow_threshold
            if outcome.data is not None:
                write_recovered(offset, outcome.data, slow=slow)
                state.pass_cursor += length
                if slow:
                    event("slow", f"Slow Pass 2 probe at offset {offset}: {outcome.elapsed:.2f}s")
                    state.adaptive_skip = state.adaptive_skip or skip_start
                    apply_pass2_jump()
                    maybe_checkpoint(force=True)
                    report("skip")
                    continue
                if state.adaptive_skip:
                    event("normal", f"Normal region rediscovered in Pass 2 at offset {offset}")
                    state.adaptive_skip = 0
            else:
                zero_unreadable(offset, length)
                state.pass_cursor += length
                event("error", f"Pass 2 probe failure at offset {offset}: {outcome.failure}")
                state.adaptive_skip = state.adaptive_skip or skip_start
                apply_pass2_jump()
                maybe_checkpoint(force=True)
                report("skip")
                continue
            maybe_checkpoint()
            report("normal")
        state.current_pass = 3
        state.pass_cursor = 0
        state.adaptive_skip = 0
        checkpoint()
        event("normal", "Pass 2 complete; Deep recovery remains optional")
        report("normal", force=True)

    def recover_sectors(offset: int, length: int) -> None:
        cursor = offset
        end = offset + length
        while cursor < end:
            size = min(sector_size, end - cursor)
            outcome = read_once(cursor, size, status="reading")
            if outcome.data is None:
                zero_unreadable(cursor, size)
            else:
                write_recovered(
                    cursor,
                    outcome.data,
                    slow=outcome.elapsed >= slow_threshold,
                )
            cursor += size

    def recover_fallback_piece(offset: int, length: int) -> None:
        outcome = read_once(offset, length, status="reading")
        if outcome.data is not None:
            write_recovered(offset, outcome.data, slow=outcome.elapsed >= slow_threshold)
        elif length <= sector_size:
            zero_unreadable(offset, length)
        else:
            recover_sectors(offset, length)

    def recover_deep_block(offset: int, length: int) -> bool:
        outcome = read_once(offset, length, status="reading")
        if outcome.data is not None:
            write_recovered(offset, outcome.data, slow=outcome.elapsed >= slow_threshold)
            return False
        event("error", f"Deep block read failure at offset {offset}; localizing")
        if length <= sector_size:
            zero_unreadable(offset, length)
        elif length <= fallback_size:
            recover_sectors(offset, length)
        else:
            cursor = offset
            end = offset + length
            while cursor < end:
                size = min(fallback_size, end - cursor)
                recover_fallback_piece(cursor, size)
                cursor += size
        return True

    def run_pass3() -> None:
        report("normal", force=True)
        while state.pass_cursor < source_size:
            item = state.range_at(state.pass_cursor)
            if item is None:
                break
            if item.status not in ("skipped", "unreadable"):
                state.pass_cursor = item.end
                continue
            offset = state.pass_cursor
            length = min(block_size, item.end - offset)
            localized = recover_deep_block(offset, length)
            state.pass_cursor += length
            maybe_checkpoint(force=localized)
            report("error" if localized else "normal")
        state.current_pass = 4
        state.pass_cursor = source_size
        state.adaptive_skip = 0
        checkpoint()
        event("complete", "Pass 3 complete")
        report("complete", force=True)

    try:
        reader = factory(source_path)
        report("starting", force=True)
        while state.current_pass <= max_pass:
            if state.current_pass == 1:
                run_pass1()
            elif state.current_pass == 2:
                run_pass2()
            elif state.current_pass == 3:
                run_pass3()
            else:
                break
        checkpoint()
        report("complete" if state.current_pass == 4 else "normal", force=True)
        return RescueResult(
            source=source_path,
            destination=destination_path,
            map_path=resolved_map_path,
            source_size=source_size,
            current_pass=state.current_pass,
            ranges=tuple(state.ranges),
        )
    except KeyboardInterrupt:
        checkpoint()
        event("error", "Interrupted; destination and map checkpointed")
        report("error", force=True)
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
