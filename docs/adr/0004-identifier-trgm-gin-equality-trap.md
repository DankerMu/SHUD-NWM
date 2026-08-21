# ADR 0004: Trigram GIN indexes on identifier columns are built on `lower(col)`

Date: 2026-08-21

## Status

Accepted (issue #1468, migration
`db/migrations/000052_authority_stats_hygiene_trgm_expression_index.sql`)

## Context

`core.river_segment.river_segment_id` carried a bare-column trigram index
created by 000031:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS river_segment_id_trgm_idx
  ON core.river_segment USING GIN (river_segment_id gin_trgm_ops);
```

Since **pg_trgm 1.6** (node-27 runs exactly that version, PG 15.2) the
`gin_trgm_ops` operator class supports the `=` operator, not only `LIKE`/`ILIKE`.
The planner therefore treats such an index as a candidate for **equality**
lookups. That is normally harmless — and on this column it was not.

`river_segment_id` values are long slugs sharing a very long prefix
(`basins_jialingjiang_shud_shud_riv_`: 14,673 rows;
`basins_zhaochen_*`: ~9k rows). Every id in a family produces the same leading
trigrams, so those posting lists cover nearly the entire table, and GIN's cost
model badly underestimates the work of intersecting them.

Measured on node-27 production, 2026-08-21, on 2,000 equality lookups of the
shape used by the #1341 backfill campaign
(`rs.river_segment_id = t.river_segment_id AND rs.river_network_version_id =
t.river_network_version_id`):

| plan | wall | buffers | estimated cost |
|---|---|---|---|
| Bitmap Index Scan, `river_segment_id_trgm_idx` (default) | **51,029 ms** | 2,560,171 | 0.72 |
| Index Only Scan, `river_segment_pkey` (`enable_bitmapscan = off`) | **17 ms** | 9,718 | 2.28 |

~2,900x, and the planner chose the slow plan **on purpose**: its estimate was
cheaper. Two candidate explanations were ruled out by measurement rather than by
argument:

- *Stale statistics.* The numbers above were sampled after two manual
  `ANALYZE` runs (2026-08-16 and 2026-08-19). The separate statistics-wipe
  defect that motivated those runs is real and is fixed elsewhere in #1468
  (autopipe stats-guard repair leg + per-table autovacuum parameters), but it is
  **not** what makes the planner pick the trigram index. Fresh statistics do not
  fix this.
- *A session knob.* `PGOPTIONS='-c enable_bitmapscan=off'` rescued the stalled
  campaign, but it only helps the one consumer that remembers to set it, and it
  suppresses bitmap plans wholesale for that session.

Deleting the index was considered: search then degrades from 9 ms to 20 ms
within a basin scope (bitmap on the network column + filter), and — worse — the
list query's three-armed `OR` loses its `BitmapOr`, which makes the two name
trigram indexes dead weight as well.

## Decision

**A trigram GIN index on an identifier column is built on the expression
`lower(col)`, and consumers spell that same expression.**

For `core.river_segment`:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS river_segment_id_trgm_idx
  ON core.river_segment USING GIN (lower(river_segment_id) gin_trgm_ops);
```

and the list search's id arm (`packages/common/model_registry.py`,
`_list_river_segments`) became

```sql
lower(rs.river_segment_id) LIKE %s ESCAPE '\'   -- pattern lowercased in Python
```

This is a **structural** exclusion, not a cost negotiation: a predicate over the
bare column cannot be matched against an index over an expression, so
`river_segment_id = $1` is no longer a candidate at any statistics freshness or
cost estimate. `lower()` rather than a no-op like `col || ''` because it is the
standard PostgreSQL case-insensitive expression-index form, needs no background
knowledge to read, and preserves the hit set: `ILIKE` on a trigram GIN already
matched lowercased trigrams, and these ids are ASCII slugs.

The two name arms (`properties_json->>'name'`, `->>'segment_name'`) keep their
bare-column indexes and `ILIKE`: they are free-text, not identifiers, and no
equality consumer exists for them.

`met.met_station`'s same-shaped `met_station_id_trgm_idx` is **left alone**, and
that is a measured decision, not an oversight: it is a partial index
(`WHERE active_flag = true`), so an equality lookup without that predicate
cannot select it structurally (measured: `met_station_pkey`, 1.7 ms / 500
lookups), and an equality join that does carry the predicate measured onto
`met_station_active_basin_station_idx` via a Hash Join (22 ms / 500). Should a
long-shared-prefix `station_id` equality hotspot ever appear under an
`active_flag` predicate, apply the same expression convention.

## Consequences

- New trigram GIN indexes on identifier-like columns must use
  `lower(col) gin_trgm_ops`; the query that is meant to use them must spell
  `lower(col) LIKE <lowercased pattern>`. A reviewer seeing a bare-column
  trigram index on an identifier column should ask for this ADR.
- The rebuild is an idempotent swap (conditional renames → `CREATE INDEX
  CONCURRENTLY` → `DROP INDEX CONCURRENTLY`) with no transaction wrapper, and
  it never takes ACCESS EXCLUSIVE on `core.river_segment`: an INVALID leftover
  from an interrupted build is RENAMED aside (`..._invalid`) and dropped
  concurrently rather than dropped in place, because a non-concurrent `DROP
  INDEX` locks the TABLE even when the index it removes has no readers. Same
  convention for any future rebuild of an index on a table serving live reads.
  See the migration header for the interrupted-build recovery step.
- Equality consumers (`scripts/node27_river_identity_backfill.py` and any future
  identity join) no longer need `enable_bitmapscan=off`. The one place that used
  it — node-27's `run-campaign-v3.sh`, session-scoped and never in this
  repository — died with the #1341 campaign.
- `tests/test_real_database_integration.py` pins the structural claim on a real
  planner: an equality join must not reference the index, and the
  `lower(...) LIKE` shape must.
- Case-sensitive substring search on `river_segment_id` is no longer expressible
  through this arm. Nothing requests it (the API contract is case-insensitive
  search), but a future case-sensitive requirement would need its own index.

## Revisit

- If pg_trgm or the planner stops offering `=` support for `gin_trgm_ops`, the
  bare-column form becomes safe again — but the expression form costs nothing,
  so there is no reason to revert.
- If a case-insensitive equality consumer of `river_segment_id` appears, it
  *will* be able to select this index (`lower(a) = lower(b)`), which reopens the
  trap with a different predicate. Re-measure before adding one.
- If `river_segment_name_trgm_idx` / `river_segment_segment_name_trgm_idx` are
  retired (both at `idx_scan = 0`, deliberately out of scope for #1468),
  re-examine whether the three-armed `OR` still wants an index on the id arm at
  all.
