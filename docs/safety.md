# Safety and real-drive checklist

## What flvrescue does

`flvrescue` reads one ordinary file with one source request at a time and writes the result to a different file. Pass 1 Survey samples across the file, Pass 2 Fill revisits likely-good survey skips, Pass 3 Retry tries slow/error ranges again, and Pass 4 Deep narrows selected unresolved ranges to smaller reads. Unrecovered ranges remain zero-filled so later readable data keeps its original offset.

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

On Ctrl+C, `flvrescue` flushes committed destination writes and atomically updates the range map before the CLI exits. A lower-level I/O request may still delay when Ctrl+C reaches Python.

After an unexpected crash, the destination can contain writes not yet advertised by the map. Resume trusts only committed range state and safely overwrites any uncommitted data when that range is processed again.
