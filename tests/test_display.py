from __future__ import annotations

from flvrescue.display import (
    GLYPH_BAD,
    GLYPH_FAST,
    GLYPH_GOOD,
    GLYPH_HARD,
    GLYPH_PENDING,
    GLYPH_SLOW,
    GLYPH_SKIP,
    format_live_header,
    format_live_lines,
    format_preparing_lines,
    format_status_view,
    range_kind,
    render_stacked_bar,
    strip_ansi,
    tally_kinds,
    visual_line_count,
)


def test_visual_line_count_includes_wrapped_rows() -> None:
    assert visual_line_count(["abc", "de"], width=10) == 2
    assert visual_line_count(["abcdefghijabcdefghij"], width=10) == 2
    assert visual_line_count([f"\x1b[32m{'x' * 20}\x1b[0m"], width=10) == 2


def test_range_kind_splits_survey_skips_from_slow_skips() -> None:
    assert range_kind("recovered", None) == "good"
    assert range_kind("recovered", None, "slow") == "slow"
    assert range_kind("recovered", None, "hard") == "hard"
    assert range_kind("skipped", "survey") == "fast"
    assert range_kind("skipped", "slow") == "slow"
    assert range_kind("unreadable", "read_error") == "bad"
    assert range_kind("unprocessed", None) == "pending"


def test_stacked_bar_orders_good_fast_slow_bad_then_pending() -> None:
    counts = {
        "good": 30,
        "fast": 20,
        "slow": 20,
        "hard": 10,
        "bad": 10,
        "pending": 10,
    }
    bar = strip_ansi(render_stacked_bar(counts, 10, color=True))
    assert bar == (
        GLYPH_GOOD * 3
        + GLYPH_FAST * 2
        + GLYPH_SLOW * 2
        + GLYPH_HARD
        + GLYPH_BAD
        + GLYPH_PENDING
    )


def test_live_lines_match_requested_layout() -> None:
    counts = {"good": 1700, "fast": 10500, "slow": 7300, "hard": 900, "bad": 332, "pending": 0}
    lines = format_live_lines(
        name="493.flv",
        total=50_700_000_000,
        counts=counts,
        elapsed=175,
        width=40,
        color=False,
    )
    assert format_live_header("493.flv", 50_700_000_000).startswith(
        "FLVRESCUE Pass 1 Survey reads 493.flv"
    )
    assert len(lines) == 2
    assert "02:55" in lines[0]
    assert lines[1].startswith("good ")
    assert "fast " in lines[1]
    assert "slow " in lines[1]
    assert "hard " in lines[1]
    assert "bad " in lines[1]


def test_live_stats_color_labels_gray_and_amounts_by_kind() -> None:
    counts = {"good": 4, "fast": 3, "slow": 2, "hard": 1, "bad": 1, "pending": 0}
    lines = format_live_lines(
        name="clip.flv",
        total=10,
        counts=counts,
        elapsed=0,
        width=24,
        color=True,
    )
    stats = lines[1]
    assert stats.index("\x1b[90mgood\x1b[0m") < stats.index("\x1b[32m")
    assert "\x1b[34m" in stats
    assert "\x1b[33m" in stats
    assert "\x1b[35m" in stats
    assert "\x1b[31m" in stats
    bar = lines[0]
    assert bar.index("\x1b[32m") < bar.index("\x1b[34m") < bar.index("\x1b[33m") < bar.index("\x1b[35m") < bar.index("\x1b[31m")


def test_live_local_read_places_detail_above_the_current_marker() -> None:
    gib = 1024**3
    total = 50 * gib
    ranges = (
        (0, 48 * gib, "recovered", None, "fast"),
        (48 * gib, gib, "skipped", "slow", "slow"),
        (49 * gib, gib, "unprocessed", None, None),
    )
    counts = tally_kinds(ranges, total)
    lines = format_live_lines(
        name="493.flv",
        total=total,
        counts=counts,
        elapsed=175,
        width=100,
        color=True,
        ranges=ranges,
        read_offset=int(48.620 * gib),
        read_size=8 * 1024**2,
        read_elapsed=23.6,
        read_status="hard",
        section_start=int(48.5 * gib),
        section_end=49 * gib,
    )

    assert len(lines) == 4
    detail = strip_ansi(lines[2])
    local_bar = strip_ansi(lines[3])
    assert detail.index("↓") == local_bar.index("▶")
    assert "48.620 GiB (8.0 MiB)  HARD 00:23.6" in detail
    assert local_bar.startswith("48.50 GiB  ")
    assert local_bar.endswith("  49.00 GiB")
    assert "\x1b[35mHARD\x1b[0m" in lines[2]
    assert "\x1b[96m▶\x1b[0m" in lines[3]


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
    assert "next: Pass 2 Fast" in body[-3]
    assert "remaining " in body[-2]
    assert body[-1].startswith("map ")


def test_preparing_lines_show_elapsed_time_without_a_fake_percent() -> None:
    lines = format_preparing_lines(
        name="rescued.flv",
        total=76 * 1024**3,
        elapsed=5,
        spinner="/",
        storage_mode="sparse",
        allocated_bytes=0,
    )
    assert lines[0] == "FLVRESCUE prepares rescued.flv"
    assert lines[1] == "Preparing destination / 00:05"
    assert "Source extent: 76.0 GiB" in lines[2]
    assert lines[3] == "Storage mode: sparse  Allocated: 0 B"
    assert "%" not in "\n".join(lines)
