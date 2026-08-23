"""Range-based, multi-pass rescue engine."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Callable, Protocol

from .mapfile import MapValidationError, RecoveryRange, RescueMap, load_map, save_map_atomic
from .policy import DEFAULT_POLICY, PASS_NAME_TO_NUMBER, RecoveryPolicy
from .progress import ProgressReporter
from .reader import FileReader, Reader
from .storage import prepare_destination, zero_destination_range


# Legacy public constants remain available; DEFAULT_POLICY is the authoritative
# source for their values.
DEFAULT_BLOCK_SIZE = DEFAULT_POLICY.block
DEFAULT_FALLBACK_SIZE = DEFAULT_POLICY.fallback
DEFAULT_SECTOR_SIZE = DEFAULT_POLICY.sector
DEFAULT_CHECKPOINT_INTERVAL = DEFAULT_POLICY.checkpoint
DEFAULT_SLOW_THRESHOLD = DEFAULT_POLICY.slow_threshold
DEFAULT_SKIP_START = DEFAULT_POLICY.skip_start
DEFAULT_SKIP_MAX = DEFAULT_POLICY.skip_max
DEFAULT_SKIP_FACTOR = DEFAULT_POLICY.skip_factor
DEFAULT_SKIP_RESET_AFTER = DEFAULT_POLICY.skip_reset_after
DEFAULT_SURVEY_STRIDE = DEFAULT_POLICY.survey_stride
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
    def easy_skipped_bytes(self) -> int:
        return sum(item.length for item in self.ranges if item.status == "skipped" and item.cause == "survey")

    @property
    def hard_skipped_bytes(self) -> int:
        return self.skipped_bytes - self.easy_skipped_bytes

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


def next_skip_width(width: int, *, skip_start: int, skip_max: int, skip_factor: int, grow: bool) -> int:
    """Return the adaptive width used for the next jump."""

    current = width if width > 0 else skip_start
    if grow:
        grown = current * skip_factor
        current = grown if grown > current else current + skip_start
    return min(skip_max, max(skip_start, current))


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


def _load_or_create_state(map_path: Path, source: Path, source_size: int, destination: Path) -> tuple[RescueMap, bool]:
    if not map_path.exists():
        return RescueMap(str(source), source_size, str(destination)), False
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
    label: str,
    destination_label: str,
) -> ProgressSink | None:
    if progress is True:
        return ProgressReporter(
            source_size, label=label, destination_label=destination_label
        )
    if progress is False or progress is None:
        return None
    required = ("update", "begin_read", "end_read", "event")
    if not all(hasattr(progress, name) for name in required):
        raise TypeError("custom progress objects must implement update/read/event methods")
    return progress


def _policy_overrides(
    *, block_size: int | None, fallback_size: int | None, sector_size: int | None,
    checkpoint_interval: int | None, slow_threshold: float | None, skip_start: int | None,
    skip_max: int | None, skip_factor: int | None, skip_reset_after: int | None,
    survey_stride: int | None,
) -> dict[str, object]:
    values = {
        "block": block_size, "fallback": fallback_size, "sector": sector_size,
        "checkpoint": checkpoint_interval, "slow_threshold": slow_threshold,
        "skip_start": skip_start, "skip_max": skip_max, "skip_factor": skip_factor,
        "skip_reset_after": skip_reset_after, "survey_stride": survey_stride,
    }
    return {name: value for name, value in values.items() if value is not None}


def _resolve_policy(
    state: RescueMap,
    *,
    resuming: bool,
    supplied_policy: RecoveryPolicy | None,
    overrides: dict[str, object],
) -> RecoveryPolicy:
    if supplied_policy is not None and not isinstance(supplied_policy, RecoveryPolicy):
        raise TypeError("policy must be a RecoveryPolicy or None")
    if state.policy is None:
        try:
            base_policy = supplied_policy or DEFAULT_POLICY
            state.policy = base_policy.with_overrides(**overrides)
        except ValueError as exc:
            raise ValueError(f"invalid recovery policy: {exc}") from exc
        return state.policy
    if not resuming:
        raise RuntimeError("a new rescue map cannot already have a policy")
    if supplied_policy is not None and supplied_policy != state.policy:
        raise ValueError("recovery policy does not match the policy saved in the resume map")
    try:
        requested = state.policy.with_overrides(**overrides)
    except ValueError as exc:
        raise ValueError(f"invalid recovery policy override: {exc}") from exc
    if requested != state.policy:
        changed = [name for name, value in overrides.items() if getattr(state.policy, name) != value]
        raise ValueError(
            "recovery policy is already saved in the resume map; refusing to change "
            + ", ".join(changed)
        )
    return state.policy


def _pass_number(value: int | str, name: str) -> int:
    if isinstance(value, str):
        try:
            return PASS_NAME_TO_NUMBER[value.lower()]
        except KeyError as exc:
            raise ValueError(f"{name} must be one of {', '.join(PASS_NAME_TO_NUMBER)}") from exc
    if isinstance(value, bool) or not isinstance(value, int) or value not in (1, 2, 3, 4):
        raise ValueError(f"{name} must be 1, 2, 3, or 4")
    return value


def _resolve_max_pass(max_pass: int | None, through: int | str | None) -> int:
    if max_pass is None and through is None:
        return DEFAULT_MAX_PASS
    by_max = _pass_number(max_pass, "max_pass") if max_pass is not None else None
    by_through = _pass_number(through, "through") if through is not None else None
    if by_max is not None and by_through is not None and by_max != by_through:
        raise ValueError("max_pass and through must name the same execution limit")
    return by_max if by_max is not None else by_through  # type: ignore[return-value]


def rescue(
    source: str | PathLike[str], destination: str | PathLike[str], *,
    map_path: str | PathLike[str] | None = None, reader_factory: ReaderFactory | None = None,
    progress: ProgressSink | bool | None = None, policy: RecoveryPolicy | None = None,
    block_size: int | None = None,
    fallback_size: int | None = None, sector_size: int | None = None,
    checkpoint_interval: int | None = None, slow_threshold: float | None = None,
    skip_start: int | None = None, skip_max: int | None = None, skip_factor: int | None = None,
    skip_reset_after: int | None = None, survey_stride: int | None = None,
    max_pass: int | None = None, through: int | str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RescueResult:
    """Recover through an execution limit without silently changing its policy."""

    execution_limit = _resolve_max_pass(max_pass, through)
    overrides = _policy_overrides(
        block_size=block_size, fallback_size=fallback_size, sector_size=sector_size,
        checkpoint_interval=checkpoint_interval, slow_threshold=slow_threshold,
        skip_start=skip_start, skip_max=skip_max, skip_factor=skip_factor,
        skip_reset_after=skip_reset_after, survey_stride=survey_stride,
    )
    source_path = _canonical_path(source)
    destination_path = _canonical_path(destination)
    if _same_path(source_path, destination_path):
        raise ValueError("source and destination must be different files")
    resolved_map_path = _canonical_path(map_path) if map_path is not None else _default_map_path(destination_path)
    if _same_path(resolved_map_path, source_path):
        raise ValueError("map_path must be different from the source file")
    if _same_path(resolved_map_path, destination_path):
        raise ValueError("map_path must be different from the destination file")
    source_size = source_path.stat().st_size
    state, resuming = _load_or_create_state(resolved_map_path, source_path, source_size, destination_path)
    had_policy = state.policy is not None
    policy = _resolve_policy(
        state,
        resuming=resuming,
        supplied_policy=policy,
        overrides=overrides,
    )
    recovered_end = max((item.end for item in state.ranges if item.status == "recovered"), default=0)
    reporter = _make_progress(
        progress, source_size, source_path.name, destination_path.name
    )
    destination_handle = None
    try:
        if reporter is not None:
            reporter.update(
                current_pass=min(state.current_pass, 4), pass_cursor=state.pass_cursor,
                recovered=state.recovered_bytes, skipped=state.skipped_bytes,
                slow=state.slow_bytes, unreadable=state.unreadable_bytes,
                easy_skipped=state.easy_skipped_bytes, hard_skipped=state.hard_skipped_bytes,
                ranges=tuple(
                    (item.offset, item.length, item.status, item.cause)
                    for item in state.ranges
                ),
                status="preparing", force=True,
            )
        destination_handle, preparation = prepare_destination(
            destination_path, source_size, resuming=resuming, recovered_end=recovered_end
        )
        if reporter is not None:
            reporter.update(
                current_pass=min(state.current_pass, 4), pass_cursor=state.pass_cursor,
                recovered=state.recovered_bytes, skipped=state.skipped_bytes,
                slow=state.slow_bytes, unreadable=state.unreadable_bytes,
                easy_skipped=state.easy_skipped_bytes, hard_skipped=state.hard_skipped_bytes,
                ranges=tuple(
                    (item.offset, item.length, item.status, item.cause)
                    for item in state.ranges
                ),
                status="preparing", storage_mode=preparation.mode, force=True,
            )
            reporter.event("normal", f"Destination storage mode: {preparation.mode}")
        # This save makes legacy v1/v2 maps policy-stable before a source read.
        save_map_atomic(resolved_map_path, state)
    except BaseException:
        if destination_handle is not None:
            destination_handle.close()
        if not resuming:
            try:
                destination_path.unlink()
            except OSError:
                pass
        if reporter is not None:
            close_reporter = getattr(reporter, "close", None)
            if callable(close_reporter):
                close_reporter()
        raise

    assert destination_handle is not None
    factory = reader_factory or FileReader
    reader: Reader | None = None
    dirty_bytes = 0
    fast_streak = 0

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
                current_pass=min(state.current_pass, 4), pass_cursor=state.pass_cursor,
                recovered=state.recovered_bytes, skipped=state.skipped_bytes,
                slow=state.slow_bytes, unreadable=state.unreadable_bytes,
                easy_skipped=state.easy_skipped_bytes, hard_skipped=state.hard_skipped_bytes,
                ranges=tuple((item.offset, item.length, item.status, item.cause) for item in state.ranges),
                status=status, force=force,
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
            if reporter is not None:
                reporter.end_read(status="slow" if elapsed >= policy.slow_threshold else "normal")
            return ReadOutcome(bytes(raw), elapsed, None)
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
        zero_destination_range(
            destination_handle, offset, length, sparse=preparation.sparse
        )
        state.replace_range(offset, length, "unreadable", "read_error")
        dirty_bytes += length

    def maybe_checkpoint(*, force: bool = False) -> None:
        if force or dirty_bytes >= policy.checkpoint:
            checkpoint()

    def apply_adaptive_skip(cause: str, *, grow: bool = True) -> bool:
        current = state.range_at(state.pass_cursor)
        if current is None or current.status != "unprocessed":
            return False
        width = state.adaptive_skip or policy.skip_start
        length = min(width, current.end - state.pass_cursor)
        offset = state.pass_cursor
        state.replace_range(offset, length, "skipped", cause)
        state.pass_cursor += length
        state.adaptive_skip = next_skip_width(
            width, skip_start=policy.skip_start, skip_max=policy.skip_max,
            skip_factor=policy.skip_factor, grow=grow,
        )
        event("skip", f"Skip {length} bytes at offset {offset}; next width {state.adaptive_skip}")
        return True

    def apply_survey_skip() -> bool:
        current = state.range_at(state.pass_cursor)
        if current is None or current.status != "unprocessed":
            return False
        length = min(policy.survey_stride, current.end - state.pass_cursor)
        if length <= 0:
            return False
        offset = state.pass_cursor
        state.replace_range(offset, length, "skipped", "survey")
        state.pass_cursor += length
        event("skip", f"Survey skip {length} bytes at offset {offset}")
        return True

    def note_fast_read(offset: int) -> None:
        nonlocal fast_streak
        if state.adaptive_skip:
            fast_streak += 1
            if fast_streak >= policy.skip_reset_after:
                event("normal", f"Normal read speed rediscovered at offset {offset}")
                state.adaptive_skip = 0
                fast_streak = 0
            else:
                apply_adaptive_skip("probe", grow=False)
            return
        fast_streak = 0
        apply_survey_skip()

    def note_slow_or_error() -> None:
        nonlocal fast_streak
        fast_streak = 0
        state.adaptive_skip = state.adaptive_skip or policy.skip_start

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
            length = min(policy.block, item.end - offset)
            outcome = read_once(offset, length, status="reading")
            slow = outcome.succeeded and outcome.elapsed >= policy.slow_threshold
            if outcome.data is not None:
                write_recovered(offset, outcome.data, slow=slow)
                state.pass_cursor += length
                if slow:
                    event("slow", f"Slow read at offset {offset}: {outcome.elapsed:.2f}s")
                    note_slow_or_error()
                    apply_adaptive_skip("slow")
                    maybe_checkpoint(force=True)
                    report("skip")
                    continue
                note_fast_read(offset)
            else:
                zero_unreadable(offset, length)
                state.pass_cursor += length
                event("error", f"Block read failure at offset {offset}: {outcome.failure}")
                note_slow_or_error()
                apply_adaptive_skip("read_error")
                maybe_checkpoint(force=True)
                report("skip")
                continue
            maybe_checkpoint()
            report("normal")
        state.current_pass, state.pass_cursor, state.adaptive_skip = 2, 0, 0
        checkpoint()
        event("normal", "Pass 1 complete; switching to Pass 2")
        report("normal", force=True)

    def run_pass2() -> None:
        """Fill survey holes independently; one failure never poisons the next."""

        state.pass_cursor, state.adaptive_skip = 0, 0
        event("normal", "Pass 2: filling likely-good survey gaps")
        report("normal", force=True)
        holes = tuple(item for item in state.ranges_for("skipped") if item.cause == "survey")
        for hole in holes:
            cursor = hole.offset
            while cursor < hole.end:
                length = min(policy.block, hole.end - cursor)
                outcome = read_once(cursor, length, status="reading")
                slow = outcome.succeeded and outcome.elapsed >= policy.slow_threshold
                if outcome.data is not None:
                    write_recovered(cursor, outcome.data, slow=slow)
                    cursor += length
                    state.pass_cursor = cursor
                    if slow:
                        event("slow", f"Slow survey-hole read at offset {cursor - length}: {outcome.elapsed:.2f}s")
                        if cursor < hole.end:
                            state.replace_range(cursor, hole.end - cursor, "skipped", "slow")
                        maybe_checkpoint(force=True)
                        report("skip")
                        break
                else:
                    zero_unreadable(cursor, length)
                    cursor += length
                    state.pass_cursor = cursor
                    event("error", f"Survey-hole read failure at offset {cursor - length}: {outcome.failure}")
                    if cursor < hole.end:
                        state.replace_range(cursor, hole.end - cursor, "skipped", "read_error")
                    maybe_checkpoint(force=True)
                    report("skip")
                    break
                maybe_checkpoint()
                report("normal")
            state.pass_cursor = hole.end
        state.pass_cursor, state.adaptive_skip = 0, 0
        if state.easy_skipped_bytes > 0:
            checkpoint()
            event("normal", "Survey gaps remain; not entering Pass 3")
            report("normal", force=True)
            return
        state.current_pass = 3
        checkpoint()
        event("normal", "Pass 2 complete; slow/error ranges remain for Pass 3")
        report("normal", force=True)

    def run_pass3() -> None:
        """Coarsely retry each hard range.  Fine localization is Pass 4 only."""

        if state.easy_skipped_bytes > 0:
            state.current_pass = 2
            event("normal", "Survey gaps remain; returning to Pass 2")
            return
        state.pass_cursor, state.adaptive_skip = 0, 0
        event("normal", "Pass 3: coarsely retrying slow/error ranges")
        report("normal", force=True)
        targets = tuple(
            item for item in state.ranges_for("skipped", "unreadable")
            if item.status == "unreadable" or item.cause != "survey"
        )
        for target in targets:
            cursor = target.offset
            while cursor < target.end:
                length = min(policy.block, target.end - cursor)
                outcome = read_once(cursor, length, status="reading")
                slow = outcome.succeeded and outcome.elapsed >= policy.slow_threshold
                if outcome.data is not None:
                    write_recovered(cursor, outcome.data, slow=slow)
                    cursor += length
                    state.pass_cursor = cursor
                    if slow:
                        event("slow", f"Slow coarse retry at offset {cursor - length}: {outcome.elapsed:.2f}s")
                        if target.status == "skipped" and cursor < target.end:
                            state.replace_range(cursor, target.end - cursor, "skipped", "slow")
                        maybe_checkpoint(force=True)
                        report("skip")
                        break
                else:
                    zero_unreadable(cursor, length)
                    cursor += length
                    state.pass_cursor = cursor
                    event("error", f"Coarse retry failure at offset {cursor - length}: {outcome.failure}")
                    if target.status == "skipped" and cursor < target.end:
                        state.replace_range(cursor, target.end - cursor, "skipped", "read_error")
                    maybe_checkpoint(force=True)
                    report("skip")
                    break
                maybe_checkpoint()
                report("normal")
            state.pass_cursor = target.end
        state.current_pass, state.pass_cursor, state.adaptive_skip = 4, 0, 0
        checkpoint()
        event("normal", "Pass 3 complete; deep recovery remains optional")
        report("normal", force=True)

    def recover_sectors(offset: int, length: int) -> None:
        cursor = offset
        end = offset + length
        while cursor < end:
            size = min(policy.sector, end - cursor)
            outcome = read_once(cursor, size, status="reading")
            if outcome.data is None:
                zero_unreadable(cursor, size)
            else:
                write_recovered(cursor, outcome.data, slow=outcome.elapsed >= policy.slow_threshold)
            cursor += size

    def recover_fallback_piece(offset: int, length: int) -> None:
        outcome = read_once(offset, length, status="reading")
        if outcome.data is not None:
            write_recovered(offset, outcome.data, slow=outcome.elapsed >= policy.slow_threshold)
        elif length <= policy.sector:
            zero_unreadable(offset, length)
        else:
            recover_sectors(offset, length)

    def recover_deep_block(offset: int, length: int) -> bool:
        outcome = read_once(offset, length, status="reading")
        if outcome.data is not None:
            write_recovered(offset, outcome.data, slow=outcome.elapsed >= policy.slow_threshold)
            return False
        event("error", f"Deep block read failure at offset {offset}; localizing")
        if length <= policy.sector:
            zero_unreadable(offset, length)
        elif length <= policy.fallback:
            recover_sectors(offset, length)
        else:
            cursor = offset
            while cursor < offset + length:
                size = min(policy.fallback, offset + length - cursor)
                recover_fallback_piece(cursor, size)
                cursor += size
        return True

    def run_pass4() -> None:
        report("normal", force=True)
        while state.pass_cursor < source_size:
            item = state.range_at(state.pass_cursor)
            if item is None:
                break
            if item.status not in ("skipped", "unreadable"):
                state.pass_cursor = item.end
                continue
            offset = state.pass_cursor
            length = min(policy.block, item.end - offset)
            localized = recover_deep_block(offset, length)
            state.pass_cursor += length
            maybe_checkpoint(force=localized)
            report("error" if localized else "normal")
        state.current_pass, state.pass_cursor, state.adaptive_skip = 5, source_size, 0
        checkpoint()
        event("complete", "Pass 4 complete")
        report("complete", force=True)

    try:
        if not had_policy:
            event("normal", f"Adopted recovery policy {policy.profile} v{policy.version}")
        reader = factory(source_path)
        report("starting", force=True)
        if execution_limit >= 2 and state.current_pass > 2 and state.easy_skipped_bytes > 0:
            state.current_pass = 2
        while state.current_pass <= execution_limit:
            # Older/interrupted maps may claim Pass 3 while blue survey gaps
            # still exist.  Fill those before any slow/error retry.
            if execution_limit >= 2 and state.current_pass > 2 and state.easy_skipped_bytes > 0:
                state.current_pass = 2
            if state.current_pass == 1:
                run_pass1()
            elif state.current_pass == 2:
                run_pass2()
                if state.easy_skipped_bytes > 0:
                    break
            elif state.current_pass == 3:
                run_pass3()
            elif state.current_pass == 4:
                run_pass4()
            else:
                break
        checkpoint()
        report("complete" if state.current_pass >= 5 else "normal", force=True)
        return RescueResult(source_path, destination_path, resolved_map_path, source_size, state.current_pass, tuple(state.ranges))
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
