# river-identity-normalization

## MODIFIED Requirements

### Requirement: The autopipeline ingest-completeness criterion SHALL judge NULL-key legacy runs by authority state, never by text fact joins

`scripts/node27_autopipeline.py::_already_ingested_runs` MUST treat a
`hydro.hydro_run` row at `status = 'published'` as fully ingested whether or
not any `hydro.river_timeseries` row is visible to it through the surrogate
key (`rt.run_key = h.run_key`), MUST keep requiring at least one key-visible
row for `status = 'parsed'`, MUST keep retiring `status = 'superseded'`
unconditionally, and MUST NOT re-admit NULL-key legacy rows through a text
identity predicate.

Amendment (#1686): the key join MAY additionally carry exactly one sanctioned
transitional pushdown aid — `rt.run_id = ANY(<the same bound run_id array that
the statement's `WHERE h.run_id = ANY(...)` binds)` — for the same reason and
under the same terms as the aids sanctioned for the display boundary and the
lateral probe bodies: `run_id` is `compress_segmentby` column 1 on
`hydro.river_timeseries` while compression settings remain text-based, so
without it every compressed chunk is fully decompressed on every tick. The aid
is removed together with the text-column drop in #1342, where a missed removal
fails loudly because the column is gone. The aid SHALL appear in the `ON`
clause of the `LEFT JOIN`, never in `WHERE`: in `WHERE` it would filter away
NULL-extended rows and delete an rt-less `published` run from the result
entirely, which is a semantic change. No other text column of `rt` may be
referenced, and the join condition itself remains `rt.run_key = h.run_key` —
`rt.run_id = h.run_id` (the text fact join) stays forbidden.

Because the aid can only narrow the rt side, `COUNT(rt.run_key)` is
monotonically non-increasing under it. A `parsed` run can therefore only flip
toward "not ingested" and be re-parsed, which the replay-convergent writer
heals idempotently. A `published` run's completeness verdict does not depend on
the rt side at all. The one non-fail-safe path is `MAX(rt.created_at) AS
parsed_at`: a `published` run whose rt rows the aid removed loses `parsed_at`,
and `_ingested_run_is_current` treats a NULL `parsed_at` as current, so product
mtime recompute detection is skipped for it. This widens the residual already
recorded for legacy runs from the NULL-key cohort to any run whose `run_id` and
`run_key` disagree. That population is bounded by structure (`run_id` is NOT
NULL and the primary key's first column; `hydro_run.run_id` and `run_key` are
bijective) and by the writer sourcing both from the same batch, and MUST be
measured and recorded in the delivery receipt rather than presumed zero — the
database's own `hydro.verify_river_identity_normalization()` audit does not
cover this pair.

For legacy runs without key-visible rows `parsed_at` is NULL, so recompute
detection degrades to the init-state comparison only — a recorded,
retention-bounded residual, not a silent one.

#### Scenario: published run whose fact rows are NULL-key in a compressed chunk is complete

- **GIVEN** a `published` run whose only `river_timeseries` rows carry
  `run_key IS NULL` and sit in a compressed chunk the backfill runner skipped
- **WHEN** the autopipeline evaluates `_already_ingested_runs` for it
- **THEN** the run is in the returned set, no forcing handoff is re-attempted
  for it, and the statement still carries `ON rt.run_key = h.run_key` with no
  text column of `rt` referenced other than the sanctioned `run_id` pushdown
  aid conjoined inside that same `ON` clause

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

#### Scenario: the sanctioned aid sits in the ON clause and binds the same array

- **WHEN** the ingest-completeness statement is inspected as text
- **THEN** `rt.run_id = ANY(` appears inside the `LEFT JOIN ... ON` clause and
  does not appear in the statement's `WHERE` clause, it is tagged as a
  transitional compressed-chunk pushdown aid bound to #1342, and the
  parameters passed to the statement bind the identical `run_id` sequence for
  the aid and for `WHERE h.run_id = ANY(...)`

#### Scenario: compressed chunks are no longer fully decompressed per tick

- **WHEN** the ingest-completeness statement runs on node-27 under
  `EXPLAIN (ANALYZE, BUFFERS)` with a representative bound `run_id` array
- **THEN** each compressed leg plans as `DecompressChunk` over an *index* scan
  of the corresponding `compress_hyper_*` relation with an `Index Cond` on
  `run_id`, not a `Seq Scan` over all batches, the statement completes inside
  the session `statement_timeout`, and the before/after plans and BUFFERS are
  recorded in the delivery receipt

#### Scenario: the run_id/run_key drift population is measured, not presumed

- **WHEN** the change is delivered
- **THEN** the delivery receipt records a live count of `river_timeseries`
  rows whose `run_key` resolves to a `hydro_run` row with a different
  `run_id`, together with the scope that count covers and the scope it does
  not, and any non-zero value is routed as a tracked issue before merge

#### Scenario: drift-induced loss of parsed_at does not silently skip recompute detection

- **GIVEN** a `published` run whose `river_timeseries` rows match its `run_key`
  but carry a `run_id` outside the array the statement binds, and whose product
  mtime is newer than any recorded parse time
- **WHEN** `_already_ingested_runs` runs with an `object_store_root`
- **THEN** the outcome is asserted against a real database rather than argued
  from structure: either the run is excluded from the returned set so the
  product is reparsed, or its inclusion is recorded as a known residual with
  the measured size of the drifting population — a NULL `parsed_at` produced
  by the pushdown aid MUST NOT pass silently as "current"

#### Scenario: node-27 no-op tick returns to its pre-#1442 envelope

- **WHEN** the change is deployed on node-27 and a tick runs in which no new
  run needs ingesting
- **THEN** that tick's `phase=ingest elapsed_sec` is back in the ~240 s band
  rather than the ~590 s regression band, and the tick ends `done rc=0`
