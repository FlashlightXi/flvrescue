# flvrescue — Next recovery-engine / UI iteration handoff

This handoff summarizes field observations from testing `flvrescue` against a genuinely failing USB-connected HDD and proposes the next implementation direction.

The observations below are intentionally generalized for a public repository. Do not add machine usernames, local paths, drive serial numbers, private filenames, or exact user-specific offsets to documentation or tests.

## Goal

The next update should focus less on maximizing recovery from every individual read and more on:

> maximizing useful recovered data per unit of wall-clock time while avoiding long-lived I/O stalls.

The current implementation already demonstrates that large readable regions can be recovered successfully. The primary remaining problem is that a very small number of pathological reads can dominate total runtime and can even make interruption/shutdown difficult.

---

## Field observations

### Large fast regions coexist with very difficult regions

The failing drive does not behave like a uniformly slow disk.

Observed behavior includes:

* large regions readable at normal HDD speeds
* regions that take a few seconds per read but still succeed
* much harder regions that stall for tens of seconds or longer
* occasional locations where a read may remain incomplete for many minutes without returning a read error
* read errors that are followed immediately by another region reading at normal speed

A region classified as slow during one pass is not necessarily permanently slow. Later attempts may read portions of it normally.

Therefore `fast`, `slow`, and `hard` should primarily describe **recovery priority / observed difficulty**, not permanent physical properties of the disk.

---

## A particularly important failure mode: incomplete I/O without an error

The most expensive failure mode observed is not necessarily `OSError`.

A read may instead behave approximately like:

```text
Read starts
→ no completion
→ drive remains nearly 100% active
→ transfer rate is near zero or only a few KiB/s
→ no read error is returned for a very long time
```

This can be significantly worse than a normal read error because the rescue loop cannot continue while the synchronous read remains blocked.

Some locations reproduce this behavior after restarting the machine, suggesting that at least part of the problem is location-dependent rather than purely caused by extended runtime.

At the same time, drive condition, previous reads, temperature, firmware retry state, and access history may also influence latency. A single pass should therefore not be treated as definitive physical diagnosis.

---

## Process termination does not necessarily terminate the underlying disk activity

During testing, terminating the terminal/process did not always immediately stop disk activity.

Observed behavior included:

* the user process disappearing
* disk activity remaining close to 100%
* low-level activity temporarily appearing under the Windows `System` process
* later no obvious file activity being listed, while the disk still remained active
* occasional tiny transfer bursts despite near-zero throughput

This suggests that already-issued requests may remain in the Windows storage stack, USB bridge, or drive firmware after the original user process has gone away.

Therefore:

> killing the Python process must not be assumed to cancel the physical disk operation.

This should influence both the I/O backend and the meaning of “force quit”.

---

# Proposed 5-pass model

Use difficulty as a queue / priority system rather than a permanent classification.

```text
Pass 1  Survey
Pass 2  Fast
Pass 3  Slow
Pass 4  Hard
Pass 5  Deep
```

### Pass 1 — Survey

Discover the coarse recovery landscape without attempting complete recovery.

The goal is to identify likely readable regions and defer expensive regions early.

### Pass 2 — Fast

Recover the easiest data first.

This should remain the default stopping point for normal operation.

A read that becomes clearly slow or hard should not be deeply explored during this pass.

### Pass 3 — Slow

Recover:

* regions classified as moderately slow
* fast-looking regions missed because of coarse Survey granularity
* readable portions around difficult ranges

This should still be considered a relatively practical recovery pass.

### Pass 4 — Hard

Attempt explicitly difficult areas with a larger time budget and smaller reads where appropriate.

This pass may be substantially slower and should remain opt-in.

### Pass 5 — Deep

Perform fine-grained / exhaustive recovery on the remaining unresolved data.

Existing fallback and sector-level recovery mechanisms can live here.

This pass should never run by default.

---

# Important semantic rule

A classification from one pass is not final.

For example:

```text
Survey: hard
Pass 3: read succeeds quickly
→ recovered
```

must be valid.

`slow` and `hard` mean roughly:

> “This region should currently be retried at this priority.”

They should not mean:

> “This region is physically and permanently slow/hard.”

---

# Highest-priority implementation: cancelable Windows reads

The current synchronous Python file read can leave the process blocked indefinitely while the drive retries internally.

Investigate a Windows-specific Reader backend using cancellable/overlapped I/O, for example around:

* `CreateFileW`
* `FILE_FLAG_OVERLAPPED`
* `ReadFile`
* waitable completion
* `CancelIoEx`

Do not assume that `CancelIoEx` guarantees immediate physical cancellation. USB bridges and drive firmware may still delay completion.

The implementation should therefore distinguish:

```text
cancel requested
cancel completed
cancel could not complete promptly
```

rather than treating cancellation as guaranteed.

Keep the existing portable Reader as a fallback where necessary.

---

# Read-time classification

Classification should happen while a read is still in progress, not only after it completes.

Conceptually:

```text
read begins
↓
READING
↓ slow threshold
SLOW
↓ hard threshold
HARD
```

If the read later succeeds, record the recovered data.

If it fails, classify appropriately.

If cancellation is supported and the current pass has exceeded its allowed read budget, request cancellation and defer the region to a later pass.

Thresholds should be policy/configuration values rather than deeply hard-coded assumptions.

---

# Pass-specific read budgets

Different passes should be allowed to spend different amounts of time on the same unresolved region.

Conceptually:

```text
Fast pass
  small time budget
  → defer quickly

Slow pass
  moderate time budget
  → attempt again

Hard pass
  larger budget

Deep pass
  maximum effort
```

The exact defaults should be chosen conservatively and remain configurable.

This makes the five-pass model meaningful even when the exact same offset behaves differently across time.

---

# Block-size strategy

Do not globally reduce the normal read size.

Large reads are efficient in healthy/fast regions and observed fast areas can sustain normal HDD throughput.

Instead consider pass- or context-dependent sizes:

```text
Fast     large block (current normal block is appropriate)
Slow     smaller block
Hard     smaller again
Deep     fallback / sector-level localization
```

The purpose of a smaller block is not to make a truly bad sector itself faster.

The purpose is to avoid one bad location causing a much larger amount of otherwise-readable data to become part of the same stalled request.

---

# No dedicated guard band for now

A fixed exclusion zone around every `bad` or `hard` region was considered, but should not be added yet.

Reasons:

* current section/range classification is already relatively coarse
* this naturally behaves somewhat like a guard area
* one pass can overestimate the difficulty of a region
* adding another fixed guard may exclude too much readable data

First test whether the improved pass routing and cancellable read behavior is sufficient.

A future adaptive quarantine mechanism can still be added if real-world measurements show a need.

---

# Manual range override

Add a command that changes only the rescue map and never opens the source drive.

Conceptually:

```text
flvrescue mark MAP --offset ... --length ... --as hard
```

Possible operations:

* mark range slow
* mark range hard
* defer range
* clear manual classification

Use case:

> a specific read has repeatedly produced extremely long stalls, so the operator wants later Fast/Slow passes to skip it without issuing another source read.

The operation must be safe, reversible where possible, and preserve already recovered ranges.

---

# Interrupt / forced-exit behavior

Current graceful interruption may itself take a long time when storage I/O is stuck.

Introduce a clear two-stage shutdown model.

### First interrupt — graceful stop

```text
stop issuing new reads
request cancellation of current read if possible
checkpoint destination/map
close normally
```

### Second interrupt — forced process exit

If shutdown remains stuck:

```text
do not wait indefinitely for the source Reader
exit the process
```

The UI/documentation must make clear:

> forced process exit does not guarantee that a request already handed to the OS/USB device has physically stopped.

Do not imply that force exit is equivalent to safely removing or resetting the disk.

---

# Improve shutdown visibility

Do not print only a final `Interrupted` message while cleanup continues invisibly.

Show shutdown phases such as:

```text
STOPPING
Cancelling current read...

Checkpoint saved
Closing source...

Still waiting
Press Ctrl+C again to force exit
```

This will also help diagnose where shutdown delays actually occur.

---

# Live UI redesign

The current persistent `FLVRESCUE ... reads filename` header contains relatively little useful information during a stall.

Print basic source/destination information once at startup.

The persistent live region can grow to approximately 4–5 lines and should prioritize current I/O state.

Avoid heavy use of `|` separators. Prefer spacing, alignment, and separate lines.

Example direction:

```text
PASS 2 FAST    53.6 / 126.8 GiB    42.3%    00:18:24    96 MiB/s
████████████████████████▒▒▒▒▒▒░░░░░░░░░░

READ  48.734 GiB + 8.0 MiB    00:23.6    HARD
SECTION  48.62–48.91 GiB    right edge 18 MiB
48.50 GiB   █████████████▒▒▒▶▒░░░██████████   49.00 GiB
```

Exact visual design can be refined during implementation.

---

# Global and local progress views

Keep a global file map, but add a second zoomed view centered around the current read.

### Global bar

Shows the whole file and current recovery classification.

Useful for answering:

> How much of the file has been recovered?

### Local / zoomed bar

Shows only the region around the current offset.

Useful for answering:

> Why is this read slow?
> Are we at the edge of a readable island?
> Did a hard read occur unexpectedly in the middle of a fast-looking region?

The current read position should be visibly marked.

The zoom span can be fixed or selected automatically from the current section size.

---

# Current-read details

Persistent UI should show at minimum:

```text
current offset
requested read size
elapsed time for this individual read
current live state
```

Prefer a representation such as:

```text
READ  48.734 GiB + 8.0 MiB    00:23.6    HARD
```

over only showing the start/end offsets.

This makes the request size immediately obvious.

---

# Section context

Show what logical recovery section/range is currently being processed.

For example:

```text
SECTION 48.62–48.91 GiB    right edge 18 MiB
```

Possible context:

```text
left edge
middle
right edge
```

This is not intended as physical-disk diagnosis.

It simply explains where the current request sits inside the range that the current pass is processing.

This is especially useful because field observations show that range boundaries often become slower, while occasional severe stalls can also occur unexpectedly inside otherwise fast-looking areas.

---

# Status colours / categories

The UI and map should clearly distinguish:

```text
recovered / good
fast queue
slow
hard
unreadable / bad
pending
```

`HARD` should also appear live while an individual read is still blocked beyond the hard threshold.

Avoid requiring an error to occur before the operator can see that the read is pathological.

---

# Map / telemetry

Do not require a full telemetry redesign for this update.

The minimum requirement is enough durable state to route later passes safely.

If useful, future versions may record observational data such as:

```text
attempt count
best observed latency
worst observed latency
last observed result
total time spent
```

But avoid making the next update unnecessarily large.

Already recovered data must never be reread merely to obtain telemetry or reclassify old ranges.

---

# Safety and compatibility requirements

Preserve:

* source read-only behavior
* explicit-offset reads
* no parallel reads against the failing source disk
* atomic rescue-map saves
* resume behavior
* already recovered output
* non-TTY logging fallback
* simulated Reader tests

Map migrations must never turn previously recovered regions back into regions requiring source reads.

---

# Testing priorities

Extend the simulated Reader so tests can model:

* fast reads
* moderately slow reads
* hard reads
* reads that do not complete until externally released/cancelled
* cancellation success
* cancellation that takes time
* read errors
* different results for the same offset on later attempts

Verify especially:

```text
Fast pass does not spend Hard-pass effort on a stalled read
Slow/Hard classifications can later become recovered
already recovered ranges are never reread
cancel/interrupt does not corrupt the map
second interrupt can escape a stuck shutdown path
manual mark changes only the map
local UI continues updating while source I/O is blocked
```

For Windows-specific cancellation tests, separate deterministic unit tests from any optional real-device integration tests.

---

# Implementation priority

Recommended order:

1. cancelable Windows Reader / explicit cancellation model
2. graceful stop + force-exit path
3. realtime `READING → SLOW → HARD`
4. current offset / size / elapsed display
5. local zoomed section bar
6. complete 5-pass routing and pass-specific budgets
7. manual range mark/defer command
8. pass-specific/adaptive read sizes
9. additional telemetry only if it remains simple

The central design principle for this iteration is:

> Avoid allowing one pathological read to consume the time that could have been used to recover large readable regions elsewhere.

The real-device observations should be treated as evidence for this design direction, not as assumptions about all failing HDDs.
