"""Durable resume-map handling for flvrescue."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from typing import Any, Iterable


MAP_VERSION = 1


class MapValidationError(ValueError):
    """A map file is malformed or does not describe a safe resume point."""


@dataclass(frozen=True, order=True)
class BadRange:
    offset: int
    length: int

    @property
    def end(self) -> int:
        return self.offset + self.length

    def to_dict(self) -> dict[str, int]:
        return {"offset": self.offset, "length": self.length}


def _is_int(value: object) -> bool:
    # bool is an int subclass but never a useful file offset.
    return isinstance(value, int) and not isinstance(value, bool)


def _require_nonnegative_int(value: object, name: str) -> int:
    if not _is_int(value) or value < 0:
        raise MapValidationError(f"{name} must be a non-negative integer")
    return value


def merge_bad_ranges(ranges: Iterable[BadRange]) -> list[BadRange]:
    """Sort and merge overlapping *and adjacent* unreadable regions."""

    ordered = sorted(ranges, key=lambda item: item.offset)
    merged: list[BadRange] = []
    for current in ordered:
        if current.length <= 0:
            raise MapValidationError("bad range length must be greater than zero")
        if current.offset < 0:
            raise MapValidationError("bad range offset must be non-negative")
        if not merged or current.offset > merged[-1].end:
            merged.append(current)
            continue
        previous = merged[-1]
        merged[-1] = BadRange(
            previous.offset,
            max(previous.end, current.end) - previous.offset,
        )
    return merged


@dataclass
class RescueMap:
    """The complete, intentionally small v1 sidecar format."""

    source_path: str
    source_size: int
    destination_path: str
    completed_until: int = 0
    bad_ranges: list[BadRange] = field(default_factory=list)
    version: int = MAP_VERSION

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
        _require_nonnegative_int(self.completed_until, "completed_until")
        if self.completed_until > self.source_size:
            raise MapValidationError("completed_until exceeds source_size")

        self.bad_ranges = merge_bad_ranges(self.bad_ranges)
        for bad_range in self.bad_ranges:
            if bad_range.end > self.source_size:
                raise MapValidationError("bad range exceeds source_size")
            if bad_range.end > self.completed_until:
                raise MapValidationError("bad range is beyond completed_until")

    @property
    def unreadable_bytes(self) -> int:
        return sum(item.length for item in self.bad_ranges)

    def add_bad_range(self, offset: int, length: int) -> None:
        if not _is_int(offset) or not _is_int(length):
            raise TypeError("bad range offset and length must be integers")
        if offset < 0 or length <= 0:
            raise ValueError("bad range must have a non-negative offset and positive length")
        if offset + length > self.source_size:
            raise ValueError("bad range exceeds source_size")
        self.bad_ranges = merge_bad_ranges([*self.bad_ranges, BadRange(offset, length)])

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "version": self.version,
            "source_path": self.source_path,
            "source_size": self.source_size,
            "destination_path": self.destination_path,
            "completed_until": self.completed_until,
            "bad_ranges": [item.to_dict() for item in self.bad_ranges],
        }


def _bad_range_from_json(value: object, index: int) -> BadRange:
    if not isinstance(value, dict):
        raise MapValidationError(f"bad_ranges[{index}] must be an object")
    offset = _require_nonnegative_int(value.get("offset"), f"bad_ranges[{index}].offset")
    length = value.get("length")
    if not _is_int(length) or length <= 0:
        raise MapValidationError(f"bad_ranges[{index}].length must be a positive integer")
    return BadRange(offset, length)


def map_from_dict(value: object) -> RescueMap:
    if not isinstance(value, dict):
        raise MapValidationError("map root must be an object")
    version = value.get("version")
    if not _is_int(version):
        raise MapValidationError("version must be an integer")
    source_path = value.get("source_path")
    destination_path = value.get("destination_path")
    source_size = _require_nonnegative_int(value.get("source_size"), "source_size")
    completed_until = _require_nonnegative_int(
        value.get("completed_until"), "completed_until"
    )
    raw_ranges = value.get("bad_ranges")
    if not isinstance(raw_ranges, list):
        raise MapValidationError("bad_ranges must be an array")
    state = RescueMap(
        source_path=source_path,
        source_size=source_size,
        destination_path=destination_path,
        completed_until=completed_until,
        bad_ranges=[_bad_range_from_json(item, index) for index, item in enumerate(raw_ranges)],
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
        # Leave an existing committed map untouched.  A stale .tmp is harmless
        # and can be inspected if a filesystem itself is failing.
        raise
