"""Command-line interface for flvrescue."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence

from .mapfile import MapValidationError
from .progress import format_bytes
from .rescue import (
    DEFAULT_BLOCK_SIZE,
    DEFAULT_FALLBACK_SIZE,
    DEFAULT_SECTOR_SIZE,
    RescueResult,
    rescue,
)


_SIZE_RE = re.compile(r"^(?P<number>[0-9]+)\s*(?P<unit>[kKmMgG]?)(?:i?[bB])?$")


def parse_size(value: str) -> int:
    """Parse byte counts and the CLI-friendly K/M/G forms (for example ``8M``)."""

    match = _SIZE_RE.fullmatch(value.strip())
    if match is None:
        raise argparse.ArgumentTypeError(
            "size must be a positive byte count or use a K, M, or G suffix"
        )
    number = int(match.group("number"))
    unit = match.group("unit").upper()
    multiplier = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[unit]
    size = number * multiplier
    if size <= 0:
        raise argparse.ArgumentTypeError("size must be greater than zero")
    return size


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flvrescue",
        description="Copy readable portions of one file while zero-filling unreadable ranges.",
    )
    parser.add_argument("source", help="read-only source file on the failing drive")
    parser.add_argument("destination", help="new rescue output file")
    parser.add_argument("--map", dest="map_path", help="resume sidecar JSON path")
    parser.add_argument(
        "--block",
        type=parse_size,
        default=DEFAULT_BLOCK_SIZE,
        help="normal read size (default: 8M)",
    )
    parser.add_argument(
        "--fallback",
        type=parse_size,
        default=DEFAULT_FALLBACK_SIZE,
        help="read size after a block error (default: 64K)",
    )
    parser.add_argument(
        "--sector",
        type=parse_size,
        default=DEFAULT_SECTOR_SIZE,
        help="zero-fill unit after a fallback error (default: 4K)",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="suppress periodic progress output",
    )
    return parser


def _print_summary(result: RescueResult) -> None:
    print("\nRescue completed.")
    print(f"Target:      {result.destination}")
    print(f"Recovered:   {format_bytes(result.recovered_bytes)}")
    print(f"Unreadable:  {format_bytes(result.unreadable_bytes)}")
    print(f"Bad ranges:  {len(result.bad_ranges)}")
    print(f"Resume map:  {result.map_path}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.block < args.fallback or args.fallback < args.sector:
        parser.error("--block >= --fallback >= --sector is required")
    try:
        result = rescue(
            args.source,
            args.destination,
            map_path=args.map_path,
            progress=not args.no_progress,
            block_size=args.block,
            fallback_size=args.fallback,
            sector_size=args.sector,
        )
    except KeyboardInterrupt:
        print("\nInterrupted. Output and resume map were checkpointed.", file=sys.stderr)
        return 0
    except (OSError, ValueError, MapValidationError) as exc:
        print(f"flvrescue: error: {exc}", file=sys.stderr)
        return 1
    _print_summary(result)
    return 0
