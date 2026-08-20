"""Create a small deterministic FLV sample with FFmpeg.

FFmpeg is intentionally optional: this script prints an actionable error when it
is not on PATH, and the corresponding pytest integration test is skipped.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def parse_size(value: str) -> str:
    width, separator, height = value.partition("x")
    if not separator or not width.isdecimal() or not height.isdecimal():
        raise argparse.ArgumentTypeError("size must be WIDTHxHEIGHT, for example 320x180")
    if int(width) <= 0 or int(height) <= 0:
        raise argparse.ArgumentTypeError("width and height must be positive")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a short synthetic FLV with FFmpeg.")
    parser.add_argument("output", type=Path, help="FLV file to create")
    parser.add_argument("--duration", type=float, default=5.0, help="duration in seconds (default: 5)")
    parser.add_argument("--size", type=parse_size, default="320x180", help="frame size (default: 320x180)")
    parser.add_argument("--rate", type=int, default=24, help="frame rate (default: 24)")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.duration <= 0:
        raise SystemExit("--duration must be positive")
    if args.rate <= 0:
        raise SystemExit("--rate must be positive")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("FFmpeg was not found on PATH; install FFmpeg to generate a test FLV.", file=sys.stderr)
        return 2

    output = args.output.expanduser()
    if output.exists() and not args.overwrite:
        print(f"Refusing to overwrite existing file: {output} (pass --overwrite to replace it)", file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if args.overwrite else "-n",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={args.size}:rate={args.rate}",
        "-t",
        str(args.duration),
        "-an",
        "-c:v",
        "flv",
        "-f",
        "flv",
        str(output),
    ]
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as error:
        print(f"FFmpeg failed with exit code {error.returncode}.", file=sys.stderr)
        return error.returncode or 1

    print(f"Created {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
