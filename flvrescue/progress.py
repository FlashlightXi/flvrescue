"""Threaded terminal progress rendering without source-drive I/O."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, TextIO

from .display import (
    format_live_header,
    format_live_lines,
    format_preparing_lines,
    tally_kinds,
    visual_line_count,
)


COLORS = {
    "normal": "\x1b[32m",
    "reading": "\x1b[36m",
    "slow": "\x1b[33m",
    "hard": "\x1b[35m",
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


@dataclass(frozen=True)
class ProgressSnapshot:
    current_pass: int = 1
    pass_cursor: int = 0
    total: int = 0
    recovered: int = 0
    skipped: int = 0
    slow: int = 0
    unreadable: int = 0
    easy_skipped: int = 0
    hard_skipped: int = 0
    ranges: tuple[
        tuple[int, int, str, str | None]
        | tuple[int, int, str, str | None, str | None],
        ...,
    ] = ()
    status: str = "starting"
    read_offset: int | None = None
    read_size: int = 0
    read_started_at: float | None = None
    read_frozen_elapsed: float | None = None
    section_start: int | None = None
    section_end: int | None = None
    slow_threshold: float = 2.0
    hard_threshold: float = 10.0
    storage_mode: str | None = None
    allocated_bytes: int | None = None


class ProgressReporter:
    """Render a shared snapshot on a dedicated thread at a fixed cadence."""

    def __init__(
        self,
        total: int,
        *,
        label: str = "file",
        destination_label: str | None = None,
        stream: TextIO | None = None,
        update_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if total < 0:
            raise ValueError("total must be non-negative")
        if update_interval <= 0:
            raise ValueError("update_interval must be greater than zero")
        self.total = total
        self.label = label
        self.destination_label = destination_label or label
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
        easy_skipped: int = 0,
        hard_skipped: int = 0,
        ranges: tuple[
            tuple[int, int, str, str | None]
            | tuple[int, int, str, str | None, str | None],
            ...,
        ] = (),
        storage_mode: str | None = None,
        allocated_bytes: int | None = None,
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
                easy_skipped=easy_skipped,
                hard_skipped=hard_skipped,
                ranges=ranges,
                status=status,
                storage_mode=(
                    storage_mode
                    if storage_mode is not None
                    else self._snapshot.storage_mode
                ),
                allocated_bytes=(
                    allocated_bytes
                    if allocated_bytes is not None
                    else self._snapshot.allocated_bytes
                ),
            )
        if force:
            self._wake.set()

    def begin_read(
        self,
        offset: int,
        size: int,
        *,
        status: str = "reading",
        section_start: int | None = None,
        section_end: int | None = None,
        slow_threshold: float = 2.0,
        hard_threshold: float = 10.0,
    ) -> None:
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                status=status,
                read_offset=offset,
                read_size=size,
                read_started_at=self._clock(),
                read_frozen_elapsed=None,
                section_start=section_start,
                section_end=section_end,
                slow_threshold=slow_threshold,
                hard_threshold=hard_threshold,
            )
        self._wake.set()

    def set_read_status(self, status: str) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, status=status)
        self._wake.set()

    def end_read(self, *, status: str) -> None:
        now = self._clock()
        with self._lock:
            started = self._snapshot.read_started_at
            frozen = (
                max(0.0, now - started)
                if started is not None
                else self._snapshot.read_frozen_elapsed
            )
            self._snapshot = replace(
                self._snapshot,
                status=status,
                read_started_at=None,
                read_frozen_elapsed=frozen,
            )
        self._wake.set()

    def announce_pass(self, pass_number: int) -> None:
        self.event(
            "normal",
            format_live_header(self.label, self.total, current_pass=pass_number),
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
        if snapshot.status == "preparing":
            spinner = "|/-\\"[int(now * 4) % 4]
            return format_preparing_lines(
                name=self.destination_label,
                total=snapshot.total,
                elapsed=elapsed,
                spinner=spinner,
                storage_mode=snapshot.storage_mode,
                allocated_bytes=snapshot.allocated_bytes,
            )
        counts = tally_kinds(snapshot.ranges, snapshot.total)
        width = max(40, shutil.get_terminal_size(fallback=(100, 24)).columns)
        if snapshot.read_started_at is not None:
            read_elapsed = max(0.0, now - snapshot.read_started_at)
        else:
            read_elapsed = snapshot.read_frozen_elapsed or 0.0
        read_status = snapshot.status
        if snapshot.read_started_at is not None and snapshot.status not in {
            "cancel requested",
            "cancelled",
            "cancel pending",
        }:
            if read_elapsed >= snapshot.hard_threshold:
                read_status = "hard"
            elif read_elapsed >= snapshot.slow_threshold:
                read_status = "slow"
            else:
                read_status = "reading"
        return format_live_lines(
            name=self.label,
            total=snapshot.total,
            counts=counts,
            elapsed=elapsed,
            width=width,
            color=self._tty,
            current_pass=snapshot.current_pass,
            recovered=snapshot.recovered,
            ranges=snapshot.ranges,
            read_offset=snapshot.read_offset,
            read_size=snapshot.read_size,
            read_elapsed=read_elapsed,
            read_status=read_status,
            section_start=snapshot.section_start,
            section_end=snapshot.section_end,
        )

    def _emit(self, text: str) -> None:
        if self._closed:
            return
        try:
            self.stream.write(text)
        except (OSError, ValueError, RuntimeError):
            self._closed = True

    def _render(self, *, final: bool = False) -> None:
        if self._closed and not final:
            return
        now = self._clock()
        snapshot, events = self._snapshot_and_events()
        speed_interval = max(now - self._last_speed_at, 1e-9)
        if final or speed_interval >= self.update_interval * 0.8:
            self._speed = max(0, snapshot.recovered - self._last_recovered) / speed_interval
            self._last_recovered = snapshot.recovered
            self._last_speed_at = now
        width = max(40, shutil.get_terminal_size(fallback=(100, 24)).columns)
        if self._tty:
            if self._live_lines:
                self._emit(f"\x1b[{self._live_lines}A")
                for _ in range(self._live_lines):
                    self._emit("\r\x1b[2K\n")
                self._emit(f"\x1b[{self._live_lines}A")
            for kind, message in events:
                self._emit(self._color(self._truncate(message), kind) + "\n")
            lines = self._lines(snapshot, now)
            for line in lines:
                self._emit("\r\x1b[2K" + line + "\n")
            self._live_lines = visual_line_count(lines, width)
        else:
            for kind, message in events:
                self._emit(f"[{kind}] {message}\n")
            self._emit("\n".join(self._lines(snapshot, now)) + "\n")
        try:
            if not self._closed:
                self.stream.flush()
        except (OSError, ValueError, RuntimeError):
            self._closed = True

    def _render_loop(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.wait(self.update_interval)
                self._wake.clear()
                if self._stop.is_set():
                    break
                self._render()
        except (OSError, ValueError, RuntimeError):
            return

    def close(self) -> None:
        if self._closed:
            return
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=max(2.0, self.update_interval * 2))
        try:
            self._render(final=True)
        except (OSError, ValueError, RuntimeError):
            pass
        self._closed = True
