## ADDED Requirements

### Requirement: review-governance tools and records SHALL select their suites

`scripts/select_ci_tests.py` SHALL carry path-exact rules, with neither `stop_on_match` nor `only_when_any_changed`:

- `scripts/governance/loop_log_audit.py` and `docs/review-loop-log.jsonl` → `tests/test_loop_log_audit_attribution.py`;
- `scripts/review_gate.py` → `tests/test_review_gate_cli.py` and `tests/test_review_gate_issue_memory.py`.

The CI `backend` paths filter SHALL list `docs/review-loop-log.jsonl` as an exact literal, so a log-only diff opens the targeted gate.

#### Scenario: a loop-log diff selects the attribution suite

- **WHEN** the changed paths are exactly `docs/review-loop-log.jsonl`
- **THEN** the CI `backend` filter matches, and the selection contains `tests/test_loop_log_audit_attribution.py`

#### Scenario: a review-gate CLI diff selects both memory suites

- **WHEN** the changed paths are exactly `scripts/review_gate.py`
- **THEN** the selection contains `tests/test_review_gate_cli.py` and `tests/test_review_gate_issue_memory.py`

## MODIFIED Requirements

### Requirement: Entropy hard gate MUST be green on master without weakening the gate

The repository SHALL keep the entropy hard gate
(`build_report(mode="hard-gate")`)
reporting `hard_gate_status == "pass"` on master, and restoring it after
a diagnostic-token finding SHALL change the flagged production-adjacent
text rather than adding checker exemptions, unless the checker itself is
provably wrong.

#### Scenario: diagnostic-token comment reworded

- **WHEN** the entropy hard gate flags a prose comment in
  `workers/shud_runtime/runtime.py` for naming a QHH diagnostic token
- **THEN** the comment is reworded to drop the literal token with zero
  executable-logic change, and
  `tests/test_entropy_audit_report_contract.py::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings` passes with
  `hard_gate_failing_count == 0`

### Requirement: Shell wrapper changes MUST gate their guard suites

The CI change-detection gate SHALL treat tracked `scripts/**/*.sh` files as
backend surface: the `backend` paths-filter matches them, and the targeted
test selector maps each shell wrapper that has committed guard tests to those
guard test files. A `scripts/**/*.sh` path with no explicit mapping falls back
to the core smoke selection instead of an empty selection.

#### Scenario: sh-only change selects the wrapper's guard suite

WHEN a pull request changes only `scripts/scheduler_file_provider_refresh_once.sh`
THEN the `backend` filter reports true
AND the targeted selector output includes `tests/test_scheduler_refresh_deployment_contract.py`

#### Scenario: unmapped shell script falls back loudly, not empty

WHEN a pull request changes only a new `scripts/**/*.sh` file that has no
selector mapping
THEN the targeted selector returns at least the core smoke test set
AND does not return an empty selection

#### Scenario: sh plus py change selects the union of guards

WHEN a pull request changes both a mapped shell wrapper and a mapped backend
python module
THEN the targeted selector output contains both surfaces' guard suites

#### Scenario: a mapped shell wrapper does not pull core smoke

WHEN a pull request changes only a shell wrapper that has an explicit guard
mapping
THEN the selection contains its guard suite and no core-smoke fallback entries

### Requirement: Calibration declaration changes MUST execute their consumer contract suites

A pull request that changes `config/calibration_overrides.yaml` MUST open the backend targeted-test gate and the targeted selector SHALL select `tests/test_publish_registry_calibration_overrides.py`, `tests/test_basins_package.py`, and `tests/test_select_ci_tests.py`. The route MUST be exact to that declaration path and SHALL NOT substitute core-smoke or collect-only execution for these assertion-level consumers.

#### Scenario: Declaration-only change reaches publication and package assertions

- **WHEN** the changed-file set contains only `config/calibration_overrides.yaml`
- **THEN** the CI `backend` paths filter matches the change
- **AND** targeted selection contains the publisher, package-manifest, and selector contract suites
- **AND** targeted selection does not fall back to the unrelated core-smoke set or zero-assertion collection

#### Scenario: Backend-filter leg cannot disappear silently

- **WHEN** the declaration path is deleted from the `backend` filter or moved under a different filter
- **THEN** `tests/test_select_ci_tests.py` fails a block-scoped assertion naming the missing backend-gate leg

#### Scenario: Selector leg cannot disappear silently

- **WHEN** the exact selector rule for the declaration is deleted or loses either consumer suite
- **THEN** `tests/test_select_ci_tests.py` fails by naming the missing assertion-level target

### Requirement: display and scheduler unit files with a content-asserting owner suite MUST select that suite

`infra/systemd/nhms-display-api.service`, `infra/systemd/nhms-scheduler-file-provider-refresh.service` and `infra/systemd/nhms-scheduler-file-provider-refresh.timer` each have at least one suite that `read_text`s that path and asserts its directives, and none of them lies inside the `infra/systemd/nhms-node27-*.service` glob rule, so before this change a unit-only diff selected nothing at all and the targeted job degraded to a zero-assertion `--collect-only` smoke. `scripts/select_ci_tests.py` SHALL carry a path-exact `PathTestRule` (neither `stop_on_match` nor `only_when_any_changed`) for each: the display-api unit targeting `tests/test_hydro_display_mvt_scaling.py`, and both scheduler file-provider-refresh units targeting `tests/test_scheduler_refresh_deployment_contract.py`; the `.service` unit also selects `tests/test_node22_refresh_timer_health.py`, which reads it too. Because none of the three matches the node-27 glob, none of the three selections SHALL contain `tests/test_node27_timeseries_retention.py`. The node-27 owner-table meta test SHALL decide whether a unit owes the sibling lane pin by matching that glob rather than by the `.service` suffix, so that a node-27 unit named outside the `nhms-node27-` prefix is judged correctly. `nhms-display-api.service` is not an `nhms-node27-*`-named unit and therefore lies outside the domain of "node-27 unit files with a content-asserting owner suite MUST select that suite", whose scope is the `infra/systemd/nhms-node27-*.service` glob; it is governed by this requirement instead. Units with no content-asserting reader (`nhms-compute-compose.service`, `nhms-display-compose.service`, `nhms-node27-frontier-alert.timer`, `nhms-node27-raw-retention.timer`, `nhms-scheduler-evidence-retention.timer`) SHALL NOT receive a rule under this requirement, and the four node-22 units already routed by the existing exact rules SHALL keep their current selections unchanged.

#### Scenario: a display-api unit diff selects its owner suite without the node-27 lane pin

- **WHEN** the changed paths are exactly `infra/systemd/nhms-display-api.service`
- **THEN** `select_tests` emits a non-empty set containing `tests/test_hydro_display_mvt_scaling.py` and not containing `tests/test_node27_timeseries_retention.py`

#### Scenario: a scheduler file-provider-refresh unit diff selects its owner suite

- **WHEN** the changed paths are exactly `infra/systemd/nhms-scheduler-file-provider-refresh.service` or exactly `infra/systemd/nhms-scheduler-file-provider-refresh.timer`
- **THEN** `select_tests` emits a non-empty set containing `tests/test_scheduler_refresh_deployment_contract.py` and not containing `tests/test_node27_timeseries_retention.py`

#### Scenario: existing node-27 owner-table selections are preserved

- **WHEN** the changed paths are exactly one of the five `infra/systemd/nhms-node27-{autopipe,download,frontier-alert,raw-retention,timeseries-compression-replay}.service` units
- **THEN** `select_tests` still emits that unit's owner suites together with `tests/test_node27_timeseries_retention.py`, and the two node-27 `.timer` rows still emit their owner suites without it

### Requirement: the scheduler refresh env template MUST select its content-asserting owner suite

`infra/env/compute.scheduler-provider-refresh.env.example` is read by path, and its content is asserted, by two suites:

- `tests/test_scheduler_refresh_deployment_contract.py`. It asserts that `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true` is present and that none of `DATABASE_URL=`, `PIPELINE_DATABASE_URL=`, `PGHOST=` or `PGPORT=` appears. That content assertion moved there when #1101 partitioned the former refresh monolith.
- `tests/test_node22_refresh_timer_health.py` (#2146). It pins the receipt-root line.

`scripts/select_ci_tests.py` SHALL carry a path-exact `PathTestRule` for the template, with neither `stop_on_match` nor `only_when_any_changed`, targeting those owner suites. The rule SHALL NOT be added to the `#1684` rollout-producer group, whose target is the static deployment contract suite and which does not read this template.

Rule matches accumulate. The `infra/env/**` rule stays in place, and the template is a production-topology scanner input. The template's selection SHALL therefore be exactly:
- the owner suites;
- `tests/test_two_node_docker_runtime.py`;
- the production-topology hard-gate node.

`tests/test_select_ci_tests.py` SHALL pin that selection as an exact set rather than by membership. The sibling `infra/env/*.example` templates SHALL keep their owner selections and gain only the hard-gate node.

#### Scenario: a refresh env template diff selects its owner suite

- **WHEN** the changed paths are exactly `infra/env/compute.scheduler-provider-refresh.env.example`
- **THEN** `select_tests` emits exactly `["tests/test_entropy_audit_report_contract.py::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings", "tests/test_node22_refresh_timer_health.py", "tests/test_scheduler_refresh_deployment_contract.py", "tests/test_two_node_docker_runtime.py"]`

#### Scenario: sibling env templates keep their existing selections

- **WHEN** the changed paths are exactly `infra/env/compute.example`
- **THEN** the selection is exactly `["tests/test_entropy_audit_report_contract.py::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings", "tests/test_slurm_gateway_deployment_contract.py", "tests/test_two_node_docker_runtime.py"]`
- **WHEN** the changed paths are exactly `infra/env/compute.scheduler-dbfree.env.example`
- **THEN** the selection is exactly `["tests/test_entropy_audit_report_contract.py::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings", "tests/test_env_templates.py", "tests/test_slurm_gateway_deployment_contract.py", "tests/test_two_node_docker_runtime.py"]`
- **WHEN** the changed paths are exactly `infra/env/display.example`
- **THEN** the selection is exactly `["tests/test_entropy_audit_report_contract.py::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings", "tests/test_two_node_docker_runtime.py"]`

### Requirement: the retention copyback mutex load-bearing modules MUST select the mutex suite

The retention copyback mutex is pinned by the two partitions `tests/test_retention_copyback_mutex_budget.py` and `tests/test_retention_copyback_mutex_protocol.py`. They came from the #2259 split of the former single mutex suite. The mutex has two load-bearing modules:
- `services/orchestrator/scheduler_runtime.py` holds its wiring: the scheduler call site that names the shared copyback root.
- `packages/common/copyback_guard.py` holds its lock semantics.

`scripts/select_ci_tests.py` SHALL select both partitions for a diff to either module:
- For `scheduler_runtime.py`, the partitions SHALL be added at the `stop_on_match` file-journal rule site that matches the path, without editing the shared `FILE_JOURNAL_READ_STATE_TESTS` constant. That rule site also carries `tests/test_retention_extra_roots.py` (#2316).
- For `copyback_guard.py`, a path-exact rule without `stop_on_match` or `only_when_any_changed` SHALL add the partitions, so that the module's other selections accumulate unchanged.

The selections for `services/orchestrator/cli.py`, `services/orchestrator/__init__.py`, `services/orchestrator/retention.py` and `tests/retention_test_helpers.py` SHALL keep selecting both partitions. The exact selection of each module is pinned in `tests/test_select_ci_tests.py`, not here. The 22/23-entry exact lists this requirement used to carry went stale when later at-site riders (#1186, #1905/#2402, #2259) grew the selection.

#### Scenario: a scheduler runtime diff selects the mutex suite

- **WHEN** the changed paths are exactly `services/orchestrator/scheduler_runtime.py`
- **THEN** the selection contains `tests/test_retention_copyback_mutex_budget.py`, `tests/test_retention_copyback_mutex_protocol.py` and `tests/test_retention_extra_roots.py`, and does not contain `tests/test_state_clone.py`

#### Scenario: a copyback guard diff selects the mutex suite without losing its owners

- **WHEN** the changed paths are exactly `packages/common/copyback_guard.py`
- **THEN** the selection contains `tests/test_copyback_guard.py`, `tests/test_retention_copyback_mutex_budget.py` and `tests/test_retention_copyback_mutex_protocol.py`, and is identical to its selection before this change
