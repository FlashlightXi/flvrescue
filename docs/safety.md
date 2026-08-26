# Safety and real-drive checklist

## What flvrescue does

`flvrescue` reads one ordinary file with one source request at a time and writes the result to a different file. Survey samples the file, Fast recovers likely-fast gaps, Slow handles light-slow ranges, Hard gives hard/error blocks one coarse attempt, and explicit Deep localizes every unresolved range. Unrecovered ranges remain zero-filled so later readable data keeps its original offset.

The source file is never opened for writing.

## What it does not do

`flvrescue` does not:

- Repair an HDD, SSD, filesystem, partition, or media container
- Recover bytes that the operating system cannot read
- Clone a disk or access raw sectors
- Inspect or change SMART settings
- Guarantee that cancelling a Windows request immediately stops lower-level drive or controller activity
- Guarantee that a zero-filled FLV remains decodable

It is complementary to, not a replacement for, device-level recovery tools such as GNU ddrescue. Use a physical-media imaging workflow when a disk or partition image, reverse passes, or broader retry control is required.

## Read cancellation boundary

On Windows, the default backend uses one explicit-offset overlapped `ReadFile` request at a time. Each pass has a saved per-read time budget. When the budget expires or Ctrl+C requests a stop, `flvrescue` calls `CancelIoEx` for that request and waits briefly for Windows to acknowledge completion.

Windows request cancellation is not a physical-device guarantee. A storage driver, USB bridge, or drive firmware may continue lower-level work after Windows reports cancellation. If cancellation is still pending after the grace period, `flvrescue` checkpoints and starts no further reads; it keeps the native request storage alive until process exit rather than releasing memory still owned by the kernel.

Pass 4 performs no fallback or sector localization and makes one attempt per coarse block. Pass 5 is the only exhaustive fallback stage and is never selected by default or by `optimize`.

The renderer runs separately and continues updating from shared state while a read is pending. On non-Windows systems, or when `--reader portable` is selected, ordinary blocking reads cannot be cancelled by Python; checkpointing occurs once control returns.

## Destination storage and SSD capacity

On Windows, a new output must accept sparse-file mode (normally NTFS or ReFS). `flvrescue` refuses a non-sparse destination instead of risking an offset write that physically fills the skipped gap. Sparse outputs grow lazily as recovered offsets are written; they are not pre-sized because even a sparse truncate can reserve clusters on some storage configurations. Holes still read as zero once a later offset extends the file. Recovered bytes still consume SSD space.

Keep enough free capacity for the bytes you expect to recover, the map, and any later validation or copy operation. Copying a sparse output to a filesystem or tool that does not preserve holes can materialize its unwritten zero regions and require the full logical file size. Check the target before copying or archiving it.

## Before reading a failing drive

1. Decide whether the data is valuable enough to require a professional recovery service.
2. Confirm that `damaged.flv` is the intended source and is not still being recorded or modified.
3. Put `rescued.flv` and its map on a healthy disk. Plan capacity for recovered data and be prepared for a later copy to require the full logical size.
4. Confirm that source, destination, and map are three different files.
5. Close media players, indexers, antivirus scans, backup jobs, and other software that may read the failing drive.
6. Test the command first with a small, known-good file and inspect the result.
7. Use stable power and avoid repeatedly disconnecting or restarting the drive.

Stop if the drive makes abnormal mechanical noises, disappears from Windows, repeatedly resets, or becomes markedly worse. Do not use FFmpeg, corruption tools, or other processing commands on the source; run them only on the rescued copy.

Keep the source and destination files in place until that rescue is finished. Resume currently verifies canonical paths and sizes, but it does not yet persist Windows file IDs; replacing either file with a different same-size file at the same path cannot be detected.

## Interruption behavior

The first Ctrl+C asks the active Windows read to cancel, prevents any new source read, flushes committed destination writes, and atomically updates the range map before normal CLI exit. The second Ctrl+C forces immediate process exit and therefore cannot promise another checkpoint or physical I/O quiescence. Use it only when waiting is riskier than losing the latest uncommitted progress.

After an unexpected crash, the destination can contain writes not yet advertised by the map. Resume trusts only committed range state and safely overwrites any uncommitted data when that range is processed again.
