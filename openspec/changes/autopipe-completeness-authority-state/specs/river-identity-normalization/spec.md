## ADDED Requirements

### Requirement: The autopipeline ingest-completeness criterion SHALL judge NULL-key legacy runs by authority state, never by text fact joins

`scripts/node27_autopipeline.py::_already_ingested_runs` MUST treat a
`hydro.hydro_run` row at `status = 'published'` as fully ingested whether or
not any `hydro.river_timeseries` row is visible to it through the surrogate
key (`rt.run_key = h.run_key`), MUST keep requiring at least one key-visible
row for `status = 'parsed'`, MUST keep retiring `status = 'superseded'`
unconditionally, and MUST NOT re-admit NULL-key legacy rows through a text
identity predicate. For legacy runs without key-visible rows `parsed_at` is
NULL, so recompute detection degrades to the init-state comparison only — a
recorded, retention-bounded residual, not a silent one.

#### Scenario: published run whose fact rows are NULL-key in a compressed chunk is complete

- **GIVEN** a `published` run whose only `river_timeseries` rows carry
  `run_key IS NULL` and sit in a compressed chunk the backfill runner skipped
- **WHEN** the autopipeline evaluates `_already_ingested_runs` for it
- **THEN** the run is in the returned set, no forcing handoff is re-attempted
  for it, and the statement still carries `ON rt.run_key = h.run_key` with no
  text column of `rt` referenced

#### Scenario: parsed run without key-visible rows stays incomplete

- **WHEN** a `parsed` run has no `river_timeseries` row matching its `run_key`
- **THEN** it is not in the returned set and the pipeline keeps retrying it

#### Scenario: legacy published run keeps init-state recompute detection only

- **GIVEN** a `published` legacy run with no key-visible rows
- **WHEN** `_already_ingested_runs` runs with an `object_store_root` whose
  manifest `initial_state` differs from `hydro_run.init_state_id`
- **THEN** the run is not in the returned set
- **AND WHEN** the manifest agrees and only the product mtime is newer
- **THEN** the run stays in the returned set, and that residual is recorded
  in the delivery receipt together with the size of the legacy cohort

#### Scenario: node-27 tick returns to its pre-regression envelope

- **WHEN** the fix is deployed on node-27 and the next autopipe tick runs
- **THEN** `already_ingested` returns to the full published population,
  `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` occurrences fall to at most the
  pre-regression baseline, and at least two consecutive ticks end `rc=0`
  within minutes, recorded in the delivery receipt
