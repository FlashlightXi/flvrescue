# Safety and real-drive checklist

## What flvrescue does

`flvrescue` reads one ordinary file with one source request at a time and writes the result to a different file. The Fast Pass skips ahead after slow or failed reads, Pass 2 revisits only skipped ranges, and optional Pass 3 narrows unresolved ranges to smaller reads. Unrecovered ranges remain zero-filled so later readable data keeps its original offset.

The source file is never opened for writing.

## What it does not do

`flvrescue` does not:

- Repair an HDD, SSD, filesystem, partition, or media container
- Recover bytes that the operating system cannot read
- Clone a disk or access raw sectors
- Inspect or change SMART settings
- Impose a reliable timeout or forcibly cancel an in-progress synchronous read
- Guarantee that a zero-filled FLV remains decodable

It is complementary to, not a replacement for, device-level recovery tools such as GNU ddrescue. Use a physical-media imaging workflow when a disk or partition image, reverse passes, or broader retry control is required.

## Blocking I/O limitation

Python's ordinary `read()` may remain blocked while Windows, a storage driver, USB bridge, or the drive firmware performs its own retries. The slow threshold changes strategy only after that read returns; it is not an I/O timeout.

The renderer runs separately and continues updating from shared state while normal file I/O is blocked. Ctrl+C checkpointing can still be delayed until control returns to Python. `flvrescue` does not kill an I/O worker thread because doing so is not a safe general cancellation mechanism.

A future native Windows reader may use `CreateFileW`, `ReadFile`, and carefully scoped `CancelIoEx` handling. That requires separate validation of handle ownership, cancellation races, alignment, and device behavior.

## Before reading a failing drive

1. Decide whether the data is valuable enough to require a professional recovery service.
2. Confirm that `damaged.flv` is the intended source and is not still being recorded or modified.
3. Put `rescued.flv` and its map on a healthy disk with enough free space for the source's full logical size.
4. Confirm that source, destination, and map are three different files.
5. Close media players, indexers, antivirus scans, backup jobs, and other software that may read the failing drive.
6. Test the command first with a small, known-good file and inspect the result.
7. Use stable power and avoid repeatedly disconnecting or restarting the drive.

Stop if the drive makes abnormal mechanical noises, disappears from Windows, repeatedly resets, or becomes markedly worse. Do not use FFmpeg, corruption tools, or other processing commands on the source; run them only on the rescued copy.

## Interruption behavior

On Ctrl+C, `flvrescue` flushes committed destination writes and atomically updates the range map before the CLI exits. A lower-level I/O request may still delay when Ctrl+C reaches Python.

After an unexpected crash, the destination can contain writes not yet advertised by the map. Resume trusts only committed range state and safely overwrites any uncommitted data when that range is processed again.
