# Parser run-window bound, published compressed-chunk decline, #2451 text cleanup

Issues: #2687, #2690, #2501. One PR; three independent parts. #2727 was planned in this change and removed
before merge: its reproduction succeeded, but the planned fix was found to trade one defect for another (see
"Removed from this change").

```text
Issue type: bugfix (+ test/doc hygiene for #2501)
Fixture level: expanded
Upstream suggested level: absent
Blast radius: #2687 - a valid forecast run is refused by the
  parser and stays failed (regression) or an out-of-window run is silently skipped by latest-cycle discovery
  (today); #2690 - a published run's re-parse is declined although it would have succeeded on retry (regression)
  or the node-27 tick stays rc=1 forever with no alert (today)
Selected risk packs: Parser / writer; Error handling / retry policy; Legacy compatibility; Documentation
Evidence floor: red-before/green-after tests per part; named regression suites below; ruff; openspec strict
```

## Why

- **#2687**. `_per_source_latest_cycles` bounds its fact probe by `valid_time BETWEEN cycle_time AND end_time`.
  Nothing enforces that on write: the parser does not know `end_time` and accepts negative offsets and absolute
  timestamps up to a day before the cycle. A run whose rows fall outside the window is silently skipped by
  latest-cycle discovery.
- **#2690**. A `published` run whose re-parse hits `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` keeps status
  `published`, fails every tick with rc=1, and is invisible to the residency lane until an operator decompresses.
- **#2501**. After PR #2500 collapsed the branch axis to `narrow`, `fix-narrow-segment-read-index-applicability`
  still describes a two-value `narrow and legacy` cross product, and `BRANCH_IDENTITY_COLUMNS["legacy"]` plus three
  offline tests pin a contract with no consumer.

## What changes

- **#2687**: `HydroRunContext` gains `end_time` (DB path and DB-free manifest path; a missing `end_time` on the
  DB-free path fails closed). For `run_type='forecast'` each row must satisfy `cycle_time <= valid_time <= end_time`
  (closed interval); a violation raises `OutputParsingError("VALID_TIME_OUTSIDE_RUN_WINDOW", ...)` naming the row,
  its `valid_time` and the window; the run is marked failed and no row is written. Analysis runs are untouched.
- **#2690**: in the node-27 autopipe, a `published` run's re-parse failing with
  `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` is recorded as a `PUBLISHED_REPARSE_FAILED` decline (detail starts with
  that code), so the tick stops returning rc=1 for it and the residency observer reports it - the #1781 treatment
  of `APPLY_COMPRESSED_CHUNK_BLOCKED`. `OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED` stays transient.
- **#2501**: dated revision notes in `fix-narrow-segment-read-index-applicability/design.md` and `tasks.md`
  (legacy half-axis superseded by #1988 task 6.3 / PR #2500 / `67fe44d5d`; stale line reference fixed);
  `BRANCH_IDENTITY_COLUMNS` keeps only `narrow`; the three legacy-only offline tests and their fixtures go; the
  module docstring stops citing a call that now raises.

## Non-goals

- #2687: the latest-cycle probe SQL; analysis runs; the registration-side window rewrite in
  `scripts/node27_ingest_run.py` (recorded in the issue, not handled).
- #2690: non-published runs; `OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED`.
- #2501: narrow-side `RECORDED_ROW_DIGESTS` and the 24-cell receipts (must be byte-identical); the specs of that
  change (they do not mention legacy).

## Removed from this change

#2727 (strict-lane rewrite of raw-manifest repair retries). The reproduction succeeded: on the strict lane
`retry_downstream_after_raw_repair` really restarts a forecast-stage forcing-input failure at `forecast`. The
planned fix (exempt the `download` full-chain shape from the escalator) was implemented and then withdrawn, for two
reasons found while testing it: under the same run id the "full chain" retry adopts the already-succeeded
convert/forcing stages and submits nothing, so it does not regenerate the forcing package the fix assumed it
would; and the exemption removes the forced forecast replay that strict-lane raw-repair retries of a
post-forecast failure get today. The evidence and the withdrawn patch are recorded on the issue; the choice of
remedy goes back to the issue owner.
