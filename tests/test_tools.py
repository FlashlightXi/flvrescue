from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FaultInjectingReader


ROOT = Path(__file__).parents[1]
GENERATE = ROOT / "tools" / "generate_test_flv.py"
CORRUPT = ROOT / "tools" / "corrupt_flv.py"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
def test_generate_flv_and_create_all_documented_corruption_scenarios(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    subprocess.run(
        [sys.executable, str(GENERATE), str(source), "--duration", "1", "--size", "64x48", "--rate", "12"],
        check=True,
    )
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).digest()
    assert source_bytes.startswith(b"FLV")

    for mode in ("zero-4k", "zero-64k", "random", "truncate", "multiple"):
        destination = tmp_path / f"{mode}.flv"
        subprocess.run(
            [sys.executable, str(CORRUPT), str(source), str(destination), "--mode", mode],
            check=True,
        )
        assert destination.is_file()
        assert hashlib.sha256(source.read_bytes()).digest() == source_hash
        if mode == "truncate":
            assert destination.stat().st_size < len(source_bytes)
        else:
            assert destination.stat().st_size == len(source_bytes)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
def test_generated_flv_decodes_with_ffmpeg(tmp_path: Path) -> None:
    source = tmp_path / "source.flv"
    subprocess.run(
        [sys.executable, str(GENERATE), str(source), "--duration", "1", "--size", "64x48"],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-i", str(source), "-f", "null", "-"],
        check=True,
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
def test_fault_injected_rescue_flv_is_accepted_by_ffmpeg(tmp_path: Path) -> None:
    """Test E: a Reader I/O error produces a zero-filled FLV FFmpeg can inspect."""
    from flvrescue import rescue

    source = tmp_path / "source.flv"
    destination = tmp_path / "rescued.flv"
    map_path = tmp_path / "rescued.map.json"
    subprocess.run(
        [sys.executable, str(GENERATE), str(source), "--duration", "2", "--size", "160x90", "--rate", "12"],
        check=True,
    )
    payload = source.read_bytes()
    sector_size = 16
    bad_offset = (len(payload) // 2 // sector_size) * sector_size
    reader = FaultInjectingReader(payload, bad_ranges=[(bad_offset, sector_size)])

    rescue(
        source,
        destination,
        map_path=map_path,
        reader_factory=lambda _source: reader,
        block_size=2048,
        fallback_size=128,
        sector_size=sector_size,
        max_pass=5,
    )

    rescued = destination.read_bytes()
    expected = bytearray(payload)
    expected[bad_offset : bad_offset + sector_size] = b"\0" * sector_size
    assert rescued == bytes(expected)
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-fflags",
            "+discardcorrupt+genpts",
            "-err_detect",
            "ignore_err",
            "-i",
            str(destination),
            "-f",
            "null",
            "-",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
