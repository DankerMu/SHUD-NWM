# Proposed survivor delta

Target contract only; canonical implementation is unchanged by this revision.

## MODIFIED Requirements

### Requirement: every node-27 service unit change MUST reach the sibling systemd.err lane set pin

The test selector SHALL preserve the surviving sibling service-unit lane pin.

`tests/test_node27_timeseries_retention.py::test_sibling_units_keep_their_systemd_err_lane`
reads every `infra/systemd/nhms-node27-*.service` by glob and pins the set of
units carrying a `StandardError=append:…/systemd.err` lane by set equality, so
any node-27 service unit added or edited changes its input.
`scripts/select_ci_tests.py` SHALL therefore carry a `PathTestRule` whose
pattern is that same glob, `infra/systemd/nhms-node27-*.service`, targeting
`tests/test_node27_timeseries_retention.py`, with neither `stop_on_match` nor
`only_when_any_changed`, so that a diff touching any such unit selects a
non-empty test set containing the pin's suite instead of degrading to
`--collect-only` or running only the unit's own suite. Because rule matches
accumulate, existing path-exact unit rules and their surviving targets SHALL be
preserved, with cold-only targets removed after retained behavior has migrated
to its actual owner's suites. The retention `.service` selection SHALL remain
exactly `["tests/test_node27_timeseries_retention.py"]`, and `.timer` units
SHALL NOT be covered by this rule (the pin's glob is `*.service`).

#### Scenario: each existing node-27 service unit selects the lane pin

- **WHEN** the changed paths are exactly one
  `infra/systemd/nhms-node27-*.service` file present in the tree, for each such
  file in turn
- **THEN** `select_tests` emits a non-empty set containing
  `tests/test_node27_timeseries_retention.py`

#### Scenario: a not-yet-existing node-27 service unit selects the lane pin

- **WHEN** the changed paths are exactly
  `infra/systemd/nhms-node27-brand-new.service`, a path that does not exist in
  the tree
- **THEN** `select_tests` emits a non-empty set containing
  `tests/test_node27_timeseries_retention.py`, and the sibling path
  `infra/systemd/nhms-node27-brand-new.timer` does not select that suite

#### Scenario: existing per-unit selections are preserved

- **WHEN** the changed paths are exactly
  `infra/systemd/nhms-node27-timeseries-retention.service`, or exactly
  `infra/systemd/nhms-node27-mvt-cache-retention.service`, or exactly
  `infra/systemd/nhms-node27-resource-governance.service`
- **THEN** the retention unit still selects exactly
  `["tests/test_node27_timeseries_retention.py"]`, the mvt-cache-retention unit
  still selects both its own suite and the pin's suite, and the
  resource-governance unit selects `tests/test_node27_resource_governance.py`,
  its extracted sampling behavior's surviving suites and the lane pin, never
  deleted `tests/test_node27_cold_governance.py`

### Requirement: node-27 unit files with a content-asserting owner suite MUST select that suite

Unit-specific test selection SHALL preserve the surviving owner coverage below.

The sibling-lane pin selected by the `infra/systemd/nhms-node27-*.service` glob
rule asserts only the `StandardError=append:…/systemd.err` lane set; it does not
read a unit's `ExecStart`, `ExecStartPre`, `Environment`, `EnvironmentFile`, or
timeout directives. For every node-27 unit file that has a suite reading it by
path and asserting such directives, `scripts/select_ci_tests.py` SHALL carry a
path-exact `PathTestRule` (neither `stop_on_match` nor `only_when_any_changed`)
targeting that owner suite, unless that owner suite is already selected by the
glob rule: `nhms-node27-autopipe.service` →
`tests/test_node27_autopipeline_preflight.py`; `nhms-node27-download.service` →
`tests/test_node27_download_cycles.py`; `nhms-node27-frontier-alert.service` →
`tests/test_node27_frontier_stall_alert.py`; `nhms-node27-raw-retention.service`
→ `tests/test_node27_raw_retention.py`;
`nhms-node27-timeseries-compression-replay.service` → both
`tests/test_node27_timeseries_compression.py` and
`tests/test_node27_timeseries_compression_supervisor.py`;
`nhms-node27-download.timer` → `tests/test_node27_download_cycles.py`;
`nhms-node27-timeseries-compression.timer` →
`tests/test_node27_timeseries_compression.py` and any surviving owner suite
protecting its compression-only budget/scheduling contract, not the deleted
cold-residency suite. Because rule matches accumulate, each `.service` selection
SHALL contain its owner suites and the glob rule's pin suite, each of the two
`.timer` selections above SHALL contain its owner suites and SHALL NOT contain
the pin suite. Existing selections for
`nhms-node27-timeseries-retention.service` (exactly the pin suite),
`nhms-node27-autopipe.timer`, and
`nhms-node27-mvt-cache-retention.{service,timer}` SHALL remain unchanged; the
retention timer SHALL select its surviving retention suite and any actual
retained reader suites, not a deleted cold suite. Units whose only path reader
is the pin suite itself, which the glob rule already selects
(`nhms-node27-unit-failure-alert@.service`) or that have no content-asserting
reader at all (`nhms-node27-frontier-alert.timer`,
`nhms-node27-raw-retention.timer`) SHALL NOT receive a rule under this
requirement.

#### Scenario: a unit-only diff selects the owner suite and the lane pin

- **WHEN** the changed paths are exactly one of
  `infra/systemd/nhms-node27-{autopipe,download,frontier-alert,raw-retention,timeseries-compression-replay}.service`
- **THEN** `select_tests` emits a set containing every owner suite listed above
  for that unit and `tests/test_node27_timeseries_retention.py`

#### Scenario: a timer-only diff selects its reader suites without the lane pin

- **WHEN** the changed paths are exactly
  `infra/systemd/nhms-node27-download.timer` or exactly
  `infra/systemd/nhms-node27-timeseries-compression.timer`
- **THEN** `select_tests` emits a non-empty set containing every owner suite
  listed above for that timer and not containing
  `tests/test_node27_timeseries_retention.py`

#### Scenario: existing exact selections are preserved

- **WHEN** the changed paths are exactly
  `infra/systemd/nhms-node27-timeseries-retention.service`, or exactly
  `infra/systemd/nhms-node27-timeseries-retention.timer`, or exactly
  `infra/systemd/nhms-node27-autopipe.timer`
- **THEN** the retention service still selects exactly
  `["tests/test_node27_timeseries_retention.py"]`, the retention timer selects
  `tests/test_node27_timeseries_retention.py` and any actual retained reader
  suites without `tests/test_node27_cold_residency.py`, and the autopipe timer
  still selects exactly
  `["tests/test_node27_autopipeline_preflight.py", "tests/test_node27_mvt_prewarm.py"]`
