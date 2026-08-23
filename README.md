# flvrescue

`flvrescue` is a fault-tolerant file recovery tool optimized for large FLV recordings on failing drives.

It recovers fast, readable ranges before revisiting slow or failed regions. The destination preserves source offsets, while a range map records both recovery state and the policy used for safe multi-pass resume.

> [!WARNING]
> Reading a failing drive can make its condition worse. If the data is irreplaceable, stop and consider a professional recovery service first. `flvrescue` does not repair drives, filesystems, or unreadable sectors.

## Quick start

Python 3.11 or newer is required. Runtime dependencies are limited to the standard library.

```powershell
py -m pip install .
flvrescue optimize damaged.flv rescued.flv
flvrescue damaged.flv rescued.flv --through survey
flvrescue resume rescued.flv --through fill
```

The source is opened read-only. The default coverage policy makes Pass 1 sample the whole file, then Pass 2 fill likely-good gaps. Resume state is saved beside the output as `rescued.flv.rescue.json`.

If the operation is interrupted, use `flvrescue resume rescued.flv`. The saved source path, destination path, and policy are validated before recovery continues, and committed recovered ranges are not read again.

`flvrescue optimize` is optional. It reads file metadata and destination free space, asks for one preference on an interactive terminal, and prints a recommended normal command. It never reads FLV contents or starts recovery.

## Main features

- Pass 1 Survey samples the whole file; Pass 2 Fill reads likely-good gaps
- Pass 3 Retry tries slow/error gaps once more; Pass 4 Deep localizes stubborn gaps
- A saved, versioned coverage policy keeps resumed behavior stable
- Non-mutating `optimize` recommendations for file size, free space, and priority
- `flvrescue status` occupancy view that does not read the source drive
- One source read at a time; no parallel reads against the failing disk
- Range-based atomic JSON map with v1 map migration
- Ctrl+C checkpointing and resumable partial output
- Threaded ANSI Live progress that continues while a source read is blocked
- Injectable Reader API for deterministic I/O-error testing

## Common commands

```powershell
# First, distribute time across the selected files.
flvrescue damaged.flv rescued.flv --through survey

# Then fill the gaps that were likely readable.
flvrescue resume rescued.flv --through fill

# Spend remaining time only on the most valuable files.
flvrescue resume rescued.flv --through retry
flvrescue resume rescued.flv --through deep

# Choose a non-default sidecar path when starting a new rescue.
flvrescue damaged.flv rescued.flv --map rescued.map.json --through survey

# Inspect a map without touching the failing drive
flvrescue status rescued.flv
```

The default recovery flow is:

```text
Pass 1 Survey: sample the whole file and preserve fast data
Pass 2 Fill: read likely-good survey gaps
Pass 3 Retry: try slow/error gaps again without deep localization
Pass 4 Deep: use progressively smaller reads for selected stubborn gaps
```

For a limited SSD, work on a group of files that fits: Survey each file, Fill each file, then move the recovered files away before starting the next group. Leave Retry and Deep until overall coverage is secured.

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
