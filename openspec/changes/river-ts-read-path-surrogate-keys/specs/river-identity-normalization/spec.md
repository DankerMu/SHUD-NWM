# river-identity-normalization delta

## ADDED Requirements

### Requirement: in-boundary river_timeseries readers SHALL filter by surrogate keys with field-identical external responses

Display-boundary readers of `hydro.river_timeseries` SHALL filter by the surrogate key and enum columns as the row-selection authority, and SHALL additionally retain redundant text pushdown predicates on exactly `run_id`, `river_network_version_id`, and `variable` — each conjoined (AND) with its key or enum counterpart — in every fact query whose plan can reach compressed chunks, as declared transitional aids for compressed-chunk `segmentby`/`orderby` predicate pushdown while compression settings remain text-based (user-adjudicated remedy, issue #1341 comment thread; removed together with the text-column drop in #1342, where any missed removal fails loudly because the columns are gone). These pushdown predicates are strict no-ops for key-carrying rows and MUST NOT widen results: NULL-key rows stay excluded by the key predicates. No other text column may appear as a fact predicate. The aids apply where the identity arrives as a bound literal; identity that reaches the fact table through an authority-table join stays key-joined only — text-column fact joins remain forbidden — so such query legs carry only the aids whose identity is bound (typically `variable` alone).
This covers `services/tiles/mvt.py`,
`packages/common/display_coverage.py`,
`apps/api/routes/hydro_display.py`, and the identity-predicated
validation queries under `services/production_closure/`: resolving caller-supplied text
identity through the four authority tables and restoring text output
via authority joins or enum-to-text casts, so that external responses
remain field-identical to the text-predicate era: JSON responses
byte-identical, MVT tiles equal as decoded feature sets, `feature_id`
concatenation byte-identical, and any ordering over identity columns
expressed on the restored text values. An unknown identity value or an
out-of-vocabulary enum literal SHALL yield the same empty result the
text predicates produced, never a SQL error. The switched read shapes
SHALL be served by an integer discovery index on `(run_key,
basin_version_key, river_network_version_key, variable_e, valid_time
DESC)` added by migration without dropping any existing text index;
text columns and text indexes remain authoritative for rollback and
for out-of-boundary readers until their separately delivered
retirement. Legacy rows whose surrogate keys remain NULL (only rows
outside the receipted backfill scope, i.e. compressed chunks pending
retention) are invisible to key-filtered reads; this exclusion is an
explicit, recorded contract with a bounded convergence deadline, not
silent data loss.

#### Scenario: Switched reads are field-identical for resolvable identities

- **WHEN** the same display request (tile, valid_times, coverage, or
  existence probe) is issued for an identity whose rows all carry
  surrogate keys, before and after the read-path switch
- **THEN** JSON responses are byte-identical, MVT tiles decode to equal
  feature sets (all properties including the `feature_id`
  concatenation, and geometry), and response ordering is unchanged

#### Scenario: Unknown or out-of-vocabulary identity degrades to empty, not error

- **WHEN** a switched query binds a `run_id` absent from
  `hydro.hydro_run` or a `variable` literal outside
  `hydro.river_variable`
- **THEN** the query returns the empty result the text predicates
  returned, and no enum-cast or other SQL error escapes to the caller

#### Scenario: NULL-key legacy rows are excluded as a recorded, converging contract

- **WHEN** rows with NULL surrogate keys exist in compressed chunks
  that the backfill runner cannot update
- **THEN** key-filtered reads exclude those rows, the exclusion scope
  (chunk ranges, row counts, retention deadline) is recorded in the
  delivery evidence, and no in-boundary reader re-admits NULL-key rows
  through text predicates: the sanctioned transitional pushdown
  predicates are conjunctive and can only narrow, never widen, the
  key-filtered result

#### Scenario: Transitional text pushdown predicates are bounded to the sanctioned set and paired with keys

- **WHEN** an in-boundary fact query contains a text identity predicate
- **THEN** that predicate is on `run_id`, `river_network_version_id`,
  or `variable` only, appears in the same conjunction as its surrogate
  key or enum counterpart, and no text predicate on
  `basin_version_id` or `river_segment_id` (nor any text-column join
  into the fact table) exists in any in-boundary read shape

#### Scenario: Switched shapes are served by the integer index without text-read regression

- **WHEN** the switched query shapes run on the production-scale
  database after the integer discovery index is applied
- **THEN** `EXPLAIN (ANALYZE, BUFFERS)` shows them planned on the
  integer index with no sequential scan of `hydro.river_timeseries`
  and latency no worse than the text-index baseline, while retained
  text indexes keep serving out-of-boundary text readers unchanged

#### Scenario: Compressed-chunk portions keep predicate pushdown via the transitional text predicates

- **WHEN** a switched query shape whose plan reaches a compressed chunk
  (text-based `segmentby`/`orderby` settings still in force) runs with
  the transitional pushdown predicates present
- **THEN** the compressed-chunk portion of the plan shows an index or
  filter condition on the compression-internal relation driven by the
  text `segmentby`/`orderby` columns, not a full-decompression
  sequential scan over all batches
