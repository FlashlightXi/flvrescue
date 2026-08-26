"""Two-stage Ctrl+C handling shared by the CLI and recovery engine."""

from __future__ import annotations

from contextlib import contextmanager
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator


class StopController:
    """Thread-safe request to stop issuing source reads."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._interrupts = 0

    @property
    def event(self) -> threading.Event:
        return self._event

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request_stop(self) -> int:
        with self._lock:
            self._interrupts += 1
            count = self._interrupts
            self._event.set()
            return count


def _default_force_exit(code: int) -> None:
    os._exit(code)


@contextmanager
def two_stage_interrupts(
    controller: StopController,
    *,
    stream: object = sys.stderr,
    force_exit: Callable[[int], None] = _default_force_exit,
) -> Iterator[None]:
    """First Ctrl+C requests cancellation; the second leaves immediately."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return

    previous = signal.getsignal(signal.SIGINT)

    def handle_interrupt(_signum: int, _frame: object) -> None:
        count = controller.request_stop()
        if count == 1:
            message = (
                "\nSTOPPING - cancelling the current read if possible; "
                "press Ctrl+C again to force process exit.\n"
            )
        else:
            message = (
                "\nFORCE EXIT - OS/device I/O may continue; "
                "do not disconnect the drive yet.\n"
            )
        write = getattr(stream, "write", None)
        flush = getattr(stream, "flush", None)
        if callable(write):
            write(message)
        if callable(flush):
            flush()
        if count >= 2:
            force_exit(130)

    signal.signal(signal.SIGINT, handle_interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
