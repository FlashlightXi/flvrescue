"""Versioned recovery policies used by the rescue engine.

A policy is persisted with a rescue map so that a resumed run does not silently
change its read strategy when the program defaults change.  Pass selection is
deliberately not part of a policy: it controls how far one invocation proceeds,
not how a pass behaves.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from typing import Any, Final


POLICY_VERSION: Final = 1
PROFILE_COVERAGE: Final = "coverage"

PASS_NAME_TO_NUMBER: Final[dict[str, int]] = {
    "survey": 1,
    "fill": 2,
    "retry": 3,
    "deep": 4,
}
PASS_NUMBER_TO_NAME: Final[dict[int, str]] = {
    number: name for name, number in PASS_NAME_TO_NUMBER.items()
}


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"policy {name} must be a positive integer")
    return value


def _positive_number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"policy {name} must be a positive number")
    return float(value)


@dataclass(frozen=True)
class RecoveryPolicy:
    """The versioned, durable strategy for the four recovery passes."""

    version: int = POLICY_VERSION
    profile: str = PROFILE_COVERAGE
    block: int = 8 * 1024 * 1024
    fallback: int = 64 * 1024
    sector: int = 4 * 1024
    checkpoint: int = 64 * 1024 * 1024
    slow_threshold: float = 2.0
    skip_start: int = 128 * 1024 * 1024
    skip_max: int = 1024 * 1024 * 1024
    skip_factor: int = 2
    skip_reset_after: int = 8
    survey_stride: int = 128 * 1024 * 1024

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version != POLICY_VERSION:
            raise ValueError(
                f"unsupported policy version {self.version!r}; expected {POLICY_VERSION}"
            )
        if not isinstance(self.profile, str) or self.profile != PROFILE_COVERAGE:
            raise ValueError(f"unsupported recovery policy profile {self.profile!r}")
        for name in (
            "block",
            "fallback",
            "sector",
            "checkpoint",
            "skip_start",
            "skip_max",
            "skip_factor",
            "skip_reset_after",
        ):
            _positive_int(getattr(self, name), name)
        if (
            isinstance(self.survey_stride, bool)
            or not isinstance(self.survey_stride, int)
            or self.survey_stride < 0
        ):
            raise ValueError("policy survey_stride must be a non-negative integer")
        _positive_number(self.slow_threshold, "slow_threshold")
        if self.block < self.fallback or self.fallback < self.sector:
            raise ValueError("policy requires block >= fallback >= sector")
        if self.skip_start > self.skip_max:
            raise ValueError("policy skip_start must not exceed skip_max")

    @property
    def block_size(self) -> int:
        """Compatibility spelling for callers using the rescue keyword."""

        return self.block

    @property
    def fallback_size(self) -> int:
        """Compatibility spelling for callers using the rescue keyword."""

        return self.fallback

    @property
    def sector_size(self) -> int:
        """Compatibility spelling for callers using the rescue keyword."""

        return self.sector

    @property
    def checkpoint_interval(self) -> int:
        """Compatibility spelling for callers using the rescue keyword."""

        return self.checkpoint

    def to_dict(self) -> dict[str, object]:
        """Return the complete, versioned representation saved in a map."""

        return {
            "version": self.version,
            "profile": self.profile,
            "block": self.block,
            "fallback": self.fallback,
            "sector": self.sector,
            "checkpoint": self.checkpoint,
            "slow_threshold": self.slow_threshold,
            "skip_start": self.skip_start,
            "skip_max": self.skip_max,
            "skip_factor": self.skip_factor,
            "skip_reset_after": self.skip_reset_after,
            "survey_stride": self.survey_stride,
        }

    @classmethod
    def from_dict(cls, value: object) -> "RecoveryPolicy":
        """Validate and load a complete persisted policy.

        A partial policy is rejected rather than completed from contemporary
        defaults.  That prevents a future release from silently changing the
        behaviour of an existing rescue map.
        """

        if not isinstance(value, dict):
            raise ValueError("policy must be an object")
        expected = {
            "version",
            "profile",
            "block",
            "fallback",
            "sector",
            "checkpoint",
            "slow_threshold",
            "skip_start",
            "skip_max",
            "skip_factor",
            "skip_reset_after",
            "survey_stride",
        }
        missing = expected.difference(value)
        if missing:
            raise ValueError(f"policy is missing fields: {', '.join(sorted(missing))}")
        unknown = set(value).difference(expected)
        if unknown:
            raise ValueError(f"policy has unknown fields: {', '.join(sorted(unknown))}")
        return cls(**{name: value[name] for name in expected})  # type: ignore[arg-type]

    def with_overrides(self, **overrides: object) -> "RecoveryPolicy":
        """Return a validated copy with explicitly supplied policy fields."""

        allowed = set(self.to_dict()) - {"version"}
        unknown = set(overrides).difference(allowed)
        if unknown:
            raise ValueError(f"unknown policy overrides: {', '.join(sorted(unknown))}")
        return replace(self, **overrides)  # type: ignore[arg-type]


DEFAULT_POLICY: Final = RecoveryPolicy()
