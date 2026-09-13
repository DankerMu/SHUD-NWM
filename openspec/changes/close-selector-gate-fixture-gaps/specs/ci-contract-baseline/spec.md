## MODIFIED Requirements

### Requirement: precip service tree changes MUST select the prewarm reader suite

`scripts/node27_mvt_prewarm.py` imports `services.precip.mirror.horizon_valid_times` at module level and `tests/test_node27_mvt_prewarm.py` imports that script at module level, so the prewarm suite is a one-hop importer suite of the `services/precip/**` tree whose valid-time grid assertions discriminate on `PRECIP_STEP_HOURS`. `scripts/node27_raw_retention.py` likewise imports `services.precip.constants.FILE_CACHE_DIR_ENV` at module level and `tests/test_node27_raw_retention.py` imports that script at module level while holding the env name as a bare literal, so a consistent rename that updates the in-lane `tests/test_precip_overlay.py` pin would otherwise stay green in the PR lane and red only on master. The `services/precip/**` `PathTestRule` in `scripts/select_ci_tests.py` SHALL target `tests/test_node27_mvt_prewarm.py` and `tests/test_node27_raw_retention.py` in addition to the four `PRECIP_SURFACE_TESTS` suites, by widening that rule's own target tuple only (neither `stop_on_match` nor `only_when_any_changed`). The shared `PRECIP_SURFACE_TESTS` tuple and the `apps/api/routes/precip.py` rule SHALL remain unchanged, so a route-only diff SHALL NOT select the prewarm or raw-retention suite. `tests/test_select_ci_tests.py` SHALL pin both outcomes with explicit literal expected lists rather than by referencing `PRECIP_SURFACE_TESTS`. Both pinned selections additionally carry `tests/test_river_segment_write_surface_scan.py`, contributed by the supplemental tree-scanning route under the requirement "Tree-scanning invariant suites MUST follow every scanned source root"; that target is named in each exact list below rather than treated as drift, and it is not routed by this requirement's rule.

#### Scenario: a precip tree module diff selects the prewarm and raw-retention reader suites

- **WHEN** the changed paths are exactly `services/precip/constants.py` or exactly `services/precip/mirror.py`
- **THEN** `select_tests` emits exactly `["tests/test_api_contract.py", "tests/test_node27_mvt_prewarm.py", "tests/test_node27_raw_retention.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py", "tests/test_river_segment_write_surface_scan.py"]`

#### Scenario: a precip route-only diff keeps its existing selection

- **WHEN** the changed paths are exactly `apps/api/routes/precip.py`
- **THEN** `select_tests` emits exactly `["tests/test_api.py", "tests/test_api_contract.py", "tests/test_monitoring_api.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py", "tests/test_river_segment_write_surface_scan.py"]`, which contains neither the prewarm nor the raw-retention suite

### Requirement: the precipitation application-composition owners MUST select the precipitation surface oracles

`apps/api/route_registry.py` imports `precip_router` and lists it in `_BUSINESS_ROUTERS`, which `register_role_aware_routes` walks to register every business router; dropping that entry turns both published precipitation endpoints — `/api/v1/precip/{source}/{cycle}/index` and `/api/v1/precip/{source}/{cycle}/{valid_time}.png` — into 404. `apps/api/main.py` calls `_patch_precip_openapi(schema)` inside `_patch_openapi_schema`; dropping that call makes the runtime schema drift from the committed `openapi/nhms.v1.yaml`. Before this change neither owner selected `tests/test_precip_overlay.py` or `tests/test_openapi_drift.py`: the registry selected only the two connection-attribution suites plus the three broad `apps/api/**` suites, and `main.py` only the API error-logging suite plus those same three. Both selections were non-empty and plausible, so the zero-assertion CI warning did not fire and a composition-owner-only diff reached the targeted lane with no precipitation oracle executed. Both owners SHALL therefore reach the precipitation surface oracles, on the terms fixed below.

`scripts/select_ci_tests.py` SHALL route both composition owners to the full set of precipitation surface suites — `tests/test_precip_overlay.py`, `tests/test_openapi_drift.py`, `tests/test_openapi_31_contract.py` and `tests/test_api_contract.py` — while preserving each owner's existing riders: the registry SHALL keep both connection-attribution suites and `main.py` SHALL keep `tests/test_api_errors_logging.py`, and both SHALL keep the three broad `apps/api/**` suites. Because a duplicate pattern splits a module's ownership across two rules, `apps/api/route_registry.py` SHALL be removed from the shared connection-attribution path tuple and given a single path-exact rule whose targets merge both suite sets, exactly as `apps/api/routes/forecast.py` was handled; the comment above that tuple SHALL be corrected so it no longer claims the registry is a member. Neither owner rule SHALL carry `stop_on_match` or `only_when_any_changed`. Both flags are inert for these two entries today: `apps/api/**` is an earlier rule whose three suites have already accumulated by the time these trailing entries are reached, and `only_when_any_changed` is consulted only in the `CHANGED_TEST_FILE_RULES` loop, never for `PATH_TEST_RULES`. The pin is therefore structural rather than behavioural — it keeps a future `stop_on_match` from shadowing a rule appended after these two whose pattern also matches these paths, and keeps a field that would silently do nothing from being added here. The selections of the five route paths remaining in that tuple, of the three store paths in the sibling connection-attribution store tuple, and of `apps/api/routes/precip.py`, `services/precip/cache.py`, `apps/api/openapi_patching.py` and `apps/api/errors.py`, SHALL remain unchanged apart from targets contributed by the supplemental tree-scanning routes under the requirement "Tree-scanning invariant suites MUST follow every scanned source root" — every one of those paths lies under a scanned root, so each now additionally carries `tests/test_river_segment_write_surface_scan.py`. No rule owned by THIS requirement SHALL change to add or remove that target. `services/precip/cache.py` additionally carries the `services/precip/**` tree-rule targets `tests/test_node27_mvt_prewarm.py` and `tests/test_node27_raw_retention.py`, which are owned by the requirement "precip service tree changes MUST select the prewarm reader suite", not by this requirement.

`tests/test_select_ci_tests.py` SHALL pin each owner's selection as an exact set written as literal test-file strings, and SHALL NOT derive the expectation from the production `PRECIP_SURFACE_TESTS` or `CONNECTION_ATTRIBUTION_TESTS` constants, so that an edit to either constant cannot move production and expectation together. It SHALL additionally carry, per owner, a reverse-missing assertion that `tests/test_precip_overlay.py`, `tests/test_openapi_drift.py` and `tests/test_openapi_31_contract.py` disappear when that owner's precipitation targets are removed. The fourth routed suite, `tests/test_api_contract.py`, is deliberately excluded from that assertion: it is also a rider of the broad `apps/api/**` rule, so it survives the removal and an assertion naming all four would fail. `tests/test_river_segment_write_surface_scan.py` is excluded from it for the same reason — it is contributed by a supplemental route the owner rules do not own. The anti-self-certification constraint is that every element of an expected set is a literal string and no value is read back from `scripts/select_ci_tests` — including `PATH_TEST_RULES` and single-suite constants such as `API_ERROR_LOGGING_TEST`; reading `PATH_TEST_RULES` solely to construct the monkeypatched mutant for a reverse-missing assertion is permitted.

#### Scenario: a route-registry diff selects the precipitation oracles and keeps its attribution riders

- **WHEN** the changed paths are exactly `apps/api/route_registry.py`
- **THEN** `select_tests` emits exactly `["tests/test_api.py", "tests/test_api_contract.py", "tests/test_monitoring_api.py", "tests/test_node27_connection_attribution.py", "tests/test_node27_connection_attribution_delegated.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py", "tests/test_river_segment_write_surface_scan.py"]`

#### Scenario: a main.py diff selects the precipitation oracles and keeps its error-logging rider

- **WHEN** the changed paths are exactly `apps/api/main.py`
- **THEN** `select_tests` emits exactly `["tests/test_api.py", "tests/test_api_contract.py", "tests/test_api_errors_logging.py", "tests/test_monitoring_api.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py", "tests/test_river_segment_write_surface_scan.py"]`

#### Scenario: the shared attribution path tuple keeps its other members unchanged

- **WHEN** the changed paths are exactly any one of `apps/api/routes/best_available.py`, `apps/api/routes/data_sources.py`, `apps/api/routes/models.py`, `apps/api/routes/pipeline.py` or `apps/api/routes/state_snapshots.py`
- **THEN** the selection still contains both connection-attribution suites and contains none of `tests/test_precip_overlay.py`, `tests/test_openapi_drift.py` or `tests/test_openapi_31_contract.py` (`tests/test_api_contract.py` is excluded because the broad `apps/api/**` rule supplies it to every path under `apps/api/`)

#### Scenario: neither owner rule carries a selection flag

- **WHEN** the `PATH_TEST_RULES` entries for `apps/api/route_registry.py` and `apps/api/main.py` are inspected
- **THEN** each has `stop_on_match` false and `only_when_any_changed` empty

## ADDED Requirements

### Requirement: the retention copyback mutex load-bearing modules MUST select the mutex suite

`tests/test_retention_copyback_mutex.py` pins the retention copyback mutex, whose wiring lives in `services/orchestrator/scheduler_runtime.py` (the scheduler call site that names the shared copyback root) and whose lock semantics live in `packages/common/copyback_guard.py`. `scripts/select_ci_tests.py` SHALL select the mutex suite for a diff to either module. For `scheduler_runtime.py` the suite SHALL be added at the `stop_on_match` file-journal rule site that matches the path, without editing the shared `FILE_JOURNAL_READ_STATE_TESTS` constant. For `copyback_guard.py` a path-exact rule without `stop_on_match` or `only_when_any_changed` SHALL add the suite so the module's existing selections accumulate unchanged. Selections for `services/orchestrator/retention.py`, `services/orchestrator/cli.py`, `services/orchestrator/__init__.py` and `tests/retention_test_helpers.py` SHALL remain unchanged.

#### Scenario: a scheduler runtime diff selects the mutex suite

- **WHEN** the changed paths are exactly `services/orchestrator/scheduler_runtime.py`
- **THEN** `select_tests` emits exactly `["tests/test_file_orchestration_journal.py", "tests/test_file_orchestration_migration.py", "tests/test_orchestration_chain.py::test_psycopg_active_slurm_jobs_includes_cycle_run_array_job_for_filtered_model", "tests/test_orchestration_chain.py::test_psycopg_active_slurm_jobs_includes_queued_pipeline_rows", "tests/test_orchestration_chain.py::test_psycopg_candidate_state_latest_truth_timestamp_selects_terminal_success", "tests/test_orchestration_chain.py::test_psycopg_candidate_state_limits_jobs_and_reads_events_for_candidate_scope", "tests/test_orchestration_chain.py::test_psycopg_find_forcing_context_populates_package_manifest_metadata", "tests/test_orchestration_chain.py::test_psycopg_has_active_pipeline_includes_queued_pipeline_rows", "tests/test_production_scheduler.py::test_db_free_from_env_raw_invalid_blocks_without_submission", "tests/test_production_scheduler.py::test_db_free_from_env_raw_missing_blocks_canonical_zero_without_submission", "tests/test_production_scheduler.py::test_db_free_from_env_raw_ready_canonical_zero_submits_convert_without_download_source_cycle", "tests/test_production_scheduler.py::test_db_free_injected_collaborators_plan_without_unimplemented_provider_blocker", "tests/test_production_scheduler.py::test_db_free_injected_factory_active_slurm_status_sync_blocks_without_factory_call", "tests/test_production_scheduler.py::test_db_free_injected_factory_cancel_active_slurm_blocks_without_factory_call", "tests/test_production_scheduler.py::test_db_free_injected_factory_ready_candidate_submit_blocks_without_factory_call", "tests/test_production_scheduler.py::test_db_free_journal_write_block_forces_retention_dry_run_before_deletion", "tests/test_production_scheduler.py::test_db_free_scheduler_fake_slurm_submission_writes_file_journal_without_database_url", "tests/test_production_scheduler.py::test_fresh_cycle_with_active_slurm_job_does_not_double_submit", "tests/test_retention_copyback_mutex.py", "tests/test_river_segment_write_surface_scan.py", "tests/test_scheduler_journal_retention_archive.py", "tests/test_scheduler_journal_retention_planning.py", "tests/test_source_cycle_raw_manifest.py"]`, which is the prior 22-entry selection plus `tests/test_retention_copyback_mutex.py`

#### Scenario: a copyback guard diff selects the mutex suite without losing its owners

- **WHEN** the changed paths are exactly `packages/common/copyback_guard.py`
- **THEN** `select_tests` emits exactly `["tests/test_api.py", "tests/test_copyback_guard.py", "tests/test_gateway.py", "tests/test_migrations.py", "tests/test_orchestration_chain.py", "tests/test_production_scheduler.py", "tests/test_retention_copyback_mutex.py", "tests/test_river_segment_write_surface_scan.py", "tests/test_select_ci_tests.py", "tests/test_timescale_write_guard_wire_site_invariant.py"]`

### Requirement: path routing rules MUST NOT carry an activation gate

`PathTestRule.only_when_any_changed` is honoured only by the `CHANGED_TEST_FILE_RULES` loop; the `PATH_TEST_RULES` and `SUPPORT_MODULE_TEST_RULES` loops never consult it, so a gate set there would be silently inert. `tests/test_select_ci_tests.py` SHALL assert at table level that no `PATH_TEST_RULES` row (and, as already asserted, no `SUPPORT_MODULE_TEST_RULES` row) sets `only_when_any_changed`, and the field's declaration and `_rule_activated` SHALL document that scope. Selection behaviour SHALL NOT change.

#### Scenario: a gated path rule is rejected by name

- **WHEN** any `PATH_TEST_RULES` row, for example `services/precip/**`, is given a non-empty `only_when_any_changed`
- **THEN** the table-level guard fails and names that row's pattern

### Requirement: the supplemental invariant-root meta-guard MUST observe routing, not only the constant

The #1656 root-drop mutant test SHALL, after replacing `TIMESCALE_WRITE_GUARD_INVARIANT_ROOTS` with a set lacking `scripts/**`, assert through `select_tests` that a new `scripts/` source no longer selects the invariant suite while a new `workers/` source still does, so removing the replacement fails the test. The scan-root derivation SHALL require exactly one top-level `return` in the invariant suite's `_scan_roots` and fail by name otherwise.

#### Scenario: the root-drop mutant is observed through the selector

- **WHEN** the supplemental roots exclude `scripts/**`
- **THEN** `select_tests(["scripts/brand_new_thing.py"])` excludes the invariant suite and `select_tests(["workers/brand_new_thing.py"])` includes it

#### Scenario: a second top-level return is rejected

- **WHEN** a copy of `_scan_roots` carries two top-level `return` statements with identical tuples
- **THEN** the derivation fails with the named exactly-one-return assertion

### Requirement: registry partition additions MUST be registered, never backfilled

The #1913 registry-partition guards SHALL compare the partitioned tree against the frozen oracle united with a tracked additions ledger (`tests/fixtures/basins_registry_partition_additions.json`). The frozen oracle SHALL stay byte-identical and every frozen definition SHALL remain byte- and AST-identical. A ledger record SHALL name its issue, a resolvable base commit that is an ancestor of `HEAD` and at whose blob the owner partition exists and does not define the name, one of the six partitions other than the retained core `tests/test_basins_registry_import.py` as owner, a name absent from the frozen rows, the pinned definition row, and its collected and integration node suffixes. Collection, integration, per-owner count, definition identity and execution-count guards SHALL expect exactly the frozen values plus the registered additions. A diff to the ledger file SHALL select `tests/test_select_ci_tests.py`.

#### Scenario: an unregistered test addition fails by name

- **WHEN** a test function is added to a partition without a ledger record
- **THEN** the definition guard fails naming `<partition>::<name>` and prints the observed row

#### Scenario: a registered addition passes

- **WHEN** the same addition carries a ledger record whose row and node suffixes match the tree
- **THEN** the collection, integration, owner-count and definition guards pass

#### Scenario: a frozen definition cannot be laundered as an addition

- **WHEN** a ledger record names a definition that exists in the frozen rows, or a frozen definition's body is edited
- **THEN** the guards fail
