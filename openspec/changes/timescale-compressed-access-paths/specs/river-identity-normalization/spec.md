## ADDED Requirements

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

### Requirement: Transitional pushdown aids SHALL be labelled with their true planner effect

The forcing-copyback discovery aid in `services/tile_publisher/forcing_copyback_backfill.py` (`_DISCOVER_BACKFILL_RUNS_SQL`, the `rt.variable = 'q_down'` conjunct) SHALL carry, next to the byte-identical `PUSHDOWN_AID_MARKER` line, a statement that it is an orderby-level batch filter and not a segmentby index pushdown. The other surviving `PUSHDOWN_AID_MARKER` sites (`packages/common/forecast_store.py`, `services/tiles/mvt.py`, `services/tile_publisher/publisher.py`, `workers/output_parser/parser.py`, `packages/common/display_coverage.py`, `apps/api/routes/hydro_display.py`) are #1342's scope and are not relabelled by this change.

#### Scenario: Copyback aid is described as an orderby filter

- **WHEN** a reader inspects `services/tile_publisher/forcing_copyback_backfill.py`'s `rt.variable = 'q_down'` aid
- **THEN** the adjacent comment states it is an orderby-level batch filter, and the marker line is unchanged so the drop-detection oracle stays green

### Requirement: The identity backfill SHALL bound lock waits strictly below its statement wall

Each backfill batch SHALL set `SET LOCAL lock_timeout` from `NODE27_RIVER_IDENTITY_BACKFILL_LOCK_TIMEOUT_MS` (default 5000) beside `SET LOCAL statement_timeout`; configuration SHALL be refused before any batch when `lock_timeout_ms >= duration_wall_ms`; the value SHALL appear in the receipt `bounds` on the clean and stopped paths and be required by the receipt schema.

#### Scenario: Pure lock wait stops as lock_contention

- **WHEN** a batch raises SQLSTATE `55P03`
- **THEN** the run stops at stage `lock_contention`, not `duration_wall`

#### Scenario: Inverted bound is refused at configuration time

- **WHEN** the lock timeout is configured at or above the duration wall
- **THEN** the runner refuses before executing any batch and reports the configuration wire code

#### Scenario: Normal batches do not trip the bound

- **WHEN** a node-27 dry run executes with the default bound
- **THEN** the receipt shows `bounds.lock_timeout_ms = 5000` and no `lock_contention` stop

## MODIFIED Requirements

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
