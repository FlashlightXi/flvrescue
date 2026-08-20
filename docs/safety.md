# Safety and real-drive checklist

## What flvrescue does

`flvrescue` reads one ordinary file sequentially and writes the result to a different file. When a large read fails, it narrows only that range to smaller reads. A final unreadable range is replaced with zeroes so later readable data keeps its original offset.

The source file is never opened for writing.

## What it does not do

`flvrescue` does not:

- Repair an HDD, SSD, filesystem, partition, or media container
- Recover bytes that the operating system cannot read
- Clone a disk or access raw sectors
- Inspect or change SMART settings
- Repeatedly retry known bad ranges
- Guarantee that a zero-filled FLV remains decodable

It is complementary to, not a replacement for, device-level recovery tools such as GNU ddrescue. Use a physical-media imaging workflow when a disk or partition image, reverse passes, or broader retry control is required.

## Blocking I/O limitation

Python's ordinary `read()` may remain blocked while Windows, a storage driver, USB bridge, or the drive firmware performs its own retries. v0.1 cannot impose a reliable timeout on that operation.

Ctrl+C can only be handled after control returns to Python. `flvrescue` does not kill an I/O worker thread because doing so is not a safe general cancellation mechanism.

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

On Ctrl+C, `flvrescue` flushes the completed destination prefix and atomically updates the resume map before the CLI exits. A lower-level I/O request may still delay when Ctrl+C reaches Python.

After an unexpected crash, the destination can contain uncommitted tail bytes beyond `completed_until`. Resume starts from the committed map offset and overwrites that tail before advancing.
