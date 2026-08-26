"""Durable range-based resume-map handling for flvrescue."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from typing import Any, Iterable, Literal

from .policy import RecoveryPolicy


MAP_VERSION = 3
RangeStatus = Literal["recovered", "skipped", "unreadable", "unprocessed"]
RangeDifficulty = Literal["fast", "slow", "hard", "failure"]
RANGE_STATUSES = frozenset({"recovered", "skipped", "unreadable", "unprocessed"})
RANGE_DIFFICULTIES = frozenset({"fast", "slow", "hard", "failure"})


class MapValidationError(ValueError):
    """A map file is malformed or does not describe safe resume state."""


@dataclass(frozen=True, order=True)
class RecoveryRange:
    offset: int
    length: int
    status: RangeStatus = "unreadable"
    cause: str | None = None
    difficulty: RangeDifficulty | None = None

    @property
    def end(self) -> int:
        return self.offset + self.length

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "offset": self.offset,
            "length": self.length,
            "status": self.status,
        }
        if self.cause is not None:
            value["cause"] = self.cause
        if self.difficulty is not None:
            value["difficulty"] = self.difficulty
        return value


# Public compatibility alias for callers that imported BadRange in v1.
BadRange = RecoveryRange
ManualClassification = Literal["slow", "hard", "defer", "clear"]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_nonnegative_int(value: object, name: str) -> int:
    if not _is_int(value) or value < 0:
        raise MapValidationError(f"{name} must be a non-negative integer")
    return value


def merge_ranges(ranges: Iterable[RecoveryRange]) -> list[RecoveryRange]:
    """Merge adjacent ranges only when status and cause are identical."""

    ordered = sorted(ranges, key=lambda item: item.offset)
    merged: list[RecoveryRange] = []
    for current in ordered:
        if current.length <= 0:
            raise MapValidationError("range length must be greater than zero")
        if current.offset < 0:
            raise MapValidationError("range offset must be non-negative")
        if current.status not in RANGE_STATUSES:
            raise MapValidationError(f"unsupported range status {current.status!r}")
        if current.cause is not None and not isinstance(current.cause, str):
            raise MapValidationError("range cause must be a string or null")
        if (
            current.difficulty is not None
            and current.difficulty not in RANGE_DIFFICULTIES
        ):
            raise MapValidationError("range difficulty is invalid")
        if merged and current.offset < merged[-1].end:
            raise MapValidationError("recovery ranges overlap")
        if (
            merged
            and current.offset == merged[-1].end
            and current.status == merged[-1].status
            and current.cause == merged[-1].cause
            and current.difficulty == merged[-1].difficulty
        ):
            previous = merged[-1]
            merged[-1] = RecoveryRange(
                previous.offset,
                previous.length + current.length,
                previous.status,
                previous.cause,
                previous.difficulty,
            )
        else:
            merged.append(current)
    return merged


@dataclass
class RescueMap:
    source_path: str
    source_size: int
    destination_path: str
    ranges: list[RecoveryRange] = field(default_factory=list)
    current_pass: int = 1
    pass_cursor: int = 0
    adaptive_skip: int = 0
    policy: RecoveryPolicy | None = None
    version: int = MAP_VERSION

    def __post_init__(self) -> None:
        if not self.ranges and self.source_size > 0:
            self.ranges = [RecoveryRange(0, self.source_size, "unprocessed")]

    def validate(self) -> None:
        if self.version != MAP_VERSION:
            raise MapValidationError(
                f"unsupported map version {self.version!r}; expected {MAP_VERSION}"
            )
        if not isinstance(self.source_path, str) or not self.source_path:
            raise MapValidationError("source_path must be a non-empty string")
        if not isinstance(self.destination_path, str) or not self.destination_path:
            raise MapValidationError("destination_path must be a non-empty string")
        _require_nonnegative_int(self.source_size, "source_size")
        if not _is_int(self.current_pass) or not 1 <= self.current_pass <= 6:
            raise MapValidationError("current_pass must be between 1 and 6")
        _require_nonnegative_int(self.pass_cursor, "pass_cursor")
        _require_nonnegative_int(self.adaptive_skip, "adaptive_skip")
        if self.policy is not None and not isinstance(self.policy, RecoveryPolicy):
            raise MapValidationError("policy must be a RecoveryPolicy or null")
        if self.pass_cursor > self.source_size:
            raise MapValidationError("pass_cursor exceeds source_size")

        self.ranges = merge_ranges(self.ranges)
        if self.current_pass == 6:
            if self.pass_cursor != self.source_size:
                raise MapValidationError(
                    "completed map pass_cursor must equal source_size"
                )
            if any(
                item.status not in ("recovered", "unreadable")
                for item in self.ranges
            ):
                raise MapValidationError(
                    "completed map cannot contain skipped or unprocessed ranges"
                )
        if self.source_size == 0:
            if self.ranges:
                raise MapValidationError("an empty source cannot have recovery ranges")
            return
        if not self.ranges or self.ranges[0].offset != 0:
            raise MapValidationError("recovery ranges must start at offset zero")
        previous_end = 0
        for item in self.ranges:
            if item.offset != previous_end:
                raise MapValidationError("recovery ranges must cover the source without gaps")
            previous_end = item.end
        if previous_end != self.source_size:
            raise MapValidationError("recovery ranges must cover the complete source")

    def replace_range(
        self,
        offset: int,
        length: int,
        status: RangeStatus,
        cause: str | None = None,
        difficulty: RangeDifficulty | None = None,
    ) -> None:
        """Replace a covered interval, splitting existing ranges as needed."""

        if not _is_int(offset) or not _is_int(length):
            raise TypeError("range offset and length must be integers")
        if offset < 0 or length <= 0 or offset + length > self.source_size:
            raise ValueError("replacement range is outside the source")
        if status not in RANGE_STATUSES:
            raise ValueError(f"unsupported range status {status!r}")
        if difficulty is not None and difficulty not in RANGE_DIFFICULTIES:
            raise ValueError(f"unsupported range difficulty {difficulty!r}")
        end = offset + length
        replacement: list[RecoveryRange] = []
        inserted = False
        for item in self.ranges:
            if item.end <= offset or item.offset >= end:
                replacement.append(item)
                continue
            if item.offset < offset:
                replacement.append(
                    RecoveryRange(
                        item.offset,
                        offset - item.offset,
                        item.status,
                        item.cause,
                        item.difficulty,
                    )
                )
            if not inserted:
                replacement.append(
                    RecoveryRange(offset, length, status, cause, difficulty)
                )
                inserted = True
            if item.end > end:
                replacement.append(
                    RecoveryRange(
                        end, item.end - end, item.status, item.cause, item.difficulty
                    )
                )
        if not inserted:
            raise ValueError("replacement range is not covered by the map")
        self.ranges = merge_ranges(replacement)

    def range_at(self, offset: int) -> RecoveryRange | None:
        if offset < 0 or offset >= self.source_size:
            return None
        for item in self.ranges:
            if item.offset <= offset < item.end:
                return item
        raise RuntimeError("validated map contains an uncovered offset")

    def ranges_for(self, *statuses: RangeStatus) -> tuple[RecoveryRange, ...]:
        selected = frozenset(statuses)
        return tuple(item for item in self.ranges if item.status in selected)

    def bytes_for(self, *statuses: RangeStatus) -> int:
        selected = frozenset(statuses)
        return sum(item.length for item in self.ranges if item.status in selected)

    def skipped_bytes_for(self, *causes: str) -> int:
        wanted = frozenset(causes)
        return sum(
            item.length
            for item in self.ranges
            if item.status == "skipped" and (item.cause or "slow") in wanted
        )

    @property
    def recovered_bytes(self) -> int:
        return self.bytes_for("recovered")

    @property
    def skipped_bytes(self) -> int:
        return self.bytes_for("skipped")

    @property
    def easy_skipped_bytes(self) -> int:
        return sum(
            item.length
            for item in self.ranges
            if item.status == "skipped"
            and item.cause in ("survey", "probe")
            and item.difficulty in (None, "fast")
        )

    @property
    def slow_skipped_bytes(self) -> int:
        return sum(
            item.length
            for item in self.ranges
            if item.status == "skipped"
            and (
                item.difficulty == "slow"
                or (item.difficulty is None and item.cause == "slow")
            )
        )

    @property
    def hard_skipped_bytes(self) -> int:
        return sum(
            item.length
            for item in self.ranges
            if item.status == "skipped"
            and (
                item.difficulty == "hard"
                or (item.difficulty is None and item.cause == "hard")
            )
        )

    @property
    def failure_skipped_bytes(self) -> int:
        return sum(
            item.length
            for item in self.ranges
            if item.status == "skipped"
            and (
                item.difficulty == "failure"
                or (item.difficulty is None and item.cause == "read_error")
            )
        )

    @property
    def unreadable_bytes(self) -> int:
        return self.bytes_for("unreadable")

    @property
    def unprocessed_bytes(self) -> int:
        return self.bytes_for("unprocessed")

    @property
    def slow_bytes(self) -> int:
        return sum(
            item.length
            for item in self.ranges
            if item.status == "recovered" and item.difficulty == "slow"
        )

    @property
    def hard_bytes(self) -> int:
        return sum(
            item.length
            for item in self.ranges
            if item.status == "recovered" and item.difficulty == "hard"
        )

    @property
    def bad_ranges(self) -> list[RecoveryRange]:
        return list(self.ranges_for("unreadable"))

    @property
    def completed_until(self) -> int:
        """Compatibility view of the classified contiguous prefix."""

        frontier = 0
        for item in self.ranges:
            if item.status == "unprocessed":
                break
            frontier = item.end
        return frontier

    def to_dict(self) -> dict[str, object]:
        self.validate()
        value: dict[str, object] = {
            "version": self.version,
            "source_path": self.source_path,
            "source_size": self.source_size,
            "destination_path": self.destination_path,
            "current_pass": self.current_pass,
            "pass_cursor": self.pass_cursor,
            "adaptive_skip": self.adaptive_skip,
            "ranges": [item.to_dict() for item in self.ranges],
        }
        if self.policy is not None:
            value["policy"] = self.policy.to_dict()
        return value


def mark_map_range(
    state: RescueMap,
    offset: int,
    length: int,
    classification: ManualClassification,
) -> int:
    """Change only unresolved map ranges, never already recovered bytes."""

    if classification not in ("slow", "hard", "defer", "clear"):
        raise ValueError("classification must be slow, hard, defer, or clear")
    if not _is_int(offset) or not _is_int(length):
        raise TypeError("range offset and length must be integers")
    if offset < 0 or length <= 0 or offset + length > state.source_size:
        raise ValueError("marked range is outside the source")

    end = offset + length
    changed = 0
    for item in tuple(state.ranges):
        overlap_start = max(offset, item.offset)
        overlap_end = min(end, item.end)
        if overlap_end <= overlap_start or item.status == "recovered":
            continue
        overlap_length = overlap_end - overlap_start
        if classification == "clear":
            if not (item.cause or "").startswith("manual_"):
                continue
            state.replace_range(overlap_start, overlap_length, "unprocessed")
        else:
            difficulty: RangeDifficulty = (
                "slow" if classification == "slow" else "hard"
            )
            state.replace_range(
                overlap_start,
                overlap_length,
                "skipped",
                f"manual_{classification}",
                difficulty,
            )
        changed += overlap_length

    if changed:
        target_pass = {"clear": 1, "slow": 3, "hard": 4, "defer": 5}[
            classification
        ]
        state.current_pass = min(state.current_pass, target_pass)
        state.pass_cursor = 0
        state.adaptive_skip = 0
        state.validate()
    return changed


def _range_from_json(value: object, index: int) -> RecoveryRange:
    if not isinstance(value, dict):
        raise MapValidationError(f"ranges[{index}] must be an object")
    offset = _require_nonnegative_int(value.get("offset"), f"ranges[{index}].offset")
    length = value.get("length")
    if not _is_int(length) or length <= 0:
        raise MapValidationError(f"ranges[{index}].length must be a positive integer")
    status = value.get("status")
    if not isinstance(status, str) or status not in RANGE_STATUSES:
        raise MapValidationError(f"ranges[{index}].status is invalid")
    cause = value.get("cause")
    if cause is not None and not isinstance(cause, str):
        raise MapValidationError(f"ranges[{index}].cause must be a string or null")
    difficulty = value.get("difficulty")
    if difficulty is not None and (
        not isinstance(difficulty, str) or difficulty not in RANGE_DIFFICULTIES
    ):
        raise MapValidationError(f"ranges[{index}].difficulty is invalid")
    return RecoveryRange(offset, length, status, cause, difficulty)  # type: ignore[arg-type]


def _legacy_difficulty(item: RecoveryRange) -> RangeDifficulty | None:
    if item.status == "unreadable" or item.cause == "read_error":
        return "failure"
    if item.cause == "slow":
        return "slow"
    if item.cause == "hard":
        return "hard"
    return None


def _v1_bad_range(value: object, index: int) -> tuple[int, int]:
    if not isinstance(value, dict):
        raise MapValidationError(f"bad_ranges[{index}] must be an object")
    offset = _require_nonnegative_int(value.get("offset"), f"bad_ranges[{index}].offset")
    length = value.get("length")
    if not _is_int(length) or length <= 0:
        raise MapValidationError(f"bad_ranges[{index}].length must be a positive integer")
    return offset, length


def _migrate_v1(value: dict[object, object]) -> RescueMap:
    source_path = value.get("source_path")
    destination_path = value.get("destination_path")
    source_size = _require_nonnegative_int(value.get("source_size"), "source_size")
    completed_until = _require_nonnegative_int(value.get("completed_until"), "completed_until")
    if completed_until > source_size:
        raise MapValidationError("completed_until exceeds source_size")
    raw_bad = value.get("bad_ranges")
    if not isinstance(raw_bad, list):
        raise MapValidationError("bad_ranges must be an array")
    bad = sorted(
        (_v1_bad_range(item, index) for index, item in enumerate(raw_bad)),
        key=lambda item: item[0],
    )
    ranges: list[RecoveryRange] = []
    cursor = 0
    for offset, length in bad:
        end = offset + length
        if offset < cursor or end > completed_until:
            raise MapValidationError("v1 bad ranges overlap or exceed completed_until")
        if offset > cursor:
            ranges.append(RecoveryRange(cursor, offset - cursor, "recovered"))
        ranges.append(
            RecoveryRange(offset, length, "unreadable", "read_error", "failure")
        )
        cursor = end
    if cursor < completed_until:
        ranges.append(RecoveryRange(cursor, completed_until - cursor, "recovered"))
    if completed_until < source_size:
        ranges.append(
            RecoveryRange(completed_until, source_size - completed_until, "unprocessed")
        )
    state = RescueMap(
        source_path=source_path,  # type: ignore[arg-type]
        source_size=source_size,
        destination_path=destination_path,  # type: ignore[arg-type]
        ranges=ranges,
        current_pass=1,
        pass_cursor=completed_until,
    )
    state.validate()
    return state


def _policy_from_json(value: object) -> RecoveryPolicy | None:
    if value is None:
        return None
    try:
        return RecoveryPolicy.from_dict(value)
    except ValueError as exc:
        raise MapValidationError(f"invalid recovery policy: {exc}") from exc


def _migrate_v2(value: dict[object, object]) -> RescueMap:
    """Move the old four-pass cursor into the five-pass state machine.

    Old Pass 4 was Deep.  A zero cursor means it had not started, so the new
    Hard pass can run next; a non-zero cursor means Deep was interrupted and is
    resumed as new Pass 5.  Old terminal state 5 becomes terminal state 6.
    Range classifications and recovered bytes are preserved verbatim; a v1
    policy is upgraded by ``RecoveryPolicy.from_dict``.
    """

    raw_ranges = value.get("ranges")
    if not isinstance(raw_ranges, list):
        raise MapValidationError("ranges must be an array")
    old_pass = value.get("current_pass")
    if not _is_int(old_pass) or not 1 <= old_pass <= 5:
        raise MapValidationError("current_pass must be between 1 and 5")
    pass_cursor = _require_nonnegative_int(value.get("pass_cursor"), "pass_cursor")
    if old_pass <= 3:
        current_pass = old_pass
    elif old_pass == 4:
        current_pass = 4 if pass_cursor == 0 else 5
    else:
        current_pass = 6
    state = RescueMap(
        source_path=value.get("source_path"),  # type: ignore[arg-type]
        source_size=_require_nonnegative_int(value.get("source_size"), "source_size"),
        destination_path=value.get("destination_path"),  # type: ignore[arg-type]
        ranges=[
            RecoveryRange(
                parsed.offset,
                parsed.length,
                parsed.status,
                parsed.cause,
                _legacy_difficulty(parsed),
            )
            for index, item in enumerate(raw_ranges)
            for parsed in (_range_from_json(item, index),)
        ],
        current_pass=current_pass,
        pass_cursor=pass_cursor,
        adaptive_skip=_require_nonnegative_int(
            value.get("adaptive_skip", 0), "adaptive_skip"
        ),
        policy=_policy_from_json(value.get("policy")),
    )
    state.validate()
    return state


def map_from_dict(value: object) -> RescueMap:
    if not isinstance(value, dict):
        raise MapValidationError("map root must be an object")
    version = value.get("version")
    if not _is_int(version):
        raise MapValidationError("version must be an integer")
    if version == 1:
        return _migrate_v1(value)
    if version == 2:
        return _migrate_v2(value)
    if version != MAP_VERSION:
        raise MapValidationError(
            f"unsupported map version {version!r}; expected 1, 2, or {MAP_VERSION}"
        )

    raw_ranges = value.get("ranges")
    if not isinstance(raw_ranges, list):
        raise MapValidationError("ranges must be an array")
    state = RescueMap(
        source_path=value.get("source_path"),  # type: ignore[arg-type]
        source_size=_require_nonnegative_int(value.get("source_size"), "source_size"),
        destination_path=value.get("destination_path"),  # type: ignore[arg-type]
        ranges=[_range_from_json(item, index) for index, item in enumerate(raw_ranges)],
        current_pass=value.get("current_pass"),  # type: ignore[arg-type]
        pass_cursor=_require_nonnegative_int(value.get("pass_cursor"), "pass_cursor"),
        adaptive_skip=_require_nonnegative_int(
            value.get("adaptive_skip", 0), "adaptive_skip"
        ),
        policy=_policy_from_json(value.get("policy")),
        version=version,
    )
    state.validate()
    return state


def load_map(path: str | PathLike[str]) -> RescueMap:
    map_path = Path(path)
    try:
        with map_path.open("r", encoding="utf-8") as handle:
            raw: Any = json.load(handle)
    except json.JSONDecodeError as exc:
        raise MapValidationError(f"invalid JSON in map file {map_path}: {exc.msg}") from exc
    return map_from_dict(raw)


def save_map_atomic(path: str | PathLike[str], state: RescueMap) -> None:
    """Write a map using a same-directory temporary file and ``os.replace``."""

    state.validate()
    map_path = Path(path)
    temporary_path = map_path.with_name(f"{map_path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(state.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, map_path)
    except BaseException:
        raise
