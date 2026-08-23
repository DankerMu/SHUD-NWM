# Design

## Risk triage

- Fixture level: **compact**. One migration containing three concurrent drops, one
  ADR paragraph, and test edits. The issue estimated S for the rebuild; the drop
  is smaller still, and no application code changes at all.
- Risk packs selected:
  1. **Live-table DDL lock discipline.** `met.met_station` serves display and MVT
     reads. A non-concurrent `DROP INDEX` takes ACCESS EXCLUSIVE on the *table*,
     even though the index it removes has no readers — that is the whole reason
     `000052` renames INVALID leftovers aside and drops them concurrently.
  2. **Irreversibility.** Dropping an index cannot be undone by a transaction
     rollback. Mitigation is that it is trivially rebuildable and its original
     definition is preserved verbatim in
     `db/migrations/000031_search_discovery_performance.sql:12-14`; the migration
     header repeats it so a reader does not have to go looking.
- Risk packs not selected, with reason: search-result equivalence — no query text
  changes, so the result set is unchanged by construction, not by argument
  (this was a selected pack in the withdrawn rebuild design and is now moot);
  oracle integrity — an assertion is *removed* here (the index-name set), which
  is exactly the kind of edit that needs justifying, so see D3; geospatial/CRS,
  SHUD numerics, Slurm lifecycle, run-manifest provenance — untouched.

## Must-preserve behavior

- **Station search keeps returning the same rows.** Guaranteed structurally: no
  SQL and no Python changes. The `ILIKE` predicate is unchanged; only its access
  path changes.
- **No ACCESS EXCLUSIVE on `met.met_station`** at any point, including the
  leftover-cleanup path.
- `met_station_name_trgm_idx` is not touched, despite being dead weight by the
  same evidence. Out of scope by the issue's own boundary; reported instead.
- A fresh database built from `db/migrations/` in order ends with the index
  absent: `000031` creates it, `000054` drops it. Correct, and idempotent on
  re-run.

## Seams under test

- The migration's statement shape, read statically: every `DROP INDEX` carries
  `CONCURRENTLY`, no transaction wrapper, no `CREATE`.
- The real planner on node-27: the index is gone, and the equality shape that
  used to select it no longer can.
- The search path still returns the same stations with the index gone.

## Decisions

### D1 — Drop, do not rebuild

The evidence is in proposal.md and is not restated here. The short form: with
`stats_reset` NULL, `met_station_id_trgm_idx` had **500** scans before this work
— exactly PR #1666's E4 probe count — and its sibling `met_station_name_trgm_idx`,
which a real search would light up through the same `BitmapOr`, has **zero**
scans in the cluster's entire history. An index with no evidence of a consumer is
not worth a rebuild.

**Round-1 correction — the second observation is corroboration, not proof, and
the paragraph above states it as though it were.** The `BitmapOr` inference
assumes a real search would necessarily touch one of the two GINs. It would not.
`packages/common/forecast_store.py:1058-1059` — the `else` branch, the only one
carrying `active_flag = true` and therefore the only one where either partial
trigram index is structurally eligible — **also carries
`ms.basin_version_id = %s`**. That hands the planner
`met_station_active_basin_station_idx`, which
`db/migrations/000033_station_mvt_active_source_index.sql` defines as
`(basin_version_id, station_id) WHERE active_flag = true`: it can satisfy the
equality predicates by itself and apply the whole `OR` as a plain row filter,
touching neither GIN. This change's own baseline receipt shows exactly that
routing, at 7.3 ms. So searches could have run organically, many times, and still
left `met_station_name_trgm_idx` at zero scans.

The decision does not depend on this leg. The 500-scan leg carries it alone:
500 lifetime scans matching PR #1666's E4 probe count exactly means no
unexplained organic usage of the index being dropped. The zero-scan observation
stays as what it is — consistent with the conclusion, not evidence for it.

The sharper lesson belongs in ADR 0004 next to the first one: when reading
`idx_scan` as evidence of non-use, check what *other* index could be serving the
query. A zero count proves the index was not chosen. It never proves the query
did not run.

Worth stating plainly because of where it happened: ADR 0004 exists to stop an
unmeasured inherited assumption from being re-asserted as fact, and this change
did exactly that inside ADR 0004 itself, one paragraph after correcting the
previous instance.

**This reverses the issue's own recommendation and this change's first
implementation, and it should be read as a correction, not a preference.** The
withdrawn design's D4 rejected deletion on the grounds that it would leave
`met_station_name_trgm_idx` as dead weight in the same query. That argument's
premise was false: the name index is *already* dead weight, and was measurably so
at the time the argument was written. It was inherited from the issue and from
ADR 0004 without being measured. The general lesson goes into ADR 0004: before
applying the expression-index convention to an index, check whether it has a
consumer — the convention exists to keep a *useful* index while removing the
trap, and it is the wrong tool when the index is not useful.

### D2 — Concurrent drops only, and clean up the leftover names

Three statements, all `DROP INDEX CONCURRENTLY IF EXISTS`:
`met_station_id_trgm_idx`, `met_station_id_trgm_idx_invalid`,
`met_station_id_trgm_idx_legacy`. The last two cannot exist today — nothing has
ever created them — but the rebuild-shaped `000052` idiom creates them
transiently, and a half-applied earlier attempt at this change's own withdrawn
version could leave one behind. Dropping unconditionally with `IF EXISTS` costs
nothing and closes that door.

`CONCURRENTLY` is not decoration and not the same as "it has no readers": a plain
`DROP INDEX` takes ACCESS EXCLUSIVE on the table. `DROP INDEX CONCURRENTLY`
cannot run inside a transaction block, which is why the file has no wrapper.

### D3 — Removing an assertion, deliberately

`tests/test_real_database_integration.py:131-136` asserts a literal set of index
names that must exist, and `met_station_id_trgm_idx` is in it. That entry has to
go, and removing an assertion to make a change pass is normally the exact thing
this repository refuses.

It is legitimate here because the assertion is being **replaced by a stronger
one, not deleted**: the name moves from "must exist" to an explicit "must not
exist", and the equality-plan assertion that the withdrawn design added stays,
now asserting the index cannot be referenced because it is gone rather than
because it is unmatched. Net assertions increase. The receipts must show the
diff, not merely claim this.

### D4 — Residual risk

If station search is ever exercised at scale in production, the id arm becomes a
filter over the heap. On today's table that measured 7.3 ms via the
`met_station_active_basin_station_idx` index-only scan and returned the same 386
rows the trigram path returned in 45.6 ms — the trigram path was *slower*, because
`met.met_station` is 563 MB of heap for 22,965 live rows (#1770). Should the table
grow by orders of magnitude and search actually get used, the answer is a fresh
index built as `lower(station_id)` per ADR 0004's convention — the convention
survives this change, only its application to this particular index does not.

Migration numbering: `000053_state_snapshot_clone_gate_kind.sql` exists, so this
is `000054`. A collision surfaces as a migration-ordering failure, not silently.
