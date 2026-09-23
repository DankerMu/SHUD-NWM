## ADDED Requirements

### Requirement: the scheduler runtime rule site MUST select the extra-roots wiring suite

`tests/test_retention_extra_roots.py` is the oracle for the `runs_only_roots` extra-root wiring that `services/orchestrator/scheduler_runtime.py` hands to the retention deleter. `scripts/select_ci_tests.py` SHALL add that suite at the `stop_on_match` file-journal rule site matching `scheduler_runtime.py`. It SHALL NOT edit the shared `FILE_JOURNAL_READ_STATE_TESTS` constant, and this rule-site edit SHALL NOT change the selection of any other `FILE_JOURNAL_READ_STATE_PATH_PATTERNS` entry (pattern [3]'s own at-site extension is the separate requirement below).

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

## MODIFIED Requirements

### Requirement: the scheduler refresh env template MUST select its content-asserting owner suite

`infra/env/compute.scheduler-provider-refresh.env.example` is read by path, and its content is asserted, by two suites:

- `tests/test_scheduler_refresh_deployment_contract.py`. It asserts that `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true` is present and that none of `DATABASE_URL=`, `PIPELINE_DATABASE_URL=`, `PGHOST=` or `PGPORT=` appears. That content assertion moved there from the retired `tests/test_scheduler_file_provider_refresh.py` when #1101 partitioned the monolith.
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

The retention copyback mutex is pinned by the two partitions `tests/test_retention_copyback_mutex_budget.py` and `tests/test_retention_copyback_mutex_protocol.py`. They came from the #2259 split of the former `tests/test_retention_copyback_mutex.py`. The mutex has two load-bearing modules:
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

### Requirement: Empty targeted-test selection MUST be loudly self-identifying

The `Unit Tests` job's collect-only fallback SHALL be independently
recognizable as a zero-assertion run whenever the PR backend gate is open
but the targeted-test selector maps the diff to zero test files: it MUST
emit a workflow
warning annotation and a step summary stating that no assertions were
executed, and the collection outcome MUST be surfaced in the job log — the
collected-count summary on success and the full collection output on
failure, with a collection failure still failing the step. The selector SHALL
NOT silently shrink its selection: when a rule-selected test target no
longer exists in the tree, the selector MUST emit a warning naming the
dropped target (stderr always; a workflow warning annotation when running
under GitHub Actions) while keeping its return-value semantics unchanged.
The collect-only branch's check name and pass/fail semantics are unchanged
by this requirement (gate-strength changes are out of scope).
Additionally, when the final selection collapses to exactly the selector
meta-guard suite (`meta_guard_only` — a property of the final
selection's shape only, with the supplemental production-topology hard-gate
node disregarded: that node rides almost every PR, because each PR's final push
carries its `openspec/changes/**/tasks.md`, so counting it would hide every
collapse; it fires for a PR whose only backend change is
`tests/test_select_ci_tests.py`, but not for a PR that changes
`scripts/select_ci_tests.py`, whose supplemental routing selects more
than the meta-guard suite; that PR keeps the full-tree smoke through
`collection_smoke_required` under "Selector-development changes MUST
retain full-tree collection smoke"), the selector SHALL expose the collapse as a
distinguishable GitHub-output field and the `Unit Tests` job SHALL run
the targeted selection AND the labeled full-tree collect-only smoke,
whose labeling on this branch MUST NOT claim zero assertions were
executed; a PR whose only backend change deletes a test file (or
touches only a `tests/` support module without derived non-gated
importer suites) thereby keeps the import-surface guard it had
before the meta-guard accumulation existed. Support modules WITH
derived non-gated importer suites are governed by the requirement
"Support-module changes MUST select their non-gated importer
suites", which routes them to assertion-level targets instead of
this collapse path.

The pinned empty-selection class "`.py` outside the five backend prefixes" is
narrowed by the river-segment write-surface routing. `BACKEND_PYTHON_SOURCE_PREFIXES`
is `apps/api/`, `packages/`, `services/`, `workers/`, `scripts/`, so before that
routing every `.py` under `apps/` that was not under `apps/api/` fell in this
class. Those paths are inside the write-surface scan's `PRODUCTION_DIRS`, so
they now select that one suite and are no longer empty. The class SHALL
therefore be respelled as the `.py` paths that are under NONE of: a backend
prefix, the write-surface scan's five roots, or the timescale write-guard
invariant's four roots — the last of which matters because `db/**` is neither a
backend prefix nor a write-surface root, yet a `.py` path under it selects the
timescale invariant and the migration suite, so a two-clause spelling would
wrongly claim `db/x.py` is empty. The production-topology reader route
(#2323) narrows the class once more: a path under that scanner's roots or equal
to one of its direct files, with a scannable text name, selects the hard-gate
node, so the class excludes those too. That removes the `.py` paths under
`openspec/changes/**` and `openspec/specs/**` (tracked evidence scripts live
there), and it also moves non-`.py` scannable text under `scripts/` (for
example `scripts/node27_display_v2_browser_evidence.mjs`) off the empty
selection. Its tracked members today are the `.py` paths under `.agents/**`
and under `openspec/**` outside `changes/` and `specs/`; a `.py` under `docs/**`, `.github/**` or an
unmapped `infra/**` would join them, and none is tracked today. This is the route-A
selector-widening the class was explicitly left open for, and it is a real
change today, not only for future paths: `apps/__init__.py` is a tracked file
that moves from an empty selection to exactly the write-surface scan, losing the
zero-assertion full-tree collect-only smoke it used to receive and gaining an
assertion-executing targeted run instead. The mechanism is the selector's
`count` output: the job's collect-only branch is guarded by `count == 0`, so a
one-element selection takes the targeted branch. Neither carve-out re-arms the
smoke — `meta_guard_only` fires only for a selection that is exactly the
selector meta-guard suite, and `collection_smoke_required` is false for this
class both before and after, since neither the selector source nor its suite is
in the diff.

#### Scenario: collect-only fallback is labeled as zero assertions

- **WHEN** a PR hits the `backend` paths-filter but the selector returns
  zero test files (e.g. a `schemas/**`-only change)
- **THEN** the `Unit Tests` job run shows a warning annotation and a step
  summary stating that 0 assertions were executed and only collect-only
  import/syntax smoke ran, and the pytest collected-count summary appears
  in the job log (full collection output on failure, which fails the step)

#### Scenario: stale rule target is dropped with a warning, not silently

- **WHEN** a selection rule maps a changed path to a test file that does
  not exist in the tree
- **THEN** the selector drops the target from its output but emits a
  warning naming the missing target, and emits no such warning when every
  selected target exists

#### Scenario: known empty-selection input classes are pinned

- **WHEN** the diff consists only of files in the known unmapped classes
  (`schemas/**`, unmapped `infra/**`, `.py` under none of the backend
  prefixes, the write-surface scan's roots, the timescale invariant's
  roots and the production-topology scan inputs, non-`.py` under backend
  prefixes that is not a production-topology scan input, non-`.py` under `tests/`,
  `.sh` files outside `scripts/`; `scripts/**/*.sh` left this list when it
  joined the backend gate — an unmapped one now arms the core-smoke fallback)
- **THEN** the selector returns an empty selection and the selector test
  suite pins each class explicitly as the route-C contract, so any future
  route-A/B policy change must flip a visible assertion

#### Scenario: a Python path under apps but outside apps/api leaves the pinned-empty class

- **WHEN** the changed paths are exactly `apps/__init__.py`, a tracked file, or `apps/frontend/scripts/gen.py`, a future-shaped one
- **THEN** the returned selection is exactly `["tests/test_river_segment_write_surface_scan.py"]`, the selector's GitHub output reports a count of 1 with `meta_guard_only=false`, and neither path appears among the pinned empty-selection classes

#### Scenario: a Python path under db stays out of the pinned-empty class

- **WHEN** the changed paths are exactly `db/brand_new_thing.py`
- **THEN** the returned selection is non-empty — it contains `tests/test_timescale_write_guard_wire_site_invariant.py` — and does not contain `tests/test_river_segment_write_surface_scan.py`

#### Scenario: meta-guard-only collapse restores the collect-only smoke

- **WHEN** a PR's only backend change deletes one `tests/test_*.py` file,
  so the missing-target filter leaves exactly
  `tests/test_select_ci_tests.py` in the selection
- **THEN** the selector's GitHub output reports `meta_guard_only=true`,
  and the `Unit Tests` job runs the meta-guard suite and additionally the
  labeled full-tree collect-only smoke, with a collection failure failing
  the step

#### Scenario: non-collapsed selections suppress the flag

- **WHEN** the selection contains any target other than the selector
  meta-guard suite, or is empty
- **THEN** the GitHub output reports `meta_guard_only=false` and the
  targeted branch behaves as before

#### Scenario: selector-development PRs fire the flag honestly

- **WHEN** the diff's only backend change is
  `tests/test_select_ci_tests.py`, so the diff-specific selection IS
  exactly the meta-guard suite
- **THEN** `meta_guard_only=true` and the collect-only smoke also runs
  — accepted by design (one extra collection pass on exactly the PR
  class that changes the gate), and the smoke labeling does not claim
  the run executed zero assertions
- **AND** when the diff's only backend change is instead
  `scripts/select_ci_tests.py`, the selection is not collapsed: it
  also contains the supplemental invariant suites routed from
  `scripts/**` (the selection includes `tests/test_select_ci_tests.py`
  plus supplemental invariant suites such as
  `tests/test_river_segment_write_surface_scan.py` and
  `tests/test_timescale_write_guard_wire_site_invariant.py`), so
  `meta_guard_only=false` as "non-collapsed selections suppress the
  flag" requires, while `collection_smoke_required=true` still runs
  the collect-only smoke

#### Scenario: the supplemental topology node does not mask the collapse

- **WHEN** the changed paths are a deleted `tests/test_*.py` file (or an unrouted `tests/` support module, or `tests/fixtures/basins_registry_partition_oracle.json`) together with an `openspec/changes/**/tasks.md`
- **THEN** the selection is the selector meta-guard suite plus the production-topology hard-gate node, and the GitHub output reports `meta_guard_only=true` and `collection_smoke_required=true`

#### Scenario: a data-only diff with a tasks update runs the hard-gate node instead of the zero-assertion smoke

- **WHEN** the changed paths are exactly `schemas/foo.json` and `openspec/changes/x/tasks.md`
- **THEN** the selection is exactly the production-topology hard-gate node, count is 1, and both `meta_guard_only` and `collection_smoke_required` are false. This is the accepted trade of the `apps/__init__.py` precedent: the diff-specific class is non-importable data
