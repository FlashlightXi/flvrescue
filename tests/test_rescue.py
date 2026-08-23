from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from conftest import FaultInjectingReader, SlowInjectingReader
from flvrescue import DEFAULT_POLICY, RecoveryPolicy, RecoveryRange, RescueMap, rescue
from flvrescue.mapfile import save_map_atomic
from flvrescue.progress import ProgressReporter


BLOCK = 32
FALLBACK = 8
SECTOR = 2


class RecordingProgress:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []
        self.events: list[tuple[str, str]] = []
        self.closed = False

    def update(self, **values: object) -> None:
        self.updates.append(values)

    def begin_read(self, offset: int, size: int, *, status: str = "reading") -> None:
        pass

    def end_read(self, *, status: str) -> None:
        pass

    def event(self, kind: str, message: str) -> None:
        self.events.append((kind, message))

    def close(self) -> None:
        self.closed = True


def patterned_bytes(size: int) -> bytes:
    return bytes((index * 37 + 11) % 256 for index in range(size))


def read_map(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_rescue(
    source: Path,
    destination: Path,
    *,
    map_path: Path | None = None,
    reader: FaultInjectingReader | None = None,
    max_pass: int = 2,
    progress: object = False,
    **kwargs,
):
    options = {
        "block_size": BLOCK,
        "fallback_size": FALLBACK,
        "sector_size": SECTOR,
        "checkpoint_interval": BLOCK,
        "slow_threshold": 0.01,
        "skip_start": BLOCK,
        "skip_max": BLOCK * 4,
        "skip_reset_after": 1,
        "survey_stride": 0,
        "max_pass": max_pass,
    }
    options.update(kwargs)
    return rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=(lambda _source: reader) if reader is not None else None,
        progress=progress,
        **options,
    )


def test_normal_copy_preserves_bytes_and_finishes_default_two_passes(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(93)
    source.write_bytes(payload)
    reader = FaultInjectingReader(payload)

    result = run_rescue(source, destination, map_path=map_path, reader=reader)

    assert destination.read_bytes() == payload
    assert hashlib.sha256(destination.read_bytes()).digest() == hashlib.sha256(payload).digest()
    assert result.current_pass == 3
    assert result.recovered_bytes == len(payload)
    map_data = read_map(map_path)
    assert map_data["version"] == 2
    assert map_data["current_pass"] == 3
    assert map_data["ranges"] == [
        {"offset": 0, "length": len(payload), "status": "recovered"}
    ]


def test_destination_preparation_is_reported_before_source_reads(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    payload = patterned_bytes(64)
    source.write_bytes(payload)
    progress = RecordingProgress()

    run_rescue(source, destination, progress=progress, max_pass=1)

    assert progress.updates[0]["status"] == "preparing"
    assert any(
        update.get("status") == "preparing"
        and update.get("storage_mode") in {"sparse", "lazy"}
        for update in progress.updates
    )
    assert any(message.startswith("Destination storage mode: ") for _, message in progress.events)
    assert progress.closed


def test_pass1_failure_skips_without_fine_fallback(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    payload = patterned_bytes(96)
    source.write_bytes(payload)
    reader = FaultInjectingReader(payload, bad_ranges=[(10, 2)])

    result = run_rescue(source, destination, reader=reader, max_pass=1)

    assert reader.calls == [(0, BLOCK), (64, BLOCK)]
    assert result.current_pass == 2
    assert [(item.offset, item.length, item.status) for item in result.ranges] == [
        (0, 32, "unreadable"),
        (32, 32, "skipped"),
        (64, 32, "recovered"),
    ]
    assert destination.stat().st_size == len(payload)


def test_consecutive_slow_reads_expand_skip_and_rediscover_normal_region(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    payload = patterned_bytes(256)
    source.write_bytes(payload)
    reader = SlowInjectingReader(payload, [(0, 32), (64, 32)], delay=0.02)

    result = run_rescue(source, destination, reader=reader, max_pass=1)

    offsets = [offset for offset, size in reader.calls if size == BLOCK]
    assert offsets[:3] == [0, 64, 160]
    assert 32 not in offsets
    assert 96 not in offsets and 128 not in offsets
    skipped = [(item.offset, item.length) for item in result.ranges if item.status == "skipped"]
    assert skipped == [(32, 32), (96, 64)]
    assert destination.read_bytes()[160:] == payload[160:]


def test_pass2_reads_only_ranges_skipped_by_pass1(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(128)
    source.write_bytes(payload)
    first_reader = SlowInjectingReader(payload, [(0, 32)], delay=0.02)
    run_rescue(
        source,
        destination,
        map_path=map_path,
        reader=first_reader,
        max_pass=1,
    )

    second_reader = FaultInjectingReader(payload)
    result = run_rescue(
        source,
        destination,
        map_path=map_path,
        reader=second_reader,
        max_pass=3,
    )

    # Pass 3 is deliberately coarse.  Fine 64K/sector localization belongs to
    # Pass 4 so a deadline-bound retry cannot turn into a long small-read loop.
    assert second_reader.calls == [(32, 32)]
    assert destination.read_bytes() == payload
    assert result.skipped_bytes == 0
    assert result.current_pass == 4


def test_pass3_is_optional_and_localizes_unreadable_block(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(96)
    source.write_bytes(payload)

    first_reader = FaultInjectingReader(payload, bad_ranges=[(10, 2)])
    before_deep = run_rescue(
        source,
        destination,
        map_path=map_path,
        reader=first_reader,
        max_pass=2,
    )
    assert before_deep.unreadable_bytes == BLOCK
    assert all(size != SECTOR for _, size in first_reader.calls)

    deep_reader = FaultInjectingReader(payload, bad_ranges=[(10, 2)])
    after_deep = run_rescue(
        source,
        destination,
        map_path=map_path,
        reader=deep_reader,
        max_pass=4,
    )
    expected = bytearray(payload)
    expected[10:12] = b"\0\0"
    assert destination.read_bytes() == bytes(expected)
    assert [(item.offset, item.length) for item in after_deep.bad_ranges] == [(10, 2)]
    assert after_deep.current_pass == 5


def test_resume_does_not_reread_committed_recovered_range(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(96)
    source.write_bytes(payload)
    interrupted = FaultInjectingReader(payload, interrupt_at_offset=BLOCK)

    with pytest.raises(KeyboardInterrupt):
        run_rescue(
            source,
            destination,
            map_path=map_path,
            reader=interrupted,
            max_pass=1,
        )
    map_data = read_map(map_path)
    assert map_data["pass_cursor"] == BLOCK

    resumed = FaultInjectingReader(payload)
    run_rescue(
        source,
        destination,
        map_path=map_path,
        reader=resumed,
        max_pass=1,
    )
    assert resumed.calls[0] == (BLOCK, BLOCK)
    assert destination.read_bytes() == payload


def test_v1_map_is_migrated_without_rereading_completed_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(64)
    source.write_bytes(payload)
    partial = bytearray(payload[:32])
    partial[10:12] = b"\0\0"
    destination.write_bytes(partial)
    monkeypatch.setattr(
        "flvrescue.storage._requires_sparse_destination", lambda: False
    )
    map_path.write_text(
        json.dumps(
            {
                "version": 1,
                "source_path": str(source.resolve()),
                "source_size": len(payload),
                "destination_path": str(destination.resolve()),
                "completed_until": 32,
                "bad_ranges": [{"offset": 10, "length": 2}],
            }
        ),
        encoding="utf-8",
    )
    reader = FaultInjectingReader(payload)

    run_rescue(
        source,
        destination,
        map_path=map_path,
        reader=reader,
        max_pass=1,
    )

    assert reader.calls[0] == (32, 32)
    assert read_map(map_path)["version"] == 2
    assert read_map(map_path)["policy"]["profile"] == "coverage"
    assert destination.read_bytes()[:10] == payload[:10]
    assert destination.read_bytes()[10:12] == b"\0\0"


def test_saved_policy_rejects_a_different_resume_override_before_any_read(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(64)
    source.write_bytes(payload)
    run_rescue(source, destination, map_path=map_path, reader=FaultInjectingReader(payload), max_pass=1)

    resumed = FaultInjectingReader(payload)
    with pytest.raises(ValueError, match="already saved"):
        rescue(
            source,
            destination,
            map_path=map_path,
            reader_factory=lambda _source: resumed,
            block_size=BLOCK * 2,
            max_pass=1,
            progress=False,
        )
    assert resumed.calls == []


def test_policyless_v2_map_adopts_default_coverage_before_source_read(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(32)
    source.write_bytes(payload)
    destination.write_bytes(payload)
    save_map_atomic(
        map_path,
        RescueMap(
            source_path=str(source.resolve()),
            source_size=len(payload),
            destination_path=str(destination.resolve()),
            ranges=[RecoveryRange(0, len(payload), "recovered")],
            current_pass=2,
        ),
    )
    reader = FaultInjectingReader(payload)

    rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: reader,
        progress=False,
        max_pass=2,
    )

    assert reader.calls == []
    assert read_map(map_path)["policy"] == DEFAULT_POLICY.to_dict()


def test_pass2_stops_only_the_current_survey_hole_on_error(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(192)
    source.write_bytes(payload)
    destination.write_bytes(payload)
    policy = RecoveryPolicy(
        block=BLOCK,
        fallback=FALLBACK,
        sector=SECTOR,
        checkpoint=BLOCK,
        slow_threshold=0.01,
        skip_start=BLOCK,
        skip_max=BLOCK * 4,
        skip_reset_after=1,
        survey_stride=BLOCK,
    )
    state = RescueMap(
        source_path=str(source.resolve()),
        source_size=len(payload),
        destination_path=str(destination.resolve()),
        ranges=[
            RecoveryRange(0, 32, "recovered"),
            RecoveryRange(32, 64, "skipped", "survey"),
            RecoveryRange(96, 32, "recovered"),
            RecoveryRange(128, 32, "skipped", "survey"),
            RecoveryRange(160, 32, "recovered"),
        ],
        current_pass=2,
        policy=policy,
    )
    save_map_atomic(map_path, state)

    reader = FaultInjectingReader(payload, bad_ranges=[(40, 2)])
    result = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: reader,
        progress=False,
        max_pass=2,
    )

    assert reader.calls == [(32, 32), (128, 32)]
    assert result.easy_skipped_bytes == 0
    assert [(item.offset, item.length, item.status, item.cause) for item in result.ranges] == [
        (0, 32, "recovered", None),
        (32, 32, "unreadable", "read_error"),
        (64, 32, "skipped", "read_error"),
        (96, 96, "recovered", None),
    ]


def test_resume_repeats_pass2_when_a_later_current_pass_has_survey_holes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(96)
    source.write_bytes(payload)
    destination.write_bytes(payload)
    policy = DEFAULT_POLICY.with_overrides(
        block=BLOCK,
        fallback=FALLBACK,
        sector=SECTOR,
        checkpoint=BLOCK,
        survey_stride=BLOCK,
    )
    save_map_atomic(
        map_path,
        RescueMap(
            source_path=str(source.resolve()),
            source_size=len(payload),
            destination_path=str(destination.resolve()),
            ranges=[
                RecoveryRange(0, 32, "recovered"),
                RecoveryRange(32, 32, "skipped", "survey"),
                RecoveryRange(64, 32, "recovered"),
            ],
            current_pass=3,
            policy=policy,
        ),
    )
    reader = FaultInjectingReader(payload)

    result = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: reader,
        progress=False,
        max_pass=2,
    )

    assert reader.calls == [(32, 32)]
    assert result.current_pass == 3
    assert result.easy_skipped_bytes == 0


def test_retry_request_fills_leftover_survey_hole_before_hard_range(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(128)
    source.write_bytes(payload)
    destination.write_bytes(payload)
    policy = DEFAULT_POLICY.with_overrides(
        block=BLOCK,
        fallback=FALLBACK,
        sector=SECTOR,
        checkpoint=BLOCK,
        survey_stride=BLOCK,
    )
    save_map_atomic(
        map_path,
        RescueMap(
            source_path=str(source.resolve()),
            source_size=len(payload),
            destination_path=str(destination.resolve()),
            ranges=[
                RecoveryRange(0, 32, "recovered"),
                RecoveryRange(32, 32, "skipped", "slow"),
                RecoveryRange(64, 32, "skipped", "survey"),
                RecoveryRange(96, 32, "recovered"),
            ],
            current_pass=3,
            policy=policy,
        ),
    )
    reader = FaultInjectingReader(payload)

    result = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: reader,
        progress=False,
        max_pass=3,
    )

    assert reader.calls[:2] == [(64, 32), (32, 32)]
    assert result.easy_skipped_bytes == 0
    assert result.hard_skipped_bytes == 0


def test_progress_updates_while_reader_is_blocked(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    payload = patterned_bytes(64)
    source.write_bytes(payload)
    reader = SlowInjectingReader(payload, [(0, 64)], delay=0.08)
    stream = io.StringIO()
    reporter = ProgressReporter(len(payload), label="source.flv", stream=stream, update_interval=0.02)

    rescue(
        source,
        destination,
        reader_factory=lambda _source: reader,
        progress=reporter,
        block_size=64,
        fallback_size=8,
        sector_size=2,
        checkpoint_interval=64,
        slow_threshold=1.0,
        skip_start=32,
        skip_max=128,
        max_pass=1,
    )

    lines = [line for line in stream.getvalue().splitlines() if "FLVRESCUE" in line]
    assert len(lines) >= 2
    assert any("source.flv" in line for line in lines)


def test_rejects_malformed_map_and_source_destination_aliases(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    malformed = tmp_path / "bad.map.json"
    payload = patterned_bytes(64)
    source.write_bytes(payload)
    malformed.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError):
        run_rescue(source, destination, map_path=malformed)
    with pytest.raises(ValueError):
        run_rescue(source, source)
    with pytest.raises(ValueError):
        run_rescue(source, destination, map_path=source)
    with pytest.raises(ValueError):
        run_rescue(source, destination, map_path=destination)


def test_skip_factor_quadruples_width_after_consecutive_slow_reads(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    payload = patterned_bytes(256)
    source.write_bytes(payload)
    reader = SlowInjectingReader(payload, [(0, 32), (64, 32)], delay=0.02)

    result = run_rescue(
        source,
        destination,
        reader=reader,
        max_pass=1,
        skip_factor=4,
        skip_max=BLOCK * 16,
    )

    offsets = [offset for offset, size in reader.calls if size == BLOCK]
    assert offsets[:3] == [0, 64, 224]
    skipped = [(item.offset, item.length) for item in result.ranges if item.status == "skipped"]
    assert skipped == [(32, 32), (96, 128)]


def test_skip_reset_after_keeps_skip_mode_until_consecutive_fast_reads(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    payload = patterned_bytes(256)
    source.write_bytes(payload)
    reader = SlowInjectingReader(payload, [(0, 32)], delay=0.02)

    result = run_rescue(
        source,
        destination,
        reader=reader,
        max_pass=1,
        skip_reset_after=2,
    )

    offsets = [offset for offset, size in reader.calls if size == BLOCK]
    assert 32 not in offsets
    assert 96 not in offsets
    skipped = [(item.offset, item.length, item.cause) for item in result.ranges if item.status == "skipped"]
    assert skipped == [(32, 32, "slow"), (96, 64, "probe")]
    assert destination.read_bytes()[160:] == payload[160:]


def test_survey_stride_samples_the_file_during_pass1(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    payload = patterned_bytes(256)
    source.write_bytes(payload)
    reader = FaultInjectingReader(payload)

    result = run_rescue(
        source,
        destination,
        reader=reader,
        max_pass=1,
        survey_stride=64,
    )

    assert reader.calls == [(0, BLOCK), (96, BLOCK), (192, BLOCK)]
    skipped = [(item.offset, item.length, item.cause) for item in result.ranges if item.status == "skipped"]
    assert skipped == [(32, 64, "survey"), (128, 64, "survey"), (224, 32, "survey")]
    assert result.recovered_bytes == 96


def test_pass2_fills_survey_gaps_before_slow_skips(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(192)
    source.write_bytes(payload)
    first_reader = SlowInjectingReader(payload, [(0, 32)], delay=0.02)
    run_rescue(
        source,
        destination,
        map_path=map_path,
        reader=first_reader,
        max_pass=1,
        survey_stride=32,
    )

    second_reader = FaultInjectingReader(payload)
    result = run_rescue(
        source,
        destination,
        map_path=map_path,
        reader=second_reader,
        max_pass=2,
        survey_stride=32,
    )

    assert second_reader.calls[0] == (128, 32)
    assert all(call[0] != 32 for call in second_reader.calls)
    assert result.easy_skipped_bytes == 0
    assert result.hard_skipped_bytes == 32
    hard = next(item for item in result.ranges if item.status == "skipped")
    assert hard.offset == 32
    assert hard.cause == "slow"

    third_reader = FaultInjectingReader(payload)
    filled = run_rescue(
        source,
        destination,
        map_path=map_path,
        reader=third_reader,
        max_pass=3,
        survey_stride=32,
    )
    assert third_reader.calls[0] == (32, 32)
    assert filled.hard_skipped_bytes == 0
    assert destination.read_bytes() == payload
