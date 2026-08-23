"""Terminal occupancy bars and status summaries."""

from __future__ import annotations

import re
from collections.abc import Sequence

GREEN = "\x1b[32m"
BLUE = "\x1b[34m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
GRAY = "\x1b[90m"
RESET = "\x1b[0m"

GLYPH_GOOD = "█"
GLYPH_SKIP = "▒"
GLYPH_BAD = "░"
GLYPH_PENDING = " "

KIND_COLOR = {
    "good": GREEN,
    "fast": BLUE,
    "slow": YELLOW,
    "bad": RED,
    "pending": "",
}
KIND_GLYPH = {
    "good": GLYPH_GOOD,
    "fast": GLYPH_SKIP,
    "slow": GLYPH_SKIP,
    "bad": GLYPH_BAD,
    "pending": GLYPH_PENDING,
}
KIND_ORDER = ("good", "fast", "slow", "bad", "pending")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

RangeTuple = tuple[int, int, str, str | None]


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def paint(text: str, color: str, *, enabled: bool) -> str:
    if not enabled or not color:
        return text
    return f"{color}{text}{RESET}"


def range_kind(status: str, cause: str | None) -> str:
    if status == "recovered":
        return "good"
    if status == "unreadable":
        return "bad"
    if status == "skipped":
        return "fast" if cause == "survey" else "slow"
    return "pending"


def tally_kinds(
    ranges: Sequence[RangeTuple],
    total: int,
) -> dict[str, int]:
    counts = {kind: 0 for kind in KIND_ORDER}
    covered = 0
    for offset, length, status, cause in ranges:
        if length <= 0:
            continue
        kind = range_kind(status, cause)
        counts[kind] += length
        covered += length
    if total > covered:
        counts["pending"] += total - covered
    return counts


def _allocate(counts: dict[str, int], width: int) -> list[tuple[str, int]]:
    width = max(1, width)
    total = sum(counts.values())
    if total <= 0:
        return [("pending", width)]
    raw = [(kind, counts[kind] * width / total) for kind in KIND_ORDER]
    sizes = {kind: int(value) for kind, value in raw}
    remainders = sorted(
        ((value - sizes[kind], kind) for kind, value in raw),
        reverse=True,
    )
    missing = width - sum(sizes.values())
    for _, kind in remainders:
        if missing <= 0:
            break
        if counts.get(kind, 0) <= 0:
            continue
        sizes[kind] += 1
        missing -= 1
    return [(kind, sizes[kind]) for kind in KIND_ORDER if sizes[kind] > 0]


def render_stacked_bar(
    counts: dict[str, int],
    width: int,
    *,
    color: bool = False,
) -> str:
    parts: list[str] = []
    for kind, size in _allocate(counts, width):
        glyph = KIND_GLYPH[kind] * size
        parts.append(paint(glyph, KIND_COLOR[kind], enabled=color))
    return "".join(parts)


def cell_kinds(
    ranges: Sequence[RangeTuple],
    total: int,
    count: int,
    *,
    read_offset: int | None = None,
) -> list[str]:
    columns = max(1, count)
    if total <= 0:
        return ["pending"] * columns
    cells: list[str] = []
    for index in range(columns):
        start = index * total // columns
        end = (index + 1) * total // columns
        if end <= start:
            end = start + 1
        if read_offset is not None and start <= read_offset < end:
            cells.append("good")
            continue
        tallies: dict[str, int] = {kind: 0 for kind in KIND_ORDER}
        for offset, length, status, cause in ranges:
            overlap = min(end, offset + length) - max(start, offset)
            if overlap > 0:
                tallies[range_kind(status, cause)] += overlap
        if sum(tallies.values()) == 0:
            winner = "pending"
        else:
            severity = {"pending": 0, "good": 1, "fast": 2, "slow": 3, "bad": 4}
            winner = max(KIND_ORDER, key=lambda kind: (tallies[kind], severity[kind]))
        cells.append(winner)
    return cells


def render_kind_row(kinds: Sequence[str], *, color: bool = False) -> str:
    return "".join(
        paint(KIND_GLYPH[kind], KIND_COLOR[kind], enabled=color) for kind in kinds
    )


def render_spatial_rows(
    ranges: Sequence[RangeTuple],
    total: int,
    *,
    width: int,
    rows: int,
    color: bool = False,
) -> list[str]:
    width = max(8, width)
    rows = max(1, rows)
    kinds = cell_kinds(ranges, total, width * rows)
    return [
        render_kind_row(kinds[row * width : (row + 1) * width], color=color)
        for row in range(rows)
    ]


def format_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes:02}:{seconds:02}"


def format_bytes(value: int) -> str:
    value = max(0, value)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} PiB"


def format_colored_amount(kind: str, value: int, *, color: bool) -> str:
    return paint(format_bytes(value), KIND_COLOR[kind], enabled=color)


def format_stats_line(
    counts: dict[str, int],
    *,
    color: bool = False,
    include_pending: bool = False,
) -> str:
    parts: list[str] = []
    for label, kind in (
        ("good", "good"),
        ("fast", "fast"),
        ("slow", "slow"),
        ("bad", "bad"),
    ):
        name = paint(label, GRAY, enabled=color)
        amount = format_colored_amount(kind, counts.get(kind, 0), color=color)
        parts.append(f"{name} {amount}")
    if include_pending:
        name = paint("todo", GRAY, enabled=color)
        amount = paint(format_bytes(counts.get("pending", 0)), GRAY, enabled=color)
        parts.append(f"{name} {amount}")
    return " ".join(parts)


def format_live_header(name: str, total: int, *, current_pass: int = 1) -> str:
    return f"FLVRESCUE {format_pass_label(current_pass)} reads {name} ({format_bytes(total)})"


def format_live_lines(
    *,
    name: str,
    total: int,
    counts: dict[str, int],
    elapsed: float,
    width: int,
    color: bool,
    current_pass: int = 1,
) -> list[str]:
    header = format_live_header(name, total, current_pass=current_pass)
    suffix = f" {format_bytes(counts.get('good', 0))} {format_clock(elapsed)}"
    bar_width = max(8, width - len(suffix))
    bar = render_stacked_bar(counts, bar_width, color=color)
    return [header, f"{bar}{suffix}", format_stats_line(counts, color=color)]


def format_preparing_lines(
    *,
    name: str,
    total: int,
    elapsed: float,
    spinner: str,
    storage_mode: str | None = None,
    allocated_bytes: int | None = None,
) -> list[str]:
    """Render destination preparation without inventing a percentage complete."""

    lines = [
        f"FLVRESCUE prepares {name}",
        f"Preparing destination {spinner} {format_clock(elapsed)}",
        f"Source extent: {format_bytes(total)}",
    ]
    if storage_mode:
        detail = f"Storage mode: {storage_mode}"
        if allocated_bytes is not None:
            detail += f"  Allocated: {format_bytes(allocated_bytes)}"
        lines.append(detail)
    return lines


PASS_TITLES = {
    1: "Survey",
    2: "Fill",
    3: "Retry",
    4: "Deep",
}


def format_pass_label(pass_number: int) -> str:
    """Return the stable, user-facing label for a recovery pass."""

    title = PASS_TITLES.get(pass_number)
    if title is None:
        return "Complete" if pass_number >= 5 else "Unknown pass"
    return f"Pass {pass_number} {title}"


def format_status_view(
    *,
    name: str,
    total: int,
    counts: dict[str, int],
    ranges: Sequence[RangeTuple],
    current_pass: int,
    map_path: str,
    width: int,
    height: int,
    color: bool,
) -> str:
    width = max(20, width)
    height = max(6, height)
    map_rows = max(1, height - 3)
    rows = render_spatial_rows(
        ranges,
        total,
        width=width,
        rows=map_rows,
        color=color,
    )
    if current_pass >= 5:
        phase = "complete"
    else:
        phase = f"next: {format_pass_label(current_pass)}"
    footer = [
        f"FLVRESCUE status {name} ({format_bytes(total)})  {phase}",
        format_stats_line(counts, color=color, include_pending=True),
        f"map {map_path}",
    ]
    return "\n".join([*rows, *footer]) + "\n"
