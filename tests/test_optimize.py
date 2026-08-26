from __future__ import annotations

from flvrescue.optimize import recommend_policy


def test_balanced_medium_file_keeps_tested_default_geometry() -> None:
    recommendation = recommend_policy(76 * 1024**3, preference="balanced")

    assert recommendation.through == "fast"
    assert recommendation.policy.block == 8 * 1024**2
    assert recommendation.policy.survey_stride == 128 * 1024**2
    assert recommendation.policy.skip_start == 128 * 1024**2


def test_fast_large_file_uses_wider_coverage_and_stops_after_survey() -> None:
    recommendation = recommend_policy(256 * 1024**3, preference="fast")

    assert recommendation.through == "survey"
    assert recommendation.runnable
    assert recommendation.policy.survey_stride == 512 * 1024**2
    assert recommendation.policy.skip_start == 512 * 1024**2


def test_low_free_space_caps_thorough_recommendation_at_survey() -> None:
    recommendation = recommend_policy(
        76 * 1024**3,
        preference="thorough",
        available_space=10 * 1024**3,
    )

    assert recommendation.through == "survey"
    assert not recommendation.runnable
    assert any("rotate the batch" in warning for warning in recommendation.warnings)


def test_survey_estimate_never_exceeds_source_size() -> None:
    recommendation = recommend_policy(1024, preference="balanced")

    assert recommendation.estimated_survey_bytes == 1024
