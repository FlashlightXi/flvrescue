# Usage and command reference

## Installation

Install `flvrescue` with Python 3.11 or newer:

```powershell
py -m pip install .
```

The program has no third-party runtime dependencies.

## Basic recovery

```powershell
flvrescue damaged.flv rescued.flv --through survey
```

The source is opened read-only. The destination must be a different file on healthy, writable storage. On Windows it must support sparse files (normally NTFS or ReFS); capacity-unsafe fallback to a non-sparse destination is refused. A new destination is never silently overwritten.

The default resume map is created as `rescued.flv.rescue.json`. It stores the coverage policy as well as recovery ranges; keep it with the partial output until recovery and validation are complete.

## Optional command recommendation

Use `optimize` when you want a command chosen from the file size, destination free space, and one operational preference:

```powershell
flvrescue optimize damaged.flv rescued.flv
flvrescue optimize damaged.flv rescued.flv --preference fast --no-input
```

On an interactive terminal, omitting `--preference` asks once for `fast`, `balanced`, or `thorough`. In scripts and redirected sessions it defaults to `balanced`. `--available-space 100G` can model a batch capacity different from the destination's currently detected free space.

This command is deliberately advisory: it reads file metadata, not source contents; it does not create the destination or map; and it does not start recovery. Copy and run the printed ordinary `flvrescue SOURCE DEST ...` command. The resulting policy is then stored in the map as usual.

If the detected or supplied free space is below the estimated Survey writes plus a safety reserve, `optimize` prints the required minimum and exits without generating a command.

`--through` selects the furthest stage this invocation may run. Its default is `fill`, so `flvrescue damaged.flv rescued.flv` performs Survey then Fill with the standard coverage policy.

| Stage | Purpose | When to use it |
|---|---|---|
| `survey` / `1` | Pass 1 Survey: sample across the whole file and preserve fast ranges | First step for every selected file |
| `fill` / `2` | Pass 2 Fill: read likely-good survey gaps | Second step for every selected file |
| `retry` / `3` | Pass 3 Retry: attempt slow/error gaps again | Only after overall coverage is secured |
| `deep` / `4` | Pass 4 Deep: progressively smaller reads around stubborn gaps | Only for the highest-value files with time left |

## Resume

After Ctrl+C, a crash, or another interruption, resume from the output or its map:

```powershell
flvrescue resume rescued.flv --through fill
flvrescue resume rescued.flv.rescue.json --through fill
```

When given a destination, `resume` accepts only the adjacent `DEST.rescue.json` map (or the exact map passed through `--map`). It verifies that `map.destination_path` matches the supplied destination before any recovery I/O. When given a `.json` map, it uses that exact map and its saved source and destination. It never falls back to another destination.

Before resuming, `flvrescue` verifies the source path and size, destination path, pass cursor, saved policy, and complete range map. It schedules only ranges that remain eligible for the requested pass. Existing maps without a saved policy are visibly adopted into the current coverage policy once; later resumes use the saved policy.

Do not edit the JSON map manually. A malformed or mismatched map is rejected. The map path must not refer to the source or destination, including through an existing hard link.

## Options

| Option | Default | Purpose |
|---|---:|---|
| `--map PATH` | `DEST.rescue.json` | Select the sidecar resume map |
| `--through {survey,fill,retry,deep,1,2,3,4}` | `fill` | Furthest named recovery stage to run |
| `--max-pass {1,2,3,4}` | — | Legacy numeric alias for `--through`; cannot be combined with it |
| `--block SIZE` | saved policy | Advanced normal-read override for a new run |
| `--fallback SIZE` | saved policy | Advanced read size after a normal-block error |
| `--sector SIZE` | saved policy | Advanced final read and zero-fill unit |
| `--checkpoint SIZE` | saved policy | Advanced durable checkpoint interval |
| `--slow-threshold SECONDS` | saved policy | Advanced successful-read duration treated as slow |
| `--skip-start`, `--skip-factor`, `--skip-max`, `--skip-reset-after` | saved policy | Advanced adaptive-skip overrides for a new run |
| `--survey-stride SIZE` | saved policy | Advanced Pass 1 sampling override for a new run |
| `--no-progress` | off | Suppress periodic progress output |

The detailed controls are intentionally unavailable on `resume`: a different policy must not silently change an in-progress rescue. To use an advanced policy, specify the overrides on the initial command; they are then saved in the map.

Sizes accept byte counts or `K`, `M`, `G`, and `T` suffixes (1024-based). `8M`, `128MiB`, `64K`, and `1G` are valid. The effective read sizes must satisfy:

```text
block >= fallback >= sector > 0
```

Examples:

```powershell
flvrescue damaged.flv rescued.flv --map rescued.map.json

# Non-default strategy; it will be saved and reused by resume.
flvrescue damaged.flv rescued.flv `
  --through survey `
  --survey-stride 128M `
  --skip-start 128M `
  --skip-factor 2 `
  --skip-max 1G `
  --skip-reset-after 8
```

Run `flvrescue --help` for the installed version's complete CLI syntax.

## Progress and summary

On a TTY, rescue uses a stacked bar, not an offset map. Its header names the current stage:

```text
FLVRESCUE Pass 2 Fill reads 493.flv (26.0 GiB)
████▒▒▒▒▒▒▒              3.5 GiB 02:55
good 1.7 GiB fast 10.5 GiB slow 7.3 GiB bad 332.4 MiB
```

During destination preparation, the display deliberately has no percentage because file allocation does not expose a trustworthy byte-by-byte completion value:

```text
FLVRESCUE prepares rescued.flv
Preparing destination / 00:05
Source extent: 76.0 GiB
Storage mode: sparse
```

- `█` good / recovered (green)
- `▒` likely-good skip (blue), then slow/error skip (yellow)
- `░` unreadable (red)
- space: not yet classified

Labels on the third line are gray; the numbers use the same colors as the bar.

Pass 1 Survey scans. Pass 2 Fill reads blue likely-good skips. Pass 3 Retry tries yellow slow/error skips. Pass 4 Deep is the optional fine-grained stage.

To see the file in offset order, filling almost the whole terminal:

```powershell
flvrescue status rescued.flv
flvrescue status rescued.flv.rescue.json
```

That command does not read the failing drive. It only renders the map: many rows of `█▒░` from 0% to 100%, then two or three summary lines. `flvrescue analyze` is the same command. Pass the rescued file or the `.rescue.json` map; the FLV bytes themselves are not opened.

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

This is used by the test suite to raise controlled `OSError` instances or delay selected reads without touching a damaged drive. A `RecoveryPolicy` can be supplied for a new run; advanced `rescue()` keyword overrides are optional and are persisted with that policy. `max_pass` remains the library spelling for the requested last pass.

Library calls checkpoint and then re-raise `KeyboardInterrupt`. The CLI catches that interruption, prints a resume message, and exits without a traceback.

## Resume-map format

The v2 JSON map stores the policy, active pass, cursor, adaptive skip state, and a complete set of non-overlapping ranges:

```json
{
  "version": 2,
  "source_path": "damaged.flv",
  "source_size": 107374182400,
  "destination_path": "rescued.flv",
  "policy": {"version": 1, "profile": "coverage", "block": 8388608, "...": "..."},
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
