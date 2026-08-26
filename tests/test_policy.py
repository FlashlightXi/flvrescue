from __future__ import annotations

import pytest

from flvrescue.policy import (
    DEFAULT_POLICY,
    PASS_NAME_TO_NUMBER,
    PASS_NUMBER_TO_NAME,
    RecoveryPolicy,
)
from flvrescue.mapfile import MapValidationError, map_from_dict


def test_default_policy_round_trips_as_a_complete_versioned_contract() -> None:
    assert DEFAULT_POLICY.profile == "coverage"
    assert DEFAULT_POLICY.block == 8 * 1024 * 1024
    assert DEFAULT_POLICY.skip_start == 128 * 1024 * 1024
    assert DEFAULT_POLICY.skip_reset_after == 8
    assert DEFAULT_POLICY.survey_stride == 128 * 1024 * 1024
    assert DEFAULT_POLICY.slow_threshold == 2.0
    assert DEFAULT_POLICY.hard_threshold == 10.0
    assert RecoveryPolicy.from_dict(DEFAULT_POLICY.to_dict()) == DEFAULT_POLICY


def test_policy_rejects_partial_unknown_and_invalid_values() -> None:
    with pytest.raises(ValueError, match="missing fields"):
        RecoveryPolicy.from_dict({"version": 1})
    invalid = DEFAULT_POLICY.to_dict()
    invalid["unexpected"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        RecoveryPolicy.from_dict(invalid)
    with pytest.raises(ValueError, match="block >= fallback"):
        DEFAULT_POLICY.with_overrides(block=1024)
    with pytest.raises(ValueError, match="hard_threshold"):
        DEFAULT_POLICY.with_overrides(hard_threshold=1.0)


def test_policy_override_and_pass_names_are_explicit() -> None:
    assert DEFAULT_POLICY.with_overrides(skip_factor=4).skip_factor == 4
    assert PASS_NAME_TO_NUMBER == {
        "survey": 1,
        "fast": 2,
        "slow": 3,
        "hard": 4,
        "deep": 5,
    }
    assert PASS_NUMBER_TO_NAME == {
        1: "survey",
        2: "fast",
        3: "slow",
        4: "hard",
        5: "deep",
    }


def test_legacy_policy_with_a_high_slow_threshold_migrates_safely() -> None:
    legacy = DEFAULT_POLICY.to_dict()
    legacy["version"] = 1
    legacy["slow_threshold"] = 30.0
    legacy.pop("hard_threshold")

    migrated = RecoveryPolicy.from_dict(legacy)

    assert migrated.slow_threshold == 30.0
    assert migrated.hard_threshold == 150.0


def test_map_rejects_an_unknown_persisted_policy_field() -> None:
    with pytest.raises(MapValidationError, match="invalid recovery policy"):
        map_from_dict(
            {
                "version": 2,
                "source_path": "source.flv",
                "source_size": 1,
                "destination_path": "rescued.flv",
                "current_pass": 1,
                "pass_cursor": 0,
                "adaptive_skip": 0,
                "ranges": [{"offset": 0, "length": 1, "status": "unprocessed"}],
                "policy": {**DEFAULT_POLICY.to_dict(), "future_field": True},
            }
        )


def test_map_v3_with_policy_v2_adopts_read_budgets() -> None:
    policy = DEFAULT_POLICY.to_dict()
    policy["version"] = 2
    for name in (
        "slow_block",
        "hard_block",
        "survey_budget",
        "fast_budget",
        "slow_budget",
        "hard_budget",
        "deep_budget",
    ):
        policy.pop(name)

    state = map_from_dict(
        {
            "version": 3,
            "source_path": "source.flv",
            "source_size": 1,
            "destination_path": "rescued.flv",
            "current_pass": 2,
            "pass_cursor": 0,
            "adaptive_skip": 0,
            "ranges": [{"offset": 0, "length": 1, "status": "skipped", "cause": "survey"}],
            "policy": policy,
        }
    )

    assert state.policy is not None
    assert state.policy.version == 3
    assert state.policy.fast_budget == DEFAULT_POLICY.fast_budget
    assert state.policy.slow_block == DEFAULT_POLICY.slow_block
