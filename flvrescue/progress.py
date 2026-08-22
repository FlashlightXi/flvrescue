"""Threaded terminal progress rendering without source-drive I/O."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, TextIO


PASS_NAMES = {1: "Fast rescue", 2: "Skipped recovery", 3: "Deep recovery", 4: "Complete"}
COLORS = {
    "normal": "\x1b[32m",
    "reading": "\x1b[36m",
    "slow": "\x1b[33m",
    "skip": "\x1b[33m",
    "error": "\x1b[31m",
    "complete": "\x1b[32m",
}
RESET = "\x1b[0m"


def _enable_ansi(stream: TextIO) -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_ulong()
        kernel32 = ctypes.windll.kernel32
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        return False


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


def format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


@dataclass(frozen=True)
class ProgressSnapshot:
    current_pass: int = 1
    pass_cursor: int = 0
    total: int = 0
    recovered: int = 0
    skipped: int = 0
    slow: int = 0
    unreadable: int = 0
    status: str = "starting"
    read_offset: int | None = None
    read_size: int = 0
    read_started_at: float | None = None


class ProgressReporter:
    """Render a shared snapshot on a dedicated thread at a fixed cadence."""

    def __init__(
        self,
        total: int,
        *,
        stream: TextIO | None = None,
        update_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if total < 0:
            raise ValueError("total must be non-negative")
        if update_interval <= 0:
            raise ValueError("update_interval must be greater than zero")
        self.total = total
        self.stream = stream if stream is not None else sys.stderr
        self.update_interval = update_interval
        self._clock = clock
        self._started_at = clock()
        self._last_speed_at = self._started_at
        self._last_recovered = 0
        self._speed = 0.0
        self._lock = threading.Lock()
        self._snapshot = ProgressSnapshot(total=total)
        self._events: list[tuple[str, str]] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._closed = False
        isatty = getattr(self.stream, "isatty", None)
        self._tty = bool(callable(isatty) and isatty() and _enable_ansi(self.stream))
        self._live_lines = 0
        self._thread = threading.Thread(
            target=self._render_loop,
            name="flvrescue-progress",
            daemon=True,
        )
        self._thread.start()

    def update(
        self,
        *,
        current_pass: int,
        pass_cursor: int,
        recovered: int,
        skipped: int,
        slow: int,
        unreadable: int,
        status: str,
        force: bool = False,
    ) -> None:
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                current_pass=current_pass,
                pass_cursor=pass_cursor,
                recovered=recovered,
                skipped=skipped,
                slow=slow,
                unreadable=unreadable,
                status=status,
            )
        if force:
            self._wake.set()

    def begin_read(self, offset: int, size: int, *, status: str = "reading") -> None:
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                status=status,
                read_offset=offset,
                read_size=size,
                read_started_at=self._clock(),
            )

    def end_read(self, *, status: str) -> None:
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                status=status,
                read_offset=None,
                read_size=0,
                read_started_at=None,
            )

    def event(self, kind: str, message: str) -> None:
        with self._lock:
            self._events.append((kind, message))
        self._wake.set()

    def _snapshot_and_events(self) -> tuple[ProgressSnapshot, list[tuple[str, str]]]:
        with self._lock:
            snapshot = self._snapshot
            events = self._events
            self._events = []
        return snapshot, events

    def _color(self, text: str, kind: str) -> str:
        if not self._tty:
            return text
        return f"{COLORS.get(kind, '')}{text}{RESET}"

    def _truncate(self, text: str) -> str:
        width = max(40, shutil.get_terminal_size(fallback=(100, 24)).columns)
        if len(text) <= width:
            return text
        return text[: max(1, width - 1)] + "…"

    def _lines(self, snapshot: ProgressSnapshot, now: float) -> list[str]:
        elapsed = max(now - self._started_at, 0.0)
        percentage = 100.0 if snapshot.total == 0 else snapshot.pass_cursor / snapshot.total * 100
        first = self._truncate(
            f"Pass {snapshot.current_pass} {PASS_NAMES.get(snapshot.current_pass, '')} | "
            f"Elapsed {format_elapsed(elapsed)} | Progress {percentage:5.1f}% | {snapshot.status}"
        )
        slow_skipped = snapshot.slow + snapshot.skipped
        second = self._truncate(
            f"Speed {format_bytes(int(self._speed))}/s | Recovered {format_bytes(snapshot.recovered)} "
            f"| Slow/Skipped {format_bytes(slow_skipped)} | Unreadable {format_bytes(snapshot.unreadable)}"
        )
        if snapshot.read_offset is None or snapshot.read_started_at is None:
            third = "Read: idle"
        else:
            waiting = max(0.0, now - snapshot.read_started_at)
            third = self._truncate(
                f"Read: offset {snapshot.read_offset} + {format_bytes(snapshot.read_size)} "
                f"| waiting {waiting:.1f}s"
            )
        kind = snapshot.status if snapshot.status in COLORS else "normal"
        return [self._color(first, kind), second, third]

    def _render(self, *, final: bool = False) -> None:
        now = self._clock()
        snapshot, events = self._snapshot_and_events()
        speed_interval = max(now - self._last_speed_at, 1e-9)
        if final or speed_interval >= self.update_interval * 0.8:
            self._speed = max(0, snapshot.recovered - self._last_recovered) / speed_interval
            self._last_recovered = snapshot.recovered
            self._last_speed_at = now
        if self._tty:
            if self._live_lines:
                self.stream.write(f"\x1b[{self._live_lines}A")
                for _ in range(self._live_lines):
                    self.stream.write("\r\x1b[2K\n")
                self.stream.write(f"\x1b[{self._live_lines}A")
            for kind, message in events:
                self.stream.write(self._color(self._truncate(message), kind) + "\n")
            lines = self._lines(snapshot, now)
            for line in lines:
                self.stream.write("\r\x1b[2K" + line + "\n")
            self._live_lines = len(lines)
        else:
            for kind, message in events:
                self.stream.write(f"[{kind}] {message}\n")
            line = self._lines(snapshot, now)[0]
            detail = self._lines(snapshot, now)[1]
            self.stream.write(f"{line} | {detail}\n")
        self.stream.flush()

    def _render_loop(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.wait(self.update_interval)
                self._wake.clear()
                if self._stop.is_set():
                    break
                self._render()
        except (OSError, ValueError):
            return

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=max(2.0, self.update_interval * 2))
        try:
            self._render(final=True)
        except (OSError, ValueError):
            pass
