# Design

## #2687 - parser run window

Governing invariant: every forecast fact row written by the parser lies in `[hydro_run.cycle_time, hydro_run.end_time]`.
The lower bound is `cycle_time` (what the probe uses), not `start_time`. The check sits in the per-row loop after
the valid time is computed, before anything is written; the existing fail-closed path
(`_mark_run_failed_preserving_error`) carries the error. Both context sources must supply `end_time`: the DB
path (`load_run_context`) and the DB-free manifest path (same source as `scripts/node27_ingest_run.py`).

Sibling surfaces: every constructor of `HydroRunContext` (source and tests); the dual-write and re-parse paths;
`tests/test_analysis_pipeline.py` (analysis unchanged). Compatibility: existing data has zero rows outside the
window (node-27 read-only proof, 2026-09-28), and `start_time = cycle_time` holds for every forecast run.

Known edge, to verify from fixtures rather than assume: whether real SHUD output can carry a row exactly at
`end_time` (allowed, closed interval) or one step past it (would now be refused). Check the existing fixtures and
any sample product in `tests/`; if a legitimate product has a row past `end_time`, stop and report.

## #2690 - published re-parse and the compressed-chunk guard

Follow the existing deterministic-failure path in `scripts/node27_autopipeline.py` that writes
`PUBLISHED_REPARSE_FAILED`; add `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` to what it treats as terminal for a
published run. The decline is keyed on `(init_state_id, product_mtime)` (`_declined_runs`), so
decompressing the chunk alone never triggers a retry: the operator re-runs with `--force` or deletes the decline
row, the same exit the forcing-side decline already documents
(`docs/runbooks/tier-node27-timeseries-storage.md`, the `APPLY_COMPRESSED_CHUNK_BLOCKED` paragraph). That
paragraph gains one line covering the parse-side code. No new retry trigger. The existing unit test that pins
`OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` as non-deterministic flips; the `GUARD_FAILED` pin stays. The base
requirement's phrase "a deterministic output-parsing code" now has this one named exception.

DB-free analysis manifests (#2687): `end_time` is required only for forecast contexts; an analysis manifest
without `end_time` keeps parsing as today.

## Review focus

- #2687: the lower bound is `cycle_time`; nothing is written when a row is refused; analysis untouched.
- #2690: only the `BLOCKED` code changes class; the tick exit code and the observer are otherwise untouched.
- #2501: `RECORDED_ROW_DIGESTS` and receipts byte-identical.

## #2501 - text and test hygiene

Mechanical; every edit is pinned to file:line in the issue. Revision notes are dated and additive; historical
receipts and digests are not edited.
