## ADDED Requirements

### Requirement: the scheduler runtime rule site MUST select the extra-roots wiring suite

`tests/test_retention_extra_roots.py` is the oracle for the `runs_only_roots` extra-root wiring that `services/orchestrator/scheduler_runtime.py` hands to the retention deleter. `scripts/select_ci_tests.py` SHALL add that suite at the `stop_on_match` file-journal rule site matching `scheduler_runtime.py`. It SHALL NOT edit the shared `FILE_JOURNAL_READ_STATE_TESTS` constant. The selections of the other `FILE_JOURNAL_READ_STATE_PATH_PATTERNS` entries SHALL remain unchanged.

#### Scenario: a scheduler runtime diff selects both retention wiring oracles

- **WHEN** the changed paths are exactly `services/orchestrator/scheduler_runtime.py`
- **THEN** the selection contains `tests/test_retention_extra_roots.py`, `tests/test_retention_copyback_mutex_budget.py` and `tests/test_retention_copyback_mutex_protocol.py`, and every target the path selected before this change

#### Scenario: the sibling journal patterns do not move

- **WHEN** the changed paths are exactly `services/orchestrator/scheduler.py`, or exactly `services/orchestrator/scheduler_core.py`, or exactly `packages/common/safe_fs.py`
- **THEN** each selection is identical to its selection before this change

### Requirement: the file-orchestration migration rule site MUST select the rollback-lane writer suites

The five `tests/test_gateway_reconcile_writer_{prepare,launch,rollforward,receipts,quiescence}.py` suites drive the file-journal rollback lanes of `services/orchestrator/file_orchestration_migration.py` through function-body imports, which the importer-gap audit cannot derive. `scripts/select_ci_tests.py` SHALL add all five suites at the `stop_on_match` file-journal rule site matching that module. The selector meta-suite's at-site extension pin for the module SHALL list all six at-site targets. The rule SHALL still stop.

#### Scenario: a migration module diff selects the writer suites

- **WHEN** the changed paths are exactly `services/orchestrator/file_orchestration_migration.py`
- **THEN** the selection contains all five writer suites and `tests/test_journal_root_lane_adoption.py`, and does not contain `tests/test_state_clone.py`

### Requirement: frozen fixture data read by a non-gated suite MUST select that suite

`scripts/select_ci_tests.py` SHALL carry path-exact rules, with neither `stop_on_match` nor `only_when_any_changed`, for fixture data files. The files are listed below with the suites that read them:

- `tests/fixtures/basins_registry_partition_oracle.json` (#1913): read only by the selector meta-suite;
- `tests/fixtures/qhh_bootstrap_partition_oracle.json` (#1948): read only by the selector meta-suite;
- `tests/fixtures/station_series_baseline_heihe_ifs_2026060100.json`: its non-gated reader is `tests/test_object_store_forcing.py`.

The existing selections of `tests/fixtures/basins_registry_partition_additions.json` and `tests/fixtures/river_ts_templates_51f9d273.json` SHALL remain unchanged.

#### Scenario: a frozen partition oracle diff selects the meta-suite

- **WHEN** the changed paths are exactly `tests/fixtures/basins_registry_partition_oracle.json`, or exactly `tests/fixtures/qhh_bootstrap_partition_oracle.json`
- **THEN** `select_tests` emits exactly `["tests/test_select_ci_tests.py"]`

#### Scenario: the station-series baseline diff selects its reader

- **WHEN** the changed paths are exactly `tests/fixtures/station_series_baseline_heihe_ifs_2026060100.json`
- **THEN** `select_tests` emits exactly `["tests/test_object_store_forcing.py"]`

### Requirement: production-topology scanner inputs MUST select the topology hard gate

`scripts/select_ci_tests.py` SHALL mirror the entropy production-topology scanner's input set:
- the roots `scripts`, `infra/env`, `instructions/agents`, `docs/governance`, `docs/runbooks`, `openspec/changes` and `openspec/specs`;
- the direct files `AGENTS.md`, `CLAUDE.md`, `infra/README.two-node-docker.md` and `openspec/project-profile.md`;
- the scanner's skip-directory, skip-prefix and skip-root-directory predicate;
- the scanner's text-name predicate.

For every changed path in that set, a supplemental additive route SHALL select `tests/test_entropy_audit_report_contract.py::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings`. The route SHALL NOT add the node when the whole file `tests/test_entropy_audit_report_contract.py` is already selected. The route SHALL NOT change any other target, stop-rule behaviour or the unknown-backend fallback. The selector meta-suite SHALL fail when the mirror drifts from the scanner:
- the scanner yields a path the mirror rejects;
- the synthetic-tree scan set differs from the mirror;
- the mirrored skip or extension constants differ from the scanner's.

#### Scenario: the PR #2321 evidence file selects the hard gate

- **WHEN** the changed paths are exactly `openspec/changes/compressed-chunk-cold-tablespace-tiering/evidence/retirement-c4-verification.json`
- **THEN** the selection contains the hard-gate node id and every target that path selected before this change

#### Scenario: a path outside the scanner input does not get the hard gate from this route

- **WHEN** the changed paths are exactly `openapi/nhms.v1.yaml`, or exactly `docs/runbooks/x.log`, or exactly `tests/x.md`
- **THEN** the selection does not contain the hard-gate node id

#### Scenario: a scanner-module diff keeps the whole partition without a duplicate node

- **WHEN** the changed paths are exactly `scripts/governance/entropy_audit/check_topology.py`
- **THEN** the selection contains every `ENTROPY_AUDIT_TESTS` member and not the hard-gate node id

#### Scenario: a scanner root that grows beyond the mirror is caught

- **WHEN** the scanner yields a path that the selector mirror rejects
- **THEN** the selector meta-suite fails and names the path

### Requirement: new forcing-template modules MUST select the forcing shape oracles

For every changed `.py` path under the forcing discovery roots (`packages`, `workers`, `scripts`, `services`, `apps`, `db`, pinned equal to `tests/forcing_ts_template_registry.py`), `scripts/select_ci_tests.py` SHALL select every `FORCING_SQL_SHAPE_ORACLE_TESTS` member when the path has no pruned directory part and its module-level imports include `packages.common.forcing_ts_render`. The route SHALL be supplemental and additive. A changed path that is missing, not a regular file, unreadable, not UTF-8 or unparsable SHALL fall through without raising.

#### Scenario: a new module importing the renderer selects the oracles

- **WHEN** the changed paths are exactly a new file under any discovery root, for example `packages/common/forcing_ts_new_reader.py`, `services/display_api/forcing_new_view.py` or `apps/api/forcing_new_endpoint.py`, that imports `packages.common.forcing_ts_render` at module level
- **THEN** the selection contains every `FORCING_SQL_SHAPE_ORACLE_TESTS` member

#### Scenario: a new module without the import does not select the oracles

- **WHEN** the changed paths are exactly a new file under a discovery root that does not import `packages.common.forcing_ts_render`
- **THEN** the selection contains no `FORCING_SQL_SHAPE_ORACLE_TESTS` member

#### Scenario: a deleted path falls through

- **WHEN** the changed path under a discovery root does not exist on disk
- **THEN** `select_tests` returns without raising
