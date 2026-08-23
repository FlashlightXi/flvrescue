"""Destination-file preparation that never opens the source for writing."""

from __future__ import annotations

import os
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import BinaryIO, Literal


StorageMode = Literal["sparse", "lazy", "resume"]
_FILE_ATTRIBUTE_SPARSE_FILE = 0x00000200
_ZERO_CHUNK_SIZE = 1024 * 1024


def _requires_sparse_destination() -> bool:
    """Windows recovery refuses capacity-unsafe non-sparse destinations."""

    return os.name == "nt"


@dataclass(frozen=True)
class DestinationPreparation:
    """Facts about how a destination was prepared for an engine run."""

    path: Path
    mode: StorageMode
    source_size: int
    initial_size: int
    logical_size: int
    sparse: bool


def _enable_sparse_windows(handle: BinaryIO) -> bool:
    """Request ``FSCTL_SET_SPARSE`` for an already-created Windows file.

    False means that callers must use lazy extension.  This intentionally does
    not fail recovery on filesystems, Python builds, or security policies that
    do not support sparse files.
    """

    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        device_io_control = ctypes.WinDLL(
            "kernel32", use_last_error=True
        ).DeviceIoControl
        device_io_control.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        device_io_control.restype = wintypes.BOOL
        bytes_returned = wintypes.DWORD(0)
        ok = device_io_control(
            wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno())),
            0x000900C4,  # FSCTL_SET_SPARSE
            None,
            0,
            None,
            0,
            ctypes.byref(bytes_returned),
            None,
        )
        return bool(ok)
    except (AttributeError, OSError, ValueError):
        return False


def _zero_sparse_range_windows(handle: BinaryIO, offset: int, length: int) -> bool:
    """Deallocate a sparse range while preserving its logical zero bytes."""

    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        class FileZeroDataInformation(ctypes.Structure):
            _fields_ = (
                ("FileOffset", ctypes.c_longlong),
                ("BeyondFinalZero", ctypes.c_longlong),
            )

        device_io_control = ctypes.WinDLL(
            "kernel32", use_last_error=True
        ).DeviceIoControl
        device_io_control.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        device_io_control.restype = wintypes.BOOL
        zero_range = FileZeroDataInformation(offset, offset + length)
        bytes_returned = wintypes.DWORD(0)
        ok = device_io_control(
            wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno())),
            0x000980C8,  # FSCTL_SET_ZERO_DATA
            ctypes.byref(zero_range),
            ctypes.sizeof(zero_range),
            None,
            0,
            ctypes.byref(bytes_returned),
            None,
        )
        return bool(ok)
    except (AttributeError, OSError, OverflowError, ValueError):
        return False


def zero_destination_range(
    handle: BinaryIO, offset: int, length: int, *, sparse: bool
) -> None:
    """Make a destination range read as zero without needless file growth.

    Sparse Windows files use ``FSCTL_SET_ZERO_DATA`` so unreadable ranges do
    not consume clusters.  The portable fallback clears only bytes already
    inside the logical file; a later offset write naturally creates zero-filled
    holes, so extending a lazy destination here would waste SSD space.
    """

    if offset < 0 or length <= 0:
        raise ValueError("zero range must have a non-negative offset and positive length")
    current_size = os.fstat(handle.fileno()).st_size
    remaining = min(offset + length, current_size) - offset
    if remaining <= 0:
        return
    if sparse:
        if not _zero_sparse_range_windows(handle, offset, remaining):
            raise OSError(
                "could not preserve sparse storage while clearing an unreadable range"
            )
        return
    handle.seek(offset)
    zeroes = b"\0" * min(_ZERO_CHUNK_SIZE, remaining)
    while remaining:
        requested = min(len(zeroes), remaining)
        written = handle.write(zeroes[:requested])
        if written is None:
            written = requested
        if not isinstance(written, int) or written <= 0:
            raise OSError("short zero write to destination")
        remaining -= written


def prepare_destination(
    path: str | PathLike[str],
    source_size: int,
    *,
    resuming: bool,
    recovered_end: int = 0,
) -> tuple[BinaryIO, DestinationPreparation]:
    """Open a destination safely and report its preparation mode.

    New Windows destinations are made sparse when possible.  Sparse and
    portable destinations both grow only as offset writes arrive; pre-sizing a
    sparse file can reserve clusters on some real NTFS configurations.  Resume
    never extends a short file merely to match the source; only already-
    committed recovered bytes are a hard lower bound.
    """

    if isinstance(source_size, bool) or not isinstance(source_size, int) or source_size < 0:
        raise ValueError("source_size must be a non-negative integer")
    if isinstance(recovered_end, bool) or not isinstance(recovered_end, int) or recovered_end < 0:
        raise ValueError("recovered_end must be a non-negative integer")
    if recovered_end > source_size:
        raise ValueError("recovered_end exceeds source_size")

    destination = Path(path)
    if resuming:
        if not destination.is_file():
            raise FileNotFoundError("destination from resume map does not exist")
        destination_stat = destination.stat()
        initial_size = destination_stat.st_size
        sparse = bool(
            getattr(destination_stat, "st_file_attributes", 0)
            & _FILE_ATTRIBUTE_SPARSE_FILE
        )
        if initial_size < recovered_end or initial_size > source_size:
            raise ValueError("destination size is incompatible with committed recovered ranges")
        if _requires_sparse_destination() and not sparse and initial_size < source_size:
            raise OSError(
                "partial destination is not sparse; refusing capacity-unsafe offset writes"
            )
        return (
            destination.open("r+b"),
            DestinationPreparation(
                path=destination,
                mode="resume",
                source_size=source_size,
                initial_size=initial_size,
                logical_size=initial_size,
                sparse=sparse,
            ),
        )

    if destination.exists():
        raise FileExistsError(
            "destination already exists without a matching rescue map; refusing to overwrite it"
        )
    handle = destination.open("x+b")
    initial_size = 0
    if _enable_sparse_windows(handle):
        return (
            handle,
            DestinationPreparation(
                path=destination,
                mode="sparse",
                source_size=source_size,
                initial_size=initial_size,
                logical_size=initial_size,
                sparse=True,
            ),
        )
    if _requires_sparse_destination():
        handle.close()
        try:
            destination.unlink()
        except OSError:
            pass
        raise OSError(
            "destination filesystem did not accept sparse files; use an NTFS/ReFS destination"
        )
    return (
        handle,
        DestinationPreparation(
            path=destination,
            mode="lazy",
            source_size=source_size,
            initial_size=initial_size,
            logical_size=initial_size,
            sparse=False,
        ),
    )
