# Tasks: ci-gate-routing-closure-batch

Fixture level: expanded · Repair intensity: broad-expanded
Issues: #1711 #1672 #1656 #1688 #1744, plus already-delivered #1597 traceability
Upstream suggested fixture level: absent; effective fixture is expanded because this batch changes a shared CI entrypoint and workflow trigger.
Minimal mergeable slice: the selector additive/supplemental invariant and its meta-tests; the workflow trigger is independently revertible but ships in the same CI-gate batch.

## 1. Selector semantics and routes

- [x] 1.1 Make `packages/common/**` Python selection retain all `CORE_SMOKE_TESTS` alongside explicit/same-name/supplemental targets; preserve all non-shared fallback and stop-rule behavior. (#1744 path B)
- [x] 1.2 Add supplemental monotonic mappings from `workers/**`, `packages/common/**`, `scripts/**`, and `db/**` to `tests/test_timescale_write_guard_wire_site_invariant.py`, without changing known-path matching or stop rules. (#1656)
- [x] 1.3 Extend `apps/api/routes/hydro_display.py` to its current direct union one-hop non-gated importer closure and register it in `GUARDED_MODULE_CLOSURES`. (#1672)
- [x] 1.4 Add `workers/mapping_builder/**` -> all eight `tests/test_mapping_builder_*.py` suites and add the package to `DIRECTORY_RULE_AUDIT_PATHS`. (#1711)
- [x] 1.5 Add explicit irregular rules for `packages/common/state_clone_hook.py` and `scripts/node22_clone_direct_grid_cutover_states.py`. (#1711)
- [x] 1.6 Keep the #1597 `services/tiles/mvt.py` rule, exact-set pin, and guarded closure behavior unchanged; record it as predecessor evidence rather than new work.

## 2. Workflow trigger and mechanical guards

- [x] 2.1 Extend `.github/workflows/ci.yml` `database` filter to cover the finite D4 registry: `packages/common/{forecast_store,display_coverage,timescale_write_guard,object_store,model_registry,grid_registry_store}.py`, `services/tiles/mvt.py`, `apps/api/{main.py,routes/hydro_display.py}`, `scripts/node27_autopipeline.py`, `workers/output_parser/parser.py`, `workers/{grid_registry,model_registry,forcing_producer}/**`, and `services/orchestrator/scheduler.py`; also add `.github/workflows/ci.yml` for gate self-triggering. No listed surface is deferred.
- [x] 2.2 Add a selector meta-test that derives the Timescale invariant's four scan roots from its source and proves the supplemental mappings cover each existing and future-shaped path.
- [x] 2.3 Add/extend selector meta-tests for shared fallback additivity, hydro-display closure, mapping-builder exact package coverage, and both irregular state-clone mappings.
- [x] 2.4 Add a mechanical database-filter contract test using D4's finite registry as its authority. Parse the `database:` block, expand bounded roots over tracked files, require every path to match, assert `.github/workflows/ci.yml` self-triggers and selects `tests/test_select_ci_tests.py`, and include a mutation that removes `packages/common/forecast_store.py` and must fail naming that exact source.
- [x] 2.5 Change only the real-DB pytest logging flags from `pytest -q -m integration` to `pytest -vv -rs -m integration`; preserve marker expression, service container, DSN, and suite selection so CI logs expose all seven residual-debt node IDs as PASSED/non-skipped.
- [x] 2.6 Round-1 closure: add `collection_smoke_required` provenance independent of `meta_guard_only`; selector-source-only changes must select meta guard + Timescale invariant and still drive the workflow's full-tree collect-only branch. Preserve zero-selection and ordinary selection behavior.
- [x] 2.7 Round-1 closure: replace the real-DB substring-only test with a structured job-scoped contract helper that requires `needs: changes`, dispatch plus database-triggered push/non-draft-PR gate semantics, Timescale service image, job-level opt-in parsed exactly as string `"1"` under runtime semantics, a non-empty job-level dedicated `NHMS_INTEGRATION_DATABASE_URL`, and the named integration command step; deleting/blanking/relocating the dedicated DSN, removing the master-push leg or `needs`, relocating the opt-in, and using a literal-quoted opt-in value must each yield a named violation through the same helper.
- [x] 2.8 Round-1 closure: route the supplemental-root, shared-baseline, mapping-builder, and database-registry live and mutant paths through the same positive invariant helpers, with live state asserting no violations and each mutant producing a named violation instead of a green negative observation.

## 3. Required red proofs

- [x] 3.1 Before source edits, land only new behavior tests and run one batched red command. Expected red rows: `state_cli.py` lacks at least `tests/test_production_scheduler.py`; each of `workers/new_guard_probe.py`, `packages/common/new_guard_probe.py`, `scripts/brand_new_thing.py`, and `db/new_guard_probe.py` lacks `tests/test_timescale_write_guard_wire_site_invariant.py`; hydro-display closure reports its derived missing set; one mapping-builder module lacks the eight-suite package set; state-clone hook lacks `test_state_clone_cutover_hook.py`; node-22 clone script lacks both recalibration suites; and database filter coverage names `packages/common/forecast_store.py`. Capture invocation/output; then restore source and leave no `red-proof` stash.
- [x] 3.2 Prove #1744 path B directly: pre-fix `select_tests(["packages/common/state_cli.py"])` lacks `tests/test_production_scheduler.py`; final output contains every `CORE_SMOKE_TESTS` member plus state suites. Separately replay #1738 by injecting `AND usable_flag = true` into the relevant `state_manager.py` lineage SQL, run the selector-chosen lane and require a red assertion, then restore; this mutation is compatibility evidence, not a substitute for the additive proof.
- [x] 3.3 Prove #1656 on four roots: each future-shaped path from 3.1 selects the invariant suite. Then add an unguarded `DELETE FROM hydro.river_timeseries` to a non-allowlisted scanned script, show that source selects the suite and the suite is red, then restore.
- [x] 3.4 Add hydro-display to `GUARDED_MODULE_CLOSURES` before filling its rule and show `test_guarded_module_rules_cover_their_non_gated_importer_closure` red with the current derived missing set; then fill the rule and make it green.
- [x] 3.5 Prove #1711 exactly: hook selection contains `test_state_clone_cutover_hook.py`; every tracked mapping-builder module contains all eight package suites and its explicit rule contains no `tests/test_state_clone.py`; node-22 clone script contains both recalibration suites; after adding mapping-builder to directory audit, the three state-clone importer pairs are live `edge-consumer` dispositions selected by non-matching owner rules and `test_directory_rule_importer_gaps_are_dispositioned` reports zero undispositioned gaps.
- [x] 3.6 Prove #1688 filter mutation: remove only `packages/common/forecast_store.py` from a constructed workflow copy, run the contract helper, and require an error naming that source; final workflow-only selection is exactly/assertion-level `tests/test_select_ci_tests.py`, and the workflow path matches both backend and database filters.
- [x] 3.7 Prove #1597 compatibility: the existing MVT exact-set test and guarded-module closure test stay green without changing/removing an existing MVT target.

## 4. Local evidence floor

- [x] 4.1 `uv run pytest -q tests/test_select_ci_tests.py` passes on final source.
- [x] 4.2 Run the newly selected local consumer suites for #1711/#1672/#1656, record passed count and wall time, and verify none is file-level gated in the targeted lane.
- [x] 4.3 Run selector CLI probes and record input -> expected membership for every AC: `state_manager.py`/`state_cli.py` -> all core smoke; four future-shaped invariant-root paths -> write-site suite; hydro-display -> derived direct ∪ one-hop closure; each mapping-builder module -> eight package suites; hook -> cutover-hook suite; node-22 script -> both recalibration suites; MVT -> existing exact pin.
- [x] 4.4 Run representative before/after lane-cost measurements for `packages/common/state_manager.py`, `forecast_store.py`, hydro-display, mapping-builder, and the Timescale supplemental suite; confirm budget remains far below the 35-minute targeted lane timeout.
- [x] 4.5 `uv run ruff check .` and `openspec validate ci-gate-routing-closure-batch --strict --no-interactive` pass.
- [x] 4.6 Run the CI test selector against the final PR changed-file set and verify it executes assertion-level tests rather than a zero-assertion collapse.

## 5. Remote/CI evidence and traceability

- [ ] 5.1 PR `Unit Tests` and required checks pass at the frozen final SHA.
- [ ] 5.2 On the PR, `changes.database == true` and `SQL Migration Dry Run` executes. Capture the run/job URL and its `-vv -rs` log excerpt listing all seven `tests/test_display_coverage_residual_debt_integration.py::*` node IDs as PASSED with none SKIPPED; quiet run status alone is insufficient. (#1688)
- [ ] 5.3 After merge, capture the master push `SQL Migration Dry Run` receipt with the same seven PASSED node IDs. If the merged batch diff cannot isolate `forecast_store.py`, record that limitation while retaining the mechanical source-pattern mutation proof; never claim an isolated trigger experiment that was not run.
- [ ] 5.4 PR body closes #1711 #1672 #1656 #1688 #1744 and #1597, explicitly stating #1597 was already delivered by PR #1670; record all plan deviations, runtime budgets, and in-scope integration-source dispositions.

## Evidence Floor

- Local oracle: selector/meta tests, selected pure-Python suites, mutation red proofs, ruff, strict OpenSpec.
- CI oracle: PR targeted Unit Tests plus real PostgreSQL `SQL Migration Dry Run` receipt.
- No node-22 or node-27 live receipt: no production/runtime/database-schema behavior changes; the existing GitHub service-container integration lane is the requirement-owned oracle for #1688.
- Must preserve: all existing selector tests, stop rules, unknown-backend behavior, MVT closure from #1597, test/spec/CI oracle strength, and all non-database workflow filters/jobs.
- Non-goals: production logic, 210-entry `packages/common` disposition path A, DSN redesign, remote deployment, suite assertion edits.
