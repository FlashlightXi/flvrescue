from __future__ import annotations

import argparse

import pytest

from flvrescue.cli import build_parser, parse_positive_int, parse_size


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4096", 4096),
        ("64K", 64 * 1024),
        ("64kB", 64 * 1024),
        ("8M", 8 * 1024 * 1024),
        ("8MiB", 8 * 1024 * 1024),
        ("1G", 1024 * 1024 * 1024),
        ("1T", 1024**4),
    ],
)
def test_parse_size_accepts_byte_counts_and_binary_suffixes(text: str, expected: int) -> None:
    assert parse_size(text) == expected


def test_parse_size_rejects_unknown_units() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_size("8MBB")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_size("0")


def test_parser_exposes_survey_and_skip_growth_options() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "src.flv",
            "dst.flv",
            "--skip-start",
            "128M",
            "--skip-factor",
            "4",
            "--skip-max",
            "1G",
            "--skip-reset-after",
            "8",
            "--survey-stride",
            "256M",
            "--max-pass",
            "1",
        ]
    )
    assert args.skip_start == 128 * 1024 * 1024
    assert args.skip_factor == 4
    assert args.skip_max == 1024 * 1024 * 1024
    assert args.skip_reset_after == 8
    assert args.survey_stride == 256 * 1024 * 1024
    assert args.max_pass == 1


def test_parse_positive_int_rejects_zero() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_positive_int("0")
