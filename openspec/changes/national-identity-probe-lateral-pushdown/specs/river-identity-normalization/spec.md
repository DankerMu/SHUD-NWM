# river-identity-normalization

## MODIFIED Requirements

### Requirement: in-boundary river_timeseries readers SHALL filter by surrogate keys with field-identical external responses

Display-boundary readers of `hydro.river_timeseries` SHALL filter by the surrogate key and enum columns as the row-selection authority, and SHALL additionally retain redundant text pushdown predicates on exactly `run_id`, `river_network_version_id`, and `variable` — each conjoined (AND) with its key or enum counterpart — in every fact query whose plan can reach compressed chunks, as declared transitional aids for compressed-chunk `segmentby`/`orderby` predicate pushdown while compression settings remain text-based (user-adjudicated remedy, issue #1341 comment thread; removed together with the text-column drop in #1342, where any missed removal fails loudly because the columns are gone). These pushdown predicates are strict no-ops for key-carrying rows and MUST NOT widen results: NULL-key rows stay excluded by the key predicates. No other text column may appear as a fact predicate, with one positional exception below. The aids apply where the identity arrives as a bound literal; identity that reaches the fact table through an authority-table join stays key-joined only — text-column fact joins remain forbidden outside the sanctioned probe bodies — so such query legs carry only the aids whose identity is bound (typically `variable` alone).
Round-3 amendment (P1 EXPLAIN-gate interception, PR #1443: the set-based national legs lost the per-segment probe path and regressed 0.77s→34.7s), extended by #1596 (the set-based `source_identity_stats` existence probe fully decompressed compressed chunks — 23-37s per probe, and 38s for an empty tile on uncovered compressed instants): inside the three `hydro-national` `CROSS JOIN LATERAL` probe bodies in `services/tiles/mvt.py` — the two per-segment data-leg probes and the per-identity existence probe of `source_identity_stats`, and only there — correlated text equalities are sanctioned as the same class of transitional pushdown aids: `run_id`, `river_network_version_id`, and `river_segment_id` in the data-leg probe bodies, and `run_id` and `river_network_version_id` only in the identity-existence probe body (it has no per-segment correlation). Each is conjoined (AND) with its surrogate-key counterpart in the same probe, each is a strict no-op for key-carrying rows (all are NOT NULL primary-key columns), and all are removed together with the text-column drop in #1342. This positionally widens the user-adjudicated three-column literal-aid set for the lateral probe bodies only — each widening recorded as a deviation in the PR 偏离记录 for user review, since the three-column set was a user-adjudicated remedy. Outside a lateral probe body the prohibition on text-column fact joins stands unchanged, and the shape oracle (`LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS` vs `FORBIDDEN_TEXT_FACT_COLUMNS`) enforces exactly this positional split.
The `source_identity_stats` existence probe for `hydro-national` SHALL locate candidate identities through the same display-coverage-gated discovery shape as `latest_runs` (a `hydro.run_display_coverage` window filter joined through the run/network authority tables, selecting the surrogate keys alongside their text identities) and SHALL verify per-instant existence by touching `hydro.river_timeseries` per identity — answering existence from the coverage window alone is forbidden: the window is a MIN/MAX over complete instants, not a per-instant bitmap, so a coverage-only answer can flip the no-data branch (HTTP 424) into an empty-tile 200 on interior window gaps. The probe's zero branch (no display-ready run, or a covered window whose instant has no fact rows) SHALL remain byte-identical to the pre-change behavior.
This covers `services/tiles/mvt.py`,
`packages/common/display_coverage.py`, and
`apps/api/routes/hydro_display.py`. It also governs any future
identity-predicated fact query under `services/production_closure/`; that set is
empty at delivery time — the directory's `river_timeseries` references are
table-level deny-write probes, an evidence-token string, and one static plan
fixture, none of which carry an identity predicate (per-file disposition in
design.md). The requirement is: resolving caller-supplied text
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
  into the fact table) exists in any in-boundary read shape — except
  inside the three `hydro-national` `CROSS JOIN LATERAL` probe bodies,
  where the amendments above additionally sanction correlated
  text equalities (`run_id`, `river_network_version_id`, and
  `river_segment_id` in the data-leg bodies; `run_id` and
  `river_network_version_id` in the identity-existence probe body),
  each key-paired, removed with #1342; no `ts.`
  fact reference may appear outside those probe bodies in the national
  legs

#### Scenario: Switched shapes are served by the integer index without text-read regression

- **WHEN** the switched query shapes run on the production-scale
  database after the integer discovery index is applied
- **THEN** `EXPLAIN (ANALYZE, BUFFERS)` shows them planned on the
  integer index with no sequential scan of `hydro.river_timeseries`
  and latency no worse than the text-index baseline, while retained
  text indexes keep serving out-of-boundary text readers unchanged;
  shape carve-out (round 3, extended by #1596): the three
  `hydro-national` lateral probe legs instead plan as per-segment (data
  legs) or per-identity (existence probe) parameterized probes on the
  text primary key (uncompressed chunks) and the compressed `segmentby`
  index (compressed chunks) — the integer index remains the planned
  path for every other switched shape, and #1342 owns the post-cutover
  index set that replaces the text plans for these legs

#### Scenario: Compressed-chunk portions keep predicate pushdown via the transitional text predicates

- **WHEN** a switched query shape whose plan reaches a compressed chunk
  (text-based `segmentby`/`orderby` settings still in force) runs with
  the transitional pushdown predicates present
- **THEN** the compressed-chunk portion of the plan shows an index or
  filter condition on the compression-internal relation driven by the
  text `segmentby`/`orderby` columns, not a full-decompression
  sequential scan over all batches

#### Scenario: The national existence probe answers interior coverage-window gaps with the no-data branch

- **WHEN** a display-ready run's coverage window covers the requested
  valid_time but `hydro.river_timeseries` holds no rows for that exact
  instant (an interior window gap), and the `hydro-national` tile is
  requested
- **THEN** `source_identity_count` is 0 and the endpoint returns the
  same HTTP 424 `MVT_LIVE_POSTGIS_UNAVAILABLE` the pre-change probe
  produced — the probe touches the fact table per identity and never
  answers existence from the coverage window alone

#### Scenario: The national existence probe stays sub-second on compressed instants in both branches

- **WHEN** the `hydro-national` tile is requested for a valid_time
  pinned inside a compressed chunk, once for an instant with a
  display-ready covered run and once for an instant with none
- **THEN** the identity-existence probe plans as per-identity
  parameterized probes (no full-decompression sequential scan over all
  batches; decompressed batch counts on the same order as the candidate
  identity count), the covered request serves the same tile bytes as
  before the change, and the uncovered request returns its empty
  response in under one second
