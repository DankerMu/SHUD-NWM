## MODIFIED Requirements

### Requirement: in-boundary river_timeseries readers SHALL filter by surrogate keys with field-identical external responses

Display-boundary readers of `hydro.river_timeseries` SHALL filter by the surrogate key and enum columns as the row-selection authority, and SHALL additionally retain redundant text pushdown predicates on exactly `run_id`, `river_network_version_id`, and `variable` — each conjoined (AND) with its key or enum counterpart — in every fact query whose plan can reach compressed chunks, as declared transitional aids for compressed-chunk `segmentby`/`orderby` predicate pushdown while compression settings remain text-based (user-adjudicated remedy, issue #1341 comment thread; removed together with the text-column drop in #1342, where any missed removal fails loudly because the columns are gone). These pushdown predicates are strict no-ops for key-carrying rows and MUST NOT widen results: NULL-key rows stay excluded by the key predicates. Store scoping (change `timeseries-narrow-store-expand-contract`): every text pushdown predicate and every sanctioned lateral-probe text equality in this requirement applies ONLY to the `legacy` store variant, rendered against `hydro.river_timeseries_legacy` whose compressed chunks are still text-segmented; the `narrow` store variant, rendered against the narrow `hydro.river_timeseries`, MUST contain none of them (the narrow table has no text identity column) and MUST keep every surrogate-key and enum predicate; a query that may span both stores is a `UNION ALL` of the two variants. After the contract batch drops the legacy table, no text identity predicate remains anywhere and this requirement's text-aid clauses are void. No other text column may appear as a fact predicate, with one positional exception below. The aids apply where the identity arrives as a bound literal; identity that reaches the fact table through an authority-table join stays key-joined only — text-column fact joins remain forbidden outside the sanctioned probe bodies — so such query legs carry only the aids whose identity is bound (typically `variable` alone).
Round-3 amendment (P1 EXPLAIN-gate interception, PR #1443: the set-based national legs lost the per-segment probe path and regressed 0.77s→34.7s), extended by #1596 (the set-based `source_identity_stats` existence probe fully decompressed compressed chunks — 23-37s per probe, and 38s for an empty tile on uncovered compressed instants): inside the three `hydro-national` `CROSS JOIN LATERAL` probe bodies in `services/tiles/mvt.py` — the two per-segment data-leg probes and the per-identity existence probe of `source_identity_stats`, and only there — correlated text equalities are sanctioned as the same class of transitional pushdown aids: `run_id`, `river_network_version_id`, and `river_segment_id` in the data-leg probe bodies, and `run_id` and `river_network_version_id` only in the identity-existence probe body (it has no per-segment correlation). Each is conjoined (AND) with its surrogate-key counterpart in the same probe, each is a strict no-op for key-carrying rows (all are NOT NULL primary-key columns), and all are removed together with the text-column drop in #1342. The identity-existence probe's #1342 survival is split by chunk state: the cutover layout mirrors the text layout column-for-column (surrogate primary key `run_key, river_network_version_key, river_segment_key, variable_e, valid_time`; `compress_segmentby` on the first three key columns — migration 000050), so after the aids are removed with the text columns the probe binds the same positional subset (PK positions 1, 2, 4, 5; segmentby 2 of 3) through its surrogate-key predicates — on compressed chunks its segmentby plan therefore survives unchanged; on uncompressed chunks its pre-cutover index pick was measured, not presumed (PR #1657 E4 receipt): the text primary key's run-scoped prefix on the hit branch, and the retained single-column `river_timeseries_valid_time_idx` on the interior-gap miss branch — neither of which #1342 removes, since the former mirrors onto the surrogate primary key at the same positions and the latter indexes the time dimension the cutover does not touch; the post-cutover index set is owned by #1342. Store scoping of this survival analysis (change `timeseries-narrow-store-expand-contract`): the layout it describes is the never-executed 000050 cutover layout and applies to no store; the narrow store's layout is primary key `(run_key, river_segment_key, variable_e, valid_time)` with `compress_segmentby (run_key, river_segment_key)`, on which the identity-existence probe binds `run_key` (segmentby 1 of 2) and prunes by run batch on compressed chunks, and its compressed and uncompressed plans are recorded, not presumed, by the rollout receipt (`timeseries-narrow-store`). This positionally widens the user-adjudicated three-column literal-aid set for the lateral probe bodies only — each widening recorded as a deviation in the PR 偏离记录 for user review, since the three-column set was a user-adjudicated remedy. Outside a lateral probe body the prohibition on text-column fact joins stands unchanged, and the shape oracle (`LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS` vs `FORBIDDEN_TEXT_FACT_COLUMNS`) enforces exactly this positional split.
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

- **WHEN** an in-boundary fact query rendered for the `legacy` store contains a text identity predicate
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

#### Scenario: The narrow store variant carries no text identity predicate

- **WHEN** the same in-boundary template is rendered for the `narrow` store
- **THEN** the rendered statement references `hydro.river_timeseries` only, contains no predicate, join or projection on `run_id`, `basin_version_id`, `river_network_version_id`, `river_segment_id`, `variable`, `unit` or `quality_flag` of the fact table, and every surrogate-key and enum predicate of the legacy variant is present unchanged

#### Scenario: Switched shapes are served by the integer index without text-read regression

- **WHEN** the switched query shapes run on the production-scale
  database after the integer discovery index is applied
- **THEN** `EXPLAIN (ANALYZE, BUFFERS)` shows them planned on the
  integer index with no sequential scan of `hydro.river_timeseries`
  and latency no worse than the text-index baseline, while retained
  text indexes keep serving out-of-boundary text readers unchanged;
  shape carve-out (round 3, extended by #1596): the three
  `hydro-national` lateral probe legs instead plan as per-segment (data
  legs) or per-identity (existence probe) parameterized probes — on
  compressed chunks all three probe through the compressed `segmentby`
  index; on uncompressed chunks the data legs plan on the text primary
  key (measured in PR #1443) while the identity probe's pick is
  recorded, not presumed, by the delivery receipt — measured in PR
  #1657 as the text primary key's run-scoped prefix on the hit branch
  and the retained single-column `river_timeseries_valid_time_idx` on
  the interior-gap miss branch, with run / network / variable falling
  to filters; that measurement is the legacy store's; the narrow store creates no single-column `valid_time` index, and the identity-existence probe's miss branch on the narrow store is recorded, not presumed, by the rollout receipt against the narrow discovery index `(run_key, basin_version_key, river_network_version_key, variable_e, valid_time DESC)` with the before/after `EXPLAIN (ANALYZE, BUFFERS)` evidence `timeseries-index-hygiene` requires for any index that disappears;
  the integer index remains the planned path for every other switched
  shape, and the narrow store's three-index set (`timeseries-narrow-store`) replaces the text plans for these legs for every run routed `narrow`

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
  parameterized probes: no full-decompression sequential scan over all
  batches, the fact-side inner node's loop count equals the number of
  identities probed before short-circuit (leading misses plus one when
  the covered instant has rows; every candidate when the covered
  instant is an interior gap; the uncovered request finds no candidates
  and never touches the fact table), and shared buffer touches on the
  compressed-chunk relations do not exceed the pre-change shape's on
  the same instant in the same session; a covered instant with rows
  serves the same tile bytes as before the change, a covered interior
  gap returns the no-data branch of the preceding scenario, and the
  uncovered request returns its empty response in under one second

## REMOVED Requirements

### Requirement: river_timeseries identity columns SHALL have integer surrogate-key targets with an idempotent, bounded, receipted backfill
**Reason**: The narrow `hydro.river_timeseries` is born with NOT NULL surrogate-key columns, so there is no fact-table backfill to run, receipt or bound, and the never-executed `hydro.cutover_river_identity_normalization()` / `hydro.verify_river_identity_normalization()` pair and `scripts/node27_river_identity_backfill.py` are deleted by the river contract batch.
**Migration**: The authority-table surrogate keys this requirement introduced (`hydro.hydro_run.run_key`, `core.basin_version.basin_version_key`, `core.river_network_version.river_network_version_key`, `core.river_segment.river_segment_key`, migration 000050) stay in place and are the identity authority of the narrow table (`timeseries-narrow-store`, "The river fact table SHALL be a narrow surrogate-key hypertable…"); the legacy table keeps its already-backfilled key columns until retention empties it (`timeseries-store-expand-contract`).

### Requirement: river_timeseries writers SHALL dual-write surrogate identity columns atomically with the text columns
**Reason**: The narrow fact table has no text identity columns, so there is nothing to dual-write; the parser writes surrogate keys and enums only (`timeseries-narrow-store`, "The parser SHALL write only the narrow table and refuse legacy runs fail-closed").
**Migration**: Runs already parsed into the text-keyed table stay readable through the `legacy` store variant until retention empties `hydro.river_timeseries_legacy`; new runs are written narrow-only; the dual-write oracle `tests/test_output_parser_dual_write.py` is re-pinned as the narrow-write oracle.

### Requirement: Out-of-boundary river_timeseries consumers SHALL filter and emit identity by surrogate keys with per-group sanctioned transitional aids
**Reason**: The sanctioned transitional text pushdown aids existed only because the compressed chunks of the text-keyed fact table were segmented by text columns. With the narrow key-segmented table the aids have no planner effect and the text columns no longer exist; keeping the per-group allowance would let dead predicates survive.
**Migration**: During the expand–contract window the legacy read variant keeps the aids verbatim (`timeseries-narrow-store`, "Read paths SHALL render one template into a per-store variant and union across stores"); the contract batch deletes every marked aid line and the legacy variant, and the cleanup oracles assert that no fact-table SQL references any text identity column (`timeseries-store-expand-contract`, "Contract SHALL remove every transitional aid and the legacy read variant").

### Requirement: The parser's river_timeseries replace chain SHALL locate rows by surrogate keys end to end
**Reason**: The replace chain's `run_id = %s` transitional aid and its marker exist only for the text-segmented legacy table; the narrow table's compression segmentby leads with `run_key`, so the probe and window statements are pushed down by the key alone.
**Migration**: `timeseries-narrow-store` "The parser SHALL write only the narrow table and refuse legacy runs fail-closed" carries the surviving clauses: replace granularity (same run + network + variable, closed `valid_time` window with both bound literals in one statement), guard-window inputs unchanged, DELETE located by surrogate keys; the aid and marker are deleted with the legacy variant at contract.

### Requirement: The forcing-copyback transitional pushdown aid SHALL be labelled with its true planner effect
**Reason**: The forcing-copyback aid is deleted with every other transitional aid when the legacy table is dropped; there is nothing left to label.
**Migration**: Until the contract batch the aid stays in the legacy variant with its existing label; the narrow variant of `services/tile_publisher/forcing_copyback_backfill.py` renders without it and the copyback schema precheck accepts the narrow column set (`timeseries-narrow-store`, non-template consumers).

### Requirement: The identity backfill SHALL bound lock waits strictly below its statement wall
**Reason**: `scripts/node27_river_identity_backfill.py` is deleted by the river contract batch: the narrow table is born with NOT NULL key columns and the legacy table is dropped, so no identity backfill can ever run again.
**Migration**: The lock-bound discipline this requirement pinned survives in the retention and compression runners' own lock bounds (`timeseries-db-retention`, `hypertable-compression`); the backfill runner, its tests and its receipt schema are removed together (`timeseries-store-expand-contract`, "Contract SHALL remove every transitional aid and the legacy read variant").

