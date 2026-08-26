from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeClock, FaultInjectingReader, LatencyInjectingReader
from flvrescue.mapfile import (
    MapValidationError,
    RecoveryRange,
    RescueMap,
    map_from_dict,
    save_map_atomic,
)
from flvrescue.policy import RecoveryPolicy
from flvrescue.rescue import rescue


BLOCK = 16
FALLBACK = 4
SECTOR = 2


def _payload(size: int) -> bytes:
    return bytes((index * 19 + 7) % 256 for index in range(size))


def _policy() -> RecoveryPolicy:
    return RecoveryPolicy(
        block=BLOCK,
        fallback=FALLBACK,
        sector=SECTOR,
        checkpoint=BLOCK,
        slow_threshold=1.0,
        hard_threshold=5.0,
        skip_start=BLOCK,
        skip_max=BLOCK * 4,
        skip_reset_after=1,
        survey_stride=BLOCK,
    )


def _saved_state(
    source: Path,
    destination: Path,
    map_path: Path,
    ranges: list[RecoveryRange],
    *,
    current_pass: int,
) -> None:
    destination.write_bytes(b"\0" * source.stat().st_size)
    save_map_atomic(
        map_path,
        RescueMap(
            source_path=str(source.resolve()),
            source_size=source.stat().st_size,
            destination_path=str(destination.resolve()),
            ranges=ranges,
            current_pass=current_pass,
            policy=_policy(),
        ),
    )


@pytest.mark.parametrize(
    ("elapsed", "difficulty"),
    [(0.999, "fast"), (1.0, "slow"), (4.999, "slow"), (5.0, "hard")],
)
def test_latency_boundaries_are_persisted(
    tmp_path: Path, elapsed: float, difficulty: str
) -> None:
    source = tmp_path / f"{difficulty}.flv"
    destination = tmp_path / f"{difficulty}.rescued.flv"
    payload = _payload(BLOCK)
    source.write_bytes(payload)
    clock = FakeClock()
    reader = LatencyInjectingReader(payload, clock, [(0, BLOCK, elapsed)])

    result = rescue(
        source,
        destination,
        reader_factory=lambda _source: reader,
        progress=False,
        policy=_policy(),
        through="survey",
        clock=clock,
    )

    assert result.ranges == (
        RecoveryRange(0, BLOCK, "recovered", None, difficulty),
    )


def test_fast_slow_and_hard_passes_only_read_their_priority(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = _payload(BLOCK * 5)
    source.write_bytes(payload)
    _saved_state(
        source,
        destination,
        map_path,
        [
            RecoveryRange(0, BLOCK, "recovered", None, "fast"),
            RecoveryRange(BLOCK, BLOCK, "skipped", "survey"),
            RecoveryRange(BLOCK * 2, BLOCK, "skipped", "survey", "slow"),
            RecoveryRange(BLOCK * 3, BLOCK, "skipped", "survey", "hard"),
            RecoveryRange(BLOCK * 4, BLOCK, "unreadable", "read_error", "failure"),
        ],
        current_pass=2,
    )

    fast_reader = FaultInjectingReader(payload)
    rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: fast_reader,
        progress=False,
        through="fast",
    )
    assert fast_reader.calls == [(BLOCK, BLOCK)]

    slow_reader = FaultInjectingReader(payload)
    rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: slow_reader,
        progress=False,
        through="slow",
    )
    assert slow_reader.calls == [(BLOCK * 2, BLOCK)]

    hard_reader = FaultInjectingReader(payload)
    rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: hard_reader,
        progress=False,
        through="hard",
    )
    assert hard_reader.calls == [(BLOCK * 3, BLOCK), (BLOCK * 4, BLOCK)]
    assert all(offset != 0 for offset, _size in hard_reader.calls)


def test_deep_alone_targets_every_unrecovered_range_and_uses_fallback(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = _payload(BLOCK * 3)
    source.write_bytes(payload)
    _saved_state(
        source,
        destination,
        map_path,
        [
            RecoveryRange(0, BLOCK, "recovered", None, "fast"),
            RecoveryRange(BLOCK, BLOCK, "skipped", "slow_pass", "hard"),
            RecoveryRange(BLOCK * 2, BLOCK, "unprocessed"),
        ],
        current_pass=5,
    )
    reader = FaultInjectingReader(payload, bad_ranges=[(BLOCK + 2, 2)])

    result = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: reader,
        progress=False,
        through="deep",
    )

    assert result.current_pass == 6
    assert all(offset >= BLOCK for offset, _size in reader.calls)
    assert (BLOCK * 2, BLOCK) in reader.calls
    assert any(size < BLOCK for _offset, size in reader.calls)


def test_v2_map_and_policy_migrate_without_inventing_fast_data() -> None:
    old_policy = _policy().to_dict()
    old_policy.pop("hard_threshold")
    old_policy["version"] = 1
    state = map_from_dict(
        {
            "version": 2,
            "source_path": "source.flv",
            "source_size": 48,
            "destination_path": "rescued.flv",
            "current_pass": 4,
            "pass_cursor": 16,
            "adaptive_skip": 0,
            "policy": old_policy,
            "ranges": [
                {"offset": 0, "length": 16, "status": "recovered"},
                {
                    "offset": 16,
                    "length": 16,
                    "status": "recovered",
                    "cause": "slow",
                },
                {
                    "offset": 32,
                    "length": 16,
                    "status": "unreadable",
                    "cause": "read_error",
                },
            ],
        }
    )

    assert state.version == 3
    assert state.current_pass == 5
    assert state.policy is not None
    assert state.policy.version == 3
    assert state.policy.slow_threshold == 1.0
    assert state.policy.hard_threshold == 10.0
    assert [item.difficulty for item in state.ranges] == [None, "slow", "failure"]

    not_started = state.to_dict()
    not_started["version"] = 2
    not_started["current_pass"] = 4
    not_started["pass_cursor"] = 0
    policy = dict(not_started["policy"])  # type: ignore[arg-type]
    policy["version"] = 1
    policy.pop("hard_threshold")
    not_started["policy"] = policy
    assert map_from_dict(not_started).current_pass == 4


def test_v2_resume_never_rereads_an_existing_recovered_range(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = _payload(BLOCK * 2)
    source.write_bytes(payload)
    destination.write_bytes(payload[:BLOCK] + b"\0" * BLOCK)
    old_policy = _policy().to_dict()
    old_policy.pop("hard_threshold")
    old_policy["version"] = 1
    map_path.write_text(
        json.dumps(
            {
                "version": 2,
                "source_path": str(source.resolve()),
                "source_size": len(payload),
                "destination_path": str(destination.resolve()),
                "current_pass": 2,
                "pass_cursor": 0,
                "adaptive_skip": 0,
                "policy": old_policy,
                "ranges": [
                    {"offset": 0, "length": BLOCK, "status": "recovered"},
                    {
                        "offset": BLOCK,
                        "length": BLOCK,
                        "status": "skipped",
                        "cause": "survey",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    reader = FaultInjectingReader(payload)

    result = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: reader,
        progress=False,
        through="fast",
    )

    assert reader.calls == [(BLOCK, BLOCK)]
    assert result.current_pass == 3
    assert json.loads(map_path.read_text(encoding="utf-8"))["version"] == 3
    assert destination.read_bytes() == payload


@pytest.mark.parametrize(("version", "current_pass"), [(2, 5), (3, 6)])
def test_terminal_map_rejects_unresolved_ranges(
    version: int, current_pass: int
) -> None:
    with pytest.raises(MapValidationError, match="completed map"):
        map_from_dict(
            {
                "version": version,
                "source_path": "source.flv",
                "source_size": BLOCK,
                "destination_path": "rescued.flv",
                "current_pass": current_pass,
                "pass_cursor": BLOCK,
                "adaptive_skip": 0,
                "ranges": [
                    {
                        "offset": 0,
                        "length": BLOCK,
                        "status": "skipped",
                        "cause": "read_error",
                    }
                ],
            }
        )


def test_terminal_map_requires_cursor_at_source_end() -> None:
    with pytest.raises(MapValidationError, match="pass_cursor"):
        map_from_dict(
            {
                "version": 3,
                "source_path": "source.flv",
                "source_size": BLOCK,
                "destination_path": "rescued.flv",
                "current_pass": 6,
                "pass_cursor": 0,
                "adaptive_skip": 0,
                "ranges": [
                    {"offset": 0, "length": BLOCK, "status": "recovered"}
                ],
            }
        )
