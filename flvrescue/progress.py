"""Low-frequency terminal progress reporting."""

from __future__ import annotations

import sys
import time
from os import PathLike
from pathlib import Path
from typing import TextIO


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


class ProgressReporter:
    """Emit one compact status line no more often than ``update_interval``."""

    def __init__(
        self,
        file_path: str | PathLike[str],
        total: int,
        *,
        stream: TextIO | None = None,
        update_interval: float = 1.0,
        clock: callable = time.monotonic,
    ) -> None:
        if total < 0:
            raise ValueError("total must be non-negative")
        if update_interval < 0:
            raise ValueError("update_interval must be non-negative")
        self.file_path = str(Path(file_path))
        self.total = total
        self.stream = stream if stream is not None else sys.stderr
        self.update_interval = update_interval
        self._clock = clock
        self._started_at = clock()
        self._last_at = self._started_at
        self._last_processed = 0
        self._wrote_status = False

    def update(
        self,
        processed: int,
        *,
        unreadable: int = 0,
        bad_ranges: int = 0,
        force: bool = False,
    ) -> bool:
        now = self._clock()
        if not force and now - self._last_at < self.update_interval:
            return False
        elapsed = max(now - self._started_at, 1e-9)
        interval = max(now - self._last_at, 1e-9)
        current_speed = max(0, processed - self._last_processed) / interval
        average_speed = max(0, processed) / elapsed
        percentage = 100.0 if self.total == 0 else processed / self.total * 100
        recovered = max(0, processed - unreadable)
        line = (
            f"File: {self.file_path} | {format_bytes(processed)} / {format_bytes(self.total)} "
            f"({percentage:5.1f}%) | Speed: {format_bytes(int(current_speed))}/s "
            f"(avg {format_bytes(int(average_speed))}/s) | Elapsed: {format_elapsed(elapsed)} "
            f"| Recovered: {format_bytes(recovered)} | Unreadable: {format_bytes(unreadable)} "
            f"| Bad ranges: {bad_ranges}"
        )
        print(line, file=self.stream, flush=True)
        self._last_at = now
        self._last_processed = processed
        self._wrote_status = True
        return True

    def close(self) -> None:
        # Status lines use ordinary newlines, so there is no terminal redraw to
        # clean up.  Kept as a hook for custom reporters and API symmetry.
        return None
