from __future__ import annotations

import io

from flvrescue.flvfill import (
    FLV_SCRIPT_TAG,
    overlay_flv_placeholders,
    overlay_map_holes,
)
from flvrescue.mapfile import RecoveryRange


def test_overlay_writes_script_tag_header_and_previous_size() -> None:
    handle = io.BytesIO(b"\x00" * 20)
    covered = overlay_flv_placeholders(handle, 0, 20)
    assert covered == 20
    data = handle.getvalue()
    assert data[0] == FLV_SCRIPT_TAG
    data_size = int.from_bytes(data[1:4], "big")
    assert data_size == 5
    assert int.from_bytes(data[16:20], "big") == 16


def test_overlay_map_holes_skips_recovered_ranges() -> None:
    handle = io.BytesIO(b"\x11" * 30)
    ranges = (
        RecoveryRange(0, 10, "recovered"),
        RecoveryRange(10, 15, "skipped", "survey"),
        RecoveryRange(25, 5, "unprocessed"),
    )
    overlay_map_holes(handle, ranges)
    data = handle.getvalue()
    assert data[:10] == b"\x11" * 10
    assert data[10] == FLV_SCRIPT_TAG
    assert data[25:] == b"\x11" * 5
