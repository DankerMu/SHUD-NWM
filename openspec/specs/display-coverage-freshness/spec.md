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

The coverage upsert SHALL NOT update an existing `hydro.run_display_coverage` row with `segment_count > 0` when the fresh scan yields zero segments, unless `force` is set or the row lies outside the time-series retention window, on every refresh path (single run, batch, all runs); the skip is whole-row (station-side columns and `refreshed_at` are kept as well). A row lies outside the window only when its stored river valid-time end is earlier than the retention cutoff: the display watermark minus the configured retention window, the same anchor and window the retention runner uses, never wall time. A row with no stored river valid-time end is never outside the window. When the retention window is not configured for the refresh, or the display watermark cannot be read, no row is outside the window and the guard holds for every row (fail-closed). Outside the window an ordinary refresh SHALL write the scanned count, zero included, because after the legacy store's contract every cohort's expired facts are equally gone and a populated row would only advertise an empty curve; a row whose facts still exist keeps its scanned count. The `--all --skip-fresh` backstop SHALL additionally select a populated row lying outside the window whose coverage was not refreshed within the expired-rescan interval (default 24 hours), so such a row is rescanned at most once per interval. A refusal SHALL be identified only from the upsert having run and skipped the row, never from a run that was not a candidate. In single-run mode the caller SHALL raise `DisplayCoverageRefreshRefused` carrying the run id, the existing segment count and an advice string, leaving the connection rolled back; the CLI SHALL report that refusal with exit code 3 and one structured stderr line rather than a traceback. Passing `force=True` (CLI `--force`) SHALL perform the zeroing. A run with no existing row, or an existing row with `segment_count = 0`, SHALL be written as before.

#### Scenario: Legacy run is protected

- **WHEN** a run whose river rows carry NULL surrogate keys has coverage `segment_count = 12`, a stored river valid-time end inside the retention window (or no cutoff available to the refresh), and is refreshed without force
- **THEN** the call raises `DisplayCoverageRefreshRefused(run_id, 12, …)` and the row still reads 12
- **AND** `scripts/node27_refresh_coverage.py --run-id <that run>` exits 3 with a `DISPLAY_COVERAGE_REFRESH_REFUSED` line on stderr

#### Scenario: Explicit force zeroes

- **WHEN** the same run is refreshed with `force=True`
- **THEN** the row is updated to `segment_count = 0`

#### Scenario: All-runs form skips populated legacy rows without classifying them

- **WHEN** the all-runs refresh statement runs over a set containing a populated legacy run whose stored end lies inside the retention window (or with no cutoff available)
- **THEN** that row is left unchanged, the other rows are refreshed, and the outcome reports no `refused` entries (the all-runs form protects but does not classify)

#### Scenario: Non-candidate run with an old populated row is not a refusal

- **WHEN** a run no longer matches the candidate query but still owns a coverage row with `segment_count > 0`
- **THEN** the refresh returns no row (single-run `False`, batch `skipped`), raises nothing, and leaves the row unchanged

#### Scenario: First refresh is not hurt

- **WHEN** a new run with no river rows and no coverage row is refreshed
- **THEN** a row with `segment_count = 0` is written

#### Scenario: An expired run's frozen row converges

- **GIVEN** a published run whose coverage row has `segment_count > 0`, a stored river valid-time end older than the retention cutoff, and no remaining river facts
- **WHEN** the `--all --skip-fresh` refresh runs with the retention window configured and the display watermark readable
- **THEN** the row is rescanned and its `segment_count` becomes 0, so the display no longer lists the run as having data

#### Scenario: An expired row whose facts survive keeps its count and is not reselected within the interval

- **GIVEN** a populated coverage row whose stored end is older than the retention cutoff but whose river facts have not been dropped yet
- **WHEN** the `--all --skip-fresh` refresh rescans it and the backstop runs again before the expired-rescan interval has elapsed
- **THEN** the row keeps its scanned count and the second run does not select it

#### Scenario: Without a window or a watermark the guard holds everywhere

- **GIVEN** a populated coverage row whose stored end is older than the retention cutoff and whose facts are gone
- **WHEN** the refresh runs without the retention window in its environment, or with the display watermark unreadable
- **THEN** no row is treated as outside the window: the single-run refresh is refused and the backstop selects no row as expired

#### Scenario: A row with no stored end is never relaxed

- **GIVEN** a populated coverage row with no stored river valid-time end and no remaining facts
- **WHEN** an ordinary refresh runs with a cutoff available
- **THEN** the refresh is refused and the row keeps its count

### Requirement: Batch refresh MUST isolate per-run failures and refusals on both worker paths

`refresh_all_run_display_coverage` SHALL record a run whose refresh raises as `failed` and a run refused by the guard as `refused`, continue with the remaining runs, and return `{"refreshed", "skipped", "failed", "refused"}` counts, identically for `workers == 1` and `workers > 1`.

#### Scenario: One of three runs fails

- **WHEN** three runs are refreshed and the injected connect raises for the second one, with `workers` 1 and then 2
- **THEN** the result is `{"refreshed": 2, "skipped": 0, "failed": 1, "refused": 0}` on both paths
- **AND** the two survivors' connections were committed and closed and the failing one saw no commit

#### Scenario: A legacy run in the batch is refused, not zeroed

- **WHEN** the batch contains one legacy populated run whose stored end lies inside the retention window (or no cutoff is available to the batch)
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
- **THEN** the report is truncated with an explicit omission line that states exactly how many
  rows it dropped, and the verdict block is printed last and is never itself truncated, so it
  remains inside the journal tail the failure handler mails
- **AND** breaching sources are ordered ahead of the others, so the rows dropped first are the
  non-breaching ones; the guarantee is bounded by the capacity of the truncated table (one of
  its rows goes to the omission line), and once breaching sources alone exceed it, breaching
  rows are dropped from the table as well
- **AND** no truncation is silent about what it dropped: the report header states the true
  count of breaching sources, and the verdict names a bounded number of them followed by the
  count of the rest when any remain

### Requirement: Coverage freshness failure stages SHALL be attributable by exit code

The coverage freshness check SHALL distinguish its two failure stages by exit code, and the operator documentation SHALL route each stage to a remediation that can actually close it. A failure to import the display module happens in the configuration stage, before any database work, and SHALL therefore be reported as a configuration failure; a display-module failure raised during observation SHALL be reported as an observation failure, alongside database unreachability, statement timeout and permission denial. Live operator documentation SHALL NOT describe "display-module error" without naming the stage it belongs to. The operator documentation SHALL name every structured failure code the check can emit and give each a first step that can close it; where one code covers several causes, it SHALL name the discriminator the mailed failure line already carries — the exception class that prefixes the reason, and the driver error class that follows it — rather than routing every cause of that code to a single remediation. An archived change record is exempt: it is the record of what was decided, and it is corrected by an appended dated note rather than by rewriting the original row.

#### Scenario: Import-time display failure is a configuration failure

- **WHEN** the display module cannot be imported (for example the unit's `PYTHONPATH` is missing, or the virtualenv lacks the display stack)
- **THEN** the check exits with the configuration exit code before attempting any observation, emits its structured configuration error line, and the runbook's row for that exit code names this case with a first step that addresses the interpreter path rather than the threshold knob

#### Scenario: Every structured failure code has a documented destination

- **WHEN** the check's structured failure codes are compared against the lane's operator documentation
- **THEN** every code the check can emit is named there with a first step, and a regression test fails if a code is added to the check without being named there

#### Scenario: One observation-failure code, several causes, routed by what the mail carries

- **WHEN** an observation failure is mailed because the database is unreachable, a statement timed out, permission was denied, the configured database lacks the lane's relations, or the display module raised during observation
- **THEN** the documentation routes each of those causes to its own first step using the reason's exception-class prefix and the driver class that follows it, and a cause that matches none of them has an explicit fallback rather than landing on another cause's remediation

### Requirement: Every node-27 coverage freshness unit SHALL be registered in the governance liveness inventory

The coverage freshness service and timer SHALL appear in the node-27 resource-governance audit's default service inventory, and the audit SHALL collect, for every unit in that inventory, the unit's load state and unit-file state, so that a disabled or masked timer is distinguishable in the governance receipt from one that was never installed, rather than being visible only in the alerting lane's own absence of mail. Registration makes that state visible for periodic human reading; it is not an alert, and the documentation SHALL NOT describe it as one. The installation documentation for the lane SHALL state that registration as part of its closure, as the sibling frontier-stall lane's documentation does.

#### Scenario: A disabled timer is visible to the governance oracle

- **WHEN** the governance audit collects systemd state over its default service inventory
- **THEN** the receipt carries an entry for both the coverage freshness service and its timer, including each unit's load state and unit-file state, so a timer that was installed and later disabled reads differently there from one that was never installed

#### Scenario: Registration is visibility, not an alert

- **WHEN** a registered timer is disabled
- **THEN** the governance audit's exit code and recommendations are unchanged, and the lane's documentation states that a disabled timer is found by reading the receipt, not by an alert

### Requirement: A read-only audit SHALL report populated coverage rows whose facts are gone

The coverage refresh CLI SHALL offer a read-only audit that counts populated coverage rows whose river facts no longer exist, split into rows inside the retention window, rows outside it (on the same cutoff the refresh uses), and rows with no stored river valid-time end, together with the watermark and cutoff it used. Each probe SHALL be bounded to the row's stored valid-time range and run as its own short statement with a statement timeout and a lock timeout, so the audit holds no lock across chunks that could block retention; a probe that times out SHALL be counted as failed and the audit SHALL continue. The audit SHALL write nothing, and SHALL exit with a typed failure when the window is not configured or the watermark cannot be read.

#### Scenario: The audit buckets rows on the refresh's cutoff and writes nothing

- **GIVEN** a seeded catalog with populated rows inside the window, outside it with and without facts, and with no stored end
- **WHEN** the audit runs with the retention window configured
- **THEN** it prints the in-window, out-of-window and no-end counts with the watermark and cutoff, and no coverage row changes

### Requirement: The coverage-freshness alert SHALL evaluate only sources the display surface serves

The coverage-freshness alert lane SHALL evaluate a discovered source key only when it belongs to the display surface's source set, taken from the same definition the display routes use for their `source` parameter. Any other key SHALL be reported with status `not-evaluated` and reason `unsupported-source` and SHALL NOT contribute to the exit code.

#### Scenario: A non-display source does not raise the alert

- **GIVEN** observations where `gfs` is fully covered and `era5` has a ready frontier but no covered cycle
- **WHEN** the alert lane runs
- **THEN** it exits 0 and reports the `era5` row as `not-evaluated` with reason `unsupported-source`

### Requirement: The autopipe cron wrapper SHALL log the real exit code of a non-fatal step

When a non-fatal step of the autopipe cron wrapper fails, the logged line SHALL carry that step's own exit status, captured before any other command or expansion runs.

#### Scenario: A failing prewarm is logged with its code

- **GIVEN** the MVT prewarm step exits with status 3
- **WHEN** the wrapper logs the non-fatal failure
- **THEN** the log line reads `rc=3`
