# flvrescue

`flvrescue` is a fault-tolerant file recovery tool optimized for large FLV recordings on failing drives.

It recovers fast, readable ranges before revisiting slow or failed regions. The destination preserves source offsets, while a range map records both recovery state and the policy used for safe multi-pass resume.

> [!WARNING]
> Reading a failing drive can make its condition worse. If the data is irreplaceable, stop and consider a professional recovery service first. `flvrescue` does not repair drives, filesystems, or unreadable sectors.

## Quick start

Python 3.11 or newer is required. Runtime dependencies are limited to the standard library.

```powershell
py -m pip install flvrescue==0.3.0
flvrescue optimize damaged.flv rescued.flv
flvrescue damaged.flv rescued.flv
flvrescue resume rescued.flv --through slow
```

The source is opened read-only. By default, Pass 1 surveys the whole file and Pass 2 recovers likely-fast gaps. Resume state is saved beside the output as `rescued.flv.rescue.json`.

If the operation is interrupted, use `flvrescue resume rescued.flv`. The saved source path, destination path, and policy are validated before recovery continues, and committed recovered ranges are not read again.

`flvrescue optimize` is optional. It reads file metadata and destination free space, asks for one preference on an interactive terminal, and prints a recommended normal command. It never reads FLV contents or starts recovery.

## Main features

- Five passes prioritize Survey, Fast, Slow, Hard, then opt-in Deep recovery
- Successful reads retain fast/slow/hard difficulty in the range map
- A saved, versioned coverage policy keeps resumed behavior stable
- Non-mutating `optimize` recommendations for file size, free space, and priority
- `flvrescue status` occupancy view that does not read the source drive
- One source read at a time; no parallel reads against the failing disk
- Range-based atomic JSON map with safe v1/v2 migration
- Ctrl+C checkpointing and resumable partial output
- Cancellable Windows reads with saved, per-pass time budgets
- Threaded ANSI Live progress with a local offset bar while a source read is blocked
- Injectable Reader API for deterministic I/O-error testing

## Common commands

```powershell
# First, distribute time across the selected files.
flvrescue damaged.flv rescued.flv --through survey

# Default: Survey, then recover likely-fast gaps.
flvrescue damaged.flv rescued.flv

# Add a relatively safe slow-region pass.
flvrescue resume rescued.flv --through slow

# Explicitly spend more drive time on valuable files.
flvrescue resume rescued.flv --through hard
flvrescue resume rescued.flv --through deep

# Choose a non-default sidecar path when starting a new rescue.
flvrescue damaged.flv rescued.flv --map rescued.map.json --through survey

# Inspect a map without touching the failing drive
flvrescue status rescued.flv

# Route only unresolved bytes in a map to Deep; source and output stay unopened
flvrescue mark rescued.flv.rescue.json --offset 48G --length 512M --as defer
```

The default recovery flow is:

```text
Pass 1 Survey: sample the whole file and preserve fast data
Pass 2 Fast: read likely-fast survey gaps without pursuing slow areas
Pass 3 Slow: recover light-slow areas and Fast-pass leftovers
Pass 4 Hard: make one bounded coarse attempt per hard/error block
Pass 5 Deep: exhaustively localize every unresolved range
```

For a limited SSD, work on a group of files that fits: run the default Survey→Fast flow, move those outputs away, and then start the next group. Leave Slow, Hard, and especially Deep until broad coverage is secured. The legacy names `fill` and `retry` remain aliases for `fast` and `slow`.

## Documentation

- [Usage and command reference](docs/usage.md)
- [Safety and real-drive checklist](docs/safety.md)
- [Testing, damaged fixtures, and FFmpeg validation](docs/testing.md)

## Development

```powershell
py -m pip install -e ".[test]"
py -m pytest
```

On Windows, the default reader uses cancellable overlapped I/O and explicit file offsets. The portable fallback remains serial and safe to resume, but an operating-system read already in progress cannot be cancelled by Python. `flvrescue` is intentionally limited to ordinary files; it does not perform raw-device cloning, filesystem repair, SMART operations, or concurrent source reads.

## License

MIT
