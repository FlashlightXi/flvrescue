from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

import pytest

from flvrescue import storage


class _TrackingHandle:
    def __init__(self, handle: BinaryIO, truncate_calls: list[int]) -> None:
        self._handle = handle
        self._truncate_calls = truncate_calls

    def truncate(self, size: int | None = None) -> int:
        self._truncate_calls.append(-1 if size is None else size)
        raise AssertionError("lazy destination preparation must not call truncate")

    def __getattr__(self, name: str) -> object:
        return getattr(self._handle, name)


def test_sparse_failure_is_rejected_on_windows_without_calling_truncate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "rescued.flv"
    truncate_calls: list[int] = []
    real_open = Path.open

    def tracked_open(path: Path, *args: object, **kwargs: object) -> _TrackingHandle:
        return _TrackingHandle(real_open(path, *args, **kwargs), truncate_calls)

    monkeypatch.setattr(storage, "_enable_sparse_windows", lambda _handle: False)
    monkeypatch.setattr(storage, "_requires_sparse_destination", lambda: True)
    monkeypatch.setattr(storage.Path, "open", tracked_open)

    with pytest.raises(OSError, match="did not accept sparse files"):
        storage.prepare_destination(destination, 1024 * 1024, resuming=False)

    assert not destination.exists()
    assert truncate_calls == []


def test_non_windows_fallback_uses_lazy_extension_without_truncate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "rescued.flv"
    truncate_calls: list[int] = []
    real_open = Path.open

    def tracked_open(path: Path, *args: object, **kwargs: object) -> _TrackingHandle:
        return _TrackingHandle(real_open(path, *args, **kwargs), truncate_calls)

    monkeypatch.setattr(storage, "_enable_sparse_windows", lambda _handle: False)
    monkeypatch.setattr(storage, "_requires_sparse_destination", lambda: False)
    monkeypatch.setattr(storage.Path, "open", tracked_open)

    handle, prepared = storage.prepare_destination(
        destination, 1024 * 1024, resuming=False
    )
    try:
        assert prepared.mode == "lazy"
        assert prepared.logical_size == 0
        assert destination.stat().st_size == 0
        assert truncate_calls == []
    finally:
        handle.close()


def test_sparse_success_also_grows_lazily_without_calling_truncate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "rescued.flv"
    truncate_calls: list[int] = []
    real_open = Path.open

    def tracked_open(path: Path, *args: object, **kwargs: object) -> _TrackingHandle:
        return _TrackingHandle(real_open(path, *args, **kwargs), truncate_calls)

    monkeypatch.setattr(storage, "_enable_sparse_windows", lambda _handle: True)
    monkeypatch.setattr(storage.Path, "open", tracked_open)

    handle, prepared = storage.prepare_destination(
        destination, 1024 * 1024, resuming=False
    )
    try:
        assert prepared.mode == "sparse"
        assert prepared.logical_size == 0
        assert destination.stat().st_size == 0
        assert truncate_calls == []
    finally:
        handle.close()


def test_sparse_zero_failure_does_not_fall_back_to_allocating_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "rescued.flv"
    destination.write_bytes(b"x" * 16)
    monkeypatch.setattr(storage, "_zero_sparse_range_windows", lambda *_args: False)

    with destination.open("r+b") as handle:
        with pytest.raises(OSError, match="preserve sparse storage"):
            storage.zero_destination_range(handle, 0, 16, sparse=True)

    assert destination.read_bytes() == b"x" * 16


def test_short_destination_can_resume_when_all_recovered_bytes_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "rescued.flv"
    destination.write_bytes(b"x" * 16)
    monkeypatch.setattr(storage, "_requires_sparse_destination", lambda: False)

    handle, prepared = storage.prepare_destination(
        destination, 128, resuming=True, recovered_end=16
    )
    try:
        assert prepared.mode == "resume"
        assert prepared.initial_size == 16
        assert prepared.logical_size == 16
        assert destination.stat().st_size == 16
    finally:
        handle.close()


def test_resume_rejects_destination_shorter_than_committed_data(tmp_path: Path) -> None:
    destination = tmp_path / "rescued.flv"
    destination.write_bytes(b"x" * 15)
    with pytest.raises(ValueError, match="incompatible"):
        storage.prepare_destination(destination, 128, resuming=True, recovered_end=16)


def test_windows_resume_rejects_short_non_sparse_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "rescued.flv"
    destination.write_bytes(b"x" * 16)
    monkeypatch.setattr(storage, "_requires_sparse_destination", lambda: True)

    with pytest.raises(OSError, match="not sparse"):
        storage.prepare_destination(destination, 128, resuming=True, recovered_end=16)


def test_zero_range_fallback_clears_existing_bytes_without_extending(tmp_path: Path) -> None:
    destination = tmp_path / "rescued.flv"
    destination.write_bytes(b"x" * 16)
    with destination.open("r+b") as handle:
        storage.zero_destination_range(handle, 8, 16, sparse=False)

    assert destination.read_bytes() == b"x" * 8 + b"\0" * 8


def test_zero_range_beyond_lazy_end_does_not_extend(tmp_path: Path) -> None:
    destination = tmp_path / "rescued.flv"
    destination.write_bytes(b"x" * 8)
    with destination.open("r+b") as handle:
        storage.zero_destination_range(handle, 64, 32, sparse=False)

    assert destination.stat().st_size == 8
