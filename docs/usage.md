# Usage and command reference

## Installation

Install `flvrescue` with Python 3.11 or newer:

```powershell
py -m pip install flvrescue==0.3.0
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

`--through` selects the furthest stage this invocation may run. Its default is `fast`, so `flvrescue damaged.flv rescued.flv` performs Survey then Fast with the standard coverage policy.

| Stage | Purpose | When to use it |
|---|---|---|
| `survey` / `1` | Pass 1 Survey: sample across the whole file and preserve fast ranges | First step for every selected file |
| `fast` / `2` | Pass 2 Fast: read likely-fast survey gaps and stop at slow reads | Default second step for every selected file |
| `slow` / `3` | Pass 3 Slow: recover light-slow ranges and Fast leftovers | Relatively safe additional recovery |
| `hard` / `4` | Pass 4 Hard: one bounded coarse attempt per hard/error block | Explicit, higher-load recovery |
| `deep` / `5` | Pass 5 Deep: progressively localize every unresolved range | Explicit exhaustive recovery for the highest-value files |

`fill` remains an alias for `fast`, and `retry` remains an alias for `slow`. Named stages are preferred because numeric Pass 4 changed from Deep in v0.2.0 to Hard in v0.3.0.

## Resume

After Ctrl+C, a crash, or another interruption, resume from the output or its map:

```powershell
flvrescue resume rescued.flv --through fast
flvrescue resume rescued.flv.rescue.json --through slow
```

When given a destination, `resume` accepts only the adjacent `DEST.rescue.json` map (or the exact map passed through `--map`). It verifies that `map.destination_path` matches the supplied destination before any recovery I/O. When given a `.json` map, it uses that exact map and its saved source and destination. It never falls back to another destination.

Before resuming, `flvrescue` verifies the source path and size, destination path, pass cursor, saved policy, and complete range map. It schedules only ranges that remain eligible for the requested pass. Existing maps without a saved policy are visibly adopted into the current coverage policy once; later resumes use the saved policy.

Do not edit the JSON map manually. A malformed or mismatched map is rejected. The map path must not refer to the source or destination, including through an existing hard link.

## Options

| Option | Default | Purpose |
|---|---:|---|
| `--map PATH` | `DEST.rescue.json` | Select the sidecar resume map |
| `--through {survey,fast,slow,hard,deep,1,2,3,4,5}` | `fast` | Furthest named recovery stage to run |
| `--max-pass {1,2,3,4,5}` | — | Legacy numeric alias for `--through`; cannot be combined with it |
| `--block SIZE` | saved policy | Advanced normal-read override for a new run |
| `--slow-block SIZE` | 1 MiB | Advanced Pass 3 read size for a new run |
| `--hard-block SIZE` | 64 KiB | Advanced Pass 4 read size for a new run |
| `--fallback SIZE` | saved policy | Advanced read size after a normal-block error |
| `--sector SIZE` | saved policy | Advanced final read and zero-fill unit |
| `--checkpoint SIZE` | saved policy | Advanced durable checkpoint interval |
| `--slow-threshold SECONDS` | saved policy | Advanced successful-read duration treated as slow |
| `--hard-threshold SECONDS` | saved policy | Advanced successful-read duration treated as hard; must exceed the slow threshold |
| `--survey-budget SECONDS` | 5 | Maximum time for one Pass 1 read before Windows cancellation |
| `--fast-budget SECONDS` | 5 | Maximum time for one Pass 2 read before Windows cancellation |
| `--slow-budget SECONDS` | 30 | Maximum time for one Pass 3 read before Windows cancellation |
| `--hard-budget SECONDS` | 120 | Maximum time for one Pass 4 read before Windows cancellation |
| `--deep-budget SECONDS` | 600 | Maximum time for one Pass 5 read before Windows cancellation |
| `--skip-start`, `--skip-factor`, `--skip-max`, `--skip-reset-after` | saved policy | Advanced adaptive-skip overrides for a new run |
| `--survey-stride SIZE` | saved policy | Advanced Pass 1 sampling override for a new run |
| `--no-progress` | off | Suppress periodic progress output |
| `--reader {auto,windows,portable}` | `auto` | Select cancellable Windows I/O or the serial portable fallback |

The detailed controls are intentionally unavailable on `resume`: a different policy must not silently change an in-progress rescue. To use an advanced policy, specify the overrides on the initial command; they are then saved in the map.

Sizes accept byte counts or `K`, `M`, `G`, and `T` suffixes (1024-based). `8M`, `128MiB`, `64K`, and `1G` are valid. The effective read sizes must satisfy:

```text
block >= fallback >= sector > 0
block >= slow-block >= hard-block >= sector
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

On a TTY, the stage header is printed once. It is not part of the repeatedly redrawn Live region:

```text
FLVRESCUE Pass 2 Fast reads 493.flv (126.8 GiB)
```

The Live region retains the whole-file stacked bar and totals, then shows a spatial window around the active read:

```text
████████▓▓▒▒░×··                   48.6 GiB 02:55
good 41.2 GiB fast 5.8 GiB slow 1.4 GiB hard 128.0 MiB bad 64.0 MiB
                                ↓ 48.620 GiB (8 MiB)  HARD 00:23.6
48.50 GiB  █████████▓▓▒▒▶▒░×··██████████  49.00 GiB
```

During destination preparation, the display deliberately has no percentage because file allocation does not expose a trustworthy byte-by-byte completion value:

```text
FLVRESCUE prepares rescued.flv
Preparing destination / 00:05
Source extent: 76.0 GiB
Storage mode: sparse
```

- `█` good / recovered (green)
- `▓` fast candidate or fast recovery (blue)
- `▒` slow range (yellow)
- `░` hard range (magenta)
- `×` unreadable (red)
- `·` not yet classified
- `▶` current read; the arrow above it reports offset, request size, state, and elapsed read time

Labels on the third line are gray; the numbers use the same colors as the bar.

The read state changes from `READING` to `SLOW` and then `HARD` while a read is still pending. A budget expiry requests cancellation and leaves the range eligible for a later pass; it is not falsely recorded as unreadable.

Pass 1 Survey samples. Pass 2 Fast reads likely-fast candidates. Pass 3 Slow handles light-slow ranges. Pass 4 Hard makes bounded coarse attempts. Pass 5 Deep is the explicit fine-grained exhaustive stage.

To see the file in offset order, filling almost the whole terminal:

```powershell
flvrescue status rescued.flv
flvrescue status rescued.flv.rescue.json
```

That command does not read the failing drive. It only renders the map: many rows of `█▒░` from 0% to 100%, then two or three summary lines. `flvrescue analyze` is the same command. Pass the rescued file or the `.rescue.json` map; the FLV bytes themselves are not opened.

## Manual range routing

Use `mark` when observation outside the program tells you that an unresolved map range should start at a later pass:

```powershell
flvrescue mark rescued.flv.rescue.json --offset 48G --length 512M --as slow
flvrescue mark rescued.flv.rescue.json --offset 48G --length 512M --as hard
flvrescue mark rescued.flv.rescue.json --offset 48G --length 512M --as defer
flvrescue mark rescued.flv.rescue.json --offset 48G --length 512M --clear
```

`slow`, `hard`, and `defer` route unresolved bytes to Pass 3, Pass 4, and Pass 5 respectively. `clear` returns them to normal scheduling. Recovered bytes are preserved. The command updates only the exact map atomically; it does not open the source or destination data files.

## Stopping safely

On the CLI, the first Ctrl+C requests cancellation of the active Windows read, checkpoints the map, and stops before starting another source read. Press Ctrl+C a second time only when immediate process exit is necessary. That second exit cannot promise that the drive or its controller has physically stopped I/O; wait for drive activity to settle before disconnecting hardware.

With `--reader auto`, Windows uses explicit-offset overlapped reads and `CancelIoEx`. Other platforms use the serial portable reader. The portable reader still checkpoints between reads, but a blocking operating-system read already in progress cannot be interrupted by the time budget.

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

Library callers can pass a `StopController` and use the cancellable reader contract. The CLI owns the two-stage Ctrl+C policy and returns after the map is checkpointed.

## Resume-map format

The v3 JSON map stores the policy, active pass, cursor, adaptive skip state, and a complete set of non-overlapping ranges:

```json
{
  "version": 3,
  "source_path": "damaged.flv",
  "source_size": 107374182400,
  "destination_path": "rescued.flv",
  "policy": {"version": 3, "profile": "coverage", "slow_threshold": 2.0, "hard_threshold": 10.0, "fast_budget": 5.0, "hard_budget": 120.0, "...": "..."},
  "current_pass": 2,
  "pass_cursor": 52428800,
  "adaptive_skip": 8388608,
  "ranges": [
    {"offset": 0, "length": 52428800, "status": "recovered", "difficulty": "fast"},
    {"offset": 52428800, "length": 8388608, "status": "skipped", "cause": "fast_pass", "difficulty": "slow"},
    {"offset": 60817408, "length": 107313364992, "status": "unprocessed"}
  ]
}
```

Statuses are `recovered`, `skipped`, `unreadable`, and `unprocessed`. Difficulty is independently stored as `fast`, `slow`, `hard`, or `failure`; legacy recovered ranges may remain unclassified rather than being guessed. Adjacent ranges merge only when status, cause, and difficulty all match. Map v1/v2 and policy v1/v2 are migrated before the first resumed source read, without rereading recovered ranges.
