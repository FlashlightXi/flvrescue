from __future__ import annotations

import ctypes
import io
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from conftest import FaultInjectingReader
from flvrescue.cli import main
from flvrescue.interrupts import StopController, two_stage_interrupts
from flvrescue.mapfile import RecoveryRange, RescueMap, load_map, save_map_atomic
from flvrescue.policy import RecoveryPolicy
from flvrescue.reader import ReadCancelledError, WindowsOverlappedReader, describe_os_error
from flvrescue.rescue import rescue


class CancellingReader(FaultInjectingReader):
    def __init__(self, data: bytes, *, completed: bool = True) -> None:
        super().__init__(data)
        self.completed = completed

    def read_at_cancellable(
        self,
        offset: int,
        size: int,
        *,
        budget: float,
        stop_event: threading.Event | None = None,
        on_cancel: object = None,
    ) -> bytes:
        self.calls.append((offset, size))
        callback = on_cancel if callable(on_cancel) else None
        if callback is not None:
            callback("cancel requested")
        raise ReadCancelledError("budget", cancellation_completed=self.completed)


class StopCancellingReader(FaultInjectingReader):
    def read_at_cancellable(
        self,
        offset: int,
        size: int,
        *,
        budget: float,
        stop_event: threading.Event | None = None,
        on_cancel: object = None,
    ) -> bytes:
        self.calls.append((offset, size))
        raise ReadCancelledError("stop", cancellation_completed=True)


def small_policy() -> RecoveryPolicy:
    return RecoveryPolicy(
        block=16,
        slow_block=8,
        hard_block=4,
        fallback=4,
        sector=2,
        checkpoint=16,
        slow_threshold=1.0,
        hard_threshold=5.0,
        survey_budget=1.0,
        fast_budget=1.0,
        slow_budget=2.0,
        hard_budget=3.0,
        deep_budget=4.0,
        skip_start=16,
        skip_max=64,
        skip_factor=2,
        skip_reset_after=1,
        survey_stride=16,
    )


def test_budget_cancel_defers_without_zeroing_and_later_pass_recovers(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = bytes(range(16))
    source.write_bytes(payload)
    cancelling = CancellingReader(payload)

    first = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: cancelling,
        progress=False,
        policy=small_policy(),
        through="survey",
    )

    assert first.unreadable_bytes == 0
    assert first.slow_skipped_bytes == len(payload)
    assert load_map(map_path).ranges[0].cause == "pass_1_budget"

    normal = FaultInjectingReader(payload)
    final = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: normal,
        progress=False,
        policy=small_policy(),
        through="slow",
    )
    assert final.recovered_bytes == len(payload)
    assert destination.read_bytes() == payload


def test_pending_cancellation_stops_after_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = bytes(range(16))
    source.write_bytes(payload)

    result = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: CancellingReader(payload, completed=False),
        progress=False,
        policy=small_policy(),
        through="survey",
    )

    assert result.stopped is True
    assert result.cancellation_pending is True
    saved = load_map(map_path)
    assert saved.ranges[0].status == "skipped"
    assert saved.ranges[0].cause == "pass_1_budget"


def test_pending_fast_pass_cancel_defers_the_rest_of_the_hole(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = bytes(range(48))
    source.write_bytes(payload)
    destination.write_bytes(payload[:16] + b"\0" * 32)
    save_map_atomic(
        map_path,
        RescueMap(
            source_path=str(source.resolve()),
            source_size=len(payload),
            destination_path=str(destination.resolve()),
            ranges=[
                RecoveryRange(0, 16, "recovered", None, "fast"),
                RecoveryRange(16, 32, "skipped", "survey"),
            ],
            current_pass=2,
            pass_cursor=16,
            policy=small_policy(),
        ),
    )

    result = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: CancellingReader(payload, completed=False),
        progress=False,
        policy=small_policy(),
        through="fast",
    )

    assert result.stopped is True
    assert result.cancellation_pending is True
    saved = load_map(map_path)
    assert [
        (item.offset, item.length, item.status, item.cause, item.difficulty)
        for item in saved.ranges
    ] == [
        (0, 16, "recovered", None, "fast"),
        (16, 16, "skipped", "pass_2_budget", "hard"),
        (32, 16, "skipped", "fast_pass", "slow"),
    ]
    assert saved.easy_skipped_bytes == 0

    reader = FaultInjectingReader(payload)
    resumed = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: reader,
        progress=False,
        policy=small_policy(),
        through="fast",
    )

    assert reader.calls == []
    assert resumed.easy_skipped_bytes == 0
    assert resumed.current_pass == 3


class _CrcReader(FaultInjectingReader):
    def read_at(self, offset: int, size: int) -> bytes:
        self.calls.append((offset, size))
        error = OSError("巡回冗長検査 (CRC) エラーです")
        error.winerror = 23
        raise error


def test_fast_pass_budget_cancel_is_hard_and_the_rest_of_the_hole_stays_slow(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = bytes(range(48))
    source.write_bytes(payload)
    destination.write_bytes(payload[:16] + b"\0" * 32)
    save_map_atomic(
        map_path,
        RescueMap(
            source_path=str(source.resolve()),
            source_size=len(payload),
            destination_path=str(destination.resolve()),
            ranges=[
                RecoveryRange(0, 16, "recovered", None, "fast"),
                RecoveryRange(16, 32, "skipped", "survey"),
            ],
            current_pass=2,
            pass_cursor=16,
            policy=small_policy(),
        ),
    )

    result = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: CancellingReader(payload, completed=True),
        progress=False,
        policy=small_policy(),
        through="fast",
    )

    assert result.stopped is False
    saved = load_map(map_path)
    assert saved.ranges[1].cause == "pass_2_budget"
    assert saved.ranges[1].difficulty == "hard"
    assert saved.ranges[2].cause == "fast_pass"
    assert saved.ranges[2].difficulty == "slow"
    assert saved.current_pass == 3
    assert result.hard_skipped_bytes == 16


def test_windows_crc_error_is_kept_in_the_failure_text(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = bytes(range(16))
    source.write_bytes(payload)
    events: list[tuple[str, str]] = []

    class _Sink:
        def update(self, **_values: object) -> None:
            return None

        def begin_read(self, offset: int, size: int, *, status: str = "reading") -> None:
            return None

        def end_read(self, *, status: str) -> None:
            return None

        def event(self, kind: str, message: str) -> None:
            events.append((kind, message))

        def close(self) -> None:
            return None

    result = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: _CrcReader(payload),
        progress=_Sink(),
        policy=small_policy(),
        through="survey",
    )

    assert result.unreadable_bytes == 16
    assert any("23 CRC" in message for _kind, message in events)


def test_describe_os_error_names_crc() -> None:
    error = OSError("巡回冗長検査 (CRC) エラーです")
    error.winerror = 23
    assert describe_os_error(error).startswith("23 CRC")


def test_deep_budget_cancellation_does_not_mark_pass_complete(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = bytes(range(16))
    source.write_bytes(payload)
    destination.write_bytes(b"\0" * len(payload))
    state = RescueMap(
        source_path=str(source.resolve()),
        source_size=len(payload),
        destination_path=str(destination.resolve()),
        ranges=[RecoveryRange(0, len(payload), "skipped", "manual_defer", "hard")],
        current_pass=5,
        pass_cursor=0,
        policy=small_policy(),
    )
    save_map_atomic(map_path, state)

    result = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: CancellingReader(payload),
        progress=False,
        policy=small_policy(),
        through="deep",
    )

    assert result.stopped is True
    assert result.current_pass == 5
    saved = load_map(map_path)
    assert saved.current_pass == 5
    assert saved.ranges[0].cause == "pass_5_budget"


def test_requested_graceful_stop_issues_no_new_source_read(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    payload = bytes(range(16))
    source.write_bytes(payload)
    reader = FaultInjectingReader(payload)
    controller = StopController()
    controller.request_stop()

    result = rescue(
        source,
        destination,
        reader_factory=lambda _source: reader,
        progress=False,
        policy=small_policy(),
        through="survey",
        stop_controller=controller,
    )

    assert result.stopped is True
    assert reader.calls == []


def test_cancelled_user_stop_preserves_pending_range_for_resume(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = bytes(range(16))
    source.write_bytes(payload)

    result = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: StopCancellingReader(payload),
        progress=False,
        policy=small_policy(),
        through="survey",
    )

    assert result.stopped is True
    saved = load_map(map_path)
    assert saved.current_pass == 1
    assert saved.pass_cursor == 0
    assert saved.ranges == [RecoveryRange(0, len(payload), "unprocessed")]


def test_mark_command_changes_only_unrecovered_map_ranges(tmp_path: Path) -> None:
    map_path = tmp_path / "rescued.map.json"
    state = RescueMap(
        source_path=str(tmp_path / "missing-source.flv"),
        source_size=32,
        destination_path=str(tmp_path / "missing-destination.flv"),
        ranges=[
            RecoveryRange(0, 8, "recovered", None, "fast"),
            RecoveryRange(8, 24, "unprocessed"),
        ],
        current_pass=4,
        pass_cursor=0,
        policy=small_policy(),
    )
    save_map_atomic(map_path, state)

    assert main(
        ["mark", str(map_path), "--offset", "0", "--length", "16", "--as", "slow"]
    ) == 0
    marked = load_map(map_path)
    assert marked.ranges[0] == RecoveryRange(0, 8, "recovered", None, "fast")
    assert marked.ranges[1] == RecoveryRange(8, 8, "skipped", "manual_slow", "slow")
    assert marked.current_pass == 3

    assert main(
        ["mark", str(map_path), "--offset", "8", "--length", "8", "--clear"]
    ) == 0
    cleared = load_map(map_path)
    assert cleared.ranges[1] == RecoveryRange(8, 24, "unprocessed")
    assert cleared.current_pass == 1


def test_manual_defer_waits_until_deep(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    payload = bytes(range(16))
    source.write_bytes(payload)
    destination.write_bytes(b"\0" * len(payload))
    state = RescueMap(
        source_path=str(source.resolve()),
        source_size=len(payload),
        destination_path=str(destination.resolve()),
        ranges=[RecoveryRange(0, len(payload), "skipped", "manual_defer", "hard")],
        current_pass=4,
        pass_cursor=0,
        policy=small_policy(),
    )
    save_map_atomic(map_path, state)
    reader = FaultInjectingReader(payload)

    result = rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: reader,
        progress=False,
        policy=small_policy(),
        through="hard",
    )

    assert reader.calls == []
    assert result.current_pass == 5
    assert result.ranges[0].cause == "manual_defer"


def test_two_stage_interrupt_requests_stop_then_forces_exit() -> None:
    controller = StopController()
    stream = io.StringIO()
    exits: list[int] = []
    previous = signal.getsignal(signal.SIGINT)

    with two_stage_interrupts(
        controller, stream=stream, force_exit=exits.append
    ):
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
        assert controller.requested
        assert exits == []
        handler(signal.SIGINT, None)

    assert exits == [130]
    assert signal.getsignal(signal.SIGINT) == previous
    assert "STOPPING" in stream.getvalue()
    assert "FORCE EXIT" in stream.getvalue()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only overlapped I/O")
def test_windows_overlapped_reader_reads_at_an_explicit_offset(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"0123456789")
    reader = WindowsOverlappedReader(source)
    try:
        assert reader.read_at_cancellable(3, 4, budget=1.0) == b"3456"
    finally:
        reader.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only overlapped I/O")
def test_windows_budget_runs_while_readfile_call_is_blocked(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"0123456789")
    reader = WindowsOverlappedReader(
        source, cancel_grace=0.5, poll_interval=0.01
    )
    release = threading.Event()

    def blocked_read_file(*_args: object) -> bool:
        release.wait(timeout=1.0)
        ctypes.set_last_error(995)  # ERROR_OPERATION_ABORTED
        return False

    reader._kernel32.ReadFile = blocked_read_file
    reader._request_cancel = lambda _overlapped, _thread: release.set()  # type: ignore[method-assign]
    started = time.monotonic()
    try:
        with pytest.raises(ReadCancelledError) as raised:
            reader.read_at_cancellable(0, 4, budget=0.05)
    finally:
        reader.close()

    assert raised.value.reason == "budget"
    assert raised.value.cancellation_completed is True
    assert time.monotonic() - started < 1.0


@pytest.mark.skipif(os.name != "nt", reason="Windows-only overlapped I/O")
def test_rescue_uses_windows_backend_end_to_end(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    payload = bytes(range(16))
    source.write_bytes(payload)

    result = rescue(
        source,
        destination,
        progress=False,
        policy=small_policy(),
        through="survey",
        reader_backend="windows",
    )

    assert result.recovered_bytes == len(payload)
    assert destination.read_bytes() == payload
