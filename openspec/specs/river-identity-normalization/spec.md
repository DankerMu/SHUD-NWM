# river-identity-normalization Specification

## Purpose
Governs the surrogate-key river identity on the read and ingest paths: in-boundary readers filter by surrogate keys with field-identical external responses, and the autopipeline judges ingest completeness, parse timestamps and the publish transition by authority state rather than text fact joins.

## Requirements

### Requirement: in-boundary river_timeseries readers SHALL filter by surrogate keys with field-identical external responses

Display-boundary readers of `hydro.river_timeseries` SHALL filter by the surrogate key and enum columns as the row-selection authority, and SHALL additionally retain redundant text pushdown predicates on exactly `run_id`, `river_network_version_id`, and `variable` — each conjoined (AND) with its key or enum counterpart — in every fact query whose plan can reach compressed chunks, as declared transitional aids for compressed-chunk `segmentby`/`orderby` predicate pushdown while compression settings remain text-based (user-adjudicated remedy, issue #1341 comment thread; removed together with the text-column drop in #1342, where any missed removal fails loudly because the columns are gone). These pushdown predicates are strict no-ops for key-carrying rows and MUST NOT widen results: NULL-key rows stay excluded by the key predicates. Store scoping (change `timeseries-narrow-store-expand-contract`): every text pushdown predicate and every sanctioned lateral-probe text equality in this requirement applies ONLY to the `legacy` store variant, rendered against `hydro.river_timeseries_legacy` whose compressed chunks are still text-segmented; the `narrow` store variant, rendered against the narrow `hydro.river_timeseries`, MUST contain none of them (the narrow table has no text identity column) and MUST keep every surrogate-key and enum predicate; a caller holding one run's route executes only that store's variant, while a query that may span both stores unions the two rendered fact-row subrelations inside its owning CTE/LATERAL/source relation before any outer aggregate, `DISTINCT`/ordering/limit or DML; no shared statement-level combinator exists. After the contract batch drops the legacy table, no text identity predicate remains anywhere and this requirement's text-aid clauses are void. No other text column may appear as a fact predicate, with one positional exception below. The aids apply where the identity arrives as a bound literal; identity that reaches the fact table through an authority-table join stays key-joined only — text-column fact joins remain forbidden outside the sanctioned probe bodies — so such query legs carry only the aids whose identity is bound (typically `variable` alone).
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

### Requirement: The autopipeline ingest-completeness criterion SHALL judge NULL-key legacy runs by authority state, never by text fact joins

`scripts/node27_autopipeline.py::_already_ingested_runs` MUST treat a
`hydro.hydro_run` row at `status = 'published'` as fully ingested whether or
not any `hydro.river_timeseries` row exists for it, MUST keep requiring evidence
of a completed parse for `status = 'parsed'`, MUST keep retiring
`status = 'superseded'` unconditionally, and MUST NOT reference
`hydro.river_timeseries` at all — neither through a text identity predicate nor
through the surrogate key. The completeness gate MUST be
`h.status = 'published' OR h.parsed_at IS NOT NULL`; a bare `h.parsed_at IS NOT NULL`
is forbidden because backfill leaves the NULL-key legacy cohort at NULL and a bare
gate would judge those published runs incomplete, re-triggering the per-cycle handoff
this requirement exists to prevent. For runs whose fact rows cannot be aggregated by
`run_key` — whether because the rows carry a NULL key or because retention dropped the
chunks that held them — `parsed_at` stays NULL, so recompute detection degrades to the
init-state comparison only. That residual MUST be reported by the delivery receipt as the
count of runs left unstamped, broken down by `hydro_run.status`, and MUST NOT be reported
solely as a NULL-key count: on node-27 the NULL-key count is zero while the genuinely
unstamped published cohort is a majority-scale fraction. The residual is unchanged in size
from the fact-join form it replaces, because the backfill aggregates by the same key over
the same rows.

#### Scenario: published run whose fact rows are NULL-key in a compressed chunk is complete

- **GIVEN** a `published` run whose only `river_timeseries` rows carry
  `run_key IS NULL` and sit in a compressed chunk the backfill runner skipped
- **WHEN** the autopipeline evaluates `_already_ingested_runs` for it
- **THEN** the run is in the returned set, no forcing handoff is re-attempted
  for it, and the statement references no `hydro.river_timeseries` column at all

#### Scenario: parsed run without a parse timestamp stays incomplete

- **WHEN** a `parsed` run has `parsed_at IS NULL`
- **THEN** it is not in the returned set and the pipeline keeps retrying it

#### Scenario: the completeness statement touches no fact table

- **WHEN** the SQL of `_already_ingested_runs` is extracted statically
- **THEN** it contains zero occurrences of `hydro.river_timeseries`, and the
  per-file census register records one such statement for
  `scripts/node27_autopipeline.py` rather than two

#### Scenario: legacy published run keeps init-state recompute detection only

- **GIVEN** a `published` legacy run whose `parsed_at` is NULL after backfill
- **WHEN** `_already_ingested_runs` runs with an `object_store_root` whose
  manifest `initial_state` differs from `hydro_run.init_state_id`
- **THEN** the run is not in the returned set
- **AND WHEN** the manifest agrees and only the product mtime is newer
- **THEN** the run stays in the returned set, and that residual is recorded
  in the delivery receipt as the unstamped count broken down by `hydro_run.status`,
  not as a NULL-key count alone

#### Scenario: a recomputed published run, once re-parsed, stays current on subsequent ticks

- **GIVEN** a `published` run that was re-parsed after its product was regenerated
- **WHEN** the next tick evaluates `_already_ingested_runs`
- **THEN** the run's `parsed_at` is newer than the product mtime, so it is treated as
  current and is NOT handed off again — and this holds tick after tick, with no
  per-tick re-ingest loop

#### Scenario: the criterion runs without decompressing a compressed chunk

- **WHEN** `EXPLAIN (ANALYZE, BUFFERS)` runs the criterion on node-27 against the
  production population
- **THEN** the plan contains no `DecompressChunk`, the statement completes within
  `statement_timeout`, and a no-op tick's `phase=ingest elapsed_sec` returns to the
  ~240 s envelope
- **AND** at least two consecutive ticks end `rc=0` within minutes, with no new forcing
  handoff attempted for the already-ingested published population on the second tick,
  recorded in the delivery receipt

### Requirement: hydro_run SHALL carry a parse timestamp written by successful parse alone

`hydro.hydro_run` SHALL carry a nullable `parsed_at timestamptz` column that MUST be
stamped by every successful output parse of that run and MUST NOT be written by any
other path. The write MUST be independent of the run's `hydro_run.status`: a re-parse
of an already-`published` run MUST bump `parsed_at`, because the status-gated
`mark_run_parsed` UPDATE excludes `published` and recompute detection consumes this
column. The register upsert and the publish step MUST NOT write it (the publish step
MAY read it as its authority predicate), and a failed parse MUST NOT stamp it. The
column MUST NOT carry a `DEFAULT`.

#### Scenario: re-parsing a published run bumps parsed_at without changing status

- **GIVEN** a run at `status = 'published'` with an existing `parsed_at`
- **WHEN** its output is parsed again successfully
- **THEN** `hydro_run.parsed_at` advances to the new parse time
- **AND** `hydro_run.status` is still `'published'`（未被 downgrade 回 `'parsed'`）

#### Scenario: a parse-ready run stamps parsed_at along with its status transition

- **GIVEN** a run at `status = 'succeeded'`
- **WHEN** its output is parsed successfully
- **THEN** `hydro_run.status` becomes `'parsed'` and `parsed_at` carries the parse time

#### Scenario: a failed parse leaves parsed_at untouched

- **WHEN** an output parse raises and the run is marked failed
- **THEN** `hydro_run.parsed_at` is unchanged from its prior value (NULL stays NULL)

#### Scenario: register and publish never write parsed_at

- **WHEN** the autopipeline register upsert runs for a run, or the publish step
  promotes a `parsed` run to `published`
- **THEN** neither statement carries `parsed_at` in its SET clause, pinned by an
  oracle assertion on the SET clause of the same shape as the existing `updated_at`
  negative assertion; the publish step's `WHERE parsed_at IS NOT NULL` read is the
  sanctioned authority predicate

### Requirement: The autopipeline publish transition SHALL key on authority state alone

`_publish_display_runs` SHALL advance a run from `parsed` to `published` on `hydro.hydro_run.status = 'parsed' AND parsed_at IS NOT NULL`, touching no fact table and carrying no transitional pushdown aid; it SHALL NOT modify `updated_at`, and legacy runs already `published` SHALL remain outside the predicate.

#### Scenario: Parsed run with a parse timestamp is published

- **WHEN** a run has `status = 'parsed'` and a non-NULL `parsed_at`
- **THEN** it becomes `published` and its `updated_at` is unchanged

#### Scenario: No fact-table access

- **WHEN** the publish statement is planned
- **THEN** the plan references only `hydro.hydro_run`, and the shape oracle counts zero `hydro.river_timeseries` references in the statement

#### Scenario: Integration seeds carry the timestamp only for completed parses

- **WHEN** a test helper seeds a run at `status = 'parsed'` together with its fact rows to represent a completed parse
- **THEN** it also stamps `parsed_at`, so the publish step publishes that run
- **AND** a seeded `parsed` run without fact rows may leave `parsed_at` NULL and is not published
- **AND** a helper that deliberately seeds `parsed` + fact rows with `parsed_at` NULL (the counterfactual the old fact-table probe misread as a completed parse) is a discriminating oracle row, not a completed parse, and is not published

#### Scenario: Manual status without timestamp stays put

- **WHEN** a run has `status = 'parsed'` and `parsed_at IS NULL`
- **THEN** it is not published
