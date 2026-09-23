## ADDED Requirements

### Requirement: A populated coverage row whose run lies outside the retention window SHALL converge to its scanned facts

The display coverage refresh SHALL keep refusing to lower a populated coverage row to zero when the row's stored river valid-time end lies inside the time-series retention window, as before. When the stored end lies before the retention cutoff — the display watermark minus the configured retention window, the same anchor and window the retention runner uses — an ordinary refresh SHALL write the scanned count, including zero, and the `--all --skip-fresh` backstop SHALL select such populated rows for rescan at most once per rescan interval. When the window is not configured for the refresh or the watermark cannot be read, the refresh SHALL NOT relax the guard and SHALL select no row as expired. A populated row with no stored valid-time end SHALL never be relaxed. A read-only audit SHALL report how many populated rows have no facts, split by inside and outside the window.

#### Scenario: An expired run's frozen row converges

- **GIVEN** a published run whose coverage row has `segment_count > 0`, a stored river valid-time end older than the retention cutoff, and no remaining river facts
- **WHEN** the `--all --skip-fresh` refresh runs
- **THEN** the row is rescanned and its `segment_count` becomes 0, so the display no longer lists the run as having data

#### Scenario: An in-window populated row is still protected

- **GIVEN** a populated coverage row whose stored river valid-time end is inside the retention window and whose facts cannot be scanned
- **WHEN** an ordinary refresh runs for that run
- **THEN** the row keeps its populated count and the refusal advice is reported

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
