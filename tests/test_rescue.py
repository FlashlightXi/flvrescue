from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from conftest import FaultInjectingReader, SlowInjectingReader
from flvrescue import rescue
from flvrescue.progress import ProgressReporter


BLOCK = 32
FALLBACK = 8
SECTOR = 2


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
        max_pass=2,
    )

    assert second_reader.calls == [(32, 8), (40, 8), (48, 8), (56, 8)]
    assert destination.read_bytes() == payload
    assert result.skipped_bytes == 0
    assert result.current_pass == 3


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
        max_pass=3,
    )
    expected = bytearray(payload)
    expected[10:12] = b"\0\0"
    assert destination.read_bytes() == bytes(expected)
    assert [(item.offset, item.length) for item in after_deep.bad_ranges] == [(10, 2)]
    assert after_deep.current_pass == 4


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


def test_v1_map_is_migrated_without_rereading_completed_prefix(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(64)
    source.write_bytes(payload)
    partial = bytearray(payload[:32])
    partial[10:12] = b"\0\0"
    destination.write_bytes(partial)
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
    assert destination.read_bytes()[:10] == payload[:10]
    assert destination.read_bytes()[10:12] == b"\0\0"


def test_progress_updates_while_reader_is_blocked(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    payload = patterned_bytes(64)
    source.write_bytes(payload)
    reader = SlowInjectingReader(payload, [(0, 64)], delay=0.08)
    stream = io.StringIO()
    reporter = ProgressReporter(len(payload), stream=stream, update_interval=0.02)

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

    lines = [line for line in stream.getvalue().splitlines() if "Pass 1" in line]
    assert len(lines) >= 2
    assert any("reading" in line for line in lines)


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
