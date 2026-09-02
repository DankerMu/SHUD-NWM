## ADDED Requirements

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
