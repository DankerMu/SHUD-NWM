## ADDED Requirements

### Requirement: hydro_run SHALL carry a parse timestamp written by successful parse alone

`hydro.hydro_run` SHALL carry a nullable `parsed_at timestamptz` column that MUST be
stamped by every successful output parse of that run and MUST NOT be written by any
other path. The write MUST be independent of the run's `hydro_run.status`: a re-parse
of an already-`published` run MUST bump `parsed_at`, because the status-gated
`mark_run_parsed` UPDATE excludes `published` and recompute detection consumes this
column. The register upsert and the publish step MUST NOT touch it, and a failed parse
MUST NOT stamp it. The column MUST NOT carry a `DEFAULT`.

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
- **THEN** neither statement references `parsed_at`, pinned by an oracle assertion
  of the same shape as the existing `updated_at` negative assertion

## MODIFIED Requirements

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
this requirement exists to prevent. For legacy runs without a backfillable parse
timestamp `parsed_at` stays NULL, so recompute detection degrades to the init-state
comparison only — a recorded, retention-bounded residual, unchanged in size from the
fact-join form it replaces.

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
  in the delivery receipt together with the size of the legacy cohort

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
