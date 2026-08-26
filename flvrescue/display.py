"""Terminal occupancy bars and status summaries."""

from __future__ import annotations

import re
from collections.abc import Sequence

GREEN = "\x1b[32m"
BLUE = "\x1b[34m"
YELLOW = "\x1b[33m"
MAGENTA = "\x1b[35m"
RED = "\x1b[31m"
GRAY = "\x1b[90m"
RESET = "\x1b[0m"

GLYPH_GOOD = "█"
GLYPH_FAST = "▓"
GLYPH_SLOW = "▒"
GLYPH_HARD = "░"
GLYPH_BAD = "×"
GLYPH_PENDING = "·"
GLYPH_CURRENT = "▶"
GLYPH_SKIP = GLYPH_SLOW

KIND_COLOR = {
    "good": GREEN,
    "fast": BLUE,
    "slow": YELLOW,
    "hard": MAGENTA,
    "bad": RED,
    "pending": GRAY,
    "current": "\x1b[96m",
}
KIND_GLYPH = {
    "good": GLYPH_GOOD,
    "fast": GLYPH_FAST,
    "slow": GLYPH_SLOW,
    "hard": GLYPH_HARD,
    "bad": GLYPH_BAD,
    "pending": GLYPH_PENDING,
    "current": GLYPH_CURRENT,
}
KIND_ORDER = ("good", "fast", "slow", "hard", "bad", "pending")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

RangeTuple = (
    tuple[int, int, str, str | None]
    | tuple[int, int, str, str | None, str | None]
)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def visual_line_count(lines: Sequence[str], width: int) -> int:
    """Count terminal rows after wrapping, so live redraw does not erase log lines."""

    columns = max(1, width)
    rows = 0
    for line in lines:
        visible = len(strip_ansi(line))
        rows += 1 if visible <= 0 else (visible + columns - 1) // columns
    return rows


def paint(text: str, color: str, *, enabled: bool) -> str:
    if not enabled or not color:
        return text
    return f"{color}{text}{RESET}"


def range_kind(
    status: str, cause: str | None, difficulty: str | None = None
) -> str:
    if status == "recovered":
        measured = difficulty or cause
        if measured in ("slow", "hard"):
            return measured
        return "good"
    if status == "unreadable" or difficulty == "failure":
        return "bad"
    if status == "skipped":
        measured = difficulty or cause
        if measured in ("slow", "hard"):
            return measured
        if measured in ("failure", "read_error"):
            return "bad"
        return "fast" if cause in ("survey", "probe") else "pending"
    return "pending"


def _range_parts(item: RangeTuple) -> tuple[int, int, str, str | None, str | None]:
    if len(item) == 4:
        offset, length, status, cause = item
        return offset, length, status, cause, None
    return item


def tally_kinds(
    ranges: Sequence[RangeTuple],
    total: int,
) -> dict[str, int]:
    counts = {kind: 0 for kind in KIND_ORDER}
    covered = 0
    for item in ranges:
        offset, length, status, cause, difficulty = _range_parts(item)
        if length <= 0:
            continue
        kind = range_kind(status, cause, difficulty)
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
    raw = [(kind, counts.get(kind, 0) * width / total) for kind in KIND_ORDER]
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
            cells.append("current")
            continue
        tallies: dict[str, int] = {kind: 0 for kind in KIND_ORDER}
        for item in ranges:
            offset, length, status, cause, difficulty = _range_parts(item)
            overlap = min(end, offset + length) - max(start, offset)
            if overlap > 0:
                tallies[range_kind(status, cause, difficulty)] += overlap
        if sum(tallies.values()) == 0:
            winner = "pending"
        else:
            severity = {
                "pending": 0,
                "good": 1,
                "fast": 2,
                "slow": 3,
                "hard": 4,
                "bad": 5,
            }
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


def format_read_clock(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    remainder = seconds % 60
    if hours:
        return f"{hours}:{minutes:02}:{remainder:04.1f}"
    return f"{minutes:02}:{remainder:04.1f}"


def format_gib_offset(value: int, *, decimals: int = 2) -> str:
    return f"{value / 1024**3:.{decimals}f} GiB"


def local_window(
    total: int,
    read_offset: int,
    section_start: int | None,
    section_end: int | None,
    *,
    target_span: int = 512 * 1024 * 1024,
) -> tuple[int, int]:
    if total <= 0:
        return 0, 0
    target_span = min(max(1, target_span), total)
    start = max(0, section_start if section_start is not None else read_offset)
    end = min(total, section_end if section_end is not None else read_offset + 1)
    if end <= start:
        start, end = read_offset, min(total, read_offset + 1)
    if end - start <= target_span:
        middle = (start + end) // 2
    else:
        middle = read_offset
    window_start = max(0, middle - target_span // 2)
    window_end = min(total, window_start + target_span)
    window_start = max(0, window_end - target_span)
    return window_start, window_end


def cell_kinds_window(
    ranges: Sequence[RangeTuple],
    start: int,
    end: int,
    count: int,
    *,
    read_offset: int,
) -> tuple[list[str], int]:
    columns = max(1, count)
    span = max(1, end - start)
    cells: list[str] = []
    marker = min(columns - 1, max(0, (read_offset - start) * columns // span))
    for index in range(columns):
        if index == marker:
            cells.append("current")
            continue
        cell_start = start + index * span // columns
        cell_end = start + (index + 1) * span // columns
        tallies = {kind: 0 for kind in KIND_ORDER}
        for item in ranges:
            offset, length, status, cause, difficulty = _range_parts(item)
            overlap = min(cell_end, offset + length) - max(cell_start, offset)
            if overlap > 0:
                tallies[range_kind(status, cause, difficulty)] += overlap
        if sum(tallies.values()) == 0:
            cells.append("pending")
        else:
            severity = {
                "pending": 0,
                "good": 1,
                "fast": 2,
                "slow": 3,
                "hard": 4,
                "bad": 5,
            }
            cells.append(max(KIND_ORDER, key=lambda kind: (tallies[kind], severity[kind])))
    return cells, marker


def format_local_read_lines(
    *,
    ranges: Sequence[RangeTuple],
    total: int,
    width: int,
    read_offset: int,
    read_size: int,
    read_elapsed: float,
    read_status: str,
    section_start: int | None,
    section_end: int | None,
    color: bool,
) -> list[str]:
    start, end = local_window(total, read_offset, section_start, section_end)
    left = format_gib_offset(start, decimals=2)
    right = format_gib_offset(end, decimals=2)
    bar_width = max(8, width - len(left) - len(right) - 4)
    kinds, marker = cell_kinds_window(
        ranges, start, end, bar_width, read_offset=read_offset
    )
    bar = render_kind_row(kinds, color=color)
    marker_column = len(left) + 2 + marker
    normalized = read_status.upper()
    status_color = {
        "READING": "\x1b[36m",
        "SLOW": YELLOW,
        "HARD": MAGENTA,
        "CANCEL REQUESTED": MAGENTA,
        "CANCELLED": YELLOW,
        "CANCEL PENDING": RED,
    }.get(normalized, GRAY)
    detail = (
        f"↓ {format_gib_offset(read_offset, decimals=3)} "
        f"({format_bytes(read_size)})  "
        f"{paint(normalized, status_color, enabled=color)} {format_read_clock(read_elapsed)}"
    )
    detail_line = " " * marker_column + detail
    return [detail_line, f"{left}  {bar}  {right}"]


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
        ("hard", "hard"),
        ("bad", "bad"),
    ):
        name = paint(label, GRAY, enabled=color)
        amount = format_colored_amount(kind, counts.get(kind, 0), color=color)
        parts.append(f"{name} {amount}")
    if include_pending:
        name = paint("remaining", GRAY, enabled=color)
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
    recovered: int | None = None,
    ranges: Sequence[RangeTuple] = (),
    read_offset: int | None = None,
    read_size: int = 0,
    read_elapsed: float = 0.0,
    read_status: str = "reading",
    section_start: int | None = None,
    section_end: int | None = None,
) -> list[str]:
    recovered_amount = counts.get("good", 0) if recovered is None else recovered
    suffix = f" {format_bytes(recovered_amount)} {format_clock(elapsed)}"
    bar_width = max(8, width - len(suffix))
    bar = render_stacked_bar(counts, bar_width, color=color)
    lines = [f"{bar}{suffix}", format_stats_line(counts, color=color)]
    if read_offset is not None:
        lines.extend(
            format_local_read_lines(
                ranges=ranges,
                total=total,
                width=width,
                read_offset=read_offset,
                read_size=read_size,
                read_elapsed=read_elapsed,
                read_status=read_status,
                section_start=section_start,
                section_end=section_end,
                color=color,
            )
        )
    return lines


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
    2: "Fast",
    3: "Slow",
    4: "Hard",
    5: "Deep",
}


def format_pass_label(pass_number: int) -> str:
    """Return the stable, user-facing label for a recovery pass."""

    title = PASS_TITLES.get(pass_number)
    if title is None:
        return "Complete" if pass_number >= 6 else "Unknown pass"
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
    if current_pass >= 6:
        phase = "complete"
    else:
        phase = f"next: {format_pass_label(current_pass)}"
    footer = [
        f"FLVRESCUE status {name} ({format_bytes(total)})  {phase}",
        format_stats_line(counts, color=color, include_pending=True),
        f"map {map_path}",
    ]
    return "\n".join([*rows, *footer]) + "\n"
