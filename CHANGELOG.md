# Changelog

## 0.3.0 - 2026-08-26

- Issue Windows `ReadFile` on a dedicated thread so Fast-pass time budgets and Ctrl+C still run when a USB driver blocks inside the call.
- Retry `CancelIoEx` together with `CancelSynchronousIo` while waiting for cancellation.
- Keep worker completion separate from the native OVERLAPPED event so a late-returning I/O thread cannot signal a closed Windows handle.
- Classify the rest of the current Fast/Slow hole before stopping on a still-pending cancel, so the next resume does not retry the adjacent survey bytes.

- Split recovery into Survey, Fast, Slow, Hard, and opt-in Deep passes.
- Add persisted slow/hard latency thresholds and per-range difficulty metadata.
- Migrate map v1/v2 and policy v1 without rereading already recovered bytes.
- Keep `fill` and `retry` as compatibility aliases for `fast` and `slow`.
- Show fast, slow, hard, failure, and remaining occupancy separately.
- Keep the default execution limit at Survey through Fast; Hard and Deep remain explicit.
- Add Windows overlapped reads with per-pass time budgets and bounded cancellation.
- Add two-stage Ctrl+C handling: checkpointed stop first, immediate process exit second.
- Add smaller Slow and Hard read blocks while preserving serial, explicit-offset source I/O.
- Add the live local offset bar, current-read marker, elapsed read state, and color-independent glyphs.
- Add `flvrescue mark` to route unresolved map ranges to Slow, Hard, or Deep without opening source or destination data.

## 0.2.0 - 2026-08-24

- Define Survey, Fill, Retry, and Deep as four named recovery stages.
- Persist the resolved recovery policy in the resume map.
- Fill every survey gap before entering slow/error Retry, including older maps already marked as Pass 3.
- Add strict `flvrescue resume` and read-only `flvrescue status` entry points.
- Add capacity-safe sparse output without eager truncate or physical unreadable zero ranges.
- Add non-mutating `flvrescue optimize` command recommendations.
- Add destination preparation and named-pass progress reporting.
