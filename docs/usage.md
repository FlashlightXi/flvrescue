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

Before resuming, `flvrescue` verifies the source path and size, destination path, completed offset, and bad ranges. It resumes at `completed_until` without rereading the committed prefix.

Do not edit the JSON map manually. A malformed or mismatched map is rejected. The map path must not refer to the source or destination, including through an existing hard link.

## Options

| Option | Default | Purpose |
|---|---:|---|
| `--map PATH` | `DEST.rescue.json` | Select the sidecar resume map |
| `--block SIZE` | `8M` | Normal read size |
| `--fallback SIZE` | `64K` | Read size after a normal-block error |
| `--sector SIZE` | `4K` | Final read and zero-fill unit |
| `--no-progress` | off | Suppress periodic progress output |

Sizes accept byte counts or `K`, `M`, and `G` suffixes. The values must satisfy:

```text
block >= fallback >= sector > 0
```

Examples:

```powershell
flvrescue damaged.flv rescued.flv --map rescued.map.json

flvrescue damaged.flv rescued.flv `
  --block 8M `
  --fallback 64K `
  --sector 4K
```

Run `flvrescue --help` for the installed version's complete CLI syntax.

## Progress and summary

Progress is printed at a low frequency so display updates do not add source-drive I/O:

```text
File: damaged.flv | 78.4 GiB / 100.0 GiB ( 78.4%) | Speed: 112.8 MiB/s (avg 108.2 MiB/s) | Elapsed: 12:21 | Recovered: 78.4 GiB | Unreadable: 16.0 KiB | Bad ranges: 3
```

The final summary reports recovered bytes, unreadable bytes, merged bad-range count, output path, and map path.

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

This is used by the test suite to raise controlled `OSError` instances without touching a damaged drive. `block_size`, `fallback_size`, `sector_size`, `checkpoint_interval`, and a custom progress sink can also be supplied.

Library calls checkpoint and then re-raise `KeyboardInterrupt`. The CLI catches that interruption, prints a resume message, and exits without a traceback.

## Resume-map format

The compact v1 JSON map stores:

```json
{
  "version": 1,
  "source_path": "damaged.flv",
  "source_size": 107374182400,
  "destination_path": "rescued.flv",
  "completed_until": 84288733184,
  "bad_ranges": [
    {"offset": 52428800, "length": 4096}
  ]
}
```

Paths are stored canonically. Adjacent and overlapping bad ranges are merged.
