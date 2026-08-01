# Fix retention receipt: freed_bytes under-reports compressed chunks (#1125)

## Why

The first live retention enforce (2026-07-25, #1072 Step C) proved the H4
measurement wrong for compressed chunks: the receipt summed 17,403,371,520 B
freed while `pg_database_size` shrank by 19,097,174,016 B — a ~1.7 GB
under-report. `_default_measure_chunk_bytes`
(`scripts/node27_timeseries_retention.py:899-940`) sizes only the main chunk
relation via `pg_total_relation_size(chunk::regclass)`; for compressed chunks
the data lives in the compressed sibling relation
(`_timescaledb_internal.compress_hyper_*`), which `drop_chunks` also removes
but whose bytes are never counted (the three compressed candidates recorded
57,344 / 57,344 / 32,768 B against true sizes of ~536 MB / ~620 MB / ~538 MB).
The receipt is the durable audit record of what retention reclaimed;
systematically wrong numbers for exactly the chunk class retention most often
drops (older-than-14d chunks are compressed by then per the compression
timer) makes capacity accounting and post-hoc audits silently wrong.

## What Changes

- Replace the per-chunk measurement query with the compression-aware public
  API, empirically validated by the live evidence (`chunks_detailed_size`
  sum matched the DB-size delta exactly):
  `SELECT total_bytes FROM chunks_detailed_size(<hypertable>::regclass)
  WHERE chunk_schema = %s AND chunk_name = %s`.
- Keep every H4 property intact: measured BEFORE drop, per-chunk isolated
  connection, per-chunk failure → record 0 and continue (best-effort receipt,
  drop still proceeds), 60 s statement timeout.
- Sync the surfaces actually pinned to the old measurement wording:
  script header H4 note + function docstring, and design #855's H4
  measurement-path sentence
  (`openspec/changes/tier-node27-timeseries-storage/design.md:1903-1904`,
  back-filled to name `chunks_detailed_size` and record the deliberate
  divergence from the compression sibling's catalog-join query) — same
  back-fill discipline as #1177's wire-code precedent. Runbook `:1870` and
  design `:1964` state only the H4 ordering (still true) and need no edit.
- Update the receipts README known-limitation section to record the fix
  (issue/PR refs), keeping the historical receipt's numbers as-is (the
  2026-07-25 receipt is immutable evidence; no rewrite).
- Unit tests: compressed-chunk fixture rows through the default measurement
  path (fake cursor) proving compressed-side bytes are included; H4
  mock-ordering assertion stays green; no-row → 0 edge.

## What Does NOT Change

- Receipt schema (`schemas/timeseries_retention_receipt.schema.json`) —
  `freed_bytes: integer >= 0` shape unchanged.
- Drop mechanics, gate logic, deferral, salvage-window derivation, wire
  codes, H5 fail-closed, H7 predicate — all untouched (the live run proved
  them correct).
- The historical 2026-07-25 receipt file.

## Impact

- Affected specs: `timeseries-db-retention` (ADDED requirement:
  compression-aware freed_bytes accounting).
- Affected code: `scripts/node27_timeseries_retention.py` (one function +
  header comment), `tests/test_node27_timeseries_retention.py`, runbook,
  design #855 back-fill, receipts README.
- Runtime risk: measurement query swap only; on any per-chunk failure the
  existing best-effort semantics record 0 exactly as today.
