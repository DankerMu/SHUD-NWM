## MODIFIED Requirements

### Requirement: Guarded-module selector rules MUST cover their non-gated importer closure

The targeted-test selector SHALL, for each selector-guarded production module (currently `packages.common.display_coverage`, `services.slurm_gateway.real_backend`, `services.tiles.mvt`, and `apps.api.routes.hydro_display`), via the rule owning that module's path, select every tracked `tests/test_*.py` that imports the module at top level and carries no `integration`/`e2e` gating marker, and a mechanized selector test SHALL derive that importer set from the tracked tree (never a frozen list) so that a new importer suite or a removed rule entry fails the selector suite instead of silently falling out of the PR lane.
The derivation SHALL additionally extend exactly ONE import hop beyond the guarded module: tracked non-test modules importing the guarded module at top level contribute their own non-gated top-level importer suites to the required set. The single-hop bound is deliberate forward-looking policy — it forecloses unbounded transitive growth (an any-depth derivation reaches roughly five times the one-hop set for `real_backend`) while today's top-level-import fixed point equals the one-hop set, and both derivation and bound rationale live in the selector test suite, never as frozen lists.
Modules under the ten audited directory paths are additionally governed by the requirement "Directory-rule importer gaps MUST be dispositioned as selections or reasoned exclusions", which owns the normative disposition rule for their direct-importer gaps.

#### Scenario: production-module change selects its importer suites

- **WHEN** a PR changes only `packages/common/display_coverage.py` or only `services/slurm_gateway/real_backend.py`
- **THEN** the selector output includes every non-gated top-level importer test suite of that module (for `real_backend`: `tests/test_real_slurm_gateway.py`, `tests/test_slurm_array_contract.py`, `tests/test_job_array.py`; for `display_coverage`: `tests/test_display_coverage_refresh.py`, `tests/test_display_coverage_parallel.py`, `tests/test_forecast_api.py`)
- **WHEN** a PR changes only `services/tiles/mvt.py`
- **THEN** the selector output includes every non-gated direct importer suite of `services.tiles.mvt` (including `tests/test_hhe_mvt_binding.py`, `tests/test_hydro_display_mvt_scaling.py`, `tests/test_node27_timeseries_compression_benchmark.py`, `tests/test_node27_timeseries_compression_live_evidence.py`) and every one-hop suite contributed by `apps/api/routes/hydro_display.py` (including `tests/test_direct_grid_display_cutover_flip_atomic.py`, `tests/test_direct_grid_display_cutover_history.py`, `tests/test_direct_grid_display_cutover_model_resolution.py`), plus `tests/test_direct_grid_display_cutover_flip_mvt_set.py`, which imports no guarded module and is carried by the same rule as the model of the station-row predicate the `_atomic` partition locks, and no core-smoke-only fallback suite
- **WHEN** a PR changes only `apps/api/routes/hydro_display.py`
- **THEN** the selector output includes its full non-gated direct union one-hop importer closure, including direct-grid cutover, display status, HHE/MVT, node-27 compression and attribution, OpenAPI, and runtime-mode suites

#### Scenario: closure completeness is mechanized

- **WHEN** a new non-gated test suite importing a guarded module at top level is added to the tree without extending the owning rule
- **THEN** the traversal guard in the selector test suite fails, naming the missing suite

#### Scenario: gated importer suites are deliberately excluded

- **WHEN** an importer test suite is gated by an `integration` or `e2e` marker (skipped in the PR lane)
- **THEN** the traversal guard does not require it in the rule, and the exclusion rationale is recorded next to the rule

#### Scenario: one-hop importer suites are selected

- **WHEN** a PR changes only `services/slurm_gateway/real_backend.py`
- **THEN** the selector output includes the non-gated top-level importer suites of the modules that import `real_backend` at top level (including `tests/test_reconcile_sacct_parse.py`, which pins the sacct parsing constants consumed by `services/orchestrator/reconcile.py`), and the guard derives this one-hop set from the tree without recursing further

#### Scenario: Existing MVT closure remains an exact compatibility anchor

- **WHEN** the selector and guarded-module registry change for this batch
- **THEN** the `services/tiles/mvt.py` exact-set pin and guarded closure delivered by #1597 remain green without removing an existing MVT target

### Requirement: display and scheduler unit files with a content-asserting owner suite MUST select that suite

`infra/systemd/nhms-display-api.service`, `infra/systemd/nhms-scheduler-file-provider-refresh.service` and `infra/systemd/nhms-scheduler-file-provider-refresh.timer` each have at least one suite that `read_text`s that path and asserts its directives, and none of them lies inside the `infra/systemd/nhms-node27-*.service` glob rule, so before this change a unit-only diff selected nothing at all and the targeted job degraded to a zero-assertion `--collect-only` smoke. `scripts/select_ci_tests.py` SHALL carry a path-exact `PathTestRule` (neither `stop_on_match` nor `only_when_any_changed`) for each: the display-api unit targeting `tests/test_hydro_display_mvt_scaling.py`, and both scheduler file-provider-refresh units targeting `tests/test_scheduler_refresh_deployment_contract.py`; the `.service` unit also selects every `tests/test_node22_refresh_timer_health_*.py` partition (#2532 split the former monolith into five partitions plus the non-collectible `tests/node22_refresh_timer_health_helpers.py`; the former monolith read it). Because none of the three matches the node-27 glob, none of the three selections SHALL contain `tests/test_node27_timeseries_retention.py`. The node-27 owner-table meta test SHALL decide whether a unit owes the sibling lane pin by matching that glob rather than by the `.service` suffix, so that a node-27 unit named outside the `nhms-node27-` prefix is judged correctly. `nhms-display-api.service` is not an `nhms-node27-*`-named unit and therefore lies outside the domain of "node-27 unit files with a content-asserting owner suite MUST select that suite", whose scope is the `infra/systemd/nhms-node27-*.service` glob; it is governed by this requirement instead. Units with no content-asserting reader (`nhms-compute-compose.service`, `nhms-display-compose.service`, `nhms-node27-frontier-alert.timer`, `nhms-node27-raw-retention.timer`, `nhms-scheduler-evidence-retention.timer`) SHALL NOT receive a rule under this requirement, and the four node-22 units already routed by the existing exact rules SHALL keep their current selections unchanged.

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
- `tests/test_node22_refresh_timer_health_history.py` (#2146). It pins the receipt-root line; it is the only partition of the former `tests/test_node22_refresh_timer_health.py` monolith (#2532) that reads the template.

`scripts/select_ci_tests.py` SHALL carry a path-exact `PathTestRule` for the template, with neither `stop_on_match` nor `only_when_any_changed`, targeting those owner suites. The rule SHALL NOT be added to the `#1684` rollout-producer group, whose target is the static deployment contract suite and which does not read this template.

Rule matches accumulate. The `infra/env/**` rule stays in place, and the template is a production-topology scanner input. The template's selection SHALL therefore be exactly:
- the owner suites;
- `tests/test_two_node_docker_runtime.py`;
- the production-topology hard-gate node.

`tests/test_select_ci_tests.py` SHALL pin that selection as an exact set rather than by membership. The sibling `infra/env/*.example` templates SHALL keep their owner selections and gain only the hard-gate node.

#### Scenario: a refresh env template diff selects its owner suite

- **WHEN** the changed paths are exactly `infra/env/compute.scheduler-provider-refresh.env.example`
- **THEN** `select_tests` emits exactly `["tests/test_entropy_audit_report_contract.py::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings", "tests/test_node22_refresh_timer_health_history.py", "tests/test_scheduler_refresh_deployment_contract.py", "tests/test_two_node_docker_runtime.py"]`

#### Scenario: sibling env templates keep their existing selections

- **WHEN** the changed paths are exactly `infra/env/compute.example`
- **THEN** the selection is exactly `["tests/test_entropy_audit_report_contract.py::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings", "tests/test_slurm_gateway_deployment_contract.py", "tests/test_two_node_docker_runtime.py"]`
- **WHEN** the changed paths are exactly `infra/env/compute.scheduler-dbfree.env.example`
- **THEN** the selection is exactly `["tests/test_entropy_audit_report_contract.py::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings", "tests/test_env_templates.py", "tests/test_slurm_gateway_deployment_contract.py", "tests/test_two_node_docker_runtime.py"]`
- **WHEN** the changed paths are exactly `infra/env/display.example`
- **THEN** the selection is exactly `["tests/test_entropy_audit_report_contract.py::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings", "tests/test_two_node_docker_runtime.py"]`
