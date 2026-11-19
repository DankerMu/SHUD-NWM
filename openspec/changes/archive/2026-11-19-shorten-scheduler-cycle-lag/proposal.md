# Shorten the production scheduler cycle lag from 16 h to 10 h (#3225)

## Why

Production submits a cycle 16 h after its nominal time and the display database receives it at T+16.4 h .. T+18.9 h
(18 source-cycles measured 2026-11-15 .. 2026-11-19). The value 16 has no recorded rationale; the owner confirmed on
2026-11-19 that it is not intentional. Measured raw-data completion on the shared store (newest raw file mtime per
cycle and source, 27 cycles 2026-11-06T12 .. 2026-11-19T12, node-27 download delay 8 h):

| source | typical (24 of 27) | slow cycles |
| --- | --- | --- |
| GFS | T+8.28 h .. T+8.79 h | T+9.24 h, T+11.23 h, T+12.45 h |
| IFS | T+8.84 h .. T+9.33 h | T+9.81 h, T+11.93 h, T+12.98 h |

So the scheduler waits 6.7 h longer than a normal cycle needs. The three slow cycles are the same three for both
sources (2026-11-10T00, 2026-11-18T00, 2026-11-18T12): the download job was slow, not the upstream publication.

## What changes

1. Production `NHMS_SCHEDULER_CYCLE_LAG_HOURS` goes from 16 to 10 (`NHMS_SCHEDULER_LOOKBACK_HOURS` stays 32): the env
   template, the live node-22 `compute.scheduler-dbfree.env`, and the runbooks that quote the value.
2. The operator-action listing requirement and its help text stop quoting production window values (the spec said
   lookback 96 h, the help text 32 h, the env 32 h): the boundary is stated, the numbers live in the env template only.

No scheduler code path changes. The default in code stays 0.

## Why 10 and not a larger margin

- A cycle whose raw data is incomplete is **deferred, not submitted**: node-22 decides `available` from the raw
  manifest plus every entry file being present and non-empty (`source_cycle_raw_manifest.py::_verify_entry_files`),
  both written atomically by the downloader. A pass that sees such a cycle stays `planned`, builds no candidate and no
  blocked candidate, and records one `not_selected` / `source_cycle_unavailable_does_not_consume_source_budget` entry
  (the reverse shape, an older unavailable cycle skipped, is
  `tests/test_production_scheduler.py::test_backfill_skips_unavailable_gap_and_selects_oldest_available_for_warm_start`;
  the newest-cycle shape was probed on master 82643e4a8 and becomes a committed test here, task 1.4). Neither the stall-health check nor the operator-action
  listing counts it.
- So the lag only has to cover the normal case; a slow cycle is picked up by the first pass after its data completes
  (passes run every five minutes) instead of waiting for a fixed margin. 10 is the smallest whole hour above the
  typical completion (9.33 h) and also covers the T+9.24 h / T+9.81 h cycles; the two slowest would have been deferred
  for 2 h and 3 h and still started 3 h earlier than today.
- 9 would defer every normal IFS cycle for a few passes; a larger value adds the same delay to every cycle to avoid a
  deferral that costs nothing.

## Constraints checked

- `LOOKBACK + CYCLE_LAG <= 48 h` (node-27 compression lag): 32 + 10 = 42.
- Forecast-data retention `RETENTION_DAYS * 24 > LOOKBACK + LAG + 2 * gap`: 336 > 32 + 10 + 24 (`NHMS_RETENTION_DAYS=14`
  is the live node-22 value; the db-free template does not set it). A smaller lag only loosens this inequality.
- Journal retention (`scheduler_journal_retention.py:316`): 90 d, same inequality.
- State-index retention window (#3118): `max(21, floor // 24 + 1)` is 21 days for lag 16 and for lag 10 (floor 400 h
  vs 394 h), so the prune window does not move.
- "Never shrink the window while a model is mid catch-up": the smaller lag moves the window start forward by at most
  one cycle; the change is applied only when the last pass reports `gap_count = 0` for both sources.

## Fixture

```text
Issue type: ops configuration + documentation/spec wording
Fixture level: light
Blast radius: a too-small lag cannot submit an incomplete cycle (deferral, see above); a wrong window could drop an
              unfinished cycle from the backfill window (guarded by the gap_count = 0 pre-check)
Selected risk packs: Legacy compatibility (operators reading runbooks that quote 16)
Evidence floor: see tasks.md
```

## Residual risks (unchanged in kind, recorded)

- Readiness checks presence and size > 0, not the downloader's `min_file_size` / checksum verification. This predates
  the change and does not depend on the lag.
- A newer cycle can overtake a still-unavailable older one once the older one is more than 22 h late (28 h before).
  The worst measured lateness is 13 h.

## Out of scope (reported on the issue)

- The node-27 download delay (8 h for both sources) is now the floor; GFS is published hours before IFS.
- `compute.host.env` and `compute.replay.env` on node-22 also carry lag 16; neither is read by the scheduler timer.
