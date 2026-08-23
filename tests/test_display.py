from __future__ import annotations

from flvrescue.display import (
    GLYPH_BAD,
    GLYPH_GOOD,
    GLYPH_PENDING,
    GLYPH_SKIP,
    format_live_lines,
    format_status_view,
    range_kind,
    render_stacked_bar,
    strip_ansi,
    tally_kinds,
)


def test_range_kind_splits_survey_skips_from_slow_skips() -> None:
    assert range_kind("recovered", None) == "good"
    assert range_kind("recovered", "slow") == "good"
    assert range_kind("skipped", "survey") == "fast"
    assert range_kind("skipped", "slow") == "slow"
    assert range_kind("unreadable", "read_error") == "bad"
    assert range_kind("unprocessed", None) == "pending"


def test_stacked_bar_orders_good_fast_slow_bad_then_pending() -> None:
    counts = {"good": 40, "fast": 20, "slow": 20, "bad": 10, "pending": 10}
    bar = strip_ansi(render_stacked_bar(counts, 10, color=True))
    assert bar == (
        GLYPH_GOOD * 4
        + GLYPH_SKIP * 2
        + GLYPH_SKIP * 2
        + GLYPH_BAD
        + GLYPH_PENDING
    )


def test_live_lines_match_requested_layout() -> None:
    counts = {"good": 1700, "fast": 10500, "slow": 7300, "bad": 332, "pending": 0}
    lines = format_live_lines(
        name="493.flv",
        total=50_700_000_000,
        counts=counts,
        elapsed=175,
        width=40,
        color=False,
    )
    assert lines[0].startswith("FLVRESCUE reads 493.flv")
    assert "02:55" in lines[1]
    assert lines[2].startswith("good ")
    assert "fast " in lines[2]
    assert "slow " in lines[2]
    assert "bad " in lines[2]


def test_live_stats_color_labels_gray_and_amounts_by_kind() -> None:
    counts = {"good": 4, "fast": 3, "slow": 2, "bad": 1, "pending": 0}
    lines = format_live_lines(
        name="clip.flv",
        total=10,
        counts=counts,
        elapsed=0,
        width=24,
        color=True,
    )
    stats = lines[2]
    assert stats.index("\x1b[90mgood\x1b[0m") < stats.index("\x1b[32m")
    assert "\x1b[34m" in stats
    assert "\x1b[33m" in stats
    assert "\x1b[31m" in stats
    bar = lines[1]
    assert bar.index("\x1b[32m") < bar.index("\x1b[34m") < bar.index("\x1b[33m") < bar.index("\x1b[31m")


def test_status_view_fills_rows_in_offset_order() -> None:
    ranges = (
        (0, 40, "recovered", None),
        (40, 20, "skipped", "survey"),
        (60, 20, "skipped", "slow"),
        (80, 20, "unreadable", "read_error"),
    )
    counts = tally_kinds(ranges, 100)
    view = format_status_view(
        name="clip.flv",
        total=100,
        counts=counts,
        ranges=ranges,
        current_pass=2,
        map_path="clip.flv.rescue.json",
        width=20,
        height=6,
        color=False,
    )
    body = strip_ansi(view).splitlines()
    assert len(body) == 6
    assert body[0] == GLYPH_GOOD * 20
    assert GLYPH_SKIP in body[1]
    assert GLYPH_BAD in body[2]
    assert "pass 2" in body[-3]
    assert "next: fill likely-good skips" in body[-3]
    assert "todo " in body[-2]
    assert body[-1].startswith("map ")
