# Fix retention receipt: freed_bytes under-reports compressed chunks (#1125)

## Why

The first live retention enforce (2026-07-25, #1072 Step C) proved the H4
measurement wrong for compressed chunks: the receipt summed 17,403,371,520 B
freed while `pg_database_size` shrank by 19,097,174,016 B — a ~1.7 GB
under-report. `_default_measure_chunk_bytes`
(`scripts/node27_timeseries_retention.py:934-1018` after this change; the
defective form is `origin/master:898-939`) sized only the main chunk
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
- Add the D2 measure-failure stderr diagnostic: the per-chunk `except`
  branch prints exactly one JSON line —
  `{"warning": "freed_bytes measurement failed; recording 0", "chunk": ...,
  "error": ...}` — before recording 0 and continuing. It is warning
  vocabulary, NOT a wire code (never enters the receipt, not a `WIRE_CODES`
  member) and changes no control flow or exit code; without it the receipt's
  `0` is indistinguishable from a genuinely empty chunk. The no-row / NULL
  `total_bytes` coercion path stays SILENT by design (no warning line).
- Add `_redact_measure_error` (new module-level helper next to the
  measurement path): psycopg2 echoes the DSN and the libpq role name
  verbatim on connection/auth failures and the wrapper captures stderr into
  `retention.log`, so the diagnostic's `error` text is routed through
  `packages/common/redaction.py` (`redact_database_dsn`, marker rendered
  `***`) plus a narrow `user "<name>"` scrub bound to the DSN's own username.
- Sync the surfaces actually pinned to the old measurement wording:
  script header H4 note + function docstring, and design #855's H4
  measurement-path sentence
  (`openspec/changes/tier-node27-timeseries-storage/design.md:1903-1904`,
  back-filled to name `chunks_detailed_size` and record the deliberate
  divergence from the compression sibling's catalog-join query) — same
  back-fill discipline as #1177's wire-code precedent. The H4 *ordering*
  claims in runbook `:1892` and design `:1964` stay true and need no edit
  (the runbook is still edited by this change, but only to add the new
  §8.2.1 / §8.6 D2 material below).
- Document the D2 diagnostic for operators:
  `docs/runbooks/tier-node27-timeseries-storage.md` gains §8.2.1 (non-code
  stderr diagnostics, incl. the silent-coercion asymmetry) and §8.6 item 5
  (how to disambiguate a `freed_bytes: 0` in an `enforced` receipt).
- Update the receipts README known-limitation section to record the fix
  (issue/PR refs), keeping the historical receipt's numbers as-is (the
  2026-07-25 receipt is immutable evidence; no rewrite).
- Unit tests: compressed-chunk fixture rows through the default measurement
  path (fake cursor) proving compressed-side bytes are included; H4
  mock-ordering assertion stays green; no-row → 0 edge; plus rows for the
  D2 diagnostic (exact three-key JSON on stderr, stderr silence on the clean
  and coercion paths, credential redaction) and byte-identity rows anchoring
  the measurement SQL prefix and the warning literal to their doc surfaces.

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
- Affected code: `scripts/node27_timeseries_retention.py`
  (`_default_measure_chunk_bytes` + the new `_redact_measure_error` helper +
  header comment), `tests/test_node27_timeseries_retention.py`, runbook
  §8.2.1/§8.6, design #855 back-fill, receipts README.
- Runtime risk: measurement query swap plus one stderr line on the failure
  path; on any per-chunk failure the existing best-effort semantics record 0
  exactly as today (never blocks or fails a drop).
