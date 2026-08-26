"""Heuristic policy recommendations that never read source contents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .policy import DEFAULT_POLICY, RecoveryPolicy


OptimizationPreference = Literal["fast", "balanced", "thorough"]


@dataclass(frozen=True)
class OptimizationRecommendation:
    """A transparent recommendation for a later, ordinary rescue command."""

    preference: OptimizationPreference
    source_size: int
    available_space: int | None
    policy: RecoveryPolicy
    through: str
    estimated_survey_bytes: int
    minimum_space: int
    runnable: bool
    warnings: tuple[str, ...] = ()


def _size_tier(source_size: int) -> tuple[int, int, int, int]:
    gib = 1024**3
    mib = 1024**2
    if source_size <= 16 * gib:
        return 64 * mib, 64 * mib, 512 * mib, 32 * mib
    if source_size <= 128 * gib:
        return 128 * mib, 128 * mib, 1024 * mib, 64 * mib
    return 256 * mib, 256 * mib, 2 * gib, 128 * mib


def recommend_policy(
    source_size: int,
    *,
    preference: OptimizationPreference = "balanced",
    available_space: int | None = None,
) -> OptimizationRecommendation:
    """Choose a conservative size/preference heuristic without touching data.

    This is intentionally not hardware benchmarking.  It changes only coarse
    coverage geometry; block/fallback/sector sizes retain the tested defaults.
    """

    if isinstance(source_size, bool) or not isinstance(source_size, int) or source_size < 0:
        raise ValueError("source_size must be a non-negative integer")
    if preference not in ("fast", "balanced", "thorough"):
        raise ValueError("preference must be fast, balanced, or thorough")
    if available_space is not None and (
        isinstance(available_space, bool)
        or not isinstance(available_space, int)
        or available_space < 0
    ):
        raise ValueError("available_space must be a non-negative integer or None")

    stride, skip_start, skip_max, checkpoint = _size_tier(source_size)
    through = "fast"
    if preference == "fast":
        stride *= 2
        skip_start *= 2
        skip_max = max(skip_max, skip_start * 4)
        through = "survey"
    elif preference == "thorough":
        stride = max(32 * 1024**2, stride // 2)
        skip_start = max(32 * 1024**2, skip_start // 2)
        skip_max = max(512 * 1024**2, skip_max // 2)
        through = "slow"

    policy = DEFAULT_POLICY.with_overrides(
        survey_stride=stride,
        skip_start=skip_start,
        skip_max=skip_max,
        checkpoint=checkpoint,
    )
    sample_span = policy.block + policy.survey_stride
    sample_count = 0 if source_size == 0 else (source_size + sample_span - 1) // sample_span
    estimated_survey_bytes = min(source_size, sample_count * policy.block)
    warnings: list[str] = []
    reserve = max(1024**3, source_size // 20)
    minimum_space = estimated_survey_bytes + reserve
    runnable = True
    if available_space is not None:
        if available_space < minimum_space:
            runnable = False
            warnings.append(
                "Free space is below the estimated Survey writes plus safety reserve."
            )
        if through != "survey" and available_space < source_size + reserve:
            through = "survey"
            warnings.append(
                "Free space may not hold Fast/Slow output; stop after Survey and rotate the batch."
            )

    return OptimizationRecommendation(
        preference=preference,
        source_size=source_size,
        available_space=available_space,
        policy=policy,
        through=through,
        estimated_survey_bytes=estimated_survey_bytes,
        minimum_space=minimum_space,
        runnable=runnable,
        warnings=tuple(warnings),
    )
