## ADDED Requirements

### Requirement: Shared-library targeted selection MUST retain its baseline

Targeted CI selection SHALL include the core-smoke baseline for every changed backend Python source under `packages/common/**`, in addition to every explicit, same-name, and supplemental mapping. A narrow rule SHALL NOT replace baseline coverage for this shared-library root. Other backend roots retain their existing known-rule and unknown-path fallback semantics.

#### Scenario: Narrow shared-library rule is additive

- **WHEN** a PR changes only `packages/common/state_manager.py` or only `packages/common/state_cli.py`
- **THEN** the selector output contains the narrow state suites and every core-smoke suite, including `tests/test_production_scheduler.py`

#### Scenario: Unmapped shared-library source keeps the same baseline

- **WHEN** a PR changes only an unmapped `packages/common/**` Python source
- **THEN** the selector output contains the same core-smoke baseline exactly once and does not depend on whether another explicit rule exists

#### Scenario: Non-shared rules preserve existing suppression semantics

- **WHEN** a mapped backend source outside `packages/common/**` changes
- **THEN** its existing explicit/same-name selection and core-smoke suppression behavior remain unchanged unless another requirement explicitly adds a supplemental oracle

### Requirement: Tree-scanning invariant suites MUST follow every scanned source root

A selector supplemental-rule authority SHALL map every Python source path under the four roots scanned by `tests/test_timescale_write_guard_wire_site_invariant.py` (`workers/**`, `packages/common/**`, `scripts/**`, and `db/**`) to that invariant suite. Supplemental mappings SHALL form a monotonic set union with ordinary rules, SHALL NOT stop later rules, and SHALL NOT change whether a path is known for fallback purposes. A selector meta-test SHALL derive the roots from the invariant suite's `_scan_roots` definition rather than freeze a second list.

#### Scenario: Existing writer and guard sources select the invariant

- **WHEN** a PR changes only `workers/output_parser/parser.py`, `workers/forcing_producer/store.py`, `packages/common/forcing_domain_handoff_apply.py`, or `packages/common/timescale_write_guard.py`
- **THEN** the selector output includes `tests/test_timescale_write_guard_wire_site_invariant.py` in addition to each source's ordinary selection

#### Scenario: Future file under a scanned root is covered

- **WHEN** the changed-file input names a previously nonexistent Python path under each scanned root, such as `scripts/brand_new_thing.py`
- **THEN** the selector output includes the invariant suite without requiring an at-site rule

#### Scenario: Root or supplemental rule drift is caught

- **WHEN** a scanned root is added or a supplemental mapping is deleted or narrowed
- **THEN** the selector meta-test fails and names the uncovered root or source

### Requirement: Selector-development changes MUST retain full-tree collection smoke

The selector SHALL expose a `collection_smoke_required` GitHub output whose provenance is independent of the final selected-target list shape. It SHALL be true when the final selection is exactly the selector meta-guard or when the changed-file set contains `scripts/select_ci_tests.py` or `tests/test_select_ci_tests.py`; otherwise it SHALL be false for non-empty ordinary selections. The `Unit Tests` workflow SHALL use this field to run the labeled full-tree collect-only smoke in addition to targeted assertions. Targeted assertions SHALL execute before this smoke; when the smoke is required, the full-tree collect command SHALL be executable and reachable, its failure SHALL emit its log and return nonzero, and its label SHALL NOT claim zero assertions after targeted assertions ran. One full-workflow positive contract SHALL prove both effective Actions metadata and shell behavior. It SHALL require the audited named-step identity, exact targeted-test environment binding, default root checkout/shell semantics, and fail-closed step/job policy. It SHALL reject any unapproved `run` payload before execution. A bounded probe SHALL execute only an independently authored trusted fixture and finite test-owned semantic variants, clean its process group on every exit, and prove the behavior matrix without running real tests, database, network, or arbitrary workflow commands. The existing `meta_guard_only` field SHALL remain a final-list shape property and zero-selection behavior SHALL remain unchanged.

#### Scenario: Selector source change keeps supplemental and collection oracles

- **WHEN** the only changed path is `scripts/select_ci_tests.py`
- **THEN** the selected targets include both `tests/test_select_ci_tests.py` and `tests/test_timescale_write_guard_wire_site_invariant.py`, `meta_guard_only=false`, `collection_smoke_required=true`, and the workflow also runs `pytest tests/ -q --collect-only`

#### Scenario: Selector test change keeps its collapse semantics

- **WHEN** the only changed path is `tests/test_select_ci_tests.py`
- **THEN** `meta_guard_only=true` and `collection_smoke_required=true`, so assertions and full-tree collection both run

#### Scenario: Ordinary non-collapsed selection does not pay collection smoke

- **WHEN** a non-empty selection contains an ordinary target and neither selector-development path changed
- **THEN** `collection_smoke_required=false` and the targeted job does not add the full-tree collection pass

#### Scenario: Existing meta-guard collapse still requires collection

- **WHEN** missing-target filtering or an unrouted support-module change leaves exactly the selector meta-guard suite
- **THEN** both `meta_guard_only=true` and `collection_smoke_required=true`

#### Scenario: Collection consumer cannot be satisfied by inert shell text

- **WHEN** a valid workflow mutation leaves required condition, targeted-test, collect, label, log, or exit tokens only in comments, quoted fragments, no-op/dead branches, or unreachable control-flow positions
- **THEN** the same positive helper used for the live named step reports the corresponding condition, ordering, collection, truthful-label, or fail-closed violation

#### Scenario: Collection failure remains an observable job failure

- **WHEN** `collection_smoke_required=true` and the trusted behavior fixture makes the full-tree collect command fail
- **THEN** the audited named-step program invokes targeted assertions first, invokes full-tree collection, emits the collection log, and returns nonzero

#### Scenario: Workflow payload and metadata cannot bypass the collection oracle

- **WHEN** a valid workflow mutation changes the named step payload, condition, continuation policy, `TARGETED_TESTS_JSON` binding, shell, working directory, or an inherited job/workflow `defaults.run` field
- **THEN** the full-workflow positive helper reports a named identity or effective-metadata violation before any unapproved payload executes

#### Scenario: Collection probe cleans timed-out descendants

- **WHEN** a test-owned controlled fixture starts a descendant and the bounded probe times out
- **THEN** the probe returns a named timeout violation and no process in its new process group remains alive

### Requirement: Irregular source and package routes MUST select their owned suites

The targeted selector SHALL map every tracked module under `workers/mapping_builder/**` to every tracked `tests/test_mapping_builder_*.py` suite, map `packages/common/state_clone_hook.py` to `tests/test_state_clone_cutover_hook.py`, and map `scripts/node22_clone_direct_grid_cutover_states.py` to both state-clone recalibration suites. Variable package sets SHALL be derived from the tracked tree where the naming/domain is stable, while intentionally irregular file-to-suite names remain explicit.

#### Scenario: Mapping-builder package selects all package suites

- **WHEN** any one of the eight tracked `workers/mapping_builder/*.py` modules changes
- **THEN** the output includes all eight tracked `tests/test_mapping_builder_*.py` suites, and the directory importer-gap guard covers the package

#### Scenario: State-clone hook selects its irregular suite

- **WHEN** a PR changes only `packages/common/state_clone_hook.py`
- **THEN** the output includes `tests/test_state_clone_cutover_hook.py`

#### Scenario: Node-22 clone script selects both recalibration suites

- **WHEN** a PR changes only `scripts/node22_clone_direct_grid_cutover_states.py`
- **THEN** the output includes `tests/test_state_clone_recalibration.py` and `tests/test_state_clone_recalibration_cli.py`

### Requirement: Integration-owned production sources MUST trigger real-database CI

The CI `database` paths filter SHALL match every production source surface in the finite integration-trigger registry defined by this change: `packages/common/forecast_store.py`, `packages/common/display_coverage.py`, `services/tiles/mvt.py`, `apps/api/routes/hydro_display.py`, `apps/api/main.py`, `scripts/node27_autopipeline.py`, `workers/output_parser/parser.py`, `packages/common/timescale_write_guard.py`, `packages/common/object_store.py`, `packages/common/model_registry.py`, `packages/common/grid_registry_store.py`, `workers/grid_registry/**`, `workers/model_registry/**`, `workers/forcing_producer/**`, and `services/orchestrator/scheduler.py`. The selector contract suite SHALL parse the `database:` filter and mechanically assert that each registered path or tracked member of a registered root matches at least one filter pattern; a workflow change SHALL self-select that contract suite. Matching the filter SHALL open the existing `real-db-integration` job, which runs the full `-m integration` suite with its PostgreSQL/Timescale service and dedicated `NHMS_INTEGRATION_DATABASE_URL`. The job SHALL expose node-level pass/skip evidence with pytest `-vv -rs`; this verbosity-only evidence change SHALL NOT alter its marker expression, dedicated DSN, service, job gate, or suite selection. One full-workflow positive job-contract helper SHALL validate those properties and the named step's effective execution context: step-level environment SHALL NOT replace either integration gate variable, a step condition SHALL NOT disable the command, step/job error-continuation policy SHALL NOT make a failing integration command non-blocking, and direct or inherited custom shell/working-directory metadata SHALL NOT alter the audited root-checkout command semantics. Deletion or effective blanking/opt-out of the dedicated integration context SHALL produce a named contract violation rather than a green job whose required nodes all skip.

#### Scenario: Forecast-store-only diff opens the parity oracle lane

- **WHEN** a non-draft PR changes only `packages/common/forecast_store.py`
- **THEN** the `changes` job reports `database=true`, `SQL Migration Dry Run` runs, and all seven tests in `tests/test_display_coverage_residual_debt_integration.py` appear as executed `PASSED` node IDs rather than skips

#### Scenario: Integration source registry and filter cannot drift silently

- **WHEN** any registered source path no longer matches a `database` pattern, including a mutation that removes the `packages/common/forecast_store.py` pattern
- **THEN** `tests/test_select_ci_tests.py` fails and names the uncovered source

#### Scenario: Dedicated integration DSN cannot disappear silently

- **WHEN** the `real-db-integration` workflow block loses `NHMS_INTEGRATION_DATABASE_URL` while retaining generic `DATABASE_URL`, `NHMS_RUN_INTEGRATION`, and `pytest -vv -rs -m integration`
- **THEN** the same positive job-contract helper used for the live workflow reports a violation naming `NHMS_INTEGRATION_DATABASE_URL`, because the integration fixture ignores generic `DATABASE_URL` without an explicit compatibility flag

#### Scenario: Named integration step cannot override or bypass its job contract

- **WHEN** a valid workflow mutation adds a step-level opt-out or blank dedicated DSN, disables the named integration step with its `if`, enables step/job `continue-on-error`, or sets direct/inherited custom shell or working-directory metadata
- **THEN** the same full-workflow structured positive helper used for the live job reports a named effective-environment, execution-condition, fail-closed-policy, shell, or working-directory violation

#### Scenario: Workflow changes execute the trigger contract

- **WHEN** `.github/workflows/ci.yml` changes
- **THEN** targeted selection includes `tests/test_select_ci_tests.py`, so the database-filter and real-DB job contracts execute on that PR

#### Scenario: Existing CI lanes retain their contracts

- **WHEN** the database filter gains the finite integration-owned source patterns and the real-DB pytest command gains `-vv -rs`
- **THEN** `unit-test-targeted`, master `unit-test`, frontend/docs/openapi/schema filters, draft gating, the integration marker expression, dedicated integration DSN, PostgreSQL/Timescale service, real-DB job gate, and suite selection remain unchanged

## MODIFIED Requirements

### Requirement: Guarded-module selector rules MUST cover their non-gated importer closure

The targeted-test selector SHALL, for each selector-guarded production module (currently `packages.common.display_coverage`, `services.slurm_gateway.real_backend`, `services.tiles.mvt`, and `apps.api.routes.hydro_display`), via the rule owning that module's path, select every tracked `tests/test_*.py` that imports the module at top level and carries no `integration`/`e2e` gating marker, and a mechanized selector test SHALL derive that importer set from the tracked tree (never a frozen list) so that a new importer suite or a removed rule entry fails the selector suite instead of silently falling out of the PR lane.
The derivation SHALL additionally extend exactly ONE import hop beyond the guarded module: tracked non-test modules importing the guarded module at top level contribute their own non-gated top-level importer suites to the required set. The single-hop bound is deliberate forward-looking policy — it forecloses unbounded transitive growth (an any-depth derivation reaches roughly five times the one-hop set for `real_backend`) while today's top-level-import fixed point equals the one-hop set, and both derivation and bound rationale live in the selector test suite, never as frozen lists.
Modules under the ten audited directory paths are additionally governed by the requirement "Directory-rule importer gaps MUST be dispositioned as selections or reasoned exclusions", which owns the normative disposition rule for their direct-importer gaps.

#### Scenario: production-module change selects its importer suites

- **WHEN** a PR changes only `packages/common/display_coverage.py` or only `services/slurm_gateway/real_backend.py`
- **THEN** the selector output includes every non-gated top-level importer test suite of that module (for `real_backend`: `tests/test_real_slurm_gateway.py`, `tests/test_slurm_array_contract.py`, `tests/test_job_array.py`; for `display_coverage`: `tests/test_display_coverage_refresh.py`, `tests/test_display_coverage_parallel.py`, `tests/test_forecast_api.py`)
- **WHEN** a PR changes only `services/tiles/mvt.py`
- **THEN** the selector output includes every non-gated direct importer suite of `services.tiles.mvt` (including `tests/test_hhe_mvt_binding.py`, `tests/test_hydro_display_mvt_scaling.py`, `tests/test_node27_timeseries_compression_benchmark.py`, `tests/test_node27_timeseries_compression_live_evidence.py`) and every one-hop suite contributed by `apps/api/routes/hydro_display.py` (including `tests/test_direct_grid_display_cutover_flip.py`, `tests/test_direct_grid_display_cutover_history.py`, `tests/test_direct_grid_display_cutover_model_resolution.py`), and no core-smoke-only fallback suite
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

### Requirement: Directory-rule importer gaps MUST be dispositioned as selections or reasoned exclusions

For every tracked module under the ten audited directory paths (`workers/output_parser`, `workers/data_adapters`, `workers/forcing_producer`, `workers/shud_runtime`, `workers/model_registry`, `workers/mapping_builder`, `services/orchestrator`, `services/slurm_gateway`, `services/tile_publisher`, `services/production_closure` — including modules owned by earlier stop-rules rather than the directory rules themselves), a mechanized selector-suite guard SHALL derive the non-gated top-level importer suites the selector does not select for that module (node-id-only selection counts as a gap) and fail unless each such gap pair is either closed by a rule or present in an explicit exclusion table whose entries carry a reason token from {`fn-gated`, `redirect`, `edge-consumer`, `runtime-budget`} — where `fn-gated` denotes gating invisible to the file-level marker filter (function-level markers or environment opt-ins), established at disposition time by a recorded measurement in which the suite executed zero assertions, `edge-consumer` entries are guard-checked to be selected by at least one rule whose pattern does not match the excluded module, and `redirect` entries are guard-checked to still reach the suite via node-qualified ids in the module's own selection — and a stale exclusion (a pair that no longer derives as a gap) SHALL also fail the guard, so the audit that previously lived in a PR body can never rot silently again.

#### Scenario: an undispositioned gap reds the suite

- **WHEN** a non-gated test suite imports a directory-rule-owned module at top level and neither a rule selects it nor an exclusion entry names the pair
- **THEN** the disposition guard fails, naming the module and suite

#### Scenario: a stale exclusion reds the suite

- **WHEN** an exclusion entry's pair no longer derives as a gap (the rule now selects it, or the module/suite left the tree)
- **THEN** the disposition guard fails, naming the stale entry

#### Scenario: reasoned exclusions satisfy the guard by token and liveness

- **WHEN** a gap pair is present in the exclusion table with a valid reason token and the pair still derives as a gap (for `edge-consumer`: and a rule not matching the module selects the suite; for `redirect`: and the module's selection reaches the suite via node-qualified ids)
- **THEN** the guard passes for that pair — the guard checks token validity, pair liveness, and the structural `edge-consumer`/`redirect` conditions only; `fn-gated`/`runtime-budget` truthfulness is anchored by the recorded measurement table in the delivering PR, not re-executed by the guard

#### Scenario: mapping-builder joins the audited domain with owned state-clone edges dispositioned

- **WHEN** `workers/mapping_builder` is added to the audited directory paths with its package-wide selector rule
- **THEN** its eight mapping-builder-suite gaps are closed by the rule, the three state-clone importer pairs remain selected by non-matching state-clone owner rules and are recorded as `edge-consumer`, and the disposition guard reports zero undispositioned pairs without adding `tests/test_state_clone.py` to the mapping-builder rule
