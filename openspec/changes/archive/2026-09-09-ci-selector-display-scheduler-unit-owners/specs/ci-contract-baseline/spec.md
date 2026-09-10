## ADDED Requirements

### Requirement: display and scheduler unit files with a content-asserting owner suite MUST select that suite

`infra/systemd/nhms-display-api.service`, `infra/systemd/nhms-scheduler-file-provider-refresh.service` and `infra/systemd/nhms-scheduler-file-provider-refresh.timer` each have exactly one suite that `read_text`s that path and asserts its directives, and none of them lies inside the `infra/systemd/nhms-node27-*.service` glob rule, so before this change a unit-only diff selected nothing at all and the targeted job degraded to a zero-assertion `--collect-only` smoke. `scripts/select_ci_tests.py` SHALL carry a path-exact `PathTestRule` (neither `stop_on_match` nor `only_when_any_changed`) for each: the display-api unit targeting `tests/test_hydro_display_mvt_scaling.py`, and both scheduler file-provider-refresh units targeting `tests/test_scheduler_file_provider_refresh.py`. Because none of the three matches the node-27 glob, none of the three selections SHALL contain `tests/test_node27_timeseries_retention.py`. The node-27 owner-table meta test SHALL decide whether a unit owes the sibling lane pin by matching that glob rather than by the `.service` suffix, so that a node-27 unit named outside the `nhms-node27-` prefix is judged correctly. `nhms-display-api.service` is not an `nhms-node27-*`-named unit and therefore lies outside the domain of "node-27 unit files with a content-asserting owner suite MUST select that suite", whose scope is the `infra/systemd/nhms-node27-*.service` glob; it is governed by this requirement instead. Units with no content-asserting reader (`nhms-compute-compose.service`, `nhms-display-compose.service`, `nhms-node27-frontier-alert.timer`, `nhms-node27-raw-retention.timer`, `nhms-scheduler-evidence-retention.timer`) SHALL NOT receive a rule under this requirement, and the four node-22 units already routed by the existing exact rules SHALL keep their current selections unchanged.

#### Scenario: a display-api unit diff selects its owner suite without the node-27 lane pin

- **WHEN** the changed paths are exactly `infra/systemd/nhms-display-api.service`
- **THEN** `select_tests` emits a non-empty set containing `tests/test_hydro_display_mvt_scaling.py` and not containing `tests/test_node27_timeseries_retention.py`

#### Scenario: a scheduler file-provider-refresh unit diff selects its owner suite

- **WHEN** the changed paths are exactly `infra/systemd/nhms-scheduler-file-provider-refresh.service` or exactly `infra/systemd/nhms-scheduler-file-provider-refresh.timer`
- **THEN** `select_tests` emits a non-empty set containing `tests/test_scheduler_file_provider_refresh.py` and not containing `tests/test_node27_timeseries_retention.py`

#### Scenario: existing node-27 owner-table selections are preserved

- **WHEN** the changed paths are exactly one of the five `infra/systemd/nhms-node27-{autopipe,download,frontier-alert,raw-retention,timeseries-compression-replay}.service` units
- **THEN** `select_tests` still emits that unit's owner suites together with `tests/test_node27_timeseries_retention.py`, and the two node-27 `.timer` rows still emit their owner suites without it
