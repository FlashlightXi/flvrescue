# Changelog

## 0.2.0 - 2026-08-24

- Define Survey, Fill, Retry, and Deep as four named recovery stages.
- Persist the resolved recovery policy in the resume map.
- Fill every survey gap before entering slow/error Retry, including older maps already marked as Pass 3.
- Add strict `flvrescue resume` and read-only `flvrescue status` entry points.
- Add capacity-safe sparse output without eager truncate or physical unreadable zero ranges.
- Add non-mutating `flvrescue optimize` command recommendations.
- Add destination preparation and named-pass progress reporting.
