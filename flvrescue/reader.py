"""Offset-based readers used by the rescue engine.

The rescue loop intentionally never relies on the implicit file position after a
read error.  ``FileReader`` therefore seeks immediately before every read.
"""

from __future__ import annotations

import os
from os import PathLike
from pathlib import Path
import threading
import time
from collections.abc import Callable
from typing import Literal, Protocol, runtime_checkable


ReaderBackend = Literal["auto", "windows", "portable"]


class ReadCancelledError(OSError):
    """A bounded read was cancelled or could not finish cancellation promptly."""

    def __init__(
        self,
        reason: Literal["budget", "stop"],
        *,
        cancellation_completed: bool,
    ) -> None:
        self.reason = reason
        self.cancellation_completed = cancellation_completed
        detail = "completed" if cancellation_completed else "still pending"
        super().__init__(f"read cancelled after {reason}; cancellation {detail}")


@runtime_checkable
class Reader(Protocol):
    """The small reader contract that makes I/O failure simulation possible."""

    def read_at(self, offset: int, size: int) -> bytes:
        """Return exactly ``size`` bytes starting at ``offset`` or raise ``OSError``."""


class FileReader:
    """Portable file-backed :class:`Reader`.

    ``os.pread`` would also suit this use case, but is not universally available
    on supported Windows Python builds.  This implementation is deliberately
    simple and is used by one sequential rescue loop only.
    """

    backend_name = "portable"
    cancellation_supported = False

    def __init__(self, path: str | PathLike[str]) -> None:
        self.path = Path(path)
        self._file = self.path.open("rb", buffering=0)

    def read_at(self, offset: int, size: int) -> bytes:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if size < 0:
            raise ValueError("size must be non-negative")

        # A failed read can leave the OS/Python file position unspecified.  Do
        # not try to recover it: establish the requested absolute position for
        # every attempt instead.
        self._file.seek(offset)
        return self._file.read(size)

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "FileReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


_PENDING_WINDOWS_READS: list[object] = []


class WindowsOverlappedReader:
    """Windows read-only backend using overlapped I/O and ``CancelIoEx``.

    Cancellation completes at the OS request layer; a USB bridge or drive may
    continue lower-level activity afterwards.  If Windows does not acknowledge
    cancellation promptly, the request storage and handle are deliberately kept
    alive until process exit rather than risking use-after-free in kernel I/O.
    """

    backend_name = "windows-overlapped"
    cancellation_supported = True

    def __init__(
        self,
        path: str | PathLike[str],
        *,
        cancel_grace: float = 2.0,
        poll_interval: float = 0.1,
    ) -> None:
        if os.name != "nt":
            raise OSError("the Windows overlapped reader is only available on Windows")
        if cancel_grace <= 0 or poll_interval <= 0:
            raise ValueError("cancel_grace and poll_interval must be greater than zero")

        import ctypes
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateEventW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(Overlapped),
        ]
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.GetOverlappedResult.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Overlapped),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
        ]
        kernel32.GetOverlappedResult.restype = wintypes.BOOL
        kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped)]
        kernel32.CancelIoEx.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        self.path = Path(path)
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._overlapped_type = Overlapped
        self._kernel32 = kernel32
        self._cancel_grace = cancel_grace
        self._poll_interval = poll_interval
        self._pending: tuple[object, object, object] | None = None
        self._closed = False

        generic_read = 0x80000000
        share_all = 0x00000001 | 0x00000002 | 0x00000004
        open_existing = 3
        flags = 0x00000080 | 0x40000000
        handle = kernel32.CreateFileW(
            str(self.path), generic_read, share_all, None, open_existing, flags, None
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle

    def _close_handle(self, handle: object) -> None:
        if handle:
            self._kernel32.CloseHandle(handle)

    def _wait(self, event: object, seconds: float) -> int:
        milliseconds = max(0, min(int(seconds * 1000), 0xFFFFFFFE))
        return int(self._kernel32.WaitForSingleObject(event, milliseconds))

    def _result(self, overlapped: object, buffer: object) -> bytes:
        transferred = self._wintypes.DWORD()
        if not self._kernel32.GetOverlappedResult(
            self._handle,
            self._ctypes.byref(overlapped),
            self._ctypes.byref(transferred),
            False,
        ):
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        return bytes(buffer.raw[: transferred.value])

    def _cancel(
        self,
        pending: tuple[object, object, object],
        reason: Literal["budget", "stop"],
    ) -> None:
        buffer, overlapped, event = pending
        cancelled = self._kernel32.CancelIoEx(
            self._handle, self._ctypes.byref(overlapped)
        )
        if not cancelled:
            error = self._ctypes.get_last_error()
            if error != 1168:  # ERROR_NOT_FOUND: request completed during cancellation.
                raise self._ctypes.WinError(error)

        deadline = time.monotonic() + self._cancel_grace
        while time.monotonic() < deadline:
            if self._wait(event, min(self._poll_interval, deadline - time.monotonic())) == 0:
                try:
                    self._result(overlapped, buffer)
                except OSError as exc:
                    if getattr(exc, "winerror", None) != 995:  # ERROR_OPERATION_ABORTED
                        raise
                self._close_handle(event)
                raise ReadCancelledError(reason, cancellation_completed=True)

        self._pending = pending
        raise ReadCancelledError(reason, cancellation_completed=False)

    def read_at_cancellable(
        self,
        offset: int,
        size: int,
        *,
        budget: float,
        stop_event: threading.Event | None = None,
        on_cancel: Callable[[str], None] | None = None,
    ) -> bytes:
        if self._closed:
            raise ValueError("reader is closed")
        if self._pending is not None:
            raise OSError("a previous Windows read is still pending")
        if offset < 0 or size < 0:
            raise ValueError("offset and size must be non-negative")
        if budget <= 0:
            raise ValueError("budget must be greater than zero")

        buffer = self._ctypes.create_string_buffer(size)
        event = self._kernel32.CreateEventW(None, True, False, None)
        if not event:
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        overlapped = self._overlapped_type()
        overlapped.Offset = offset & 0xFFFFFFFF
        overlapped.OffsetHigh = (offset >> 32) & 0xFFFFFFFF
        overlapped.hEvent = event
        pending = (buffer, overlapped, event)
        immediate = self._wintypes.DWORD()
        started = time.monotonic()

        issued = self._kernel32.ReadFile(
            self._handle,
            buffer,
            size,
            self._ctypes.byref(immediate),
            self._ctypes.byref(overlapped),
        )
        if issued:
            self._close_handle(event)
            return bytes(buffer.raw[: immediate.value])
        error = self._ctypes.get_last_error()
        if error != 997:  # ERROR_IO_PENDING
            self._close_handle(event)
            raise self._ctypes.WinError(error)

        while True:
            elapsed = time.monotonic() - started
            reason: Literal["budget", "stop"] | None = None
            if stop_event is not None and stop_event.is_set():
                reason = "stop"
            elif elapsed >= budget:
                reason = "budget"
            if reason is not None:
                if on_cancel is not None:
                    on_cancel("cancel requested")
                self._cancel(pending, reason)
            wait_for = min(self._poll_interval, max(0.0, budget - elapsed))
            if self._wait(event, wait_for) == 0:
                try:
                    return self._result(overlapped, buffer)
                finally:
                    self._close_handle(event)

    def read_at(self, offset: int, size: int) -> bytes:
        return self.read_at_cancellable(offset, size, budget=24 * 60 * 60)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pending is not None:
            pending = self._pending
            try:
                self._cancel(pending, "stop")
            except ReadCancelledError as exc:
                if not exc.cancellation_completed:
                    _PENDING_WINDOWS_READS.append((self._handle, pending))
                    self._handle = None
            finally:
                self._pending = None
        if self._handle:
            self._close_handle(self._handle)
            self._handle = None

    def __enter__(self) -> "WindowsOverlappedReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def open_reader(path: str | PathLike[str], backend: ReaderBackend = "auto") -> Reader:
    if backend not in ("auto", "windows", "portable"):
        raise ValueError("reader backend must be auto, windows, or portable")
    if backend == "portable":
        return FileReader(path)
    if backend == "windows" or (backend == "auto" and os.name == "nt"):
        return WindowsOverlappedReader(path)
    return FileReader(path)
