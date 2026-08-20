# Testing and FFmpeg validation

## Automated tests

Install the test dependency and run the suite:

```powershell
py -m pip install -e ".[test]"
py -m pytest
```

The tests use an injectable Reader that raises `OSError` whenever a requested read intersects a configured bad range. This tests actual error handling rather than merely changing readable file bytes.

Coverage includes:

- Normal byte-for-byte copy and hash equality
- A short final normal block
- The default 8 MiB -> 64 KiB -> 4 KiB hierarchy
- 4 KiB and 64 KiB aligned unreadable ranges
- Multiple, adjacent, merged, and block-boundary ranges
- Zero-filling only the final unreadable units
- Recovery of readable data after damaged ranges
- Ctrl+C checkpointing and forward-only resume
- Malformed or mismatched maps and unsafe path aliases
- Generated FLV fixtures and FFmpeg decode checks

## Generate a test FLV

FFmpeg must be available on `PATH`:

```powershell
py tools/generate_test_flv.py sample.flv --duration 10
```

The generator uses a synthetic video source and never needs an input recording.

## Generate damaged copies

Each command copies `sample.flv` first and modifies only the named destination:

```powershell
py tools/corrupt_flv.py sample.flv sample.zero4k.flv --mode zero-4k
py tools/corrupt_flv.py sample.flv sample.zero64k.flv --mode zero-64k
py tools/corrupt_flv.py sample.flv sample.random.flv --mode random --seed 42
py tools/corrupt_flv.py sample.flv sample.truncated.flv --mode truncate
py tools/corrupt_flv.py sample.flv sample.multiple.flv --mode multiple
```

These corruption fixtures remain readable by the operating system. They test media-container behavior, not the rescue engine's `OSError` fallback. The injectable Reader tests serve that separate purpose.

## Inspect a rescued FLV

Check streams and container metadata:

```powershell
ffprobe -v error -show_format -show_streams rescued.flv
```

Run a decode pass without creating another media file:

```powershell
ffmpeg -hide_banner -v warning `
  -fflags +discardcorrupt+genpts `
  -err_detect ignore_err `
  -i rescued.flv `
  -f null -
```

Alternatively, try a remux on the rescued copy:

```powershell
ffmpeg -fflags +discardcorrupt+genpts -err_detect ignore_err `
  -i rescued.flv -map 0 -c copy rescued.remux.flv
```

Record more than the process exit code:

- The first warning or decode error
- Whether decoding resumes after the damaged range
- The retained duration
- The visible or audible corruption interval
- Whether timestamps remain usable for the intended transcode

FFmpeg behavior depends on where the unreadable bytes intersect FLV tags, codecs, and timestamps. A passing synthetic fixture does not guarantee the same outcome for a real recording.
