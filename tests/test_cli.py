from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from flvrescue.cli import (
    _selected_max_pass,
    build_parser,
    main,
    parse_positive_int,
    parse_size,
    parse_through,
)
from flvrescue.policy import DEFAULT_POLICY
from flvrescue.rescue import rescue as rescue_api


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


def test_parser_keeps_advanced_overrides_optional_and_accepts_named_passes() -> None:
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
            "--through",
            "survey",
        ]
    )
    assert args.skip_start == 128 * 1024 * 1024
    assert args.skip_factor == 4
    assert args.skip_max == 1024 * 1024 * 1024
    assert args.skip_reset_after == 8
    assert args.survey_stride == 256 * 1024 * 1024
    assert args.through == 1
    assert args.max_pass is None

    defaults = parser.parse_args(["src.flv", "dst.flv"])
    assert defaults.block is None
    assert defaults.survey_stride is None
    assert _selected_max_pass(defaults) == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("survey", 1),
        ("fill", 2),
        ("retry", 3),
        ("deep", 4),
        ("1", 1),
        ("4", 4),
    ],
)
def test_parse_through_accepts_named_and_numeric_passes(text: str, expected: int) -> None:
    assert parse_through(text) == expected


def test_parser_rejects_through_and_legacy_max_pass_together() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["src.flv", "dst.flv", "--through", "fill", "--max-pass", "2"]
        )


def test_parse_positive_int_rejects_zero() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_positive_int("0")


def test_rescue_exposes_the_policy_aware_cli_contract() -> None:
    parameters = inspect.signature(rescue_api).parameters
    assert parameters["policy"].default is None
    for name in (
        "block_size",
        "fallback_size",
        "sector_size",
        "slow_threshold",
        "skip_start",
        "skip_factor",
        "skip_max",
        "skip_reset_after",
        "survey_stride",
    ):
        assert parameters[name].default is None


def _result(*, source: Path, destination: Path, map_path: Path, current_pass: int) -> SimpleNamespace:
    return SimpleNamespace(
        source=source,
        destination=destination,
        map_path=map_path,
        recovered_bytes=128,
        easy_skipped_bytes=0,
        hard_skipped_bytes=0,
        unreadable_bytes=0,
        unprocessed_bytes=0,
        current_pass=current_pass,
    )


def _write_map(
    map_path: Path,
    *,
    source: Path,
    destination: Path,
    include_policy: bool = True,
) -> None:
    data: dict[str, object] = {
        "version": 2,
        "source_path": str(source),
        "source_size": 128,
        "destination_path": str(destination),
        "current_pass": 2,
        "pass_cursor": 0,
        "adaptive_skip": 0,
        "ranges": [{"offset": 0, "length": 128, "status": "unprocessed"}],
    }
    if include_policy:
        data["policy"] = DEFAULT_POLICY.to_dict()
    map_path.write_text(json.dumps(data), encoding="utf-8")


def test_new_run_uses_default_policy_and_forwards_only_explicit_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    seen: dict[str, object] = {}

    def fake_rescue(*args: object, **kwargs: object) -> SimpleNamespace:
        seen["args"] = args
        seen.update(kwargs)
        return _result(
            source=source,
            destination=destination,
            map_path=Path(str(destination) + ".rescue.json"),
            current_pass=2,
        )

    monkeypatch.setattr("flvrescue.cli.rescue", fake_rescue)

    assert main([str(source), str(destination), "--through", "survey", "--no-progress"]) == 0
    assert seen["policy"] is None
    assert seen["max_pass"] == 1
    assert seen["block_size"] is None
    assert seen["checkpoint_interval"] is None
    assert seen["survey_stride"] is None
    assert "Completed through: Pass 1 Survey" in capsys.readouterr().out


def test_optimize_prints_a_command_without_starting_rescue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source &' recording.flv"
    destination = tmp_path / "rescued &' recording.flv"
    source.write_bytes(b"FLV" + b"x" * 1024)

    def unexpected_rescue(*args: object, **kwargs: object) -> SimpleNamespace:
        raise AssertionError("optimize must not start rescue")

    monkeypatch.setattr("flvrescue.cli.rescue", unexpected_rescue)

    assert main(
        [
            "optimize",
            str(source),
            str(destination),
            "--preference",
            "fast",
            "--available-space",
            "10G",
            "--no-input",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "FLVRESCUE optimization recommendation" in output
    assert "Suggested stage:  survey" in output
    assert "--through survey" in output
    assert "source &'' recording.flv'" in output
    assert "rescued &'' recording.flv'" in output
    assert "did not read source contents or start recovery" in output
    assert not destination.exists()


def test_optimize_low_space_recommends_batch_rotation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    source.write_bytes(b"x" * 1024)

    assert main(
        [
            "optimize",
            str(source),
            str(destination),
            "--preference",
            "thorough",
            "--available-space",
            "1M",
            "--no-input",
        ]
    ) == 2
    output = capsys.readouterr().out
    assert "Warning:" in output
    assert "No command generated:" in output
    assert "Recommended command:" not in output


def test_resume_uses_saved_paths_and_policy_without_destination_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.flv.rescue.json"
    _write_map(map_path, source=source, destination=destination)
    seen: dict[str, object] = {}

    def fake_rescue(*args: object, **kwargs: object) -> SimpleNamespace:
        seen["args"] = args
        seen.update(kwargs)
        return _result(
            source=source,
            destination=destination,
            map_path=map_path,
            current_pass=3,
        )

    monkeypatch.setattr("flvrescue.cli.rescue", fake_rescue)

    assert main(["resume", str(destination), "--through", "fill", "--no-progress"]) == 0
    assert seen["args"] == (str(source), str(destination))
    assert seen["map_path"] == map_path.resolve()
    assert seen["policy"] == DEFAULT_POLICY
    assert seen["max_pass"] == 2


def test_resume_rejects_destination_that_does_not_match_selected_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.flv"
    saved_destination = tmp_path / "saved.flv"
    requested_destination = tmp_path / "requested.flv"
    map_path = tmp_path / "saved.map.json"
    _write_map(map_path, source=source, destination=saved_destination)

    def unexpected_rescue(*args: object, **kwargs: object) -> SimpleNamespace:
        raise AssertionError("rescue must not begin before destination validation")

    monkeypatch.setattr("flvrescue.cli.rescue", unexpected_rescue)

    assert main(["resume", str(requested_destination), "--map", str(map_path)]) == 1
    assert "does not match destination_path" in capsys.readouterr().err


def test_resume_adopts_legacy_policy_with_an_explicit_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.flv.rescue.json"
    _write_map(map_path, source=source, destination=destination, include_policy=False)
    seen: dict[str, object] = {}

    def fake_rescue(*args: object, **kwargs: object) -> SimpleNamespace:
        seen.update(kwargs)
        return _result(
            source=source,
            destination=destination,
            map_path=map_path,
            current_pass=3,
        )

    monkeypatch.setattr("flvrescue.cli.rescue", fake_rescue)

    assert main(["resume", str(map_path), "--no-progress"]) == 0
    assert seen["policy"] is DEFAULT_POLICY
    assert "legacy map has no saved policy" in capsys.readouterr().err


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
