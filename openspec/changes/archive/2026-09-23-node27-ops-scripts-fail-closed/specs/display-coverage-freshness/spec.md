## MODIFIED Requirements

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

## ADDED Requirements

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
