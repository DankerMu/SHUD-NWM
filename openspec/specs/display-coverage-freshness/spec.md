# display-coverage-freshness Specification

## Purpose
TBD - created by archiving change display-coverage-residual-debt. Update Purpose after archive.
## Requirements
### Requirement: Publish is a status-only transition for coverage freshness

Publishing a parsed run for display SHALL NOT advance the run's `updated_at`, so that coverage refreshed during ingest remains fresh across the publish transition; `updated_at` advances only on data mutations (registration, parse completion).

#### Scenario: No false staleness after publish

- **WHEN** a run whose display coverage was refreshed during ingest is subsequently published
- **THEN** the coverage staleness predicate (`refreshed_at < updated_at`) does not mark that run stale
- **AND** the coverage backstop reports zero stale runs attributable to the publish transition alone.

#### Scenario: Display cache revision still rotates on publish

- **WHEN** a run transitions from `parsed` to `published`
- **THEN** the MVT tile revision digest changes because its basis includes the run `status`
- **AND** removing the publish-time `updated_at` bump does not suppress tile cache rotation.

### Requirement: Autopipe tick phases expose per-phase durations

The autopipe cron tick SHALL log a distinguishable elapsed-time line for each of its phases (ingest, coverage backstop, MVT prewarm), in addition to the whole-tick markers.

#### Scenario: Phase durations in cron log

- **WHEN** one autopipe tick completes
- **THEN** the cron log contains one elapsed-seconds line per executed phase, each naming its phase
- **AND** the whole-tick START/END markers remain present.

### Requirement: Coverage refresh MUST refuse to zero a populated row unless forced

The coverage upsert SHALL NOT update an existing `hydro.run_display_coverage` row with `segment_count > 0` when the fresh scan yields zero segments unless `force` is set, on every refresh path (single run, batch, all runs); the skip is whole-row (station-side columns and `refreshed_at` are kept as well, accepted for the finished pre-cutover cohort). A refusal SHALL be identified only from the upsert having run and skipped the row, never from a run that was not a candidate. In single-run mode the caller SHALL raise `DisplayCoverageRefreshRefused` carrying the run id, the existing segment count and an advice string, leaving the connection rolled back; the CLI SHALL report that refusal with exit code 3 and one structured stderr line rather than a traceback. Passing `force=True` (CLI `--force`) SHALL perform the zeroing. A run with no existing row, or an existing row with `segment_count = 0`, SHALL be written as before.

#### Scenario: Legacy run is protected

- **WHEN** a run whose river rows carry NULL surrogate keys has coverage `segment_count = 12` and is refreshed without force
- **THEN** the call raises `DisplayCoverageRefreshRefused(run_id, 12, …)` and the row still reads 12
- **AND** `scripts/node27_refresh_coverage.py --run-id <that run>` exits 3 with a `DISPLAY_COVERAGE_REFRESH_REFUSED` line on stderr

#### Scenario: Explicit force zeroes

- **WHEN** the same run is refreshed with `force=True`
- **THEN** the row is updated to `segment_count = 0`

#### Scenario: All-runs form skips populated legacy rows without classifying them

- **WHEN** the all-runs refresh statement runs over a set containing a populated legacy run
- **THEN** that row is left unchanged, the other rows are refreshed, and the outcome reports no `refused` entries (the all-runs form protects but does not classify)

#### Scenario: Non-candidate run with an old populated row is not a refusal

- **WHEN** a run no longer matches the candidate query but still owns a coverage row with `segment_count > 0`
- **THEN** the refresh returns no row (single-run `False`, batch `skipped`), raises nothing, and leaves the row unchanged

#### Scenario: First refresh is not hurt

- **WHEN** a new run with no river rows and no coverage row is refreshed
- **THEN** a row with `segment_count = 0` is written

### Requirement: Batch refresh MUST isolate per-run failures and refusals on both worker paths

`refresh_all_run_display_coverage` SHALL record a run whose refresh raises as `failed` and a run refused by the guard as `refused`, continue with the remaining runs, and return `{"refreshed", "skipped", "failed", "refused"}` counts, identically for `workers == 1` and `workers > 1`.

#### Scenario: One of three runs fails

- **WHEN** three runs are refreshed and the injected connect raises for the second one, with `workers` 1 and then 2
- **THEN** the result is `{"refreshed": 2, "skipped": 0, "failed": 1, "refused": 0}` on both paths
- **AND** the two survivors' connections were committed and closed and the failing one saw no commit

#### Scenario: A legacy run in the batch is refused, not zeroed

- **WHEN** the batch contains one legacy populated run
- **THEN** it is counted under `refused`, its row is unchanged, and the other runs are refreshed

### Requirement: A coverage freshness observer MUST alert when the national catalog's frontier falls behind ingest

node-27 SHALL run a read-only coverage freshness check that, per source key
`COALESCE(lower(source_id), '__null_source__')`, compares the **display-ready frontier** —
`max(cycle_time)` over `hydro.hydro_run` rows with `status IN ('succeeded','parsed','published')`
and `cycle_time IS NOT NULL`, joined to a `core.model_instance` row with `active_flag` and a
non-NULL `river_network_version_id` — against the **covered frontier**, which SHALL be obtained
from the display module's own catalog owner (`services/tiles/mvt.py` `national_discharge_cycles`,
field `default_cycle`) rather than re-derived, so the observer cannot drift from the surface it
watches. A source SHALL be evaluated only when its display-ready frontier is not older than
`NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`; the `__null_source__` key SHALL never be evaluated.
The check SHALL exit non-zero and name the offending sources when an evaluated source's gap
exceeds the threshold or it has no covered cycle at all, and its systemd unit SHALL route that
non-zero exit to the node-27 unit-failure mail handler with the verdict inside the journal tail
that handler mails. The threshold SHALL be derived from `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`
rather than a hard-coded day count, and SHALL be rejected as a configuration error when it is
not a finite number strictly greater than zero and strictly smaller than that constant. The
check SHALL hold no persistent state.

#### Scenario: Coverage stall is detected before the layer goes dark

- **WHEN** a source's display-ready frontier keeps advancing while its covered frontier stays
  behind by more than the threshold
- **THEN** the check exits non-zero, names that source with its gap in days, and the unit's
  `OnFailure=` handler mails the operator

#### Scenario: Outcome-based, not return-code-based

- **WHEN** every coverage refresh returned rc=0 but produced no advancing coverage row (the
  `no_coverage_row` path)
- **THEN** the check still exits non-zero, because its criterion reads only the two frontiers
  and never a refresh return code

#### Scenario: Populated but unlistable coverage rows are not counted as covered

- **WHEN** a cycle's coverage rows exist with `segment_count > 0` but the catalog refuses to
  list that cycle — incomplete active-network coverage, inconsistent coverage window columns,
  or no valid time on the published stride
- **THEN** the covered frontier used by the check is the catalog's own newest listed cycle, so
  the check reports the gap the layer will actually show

#### Scenario: Full ingest stall does not double-alert

- **WHEN** both frontiers stop advancing together because ingest has stalled
- **THEN** the gap does not grow, the check exits zero, and the condition remains owned by the
  `frontier-stalled` lane

#### Scenario: A source outside the display window is not evaluated

- **WHEN** a source's display-ready frontier is older than `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`,
  or the source key is `__null_source__`
- **THEN** that source is reported as not evaluated and cannot by itself make the check exit
  non-zero

#### Scenario: Nothing observable is fail-closed

- **WHEN** the display-ready frontier query returns no source key at all, so the check cannot see
  the run set the catalog is built from
- **THEN** the check exits non-zero rather than reporting health, because the frontier-stall lane
  does not observe river-network activity and would stay silent through the same condition

#### Scenario: Threshold at or beyond the display window is refused

- **WHEN** the configured threshold is non-finite, zero, negative, or at least
  `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`
- **THEN** the check exits with a configuration error before observing anything, rather than
  clamping the value

#### Scenario: Observation failure is fail-closed and redacted

- **WHEN** the observation cannot run (database unreachable, statement timeout, or permission
  denied)
- **THEN** the check exits non-zero so the failure is mailed, and the reported error contains no
  DSN password

#### Scenario: The verdict survives the mailed journal tail

- **WHEN** more sources exist than the report may print
- **THEN** the report is truncated with an explicit omission line, breaching sources are kept,
  and the verdict block is printed last so it remains inside the journal tail the failure
  handler mails

