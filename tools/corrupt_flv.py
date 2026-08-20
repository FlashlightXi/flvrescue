"""Produce reproducible, intentionally damaged FLV copies for FFmpeg experiments."""

from __future__ import annotations

import argparse
import random
import shutil
import sys
from pathlib import Path


ZERO_4K = 4 * 1024
ZERO_64K = 64 * 1024


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy an FLV, then apply one controlled corruption to the copy."
    )
    parser.add_argument("source", type=Path, help="input FLV; it is never modified")
    parser.add_argument("destination", type=Path, help="new corrupted FLV")
    parser.add_argument(
        "--mode",
        required=True,
        choices=("zero-4k", "zero-64k", "random", "truncate", "multiple"),
        help="corruption scenario",
    )
    parser.add_argument("--offset", type=nonnegative_int, help="byte offset (default: centered)")
    parser.add_argument("--seed", type=int, default=0, help="seed for --mode random (default: 0)")
    parser.add_argument(
        "--random-length",
        type=nonnegative_int,
        default=ZERO_4K,
        help="bytes to overwrite in random mode (default: 4096)",
    )
    parser.add_argument(
        "--truncate-bytes",
        type=nonnegative_int,
        default=ZERO_64K,
        help="bytes to remove from the end in truncate mode (default: 65536)",
    )
    parser.add_argument("--overwrite", action="store_true", help="replace an existing destination")
    return parser


def centered_offset(file_size: int, length: int) -> int:
    return max(0, (file_size - length) // 2)


def bounded_span(file_size: int, offset: int, length: int) -> tuple[int, int]:
    if offset > file_size:
        raise ValueError(f"offset {offset} is beyond the {file_size}-byte source")
    return offset, min(length, file_size - offset)


def write_zeros(handle, offset: int, length: int) -> None:
    handle.seek(offset)
    handle.write(b"\0" * length)


def corrupt(destination: Path, mode: str, *, offset: int | None, seed: int, random_length: int, truncate_bytes: int) -> None:
    file_size = destination.stat().st_size
    if mode == "truncate":
        new_size = max(0, file_size - truncate_bytes)
        with destination.open("r+b") as handle:
            handle.truncate(new_size)
        return

    if mode == "zero-4k":
        start, length = bounded_span(file_size, offset if offset is not None else centered_offset(file_size, ZERO_4K), ZERO_4K)
        with destination.open("r+b") as handle:
            write_zeros(handle, start, length)
        return

    if mode == "zero-64k":
        start, length = bounded_span(file_size, offset if offset is not None else centered_offset(file_size, ZERO_64K), ZERO_64K)
        with destination.open("r+b") as handle:
            write_zeros(handle, start, length)
        return

    if mode == "random":
        start, length = bounded_span(
            file_size,
            offset if offset is not None else centered_offset(file_size, random_length),
            random_length,
        )
        generator = random.Random(seed)
        random_bytes = generator.randbytes(length)
        with destination.open("r+b") as handle:
            handle.seek(start)
            handle.write(random_bytes)
        return

    # Spread three 4 KiB zero regions through the file; short files receive
    # bounded writes at the same deterministic positions.
    positions = [file_size // 5, file_size // 2, (file_size * 4) // 5]
    with destination.open("r+b") as handle:
        for start in positions:
            start, length = bounded_span(file_size, start, ZERO_4K)
            write_zeros(handle, start, length)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = args.source.expanduser()
    destination = args.destination.expanduser()
    if not source.is_file():
        print(f"Source is not a file: {source}", file=sys.stderr)
        return 2
    if source.resolve() == destination.resolve():
        print("Source and destination must be different files.", file=sys.stderr)
        return 2
    if destination.exists() and not args.overwrite:
        print(f"Refusing to overwrite existing file: {destination} (pass --overwrite to replace it)", file=sys.stderr)
        return 2

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    try:
        corrupt(
            destination,
            args.mode,
            offset=args.offset,
            seed=args.seed,
            random_length=args.random_length,
            truncate_bytes=args.truncate_bytes,
        )
    except ValueError as error:
        destination.unlink(missing_ok=True)
        print(str(error), file=sys.stderr)
        return 2

    print(f"Created {destination} using {args.mode} ({destination.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
