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
- Pass 2 recovery limited to ranges skipped by the Fast Pass
- Optional Pass 3 localization from 8 MiB to 64 KiB and 4 KiB
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
flvrescue damaged.flv rescued.flv --max-pass 3

# Tune slow detection and adaptive skipping
flvrescue damaged.flv rescued.flv --slow-threshold 2 --skip-start 8M --skip-max 1G

# Disable periodic progress output
flvrescue damaged.flv rescued.flv --no-progress
```

The default recovery flow is:

```text
Pass 1: fast read succeeds -> write it
        slow/error         -> preserve any data, skip ahead, and probe
Pass 2: revisit skipped ranges with smaller probes
Pass 3: optional 8 MiB -> 64 KiB -> 4 KiB deep localization
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
