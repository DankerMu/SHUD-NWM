## ADDED Requirements

### Requirement: every node-27 service unit change MUST reach the sibling systemd.err lane set pin

`tests/test_node27_timeseries_retention.py::test_sibling_units_keep_their_systemd_err_lane` reads every `infra/systemd/nhms-node27-*.service` by glob and pins the set of units carrying a `StandardError=append:…/systemd.err` lane by set equality, so any node-27 service unit added or edited changes its input. `scripts/select_ci_tests.py` SHALL therefore carry a `PathTestRule` whose pattern is that same glob, `infra/systemd/nhms-node27-*.service`, targeting `tests/test_node27_timeseries_retention.py`, with neither `stop_on_match` nor `only_when_any_changed`, so that a diff touching any such unit — the ten units present today and any unit created later, without a per-unit rule — selects a non-empty test set containing the pin's suite instead of degrading to `--collect-only` or running only the unit's own suite. Because rule matches accumulate, the existing path-exact unit rules and their own targets SHALL be kept unchanged, the retention `.service` selection SHALL remain exactly `["tests/test_node27_timeseries_retention.py"]`, and `.timer` units SHALL NOT be covered by this rule (the pin's glob is `*.service`).

#### Scenario: each existing node-27 service unit selects the lane pin

- **WHEN** the changed paths are exactly one `infra/systemd/nhms-node27-*.service` file present in the tree, for each such file in turn
- **THEN** `select_tests` emits a non-empty set containing `tests/test_node27_timeseries_retention.py`

#### Scenario: a not-yet-existing node-27 service unit selects the lane pin

- **WHEN** the changed paths are exactly `infra/systemd/nhms-node27-brand-new.service`, a path that does not exist in the tree
- **THEN** `select_tests` emits a non-empty set containing `tests/test_node27_timeseries_retention.py`, and the sibling path `infra/systemd/nhms-node27-brand-new.timer` does not select that suite

#### Scenario: existing per-unit selections are preserved

- **WHEN** the changed paths are exactly `infra/systemd/nhms-node27-timeseries-retention.service`, or exactly `infra/systemd/nhms-node27-mvt-cache-retention.service`, or exactly `infra/systemd/nhms-node27-resource-governance.service`
- **THEN** the retention unit still selects exactly `["tests/test_node27_timeseries_retention.py"]`, the mvt-cache-retention unit still selects both its own suite and the pin's suite, and the resource-governance unit still selects `tests/test_node27_cold_governance.py` and `tests/test_node27_resource_governance.py`
