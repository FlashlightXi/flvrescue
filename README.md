# flvrescue

`flvrescue` is a fault-tolerant file recovery tool optimized for large FLV recordings on failing drives.

It recovers fast, readable ranges before revisiting slow or failed regions. The destination preserves the source file's offsets and final size, while a range map supports safe multi-pass resume.

> [!WARNING]
> Reading a failing drive can make its condition worse. If the data is irreplaceable, stop and consider a professional recovery service first. `flvrescue` does not repair drives, filesystems, or unreadable sectors.

## Quick start

Python 3.11 or newer is required. Runtime dependencies are limited to the standard library.

```powershell
py -m pip install .
flvrescue damaged.flv rescued.flv
```

The source is opened read-only. By default, resume state is saved beside the output as `rescued.flv.rescue.json`.

If the operation is interrupted, run the same command again. The saved map is validated before recovery continues, and committed recovered ranges are not read again.

## Main features

- Fast Pass with timed reads, probes, and adaptive skipping
- Pass 2 fills likely-good (survey) skips; Pass 3 retries slow/error skips
- Optional Pass 4 localization from 8 MiB to 64 KiB and 4 KiB
- `flvrescue status` occupancy view that does not read the source drive
- One source read at a time; no parallel reads against the failing disk
- Range-based atomic JSON map with v1 map migration
- Ctrl+C checkpointing and resumable partial output
- Threaded ANSI Live progress that continues while a source read is blocked
- Injectable Reader API for deterministic I/O-error testing

## Common commands

```powershell
# Choose the resume-map location
flvrescue damaged.flv rescued.flv --map rescued.map.json

# Explicitly select the recovery hierarchy
flvrescue damaged.flv rescued.flv --block 8M --fallback 64K --sector 4K

# Stop after the fastest pass, or opt into deep recovery
flvrescue damaged.flv rescued.flv --max-pass 1
flvrescue damaged.flv rescued.flv --max-pass 4

# Tune slow detection and adaptive skipping
flvrescue damaged.flv rescued.flv --slow-threshold 2 --skip-start 8M --skip-max 1G

# Coarse whole-file Pass 1 (K/M/G/T suffixes, 1024-based)
flvrescue damaged.flv rescued.flv --max-pass 1 --survey-stride 128M --skip-start 128M --skip-factor 2 --skip-reset-after 8

# Inspect a map without touching the failing drive
flvrescue status rescued.flv
```

The default recovery flow is:

```text
Pass 1: sample the file; keep fast data; mark likely-good vs slow/error skips
Pass 2: fill likely-good (fast) skips
Pass 3: retry slow/error skips
Pass 4: optional 8 MiB -> 64 KiB -> 4 KiB deep localization
```

## Documentation

- [Usage and command reference](docs/usage.md)
- [Safety and real-drive checklist](docs/safety.md)
- [Testing, damaged fixtures, and FFmpeg validation](docs/testing.md)

## Development

```powershell
py -m pip install -e ".[test]"
py -m pytest
```

`flvrescue` is currently Windows-focused and intentionally limited to ordinary files. It does not perform raw-device cloning, filesystem repair, SMART operations, forced read cancellation, or concurrent source reads.

## License

MIT
