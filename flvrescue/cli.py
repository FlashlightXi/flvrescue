"""Command-line interface for flvrescue."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from .display import format_bytes, format_pass_label, format_status_view, tally_kinds
from .mapfile import MapValidationError, load_map
from .optimize import OptimizationPreference, OptimizationRecommendation, recommend_policy
from .policy import DEFAULT_POLICY, PASS_NAME_TO_NUMBER, PASS_NUMBER_TO_NAME
from .rescue import RescueResult, rescue
from .version import __version__


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


PASS_THROUGH_METAVAR = "{survey,fill,retry,deep,1,2,3,4}"


def parse_through(value: str) -> int:
    """Parse a named recovery stage or its backward-compatible pass number."""

    normalized = value.strip().lower()
    if normalized in PASS_NAME_TO_NUMBER:
        return PASS_NAME_TO_NUMBER[normalized]
    try:
        number = int(normalized, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "through must be survey, fill, retry, deep, or 1 through 4"
        ) from exc
    if number not in PASS_NUMBER_TO_NAME:
        raise argparse.ArgumentTypeError(
            "through must be survey, fill, retry, deep, or 1 through 4"
        )
    return number


def _add_through_options(parser: argparse.ArgumentParser) -> None:
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--through",
        type=parse_through,
        choices=tuple(PASS_NUMBER_TO_NAME),
        metavar=PASS_THROUGH_METAVAR,
        help=(
            "last recovery stage: survey (whole-file coverage), fill (likely-good "
            "gaps), retry (slow/error gaps), or deep (fine-grained recovery); "
            "default: fill"
        ),
    )
    selection.add_argument(
        "--max-pass",
        type=int,
        choices=tuple(PASS_NUMBER_TO_NAME),
        help="legacy numeric alias for --through; cannot be combined with --through",
    )


def _selected_max_pass(args: argparse.Namespace) -> int:
    if args.through is not None:
        return args.through
    if args.max_pass is not None:
        return args.max_pass
    return PASS_NAME_TO_NUMBER["fill"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flvrescue",
        description=(
            "Run Survey for whole-file coverage, then Fill likely-good gaps; "
            "Retry and Deep are opt-in later stages. Sizes accept bytes or K/M/G/T suffixes "
            "(1024-based)."
        ),
        epilog=(
            "Inspect a saved map without reading the source drive: "
            "flvrescue status FILE\n"
            "Resume from the saved source and destination paths: flvrescue resume FILE\n"
            "Print a non-mutating recommendation: flvrescue optimize SOURCE DEST"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("source", help="read-only source file on the failing drive")
    parser.add_argument("destination", help="new rescue output file")
    parser.add_argument("--map", dest="map_path", help="resume sidecar JSON path")
    parser.add_argument(
        "--block",
        type=parse_size,
        default=None,
        metavar="SIZE",
        help="advanced override for normal read size (normally use the saved policy)",
    )
    parser.add_argument(
        "--fallback",
        type=parse_size,
        default=None,
        metavar="SIZE",
        help="advanced override for read size after a block error",
    )
    parser.add_argument(
        "--sector",
        type=parse_size,
        default=None,
        metavar="SIZE",
        help="advanced override for final read and zero-fill unit",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="suppress periodic progress output",
    )
    parser.add_argument(
        "--slow-threshold",
        type=float,
        default=None,
        metavar="SECONDS",
        help="advanced override for successful read duration treated as slow",
    )
    parser.add_argument(
        "--checkpoint",
        type=parse_size,
        default=None,
        metavar="SIZE",
        help="advanced override for bytes written between durable checkpoints",
    )
    parser.add_argument(
        "--skip-start",
        type=parse_size,
        default=None,
        metavar="SIZE",
        help="advanced override for initial adaptive skip width",
    )
    parser.add_argument(
        "--skip-factor",
        type=parse_positive_int,
        default=None,
        metavar="N",
        help="advanced override for adaptive skip multiplier",
    )
    parser.add_argument(
        "--skip-max",
        type=parse_size,
        default=None,
        metavar="SIZE",
        help="advanced override for maximum adaptive skip width",
    )
    parser.add_argument(
        "--skip-reset-after",
        type=parse_positive_int,
        default=None,
        metavar="N",
        help="advanced override for fast reads required to leave skip mode",
    )
    parser.add_argument(
        "--survey-stride",
        type=parse_nonnegative_size,
        default=None,
        metavar="SIZE",
        help=(
            "advanced override for Pass 1 sample stride; 0 disables sampling"
        ),
    )
    _add_through_options(parser)
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


def _canonical_path(path: Path) -> Path:
    return path.expanduser().resolve()


def _same_canonical_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def resolve_resume_map_path(path: Path, *, explicit: Path | None = None) -> tuple[Path, Path | None]:
    """Resolve a resume target without status's permissive map discovery.

    A map argument names that exact map.  A destination argument permits only
    its adjacent ``DEST.rescue.json`` map (or a map explicitly supplied with
    ``--map``); it never searches or falls back to another destination.
    """

    target = _canonical_path(path)
    if target.suffix.lower() == ".json":
        if explicit is not None:
            raise ValueError("FILE is already a map; do not also pass --map")
        return target, None
    map_path = (
        _canonical_path(explicit)
        if explicit is not None
        else _canonical_path(Path(str(target) + ".rescue.json"))
    )
    if not map_path.is_file():
        if explicit is not None:
            raise FileNotFoundError(f"explicit resume map does not exist: {map_path}")
        raise FileNotFoundError(f"no adjacent rescue map found for destination {target}")
    return map_path, target


def build_status_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flvrescue status",
        description="Render the occupancy map for a rescue output or map file.",
    )
    parser.add_argument("path", help="rescue destination or .rescue.json map")
    parser.add_argument("--map", dest="map_path", help="explicit resume map path")
    return parser


def build_resume_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flvrescue resume",
        description="Resume a saved rescue map without repeating source or policy options.",
    )
    parser.add_argument("path", help="rescue destination or .rescue.json map")
    parser.add_argument("--map", dest="map_path", help="explicit resume map path")
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="suppress periodic progress output",
    )
    _add_through_options(parser)
    return parser


def build_optimize_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flvrescue optimize",
        description=(
            "Recommend a recovery command from file size, free space, and one "
            "preference. Source contents are not read and rescue is not started."
        ),
    )
    parser.add_argument("source", help="source file whose size should be inspected")
    parser.add_argument("destination", help="intended rescue output path")
    parser.add_argument(
        "--preference",
        choices=("fast", "balanced", "thorough"),
        help="fast coverage, balanced default, or more recovery effort",
    )
    parser.add_argument(
        "--available-space",
        type=parse_size,
        metavar="SIZE",
        help="override automatically detected destination free space",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="never prompt; use balanced when --preference is omitted",
    )
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


def _print_summary(result: RescueResult, *, through: int) -> None:
    print("\nRescue run completed.")
    print(f"Target:           {result.destination}")
    print(f"Recovered:        {format_bytes(result.recovered_bytes)}")
    print(f"Likely-good skip: {format_bytes(result.easy_skipped_bytes)}")
    print(f"Slow/error skip:  {format_bytes(result.hard_skipped_bytes)}")
    print(f"Unreadable:       {format_bytes(result.unreadable_bytes)}")
    print(f"Unprocessed:      {format_bytes(result.unprocessed_bytes)}")
    print(f"Completed through: {format_pass_label(through)}")
    if result.current_pass <= 4:
        print(f"Next pass:        {format_pass_label(result.current_pass)}")
    print(f"Resume map:       {result.map_path}")


def _validate_advanced_options(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.block is not None and args.fallback is not None and args.block < args.fallback:
        parser.error("--block must not be smaller than --fallback")
    if args.fallback is not None and args.sector is not None and args.fallback < args.sector:
        parser.error("--fallback must not be smaller than --sector")
    if args.slow_threshold is not None and args.slow_threshold <= 0:
        parser.error("--slow-threshold must be greater than zero")
    if (
        args.skip_start is not None
        and args.skip_max is not None
        and args.skip_start > args.skip_max
    ):
        parser.error("--skip-start must not exceed --skip-max")


def _advanced_rescue_kwargs(args: argparse.Namespace) -> dict[str, int | float | None]:
    return {
        "block_size": args.block,
        "fallback_size": args.fallback,
        "sector_size": args.sector,
        "checkpoint_interval": args.checkpoint,
        "slow_threshold": args.slow_threshold,
        "skip_start": args.skip_start,
        "skip_factor": args.skip_factor,
        "skip_max": args.skip_max,
        "skip_reset_after": args.skip_reset_after,
        "survey_stride": args.survey_stride,
    }


def _choose_optimization_preference(
    selected: str | None, *, allow_input: bool
) -> OptimizationPreference:
    if selected is not None:
        return selected  # type: ignore[return-value]
    if not allow_input or not sys.stdin.isatty():
        return "balanced"
    print("Recovery preference:")
    print("  1) fast      - cover selected files first")
    print("  2) balanced  - Survey then likely-readable Fill [default]")
    print("  3) thorough  - smaller gaps and Retry when capacity permits")
    answer = input("Choose 1-3 [2]: ").strip().lower()
    return {
        "1": "fast",
        "fast": "fast",
        "2": "balanced",
        "": "balanced",
        "balanced": "balanced",
        "3": "thorough",
        "thorough": "thorough",
    }.get(answer, "balanced")  # type: ignore[return-value]


def _optimization_command(
    source: Path,
    destination: Path,
    recommendation: OptimizationRecommendation,
) -> str:
    def powershell_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def size_argument(value: int) -> str:
        for suffix, multiplier in (
            ("T", 1024**4),
            ("G", 1024**3),
            ("M", 1024**2),
            ("K", 1024),
        ):
            if value >= multiplier and value % multiplier == 0:
                return f"{value // multiplier}{suffix}"
        return str(value)

    policy = recommendation.policy
    arguments = ["flvrescue", powershell_quote(str(source)), powershell_quote(str(destination))]
    size_options = (
        ("--block", "block"),
        ("--fallback", "fallback"),
        ("--sector", "sector"),
        ("--checkpoint", "checkpoint"),
        ("--skip-start", "skip_start"),
        ("--skip-max", "skip_max"),
        ("--survey-stride", "survey_stride"),
    )
    for option, field in size_options:
        value = getattr(policy, field)
        if value != getattr(DEFAULT_POLICY, field):
            arguments.extend((option, size_argument(value)))
    for option, field in (
        ("--skip-factor", "skip_factor"),
        ("--skip-reset-after", "skip_reset_after"),
    ):
        value = getattr(policy, field)
        if value != getattr(DEFAULT_POLICY, field):
            arguments.extend((option, str(value)))
    if policy.slow_threshold != DEFAULT_POLICY.slow_threshold:
        arguments.extend(("--slow-threshold", str(policy.slow_threshold)))
    arguments.extend(("--through", recommendation.through))
    return " ".join(arguments)


def run_optimize(argv: Sequence[str] | None = None) -> int:
    parser = build_optimize_parser()
    args = parser.parse_args(argv)
    source = Path(args.source).expanduser().resolve()
    destination = Path(args.destination).expanduser().resolve()
    try:
        if not source.is_file():
            raise FileNotFoundError(f"source file does not exist: {source}")
        if os.path.normcase(str(source)) == os.path.normcase(str(destination)):
            raise ValueError("source and destination must be different files")
        if destination.exists():
            raise FileExistsError(
                "destination already exists; use flvrescue resume for an existing rescue"
            )
        destination_parent = destination.parent
        if not destination_parent.is_dir():
            raise FileNotFoundError(
                f"destination directory does not exist: {destination_parent}"
            )
        preference = _choose_optimization_preference(
            args.preference, allow_input=not args.no_input
        )
        available_space = (
            args.available_space
            if args.available_space is not None
            else shutil.disk_usage(destination_parent).free
        )
        recommendation = recommend_policy(
            source.stat().st_size,
            preference=preference,
            available_space=available_space,
        )
    except (OSError, ValueError) as exc:
        print(f"flvrescue: error: {exc}", file=sys.stderr)
        return 1

    print("FLVRESCUE optimization recommendation")
    print(f"Preference:       {recommendation.preference}")
    print(f"Source size:      {format_bytes(recommendation.source_size)}")
    print(f"Destination free: {format_bytes(available_space)}")
    print(
        "Estimated Survey writes: "
        f"{format_bytes(recommendation.estimated_survey_bytes)}"
    )
    print(f"Suggested stage:  {recommendation.through}")
    print(f"Survey stride:    {format_bytes(recommendation.policy.survey_stride)}")
    print(f"Adaptive skip:    {format_bytes(recommendation.policy.skip_start)}")
    for warning in recommendation.warnings:
        print(f"Warning: {warning}")
    if not recommendation.runnable:
        print(
            "No command generated: at least "
            f"{format_bytes(recommendation.minimum_space)} free is recommended for Survey."
        )
        return 2
    print("\nRecommended command:")
    print(_optimization_command(source, destination, recommendation))
    print("\nThis is a heuristic only; it did not read source contents or start recovery.")
    return 0


def run_resume(argv: Sequence[str] | None = None) -> int:
    parser = build_resume_parser()
    args = parser.parse_args(argv)
    through = _selected_max_pass(args)
    try:
        map_path, requested_destination = resolve_resume_map_path(
            Path(args.path),
            explicit=Path(args.map_path) if args.map_path else None,
        )
        state = load_map(map_path)
        saved_destination = _canonical_path(Path(state.destination_path))
        if (
            requested_destination is not None
            and not _same_canonical_path(requested_destination, saved_destination)
        ):
            raise MapValidationError(
                "resume destination does not match destination_path stored in the map"
            )
        policy = state.policy
        if policy is None:
            print(
                "flvrescue: note: legacy map has no saved policy; adopting the current coverage policy.",
                file=sys.stderr,
            )
            policy = DEFAULT_POLICY
        result = rescue(
            state.source_path,
            state.destination_path,
            map_path=map_path,
            progress=not args.no_progress,
            policy=policy,
            max_pass=through,
        )
    except KeyboardInterrupt:
        print("\nInterrupted. Output and resume map were checkpointed.", file=sys.stderr)
        return 0
    except (OSError, ValueError, MapValidationError) as exc:
        print(f"flvrescue: error: {exc}", file=sys.stderr)
        return 1
    _print_summary(result, through=through)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    if argv_list and argv_list[0] in {"status", "analyze"}:
        return run_status(argv_list[1:])
    if argv_list and argv_list[0] == "resume":
        return run_resume(argv_list[1:])
    if argv_list and argv_list[0] == "optimize":
        return run_optimize(argv_list[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_advanced_options(args, parser)
    through = _selected_max_pass(args)
    try:
        result = rescue(
            args.source,
            args.destination,
            map_path=args.map_path,
            progress=not args.no_progress,
            # Let the engine adopt DEFAULT_POLICY for a new run or the saved
            # policy for a repeated-command resume.
            policy=None,
            max_pass=through,
            **_advanced_rescue_kwargs(args),
        )
    except KeyboardInterrupt:
        print("\nInterrupted. Output and resume map were checkpointed.", file=sys.stderr)
        return 0
    except (OSError, ValueError, MapValidationError) as exc:
        print(f"flvrescue: error: {exc}", file=sys.stderr)
        return 1
    _print_summary(result, through=through)
    return 0
