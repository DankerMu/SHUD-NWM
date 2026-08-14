# timeseries-index-hygiene Specification

## Purpose
TBD - created by archiving change drop-redundant-river-ts-indexes. Update Purpose after archive.
## Requirements
### Requirement: Redundant timeseries-index removal MUST be evidence-gated and replay-safe

Dropping an index on a production timeseries hypertable SHALL be justified by live scan/size measurements (per-index `idx_scan` and `pg_total_relation_size` across all chunks), SHALL be verified by before/after `EXPLAIN (ANALYZE, BUFFERS)` captures of the in-repo query surfaces that name-match the dropped columns — or of single-table predicate-shape proxies of those surfaces, provided the proxy relationship is recorded in the change's design (a proxy proves shape-index usage, not the real query's plan node type) showing no plan regression, and SHALL record the reclaimed bytes. The migration SHALL use `DROP INDEX CONCURRENTLY IF EXISTS` so it neither blocks concurrent reads nor fails on databases where the index never existed (including indexes created out-of-band on production only). In-repo evidence statements that name a dropped index SHALL be realigned in the same change.

#### Scenario: evidence-backed drop

- **WHEN** a migration drops an index whose measured carrying cost (size, scan counts) is weighed against its measured in-repo query surfaces as a recorded tradeoff — with every coverage loss enumerated and bounded by a before/after `EXPLAIN` gate — and post-drop plans show no Seq Scan fallback or order-of-magnitude slowdown on the matched query surfaces
- **THEN** the drop stands, and the recorded receipt carries the before/after sizes and plans

#### Scenario: plan regression after drop

- **WHEN** any post-drop plan on the matched query surfaces regresses to a sequential scan or degrades by an order of magnitude
- **THEN** the index SHALL be rebuilt (drops are reversible by re-creation) and the removal withdrawn

#### Scenario: replay on a database where the index never existed

- **WHEN** the migration chain replays on a freshly built database that never had an out-of-band index
- **THEN** the drop statement SHALL succeed as a no-op via `IF EXISTS`

