# Proposal — drop `met_station_id_trgm_idx` (#1669)

## Why

`met.met_station` carries the same defect #1468 fixed on `core.river_segment`: a
trigram GIN built on a bare identifier column. Since pg_trgm 1.6 the
`gin_trgm_ops` operator family answers `=`, so the planner treats such an index
as a candidate for **equality** lookups, and its cost model underestimates
identifier columns systematically (ADR 0004).

ADR 0004 recorded the partial predicate (`WHERE active_flag = true`) as making
this index safe. That was overturned by measurement, twice — most recently on
2026-08-23, where the trap was the **default plan on first contact**, needing no
statistics flip: a 500-row equality batch carrying `active_flag = true` chose
`Bitmap Index Scan on met_station_id_trgm_idx`, 168.60 ms, 30,128 buffers, and
still chose it with sequential, index and index-only scans disabled.

## What changes, and why it changed mid-flight

This change originally mirrored #1468: rebuild the index on `lower(station_id)`.
That was implemented in full and then **withdrawn on evidence**, at the user's
call. The deciding measurement (`stats_reset` is NULL, so these counters cover
the cluster's entire life):

```
met_station_pkey                      idx_scan 101,500,503   2104 kB
met_station_id_trgm_idx               idx_scan       2,502   1664 kB
met_station_basin_idx                 idx_scan          57   1472 kB
met_station_active_basin_station_idx  idx_scan           8    784 kB
met_station_geom_gix                  idx_scan           0     18 MB
met_station_name_trgm_idx             idx_scan           0   2576 kB
```

`met_station_id_trgm_idx` stood at **500** scans before any work on this issue —
exactly the lookup count of PR #1666's E4 probe — and reached 2,502 only because
this change's own baseline probes ran 500-iteration loops against it. The
decisive corroboration is the last row: the id arm and the name arm sit in the
same `OR` in `packages/common/forecast_store.py`, so a real search lights up both
through a `BitmapOr`. `met_station_name_trgm_idx` has **zero** scans in the
cluster's entire history.

Round 1 corrected how much that second observation proves: corroboration, not
proof. `met_station_active_basin_station_idx` can serve the whole search
predicate without touching either GIN, so zero scans on the name index is
*consistent with* searches happening rather than evidence that they did not. See
design.md D1. The decision rests on the 500-scan leg, which stands alone.

So the index is not accelerating anything. Rebuilding it as an expression index
would remove the trap while continuing to maintain, write to, and store an index
with no evidence of a consumer. Dropping it removes the trap *and* the index.

- New migration `000054`: `DROP INDEX CONCURRENTLY` on `met_station_id_trgm_idx`,
  plus concurrent cleanup of any `..._invalid` / `..._legacy` leftovers. No
  `CREATE`. Never takes ACCESS EXCLUSIVE on `met.met_station`.
- `packages/common/forecast_store.py` is **unchanged**. The id arm keeps
  `ms.station_id ILIKE %s ESCAPE '\'`; with no index to serve it, it degrades to
  a filter, measured at 7.3 ms over the `met_station_active_basin_station_idx`
  index-only scan on this 22,965-row table.
- `docs/adr/0004-identifier-trgm-gin-equality-trap.md` records the reversal and
  the general lesson: check whether the index has a consumer before applying the
  expression-index convention to it.
- `tests/test_real_database_integration.py:131-136` drops
  `met_station_id_trgm_idx` from the expected index-name set, and gains an
  assertion that it is gone and unreferenced by the equality plan.

## Non-goals

- `met_station_name_trgm_idx` (2,576 kB, zero scans) and `met_station_geom_gix`
  (18 MB, zero scans). Both are dead weight by the same evidence and both are
  outside this issue's declared boundary; they belong to a met_station index
  review, not here. Reported, not fixed.
- `core.river_segment`'s trigram index. #1468 shipped and deployed the expression
  rebuild there; its `idx_scan` is 4, which raises the same question, but
  reopening a merged and deployed decision is its own proposition.
- `met.met_station` vacuum hygiene — 563 MB of heap for 22,965 live rows. Tracked
  as #1770. It is why the filter path reads as much as it does.
