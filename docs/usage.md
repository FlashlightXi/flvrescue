# Usage and command reference

## Installation

Install `flvrescue` with Python 3.11 or newer:

```powershell
py -m pip install .
```

The program has no third-party runtime dependencies.

## Basic recovery

```powershell
flvrescue damaged.flv rescued.flv
```

The source is opened read-only. The destination must be a different file on healthy, writable storage. A new destination is never silently overwritten.

The default resume map is created as `rescued.flv.rescue.json`. Keep it with the partial output until recovery and validation are complete.

## Resume

After Ctrl+C, a crash, or another interruption, repeat the same command:

```powershell
flvrescue damaged.flv rescued.flv
```

Before resuming, `flvrescue` verifies the source path and size, destination path, pass cursor, and complete range map. It schedules only ranges that remain eligible for the current pass. Existing v1 maps are migrated when they are next checkpointed.

Do not edit the JSON map manually. A malformed or mismatched map is rejected. The map path must not refer to the source or destination, including through an existing hard link.

## Options

| Option | Default | Purpose |
|---|---:|---|
| `--map PATH` | `DEST.rescue.json` | Select the sidecar resume map |
| `--block SIZE` | `8M` | Normal read size |
| `--fallback SIZE` | `64K` | Read size after a normal-block error |
| `--sector SIZE` | `4K` | Final read and zero-fill unit |
| `--slow-threshold SECONDS` | `2.0` | Successful read duration treated as slow |
| `--skip-start SIZE` | `8M` | Initial adaptive skip width after a slow or failed read |
| `--skip-factor N` | `2` | Multiply skip width after each slow/error |
| `--skip-max SIZE` | `1G` | Maximum adaptive skip width |
| `--skip-reset-after N` | `1` | Consecutive fast reads required before leaving skip mode |
| `--survey-stride SIZE` | `0` | Pass 1 whole-file sample skip after each fast read; `0` disables |
| `--max-pass {1,2,3}` | `2` | Last pass to execute; Pass 3 is optional deep recovery |
| `--no-progress` | off | Suppress periodic progress output |

Sizes accept byte counts or `K`, `M`, `G`, and `T` suffixes (1024-based). `8M`, `128MiB`, `64K`, and `1G` are valid. The read sizes must satisfy:

```text
block >= fallback >= sector > 0
```

Examples:

```powershell
flvrescue damaged.flv rescued.flv --map rescued.map.json

flvrescue damaged.flv rescued.flv `
  --block 8M `
  --fallback 64K `
  --sector 4K `
  --slow-threshold 2 `
  --max-pass 2

# Coarse Pass 1: sample the file, grow skips quickly, stay in skip mode
# until several fast reads in a row
flvrescue damaged.flv rescued.flv `
  --max-pass 1 `
  --survey-stride 128M `
  --skip-start 128M `
  --skip-factor 2 `
  --skip-max 1G `
  --skip-reset-after 8
```

Run `flvrescue --help` for the installed version's complete CLI syntax.

## Progress and summary

On a TTY, a dedicated renderer keeps a compact live display at the bottom of the terminal. Important slow, error, skip, rediscovery, and pass-change events remain visible above it. Rendering reads shared memory only and never touches the source drive.

```text
Pass 1 Fast rescue | Elapsed 12:21 | Progress 78.4% | reading
0% ##########!!!!!!!!!!!!!!...................... 100%
# recovered  ~ slow-ok  . likely-good skip  ! slow/error skip  x unread  ? pending  * reading
Speed 112.8 MiB/s | Recovered 78.4 GiB | Likely-good skip 12.0 GiB | Slow/error skip 64.0 MiB | Unreadable 8.0 MiB
Read: offset 84288733184 + 8.0 MiB | waiting 0.4s
```

The map spans the whole file from 0% to 100%. `.` is a survey skip (Pass 1 jumped because the last read was fast). `!` is a skip after a slow or failed read. Pass 2 fills `.` first, then retries `!`.

Non-TTY streams receive ordinary line-based logs. The final summary reports recovered, likely-good skips, slow/error skips, unreadable, and unprocessed bytes, plus the next available pass and map path.

## Library API

```python
from flvrescue import rescue

result = rescue(
    "damaged.flv",
    "rescued.flv",
    map_path="rescued.map.json",
)

print(result.recovered_bytes)
print(result.unreadable_bytes)
```

Advanced callers can provide a `reader_factory`. Its Reader implements:

```python
def read_at(offset: int, size: int) -> bytes:
    ...
```

This is used by the test suite to raise controlled `OSError` instances or delay selected reads without touching a damaged drive. Pass limits, slow threshold, skip start/factor/max, skip-reset-after, survey stride, read sizes, checkpoint interval, and a custom progress sink can also be supplied.

Library calls checkpoint and then re-raise `KeyboardInterrupt`. The CLI catches that interruption, prints a resume message, and exits without a traceback.

## Resume-map format

The v2 JSON map stores the active pass, its cursor, adaptive skip state, and a complete set of non-overlapping ranges:

```json
{
  "version": 2,
  "source_path": "damaged.flv",
  "source_size": 107374182400,
  "destination_path": "rescued.flv",
  "current_pass": 2,
  "pass_cursor": 52428800,
  "adaptive_skip": 8388608,
  "ranges": [
    {"offset": 0, "length": 52428800, "status": "recovered"},
    {"offset": 52428800, "length": 8388608, "status": "skipped", "cause": "slow"},
    {"offset": 60817408, "length": 107313364992, "status": "unprocessed"}
  ]
}
```

Statuses are `recovered`, `skipped`, `unreadable`, and `unprocessed`. Adjacent ranges with identical status and cause are merged. Paths are stored canonically.
