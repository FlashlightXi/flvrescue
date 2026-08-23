"""Command-line interface for flvrescue."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from .display import format_bytes, format_status_view, tally_kinds
from .mapfile import MapValidationError, load_map
from .rescue import (
    DEFAULT_BLOCK_SIZE,
    DEFAULT_FALLBACK_SIZE,
    DEFAULT_MAX_PASS,
    DEFAULT_SECTOR_SIZE,
    DEFAULT_SKIP_FACTOR,
    DEFAULT_SKIP_MAX,
    DEFAULT_SKIP_RESET_AFTER,
    DEFAULT_SKIP_START,
    DEFAULT_SLOW_THRESHOLD,
    DEFAULT_SURVEY_STRIDE,
    RescueResult,
    rescue,
)


_SIZE_RE = re.compile(
    r"^(?P<number>[0-9]+)\s*(?P<unit>[kKmMgGtT]?)(?:i?[bB])?$"
)


def parse_size(value: str, *, allow_zero: bool = False) -> int:
    """Parse byte counts with 1024-based K/M/G/T suffixes.

    Accepted forms include ``4096``, ``64K``, ``8M``, ``128MiB``, and ``1G``.
    """

    match = _SIZE_RE.fullmatch(value.strip())
    if match is None:
        raise argparse.ArgumentTypeError(
            "size must be a byte count or use a K, M, G, or T suffix"
        )
    number = int(match.group("number"))
    unit = match.group("unit").upper()
    multiplier = {
        "": 1,
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
    }[unit]
    size = number * multiplier
    if size < 0 or (size == 0 and not allow_zero):
        raise argparse.ArgumentTypeError("size must be greater than zero")
    return size


def parse_nonnegative_size(value: str) -> int:
    return parse_size(value, allow_zero=True)


def parse_positive_int(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flvrescue",
        description=(
            "Recover fast ranges first, then fill likely-good skips, then retry slow ranges. "
            "Sizes accept bytes or K/M/G/T suffixes (1024-based)."
        ),
        epilog=(
            "Inspect a saved map without reading the source drive: "
            "flvrescue status FILE"
        ),
    )
    parser.add_argument("source", help="read-only source file on the failing drive")
    parser.add_argument("destination", help="new rescue output file")
    parser.add_argument("--map", dest="map_path", help="resume sidecar JSON path")
    parser.add_argument(
        "--block",
        type=parse_size,
        default=DEFAULT_BLOCK_SIZE,
        metavar="SIZE",
        help="normal read size (default: 8M)",
    )
    parser.add_argument(
        "--fallback",
        type=parse_size,
        default=DEFAULT_FALLBACK_SIZE,
        metavar="SIZE",
        help="read size after a block error (default: 64K)",
    )
    parser.add_argument(
        "--sector",
        type=parse_size,
        default=DEFAULT_SECTOR_SIZE,
        metavar="SIZE",
        help="zero-fill unit after a fallback error (default: 4K)",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="suppress periodic progress output",
    )
    parser.add_argument(
        "--slow-threshold",
        type=float,
        default=DEFAULT_SLOW_THRESHOLD,
        metavar="SECONDS",
        help="successful read duration treated as slow (default: 2.0)",
    )
    parser.add_argument(
        "--skip-start",
        type=parse_size,
        default=DEFAULT_SKIP_START,
        metavar="SIZE",
        help="initial adaptive skip width after slow/error (default: 8M)",
    )
    parser.add_argument(
        "--skip-factor",
        type=parse_positive_int,
        default=DEFAULT_SKIP_FACTOR,
        metavar="N",
        help="multiply skip width after each slow/error (default: 2)",
    )
    parser.add_argument(
        "--skip-max",
        type=parse_size,
        default=DEFAULT_SKIP_MAX,
        metavar="SIZE",
        help="maximum adaptive skip width (default: 1G)",
    )
    parser.add_argument(
        "--skip-reset-after",
        type=parse_positive_int,
        default=DEFAULT_SKIP_RESET_AFTER,
        metavar="N",
        help="consecutive fast reads required before leaving skip mode (default: 1)",
    )
    parser.add_argument(
        "--survey-stride",
        type=parse_nonnegative_size,
        default=DEFAULT_SURVEY_STRIDE,
        metavar="SIZE",
        help=(
            "Pass 1 whole-file sample skip after each fast read; 0 disables "
            "(example: 128M)"
        ),
    )
    parser.add_argument(
        "--max-pass",
        type=int,
        choices=(1, 2, 3, 4),
        default=DEFAULT_MAX_PASS,
        help=(
            "last pass to run: 1 fast scan, 2 likely-good fill, "
            "3 slow/error retry, 4 deep recovery (default: 2)"
        ),
    )
    return parser


def resolve_map_path(path: Path, *, explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    path = path.expanduser().resolve()
    candidates: list[Path] = []
    if path.name.endswith(".json"):
        candidates.append(path)
    candidates.extend(
        [
            path.with_name(f"{path.name}.rescue.json"),
            Path(str(path) + ".rescue.json"),
        ]
    )
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        try:
            load_map(candidate)
        except (OSError, MapValidationError, UnicodeDecodeError, ValueError):
            continue
        return candidate
    raise FileNotFoundError(f"no rescue map found for {path}")


def build_status_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flvrescue status",
        description="Render the occupancy map for a rescue output or map file.",
    )
    parser.add_argument("path", help="rescue destination or .rescue.json map")
    parser.add_argument("--map", dest="map_path", help="explicit resume map path")
    return parser


def run_status(argv: Sequence[str] | None = None) -> int:
    parser = build_status_parser()
    args = parser.parse_args(argv)
    try:
        map_path = resolve_map_path(
            Path(args.path),
            explicit=Path(args.map_path) if args.map_path else None,
        )
        state = load_map(map_path)
    except (OSError, MapValidationError) as exc:
        print(f"flvrescue: error: {exc}", file=sys.stderr)
        return 1
    ranges = tuple(
        (item.offset, item.length, item.status, item.cause) for item in state.ranges
    )
    counts = tally_kinds(ranges, state.source_size)
    size = shutil.get_terminal_size(fallback=(80, 24))
    color = bool(sys.stdout.isatty())
    sys.stdout.write(
        format_status_view(
            name=Path(state.source_path).name,
            total=state.source_size,
            counts=counts,
            ranges=ranges,
            current_pass=state.current_pass,
            map_path=str(map_path),
            width=size.columns,
            height=size.lines,
            color=color,
        )
    )
    return 0


def _print_summary(result: RescueResult) -> None:
    print("\nRescue run completed.")
    print(f"Target:           {result.destination}")
    print(f"Recovered:        {format_bytes(result.recovered_bytes)}")
    print(f"Likely-good skip: {format_bytes(result.easy_skipped_bytes)}")
    print(f"Slow/error skip:  {format_bytes(result.hard_skipped_bytes)}")
    print(f"Unreadable:       {format_bytes(result.unreadable_bytes)}")
    print(f"Unprocessed:      {format_bytes(result.unprocessed_bytes)}")
    if result.current_pass <= 4:
        print(f"Next pass:        {result.current_pass}")
    print(f"Resume map:       {result.map_path}")


def main(argv: Sequence[str] | None = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    if argv_list and argv_list[0] in {"status", "analyze"}:
        return run_status(argv_list[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.block < args.fallback or args.fallback < args.sector:
        parser.error("--block >= --fallback >= --sector is required")
    if args.slow_threshold <= 0:
        parser.error("--slow-threshold must be greater than zero")
    if args.skip_start > args.skip_max:
        parser.error("--skip-start must not exceed --skip-max")
    try:
        result = rescue(
            args.source,
            args.destination,
            map_path=args.map_path,
            progress=not args.no_progress,
            block_size=args.block,
            fallback_size=args.fallback,
            sector_size=args.sector,
            slow_threshold=args.slow_threshold,
            skip_start=args.skip_start,
            skip_factor=args.skip_factor,
            skip_max=args.skip_max,
            skip_reset_after=args.skip_reset_after,
            survey_stride=args.survey_stride,
            max_pass=args.max_pass,
        )
    except KeyboardInterrupt:
        print("\nInterrupted. Output and resume map were checkpointed.", file=sys.stderr)
        return 0
    except (OSError, ValueError, MapValidationError) as exc:
        print(f"flvrescue: error: {exc}", file=sys.stderr)
        return 1
    _print_summary(result)
    return 0
