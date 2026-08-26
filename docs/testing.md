# Testing and FFmpeg validation

## Automated tests

Install the test dependency and run the suite:

```powershell
py -m pip install -e ".[test]"
py -m pytest
py -m flvrescue optimize --help
```

The tests use injectable Readers that raise `OSError`, really delay selected reads, or deterministically advance a fake clock for fast/slow/hard regions. This tests latency-priority behavior without making the suite wait for long reads.

Coverage includes:

- Normal byte-for-byte copy and hash equality
- A short final normal block
- Fast Pass continuation over normal ranges without fallback reads
- Slow detection, adaptive skip growth, skip-reset hysteresis, and normal-region rediscovery
- Pass 1 optional survey stride for whole-file sampling
- Pass 2 Fast of likely-fast survey skips and Pass 3 Slow without hard-region pursuit
- Bounded Pass 4 Hard attempts and optional Pass 5 Deep block/fallback/sector localization
- Per-pass read-budget deferral, completed and still-pending cancellation, remainder-of-hole deferral on a pending Fast-pass cancel, and no new read after a stop request
- Windows overlapped explicit-offset reads issued off the wait/cancel thread, and an end-to-end Windows backend rescue
- Saved policy validation, legacy-policy adoption, and strict `resume` destination/map matching
- `flvrescue status` occupancy rendering and pass labels from a saved map
- Range splitting, merging, and zero-filling of final unreadable units
- Ctrl+C checkpointing and resume without rereading recovered ranges
- Safe migration of v1 prefix and v2 range maps to map v3, including policy v1/v2 to v3
- Progress updates while a simulated source read is blocked, and destination preparation without a fake percent
- Local read-marker alignment, color-independent glyphs, and timed Slow/Hard display states
- Manual Slow/Hard/Deep routing that changes only unresolved map ranges
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
