## ADDED Requirements

### Requirement: node-27 unit files with a content-asserting owner suite MUST select that suite

The sibling-lane pin selected by the `infra/systemd/nhms-node27-*.service` glob rule asserts only the `StandardError=append:…/systemd.err` lane set; it does not read a unit's `ExecStart`, `ExecStartPre`, `Environment`, `EnvironmentFile`, or timeout directives. For every node-27 unit file that has a suite reading it by path and asserting such directives, `scripts/select_ci_tests.py` SHALL carry a path-exact `PathTestRule` (neither `stop_on_match` nor `only_when_any_changed`) targeting that owner suite, unless that owner suite is already selected by the glob rule: `nhms-node27-autopipe.service` → `tests/test_node27_autopipeline_preflight.py`; `nhms-node27-download.service` → `tests/test_node27_download_cycles.py`; `nhms-node27-frontier-alert.service` → `tests/test_node27_frontier_stall_alert.py`; `nhms-node27-raw-retention.service` → `tests/test_node27_raw_retention.py`; `nhms-node27-timeseries-compression-replay.service` → both `tests/test_node27_timeseries_compression.py` and `tests/test_node27_timeseries_compression_supervisor.py`; `nhms-node27-download.timer` → `tests/test_node27_download_cycles.py`; `nhms-node27-timeseries-compression.timer` → both `tests/test_node27_cold_residency.py` and `tests/test_node27_timeseries_compression.py`. Because rule matches accumulate, each `.service` selection SHALL contain its owner suites and the glob rule's pin suite, each of the two `.timer` selections above SHALL contain its owner suites and SHALL NOT contain the pin suite, and the existing selections for `nhms-node27-timeseries-retention.service` (exactly the pin suite), `nhms-node27-timeseries-retention.timer`, `nhms-node27-autopipe.timer`, and `nhms-node27-mvt-cache-retention.{service,timer}` SHALL remain unchanged. Units whose only path reader is the pin suite itself, which the glob rule already selects (`nhms-node27-unit-failure-alert@.service`, asserted at `tests/test_node27_timeseries_retention.py:3589-3592`) or that have no content-asserting reader at all (`nhms-node27-frontier-alert.timer`, `nhms-node27-raw-retention.timer`) SHALL NOT receive a rule under this requirement.

#### Scenario: a unit-only diff selects the owner suite and the lane pin

- **WHEN** the changed paths are exactly one of `infra/systemd/nhms-node27-{autopipe,download,frontier-alert,raw-retention,timeseries-compression-replay}.service`
- **THEN** `select_tests` emits a set containing every owner suite listed above for that unit and `tests/test_node27_timeseries_retention.py`

#### Scenario: a timer-only diff selects its reader suites without the lane pin

- **WHEN** the changed paths are exactly `infra/systemd/nhms-node27-download.timer` or exactly `infra/systemd/nhms-node27-timeseries-compression.timer`
- **THEN** `select_tests` emits a non-empty set containing every owner suite listed above for that timer and not containing `tests/test_node27_timeseries_retention.py`

#### Scenario: existing exact selections are preserved

- **WHEN** the changed paths are exactly `infra/systemd/nhms-node27-timeseries-retention.service`, or exactly `infra/systemd/nhms-node27-timeseries-retention.timer`, or exactly `infra/systemd/nhms-node27-autopipe.timer`
- **THEN** the retention service still selects exactly `["tests/test_node27_timeseries_retention.py"]`, the retention timer still selects exactly `["tests/test_node27_cold_residency.py", "tests/test_node27_timeseries_retention.py"]`, and the autopipe timer still selects exactly `["tests/test_node27_autopipeline_preflight.py", "tests/test_node27_mvt_prewarm.py"]`
