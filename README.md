# flvrescue

`flvrescue` is a fault-tolerant file recovery tool optimized for large FLV recordings on failing drives.

It copies readable data in one forward pass, zero-fills unreadable regions, and preserves the source file's offsets and final size. The recovery algorithm is format-neutral, while the workflow is designed around large recordings that will later be inspected or processed with FFmpeg.

> [!WARNING]
> Reading a failing drive can make its condition worse. If the data is irreplaceable, stop and consider a professional recovery service first. `flvrescue` does not repair drives, filesystems, or unreadable sectors.

## Quick start

Python 3.11 or newer is required. Runtime dependencies are limited to the standard library.

```powershell
py -m pip install .
flvrescue damaged.flv rescued.flv
```

The source is opened read-only. By default, resume state is saved beside the output as `rescued.flv.rescue.json`.

If the operation is interrupted, run the same command again. The saved map is validated before recovery continues, and the completed prefix is not read again.

## Main features

- Sequential, forward-only recovery with no parallel reads
- 8 MiB reads, localized to 64 KiB and then 4 KiB after errors
- Zero-fill for unreadable final ranges while preserving logical offsets
- Atomic JSON resume map with merged bad-range records
- Ctrl+C checkpointing and resumable partial output
- Progress reporting for speed, recovered bytes, unreadable bytes, and elapsed time
- Injectable Reader API for deterministic I/O-error testing

## Common commands

```powershell
# Choose the resume-map location
flvrescue damaged.flv rescued.flv --map rescued.map.json

# Explicitly select the recovery hierarchy
flvrescue damaged.flv rescued.flv --block 8M --fallback 64K --sector 4K

# Disable periodic progress output
flvrescue damaged.flv rescued.flv --no-progress
```

The default recovery flow is:

```text
8 MiB read succeeds  -> write it
8 MiB read fails     -> read that range in 64 KiB pieces
64 KiB read fails    -> read that piece in 4 KiB pieces
4 KiB read fails     -> write zeroes and record the bad range
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

`flvrescue` is currently Windows-focused and intentionally limited to ordinary files. It does not perform raw-device cloning, filesystem repair, automatic bad-region retry passes, SMART operations, or concurrent reads.

## License

MIT
