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

    ``ReadFile`` runs on a dedicated thread so a USB driver that blocks inside
    the call cannot prevent the budget wait or Ctrl+C handler from running.
    Cancellation uses both ``CancelIoEx`` and ``CancelSynchronousIo``.  It
    completes at the OS request layer; a USB bridge or drive may continue
    lower-level activity afterwards.  If Windows does not acknowledge
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
        kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
        kernel32.CancelSynchronousIo.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentThread.argtypes = []
        kernel32.GetCurrentThread.restype = wintypes.HANDLE
        kernel32.DuplicateHandle.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.DuplicateHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        self.path = Path(path)
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._overlapped_type = Overlapped
        self._kernel32 = kernel32
        self._cancel_grace = cancel_grace
        self._poll_interval = poll_interval
        self._pending: tuple[
            object, object, object, object, threading.Event, list[str]
        ] | None = None
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

    def _duplicate_current_thread_handle(self) -> object:
        duplicated = self._wintypes.HANDLE()
        if not self._kernel32.DuplicateHandle(
            self._kernel32.GetCurrentProcess(),
            self._kernel32.GetCurrentThread(),
            self._kernel32.GetCurrentProcess(),
            self._ctypes.byref(duplicated),
            0,
            False,
            0x00000002,  # DUPLICATE_SAME_ACCESS
        ):
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        return duplicated.value

    def _request_cancel(self, overlapped: object, thread_handle: object | None) -> None:
        # Best-effort at both layers.  USB drivers may not have queued an IRP
        # yet, so CancelIoEx / CancelSynchronousIo failing is not fatal; the
        # wait loop decides whether cancellation completed.
        self._kernel32.CancelIoEx(self._handle, self._ctypes.byref(overlapped))
        if thread_handle:
            self._kernel32.CancelSynchronousIo(thread_handle)

    def _cancel(
        self,
        pending: tuple[
            object, object, object, object, threading.Event, list[str]
        ],
        reason: Literal["budget", "stop"],
    ) -> None:
        buffer, overlapped, event, thread_handle, issue_finished, issue_result = pending
        self._request_cancel(overlapped, thread_handle)

        deadline = time.monotonic() + self._cancel_grace
        while time.monotonic() < deadline:
            self._request_cancel(overlapped, thread_handle)
            # CancelSynchronousIo may make a driver-blocked ReadFile return
            # before an OVERLAPPED request was ever queued. In that case there
            # is no native completion event to wait for, but the call is no
            # longer using the request storage.
            if issue_finished.is_set() and issue_result != ["pending"]:
                self._close_handle(event)
                self._close_handle(thread_handle)
                raise ReadCancelledError(reason, cancellation_completed=True)
            if self._wait(event, min(self._poll_interval, deadline - time.monotonic())) == 0:
                try:
                    self._result(overlapped, buffer)
                except OSError as exc:
                    if getattr(exc, "winerror", None) != 995:  # ERROR_OPERATION_ABORTED
                        raise
                self._close_handle(event)
                self._close_handle(thread_handle)
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
        immediate = self._wintypes.DWORD()
        started_event = threading.Event()
        issue_finished = threading.Event()
        issue_error: list[BaseException] = []
        issue_result: list[str] = []
        thread_handles: list[object] = []

        def issue() -> None:
            try:
                thread_handles.append(self._duplicate_current_thread_handle())
            except OSError as exc:
                issue_error.append(exc)
                started_event.set()
                issue_finished.set()
                return
            started_event.set()
            try:
                issued = self._kernel32.ReadFile(
                    self._handle,
                    buffer,
                    size,
                    self._ctypes.byref(immediate),
                    self._ctypes.byref(overlapped),
                )
                if issued:
                    issue_result.append("completed")
                    return
                error = self._ctypes.get_last_error()
                if error == 997:  # ERROR_IO_PENDING
                    issue_result.append("pending")
                else:
                    issue_error.append(self._ctypes.WinError(error))
            finally:
                # Do not signal the native OVERLAPPED event here. The main
                # thread may already have closed it after cancellation; a late
                # SetEvent could otherwise target a recycled Windows handle.
                issue_finished.set()

        worker = threading.Thread(
            target=issue, name="flvrescue-windows-read", daemon=True
        )
        worker.start()
        # This handshake happens before source I/O. Waiting without a timeout
        # avoids letting a delayed thread issue ReadFile with an event handle
        # that the caller has already closed.
        started_event.wait()
        thread_handle = thread_handles[0] if thread_handles else None
        pending = (
            buffer,
            overlapped,
            event,
            thread_handle,
            issue_finished,
            issue_result,
        )
        started = time.monotonic()

        try:
            while True:
                elapsed = time.monotonic() - started
                reason: Literal["budget", "stop"] | None = None
                if stop_event is not None and stop_event.is_set():
                    reason = "stop"
                elif elapsed >= budget:
                    reason = "budget"
                if issue_finished.is_set():
                    if issue_error:
                        self._close_handle(event)
                        self._close_handle(thread_handle)
                        raise issue_error[0]
                    if issue_result == ["completed"]:
                        self._close_handle(event)
                        self._close_handle(thread_handle)
                        return bytes(buffer.raw[: immediate.value])
                if self._wait(event, 0) == 0:
                    try:
                        return self._result(overlapped, buffer)
                    finally:
                        self._close_handle(event)
                        self._close_handle(thread_handle)
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
                        self._close_handle(thread_handle)
        finally:
            if worker.is_alive():
                worker.join(timeout=0.0)

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
