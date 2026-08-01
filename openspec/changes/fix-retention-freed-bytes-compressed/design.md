# Design: compression-aware freed_bytes measurement (#1125)

## Decision 1 — measurement source: `chunks_detailed_size`, per chunk

Replace the H4 query with the TimescaleDB public API, filtered to the one
chunk, keeping the per-chunk connection shape:

```sql
SELECT total_bytes FROM chunks_detailed_size(%s::regclass)
WHERE chunk_schema = %s AND chunk_name = %s
```

parameters `(f"{chunk.hypertable_schema}.{chunk.hypertable_name}",
chunk.chunk_schema, chunk.chunk_name)` — all already on `ChunkRow`.

Version + API pinned by live read-only probe (node-27 primary,
2026-08-01, `psql` SELECT only — no mutation):

- `pg_extension.extversion` = **TimescaleDB 2.10.2**.
- `chunks_detailed_size('hydro.river_timeseries'::regclass)` accepts the
  hypertable regclass and returns `chunk_schema, chunk_name, table_bytes,
  total_bytes` — column names match the filter and `ChunkRow` fields.
- Compressed-chunk ground truth on the target instance:

  | chunk | is_compressed | pg_total_relation_size(main) | total_bytes |
  |---|---|---|---|
  | `_hyper_3_14_chunk` | t | 57,344 | 5,904,531,456 |
  | `_hyper_3_10_chunk` | t | 57,344 | 2,905,137,152 |
  | `_hyper_3_9_chunk` | t | 57,344 | 872,529,920 |

  Main relation reads the exact 57,344 B under-report signature from the
  2026-07-25 incident; `total_bytes` carries the compressed sibling. This
  also re-confirms the earlier receipts-README proof (four-candidate
  `chunks_detailed_size` sum == `pg_database_size` delta, byte-exact).

Why this over the alternative (main + sibling catalog join): **deliberate,
recorded divergence from the compression sibling.** The compression script
does use the private-catalog join (`scripts/node27_timeseries_compression.py:290-300`,
`_COMPRESSED_SIBLING_QUERY` on `_timescaledb_catalog.chunk.compressed_chunk_id`,
production-validated on 2.10.2), and design #855's H4 entry says "Reuse
compression `_default_measure_chunk_bytes` pattern". We keep the reuse of
the *connection/failure pattern* but not the *query*: retention needs one
total-reclaim number, which the public function returns directly and which
the live evidence validated byte-exactly, whereas the catalog join computes
it in two steps through private catalog tables. The design #855 back-fill
(D3) records this divergence so the sibling-reuse-fidelity review axis
does not reopen.

Why not one call per hypertable for all chunks: what per-chunk calls
preserve is **connection/transaction isolation** — #855 Class C fixed
`InFailedSqlTransaction` poisoning where one failed statement zeroed every
subsequent chunk in a shared transaction; that isolation is orthogonal to
where the size numbers come from and must not regress. (The computation
inside `chunks_detailed_size` is hypertable-wide either way, so the
per-call cost rises from O(1 relation) to O(hypertable chunks) — currently
8 chunks per hypertable on the live instance, milliseconds against the
60 s statement timeout; see the resource-limits risk pack.)

## Decision 2 — failure and empty-result semantics unchanged

- Per-chunk exception → record 0, continue (best-effort receipt, drop
  proceeds). Identical to today.
- New edge introduced by the filtered function: zero rows returned for the
  chunk (dropped/renamed between enumeration and measurement) → record 0
  via the same `int((row[0] if row else 0) or 0)` shape. `total_bytes` NULL
  → 0 via the same coercion.
- `chunks_detailed_size` on a non-hypertable/garbage regclass raises → falls
  into the existing except-→ -0 path. No new wire code, no new receipt
  field: this is a measurement-accuracy fix, not a contract change.

## Decision 3 — H4 ordering and doc pins

"Measured BEFORE drop" is untouched: the measurement loop still runs
strictly before the drop loop and the existing mock-ordering unit test
stays as the oracle. Doc surfaces pinned to the old query wording are
synced in the same commit (byte-consistency discipline, #1177 back-fill
precedent):

Actually-stale surfaces (MUST change — they name the old query):

1. `scripts/node27_timeseries_retention.py` header H4 note (`:34-35`) and
   `_default_measure_chunk_bytes` docstring + query (`:901-931`).
2. `openspec/changes/tier-node27-timeseries-storage/design.md:1903-1904`
   H4 measurement-path sentence — back-filled to name
   `chunks_detailed_size` AND to record the deliberate divergence from the
   compression sibling's catalog-join query (D1).
3. Receipts README known-limitation section: append the resolution
   (fixed by #1125 / PR, measurement now compression-aware); the
   2026-07-25 receipt numbers themselves are immutable history — never
   rewritten.

NOT stale (no edit required): `docs/runbooks/tier-node27-timeseries-storage.md:1870`
and design #855 `:1964` say only "measured BEFORE `drop_chunks`" without
naming the function — the H4 ordering claim stays true. Optional naming
there is allowed but not gated (tasks 2.7 greps only the stale surfaces).

## Decision 3b — archive ordering note

Capability `timeseries-db-retention`'s base spec still lives in the active
change `tier-node27-timeseries-storage` (base requirements :5/:59/:87/:99
share no name with this delta's ADDED requirement, so either archive order
merges cleanly). If this change archives first, the generated
`openspec/specs/timeseries-db-retention/spec.md` will carry a `Purpose TBD`
header (precedent: `openspec/specs/archive-rebuild-drill/spec.md`) —
Purpose back-fill responsibility rides with whichever change archives
second (expected: the milestone change).

## Decision 4 — test oracle

`tests/test_node27_timeseries_retention.py` gains default-path tests driving
`_default_measure_chunk_bytes` with a fake psycopg2 module (in-file
precedent: the suite already stubs connections for default-path coverage; if
none exists for this function, inject via `sys.modules` monkeypatching as
the sibling compression tests do):

- compressed-chunk fixture: fake cursor returns `total_bytes` (large value);
  assert recorded value equals it — proving the query path reports
  compression-inclusive bytes and binds `(hypertable, chunk_schema,
  chunk_name)` correctly (assert on executed SQL + params).
- zero-rows edge → 0.
- exception edge → 0 and the remaining chunks still measured (existing
  semantics pinned).
- H4 mock-ordering test unchanged and green.

Red-first: the query-shape assertion fails against the current
`pg_total_relation_size` implementation.

## Non-goals

- No schema change, no wire-code change, no gate/deferral/salvage change.
- No live node-27 enforce as acceptance evidence: the next scheduled
  retention tick will produce a receipt whose accuracy can be checked
  opportunistically; requiring a live drop to merge a measurement fix
  inverts the risk (drops are irreversible). Unit oracle + the recorded
  live discrepancy suffice.
