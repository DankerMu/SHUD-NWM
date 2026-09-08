## ADDED Requirements

### Requirement: node-27 mvt-cache-retention unit changes MUST reach the sibling systemd.err lane set pin

`tests/test_node27_timeseries_retention.py::test_sibling_units_keep_their_systemd_err_lane` pins, by glob over `infra/systemd/nhms-node27-*.service` and set equality, exactly which node-27 units carry a `StandardError=append:…/systemd.err` lane. Because that pin is a glob reader, the `PathTestRule` for `infra/systemd/nhms-node27-mvt-cache-retention.service` in `scripts/select_ci_tests.py` SHALL list the pin's suite among its targets, so a unit-only PR diff for that unit runs the pin in targeted CI instead of surfacing on master's full run (the same gap in the `timeseries-compression` and `resource-governance` unit rules, and the units with no rule at all, is out of this requirement's scope and tracked by #2173, the same rule/reader-mismatch family as #2122). The pinned set SHALL include the seven append lanes as of #2032 (autopipe, download, frontier-alert, mvt-cache-retention, raw-retention, timeseries-compression, timeseries-compression-replay), SHALL keep `nhms-node27-resource-governance.service` as the one named journal exception, and SHALL stay set equality rather than membership.

#### Scenario: a unit-only diff selects the owning suite and the lane pin

- **WHEN** the changed paths are exactly `infra/systemd/nhms-node27-mvt-cache-retention.service`
- **THEN** `select_tests` emits both `tests/test_node27_mvt_cache_retention.py` and
  `tests/test_node27_timeseries_retention.py`, and the selection is non-empty (no `--collect-only` degradation)

#### Scenario: the lane pin is green with the #2032 unit present

- **WHEN** `uv run pytest -q tests/test_node27_timeseries_retention.py -k sibling_units` runs against a tree that
  contains `infra/systemd/nhms-node27-mvt-cache-retention.service` with its append lane
- **THEN** the set-equality assertion passes with seven members and the `resource-governance` negative assertion
  still holds
