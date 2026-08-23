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


def test_policy_override_and_pass_names_are_explicit() -> None:
    assert DEFAULT_POLICY.with_overrides(skip_factor=4).skip_factor == 4
    assert PASS_NAME_TO_NUMBER == {"survey": 1, "fill": 2, "retry": 3, "deep": 4}
    assert PASS_NUMBER_TO_NAME == {1: "survey", 2: "fill", 3: "retry", 4: "deep"}


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
