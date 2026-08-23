from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from flvrescue.cli import build_parser, main, parse_positive_int, parse_size


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


def test_through_fill_means_pass2_only() -> None:
    parser = build_parser()
    args = parser.parse_args(["src.flv", "dst.flv", "--through", "fill"])
    assert args.through == "fill"
    assert args.max_pass is None


def test_parse_positive_int_rejects_zero() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_positive_int("0")


def test_status_command_renders_map_without_source_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "clip.flv"
    destination = tmp_path / "rescued.flv"
    source.write_bytes(b"FLV\x01" + b"abcdefgh" * 16)
    destination.write_bytes(source.read_bytes())
    (tmp_path / "rescued.flv.rescue.json").write_text(
        """
{
  "version": 2,
  "source_path": "%s",
  "source_size": 128,
  "destination_path": "%s",
  "current_pass": 2,
  "pass_cursor": 0,
  "adaptive_skip": 0,
  "ranges": [
    {"offset": 0, "length": 32, "status": "recovered"},
    {"offset": 32, "length": 32, "status": "skipped", "cause": "survey"},
    {"offset": 64, "length": 32, "status": "skipped", "cause": "slow"},
    {"offset": 96, "length": 32, "status": "unreadable", "cause": "read_error"}
  ]
}
"""
        % (source.as_posix(), destination.as_posix()),
        encoding="utf-8",
    )
    assert main(["status", str(destination)]) == 0
    output = capsys.readouterr().out
    assert "FLVRESCUE status clip.flv" in output
    assert "good " in output
    assert "fast " in output
    assert "slow " in output
    assert "bad " in output
    assert "map " in output
    assert main(["analyze", str(tmp_path / "rescued.flv.rescue.json")]) == 0
    again = capsys.readouterr().out
    assert "FLVRESCUE status clip.flv" in again
