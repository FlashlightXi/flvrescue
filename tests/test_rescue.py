from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from conftest import FaultInjectingReader


SMALL_BLOCK = 32
SMALL_FALLBACK = 8
SMALL_SECTOR = 2
DEFAULT_BLOCK = 8 * 1024 * 1024
DEFAULT_FALLBACK = 64 * 1024
DEFAULT_SECTOR = 4 * 1024


def patterned_bytes(size: int) -> bytes:
    return bytes((index * 37 + 11) % 256 for index in range(size))


def run_rescue(
    source: Path,
    destination: Path,
    *,
    map_path: Path | None = None,
    reader: FaultInjectingReader | None = None,
) -> None:
    from flvrescue import rescue

    kwargs: dict[str, Any] = {
        "map_path": map_path,
        "block_size": SMALL_BLOCK,
        "fallback_size": SMALL_FALLBACK,
        "sector_size": SMALL_SECTOR,
    }
    if reader is not None:
        kwargs["reader_factory"] = lambda _source: reader
    rescue(source, destination, **kwargs)


def read_map(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def map_value(map_data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in map_data:
            return map_data[key]
    raise AssertionError(f"map is missing one of {keys!r}: {map_data!r}")


def assert_path_value(actual: Any, expected: Path) -> None:
    assert Path(actual).resolve() == expected.resolve()


def test_normal_copy_preserves_bytes_hash_and_map_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(93)  # Includes a short final normal block.
    source.write_bytes(payload)

    run_rescue(source, destination, map_path=map_path)

    recovered = destination.read_bytes()
    assert recovered == payload
    assert hashlib.sha256(recovered).digest() == hashlib.sha256(payload).digest()

    map_data = read_map(map_path)
    assert_path_value(map_value(map_data, "source_path", "source"), source)
    assert map_value(map_data, "source_size") == len(payload)
    assert_path_value(map_value(map_data, "destination_path", "destination"), destination)
    assert map_value(map_data, "completed_until") == len(payload)
    assert map_value(map_data, "bad_ranges") == []


def test_io_error_uses_hierarchical_fallback_and_zeros_only_bad_sector(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(64)
    source.write_bytes(payload)
    reader = FaultInjectingReader(payload, bad_ranges=[(10, 2)])

    run_rescue(source, destination, map_path=map_path, reader=reader)

    expected = bytearray(payload)
    expected[10:12] = b"\0\0"
    assert destination.read_bytes() == bytes(expected)
    assert reader.calls[0] == (0, SMALL_BLOCK)
    assert (8, SMALL_FALLBACK) in reader.calls
    assert reader.calls.index((0, SMALL_BLOCK)) < reader.calls.index((8, SMALL_FALLBACK))
    assert [(8, SMALL_SECTOR), (10, SMALL_SECTOR), (12, SMALL_SECTOR), (14, SMALL_SECTOR)] == [
        call for call in reader.calls if 8 <= call[0] < 16 and call[1] == SMALL_SECTOR
    ]
    assert read_map(map_path)["bad_ranges"] == [{"offset": 10, "length": 2}]
    assert reader.closed


def test_fallback_zeros_a_full_fallback_sized_unreadable_range(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    payload = patterned_bytes(64)
    source.write_bytes(payload)
    reader = FaultInjectingReader(payload, bad_ranges=[(8, SMALL_FALLBACK)])

    run_rescue(source, destination, reader=reader)

    expected = bytearray(payload)
    expected[8 : 8 + SMALL_FALLBACK] = b"\0" * SMALL_FALLBACK
    assert destination.read_bytes() == bytes(expected)


@pytest.mark.parametrize(
    "bad_length",
    [DEFAULT_SECTOR, DEFAULT_FALLBACK],
    ids=["4k-aligned-bad-range", "64k-aligned-bad-range"],
)
def test_default_size_hierarchy_localizes_real_4k_and_64k_bad_ranges(
    tmp_path: Path, bad_length: int
) -> None:
    """Exercise the production 8 MiB -> 64 KiB -> 4 KiB defaults unchanged."""
    from flvrescue import rescue

    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    # One failed 8 MiB block plus a small successful final block proves that
    # recovery resumes after the localized failure without making a huge fixture.
    payload_size = DEFAULT_BLOCK + 137
    payload = (bytes(range(256)) * ((payload_size + 255) // 256))[:payload_size]
    bad_offset = 2 * DEFAULT_FALLBACK  # aligned to both 64 KiB and 4 KiB
    source.write_bytes(payload)
    reader = FaultInjectingReader(payload, bad_ranges=[(bad_offset, bad_length)])

    rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: reader,
        progress=False,
    )

    expected = bytearray(payload)
    expected[bad_offset : bad_offset + bad_length] = b"\0" * bad_length
    recovered = destination.read_bytes()
    assert recovered == bytes(expected)
    assert recovered[bad_offset + bad_length :] == payload[bad_offset + bad_length :]
    assert reader.calls[0] == (0, DEFAULT_BLOCK)
    assert (bad_offset, DEFAULT_FALLBACK) in reader.calls
    assert [
        (offset, DEFAULT_SECTOR)
        for offset in range(bad_offset, bad_offset + DEFAULT_FALLBACK, DEFAULT_SECTOR)
    ] == [
        call
        for call in reader.calls
        if bad_offset <= call[0] < bad_offset + DEFAULT_FALLBACK and call[1] == DEFAULT_SECTOR
    ]
    map_data = read_map(map_path)
    assert map_data["completed_until"] == len(payload)
    assert map_data["bad_ranges"] == [{"offset": bad_offset, "length": bad_length}]
    assert reader.closed


def test_contiguous_bad_sectors_are_merged_in_map(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(64)
    source.write_bytes(payload)
    reader = FaultInjectingReader(payload, bad_ranges=[(10, 2), (12, 2)])

    run_rescue(source, destination, map_path=map_path, reader=reader)

    expected = bytearray(payload)
    expected[10:14] = b"\0" * 4
    assert destination.read_bytes() == bytes(expected)
    assert read_map(map_path)["bad_ranges"] == [{"offset": 10, "length": 4}]


def test_bad_range_crossing_normal_block_boundary_recovers_after_it(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    payload = patterned_bytes(67)
    source.write_bytes(payload)
    reader = FaultInjectingReader(payload, bad_ranges=[(30, 4)])

    run_rescue(source, destination, reader=reader)

    expected = bytearray(payload)
    expected[30:34] = b"\0" * 4
    assert destination.read_bytes() == bytes(expected)
    assert payload[34:] == destination.read_bytes()[34:]
    assert (32, SMALL_BLOCK) in reader.calls


def test_resume_starts_at_saved_offset_and_matches_one_shot_copy(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = patterned_bytes(96)
    source.write_bytes(payload)

    interrupted_reader = FaultInjectingReader(payload, interrupt_at_offset=SMALL_BLOCK)
    try:
        run_rescue(source, destination, map_path=map_path, reader=interrupted_reader)
    except KeyboardInterrupt:
        # Either returning normally or re-raising is acceptable if the durable map exists.
        pass

    map_data = read_map(map_path)
    completed_until = map_data["completed_until"]
    assert completed_until == SMALL_BLOCK
    # Only the map's contiguous prefix is advertised as durable recovered data.
    # Implementations may preallocate the final logical size, or leave a short
    # partial file until resume.
    partial = destination.read_bytes()
    assert partial[:completed_until] == payload[:completed_until]
    assert len(partial) >= completed_until
    if len(partial) > completed_until:
        assert partial[completed_until:] == b"\0" * (len(partial) - completed_until)

    resumed_reader = FaultInjectingReader(payload)
    run_rescue(source, destination, map_path=map_path, reader=resumed_reader)

    assert resumed_reader.calls
    assert resumed_reader.calls[0][0] >= completed_until
    assert destination.read_bytes() == payload


def test_rejects_malformed_map_source_change_and_same_source_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    malformed_map = tmp_path / "malformed.map.json"
    payload = patterned_bytes(64)
    source.write_bytes(payload)
    malformed_map.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(ValueError):
        run_rescue(source, destination, map_path=malformed_map)

    with pytest.raises(ValueError):
        run_rescue(source, source)

    map_path = tmp_path / "resume.map.json"
    interrupted_reader = FaultInjectingReader(payload, interrupt_at_offset=SMALL_BLOCK)
    try:
        run_rescue(source, destination, map_path=map_path, reader=interrupted_reader)
    except KeyboardInterrupt:
        pass
    source.write_bytes(payload + b"changed")
    with pytest.raises(ValueError):
        run_rescue(source, destination, map_path=map_path)


def test_rejects_map_path_that_aliases_source_or_destination(tmp_path: Path) -> None:
    """A map must never be allowed to overwrite either data file."""
    from flvrescue import rescue

    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    payload = patterned_bytes(64)
    source.write_bytes(payload)

    with pytest.raises(ValueError):
        rescue(source, destination, map_path=source)
    assert source.read_bytes() == payload
    assert not destination.exists()

    with pytest.raises(ValueError):
        rescue(source, destination, map_path=destination)
    assert source.read_bytes() == payload
    assert not destination.exists()
