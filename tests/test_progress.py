from __future__ import annotations

from flvrescue.progress import classify_span, render_file_map


def test_classify_span_distinguishes_survey_skips_from_slow_skips() -> None:
    assert classify_span("recovered", None) == "recovered"
    assert classify_span("recovered", "slow") == "recovered_slow"
    assert classify_span("skipped", "survey") == "survey"
    assert classify_span("skipped", "slow") == "hard_skip"
    assert classify_span("unreadable", "read_error") == "unreadable"


def test_render_file_map_uses_majority_class_and_marks_read_head() -> None:
    ranges = (
        (0, 40, "recovered", None),
        (40, 20, "skipped", "slow"),
        (60, 40, "skipped", "survey"),
    )
    glyphs = render_file_map(ranges, 100, width=10, read_offset=70)
    assert len(glyphs) == 10
    assert glyphs.startswith("####")
    assert "!" in glyphs
    assert "*" in glyphs
    assert glyphs.endswith(".")
