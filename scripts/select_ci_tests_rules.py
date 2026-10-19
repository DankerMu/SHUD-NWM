"""Rule tables of the CI test selector (`scripts/select_ci_tests.py`).

`CHANGED_TEST_FILE_RULES`, `SUPPORT_MODULE_TEST_RULES` and `PATH_TEST_RULES`, plus
the single-target constants and patterns that only those tables (and, for four
supplemental guards, `select_tests`) read. Moved out of the facade by
entropy-burndown task 6.3 (#2950): source order and comments kept, except one
stale self line reference now names the `select_tests` branch it meant. The data
names come from `scripts/select_ci_tests_tables.py`; this module never imports
the facade. The facade re-binds the names `select_tests` and the tests read, and
`select_tests` looks the tables up in the facade globals at call time, so tests
keep patching `scripts.select_ci_tests.PATH_TEST_RULES` -- patching this
module's attribute does not steer selection.
"""

from __future__ import annotations

from scripts.select_ci_tests_tables import (
    API_CONTRACT_HELPERS_CONSUMER_TESTS,
    API_CONTRACT_HELPERS_PATH,
    API_CONTRACT_TESTS,
    BASIN_RETIREMENT_HELPERS_PATH,
    BASIN_RETIREMENT_OWNER_PATH,
    BASIN_RETIREMENT_TESTS,
    BASINS_PACKAGE_HELPERS_CONSUMER_TESTS,
    BASINS_PACKAGE_HELPERS_PATH,
    BASINS_PACKAGE_PUBLICATION_TESTS,
    BASINS_REGISTRY_IMPORT_HELPERS_CONSUMER_TESTS,
    BASINS_REGISTRY_IMPORT_HELPERS_PATH,
    BASINS_REGISTRY_IMPORT_TESTS,
    C4_PRODUCTION_ACCEPTANCE_TESTS,
    CALIBRATION_OVERRIDES_CONSUMER_TESTS,
    CALIBRATION_OVERRIDES_PATH,
    CHAIN_IMPORTER_TESTS,
    CONNECTION_ATTRIBUTION_TESTS,
    CORE_SMOKE_TESTS,
    DATA_ADAPTERS_BASE_HELPER_TESTS,
    DATA_ADAPTERS_DIRECTORY_TESTS,
    DIRECT_GRID_CONTRACT_CANONICAL_PATH,
    DIRECT_GRID_CONTRACT_IMPORTER_TESTS,
    DIRECT_GRID_DISPLAY_CUTOVER_FLIP_HELPERS_PATH,
    DIRECT_GRID_DISPLAY_CUTOVER_FLIP_TESTS,
    DIRECT_GRID_SURFACE_TESTS,
    ENTROPY_AUDIT_HELPERS_PATH,
    ENTROPY_AUDIT_OWNER_PATH,
    ENTROPY_AUDIT_TESTS,
    FILE_JOURNAL_READ_STATE_TESTS,
    FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS,
    FLOATING_RESOLVE_SCRIPT_PATH,
    FLOATING_RESOLVE_WORKFLOW_PATH,
    FORCED_RESUBMIT_SURFACE_TESTS,
    FORCING_SQL_SHAPE_ORACLE_TESTS,
    FULL_REGRESSION_SHARD_DURATIONS_PATH,
    FULL_REGRESSION_SHARD_SCRIPT_PATH,
    FULL_REGRESSION_WATCH_SCRIPT_PATH,
    FULL_REGRESSION_WATCH_WORKFLOW_PATH,
    GOVERNANCE_WORKFLOW_PATH,
    HYDRO_DISPLAY_MVT_SCALING_FACADE_TESTS,
    HYDRO_DISPLAY_MVT_SCALING_TESTS,
    HYDROLOGIC_FINGERPRINT_CONTRACT_PATH,
    JOURNAL_RETENTION_TESTS,
    LINE_REFERENCE_BASELINE_PATH,
    LOOP_LOG_AUDIT_PATH,
    LOOP_LOG_PATH,
    MAPPING_BUILDER_TESTS,
    MERGED_REGISTRY_PUBLISH_HELPERS_PATH,
    MERGED_REGISTRY_PUBLISH_OWNER_PATH,
    MERGED_REGISTRY_PUBLISH_TESTS,
    MERGED_REGISTRY_PUBLISH_TOOL_TESTS,
    MODEL_REGISTRY_IMPORTER_TESTS,
    MODEL_SUCCESSION_HELPERS_PATH,
    MODEL_SUCCESSION_OWNER_PATH,
    MODEL_SUCCESSION_TESTS,
    NODE22_CLONE_CUTOVER_STATES_TESTS,
    NODE22_ENTRYPOINT_HELPERS_PATH,
    NODE22_ENTRYPOINT_INVARIANT_TESTS,
    NODE22_REFRESH_TIMER_HEALTH_ENV_TEMPLATE_TESTS,
    NODE22_REFRESH_TIMER_HEALTH_HELPERS_PATH,
    NODE22_REFRESH_TIMER_HEALTH_OWNER_PATH,
    NODE22_REFRESH_TIMER_HEALTH_TESTS,
    NODE27_MVT_CACHE_RETENTION_HELPERS_PATH,
    NODE27_MVT_CACHE_RETENTION_TESTS,
    NODE27_PGDATA_WORKLOAD_TESTS,
    OPENAPI_CONTRACT_TESTS,
    ORCHESTRATOR_CLI_IMPORTER_TESTS,
    ORCHESTRATOR_MANIFEST_SURFACE_TESTS,
    PRECIP_SURFACE_TESTS,
    PRODUCTION_OPS_RUNBOOK_HELPERS_PATH,
    PRODUCTION_OPS_RUNBOOK_TESTS,
    PROVISION_DIRECT_GRID_DRY_RUN_TEST,
    PROVISION_DIRECT_GRID_SCRIPT_TESTS,
    PROVISION_OUTPUT_REGISTRY_PARENT_TEST,
    PUBLISH_SCHEDULER_REGISTRY_HELPERS_PATH,
    PUBLISH_SCHEDULER_REGISTRY_TESTS,
    PYTHON_ENVIRONMENT_TRUTH_TEST,
    QHH_PRODUCTION_BOOTSTRAP_HELPERS_PATH,
    QHH_PRODUCTION_BOOTSTRAP_TESTS,
    READONLY_DB_VALIDATION_TESTS,
    RECALIBRATION_CLI_FIXTURES_TESTS,
    REGION_CONTRACT_PATH,
    RELEASED_RESERVATION_RECOVERY_TESTS,
    RESPONSE_MODEL_PRESERVATION_TESTS,
    RETENTION_COPYBACK_MUTEX_HELPERS_PATH,
    RETENTION_COPYBACK_MUTEX_OWNER_PATH,
    RETENTION_COPYBACK_MUTEX_OWNER_TESTS,
    RETENTION_COPYBACK_MUTEX_TESTS,
    REVIEW_GATE_CLI_PATH,
    REVIEW_GATE_CLI_TEST,
    REVIEW_GATE_ISSUE_MEMORY_CONSUMER_TESTS,
    REVIEW_GATE_ISSUE_MEMORY_PATH,
    REVIEW_GATE_ISSUE_MEMORY_TEST,
    RIVER_TS_READ_PATH_SURROGATE_KEYS_INTEGRATION_TESTS,
    SCHEDULER_IMPORTER_TESTS,
    SCHEDULER_REFRESH_DEPLOYMENT_TESTS,
    SCHEDULER_REFRESH_HELPER_TESTS,
    SCHEDULER_REFRESH_HELPERS_PATH,
    SCHEDULER_REFRESH_INSTALLER_HARNESS_PATH,
    SCHEDULER_REFRESH_INSTALLER_HARNESS_TESTS,
    SCHEDULER_REFRESH_INSTALLER_TESTS,
    SCHEDULER_REFRESH_OWNER_PATH,
    SCHEDULER_REFRESH_RECEIPT_HELPER_TESTS,
    SCHEDULER_REFRESH_RECEIPT_HELPERS_PATH,
    SCHEDULER_REFRESH_RUNNER_TESTS,
    SCHEDULER_REFRESH_TESTS,
    SELECTOR_META_GUARD_TEST,
    SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST,
    SQL_SHAPE_ORACLE_TESTS,
    STATE_CLONE_HOOK_TESTS,
    STATE_INDEX_COPYBACK_REPLAY_HELPERS_PATH,
    STATE_INDEX_COPYBACK_REPLAY_OWNER_PATH,
    STATE_INDEX_COPYBACK_REPLAY_TESTS,
    SUCCESSION_RECEIPT_TESTS,
    THREAD_EXCEPTION_POLICY_TESTS,
    TIME_NORMALIZATION_CONTRACT_PATH,
    PathTestRule,
)

# #2920: the import layering guard. It AST-parses every Python file under
# packages/ and workers/ and fails on an upward import (packages -> workers/
# services/apps/scripts, workers -> services/apps) that
# tests/import_layering_allowlist.json does not name, and on a stale allowlist
# entry. A new upward import anywhere under those roots must red the MERGE GATE,
# so every Python path the guard reads routes to it, SUPPLEMENTALLY in the shape
# #1656/#2185/#1627 established: no `matched`, no stop-rule participation, so a
# `stop_on_match` owner of the changed path cannot shadow it.
IMPORT_LAYERING_GUARD_TEST = "tests/test_import_layering_guard.py"

# #2937 (entropy-burndown task 4.2): the `Any` annotation ratchet. It counts
# `Any` inside annotations of every Python file under the five production roots
# and fails above tests/any_ratchet_ceiling.json's ceiling or more than 5 %
# below it. Any Python change under those roots can move the count, so every
# such path routes to it SUPPLEMENTALLY in the #1656/#2185/#1627/#2920 shape:
# no `matched`, no stop-rule participation, so a `stop_on_match` owner of the
# changed path cannot shadow it.
ANY_RATCHET_TEST = "tests/test_any_ratchet.py"

# entropy-burndown task 2.1a: the status-vocabulary parity suite. It imports and
# pins the canonical status-set objects of modules across the five production
# roots (and task 2.1b adds a duplicate-literal scan over the same roots), so
# every Python change there routes to it SUPPLEMENTALLY in the #1656 shape: no
# `matched`, no stop-rule participation, so a `stop_on_match` owner of the
# changed path cannot shadow it.
STATUS_VOCAB_PARITY_TEST = "tests/test_status_vocab_parity.py"

# entropy-burndown task 3.1: the time/identity helper consolidation ceiling. It
# counts module-level local re-definitions of the canonical time/identity helpers
# (`_utcnow`, `_parse_time`, `_format_time`, `_normalize_source_id`, ...) in every
# Python file under the five production roots and fails above
# tests/time_helper_ceiling.json's ceiling or more than 5 % below it. Any Python
# change under those roots can move the count, so every such path routes to it
# SUPPLEMENTALLY in the #1656 shape: no `matched`, no stop-rule participation, so
# a `stop_on_match` owner of the changed path cannot shadow it.
TIME_HELPER_CONSOLIDATION_TEST = "tests/test_time_helper_consolidation.py"

SQLALCHEMY_URL_HELPER_TEST = "tests/test_sqlalchemy_url.py"
# #1571: the two-node Docker runbook is the current deployment-docs entry whose
# repo-Python commands must use the exact checkout interpreter; its suite is
# not same-name derivable. The producer is `infra/**`, which opens the backend
# lane, but without a rule a runbook-only PR would select nothing and drop to
# collect-only.
TWO_NODE_DOCKER_RUNBOOK_ENV_TEST = "tests/test_two_node_docker_runbook_environment_invariant.py"
# #1571: the QHH diagnostic README and its Slurm sbatch wrapper are two current
# producers that previously neither started the backend lane nor selected their
# QHH-static owner. Exact ci.yml paths now start the lane; these rules attach
# its assertions. The existing `scripts/run_qhh_backend_smoke.sh` rule routes to
# tests/test_qhh_scripts_static.py, so these two exact producers share the
# same owner via the same additive (non-stop) rule shape.
QHH_DIAGNOSTIC_README = "scripts/diagnostic/qhh/README.md"
QHH_CYCLE_SBATCH = "scripts/run_qhh_cycle.sbatch"
NODE22_SLURM_GATEWAY_UNIT = "infra/systemd/nhms-slurm-gateway.service"
NODE22_RETENTION_UNIT = "infra/systemd/nhms-scheduler-evidence-retention.service"
NODE22_JOURNAL_RETENTION_SERVICE = "infra/systemd/nhms-scheduler-journal-retention.service"
NODE22_JOURNAL_RETENTION_TIMER = "infra/systemd/nhms-scheduler-journal-retention.timer"
# #2653: the state-index capacity watch (a prune-retention dry-run wrapper) and
# its two units. The watch suite drives the wrapper end to end; the node-22
# invariant owner pins both units' exact fields and scans them.
NODE22_STATE_INDEX_CAPACITY_SERVICE = "infra/systemd/nhms-scheduler-state-index-capacity.service"
NODE22_STATE_INDEX_CAPACITY_TIMER = "infra/systemd/nhms-scheduler-state-index-capacity.timer"
NODE22_STATE_INDEX_CAPACITY_WATCH_TEST = "tests/test_node22_state_index_capacity_watch.py"
NODE22_REPAIR_SCRIPT = "scripts/ops/node22_repair_placeholder_hydro_uris.py"
LOOP_LOG_AUDIT_TEST = "tests/test_loop_log_audit_attribution.py"
LINE_REFERENCE_TEST = "tests/test_entropy_audit_line_references.py"

# #2573: the CI install contract (every pytest job installs from uv.lock with
# `uv sync --locked`, PATH only via $GITHUB_PATH) and the eccodes-then-pyproj
# single-PROJ regression it exists for. Both re-prove on a ci.yml or
# dependency change; the isolation test also on pyproject.toml / uv.lock.
CI_LOCKED_INSTALL_TEST = "tests/test_ci_workflow_locked_install.py"
NATIVE_PROJ_ISOLATION_TEST = "tests/test_native_proj_isolation.py"
FULL_REGRESSION_WATCH_TEST = "tests/test_full_regression_watch.py"
FULL_REGRESSION_SHARD_TEST = "tests/test_ci_shard_tests.py"
FLOATING_RESOLVE_TEST = "tests/test_floating_resolve_report.py"
GOVERNANCE_HARD_GATE_WORKFLOW_TEST = "tests/test_governance_workflow_hard_gate.py"

# #1684 shared-auth owner-to-focused-suite mappings (EVID-01). These shared
# modules have no same-name derivable suite and their consumers are the focused
# partitioned Slurm auth suites, not the broad core-smoke/API/orchestrator
# suites: an owner-only PR previously selected only generic riders and none of
# the focused contracts. Each is an exact additive (non-stop) rule so existing
# intentional supplemental routing (core-smoke baseline for packages/common/**,
# #1656 timescale rider) survives.
#
# `packages/common/auth_policy.py` owns the canonical RBAC action matrix and is
# asserted by the dedicated matrix suite; `packages/common/request_auth.py` owns
# the service-token contract (reader/matcher/client/preflight) and
# `packages/common/openapi_auth_security.py` owns the published scheme/security
# metadata; `apps/api/auth.py` is the facade whose drift is caught by the
# shared-auth contract suites plus the role-boundary static suite; the two
# orchestrator modules (client + scheduler preflight) are owned by their
# focused auth-client/deployment suites.
AUTH_POLICY_TEST = "tests/test_auth_policy_matrix.py"
SLURM_AUTH_CLIENT_TEST = "tests/test_slurm_gateway_auth_client.py"
SLURM_AUTH_DEPLOYMENT_TEST = "tests/test_slurm_gateway_auth_deployment.py"
SLURM_AUTH_CORE_TEST = "tests/test_slurm_gateway_auth.py"
# #1684 EVID-02 partition: the full compute/dev mount matrix lives in its own
# module (the core suite is at the repo 1,000-line limit); every owner that
# selects the core suite must select the partition too.
SLURM_AUTH_FULLMOUNT_TEST = "tests/test_slurm_gateway_auth_fullmount.py"
SLURM_OPENAPI_SECURITY_TEST = "tests/test_slurm_gateway_openapi_security.py"
PRODUCTION_OPS_SUBRUNBOOK_GLOB = "docs/runbooks/production-ops/**"

# The literal rule rows are declared in PATH_TEST_RULES;
# these constants are the single names the selector meta-suite pins.


ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS: tuple[str, ...] = (
    "services/orchestrator/chain_types.py",
    "services/orchestrator/chain_manifests.py",
    "services/orchestrator/chain.py",
    "services/orchestrator/scheduler.py",
)

DIRECT_GRID_SURFACE_PATH_PATTERNS: tuple[str, ...] = ("openspec/changes/direct-grid-forcing/**",)

FILE_JOURNAL_READ_STATE_PATH_PATTERNS: tuple[str, ...] = (
    "packages/common/safe_fs.py",
    "services/orchestrator/chain_repository_state.py",
    "services/orchestrator/file_orchestration_journal.py",
    "services/orchestrator/file_orchestration_migration.py",
    "services/orchestrator/scheduler_journal_archive.py",
    "services/orchestrator/scheduler_journal_restore.py",
    "services/orchestrator/scheduler_journal_retention.py",
    "services/orchestrator/scheduler_journal_retention_types.py",
    "services/orchestrator/cli.py",
    "services/orchestrator/scheduler.py",
    "services/orchestrator/scheduler_core.py",
    "services/orchestrator/scheduler_runtime.py",
)


CHANGED_TEST_FILE_RULES: tuple[PathTestRule, ...] = (
    PathTestRule(
        "tests/test_sql_shape_helpers.py",
        SQL_SHAPE_ORACLE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_c4_production_acceptance.py",
        C4_PRODUCTION_ACCEPTANCE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_c4_production_acceptance_boundaries.py",
        C4_PRODUCTION_ACCEPTANCE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_pgdata_workload.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_pgdata_workload_plan.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_pgdata_workload_io.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_pgdata_workload_server_identity.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),

    PathTestRule(
        "tests/test_orchestration_chain.py",
        FILE_JOURNAL_READ_STATE_TESTS,
        only_when_any_changed=FILE_JOURNAL_READ_STATE_PATH_PATTERNS,
    ),
    PathTestRule(
        "tests/test_production_scheduler.py",
        FILE_JOURNAL_READ_STATE_TESTS,
        only_when_any_changed=FILE_JOURNAL_READ_STATE_PATH_PATTERNS,
    ),
    PathTestRule(
        "tests/test_orchestration_chain.py",
        ORCHESTRATOR_MANIFEST_SURFACE_TESTS,
        stop_on_match=True,
        only_when_any_changed=ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS,
    ),
    PathTestRule(
        "tests/test_production_scheduler.py",
        ORCHESTRATOR_MANIFEST_SURFACE_TESTS,
        stop_on_match=True,
        only_when_any_changed=ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS,
    ),
)


# Support modules under `tests/` (fixtures, helpers, fakes) are not collectible,
# so they map to the meta-guard suite plus ci.yml's full-tree collect-only smoke
# — import/syntax only, zero assertions (#1453/#1454). For a support module that
# real suites import at file level, that lane is blind to exactly the breakage a
# fixture edit causes, so #1487 routes such a module to its non-gated top-level
# importer suites instead. Exact paths, no globs: the rule table is closed
# against the tracked importer tree by tests/test_select_ci_tests.py (required
# sets are derived, never frozen), so a new importer suite reddens naming the
# module and missing suite. `tests/integration_helpers.py` remains deliberately
# absent under issue #1487's measured partial-coverage carve-out;
# `tests/conftest.py` left that carve-out in #1571 because its skip-guidance
# contract also requires the node-22 invariant owner.
# #1442 note: `tests/integration_helpers.py` also owns a statement registered in
# tests/test_river_ts_text_identity_cleanup.py, so a diff to it should ideally
# select that oracle. It does not, because of the carve-out above: the file maps
# to the meta-guard suite only. The oracle still guards it on every OTHER path —
# it is a consumer of tests/test_sql_shape_helpers.py, so the SQL_SHAPE_ORACLE
# rule runs it on a helper diff, and it self-selects on its own diff. Closing the
# gap belongs to #1487's carve-out, not here.
SUPPORT_MODULE_TEST_RULES: tuple[PathTestRule, ...] = (
    PathTestRule(
        # #1571 local-repair: tests/conftest.py is a non-collectible support
        # module, so without a SUPPORT_MODULE_TEST_RULES entry it collapses to
        # the meta-guard only. It has two file-level non-gated importer suites
        # (tests/test_integration_gate.py, tests/test_grid_stability_verification.py)
        # a fixture edit breaks, and the #1487 carve-out's exact skip-guidance
        # clause is asserted by the node-22 owner (test_conftest_skip_guidance_
        # points_to_runbook). This rule is reached through the `tests/**`
        # changed-test branch — BEFORE PATH_TEST_RULES — so it preserves the
        # selector meta-guard rider and adds the node-22 owner. Deliberately
        # NOT a PATH_TEST_RULES row: the `tests/**` branch handles conftest and
        # a PATH row would be dead. The `database:`-filter carve-out remains
        # recorded and pinned elsewhere; this routing is additive to it.
        "tests/conftest.py",
        (
            # #2710: a literal-path consumer — it asserts the shard collector
            # never lists this file as a test file.
            FULL_REGRESSION_SHARD_TEST,
            # #2594: the grib opt-in preflight's pin imports this module to
            # drive `pytest_collection_finish` with a replaced ecCodes probe.
            "tests/test_conftest_grib_preflight.py",
            "tests/test_grid_stability_verification.py",
            "tests/test_integration_gate.py",
            "tests/test_node27_docker_collection_gate.py",
            *NODE22_ENTRYPOINT_INVARIANT_TESTS,
        ),
    ),
    PathTestRule(
        "tests/fixtures/mapping_builder/in_memory_grid_snapshot.py",
        (
            "tests/test_mapping_builder_algorithm.py",
            "tests/test_mapping_builder_binding.py",
            "tests/test_mapping_builder_cli.py",
            "tests/test_mapping_builder_evidence.py",
            "tests/test_mapping_builder_integration.py",
            PROVISION_DIRECT_GRID_DRY_RUN_TEST,
        ),
    ),
    PathTestRule(
        "tests/slurm_template_helpers.py",
        (
            "tests/test_production_slurm_validation.py",
            "tests/test_slurm_array_contract.py",
        ),
    ),
    PathTestRule(
        # The recalibration carry-over package fixtures, fakes AND the
        # independent fingerprint oracle (#1697). The oracle is why this rule
        # matters more than a fixture-builder rule usually does: it re-implements
        # the documented hash format from the fixture bytes, so a change here can
        # flip the gate suites from "gate proven" to "gate agreeing with itself"
        # without touching a line of production code. After the CLI suite split
        # this rule lists the recalibration core suite plus BOTH recalibration
        # CLI modules (the end-to-end and the validation split); the baseline
        # CLI suite ALSO top-level-imports `_write_package`, the calibration
        # constants and `_IC_V1`/`_PARA_V1`, so it is a fourth direct consumer
        # and is listed here too. All are sub-second.
        "tests/state_clone_recalibration_fixtures.py",
        (
            "tests/test_state_clone_recalibration.py",
            "tests/test_state_clone_recalibration_cli.py",
            "tests/test_state_clone_recalibration_cli_validation.py",
            "tests/test_state_clone_baseline_cutover_cli.py",
            # #2739: the model succession support module writes its old and new
            # packages with `_write_package` and the calibration constants.
            *MODEL_SUCCESSION_TESTS,
        ),
    ),
    PathTestRule(
        # The CLI environment helpers shared by both recalibration CLI modules
        # (extracted at the §6.8 split). A change here can silently alter what
        # either module's dispatch/apply tests build, so both consumers must run;
        # the suite names are not same-name derivable, hence the explicit route.
        "tests/state_clone_recalibration_cli_fixtures.py",
        RECALIBRATION_CLI_FIXTURES_TESTS,
    ),
    PathTestRule(
        # The #1735 lineage index builders: every lineage suite publishes REAL
        # index entries through `publish_state_snapshot_index`, so a change to
        # the builder shape (clone provenance pass-through, `usable_flag`)
        # silently changes what the resolver reads. `test_scheduler_generation.
        # py` and `test_state_manager_generation_history.py` import the builders
        # inside a function body, so the derived non-gated closure (which sees
        # module-level imports only) does not require them — but they are routed
        # anyway: the closure is a FLOOR, not a ceiling, and this PR's own fix
        # changed `index_entry`'s signature (a keyword-only `usable_flag`), which
        # is exactly the class of change a function-body caller breaks on while
        # the floor stays green. Cost: +12.2s for the two, against a fixture
        # whose whole purpose is to be the shared index-entry shape.
        "tests/lineage_state_index_fixtures.py",
        (
            "tests/test_scheduler_backfill.py",
            "tests/test_scheduler_lineage.py",
            "tests/test_scheduler_generation.py",
            "tests/test_state_manager_generation_history.py",
        ),
    ),
    PathTestRule(
        # #2074: the single home of every double the nine hydro-display MVT
        # partitions share. Without this entry the module is a non-collectible
        # `tests/` support module, so a fixture-only diff would collapse to the
        # selector meta-guard and none of the 210 cases that depend on these
        # fakes would run. The fakes are not passive: `_dual_patch` decides
        # WHETHER a patch bites at all (both the facade and
        # `hydro_display_catalog` homes, #2026), `_NationalRouteSession`
        # classifies statements by SQL landmark and so decides which branch each
        # route case exercises, and `_TILE_ROUTE_LOGGER` is the literal logger
        # name the `#2030` negative caplog assertions filter on — a typo there
        # makes them pass vacuously. Every partition imports it at module scope,
        # so the routed set IS the derived closure.
        "tests/hydro_display_mvt_helpers.py",
        HYDRO_DISPLAY_MVT_SCALING_TESTS,
    ),
    PathTestRule(
        # #2527: the station-flag flip harness both flip partitions share -- the
        # `met.met_station` fake, the fake cursor / transaction, the
        # `_FlipHarnessStore` that drives the real lifecycle path, the audit
        # recorder and the model / mirror factories. Both partitions import it at
        # module scope, so the routed set IS the derived closure.
        DIRECT_GRID_DISPLAY_CUTOVER_FLIP_HELPERS_PATH,
        DIRECT_GRID_DISPLAY_CUTOVER_FLIP_TESTS,
    ),
    PathTestRule(
        # #2074: the single home of the six mock stores, the retry gateway double
        # and the eight private assertion helpers the three API-contract
        # partitions share. Without this entry the module is a non-collectible
        # `tests/` support module, so a doubles-only diff would collapse to the
        # selector meta-guard and none of the 38 cases that depend on these stubs
        # would run. The stubs decide what the assertions see: `_RunStore` and
        # `_ModelRegistryStore` are the response shapes three suites compare
        # against the published schemas, `_assert_success_envelope` is what makes
        # the envelope claim at all, and `_bounded_qhh_latest_reflected_value` is
        # the reflected-value bound the QHH-latest identity cases hang on. The
        # routed set is the derived closure: the three partitions plus
        # `tests/test_openapi_response_conformance.py`, whose module-scope
        # `from tests.api_contract_helpers import _ModelRegistryStore, _RunStore`
        # is the fourth edge.
        API_CONTRACT_HELPERS_PATH,
        API_CONTRACT_HELPERS_CONSUMER_TESTS,
    ),
    PathTestRule(
        # Pins the modes `provider_atomic`'s two fail-closed gates inspect, for
        # tests that PRE-create a lock parent or a provider destination (#1513).
        # Its whole purpose is to make those tests independent of the ambient
        # umask, so the breakage it prevents is invisible on the umask-0022 CI
        # runner and shows up only on node-27 (umask 0002) -- exactly the class
        # the meta-guard collapse cannot catch, since import/syntax succeeds
        # either way.
        "tests/provider_mode_helpers.py",
        (
            "tests/test_production_scheduler.py",
            "tests/test_scheduler_registry_capacity.py",
            # #1943: the journal-root authority suite builds its symlinked and
            # mode-sensitive journal roots through
            # `make_directory_with_explicit_mode`, so a helper change moves what
            # its refusal cases actually construct.
            "tests/test_scheduler_journal_root_authority.py",
            # #1101: four of the fifteen refresh partitions import this helper at
            # module scope (the provider-atomic, terminability-probe,
            # worker-mirror and predicate suites). The other eleven reach it
            # only through `tests/scheduler_refresh_helpers.py`, and the
            # support-module closure guard derives DIRECT importers only, so
            # listing them here would be routing noise the guard cannot justify
            # -- the helper's own rule below carries them instead.
            "tests/test_scheduler_refresh_predicates_and_dry_run_reconciliation.py",
            "tests/test_scheduler_refresh_provider_atomic.py",
            "tests/test_scheduler_refresh_terminability_probes.py",
            "tests/test_scheduler_refresh_worker_mirror_transactions.py",
            "tests/test_scheduler_state_index_repair.py",
            "tests/test_state_manager.py",
            "tests/test_run_tree_copyback.py",
            "tests/test_source_cycle_raw_manifest.py",
            # #2403: the db-free unreadable-index lineage test pre-creates the
            # state-index lock parent and its corrupt destination through this
            # helper, so a helper change moves what that publish gate sees.
            "tests/test_scheduler_backfill.py",
            # #1102: two of the seven publisher partitions import this helper at
            # module scope — the manual-CLI one pre-creates a provider
            # destination and a mode-sensitive parent, the manifest-audit one
            # builds its registry destination through
            # `make_directory_with_explicit_mode`. The other five never open a
            # gated surface, so listing them would be routing noise.
            "tests/test_publish_registry_manifest_audit.py",
            "tests/test_publish_registry_manual_cli.py",
            # #2738: the merged-registry publish partitions build both manifest
            # directories and the refresh-lock parent through
            # `make_directory_with_explicit_mode`, in the `workspace` fixture
            # of their shared support module.
            *MERGED_REGISTRY_PUBLISH_TESTS,
            # #2739: the model succession `space` fixture pre-creates the
            # manifest, state-index and refresh-lock parents the same way.
            *MODEL_SUCCESSION_TESTS,
            # #2614 / #2548: both pre-create the state-index lock parent through
            # `make_directory_with_explicit_mode`.
            "tests/test_state_index_upsert_cost.py",
            "tests/test_state_index_retention.py",
        ),
    ),
    PathTestRule(
        # #1611: the four replay partitions share their whole fixture surface
        # through this helper (the `fixture` / `private_umask_fixture` factories,
        # the alias-identity and fsync-failure seams, the index-entry builder).
        # Each partition imports it at module scope, so all four are its derived
        # importer closure and a helper-only diff must run all four.
        STATE_INDEX_COPYBACK_REPLAY_HELPERS_PATH,
        STATE_INDEX_COPYBACK_REPLAY_TESTS,
    ),
    PathTestRule(
        # #2738: the `workspace` fixture, the autouse no-database fixture and
        # the row / operation builders of the merged-registry publish suite.
        # All three partitions import it at module scope.
        # #2739: the model succession support module builds its two stores on
        # this module's `Workspace` (rows, seed, provision receipt), so the
        # succession partitions are in its importer closure.
        MERGED_REGISTRY_PUBLISH_HELPERS_PATH,
        MERGED_REGISTRY_PUBLISH_TOOL_TESTS,
    ),
    PathTestRule(
        # #2739: the `space` fixture (both stores, packages, state rows), the
        # fake `systemctl` with its refresh unit, and the tree comparisons of
        # the model succession suite. All three partitions import it.
        MODEL_SUCCESSION_HELPERS_PATH,
        MODEL_SUCCESSION_TESTS,
    ),
    PathTestRule(
        # #2757: the `space` fixture, the strict fake database, the fake
        # registry store and the fake `systemctl` of the basin retirement
        # suite. All five partitions import it at module scope.
        BASIN_RETIREMENT_HELPERS_PATH,
        BASIN_RETIREMENT_TESTS,
    ),
    PathTestRule(
        # #2532: the node-22 refresh-timer probe suite's shared fakes (the fake
        # `systemctl`, receipt builders, `_run` / `_verdict`), installer harness
        # and runbook section reader. All five partitions import it at module
        # scope, so all five are its derived importer closure.
        NODE22_REFRESH_TIMER_HEALTH_HELPERS_PATH,
        NODE22_REFRESH_TIMER_HEALTH_TESTS,
    ),
    PathTestRule(
        # #1101: the runtime fixture surface of the fifteen refresh partitions --
        # the RefreshConfig factory, both provider-pipeline stubs, the four-lane
        # tracked transaction fixture and the #1080 gate drivers. Eleven of the
        # fifteen partitions import it at module scope; those eleven are its
        # derived importer closure and a helper-only diff must run all of them.
        SCHEDULER_REFRESH_HELPERS_PATH,
        SCHEDULER_REFRESH_HELPER_TESTS,
    ),
    PathTestRule(
        # #1101: the static receipt/classification corpora the same corpus
        # shares, including the ONE `_receipt_schema_validator` factory (the
        # monolith defined that name twice; only the later binding was ever
        # live). Five partitions import no name from it, so its targets are the
        # ten derived importers rather than the whole corpus -- the
        # support-module closure guard derives that set from the tracked tree, so
        # an eleventh importer reddens there instead of rotting.
        SCHEDULER_REFRESH_RECEIPT_HELPERS_PATH,
        SCHEDULER_REFRESH_RECEIPT_HELPER_TESTS,
    ),
    PathTestRule(
        # #2294: the refresh installer's fake-`systemctl` harness (real systemd
        # exit codes, the per-call failure/no-op/after injectors, the rig and
        # its snapshot). Both installer suites import it at module scope, so
        # they are its derived importer closure.
        SCHEDULER_REFRESH_INSTALLER_HARNESS_PATH,
        SCHEDULER_REFRESH_INSTALLER_HARNESS_TESTS,
    ),
    PathTestRule(
        # A 0-byte package file with a rule looks wrong until you follow the
        # import: `from tests import X` contributes the base name `tests`, and
        # the repo's derivation authority deliberately aliases a package
        # `__init__.py` to the package itself (tests/test_select_ci_tests.py,
        # _dotted_module_name + test_dotted_module_name_maps_a_package_init_to_the_package).
        # These three suites therefore ARE its derived importers, and a PR that
        # turns this file into a real package surface would otherwise run none of
        # them. Measured 454 passed in 40.18 s locally — inside the lane budget.
        "tests/__init__.py",
        (
            # #2594: `from tests import conftest`, the same edge as the gate suite.
            "tests/test_conftest_grib_preflight.py",
            "tests/test_integration_gate.py",
            "tests/test_node27_docker_collection_gate.py",
            "tests/test_node27_timeseries_compression_capture.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
        ),
    ),
    PathTestRule(
        # A mock SHUD CLI nothing imports: workers/shud_runtime/runtime.py runs it
        # as `[sys.executable, <path>, *args]`, so the consumption edge is the
        # exact literal `"tests/mock_shud_omp.py"` in the consumer's source, not
        # an import. An import-only derivation reads this module as 0-importer and
        # collapses it to the meta-guard, running none of the assertions that
        # depend on the mock's output contract (#1498). The three suites below are
        # its derived literal-path consumers; measured 251 passed in ~27 s for the
        # first two, inside the lane budget. HONEST LIMIT: test_e2e.py's
        # consumption sits inside function-level `@pytest.mark.e2e` tests that
        # auto-skip in the PR lane, so that file contributes ZERO mock assertions
        # there — it is routed because the edge exists in the tree and closure
        # integrity is what the guard derives, not because it executes the mock.
        "tests/mock_shud_omp.py",
        (
            "tests/test_shud_runtime.py",
            "tests/test_direct_grid_e2e.py",
            "tests/test_e2e.py",
        ),
    ),
    PathTestRule(
        # The #1872 retention partition's shared constants/helpers. The five
        # collectible retention partitions that import it do so at module scope
        # (a design requirement: selector importer derivation must see the
        # dependency), so a fixture edit breaks all five during PR-lane
        # collection. They are the derived importer set; the meta-guard rider
        # covers the tree-derived guards this very routing can invalidate.
        # #2238's copyback-mutex partition joined that set: it imports
        # EXTRA_CONFIG/NOW/_seed_cycle/_pass_scheduler from here at top level,
        # so it is a derived importer like the other four, not a rider.
        # 25 tests in 5.27s (median of three runs of the then-single
        # `tests/test_retention_copyback_mutex.py`: 5.27/5.24/5.30s).  The same
        # 25 cases now collect from `tests/test_retention_copyback_mutex_budget.py`
        # plus `tests/test_retention_copyback_mutex_protocol.py`, which is what
        # `RETENTION_COPYBACK_MUTEX_TESTS` below expands to.
        # #2259 split that partition in two and moved its 99-line fixture
        # preamble into this helper, so the derived importer set is SIX, not
        # five, and the helper now also owns the `_forbid_acquisitions` /
        # `_observe_removals` monkeypatch seams. Dropping either partition here
        # reds the support-module closure guard, which derives importers from
        # the tracked tree rather than from this tuple.
        RETENTION_COPYBACK_MUTEX_HELPERS_PATH,
        (
            "tests/test_retention.py",
            *RETENTION_COPYBACK_MUTEX_TESTS,
            "tests/test_retention_extra_roots.py",
            "tests/test_retention_pipeline_frontier.py",
            "tests/test_retention_root_admission.py",
            # #2596: a derived importer too (EXTRA_CONFIG/NOW/_pass_scheduler).
            "tests/test_retention_unexpandable_roots.py",
        ),
    ),
    PathTestRule(
        # Shared journal-retention fixtures are top-level imported by both
        # behavioral partitions, so fixture-only changes must execute each.
        "tests/scheduler_journal_retention_fixtures.py",
        (
            "tests/test_scheduler_journal_retention_planning.py",
            "tests/test_scheduler_journal_retention_archive.py",
        ),
    ),
    PathTestRule(
        # The #1564 split-demote suites' shared fixture module. The four split
        # suites import it at file level, and the public operator-recovery cycle
        # tests import it through a local (function-scope) import, which the
        # derived importer scan does not see — so the rule names all five
        # consumers explicitly. #2668 adds the bind CLI suite as a sixth, file-level
        # consumer (it reuses the secret literals).
        "tests/orchestrator_demote_reserved_job_helpers.py",
        (
            "tests/test_orchestrator_demote_cli_security.py",
            "tests/test_orchestrator_demote_core_cas.py",
            "tests/test_orchestrator_demote_projection_faults.py",
            "tests/test_orchestrator_demote_reclaim_lifecycle.py",
            "tests/test_orchestration_chain.py",
            # #2668: the bind CLI suite imports the demote secret literals at
            # file level.
            "tests/test_orchestrator_bind_reserved_job_cli.py",
        ),
    ),
    PathTestRule(
        # #2668: the bind-reserved-job suites' shared fixture module. The CAS and
        # CLI suites import it at file level; the held-listing suite imports it
        # function-locally (its serialized-anchor test), which the derived
        # importer scan does not see, so the rule names all three.
        "tests/orchestrator_bind_reserved_job_helpers.py",
        (
            "tests/test_orchestrator_bind_reserved_job_cas.py",
            "tests/test_orchestrator_bind_reserved_job_cli.py",
            "tests/test_operator_action_listing_held_reservations.py",
            # #2675: the forcing bind suite imports the held-forcing builder at
            # file level.
            "tests/test_orchestrator_bind_reserved_job_forcing.py",
            # #2682: so does the forcing absence-exit suite.
            "tests/test_orchestrator_demote_reserved_job_forcing.py",
        ),
    ),
    PathTestRule(
        # #2674: the test-only seed for a pre-existing shape (c) row. The guard
        # suite imports it at file level; the journal suite imports it
        # function-locally; every other consumer reaches it through
        # tests/gateway_reconcile_helpers.py `_file_cohort_repository(...,
        # versioned=False)`, which imports it function-locally (a
        # support-to-support edge), either directly or via
        # tests/orchestrator_demote_reserved_job_helpers.py
        # `_axis_repository(..., "legacy")`. The derived importer scan sees only
        # the file-level one, so the rule names every suite that calls
        # `versioned=False` (grep `versioned=False` / `_axis_repository` /
        # `seed_pre_existing_legacy_row` under tests/).
        "tests/file_journal_legacy_seed_helpers.py",
        (
            "tests/test_file_journal_legacy_unversioned_reserved_guard.py",
            "tests/test_file_orchestration_journal.py",
            "tests/test_gateway_reconcile_comment_accounting.py",
            "tests/test_gateway_reconcile_comment_capability.py",
            "tests/test_gateway_reconcile_file_cohort_comment.py",
            "tests/test_gateway_reconcile_inventory.py",
            "tests/test_gateway_reconcile_master_transitions.py",
            "tests/test_gateway_reconcile_writer_receipts.py",
            "tests/test_orchestrator_bind_reserved_job_cas.py",
            "tests/test_orchestrator_demote_core_cas.py",
        ),
    ),
    PathTestRule(
        # #1809: the shared store/cohort/identity fixtures extracted from the
        # gateway-reconcile monolith. The 22 file-level importing partitions are
        # named exactly (derived-set members, all sub-second to import);
        # store_reset is not a consumer — its shells build duck-typed stores and
        # import nothing here. The five demote/chain suites below are the known
        # ultimate consumers reached through the demote helper
        # (tests/orchestrator_demote_reserved_job_helpers.py imports
        # `_file_cohort_repository` from this module at file level; the four
        # split-demote suites import that helper at file level and the public
        # operator-recovery chain suite at function scope) — a
        # support-to-support edge the derived AST scan cannot see, so they are
        # listed explicitly here. tests/test_production_scheduler.py stays
        # excluded on the deliberate 1870-test runtime-budget boundary: its only
        # consumption is a function-local import that would buy a fixture edit
        # the whole suite lane.
        "tests/gateway_reconcile_helpers.py",
        (
            "tests/test_gateway_reconcile_comment_accounting.py",
            "tests/test_gateway_reconcile_comment_capability.py",
            "tests/test_gateway_reconcile_comment_sacct_bounds.py",
            "tests/test_gateway_reconcile_file_cohort_authority.py",
            "tests/test_gateway_reconcile_file_cohort_comment.py",
            "tests/test_gateway_reconcile_file_cohort_identity.py",
            "tests/test_gateway_reconcile_file_cohort_projection.py",
            "tests/test_gateway_reconcile_file_submit_barrier.py",
            "tests/test_gateway_reconcile_grace_guard.py",
            "tests/test_gateway_reconcile_idempotency_barrier.py",
            "tests/test_gateway_reconcile_identity_invariants.py",
            "tests/test_gateway_reconcile_identity_release.py",
            "tests/test_gateway_reconcile_inflight_identity.py",
            "tests/test_gateway_reconcile_inventory.py",
            "tests/test_gateway_reconcile_master_transitions.py",
            "tests/test_gateway_reconcile_reservation_lifecycle.py",
            "tests/test_gateway_reconcile_round10.py",
            "tests/test_gateway_reconcile_writer_launch.py",
            "tests/test_gateway_reconcile_writer_prepare.py",
            "tests/test_gateway_reconcile_writer_quiescence.py",
            "tests/test_gateway_reconcile_writer_receipts.py",
            "tests/test_gateway_reconcile_writer_rollforward.py",
            # #1850: the binding-provenance and claimant-exclusivity suites
            # top-level-import this helper at file level and are sub-second
            # beside the partitions above, so they join the exact rule rather
            # than riding the closure guard as a rule-gap exclusion.
            "tests/test_gateway_reconcile_binding_provenance.py",
            "tests/test_gateway_reconcile_claimant_exclusivity.py",
            # #2655: the SubmitLine exact-key fallback-bind suite top-level-imports
            # this helper the same way; 20 tests in ~1.5s, DB-free.
            "tests/test_gateway_reconcile_submitline_exact.py",
            # The five ultimate consumers via the demote helper (see above).
            "tests/test_orchestrator_demote_cli_security.py",
            "tests/test_orchestrator_demote_core_cas.py",
            "tests/test_orchestrator_demote_projection_faults.py",
            "tests/test_orchestrator_demote_reclaim_lifecycle.py",
            "tests/test_orchestration_chain.py",
            # #2668: the bind CAS suite imports this helper at file level; the
            # CLI and held-listing suites reach it through
            # tests/orchestrator_bind_reserved_job_helpers.py (file level /
            # function-local respectively), a support-to-support edge.
            "tests/test_orchestrator_bind_reserved_job_cas.py",
            "tests/test_orchestrator_bind_reserved_job_cli.py",
            "tests/test_operator_action_listing_held_reservations.py",
            # #2675: the forcing bind suite reaches it the same way (file-level
            # bind helper import) and calls it function-locally.
            "tests/test_orchestrator_bind_reserved_job_forcing.py",
            # #2682: the forcing absence-exit suite reaches it through the same
            # file-level bind helper import.
            "tests/test_orchestrator_demote_reserved_job_forcing.py",
        ),
    ),
    PathTestRule(
        # #1809: the writer/barrier utilities extracted from the monolith. Its
        # six file-level importing partitions are the derived consumer set.
        "tests/gateway_reconcile_writer_helpers.py",
        (
            "tests/test_gateway_reconcile_idempotency_barrier.py",
            "tests/test_gateway_reconcile_writer_launch.py",
            "tests/test_gateway_reconcile_writer_prepare.py",
            "tests/test_gateway_reconcile_writer_quiescence.py",
            "tests/test_gateway_reconcile_writer_receipts.py",
            "tests/test_gateway_reconcile_writer_rollforward.py",
        ),
    ),
    PathTestRule(
        # #1912: the non-collectible helper owning the Basins publication corpus's
        # shared fixture builders (`_write_valid_inventory`, `_make_valid_model`,
        # `_object_store_env`, the canonical required-file/manifest/CLI helpers and
        # the identity snapshot). All seven collectible partitions import it at module
        # scope, and so does `tests/test_basins_package.py`, whose two helpers moved
        # here from the former monolith. A builder edit can therefore flip publication
        # oracles from "refused" to "published" without touching a single test file, so
        # the whole consumer set must run — the selector's meta-guard rider is added by
        # the support-module branch itself and is deliberately NOT repeated here.
        BASINS_PACKAGE_HELPERS_PATH,
        BASINS_PACKAGE_HELPERS_CONSUMER_TESTS,
    ),
    PathTestRule(
        # #1948: the QHH bootstrap helper owns the registry-fixture builder, the
        # scheduler-readiness seeds/teardown, the four readiness constants and the
        # `qhh_scheduler_canonical_readiness` fixture. All three partitions import it at
        # module scope, so a helper-only diff must run the whole consumer set — and it
        # must ALSO open the real-database lane, which SUPPORT_MODULE_TEST_RULES cannot do
        # on its own: that is why the same path is an exact `database:` trigger in ci.yml.
        # The meta-guard rider is added by the support-module branch, deliberately not here.
        QHH_PRODUCTION_BOOTSTRAP_HELPERS_PATH,
        QHH_PRODUCTION_BOOTSTRAP_TESTS,
    ),
    PathTestRule(
        # I1 #1980 river_ts_render: the non-collectible register of river read
        # templates. Every SQL-shape oracle in the group derives its coverage
        # FROM it — which templates exist, how many aids each carries, how many
        # times each file names the fact table — so an edit here silently changes
        # what four suites assert without touching any of them. The forecast
        # store-routing and hydro-display MVT suites also import the register
        # and must run on changes.
        # #2208: the national probe's file-level integration gate makes it a
        # database-lane consumer, not a unit target. ci.yml routes this support
        # path there; retain only the non-gated importers here.
        # #2074: the hydro-display MVT suite was partitioned, and exactly ONE
        # partition imports this register — `..._instants.py`, whose
        # `hydro_display:mvt_source_identity_probe` cases render the registered
        # template and compare it to the probe's statement. The other eight
        # partitions never touch it, so listing them here would buy suites that
        # cannot red on a register edit.
        "tests/river_ts_template_registry.py",
        (
            *SQL_SHAPE_ORACLE_TESTS,
            "tests/test_forecast_store_routing.py",
            "tests/test_hydro_display_mvt_scaling_instants.py",
            # #2424 D1: renders the registered `latest_cycle_fact_probe` entry and
            # the `per_source_latest_cycles` execution against its shape pins.
            "tests/test_latest_cycle_discovery_shape.py",
        ),
    ),
    PathTestRule(
        # I11 #1990 forcing_ts_render (cut a): the forcing counterpart of the
        # river register above — the non-collectible module that owns the
        # discovery roots, the exemption/unwired-reader ledgers, the renderer
        # constant declarations and the mention counter the forcing
        # discovery-set census judges the tree with. Everything that census
        # asserts (which files may name `met.forcing_station_timeseries`, how
        # many times each does, which mentions are registered reads) is DERIVED
        # from this module, so an edit here rewrites the verdict without
        # touching the suite that renders it.
        # Its non-gated importer closure is exactly one suite,
        # tests/test_forcing_ts_template_census.py, derived over both edge kinds
        # (module-scope import + literal-path consumption). Nothing is
        # deliberately excluded here: unlike tests/river_ts_template_registry.py
        # (#2208) this module has no integration-gated importer at all, so no
        # `database:` entry in ci.yml is owed for it.
        # Cut (a) landed the register EMPTY of reader entries; cut (b) (task
        # 7.2) populated it with the nine and brought a SECOND importer,
        # tests/test_forcing_read_path_store_routing.py, which parametrises its
        # call-site / narrow-shape assertions over this register — so
        # an entry added or dropped here rewrites what that suite asserts too.
        # Both importers are listed; both are non-gated.
        "tests/forcing_ts_template_registry.py",
        (
            "tests/test_forcing_ts_template_census.py",
            # Cut (b)'s second importer: the register is now the parametrisation
            # source of the call-site / narrow-shape oracle too, so
            # an entry added or dropped here changes what that suite asserts
            # without touching it.
            "tests/test_forcing_read_path_store_routing.py",
            # #1991 task 7.3's third importer: the renderer suite takes this
            # module's REPO_ROOT to read
            # `db/migrations/000061_forcing_station_timeseries_narrow_expand.sql`
            # and hold the FORCING_TABLE constant against the `CREATE TABLE`
            # that made it true. A one-name import, but
            # the closure check is over edges and not over how much is imported.
            "tests/test_forcing_ts_render.py",
        ),
    ),
    PathTestRule(
        # #2451: the segment-index measurement bench's pass criteria. This module
        # owns the three `design.md` criteria a measured plan must satisfy, the
        # branch identity columns and the shared-hit floors — so an edit here
        # moves what every cell of the cross product is judged against without
        # touching a line of the suites that judge. Its two non-gated importers
        # (the synthetic-plan proof, which is also the same-name owner, and the
        # offline capture/digest suite) import it at module scope.
        # The integration bench (tests/test_river_timeseries_stats_index_choice_
        # integration.py) is a third module-scope importer and is deliberately
        # ABSENT: its file-level `pytestmark = pytest.mark.integration` would skip
        # it in the PR lane, so routing it buys constant skips (#1447, and the
        # same call #2208 made for tests/river_ts_template_registry.py). This
        # table cannot open the `database:` lane for it either; that is ci.yml's.
        # 52 passed in 0.50 s for both routed suites together.
        "tests/river_ts_plan_criteria.py",
        (
            "tests/test_river_ts_plan_criteria.py",
            "tests/test_river_ts_stats_harness_offline.py",
        ),
    ),
    PathTestRule(
        # #2451: the bench's condition seed — the scenario table, the step count
        # and the array-literal parser the recorded fact rows are digested
        # through. The offline suite is its only non-gated module-scope importer
        # and asserts exactly those shapes against the real producer, so a seed
        # edit that the suite does not run is an unjudged change to the measured
        # input. The integration bench imports it too and is excluded for the
        # file-level gating reason recorded on the rule above.
        "tests/river_ts_stats_matrix_seed.py",
        ("tests/test_river_ts_stats_harness_offline.py",),
    ),
    PathTestRule(
        # #1913: the registry-import helper owns the former monolith's 19 support
        # functions, `_FakeRiverSegmentCursor` and the four private constants. Its eleven
        # direct collectible importers are the seven registry suites plus the four #1102
        # publisher partitions that name `_write_registry_fixture` / `_make_valid_model`,
        # all at module scope. The TWO support-to-support edges are invisible to the
        # non-recursive support-rule walk, so their collectible reach is routed
        # explicitly here — `tests/qhh_production_bootstrap_helpers.py` (module-scope
        # `_write_registry_fixture`, function-scope `_package_manifest_for_model`) brings
        # the three QHH bootstrap partitions, and `tests/publish_registry_helpers.py`
        # (module-scope `_make_valid_model`) brings the one publisher partition that
        # reaches `_write_healthy_basin_pair` without importing this helper itself.
        # Fifteen collectible suites total, meta-guard rider added by the support-module
        # branch itself.
        BASINS_REGISTRY_IMPORT_HELPERS_PATH,
        BASINS_REGISTRY_IMPORT_HELPERS_CONSUMER_TESTS,
    ),
    PathTestRule(
        # #1102: the shared fixture surface of the seven publisher partitions — the
        # module-wide autouse `basins_package_source_identity` stub and its two readers,
        # the canonical catalog seeder, the healthy/broken/radiation basin-pair builders,
        # the fake inventory/packager/sources triple and the calibration-declaration
        # builders. The autouse stub decides what EVERY case in the corpus sees (without
        # it the real content-addressed identity runs and the synthetic inventories stop
        # being stable), so all seven import it at module scope and the routed set IS the
        # derived closure.
        PUBLISH_SCHEDULER_REGISTRY_HELPERS_PATH,
        PUBLISH_SCHEDULER_REGISTRY_TESTS,
    ),
    PathTestRule(
        # #1823: the shared surface of the fifteen entropy-audit partitions --
        # REPO_ROOT/BASELINE path constants, the memoized whole-repository
        # `build_report` accessor (a ~20s call the corpus asks for from a dozen
        # places), the finding selectors and every fixture builder. All fifteen
        # import it at module scope, so the routed set IS the derived closure;
        # without this row a helper-only diff collapses to the meta-guard,
        # because the filename is not `test_*` and never reaches the `tests/**`
        # suite branch.
        ENTROPY_AUDIT_HELPERS_PATH,
        ENTROPY_AUDIT_TESTS,
    ),
    PathTestRule(
        # #1103: the shared surface of the two node-22 entrypoint partitions --
        # the repo root, the node-22/node-27 root constants and the `_read`
        # both partitions call on every governed file. Neither partition can
        # run without it, so a helper-only diff must select both; the filename
        # is not `test_*` and never reaches the `tests/**` suite branch.
        NODE22_ENTRYPOINT_HELPERS_PATH,
        NODE22_ENTRYPOINT_INVARIANT_TESTS,
    ),
    PathTestRule(
        # #1103: the production-ops surface set. It decides WHICH files every
        # whole-document runbook guard scans, so a change here can silently
        # shrink six suites' reach to the (command-free) index page -- exactly
        # the vacuous pass the split had to avoid. Routed to the full reader
        # set, not just its importers, because the readers that pin a single
        # sub-runbook by path are governed by the same surface decision.
        PRODUCTION_OPS_RUNBOOK_HELPERS_PATH,
        PRODUCTION_OPS_RUNBOOK_TESTS,
    ),
    PathTestRule(
        # #2516 D3: master's frozen station legs (legacy + narrow) that the
        # membership-fence shape suite compares both templates with. Its only
        # non-gated importer is that suite; the filename is not `test_*`, so
        # without this row an oracle-only diff collapses to the meta-guard.
        "tests/station_membership_fence_oracle.py",
        ("tests/test_station_membership_fence.py",),
    ),
    PathTestRule(
        # #2424 D1: master's frozen `_per_source_latest_cycles` statement, the
        # Evidence-Floor-1 oracle. The shape suite pins its bytes to master's
        # sha256 and is its only non-gated importer; the equality suite
        # (tests/test_latest_cycle_discovery_integration.py) is a module-scope
        # importer too but deliberately ABSENT: its file-level
        # `pytestmark = pytest.mark.integration` would skip it in the PR lane
        # (the #1447 / #2208 call). Without this row an oracle-only diff collapses
        # to the meta-guard and the pin never runs.
        "tests/latest_cycle_discovery_oracle.py",
        ("tests/test_latest_cycle_discovery_shape.py",),
    ),
    PathTestRule(
        # #2490: the shared builders and the autouse `_clean_env` of the three
        # MVT cache-retention partitions. All three import it at module scope,
        # so the routed set IS the derived closure; without this row a
        # helper-only diff would collapse to the meta-guard and a broken
        # `_clean_env` would let the display-process env leak into every case.
        NODE27_MVT_CACHE_RETENTION_HELPERS_PATH,
        NODE27_MVT_CACHE_RETENTION_TESTS,
    ),
    PathTestRule(
        # #2595: the real-disk suite's run-time cycle selection. The real-disk
        # suite itself is file-level `e2e`-gated, so it is not a derived
        # importer and is deliberately absent (it would only skip in the PR
        # lane); the local proof with temporary stores is the one non-gated
        # importer, and it is what a selection change must run.
        "tests/object_store_forcing_real_disk_support.py",
        ("tests/test_object_store_forcing_real_disk_support.py",),
    ),
)
# #2348: binds the hand-published success schemas (static openapi/nhms.v1.yaml)
# to the runtime response models; the only oracle that reads both sources.
RESPONSE_MODEL_SCHEMA_PARITY_TEST = "tests/test_response_model_schema_parity.py"
# #2222: the `/runs` public projection (SQL allowlist, serializer, route model).
HYDRO_RUN_PUBLIC_PROJECTION_TEST = "tests/test_hydro_run_public_projection.py"
# #2048: 000062-000064 and the runner's ledger/disk check against a production-shaped catalog.
SCHEMA_LEDGER_CONVERGENCE_INTEGRATION_TEST = "tests/test_schema_ledger_convergence_integration.py"
# The route modules the unit-level guard walks from the registry: each declares
# a module-level `_APPLICATION_NAME` and injects it into its store factories.
# #2078: apps/api/routes/forecast.py is deliberately absent — it gained an exact
# rule (tests/test_forecast_api.py) and these suites are MERGED into that entry
# instead, exactly like forecast_store.py / state_manager.py below: a duplicate
# pattern splits the module's ownership across two rules
# (test_path_rule_duplicate_patterns_are_allowlisted_decisions).
# #2098: apps/api/route_registry.py — the walk's root — is deliberately absent
# for the same reason. It gained an exact rule (see the precipitation
# composition owners near the end of PATH_TEST_RULES) and these suites are
# MERGED into that entry. It never satisfied the `_APPLICATION_NAME` sentence
# above either: the registry declares no such name, it only composes the
# routers that do.
CONNECTION_ATTRIBUTION_ROUTE_PATHS: tuple[str, ...] = (
    "apps/api/routes/best_available.py",
    "apps/api/routes/data_sources.py",
    "apps/api/routes/models.py",
    "apps/api/routes/state_snapshots.py",
)

# The packages/common stores that carry the #1728 injection seam
# (`application_name=` through `from_env` down to the connect call). Two more —
# forecast_store.py and state_manager.py — already have exact rules, so the
# suites are MERGED into those instead of listed here: a duplicate pattern
# splits a module's ownership across two rules
# (test_path_rule_duplicate_patterns_are_allowlisted_decisions).
# #1990 removed best_available.py from this tuple and gave it an exact rule (see
# the forcing-census block in PATH_TEST_RULES): it holds a registered forcing
# read template, so it needs the forcing oracle rider, and a duplicate pattern
# would split its ownership across two rules. CONNECTION_ATTRIBUTION_TESTS is
# MERGED into that entry — the same disposition forecast_store.py and
# state_manager.py already have, for the same reason.
# #2617: `model_registry_*.py` are the owner modules of the model registry
# store's methods (the facade keeps the connection); the connection-attribution
# scans walk and parametrize them, so an owner-only diff routes like the facade.
# #2348: the rule built from this tuple also carries the response-model
# preservation oracle. The oracle fakes only the registry store; the route
# pipes the faked rows through the production projections
# `sanitize_basin_version_list_payload` / `sanitize_model_list_payload` /
# `sanitize_model_detail_payload` (apps/api/routes/models.py:429,738,762),
# defined in model_registry_public.py:21-31 and imported through the facade's
# re-exports (model_registry.py:152-155), so a diff to either changes a sampled
# payload. The sibling owner modules ride along because #2617 pins every owner
# to select exactly what the facade selects
# (tests/test_select_ci_tests.py::test_model_registry_owner_modules_route_like_the_facade).
# #2348 removed object_store_forcing.py for the same reason #1990 removed
# best_available.py: it needs the response-model preservation oracle too, so it
# has an exact rule (next to the route members' rules in PATH_TEST_RULES) that
# MERGES CONNECTION_ATTRIBUTION_TESTS.
# #2612: the rule built from this tuple also carries MODEL_REGISTRY_IMPORTER_TESTS
# (right below), for the same facade-parity reason.
CONNECTION_ATTRIBUTION_STORE_PATHS: tuple[str, ...] = (
    "packages/common/model_registry.py",
    "packages/common/model_registry_*.py",
)
# #1704: the only suite that asserts the error-response log line exists, is
# redacted, is bounded, and that the request-id is not client-forgeable. Both
# halves of that contract live in apps/api/errors.py (the chokepoint) and
# apps/api/main.py (the handler install + the request-id middleware); the
# `apps/api/**` rule's three broad API suites assert none of it.
API_ERROR_LOGGING_TEST = "tests/test_api_errors_logging.py"


PATH_TEST_RULES: tuple[PathTestRule, ...] = (
    PathTestRule(
        ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS[0],
        (*ORCHESTRATOR_MANIFEST_SURFACE_TESTS, "tests/test_pipeline_job_provenance_publisher.py"),
        stop_on_match=True,
    ),
    PathTestRule(
        # #2539: the runtime-manifest schema contract validates this module's real
        # builder output against schemas/run_manifest.schema.json.
        ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS[1],
        (
            *ORCHESTRATOR_MANIFEST_SURFACE_TESTS,
            "tests/test_run_manifest_schema_contract.py",
            # #2744: the canonical/mirror comparison bounds its reads by the
            # shared registry bound; the capacity suite fails if it stops.
            "tests/test_scheduler_registry_capacity.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS[2],
        (*ORCHESTRATOR_MANIFEST_SURFACE_TESTS, *CHAIN_IMPORTER_TESTS),
        stop_on_match=True,
    ),
    PathTestRule(
        # scheduler.py is the allowlisted duplicate pattern: this non-stop entry
        # plus the stop-on-match FILE_JOURNAL entry below. #1455's additions
        # extend THIS existing entry rather than adding a third — the duplicate
        # allowlist records a two-entry layering, and a third would split the
        # module's ownership again.
        ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS[3],
        (*ORCHESTRATOR_MANIFEST_SURFACE_TESTS, *SCHEDULER_IMPORTER_TESTS),
    ),
    PathTestRule(
        # #2921: the contract module moved here and inherited the deleted shim
        # row's targets and stop semantics. Extended AT THE RULE SITE, not by
        # editing DIRECT_GRID_SURFACE_TESTS: the shared constant also serves the
        # openspec-change pattern below, whose selection must not move (#1455
        # scopes to the nine directories). `packages/common/**` still adds the
        # #1744 core-smoke baseline outside the rule loop -- intentionally.
        DIRECT_GRID_CONTRACT_CANONICAL_PATH,
        (*DIRECT_GRID_SURFACE_TESTS, *DIRECT_GRID_CONTRACT_IMPORTER_TESTS),
        stop_on_match=True,
    ),
    PathTestRule(
        DIRECT_GRID_SURFACE_PATH_PATTERNS[0],
        DIRECT_GRID_SURFACE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        # Extended AT THE RULE SITE (#1192), not by editing the shared constant:
        # safe_fs.py owns tests/test_safe_fs.py, while the journal mappings below
        # retain the file-journal closure. A separate stop_on_match rule would
        # shift selection because first match wins.
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[0],
        (*FILE_JOURNAL_READ_STATE_TESTS, "tests/test_safe_fs.py", *C4_PRODUCTION_ACCEPTANCE_TESTS),
        stop_on_match=True,
    ),
    PathTestRule(
        # The dedicated no-clobber publication owner is used by archive
        # publication and restore. It needs both its filesystem contract and
        # the two journal-retention behavioral partitions.
        "packages/common/safe_fs_publication.py",
        (
            *FILE_JOURNAL_READ_STATE_TESTS,
            "tests/test_safe_fs.py",
            *C4_PRODUCTION_ACCEPTANCE_TESTS,
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        # #2540: the shared lock-file opener. Its own contract suite plus the
        # suites of its three callers -- the journal cycle lock (the macOS
        # first-creation race reproducer and the journal suite), the provider
        # destination lock, and the safe_fs layer it belongs to. The lease
        # guard site is covered by the contract suite's binding pin and Barrier
        # test. DB-free.
        "packages/common/safe_fs_lock.py",
        (
            "tests/test_safe_fs_lock.py",
            "tests/test_forcing_submit_ambiguity.py",
            "tests/test_file_orchestration_journal.py",
            "tests/test_scheduler_refresh_provider_atomic.py",
            "tests/test_safe_fs.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        # #2540: the scheduler lease guard file opens through the shared
        # lock-file opener; the contract suite pins that binding and runs the
        # first-creation Barrier race against this module. Additive (no stop):
        # the broad orchestrator directory rule still applies. Sub-second.
        "services/orchestrator/scheduler_lease.py",
        # #2917: the env drift table asserts `_file_lock_guard_mode()`'s unset default.
        ("tests/test_safe_fs_lock.py", "tests/test_node22_scheduler_env_drift_table.py"),
    ),
    # C4 reads pinned evidence through evidence_io's descriptor-bound
    # byte/JSON identity primitives. This exact owner has no same-name suite,
    # so route the surviving C4 acceptance partitions without pulling retired
    # C1/C2/C3 suites; shared core/invariant riders remain additive outside
    # PATH_TEST_RULES.
    PathTestRule(
        "packages/common/evidence_io.py",
        C4_PRODUCTION_ACCEPTANCE_TESTS,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[1],
        FILE_JOURNAL_READ_STATE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[2],
        (*FILE_JOURNAL_READ_STATE_TESTS, *FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS),
        stop_on_match=True,
    ),
    PathTestRule(
        "services/orchestrator/file_journal_*.py",
        (*FILE_JOURNAL_READ_STATE_TESTS, *FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS),
        stop_on_match=True,
    ),
    PathTestRule(
        # Extended AT THE RULE SITE (#1955), not by editing the shared constant:
        # FILE_JOURNAL_READ_STATE_TESTS also serves safe_fs.py and every other
        # journal pattern, whose selection must not move. This module owns the
        # create-capable import lane and `_rollback_execution_lock` — the two
        # places design D2/D3 decide whether a root may be created and which
        # tree the lock lands in — and the lane-adoption suite is their
        # requirement oracle, so this stop rule is where its importer gap
        # closes. DB-free, 76 tests in 0.70s, hence a rule rather than a
        # rule-gap exclusion.
        #
        # #2390 (ruling A) adds the five gateway-reconcile writer suites at the
        # same site and for the same reason: the shared constant serves eleven
        # other patterns, and this stop rule shadows the broad
        # `services/orchestrator/**` list, so the rule site is the only place a
        # route for this module can live. The five suites drive the file-journal
        # rollback lanes (`prepare_file_journal_rollback` and the rollback
        # launch / roll-forward / receipt / quiescence legs) of THIS module, but
        # every one of them imports it inside function bodies, never at module
        # scope — so the meta-suite's `_non_gated_top_level_importer_index`,
        # which walks module-level imports only, derives zero `(module, suite)`
        # pairs and the importer-gap audit could not see the missing route (nor
        # accept an INTENTIONAL_RULE_GAP_EXCLUSIONS entry for a pair it never
        # derives).
        # 34 of the 36 top-level tests touch the module, so no node-id subset is
        # defensible; the whole files ride. DB-free, 77 tests in 14.78s (macOS
        # local) — above the sub-2s riders here, far below CHAIN_IMPORTER_TESTS.
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[3],
        (
            *FILE_JOURNAL_READ_STATE_TESTS,
            "tests/test_journal_root_lane_adoption.py",
            "tests/test_gateway_reconcile_writer_prepare.py",
            "tests/test_gateway_reconcile_writer_launch.py",
            "tests/test_gateway_reconcile_writer_rollforward.py",
            "tests/test_gateway_reconcile_writer_receipts.py",
            "tests/test_gateway_reconcile_writer_quiescence.py",
            # #2674: the shape (c) guard suite top-level-imports this module; its
            # E7 cases pin the import pre-scan that fails closed before the
            # journal root is created. DB-free, ~1s.
            "tests/test_file_journal_legacy_unversioned_reserved_guard.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[4],
        (*FILE_JOURNAL_READ_STATE_TESTS, *ORCHESTRATOR_CLI_IMPORTER_TESTS),
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[5],
        FILE_JOURNAL_READ_STATE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[6],
        FILE_JOURNAL_READ_STATE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[7],
        FILE_JOURNAL_READ_STATE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        # Extended AT THE RULE SITE (#2238), not by editing the shared constant:
        # ORCHESTRATOR_CLI_IMPORTER_TESTS is also spliced into pattern[4]
        # (scheduler_journal_archive.py), which the copyback-mutex suite does not
        # import and whose selection must not move. cli.py is stop-rule owned, so
        # the broad `services/orchestrator/**` list that carries the other four
        # retention partitions (and the independent frontier suite, which is not
        # a partition) is unreachable here — this is where the fifth
        # partition's importer gap closes. The suite drives `cli._run_cleanup`,
        # the out-of-pass entrypoint that reads NHMS_OBJECT_STORE_COPYBACK_ROOT
        # from the environment and thereby decides which roots the deleter locks,
        # so an env-read or root-assembly edit here must run it. DB-free, 25
        # tests in 5.27s, hence a rule rather than a rule-gap exclusion.
        #
        # #1955/#1953 added two more at-site targets for the same reason. Both
        # drive `_click_main` AND `_argparse_main` and both own an exit-code and
        # stderr-rendering contract that lives HERE: the lane-adoption suite
        # pins the typed `FILE_JOURNAL_INVALID_ROOT` line and exit 2 on five
        # commands (`migrate-scheduler-state` among them, where the refusal is
        # inherited and the `except OrchestratorError` arm ordering decides
        # whether the code prefix survives), the budget-contract suite pins the
        # one typed line the recovery command renders when the whole-tree replay
        # raises. An arm-ordering or exit-code edit in cli.py must run them.
        # DB-free, 87 tests in 2.07s together.
        #
        # #1555/#1768 added a fourth at-site target for the same reason: the
        # operator re-entry confirmation suite's whole write side goes through
        # the shipped `cli.main(["confirm-operator-reentry", ...])` entry, so the
        # command's registration, argument parsing and exit codes live HERE. It
        # does not import scheduler_journal_archive.py, so it rides the rule site
        # rather than ORCHESTRATOR_CLI_IMPORTER_TESTS (spliced into pattern[4]).
        # DB-free, 23 tests in 46.71s — heavier than the three targets above but
        # far under the ~5 min per-module line, so a rule not an exclusion.
        #
        # #1186 added a fifth, on the same ground and for a cheaper suite: every
        # test in the operator-action listing suite drives the shipped
        # `cli.main(["list-operator-actions", ...])` entry, so the command's
        # registration, `--evidence-root` / `--passes` parsing and the 0/1/2/3
        # exit-code contract are decided HERE — the argparse fallback leg is
        # pinned against the click leg in the same file. It does not import
        # scheduler_journal_archive.py, so it rides the rule site rather than
        # ORCHESTRATOR_CLI_IMPORTER_TESTS (spliced into pattern[4]). DB-free, 75
        # tests in 0.34s, the cheapest target on this list.
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[8],
        (
            *FILE_JOURNAL_READ_STATE_TESTS,
            *ORCHESTRATOR_CLI_IMPORTER_TESTS,
            # #2259 split the copyback-mutex partition in two; the EF-15 case
            # that drives `cli._run_cleanup` is in the budget half, but the
            # protocol half rides with it because the two share every fixture.
            *RETENTION_COPYBACK_MUTEX_TESTS,
            "tests/test_journal_root_lane_adoption.py",
            "tests/test_file_journal_full_tree_budget_contract.py",
            "tests/test_operator_reentry_confirmation.py",
            "tests/test_operator_action_listing.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[9],
        FILE_JOURNAL_READ_STATE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        # Extended AT THE RULE SITE (#1943), not by editing the shared constant:
        # FILE_JOURNAL_READ_STATE_TESTS also serves packages/common/safe_fs.py
        # and the other journal patterns, whose selection must not move. The
        # root-authority suite top-level-imports
        # `_db_free_orchestration_repository_from_config` from scheduler_core.py
        # — the factory that must now refuse a symlinked journal root — so this
        # stop rule is where its importer gap closes.
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[10],
        (*FILE_JOURNAL_READ_STATE_TESTS, "tests/test_scheduler_journal_root_authority.py"),
        stop_on_match=True,
    ),
    PathTestRule(
        # Extended AT THE RULE SITE (#2260), not by editing the shared constant:
        # FILE_JOURNAL_READ_STATE_TESTS also serves every other journal pattern,
        # whose selection must not move. scheduler_runtime.py is stop-rule owned,
        # so the broad `services/orchestrator/**` list that carries the
        # copyback-mutex partition is unreachable here. It is the scheduler call
        # site that names the shared copyback root the retention deleter locks,
        # one of the mutex's two load-bearing modules (the other is
        # packages/common/copyback_guard.py, routed below), so this stop rule is
        # where its gap closes.
        #
        # #2316 closes the sibling tests/test_retention_extra_roots.py gap the
        # #2260 PR recorded as a known limit, at this same site and on the same
        # ground: that suite is the oracle for the `runs_only_roots` extra-root
        # wiring (`(os.getenv("WORKSPACE_ROOT"),
        # self.config.object_store_copyback_root)`) this module hands the
        # retention deleter. Patterns [9] (scheduler.py) and [10]
        # (scheduler_core.py) deliberately do NOT carry it: the suite's
        # `_pass_scheduler` helper (tests/retention_test_helpers.py) imports
        # ProductionScheduler from scheduler.py only to construct the object
        # under test, so a regression of the extra-root deletion surface has to
        # touch THIS file, and widening [9]/[10] would move two more selections
        # for no failure they could catch. DB-free, 32 tests in 0.30s (macOS
        # local).
        #
        # #1186 round 2 adds the second at-site target. scheduler_runtime.py
        # writes most of the top-level keys of a pass evidence payload (it keeps
        # adding to the dict `base_evidence` returned, `counts` and
        # `duplicate_exclusions` among them), and the scope-dimension closure pin
        # runs a REAL pass and asserts every published key carries a disposition.
        # A new key here without a disposition is exactly the silent false-exit-0
        # this PR exists to close, so that one node id — not the whole 2000-test
        # suite — rides this rule. Measured at 0.04s of call time.
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[11],
        (
            *FILE_JOURNAL_READ_STATE_TESTS,
            # #2259: both halves of the split copyback-mutex partition.
            *RETENTION_COPYBACK_MUTEX_TESTS,
            "tests/test_production_scheduler.py::test_every_dimension_a_real_pass_publishes_has_a_scope_disposition",
            # #1905/#2402 adds the third at-site target. scheduler_runtime.py is
            # the pass writer that reaches the evidence-size ladder (it hands the
            # payload to `_write_evidence` and takes the post-write status back
            # into SchedulerPassResult), and the non-blocking summary tier is
            # exactly what keeps that status TRUE instead of rewriting it to
            # `resource_limit_blocked`. The decidability suite is the oracle for
            # that agreement, and being module-scope-import-free it has no
            # importer derivation, so this stop rule is its only route for this
            # module. DB-free, ~20 tests in ~20s.
            "tests/test_scheduler_evidence_decidability.py",
            # #2405 adds the fourth at-site target on the same grounds: this
            # module registers the lease heartbeat's touch path on the
            # reservation it just reserved, and the reservation-lease suite is
            # the oracle for "registered only after `reserved`". Also
            # module-scope-import-free, so this stop rule is its only route for
            # this module. DB-free, 15 tests in ~1s.
            "tests/test_operator_action_reservation_lease.py",
            # #2442 adds the fifth at-site target. This module is the PASS
            # WRITER whose `run_once` status literals the closure pin reads with
            # `ast`: a status added or moved here is exactly what the pin has to
            # go red on, and the pin imports nothing at module scope, so this
            # stop rule is its only route for this module. DB-free, 4 tests in
            # ~0.3s.
            "tests/test_operator_action_status_closure.py",
            # #2668 adds the sixth at-site target: this module serializes the
            # reserved-unbound outcome rows (now with the attempt anchor) that
            # the held-reservation listing ages, and that suite pins the
            # serialized key. DB-free, 41 tests in ~4s.
            "tests/test_operator_action_listing_held_reservations.py",
            # #2662 adds the seventh at-site target, a D4 parity edge like the
            # scheduler_evidence.py / scheduler_no_progress.py rows further
            # down. The node-22 stall probe is stdlib-only, so it carries a
            # literal copy of this module's `_RETENTION_TERMINAL_SKIP_REASONS`
            # (a skip reason outside it is in-flight and must not clear the
            # probe's stall streak). The parity test imports this module inside
            # its function body, so no importer derivation reaches it and this
            # stop rule is its only route. One node id, not the ~170-test probe
            # suite: the rest of that suite reads nothing from this module.
            "tests/test_node22_scheduler_stall_health.py::test_e5_the_terminal_skip_reasons_equal_the_schedulers_own",
            # #2912: parity pin for the probe's `scheduler_root_preflight_blocked` literal (writer source).
            "tests/test_node22_scheduler_stall_health.py::test_j11_the_root_blocked_boundary_is_the_literal_the_scheduler_writes",
            # #2316: the extra-root wiring oracle (rationale above).
            "tests/test_retention_extra_roots.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        # #1748 recovery-CLI helper extraction.  Stop rule on
        # purpose: without it the broad `services/orchestrator/**` rule would
        # drag the full directory list for a module whose only production
        # consumer is cli.py.  The two suites that drive the operator-facing
        # channel and the signal/command e2e are named exactly, plus the demote
        # CLI security suite which shares the register boundary.
        "services/orchestrator/operator_released_reservation_recovery.py",
        RELEASED_RESERVATION_RECOVERY_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        # The three object-store-root additions (#1455) are non-gated top-level
        # importer suites of the adapters that this rule already owns:
        # test_object_store_roots.py builds ERA5/GFS/IFS adapters directly, and
        # the bbox pair consumes the bbox contract (region.py here until 1.3). They are cheap
        # (0.8-2.4s) and every adapter shares the object-store root convention,
        # so they ride the directory list rather than three narrow rules.
        "workers/data_adapters/**",
        DATA_ADAPTERS_DIRECTORY_TESTS,
    ),
    PathTestRule(
        # #2964: GeoBBox / china_buffered_bbox_from_env moved here from
        # workers/data_adapters/region.py. NOT a stop row: the targets are the
        # `workers/data_adapters/**` row's (shared constant, so they cannot
        # drift apart) plus test_download_bbox_clip.py, which reaches region
        # only through importlib and so is invisible to gap derivation; it is
        # added here only (deviation from "same test set", precedent #2922).
        REGION_CONTRACT_PATH,
        (*DATA_ADAPTERS_DIRECTORY_TESTS, "tests/test_download_bbox_clip.py"),
    ),
    PathTestRule(
        # base.py's remaining non-gated importers are orchestration-layer suites
        # borrowing its cycle-identity helpers (cycle_id_for / format_cycle_time
        # / CycleDiscovery); those stay with the rules that own them (#1455
        # `edge-consumer` routings). The archive contract independently rebuilds
        # cycle identity through cycle_id_for, so its focused suite belongs here.
        # #2420: the provenance publisher/importer and Ops identity envelope mint
        # cycle ids through cycle_id_for, so a helper-only PR must run them.
        "workers/data_adapters/base.py",
        DATA_ADAPTERS_BASE_HELPER_TESTS,
    ),
    PathTestRule(
        # #2922: the nine cycle-time helpers moved here from base.py, and
        # check_three_way_time_consistency from services/orchestrator/
        # time_consistency.py. NOT a stop row and NOT a copy of the broad
        # `services/orchestrator/**` row: the targets are base.py's two rows
        # (shared constants, so they cannot drift apart) plus the direct
        # consumer suite of the three-way check.
        TIME_NORMALIZATION_CONTRACT_PATH,
        (*DATA_ADAPTERS_DIRECTORY_TESTS, *DATA_ADAPTERS_BASE_HELPER_TESTS, "tests/test_warm_start_chaining.py"),
    ),
    PathTestRule(
        # #1455: the producer/cli/store/package surface has same-subject suites
        # that no rule reached. All are seconds-scale; the heavier e2e-named
        # importers are dispositioned in tests/test_select_ci_tests.py instead.
        "workers/forcing_producer/**",
        (
            "tests/test_forcing_producer.py",
            "tests/test_production_met_validation.py",
            "tests/test_forcing_producer_cli.py",
            "tests/test_grid_signature.py",
            "tests/test_grid_snapshot_registration.py",
            "tests/test_source_identity.py",
            "tests/test_timescale_write_guard_wired.py",
            "tests/test_direct_grid_variant_registration.py",
            "tests/test_producer_bbox_preflight.py",
            "tests/test_object_store_roots.py",
            "tests/test_direct_grid_e2e.py",
            "tests/test_e2e.py",
            "tests/test_e2e_ifs.py",
            "tests/test_ifs_forecast_integration.py",
            # #2713: store.py's timeseries replace opens with the
            # compression/ingest fence and the producer + CLI report a busy
            # fence as deferred; those arms and the fence-first wire site are
            # asserted in these two suites.
            "tests/test_timeseries_compression_fence_writers.py",
            "tests/test_timeseries_compression_fence_wire_site_invariant.py",
        ),
    ),
    PathTestRule(
        "workers/shud_runtime/**",
        (
            "tests/test_shud_runtime.py",
            "tests/test_runtime_mode.py",
            "tests/test_runtime_ic_header.py",
            # #1908: the production-closure live-submit lane makes its task run
            # ids submission-scoped precisely so cli.py's manifest safety gate
            # stays unchanged; that suite asserts the gate directly.
            "tests/test_production_slurm_validation.py",
        ),
    ),
    PathTestRule(
        # #1455: the warm-start pair top-level-imports runtime.py and nothing
        # selected it. Narrow rather than on the directory list — neither
        # `__init__.py` nor `cli.py` imports warm-start behavior.
        "workers/shud_runtime/runtime.py",
        (
            "tests/test_warm_start.py",
            "tests/test_warm_start_chaining.py",
            "tests/test_direct_grid_e2e.py",
            "tests/test_e2e.py",
        ),
    ),
    PathTestRule(
        # #1455: nine cheap (0.6-4.6s) basin/registry suites top-level-import
        # modules of this small directory and none were selected. They ride the
        # directory list because the precision loss is nil — every one of them
        # is a basin-registry suite, and the whole added set runs in ~20s.
        "workers/model_registry/**",
        (
            "tests/test_model_registration.py",
            "tests/test_model_registry_basin_versions.py",
            "tests/test_model_registry_list_basins.py",
            "tests/test_basins_discovery.py",
            # #1832: `tests/test_basins_package.py` top-level-imports
            # `basins_discovery` (and `basins_calibration_overrides`), and is
            # the suite that owns the packaging contract those modules feed.
            # It rides the directory list for the same reason as the rest.
            "tests/test_basins_package.py",
            # #1912/#1903: the single publication core target is replaced by the seven-way
            # partition authority. Only `tests/test_basins_package_publication.py`
            # is same-name derivable from a Basins production module, and even that
            # derivation is not this rule's leg — so every one of the seven is proven
            # load-bearing by tests/test_select_ci_tests.py's per-edge RED rows.
            *BASINS_PACKAGE_PUBLICATION_TESTS,
            # #1913: the single registry monolith literal is replaced by the
            # seven-way partition authority. Only the retained core is same-name
            # derivable from a Basins production module, and even that derivation is
            # not this rule's leg for a probe like
            # `workers/model_registry/basins_reingest.py` — so every one of the seven
            # is proven load-bearing by tests/test_select_ci_tests.py's per-edge RED
            # rows.
            *BASINS_REGISTRY_IMPORT_TESTS,
            "tests/test_basins_reingest.py",
            "tests/test_direct_grid_variant_registration.py",
            "tests/test_hhe_mvt_binding.py",
            # #1813: this suite top-level-imports `basins_package` because it
            # owns the parity test binding the packager's forcing checksum
            # material to production-closure's reconstruction of it.  A change
            # to one implementation must run the test that pins both.
            "tests/test_production_object_store_validation.py",
            # #1102: the single publisher monolith literal expands to its seven
            # partitions. The whole corpus rides the directory list, not a
            # subset: `publish_all_basin_scheduler_registry` calls into
            # basins_discovery / basins_package / basins_radiation_template /
            # basins_calibration_overrides on every one of them, and the whole
            # added set is the same ~5s the monolith was.
            *PUBLISH_SCHEDULER_REGISTRY_TESTS,
            # #1948: the single QHH bootstrap monolith literal expands to the three
            # partitions; no prior target of this rule is dropped (the retained
            # historical path is QHH_PRODUCTION_BOOTSTRAP_TESTS[0]).
            *QHH_PRODUCTION_BOOTSTRAP_TESTS,
            "tests/test_qhh_scripts_static.py",
            # #2491: AST pin that `import_basin_into_registry_core` locks the
            # basin_version row first and that `_bootstrap_database` locks its
            # scope before delegating -- both read from this directory.
            "tests/test_registry_parent_lock_order_scan.py",
        ),
    ),
    PathTestRule(
        # #1711: every tracked module under workers/mapping_builder/ is owned by
        # this one directory rule selecting all tracked `tests/test_mapping_builder_*.py`
        # suites (MAPPING_BUILDER_TESTS — an explicit tuple today, guarded
        # against drift by the selector meta-suite's tree-derived
        # `_tracked_mapping_builder_suites()` equality assertion). Deliberately
        # NOT a stop rule: nothing earlier shadows the directory, and the
        # same-name derivation still adds tests/test_<module>.py where one
        # exists. The directory also joins DIRECTORY_RULE_AUDIT_PATHS so future
        # module/importer growth is dispositioned by the importer-gap guard
        # instead of silently falling out of the PR lane.
        #
        # The rule carries ONLY the mapping-builder package suites. rewrite.py's
        # three non-gated importer suites OUTSIDE the package (tests/test_state_clone.py,
        # tests/test_state_clone_cutover_hook.py, tests/test_state_clone_recalibration.py)
        # are deliberately NOT carried here: each already has an independent
        # owning surface (services/orchestrator/**, the state_clone_hook and
        # data_adapters/base rules, the node22-clone-script rule), so routing
        # them on every mapping-builder change would contaminate the lane. They
        # are dispositioned as `edge-consumer` pairs in
        # tests/test_select_ci_tests.py's INTENTIONAL_RULE_GAP_EXCLUSIONS.
        "workers/mapping_builder/**",
        MAPPING_BUILDER_TESTS,
    ),
    PathTestRule(
        # #2923: the verify_hydrologic_core_fingerprint_equal closure (and
        # STATE_COMPATIBILITY_SURFACES) moved here from
        # workers/mapping_builder/rewrite.py. NOT a stop row: same targets as
        # the `workers/mapping_builder/**` row (shared constant, so they cannot
        # drift apart); like that row it leaves the state_clone suites to their
        # own owning surfaces.
        HYDROLOGIC_FINGERPRINT_CONTRACT_PATH,
        MAPPING_BUILDER_TESTS,
    ),
    PathTestRule(
        # #1711: state_clone_hook.py's suite name is deliberately not same-name
        # derivable (no tests/test_state_clone_hook.py; its consumer suite is
        # the cutover-hook suite). Explicit irregular mapping.
        "packages/common/state_clone_hook.py",
        STATE_CLONE_HOOK_TESTS,
    ),
    PathTestRule(
        # #1711: the node-22 clone script's four suites are the recalibration
        # core, the recalibration CLI end-to-end, the recalibration CLI
        # validation split, and the baseline-cutover CLI suite, not same-name
        # derivable. Explicit irregular mapping.
        # #2739: the model succession partitions call its `build_parser` /
        # `enforce_mode_flags` / `dispatch` for real and read its receipts.
        # #2740: the succession tool's kind check compares the packages of a
        # pair with this script's `state_compatibility_gate_inputs`.
        "scripts/node22_clone_direct_grid_cutover_states.py",
        (*NODE22_CLONE_CUTOVER_STATES_TESTS, *MODEL_SUCCESSION_TESTS),
    ),
    PathTestRule(
        # #2744: the registry byte / node bounds are pinned, with their
        # boundary cases, only in the capacity suite.
        "packages/scheduler/registry_limits.py",
        ("tests/test_scheduler_registry_capacity.py",),
    ),
    PathTestRule(
        # #2744: this reader once carried a private registry byte bound; the
        # capacity suite is what fails if one reappears (chain_manifests.py
        # gets the same suite through its manifest-surface rule).
        # #2740: a cold-start succession gates on this script's `build_receipt`,
        # called in-process on real package files by the cold-start partition.
        # #2756: an add-basin succession gates on it the same way.
        "scripts/audit_first_cycle_initial_state.py",
        (
            "tests/test_scheduler_registry_capacity.py",
            "tests/test_first_cycle_initial_state_audit.py",
            "tests/test_node22_model_succession_add_basin.py",
            "tests/test_node22_model_succession_cold_start.py",
        ),
    ),
    PathTestRule(
        # #2737: the provision script's dry-run / apply / receipt suite and its
        # IC-header gate suite. Explicit irregular mapping.
        "scripts/provision_direct_grid_scheduler_registry.py",
        PROVISION_DIRECT_GRID_SCRIPT_TESTS,
    ),
    PathTestRule(
        # #2737: the succession receipt helpers have no suite of their own;
        # they are asserted through the provision script's suite. #2738: the
        # publish suite pins that this module still exports the shared helpers.
        "packages/common/provision_succession_receipt.py",
        SUCCESSION_RECEIPT_TESTS,
    ),
    PathTestRule(
        # #2738: the step-independent receipt helpers (succession id, receipt
        # directory, exclusive-create write) are used by the provision step and
        # by the merged-registry publish step; a change must run both suites.
        # #2739: the model succession tool writes its plan, step, failure and
        # abort receipts through the same helpers.
        # #2757: so does the node-27 basin retirement tool (receipt directory,
        # exclusive-create write, the succession id).
        # #2699: and the node-27 weight purge tool (exclusive-create write, file
        # sha256, the checkout commit).
        "packages/common/succession_receipt.py",
        (
            *SUCCESSION_RECEIPT_TESTS,
            *MODEL_SUCCESSION_TESTS,
            *BASIN_RETIREMENT_TESTS,
            "tests/test_node27_purge_superseded_weights.py",
        ),
    ),
    # #2738: the publish tool's entry script and one row per package module --
    # see MERGED_REGISTRY_PUBLISH_PACKAGE_MODULES.
    # #2739: each row also carries the model succession partitions, which run
    # the publish tool's dry-run and apply for real.
    PathTestRule(MERGED_REGISTRY_PUBLISH_OWNER_PATH, MERGED_REGISTRY_PUBLISH_TOOL_TESTS),
    PathTestRule("scripts/merged_registry_publish/apply.py", MERGED_REGISTRY_PUBLISH_TOOL_TESTS),
    PathTestRule("scripts/merged_registry_publish/model.py", MERGED_REGISTRY_PUBLISH_TOOL_TESTS),
    PathTestRule("scripts/merged_registry_publish/planning.py", MERGED_REGISTRY_PUBLISH_TOOL_TESTS),
    PathTestRule("scripts/merged_registry_publish/receipts.py", MERGED_REGISTRY_PUBLISH_TOOL_TESTS),
    # #2739: the succession tool's entry script and one row per package module --
    # see MODEL_SUCCESSION_PACKAGE_MODULES.
    PathTestRule(MODEL_SUCCESSION_OWNER_PATH, MODEL_SUCCESSION_TESTS),
    PathTestRule("scripts/model_succession/copyback.py", MODEL_SUCCESSION_TESTS),
    PathTestRule("scripts/model_succession/model.py", MODEL_SUCCESSION_TESTS),
    PathTestRule("scripts/model_succession/plan.py", MODEL_SUCCESSION_TESTS),
    PathTestRule("scripts/model_succession/run.py", MODEL_SUCCESSION_TESTS),
    PathTestRule("scripts/model_succession/scheduler.py", MODEL_SUCCESSION_TESTS),
    # #2779: the gateway deployment contract parametrises the runbook's pass
    # guard over this module's NOT_RUNNING_STATES / RUNNING_STATES.
    PathTestRule(
        "scripts/model_succession/systemd.py",
        (*MODEL_SUCCESSION_TESTS, SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST),
    ),
    PathTestRule("scripts/model_succession/tools.py", MODEL_SUCCESSION_TESTS),
    # #2757: the basin retirement tool's entry script and one row per package
    # module -- see BASIN_RETIREMENT_PACKAGE_MODULES.
    PathTestRule(BASIN_RETIREMENT_OWNER_PATH, BASIN_RETIREMENT_TESTS),
    PathTestRule("scripts/basin_retirement/autopipe.py", BASIN_RETIREMENT_TESTS),
    PathTestRule("scripts/basin_retirement/database.py", BASIN_RETIREMENT_TESTS),
    PathTestRule("scripts/basin_retirement/envfile.py", BASIN_RETIREMENT_TESTS),
    PathTestRule("scripts/basin_retirement/model.py", BASIN_RETIREMENT_TESTS),
    PathTestRule("scripts/basin_retirement/run.py", BASIN_RETIREMENT_TESTS),
    PathTestRule("scripts/basin_retirement/steps.py", BASIN_RETIREMENT_TESTS),
    PathTestRule(
        # #2737: `plan_direct_grid_variant` is pinned against what
        # `register_direct_grid_variant` then registers only in the provision
        # suite. Additive to the broad `workers/model_registry/**` rule.
        "workers/model_registry/direct_grid_variant_registration.py",
        (PROVISION_DIRECT_GRID_DRY_RUN_TEST,),
    ),
    PathTestRule(
        # #1455: `tests/test_output_parser.py` was the only target, so the cli
        # and dual-write suites — the ones that actually exercise the parser
        # package's entry points — never ran on an output_parser PR. Both are
        # sub-second and every module in this three-file directory is reachable
        # through them.
        "workers/output_parser/**",
        (
            "tests/test_output_parser.py",
            "tests/test_output_parser_cli.py",
            "tests/test_output_parser_dual_write.py",
            "tests/test_e2e.py",
            # #1714: parser.py is a registered connect-owning surface in the
            # attribution guard's per-file AST registry, and the suite
            # top-level-imports both it and the package, so BOTH directory
            # members' importer gaps close here. None of the four above assert
            # the component-level fallback_application_name identity, so a diff
            # that drops or renames it would otherwise reach CI green.
            "tests/test_node27_connection_attribution.py",
            "tests/test_node27_connection_attribution_delegated.py",
            # #2713: the river replace opens with the compression/ingest fence
            # and both CLI legs print the busy code the autopipe tick keys on;
            # asserted, end to end through the tick, in these two suites.
            "tests/test_timeseries_compression_fence_writers.py",
            "tests/test_timeseries_compression_fence_wire_site_invariant.py",
        ),
    ),
    PathTestRule(
        # #1455: parser.py alone carries these two importers (46s + 3s), so they
        # stay off the directory list and are paid only by a parser.py PR.
        "workers/output_parser/parser.py",
        (
            "tests/test_analysis_pipeline.py",
            "tests/test_timescale_write_guard_wired.py",
            # #1442: the replace chain's three statements plus the dual-write
            # INSERT are registered in the zero-text-identity oracle. It sits on
            # the narrow parser.py rule rather than the package directory rule
            # because parser.py is the only module in the package the oracle
            # reads.
            "tests/test_river_ts_text_identity_cleanup.py",
            # I1 #1980 river_ts_render: this file's river read templates are
            # registered in tests/river_ts_template_registry.py and rendered per
            # timeseries store by the whole SQL-shape oracle group, so a layout
            # or predicate edit here must run all of it — the golden equivalence
            # oracle included, which is what proves the edit was behaviour-free.
            # (One block per site rather than one new rule per path: the
            # selector's own duplicate-pattern guard forbids a second
            # PATH_TEST_RULES entry for an already-owned module.)
            *SQL_SHAPE_ORACLE_TESTS,
        ),
    ),
    PathTestRule(
        # #1455 closed 23 importer gaps here at once. Each addition is a
        # non-gated top-level importer of at least one module this rule owns,
        # and the whole added set measured ~55s locally — next to
        # test_orchestration_chain.py, which this rule already carried, that is
        # noise. They ride the directory list rather than 23 narrow per-module
        # rules because the alternative triples the rule table for suites nobody
        # would object to running on an orchestrator change; the modules with
        # genuinely distinct or expensive gaps (chain.py, scheduler.py, cli.py,
        # file_orchestration_journal.py) are owned by stop rules above and are
        # extended at THEIR sites instead.
        "services/orchestrator/**",
        (
            "tests/test_orchestrator.py",
            "tests/test_orchestration_chain.py",
            "tests/test_pipeline_job_provenance_publisher.py",
            "tests/test_pipeline_job_provenance_importer.py",
            "tests/test_pipeline_job_provenance_copyback.py",
            "tests/test_pipeline_ops_identity_envelope.py",
            # The delegated attribution suite now top-level-imports
            # services.orchestrator (package __init__) and
            # pipeline_job_provenance.py so the provenance importer seam is
            # classified. Those importer gaps close on this directory rule,
            # the same disposition display_coverage / hydro_display already use.
            *CONNECTION_ATTRIBUTION_TESTS,
            "tests/test_production_scheduler.py",
            "tests/test_scheduler_backfill.py",
            "tests/test_warm_start_chaining.py",
            # #1850: the accepted-submit-identity binding-provenance and
            # claimant-exclusivity suites top-level-import
            # services/orchestrator/accepted_submit_identity.py and are
            # sub-second fixtures beside the gateway-reconcile lane they join.
            "tests/test_gateway_reconcile_binding_provenance.py",
            "tests/test_gateway_reconcile_claimant_exclusivity.py",
            # #2655: the SubmitLine exact-key fallback-bind suite top-level-imports
            # accepted_submit_identity.py like its claimant-exclusivity sibling.
            # DB-free, 20 tests in ~1.5s.
            "tests/test_gateway_reconcile_submitline_exact.py",
            "tests/test_forcing_submit_ambiguity.py",
            # #2584: the ambiguous state_save_qc submit suite. Its subject is the
            # submit-failure branch of chain_stage_execution.py and the
            # recorded/origin error-code split in chain_forecast_submission.py;
            # it also pins retry.py / scheduler_state_types.py registration and
            # the scheduler_state_decision.py exhaustion reason. None of those is
            # stop-rule owned, so this directory rule is their route; its chain.py
            # and file_orchestration_journal.py pairs ride CHAIN_IMPORTER_TESTS and
            # FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS. DB-free, sub-second.
            "tests/test_state_save_submit_ambiguity.py",
            # #2624: the #2348 response-model preservation oracle. Its pipeline
            # half drives apps/api/routes/pipeline.py, which runs persistence.py's
            # `PipelineStore`, public_evidence.py's `_public_evidence` /
            # `_public_message` and retry.py's retry assembly un-faked on the
            # sampled paths (pipeline.py:40-49), so an edit to any of them that
            # changes a wire value reds it. Both halves ride together (the
            # pipeline half imports the core). DB-free, 25 tests in ~1.1s.
            *RESPONSE_MODEL_PRESERVATION_TESTS,
            # #2570 A / #2603 / #2559: the poll-isolation, cohort-membership and
            # forecast-projection suites. Their subjects are chain_stage_execution.py,
            # chain_forecast_execution.py, chain_types.py, chain_array_accounting.py,
            # chain_forecast_orchestrator_cycle.py, chain_source_cycle.py and the
            # scheduler_state_* readers; their top-level importer gaps on
            # accepted_submit_identity.py, chain_stages.py, retry.py,
            # scheduler_candidate_quality.py, scheduler_timing.py and the package
            # __init__ close on this directory rule. The chain.py / scheduler.py /
            # file_orchestration_journal.py pairs ride their stop-rule tuples.
            # DB-free, together ~12s.
            "tests/test_chain_stage_poll_isolation.py",
            "tests/test_cohort_membership_attribution.py",
            "tests/test_forecast_cohort_projection_restart_stage.py",
            # #2666: the held-reservation block suite. Its subject is
            # scheduler_state_decision.py / scheduler_state_rows.py; its top-level
            # importer gaps on accepted_submit_identity.py, chain_stages.py and
            # scheduler_state_types.py close on this directory rule. The
            # scheduler.py / file_orchestration_journal.py pairs ride their
            # stop-rule tuples. DB-free, 19 tests in ~7s.
            "tests/test_scheduler_held_reservation_block.py",
            # #2668: the four bind-reserved-job / held-listing suites. Their
            # importer gaps on `services.orchestrator` itself,
            # accepted_submit_identity.py, chain_types.py,
            # operator_reserved_bind.py, operator_action_listing.py,
            # operator_action_listing_held.py and scheduler_evidence_payload.py
            # close on this directory rule; the cli.py / scheduler.py /
            # scheduler_runtime.py / file_orchestration_journal.py pairs ride
            # their stop-rule tuples. DB-free, together ~20s.
            "tests/test_orchestrator_bind_reserved_job_cas.py",
            "tests/test_orchestrator_bind_reserved_job_cli.py",
            "tests/test_orchestrator_bind_reserved_job_lane.py",
            "tests/test_operator_action_listing_held_reservations.py",
            # #2675: the forcing-lane bind suites. Their importer gaps on
            # `services.orchestrator` itself, accepted_submit_identity.py,
            # chain_types.py, reconcile.py, operator_reserved_bind.py and
            # operator_action_listing_held.py close on this directory rule; the
            # cli.py / file_orchestration_journal.py pairs ride their stop-rule
            # tuples. DB-free, together ~6s.
            "tests/test_orchestrator_bind_reserved_job_forcing.py",
            "tests/test_orchestrator_bind_reserved_job_forcing_lane.py",
            # #2682: the forcing absence-exit suite. Its importer gaps on
            # `services.orchestrator` itself, accepted_submit_identity.py,
            # forcing_submit_identity.py, reconcile.py and reservation.py
            # close on this directory rule; the cli.py /
            # file_orchestration_journal.py pairs ride their stop-rule tuples.
            "tests/test_orchestrator_demote_reserved_job_forcing.py",
            # #2674: the shape (c) guard suite. Its importer gaps on
            # `services.orchestrator` itself and accepted_submit_identity.py close
            # on this directory rule; the file_orchestration_journal.py /
            # file_orchestration_migration.py pairs ride their stop-rule tuples.
            # DB-free, ~1s.
            "tests/test_file_journal_legacy_unversioned_reserved_guard.py",
            "tests/test_cli_cleanup_frontier.py",
            "tests/test_cli_publish_qdown.py",
            "tests/test_orchestrator_demote_cli_security.py",
            "tests/test_orchestrator_demote_core_cas.py",
            "tests/test_orchestrator_demote_projection_faults.py",
            "tests/test_orchestrator_demote_reclaim_lifecycle.py",
            "tests/test_file_orchestration_journal.py",
            "tests/test_file_orchestration_journal_read_cache.py",
            "tests/test_file_orchestration_migration.py",
            "tests/test_scheduler_journal_retention_planning.py",
            "tests/test_scheduler_journal_retention_archive.py",
            # #1943/#1944: the journal-root authority and job-id scope census
            # suites top-level-import `services.orchestrator` itself plus the
            # two new owner modules (journal_root_authority.py,
            # journal_scope_census.py) and operator_reserved_demotion.py /
            # scheduler_config/db_free.py — modules no stop rule owns, so the
            # directory rule is where those importer gaps close. Both are
            # orchestrator-journal suites; measured together, 50 tests in 1.50s.
            "tests/test_scheduler_journal_root_authority.py",
            "tests/test_scheduler_journal_scope_census.py",
            # #1955/#1953: the lane-adoption and full-tree budget suites
            # top-level-import `services.orchestrator` itself plus
            # journal_root_authority.py (the seam under test) and
            # chain_types.py / chain_runtime_utils.py (the terminal-status sets
            # the blocked sentinel must stay outside of) — modules no stop rule
            # owns, so the directory rule is where those importer gaps close.
            # Their other importer pairs (cli.py, file_orchestration_journal.py,
            # file_orchestration_migration.py) are stop-rule owned and ride
            # THEIR sites, per this rule's #1455 note above. Both are
            # orchestrator-journal suites; measured together, 87 tests in 2.07s.
            "tests/test_journal_root_lane_adoption.py",
            "tests/test_file_journal_full_tree_budget_contract.py",
            # #1555/#1768: the operator re-entry confirmation suite
            # top-level-imports `services.orchestrator` itself plus
            # journal_root_authority.py (the refusal message it pins) — modules
            # no stop rule owns, so the directory rule is where those two
            # importer gaps close. Its other importer pairs (cli.py,
            # scheduler.py, file_orchestration_journal.py) are stop-rule owned
            # and ride THEIR sites, per this rule's #1455 note above. This route
            # is also the only one that reaches the suite for a PR touching its
            # own subject module, services/orchestrator/operator_reentry_
            # confirmation.py, which the suite drives through the CLI rather
            # than importing. DB-free, 23 tests in 46.71s.
            "tests/test_operator_reentry_confirmation.py",
            # #2401: the newest-truth terminal-skip suite top-level-imports
            # `services.orchestrator` itself (via `scheduler`), a module no stop
            # rule owns, so the directory rule is where that importer gap closes.
            # Its `scheduler.py` pair is stop-rule owned and rides THAT site
            # (SCHEDULER_IMPORTER_TESTS), per this rule's #1455 note above; its
            # subject modules route it through their own per-file rows. DB-free,
            # 21 tests in ~10s.
            "tests/test_scheduler_terminal_recency.py",
            # #2453: the root-check loop-convergence suite's subject is
            # scheduler_runtime_roots.py::_scheduler_root_check, and it pins the
            # upstream normaliser's product from scheduler_config/path_modes.py; it
            # also top-level-imports `services.orchestrator` itself. None of the
            # three is stop-rule owned, so this directory rule is where those
            # importer gaps close; its scheduler.py pair rides
            # SCHEDULER_IMPORTER_TESTS. DB-free, 51 tests in ~0.4s.
            "tests/test_scheduler_root_check_loop_convergence.py",
            # #2397: the §8.7 identity-authority suite top-level-imports
            # `services.orchestrator` itself, scheduler_candidates.py (the
            # journal-predecessor quarantine it drives), scheduler_discovery.py
            # (the discovery-side §8.7 scoring it asserts through) and
            # scheduler_state_types.py (the decision type it builds) — none
            # stop-rule owned, so those four importer gaps close here, the same
            # disposition the re-entry confirmation suite above uses. Its journal
            # pair is stop-rule owned and rides FILE_ORCHESTRATION_JOURNAL_
            # IMPORTER_TESTS. DB-free, 7 tests in ~13s.
            "tests/test_quarantine_identity_authority.py",
            # #2396/#2407/#2408: the forecast-restart guard suite top-level-imports
            # only scheduler_candidates.py (the terminal-skip dispatch and repair
            # policy it drives) and scheduler_state_types.py — neither stop-rule
            # owned — so this directory rule is its route. Its scheduler-suite
            # helpers are imported function-locally. DB-free, 10 tests in ~3s.
            "tests/test_quarantine_forecast_restart_guards.py",
            # #2404: the retry-mint-floor suite drives the strict warm-start
            # budget (scheduler_candidates.py), the chain's cycle-stage mint
            # (chain_forecast_execution.py), the shared floor field
            # (retry_identity.py) and the cohort reconcile write
            # (file_orchestration_journal.py) end to end. All its imports are
            # function-local, so no importer derivation can reach it; this
            # directory rule is its route. DB-free, 6 tests in ~3s.
            "tests/test_retry_mint_floor.py",
            # #2542 / #2557 / #2539: the per-member cohort charge, hydro-attempt
            # triage and run-manifest schema contract suites drive
            # chain_forecast_orchestrator_cycle.py (the reservation write),
            # retry_identity.py (the charge rule), accepted_submit_identity.py
            # and the real orchestrate_cycle; all their imports are
            # function-local, so this directory rule is their route. #2546: the
            # convert-members suite's top-level importer gaps on
            # accepted_submit_identity.py, chain_types.py,
            # forcing_submit_identity.py, scheduler_state_types.py and the
            # package __init__ close here; its scheduler.py and
            # file_orchestration_journal.py pairs ride their stop-rule tuples.
            # DB-free, together ~9s.
            "tests/test_cohort_member_charge.py",
            "tests/test_convert_cohort_members.py",
            "tests/test_hydro_run_attempt_triage.py",
            "tests/test_run_manifest_schema_contract.py",
            # #2393/#1845/#2416/#2394: the cross-stage state-residue suite drives
            # the forecast chain stage loop (chain_forecast_execution.py,
            # chain_forecast_cycle.py, chain_forecast_orchestrator_cycle.py), the
            # cohort restart read (chain_runtime_utils.py) and the run-manifest
            # restart write (scheduler_candidate_manifest.py) -- none stop-rule
            # owned and named after none of them, so this directory rule is its
            # route. DB-free, sub-second.
            "tests/test_chain_cross_stage_state.py",
            # #2387: the read-blocked sentinel's chain consumer end. The suite
            # drives `chain_forecast_execution._retry_job_for_stage_result`
            # through its real caller `_schedule_cycle_stage_retry`
            # (chain_forecast_orchestrator_cycle.py) to prove the classified
            # refusal precedes `handle_failed_job` -- neither module is
            # stop-rule owned and it is named after neither, so this directory
            # rule is its route for them. Its journal pair IS stop-rule owned
            # and rides FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS, per this
            # rule's #1455 note above. Every import is function-local, so no
            # importer derivation can reach it. DB-free, 17 tests in ~1s.
            "tests/test_file_journal_read_blocked_consumers.py",
            # #2566/#2567/#2600: the runtime-root blocked-read suite (subject:
            # retry.py's candidate batch + evidence renderer, imported
            # function-locally) and the manual-retry restart-point suite (subject:
            # scheduler_state_failure.py / scheduler_state_decision.py /
            # scheduler_candidates.py; top-level importer gaps on the package
            # __init__ and scheduler_candidate_manifest.py close here). Their
            # scheduler.py / file_orchestration_journal.py pairs ride the stop-rule
            # tuples. DB-free, together ~6s.
            "tests/test_retry_runtime_root_blocked_reads.py",
            "tests/test_manual_retry_failed_stage_restart.py",
            # #1186: the operator-action listing suite top-level-imports
            # `services.orchestrator` itself and scheduler_evidence_payload.py
            # (it writes every size-fallback fixture through the REAL
            # `bounded_evidence_payload`, so the listing's undecidability rule
            # rests on that writer's shape) — neither is stop-rule owned, so the
            # directory rule is where those two importer gaps close. Its third
            # importer pair, cli.py, is stop-rule owned and rides THAT site, per
            # this rule's #1455 note above. This route is also the only one that
            # reaches the suite for a PR touching its own subject module,
            # services/orchestrator/operator_action_listing.py, which the suite
            # drives through the CLI rather than importing. DB-free, 75 tests in
            # 0.34s.
            "tests/test_operator_action_listing.py",
            # #1905/#2402: the evidence-size decidability suite is the oracle for
            # the non-blocking summary tier and the bounded breaker-released
            # source-cycle projection. It imports NOTHING at module scope (every
            # `services.*`/`tests.*` import is function-local, so the frozen
            # `tests.test_production_scheduler` importer closure does not move),
            # which means no importer derivation can reach it: this directory
            # rule is its route for scheduler_evidence_payload.py,
            # scheduler_evidence.py, scheduler_discovery.py, scheduler_lease.py
            # and its reader subject operator_action_listing.py. Its sixth
            # module, scheduler_runtime.py, is stop-rule owned and rides THAT
            # site, per this rule's #1455 note above. DB-free, ~20 tests in ~20s
            # (one real breaker `run_once()` pass, module-scoped).
            "tests/test_scheduler_evidence_decidability.py",
            # #2405: the reservation-lease suite, on the same footing and for the
            # same reason -- named after none of its modules and importing
            # nothing at module scope. This directory rule is its route for
            # scheduler_lease.py (the heartbeat touch), scheduler_evidence.py
            # (the reservation `lease` block) and operator_action_listing.py (the
            # orphan rule); scheduler_runtime.py rides the stop rule above.
            # DB-free, 15 tests in ~1s.
            "tests/test_operator_action_reservation_lease.py",
            # #2564: the heartbeat-mtime x evidence-retention cross suite, on the
            # same footing -- module-scope-import-free, so this directory rule is
            # its route for scheduler_evidence.py (the reservation writer),
            # scheduler_lease.py (the heartbeat touch) and
            # operator_action_listing.py (the reader). DB-free, 3 tests in ~0.3s.
            "tests/test_scheduler_evidence_retention_reservation_mtime.py",
            # #2582: the looping-workspace-root refusal suite for
            # scheduler_lease.py / scheduler_evidence.py, module-scope-import-free
            # for the same reason. DB-free, 8 tests in ~0.3s.
            "tests/test_scheduler_lease_loop_root.py",
            # #2442: the writer-bound status closure pin. It reads five writer
            # sources as TEXT (it must not import them), so it has no importer
            # derivation at all: this directory rule is its route for
            # scheduler_candidate_runtime.py, scheduler_evidence_proofs.py,
            # scheduler_candidate_execution_evidence.py,
            # scheduler_evidence_payload.py and the reader whose exit code the
            # whitelist decides, operator_action_listing.py. Its fifth writer,
            # scheduler_runtime.py, is stop-rule owned and rides THAT site, per
            # this rule's #1455 note above. DB-free, 4 tests in ~0.3s.
            "tests/test_operator_action_status_closure.py",
            # #1581 (+#1999): the hydro-status parity lock top-level-imports
            # eight modules of this package plus `services.orchestrator` itself,
            # so nine importer pairs land here. Seven close on this list
            # (__init__.py, chain_forecast_trigger.py, chain_repository.py,
            # scheduler_state_decision.py, scheduler_state_failure.py,
            # scheduler_state_manual_retry.py, scheduler_state_types.py);
            # chain.py and file_orchestration_journal.py are stop-rule owned
            # and are extended at THEIR sites, per this rule's #1455 note
            # above. 9 tests in 0.29s, DB-free, so a rule rather than a
            # rule-gap exclusion.
            "tests/test_hydro_status_set_parity.py",
            "tests/test_live_monitoring.py",
            "tests/test_monitoring_api.py",
            "tests/test_pipeline_persistence.py",
            # #1102: the publisher corpus is physically partitioned; the broad
            # orchestrator rule must select every collectible partition so a
            # scheduler_file_providers change never blinds targeted CI to moved
            # cases. Three of the seven top-level-import that module directly
            # (package-contexts, manifest-audit, refresh-lane); the other four
            # reach the same providers through `registry_script`, and the rule
            # carried the undivided monolith before the split.
            *PUBLISH_SCHEDULER_REGISTRY_TESTS,
            "tests/test_scheduler_registry_capacity.py",
            "tests/test_reconcile_sacct_parse.py",
            "tests/test_replay_lineage.py",
            "tests/test_retention.py",
            # #1872: the retention corpus is physically partitioned; the
            # production owner rule must select every collectible partition so
            # a retention change never blinds targeted CI to moved cases.
            # #2238 added the fifth partition — the sixth retention suite,
            # counting #1872's independent frontier suite, which is not itself
            # a partition. It is the requirement oracle for retention.py's
            # copyback-mutex lane (one batch-lock acquisition per removed tree
            # under the shared copyback root, a pass-level wait budget,
            # `failed` rather than abort on contention), and it
            # top-level-imports `services.orchestrator` itself, so BOTH
            # directory members' importer gaps close here — cli.py's third one is
            # stop-rule owned and rides THAT site, per this rule's #1455 note
            # above. DB-free, local, 25 tests in 5.27s, so a rule rather than a
            # rule-gap exclusion.
            # #2259 partitioned it into these two halves and deleted the
            # monolith; this rule is where `services/orchestrator/retention.py`
            # and the extracted `retention_copyback_mutex.py` both reach them.
            *RETENTION_COPYBACK_MUTEX_TESTS,
            # #2262: retention.py's typed lock-failure signal and the
            # compaction that must keep it (scheduler_evidence_payload.py rides
            # this rule too). DB-free, local, sub-second.
            "tests/test_retention_copyback_lock_signal.py",
            "tests/test_retention_extra_roots.py",
            "tests/test_retention_frontier.py",
            "tests/test_retention_pipeline_frontier.py",
            "tests/test_retention_root_admission.py",
            # #2596: the unexpandable-root suite top-level-imports cli.py,
            # retention.py and `services.orchestrator` itself; all three importer
            # pairs close here. DB-free, 9 tests in ~0.3s.
            "tests/test_retention_unexpandable_roots.py",
            "tests/test_retry.py",
            "tests/test_retry_cancel_consistency.py",
            "tests/test_run_identity.py",
            "tests/test_run_tree_copyback.py",
            # #2237: run_tree_copyback.py's backup lifecycle oracle. Sub-second.
            "tests/test_run_tree_copyback_backup_lifecycle.py",
            "tests/test_scheduler_backfill_predecessor.py",
            # #1101 partitioned the 9614-line refresh monolith into fifteen
            # suites and deleted it; this directory rule was one of the eight
            # sites that named it by hand. The whole corpus rides here because
            # the rule's subject is every orchestrator module the runner calls.
            *SCHEDULER_REFRESH_TESTS,
            "tests/test_scheduler_generation.py",
            # #1735: the lineage resolver suite imports `services.orchestrator`
            # (hence `__init__.py`, which has no same-name suite of its own), so
            # the directory rule is where its importer gap closes. It IS an
            # orchestrator suite and it is sub-second, so it rides the directory
            # list rather than earning a narrow rule.
            "tests/test_scheduler_lineage.py",
            "tests/test_scheduler_timing.py",
            "tests/test_source_cycle_raw_manifest.py",
            "tests/test_source_scoped_dispatch.py",
            "tests/test_state_clone.py",
            "tests/test_variant_activation_cutover.py",
            "tests/test_e2e_m3.py",
        ),
    ),
    PathTestRule(
        "services/slurm_gateway/**",
        (
            "tests/test_gateway.py",
            # #1809: the 14k-line gateway-reconcile monolith was physically
            # partitioned into flat responsibility modules; every collectible
            # partition replaces the deleted single target here, sorted.
            "tests/test_gateway_reconcile_comment_accounting.py",
            "tests/test_gateway_reconcile_comment_capability.py",
            "tests/test_gateway_reconcile_comment_sacct_bounds.py",
            "tests/test_gateway_reconcile_file_cohort_authority.py",
            "tests/test_gateway_reconcile_file_cohort_comment.py",
            "tests/test_gateway_reconcile_file_cohort_identity.py",
            "tests/test_gateway_reconcile_file_cohort_projection.py",
            "tests/test_gateway_reconcile_file_submit_barrier.py",
            "tests/test_gateway_reconcile_grace_guard.py",
            "tests/test_gateway_reconcile_idempotency_barrier.py",
            "tests/test_gateway_reconcile_identity_invariants.py",
            "tests/test_gateway_reconcile_identity_release.py",
            "tests/test_gateway_reconcile_inflight_identity.py",
            "tests/test_gateway_reconcile_inventory.py",
            "tests/test_gateway_reconcile_master_transitions.py",
            "tests/test_gateway_reconcile_reservation_lifecycle.py",
            "tests/test_gateway_reconcile_round10.py",
            "tests/test_gateway_reconcile_store_reset.py",
            "tests/test_gateway_reconcile_writer_launch.py",
            "tests/test_gateway_reconcile_writer_prepare.py",
            "tests/test_gateway_reconcile_writer_quiescence.py",
            "tests/test_gateway_reconcile_writer_receipts.py",
            "tests/test_gateway_reconcile_writer_rollforward.py",
            "tests/test_slurm_gateway_app.py",
            "tests/test_slurm_gateway_auth.py",
            "tests/test_slurm_gateway_auth_fullmount.py",
            "tests/test_slurm_gateway_auth_client.py",
            "tests/test_slurm_gateway_auth_deployment.py",
            # #1684 large-file guard repair: the auth suite was physically
            # partitioned; every partition replaces the single target.
            "tests/test_slurm_route_contract.py",
            "tests/test_slurm_route_security_contract.py",
            "tests/test_real_slurm_gateway.py",
            "tests/test_slurm_array_contract.py",
            "tests/test_job_array.py",
        ),
    ),
    # #1455: three narrow rules rather than four more entries on the directory
    # list above. Only app.py, config.py and gateway.py have these importer
    # gaps, and putting them on the directory rule would also change what a
    # `services/slurm_gateway/real_backend.py` PR selects — the exact output
    # issue #1455's own Verification command pins, and a surface PR #1486 just
    # closed. Narrow keeps that output byte-identical.
    PathTestRule(
        "services/slurm_gateway/app.py",
        (
            "tests/test_role_boundary_static.py",
            "tests/test_monitoring_api.py",
        ),
    ),
    PathTestRule(
        "services/slurm_gateway/config.py",
        (
            "tests/test_role_boundary_static.py",
            "tests/test_m24_gateway_proof.py",
        ),
    ),
    PathTestRule(
        "services/slurm_gateway/gateway.py",
        (
            "tests/test_monitoring_api.py",
            "tests/test_retry_cancel_consistency.py",
        ),
    ),
    PathTestRule(
        # One-hop extension of the guarded-module closure (#1455): three tracked
        # non-test modules import real_backend at file level
        # (services/orchestrator/reconcile.py,
        # services/production_closure/slurm_validation.py,
        # services/slurm_gateway/mock_backend.py), and their own non-gated
        # top-level importer suites were not selected by the
        # `services/slurm_gateway/**` rule above. This rule is deliberately
        # narrow — only a real_backend.py PR pays the extra ~20s; every other
        # slurm_gateway path keeps today's seven targets. The set is DERIVED
        # from the tracked tree by tests/test_select_ci_tests.py, never frozen
        # there, so a new one-hop importer suite reddens the guard.
        # The #1809 gateway-reconcile partitions are one-hop members too but
        # already ride the `services/slurm_gateway/**` rule; they are not
        # repeated here.
        # #1564: the split demote suites are one-hop members via
        # services/orchestrator/reconcile.py (see #1455 above) and are not
        # covered by either slurm_gateway rule, so they join this narrow rule.
        "services/slurm_gateway/real_backend.py",
        (
            "tests/test_production_e2e_validation.py",
            "tests/test_production_met_validation.py",
            "tests/test_production_object_store_validation.py",
            "tests/test_production_ops_validation.py",
            "tests/test_production_readiness_validation.py",
            "tests/test_production_scale_validation.py",
            "tests/test_production_slurm_validation.py",
            "tests/test_reconcile_sacct_parse.py",
            "tests/test_forcing_submit_ambiguity.py",
            "tests/test_orchestrator_demote_cli_security.py",
            "tests/test_orchestrator_demote_core_cas.py",
            "tests/test_orchestrator_demote_projection_faults.py",
            "tests/test_orchestrator_demote_reclaim_lifecycle.py",
            # #2675: the forcing bind lane suite is a one-hop member the same
            # way (it top-level-imports services/orchestrator/reconcile.py for
            # restart reconcile and inflight projection). DB-free, ~1s.
            "tests/test_orchestrator_bind_reserved_job_forcing_lane.py",
            # #2682: the forcing absence-exit suite is a one-hop member the same
            # way (top-level reconcile.py import for the absence grace).
            "tests/test_orchestrator_demote_reserved_job_forcing.py",
        ),
    ),
    PathTestRule(
        # #1455: test_cli_publish_qdown.py top-level-imports the package and
        # publisher.py — the directory's only two importer gaps, both closed by
        # one 1.5s target, so the directory list is the right home.
        "services/tile_publisher/**",
        (
            "tests/test_tile_publisher.py",
            "tests/test_forcing_copyback_backfill.py",
            # #2236: forcing_copyback_backfill.py's `--apply` lock-scope oracle.
            "tests/test_forcing_copyback_backfill_lock_scope.py",
            "tests/test_static_serving.py",
            "tests/test_cli_publish_qdown.py",
            # #1442: publisher.py (B) and forcing_copyback_backfill.py (C) are
            # both in the zero-text-identity oracle's register, and both ride
            # this directory rule. The four suites above assert behaviour, not
            # the SQL identity shape, so the oracle joins the directory list
            # rather than getting two per-file entries for the same targets.
            "tests/test_river_ts_text_identity_cleanup.py",
            # I1 #1980 river_ts_render: this file's river read templates are
            # registered in tests/river_ts_template_registry.py and rendered per
            # timeseries store by the whole SQL-shape oracle group, so a layout
            # or predicate edit here must run all of it — the golden equivalence
            # oracle included, which is what proves the edit was behaviour-free.
            # (One block per site rather than one new rule per path: the
            # selector's own duplicate-pattern guard forbids a second
            # PATH_TEST_RULES entry for an already-owned module.)
            *SQL_SHAPE_ORACLE_TESTS,
            *RIVER_TS_READ_PATH_SURROGATE_KEYS_INTEGRATION_TESTS,
        ),
    ),
    PathTestRule(
        "services/tiles/mvt.py",
        (
            # #2074: the RETAINED BASE path of the partitioned API-contract
            # corpus, not the whole corpus. It is the only partition that imports
            # `apps.api.routes.hydro_display*` and asserts the tile routes'
            # published shape, so it is the one this module's derived closure
            # requires; the other two never reach a tile.
            "tests/test_api_contract.py",
            "tests/test_display_publish_status_only.py",
            "tests/test_migrations.py",
            "tests/test_openapi_drift.py",
            # The #1341 surrogate-key / transitional-pushdown shape pins live
            # here; an mvt.py diff that quietly drops a predicate pairing must
            # not reach CI green without them.
            "tests/test_river_ts_read_path_surrogate_keys.py",
            # #1597: the eight below are DERIVED by the #1455 importer-closure
            # guard in tests/test_select_ci_tests.py
            # (test_guarded_module_rules_cover_their_non_gated_importer_closure,
            # now covering services.tiles.mvt), not hand-curated — the guard
            # owns the required set, so a future importer reds it here
            # instead of silently falling out of the PR lane. Of the eight
            # added entries, four are direct non-gated importers that assert
            # the postgis_tile_sql() output shape (hhe_mvt_binding,
            # hydro_display_mvt_scaling, the two node27_timeseries_compression
            # suites); the three direct_grid_display_cutover_* suites plus
            # test_openapi_31_contract.py are the one-hop additions via
            # apps/api/routes/hydro_display.py and
            # apps/api/openapi_patching.py respectively. The two
            # `integration`-marked importers stay out per the #1447 ruling:
            # they auto-skip without NHMS_RUN_INTEGRATION (tests/conftest.py),
            # so requiring them buys constant skips and zero assertions.
            # #2490: the national identity probe is now three partitions
            # (`test_mvt_national_identity_probe{,_cycles,_digest}_integration.py`),
            # each with the module-level `integration` mark, so all three stay
            # out under the same ruling.
            # #2527: the flip suite is two partitions; the row carries both
            # (see DIRECT_GRID_DISPLAY_CUTOVER_FLIP_TESTS for why both).
            *DIRECT_GRID_DISPLAY_CUTOVER_FLIP_TESTS,
            "tests/test_direct_grid_display_cutover_history.py",
            "tests/test_direct_grid_display_cutover_model_resolution.py",
            "tests/test_hhe_mvt_binding.py",
            # #2074: the hydro-display MVT suite is now nine partitions and ALL
            # of them are in this closure (direct importers, plus
            # `..._catalog_cache.py` one hop through
            # apps/api/routes/hydro_display.py). Listing the tuple rather than
            # the nine literals keeps this rule and the facade rule below from
            # drifting apart.
            *HYDRO_DISPLAY_MVT_SCALING_TESTS,
            # #2121-A: real QueuePool admission regression imports the shared
            # TileInput/TileResponse/cache-key contract at module scope.
            "tests/test_display_mvt_cold_admission.py",
            # #2032: guard-derived, not hand-curated — both new suites import
            # services.tiles.mvt at file level. The lock suite drives
            # tile_generation_lock / _open_live_lock_file directly; the
            # retention suite pins its three path regexes against
            # _file_cache_path / _file_cache_lock_path / _write_file_cache, so
            # a layout change here must red there rather than after merge.
            # Neither suite imports apps.api.routes.hydro_display, so that
            # rule is deliberately left alone. #2490: the retention suite is
            # three partitions and the row carries all of them.
            "tests/test_mvt_tile_generation_lock.py",
            *NODE27_MVT_CACHE_RETENTION_TESTS,
            # #2716: guard-derived — the DB tile-cache write-privilege suite
            # imports services.tiles.mvt at file level and drives
            # build_raw_tile_response / build_tile_response through a recording
            # PostgreSQL-dialect session, so a change to the write gate here
            # must red there in the PR lane. It imports no
            # apps.api.routes.hydro_display module, so that rule is left alone.
            "tests/test_mvt_db_cache_write_privilege.py",
            # #2550: guard-derived — the basemap proxy suite imports
            # apps.api.routes.basemap, which takes MVT_FILE_CACHE_DIR_ENV from
            # this module: both caches share one root, so renaming the env here
            # must red the proxy's cache-path assertions in the PR lane.
            "tests/test_basemap_proxy.py",
            # #2013: same guard-derived provenance — the prewarm suite imports
            # NATIONAL_DISCHARGE_VALID_TIME_STRIDE_HOURS from services.tiles.mvt
            # at file level (issue #2013's planned-envelope assertion steps the
            # published valid-time grid with it, so a stride change reds the
            # envelope count), which makes it a DIRECT non-gated importer here.
            # Its own entry, not one of the "eight below" above: the list is
            # sorted, so it is INTERPOSED among them by position only and the
            # #1597 census stays true.
            "tests/test_node27_mvt_prewarm.py",
            # #2156 (D-2): guard-derived, not hand-curated — the run/river-
            # network geometry-identity suite imports TileInput, cache_key and
            # display_ready_run from services.tiles.mvt at file level, so it is
            # a DIRECT non-gated importer here. Unlike the #2032 pair above it
            # ALSO imports apps.api.routes.hydro_display at file level (the
            # _river_network_source_version / _run_row / _run_source_version
            # SQL and the route cache keys), so it sits on that rule too. Its
            # own entry, so the #1597 "eight below" census stays true.
            "tests/test_mvt_run_and_river_network_geometry_identity.py",
            # #2017: the coordinate-budget harness imports
            # postgis_tile_sql / collection_coordinate_limit / MVT_MAX_COORDINATES
            # from services.tiles.mvt at module level, so its suite is a DIRECT
            # non-gated importer of this guarded module too.
            "tests/test_node27_river_tile_coordinate_evidence.py",
            "tests/test_node27_timeseries_compression_benchmark.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_openapi_31_contract.py",
            # #1684 large-file guard repair: the 3.1-contract security half is
            # a one-hop importer via tests/test_openapi_31_contract.py, so it
            # joins the derived closure here (and in the hydro_display rule).
            "tests/test_slurm_gateway_openapi_security.py",
            # #1714: same guard-derived provenance as the #1597 batch above,
            # not hand-curated — a one-hop importer reached through
            # apps/api/routes/hydro_display.py. Kept as its own entry so the
            # "eight below" census stays true.
            "tests/test_node27_connection_attribution.py",
            "tests/test_node27_connection_attribution_delegated.py",
            # #1704: same guard-derived provenance — the API error-logging
            # suite imports apps/api/routes/hydro_display.py at file level,
            # which imports services.tiles.mvt, so it is a one-hop importer
            # here (and a DIRECT importer on the hydro_display rule below).
            "tests/test_api_errors_logging.py",
            # #2010: same guard-derived provenance — the precip suite imports
            # services.tiles.mvt (layer_metadata / MVT_FILE_CACHE_DIR_ENV) at
            # file level, so it is a DIRECT non-gated importer here.
            "tests/test_precip_overlay.py",
            # I1 #1980 river_ts_render: this file's river read templates are
            # registered in tests/river_ts_template_registry.py and rendered per
            # timeseries store by the whole SQL-shape oracle group, so a layout
            # or predicate edit here must run all of it — the golden equivalence
            # oracle included, which is what proves the edit was behaviour-free.
            # (One block per site rather than one new rule per path: the
            # selector's own duplicate-pattern guard forbids a second
            # PATH_TEST_RULES entry for an already-owned module.)
            *SQL_SHAPE_ORACLE_TESTS,
        ),
    ),
    # The other two #1341 switched surfaces. Both are covered by broad rules
    # (packages/common/** and the API route rules) that do not include the
    # read-path shape pins, so without these entries a diff that drops a
    # pushdown pairing or reintroduces a text fact predicate in either file
    # reaches CI green unchallenged.
    PathTestRule(
        # #1672: hydro_display.py joins GUARDED_MODULE_CLOSURES and its rule is
        # extended to the current mechanically derived direct UNION one-hop
        # non-gated importer closure (tests/test_select_ci_tests.py derives the
        # required set from the tracked tree, never frozen). The three cutover
        # suites (four files since #2527 split the flip suite in two; only its
        # `_atomic` partition is a derived importer), display status-only, HHE/MVT, node-27 compression and
        # attribution suites are direct importers; the 3.1-contract and
        # runtime-mode suites are the one-hop contributions via
        # apps/api/openapi_patching.py and apps/api/route_registry.py. The two
        # `integration`-marked importers (test_display_coverage_residual_debt_
        # integration.py, test_mvt_national_identity_probe_integration.py and,
        # since #2490, its `_cycles_` / `_digest_` partitions) stay
        # out per the #1447 ruling — they auto-skip in the PR lane. The #1341
        # read-path shape pin rides along as an exact at-site entry.
        # #2026: the pattern is a glob over the whole `hydro_display*` family.
        # The facade was split into hydro_display_{constants,models,instants,
        # catalog,identity,postgis}.py and the SQL, the budget signals and the
        # layer catalog now live in the owner modules, so an exact-path entry
        # would leave an owner-module-only diff selecting nothing here. One
        # entry, not seven: the selector's duplicate-pattern guard forbids a
        # second PATH_TEST_RULES entry for an already-owned module, and every
        # suite below imports the facade, which imports all six owners.
        "apps/api/routes/hydro_display*.py",
        (
            # #2074: the RETAINED BASE path. Guard-derived, like every other
            # entry here — it is the API-contract corpus's only direct module-
            # scope importer of this family (it patches
            # `hydro_display_catalog` and rebinds two facade names), which is
            # also why the `apps/api/routes/hydro_display_catalog.py` row of
            # GUARDED_MODULE_CLOSURES still anchors on this exact path.
            "tests/test_api_contract.py",
            # #1704: guard-derived, not hand-curated — the API error-logging
            # suite imports get_hydro_display_session from this module at file
            # level, so it is a direct non-gated importer.
            "tests/test_api_errors_logging.py",
            # #2527: the flip suite is two partitions; the row carries both.
            # Only `_atomic` is a derived direct importer of this family; the
            # `_mvt_set` partition rides along as the model of the predicate
            # `_atomic` locks (see DIRECT_GRID_DISPLAY_CUTOVER_FLIP_TESTS).
            *DIRECT_GRID_DISPLAY_CUTOVER_FLIP_TESTS,
            "tests/test_direct_grid_display_cutover_history.py",
            "tests/test_direct_grid_display_cutover_model_resolution.py",
            "tests/test_display_publish_status_only.py",
            "tests/test_hhe_mvt_binding.py",
            # #2074: seven of the nine hydro-display MVT partitions. The
            # `..._discovery.py` and `..._national_sql.py` two import no
            # apps.api.routes module, so they are outside this family's derived
            # closure and stay routed by the services/tiles/mvt.py rule above.
            *HYDRO_DISPLAY_MVT_SCALING_FACADE_TESTS,
            # #2156 (D-2): guard-derived — the run/river-network geometry-
            # identity suite imports this module at file level and runs the real
            # SQL of _river_network_source_version / _run_row /
            # _run_source_version plus the route cache keys, so it is a DIRECT
            # non-gated importer (and on the services/tiles/mvt.py rule too).
            "tests/test_mvt_run_and_river_network_geometry_identity.py",
            "tests/test_display_mvt_cold_admission.py",
            "tests/test_node27_connection_attribution.py",
            "tests/test_node27_connection_attribution_delegated.py",
            # #2017: the checked-in 2.4/2.5 coordinate-budget harness imports
            # apps.api.routes.hydro_display._postgis_tile_params at module level
            # (the whole point of the harness is that the binds match production
            # by construction), and its suite imports the harness, so the suite
            # is a DIRECT non-gated importer here. Without this entry a
            # hydro_display-only diff could change the bind dict and leave the
            # harness asserting the old shape, green in the PR lane.
            "tests/test_node27_river_tile_coordinate_evidence.py",
            "tests/test_node27_timeseries_compression_benchmark.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_openapi_31_contract.py",
            # #1684 large-file guard repair: the 3.1-contract security half is
            # a one-hop importer via tests/test_openapi_31_contract.py.
            "tests/test_slurm_gateway_openapi_security.py",
            "tests/test_openapi_drift.py",
            # #2464: NOT guard-derived (the import is function-local on purpose,
            # so the suite is no importer edge of the display stack). It holds
            # the pin tying the coverage-freshness lane's allowlist to the
            # `source` annotation of the three display routes; without this
            # entry a route-only diff that inlines its own `Literal` again would
            # leave the lane observing a set the routes no longer serve, green
            # in the PR lane.
            "tests/test_node27_coverage_freshness_alert.py",
            # #2010: guard-derived — the precip suite imports
            # apps/api/routes/hydro_display.py at file level (the shared
            # `Rfc3339Instant` / seconds-precision gate and the DB-dependency
            # assertion), so it is a DIRECT non-gated importer.
            "tests/test_precip_overlay.py",
            "tests/test_river_ts_read_path_surrogate_keys.py",
            "tests/test_runtime_mode.py",
            # I1 #1980 river_ts_render: this file's river read templates are
            # registered in tests/river_ts_template_registry.py and rendered per
            # timeseries store by the whole SQL-shape oracle group, so a layout
            # or predicate edit here must run all of it — the golden equivalence
            # oracle included, which is what proves the edit was behaviour-free.
            # (One block per site rather than one new rule per path: the
            # selector's own duplicate-pattern guard forbids a second
            # PATH_TEST_RULES entry for an already-owned module.)
            *SQL_SHAPE_ORACLE_TESTS,
        ),
    ),
    PathTestRule(
        # #2010: the precipitation raster service's own tree. `services/**` has
        # no broad rule, so before this entry a mirror/window/render/cache diff
        # selected only the same-name derivation (which finds nothing: there is
        # no tests/test_mirror.py) and reached the PR lane with zero assertions.
        # #2122: PR #2117 made scripts/node27_mvt_prewarm.py:57 import
        # services.precip.mirror.horizon_valid_times at module level, and
        # tests/test_node27_mvt_prewarm.py:17 imports that script at module
        # level, so the prewarm suite is a one-hop importer suite of this tree.
        # It is the sole holder of the clamped-first-valid-time / PNG-horizon
        # contract, asserted by
        # test_a_clamped_first_valid_time_is_warmed_from_that_entry_not_from_the_cycle
        # (referenced by case name, never by line range, per openspec/changes/
        # display-v2-national-timeline-precip-overlay/tasks.md:548), and
        # PRECIP_STEP_HOURS feeds that grid — before this target the PR lane
        # could not reach that oracle at all. It is NOT the only suite a step
        # flip reds: tests/test_precip_overlay.py, already selected by this
        # rule, discriminates on the step too (measured 31 failed at step 6).
        # The union is spelled in place rather than appended to
        # PRECIP_SURFACE_TESTS so the shared tuple (and with it the
        # apps/api/routes/precip.py rule below) does not inherit the prewarm
        # suite: a route-only diff must not pay for it.
        # #2191: scripts/node27_raw_retention.py imports
        # services.precip.constants.FILE_CACHE_DIR_ENV at module level and
        # tests/test_node27_raw_retention.py imports that script at module level,
        # so the raw-retention suite is a second one-hop importer suite of this
        # tree. It is not redundant with tests/test_precip_overlay.py: that suite
        # catches a first-order rename of the env name, but a CONSISTENT rename
        # that also updates its pin leaves the raw-retention suite's bare literal
        # stale — green in the PR lane, red only on master. Spelled in place for
        # the same shared-tuple reason as the prewarm suite.
        "services/precip/**",
        (*PRECIP_SURFACE_TESTS, "tests/test_node27_mvt_prewarm.py", "tests/test_node27_raw_retention.py"),
    ),
    PathTestRule(
        # #2010: the two public routes. `apps/api/**` below buys the three broad
        # API suites, none of which exercises a precip route; the exact entry
        # adds the behavioural oracle plus the public-contract suites. Exact path
        # beside the broad rule is the same shape the hydro_display.py rule uses.
        "apps/api/routes/precip.py",
        PRECIP_SURFACE_TESTS,
    ),
    PathTestRule(
        # #2550: the Tianditu basemap proxy route. Same shape as the precip
        # route rule: `apps/api/**` buys only the broad API suites, none of
        # which calls this route, so the exact entry adds its behavioural oracle
        # plus the public-contract suites its OpenAPI entry is pinned by.
        "apps/api/routes/basemap.py",
        (
            "tests/test_basemap_proxy.py",
            "tests/test_openapi_drift.py",
            "tests/test_openapi_31_contract.py",
            "tests/test_api_contract.py",
        ),
    ),
    PathTestRule(
        "services/production_closure/readonly_db_validation.py",
        READONLY_DB_VALIDATION_TESTS,
    ),
    PathTestRule(
        "services/production_closure/readonly_db_route_smoke.py",
        READONLY_DB_VALIDATION_TESTS,
    ),
    PathTestRule(
        "services/production_closure/__init__.py",
        C4_PRODUCTION_ACCEPTANCE_TESTS,
    ),
    PathTestRule(
        "services/production_closure/c4_production_acceptance.py",
        C4_PRODUCTION_ACCEPTANCE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "services/production_closure/c4_production_acceptance_io.py",
        C4_PRODUCTION_ACCEPTANCE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "scripts/node27_c4_production_acceptance.py",
        C4_PRODUCTION_ACCEPTANCE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_workload.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_workload_types.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_workload_query.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_workload_http.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_workload_plan.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_workload_measure.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_workload_io.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "scripts/node27_pgdata_workload.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        # #1455: the directory's 25 importer gaps collapse onto four suites, all
        # of which are production-closure suites that other rules happened to own
        # (real_backend.py, forcing_producer, the readonly-db script). The
        # `two_node_e2e_*` lane family in particular ALL points at
        # tests/test_two_node_e2e_evidence.py — one target closes thirteen
        # modules' gaps. Measured in the PR lane, not assumed from the name:
        # test_two_node_e2e_evidence.py runs 844 real assertions in ~2.5 min.
        "services/production_closure/**",
        (
            "tests/test_production_readiness_validation.py",
            "tests/test_production_ops_validation.py",
            # #1684 large-file guard repair: the ops-validation suite was
            # physically partitioned into auth/dependency/hardening modules;
            # every collectible partition replaces the single target so a
            # production_closure change never blinds targeted CI to moved
            # cases.
            "tests/test_slurm_gateway_ops_auth_evidence.py",
            "tests/test_slurm_gateway_ops_dependency_closure.py",
            "tests/test_slurm_gateway_ops_dependency_hardening.py",
            "tests/test_production_object_store_validation.py",
            # #1911: every historical-facade and split-owner change must run the
            # direct compatibility contract oracle, not merely the production
            # behavior suite that happens to exercise portions of the facade.
            "tests/test_object_store_validation_facade_contract.py",
            "tests/test_production_slurm_validation.py",
            "tests/test_production_scale_validation.py",
            "tests/test_production_e2e_validation.py",
            "tests/test_production_met_validation.py",
            *READONLY_DB_VALIDATION_TESTS,
            "tests/test_two_node_e2e_evidence.py",
            # #2484 D3: the shared cycle-hour helper's suite also pins the
            # two-node facade's private-name bindings, so it imports the package
            # and two_node_e2e_evidence and must ride the directory rule.
            "tests/test_identity_matching.py",
        ),
    ),
    PathTestRule(
        "packages/common/object_store.py",
        (
            "tests/test_object_store_roots.py",
            "tests/test_storage.py",
            # #2670: `ObjectStoreReadLimitExceededError` (the over-limit vs
            # missing discriminator) is defined here, and its only assertion
            # sits in the SHUD runtime suite, which no import derivation
            # reaches from this path. One node id, not the whole suite.
            "tests/test_shud_runtime.py::test_object_store_limited_read_tells_over_limit_from_missing",
        ),
    ),
    PathTestRule(
        # #2260: the retention copyback mutex's lock semantics live here, yet no
        # rule named this module, so a guard-only diff reached only its same-name
        # suite plus the packages/common/** supplemental routes and never the
        # mutex suite. Path-exact with neither flag: no stop rule matches this
        # path, so the existing selections accumulate unchanged beside it.
        "packages/common/copyback_guard.py",
        (
            # #2259: both halves of the split copyback-mutex partition.
            *RETENTION_COPYBACK_MUTEX_TESTS,
            # #2252/#2262 (harden-copyback-mutex-residuals): the guard gained the
            # `posix` primitive, the non-finite timeout refusal, the budget
            # constant and the lock-failure classifier. Its own primitive suite
            # and every lane suite whose contract reads those names ride here, so
            # a guard-only diff reaches each lane's requirement oracle.
            "tests/test_copyback_guard_primitive.py",
            "tests/test_forcing_copyback_backfill_lock_scope.py",
            "tests/test_node27_raw_retention_copyback_mutex.py",
            "tests/test_retention_copyback_lock_signal.py",
            "tests/test_run_tree_copyback_backup_lifecycle.py",
        ),
    ),
    PathTestRule(
        # #2259: the extracted copyback-mutex owner. Its same-name derivation
        # points at the deleted monolith, so without this row its only route is
        # the broad `services/orchestrator/**` rule -- which any later stop rule
        # on this path would shadow without a sound. Path-exact with neither
        # flag, so that directory selection still accumulates beside it.
        RETENTION_COPYBACK_MUTEX_OWNER_PATH,
        RETENTION_COPYBACK_MUTEX_OWNER_TESTS,
    ),
    PathTestRule(
        # #2252: the node-27 canonical lane's copyback-mutex oracle. Not a
        # same-name suite, so derivation never reaches it. Path-exact with
        # neither flag, so the same-name suite still accumulates beside it.
        "scripts/node27_raw_retention.py",
        ("tests/test_node27_raw_retention_copyback_mutex.py",),
    ),
    PathTestRule(
        # I1 #1980 river_ts_render: the shared per-store renderer. It owns the
        # text-identity vocabulary and the table-scoped attribution every oracle
        # in the group now imports, so a diff to it can blunt all of them at once
        # — the #1442 failure mode one level down. It is also a registered source
        # of tests/test_river_ts_text_identity_cleanup.py (census 2, the two
        # table-name constants), which that oracle's wiring meta-test requires to
        # be routed here.
        "packages/common/river_ts_render.py",
        SQL_SHAPE_ORACLE_TESTS,
    ),
    PathTestRule(
        # I11 #1990 forcing_ts_render: the forcing renderer, and the
        # same gap as the river rule above one layer up from the register.
        # No pattern matched this path before — `packages/common/**` is not a
        # broad rule, it only re-adds the core-smoke baseline (#1744 path B) —
        # so a renderer diff selected its same-name suite and nothing else.
        # It is a registered source of the forcing discovery-set census
        # (FORCING_TABLE_CENSUS pins it at 1: `FORCING_TABLE`), so a second
        # table-name mention here, or the mention deleted, reds a suite this
        # path did not run — post-merge, exactly the failure river's wiring
        # meta-test exists to prevent. The census also imports
        # render_forcing_ts_sql at module scope and renders every registered
        # template through it.
        # The same-name suite is named explicitly rather than left to same-name
        # derivation, as the river rule does through SQL_SHAPE_ORACLE_TESTS: the
        # rule should read as the renderer's full unit closure, not as the half
        # that derivation misses. Both targets are non-gated.
        "packages/common/forcing_ts_render.py",
        FORCING_SQL_SHAPE_ORACLE_TESTS,
    ),
    # I11 #1990 task 7.2 — the forcing census's remaining unrouted files.
    #
    # Each of these carries mentions the census pins a NUMBER for, and none of
    # them reached a rule that runs it: `packages/common/**`,
    # `workers/**` and `services/**` are not broad backend rules (they only
    # re-add the core-smoke baseline, #1744 path B), and four of the seven have no
    # same-name suite to fall back to either. So a forcing table mention added or
    # removed in any of them was red on the post-merge master run and nowhere
    # else — exactly the failure river's per-site rider exists to prevent.
    #
    # Each rule pairs the rider with the file's own owning suites where those
    # were also unrouted, so the rule reads as the path's real closure rather
    # than as a census appendage.
    PathTestRule(
        "packages/common/forcing_domain_handoff.py",
        (
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_forcing_domain_handoff_contract.py",
            "tests/test_forcing_domain_handoff_apply.py",
        ),
    ),
    PathTestRule(
        "packages/common/forcing_domain_handoff_apply.py",
        (
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_forcing_domain_handoff_contract.py",
            # #1991 task 7.3: this module is one of the two narrow-only writers,
            # and the refusal-BEFORE-any-DELETE ordering (must-preserve M3, the
            # one data-loss property in that task's surface) is asserted there.
            # Its own same-name suite covers the apply report; the ordering does
            # not live in it.
            "tests/test_timescale_write_guard_wired.py",
            # #2713: the compression/ingest fence opens `_apply_with_cursor`
            # and its busy report is what the autopipe tick skips on.
            "tests/test_timeseries_compression_fence_writers.py",
            "tests/test_timeseries_compression_fence_wire_site_invariant.py",
            # #2300: `_upsert_met_stations` must not rewrite a compatible row;
            # only the real-PG suite can see that (ci.yml's `database:` lane).
            "tests/test_forcing_domain_handoff_apply_station_upsert_integration.py",
            # #2699: the weight purge tool takes `_lock_interp_weight_scope`'s
            # advisory lock by spelling the same key; its suite compares the two.
            "tests/test_node27_purge_superseded_weights.py",
        ),
    ),
    PathTestRule(
        "workers/forcing_producer/store.py",
        (
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_forcing_producer.py",
            # #2699: likewise for the lock `upsert_interp_weights` takes.
            "tests/test_node27_purge_superseded_weights.py",
        ),
    ),
    PathTestRule(
        # #1991 task 7.3: the forcing-version key lookup and the narrow INSERT
        # column list / row template both writers and the seed depend on. No
        # pattern matched it before -- `packages/common/**` is not a broad rule
        # (#1744 path B) and it has no same-name suite -- so a diff to the
        # lookup or the INSERT template would have been red only on the
        # post-merge master run.
        "packages/common/forcing_store_routing.py",
        (
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_timescale_write_guard_wired.py",
            "tests/test_forcing_domain_handoff_apply.py",
            "tests/test_forcing_producer.py",
            "tests/test_node27_autopipeline_handoff.py",
            "tests/test_seed.py",
        ),
    ),
    PathTestRule(
        "workers/forcing_producer/file_store.py",
        (
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_forcing_producer.py",
        ),
    ),
    PathTestRule(
        # A wired reader (the QHH bootstrap forcing-state count) with no rule of
        # its own: its templates live here, so the byte-identity and M6 pins have
        # to run on a diff to it.
        "workers/model_registry/qhh_production_bootstrap.py",
        (
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_qhh_scripts_static.py",
        ),
    ),
    PathTestRule(
        # A wired reader (the forcing-inputs listing) AND a #1728
        # connection-attribution store. Both sets live on this one rule rather
        # than on two rows with the same pattern — see
        # CONNECTION_ATTRIBUTION_STORE_PATHS, which this path was removed from.
        "packages/common/best_available.py",
        (
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            *CONNECTION_ATTRIBUTION_TESTS,
            "tests/test_best_available.py",
            # #2348: `_selection_row_response` builds the best-available rows the
            # preservation oracle compares with master.
            *RESPONSE_MODEL_PRESERVATION_TESTS,
        ),
    ),
    PathTestRule(
        "scripts/node27_timeseries_compression_live_evidence.py",
        FORCING_SQL_SHAPE_ORACLE_TESTS,
    ),
    PathTestRule(
        "services/production_closure/two_node_e2e_readonly_db_lane.py",
        FORCING_SQL_SHAPE_ORACLE_TESTS,
    ),
    PathTestRule(
        # I1 #1980 river_ts_render: the captured golden of every registered read
        # template — the ONE artefact that can void #1980's whole equivalence
        # argument. It is data, not Python, so the `tests/**.py` branch above
        # never sees it and it reached no rule at all: a PR that regenerated the
        # golden after editing a template — certifying the edit against itself —
        # selected zero backend tests and merged on a collect-only smoke
        # (review #1996, C10). Globbed on the capture SHA so a re-capture at a
        # new base is routed the same way.
        "tests/fixtures/river_ts_templates_*.json",
        SQL_SHAPE_ORACLE_TESTS,
    ),
    PathTestRule(
        # #2183: the #1913 registry-partition additions ledger is a hand-edited
        # guard input read only by the meta-suite; as data it reaches no other
        # rule, so a ledger-only PR would select nothing and fail on master.
        "tests/fixtures/basins_registry_partition_additions.json",
        ("tests/test_select_ci_tests.py",),
    ),
    # #2317: the two FROZEN partition oracles the #2183 ledger sits beside —
    # the #1913 basins-registry and #1948 QHH-bootstrap partition snapshots.
    # Each is read only by the meta-suite, whose self-digest guards are the only
    # thing that notices an edit, so as data each reached no rule and an
    # oracle-only PR selected nothing and merged on the collect-only smoke.
    # Path-exact, one row each, rather than a `*_partition_*.json` glob: a glob
    # would change the shape #2313 just pinned and could absorb a future
    # fixture whose reader is not the meta-suite.
    PathTestRule(
        "tests/fixtures/basins_registry_partition_oracle.json",
        ("tests/test_select_ci_tests.py",),
    ),
    PathTestRule(
        "tests/fixtures/qhh_bootstrap_partition_oracle.json",
        ("tests/test_select_ci_tests.py",),
    ),
    PathTestRule(
        # #2317's adjacent gap, same failure class: the Heihe IFS station-series
        # baseline selected zero tests. Its only NON-gated reader is
        # tests/test_object_store_forcing.py; the e2e/real-disk reader is
        # marker-gated and deselected in the PR lane anyway, so it is not named.
        "tests/fixtures/station_series_baseline_heihe_ifs_2026060100.json",
        ("tests/test_object_store_forcing.py",),
    ),
    # #2938 (entropy-burndown task 5.0): the journal cluster map is data read only
    # by its own test; there is no generic `tests/fixtures/*.json` row, so a
    # map-only PR would otherwise select nothing.
    PathTestRule("tests/fixtures/file_journal_cluster_map.json", ("tests/test_file_journal_cluster_map.py",)),
    # #2920: the import layering allowlist is data read only by the guard; as
    # data it reaches no other rule, so an allowlist-only PR would select nothing.
    PathTestRule("tests/import_layering_allowlist.json", (IMPORT_LAYERING_GUARD_TEST,)),
    # #2937: the `Any` ratchet ceiling is data read only by the ratchet; a
    # ceiling-only PR would otherwise select nothing.
    PathTestRule("tests/any_ratchet_ceiling.json", (ANY_RATCHET_TEST,)),
    # entropy-burndown task 2.1b: the status-literal allowlist is data read only by
    # the status-vocabulary parity suite; an allowlist-only PR (2.2-2.4 delete
    # entries) would otherwise select nothing.
    PathTestRule("tests/status_literal_allowlist.json", (STATUS_VOCAB_PARITY_TEST,)),
    # entropy-burndown task 3.1: the time-helper ceiling and allowlist are data read
    # only by the consolidation test; a ceiling- or allowlist-only PR (3.2-3.5
    # lower the ceiling / adjudicate entries) would otherwise select nothing.
    PathTestRule("tests/time_helper_ceiling.json", (TIME_HELPER_CONSOLIDATION_TEST,)),
    PathTestRule("tests/time_helper_allowlist.json", (TIME_HELPER_CONSOLIDATION_TEST,)),
    PathTestRule(
        "packages/common/forecast_store.py",
        (
            # #1990 task 7.2: this path is in the forcing discovery-set
            # census (or holds a registered forcing read template), so a new
            # met.forcing_station_timeseries mention here must redden the
            # census on THIS PR rather than on the post-merge master run.
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_forecast_api.py",
            "tests/test_forecast_store_routing.py",
            # #2424 D1: `_per_source_latest_cycles` lives here; its hydro_run-driven
            # shape (fence, outer ORDER BY, no status, bounded LATERAL fact read) is
            # pinned only by this suite.
            "tests/test_latest_cycle_discovery_shape.py",
            # #2516 D3: the narrow latest-product station leg's membership-EXISTS
            # `OFFSET 0` fence; its display_coverage.py twin routes here too.
            "tests/test_station_membership_fence.py",
            # #2222: `HYDRO_RUN_PUBLIC_COLUMNS` / `_hydro_run_response` live here;
            # only this suite pins the `/runs` SQL allowlist (no `h.*`).
            HYDRO_RUN_PUBLIC_PROJECTION_TEST,
            # #2348: the preservation oracle drives the builders that live here
            # (`_hydro_run_response`, `_qhh_latest_candidate_response`,
            # `_qhh_identity_product`, `_data_source_response`, `_cycle_response`,
            # `_station_response`) and compares their wire value with master.
            *RESPONSE_MODEL_PRESERVATION_TESTS,
            "tests/test_node27_pgdata_workload.py",
            "tests/test_node27_pgdata_workload_plan.py",
            "tests/test_node27_pgdata_workload_io.py",
            "tests/test_list_search_contract.py",
            "tests/test_migrations.py",
            "tests/test_model_registry_list_basins.py",
            # The benchmark captures this owner's named bindings; live evidence
            # independently verifies the serialized name/value pairs.
            "tests/test_node27_timeseries_compression_benchmark.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_qhh_latest_fallback_pushdown.py",
            # #1442: this file carries nine of the zero-text-identity oracle's
            # registered statements. None of the suites above assert the
            # pushdown-aid pairing or the statement census, so without this
            # entry a diff that reintroduces a text fact predicate here reaches
            # CI green unchallenged (same at-site reasoning as #1341's
            # mvt.py / display_coverage.py entries).
            "tests/test_river_ts_text_identity_cleanup.py",
            # #1728: this module carries the connection-attribution injection
            # seam (`application_name=` through `from_env` to the connect call)
            # for BOTH nhms-api-forecast and nhms-api-data-sources. MERGED into
            # this rule rather than added as a second exact entry — a duplicate
            # pattern splits the module's ownership across two rules
            # (test_path_rule_duplicate_patterns_are_allowlisted_decisions).
            *CONNECTION_ATTRIBUTION_TESTS,
            # I1 #1980 river_ts_render: this file's river read templates are
            # registered in tests/river_ts_template_registry.py and rendered per
            # timeseries store by the whole SQL-shape oracle group, so a layout
            # or predicate edit here must run all of it — the golden equivalence
            # oracle included, which is what proves the edit was behaviour-free.
            # (One block per site rather than one new rule per path: the
            # selector's own duplicate-pattern guard forbids a second
            # PATH_TEST_RULES entry for an already-owned module.)
            *SQL_SHAPE_ORACLE_TESTS,
            # selector-route-gaps-batch-ci1: a non-gated suite that imports this
            # module at file level and drives `PsycopgForecastStore`'s river/station
            # stats read paths offline over a fake connection.
            "tests/test_river_ts_stats_harness_offline.py",
        ),
    ),
    PathTestRule(
        # No rule covered this module before, and no broad `packages/common/**`
        # rule exists, so a display-coverage-only PR fell through to the
        # core-smoke fallback — which imports none of it. The three targets are
        # its non-gated top-level importer suites. The fourth importer,
        # tests/test_display_coverage_residual_debt_integration.py, is
        # deliberately excluded: it is `integration`-marked and therefore skipped
        # in the PR lane, so listing it buys constant skips and zero assertions.
        # tests/test_select_ci_tests.py derives this closure from the tracked
        # tree and reddens if a new non-gated importer suite appears here.
        #
        # #1443 merge consolidation: this module also carries the #1341
        # read-path shape pins. That surface's broad rules (packages/common/**
        # and the API route rules) do not include them, so a diff dropping a
        # pushdown pairing or reintroducing a text fact predicate here would
        # reach CI green unchallenged. The pins joined this rule instead of
        # getting a second entry for the same pattern — a duplicate pattern
        # splits the module's ownership across two rules with nothing saying so
        # (test_path_rule_duplicate_patterns_are_allowlisted_decisions).
        "packages/common/display_coverage.py",
        (
            # #1990 task 7.2: this path is in the forcing discovery-set
            # census (or holds a registered forcing read template), so a new
            # met.forcing_station_timeseries mention here must redden the
            # census on THIS PR rather than on the post-merge master run.
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            # #2516 D3: the narrow station leg's membership-EXISTS `OFFSET 0`
            # fence (and its forecast_store.py twin) is pinned only by this suite.
            "tests/test_station_membership_fence.py",
            "tests/test_display_coverage_refresh.py",
            "tests/test_display_coverage_parallel.py",
            "tests/test_forecast_api.py",
            "tests/test_river_ts_read_path_surrogate_keys.py",
            # #1714: this module is a registered connect-owning surface in the
            # attribution guard's DELEGATED_CONNECT_CLOSURE — it opens the
            # connection a registered component delegates to, which is exactly
            # the shape that shipped unattributed once. The four above assert
            # coverage behaviour and read-path shape, not the component-level
            # fallback_application_name identity.
            "tests/test_node27_connection_attribution.py",
            "tests/test_node27_connection_attribution_delegated.py",
            # #1446: the CLI suite is a one-hop importer via
            # scripts/node27_refresh_coverage.py. It owns the operator-facing
            # half of the overwrite guard (exit 3 + the structured refusal
            # line, `refused` in the --all report) — behaviour none of the
            # suites above assert.
            "tests/test_node27_refresh_coverage_cli.py",
            # I1 #1980 river_ts_render: this file's river read templates are
            # registered in tests/river_ts_template_registry.py and rendered per
            # timeseries store by the whole SQL-shape oracle group, so a layout
            # or predicate edit here must run all of it — the golden equivalence
            # oracle included, which is what proves the edit was behaviour-free.
            # (One block per site rather than one new rule per path: the
            # selector's own duplicate-pattern guard forbids a second
            # PATH_TEST_RULES entry for an already-owned module.)
            *SQL_SHAPE_ORACLE_TESTS,
        ),
    ),
    PathTestRule(
        # #1446: the refusal exit code and stderr line are this script's own
        # contract (the autopipeline branches on the rc), and no other rule
        # covers this file — a CLI-only diff fell through to the core-smoke
        # fallback, which imports none of it.
        "scripts/node27_refresh_coverage.py",
        (
            "tests/test_node27_refresh_coverage_cli.py",
            "tests/test_node27_connection_attribution.py",
            "tests/test_node27_connection_attribution_delegated.py",
        ),
    ),
    PathTestRule(
        "packages/common/state_manager.py",
        (
            "tests/test_state_manager.py",
            "tests/test_state_qc.py",
            # #2348: `state_snapshot_to_dict` builds the state-snapshot payload
            # the preservation oracle compares with master.
            *RESPONSE_MODEL_PRESERVATION_TESTS,
            # #1735: the clone-lineage read path (`get_earliest_clone_row_for_
            # model_source`, `clone_lineage_signal`, `_clone_entries_for_model_
            # source`) lives in this module, but the two suites routed above
            # assert NOTHING about it — every assertion, the DB-plane SQL shape
            # included, sits in the scheduler suites. Without these two, the
            # negative pin written to guard that SQL (`test_earliest_clone_row_
            # query_is_ascending_and_clone_scoped`, which asserts `usable_flag`
            # never enters the statement) was not in this module's lane:
            # injecting `AND usable_flag = true` into the query left the routed
            # lane green. 24 tests in 0.09s and 52 in 0.82s — under a second.
            "tests/test_scheduler_lineage.py",
            "tests/test_scheduler_backfill.py",
            # NODE IDS, not the file: `test_production_scheduler.py` names none
            # of these symbols directly — it drives the read path through a
            # duck-typed fake in its cohort suppression, which is the seam a
            # signature or ordering change breaks. The whole file is 1870 tests
            # in 186s — far past what this lane can carry. These three are the
            # only tests in it that exercise that seam, 0.34s together. Node ids
            # are first-class targets here: `_test_target_exists` splits on
            # `::`, ci.yml passes the selection straight to `pytest -q`, and the
            # meta-guard re-checks every pinned node id still names a live
            # `def`, so a rename reds instead of silently dropping the route.
            "tests/test_production_scheduler.py::test_build_candidates_suppresses_a_model_before_its_lineage_cutover",
            "tests/test_production_scheduler.py::test_build_candidates_admits_a_model_at_its_lineage_cutover",
            "tests/test_production_scheduler.py::test_build_candidates_without_lineage_is_unchanged",
            # #2548 / #2541: the prune-retention planner and capacity evidence, and
            # the per-pass validation cost pin, live in this module.
            "tests/test_state_index_retention.py",
            "tests/test_state_index_upsert_cost.py",
            # #2653 reader edge: the capacity watch grades the dry-run summary
            # fields this module produces (`checksum_valid`,
            # `retention.entry_count_before`, `capacity_before/after.warning`).
            NODE22_STATE_INDEX_CAPACITY_WATCH_TEST,
            # #1728: this module carries the connection-attribution injection
            # seam for nhms-api-state-snapshots (StateManager.from_env ->
            # PsycopgStateSnapshotRepository -> connect). MERGED here for the
            # same duplicate-pattern reason as forecast_store.py above.
            *CONNECTION_ATTRIBUTION_TESTS,
        ),
    ),
    PathTestRule(
        # #2548: the scheduler lookback cap lives here so the state-index
        # retention floor can enforce it without importing the scheduler; the
        # scheduler re-exports it and its config rejects larger lookbacks.
        "packages/common/scheduler_limits.py",
        (
            "tests/test_state_index_retention.py",
            "tests/test_production_scheduler.py::test_scheduler_caps_reject_oversized_config_and_bound_candidate_work",
        ),
    ),
    PathTestRule(
        # #2548: the retention suite's decision test feeds before/after-prune
        # reader signals through `evaluate_transition_decision`; a change to the
        # decision matrix moves exactly what "a prune never admits" is asserted
        # against. Sub-5s, DB-free.
        "services/orchestrator/scheduler_generation.py",
        ("tests/test_state_index_retention.py",),
    ),
    PathTestRule(
        # entropy-burndown task 2.1a: `scheduler_state_types` binds its hydro
        # status sets from the vocabulary, and the #1581 parity lock pins their
        # members, mutability and shared-object identity. The vocabulary's own
        # parity suite arrives through the `packages/**` supplemental loop.
        "packages/common/status_vocab.py",
        ("tests/test_hydro_status_set_parity.py",),
    ),
    PathTestRule(
        "packages/common/state_cli.py",
        (
            "tests/test_state_manager.py",
            "tests/test_state_qc.py",
        ),
    ),
    PathTestRule(
        "packages/common/redaction.py",
        (
            "tests/test_redaction.py",
            # #2624: apps/api/routes/pipeline.py:32 runs `redact_payload` over the
            # sampled pipeline bodies the #2348 preservation oracle compares with
            # master, un-faked, so a redaction edit that changes a wire value reds
            # it. Both halves ride together (the pipeline half imports the core).
            *RESPONSE_MODEL_PRESERVATION_TESTS,
        ),
    ),
    PathTestRule(
        # #2624: apps/api/routes/pipeline.py:33 runs `normalize_source_id` on the
        # sampled pipeline paths, un-faked -- same reason as redaction.py above.
        # The same-name tests/test_source_identity.py still arrives by derivation.
        "packages/common/source_identity.py",
        RESPONSE_MODEL_PRESERVATION_TESTS,
    ),
    PathTestRule(
        "packages/common/node27_container_contract.py",
        (
            # #1990 task 7.2: this path is in the forcing discovery-set
            # census (or holds a registered forcing read template), so a new
            # met.forcing_station_timeseries mention here must redden the
            # census on THIS PR rather than on the post-merge master run.
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_node27_external_contract_snapshot.py",
            "tests/test_node27_timeseries_compression_benchmark.py",
            "tests/test_node27_timeseries_compression_capture.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_node27_timeseries_compression_supervisor.py",
            "tests/test_node27_timeseries_decompression_replay.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_timeseries_discovery.py",
            "tests/test_node27_lifecycle_contract.py",
        ),
    ),
    PathTestRule(
        "packages/common/node27_external_contract_snapshot.json",
        (
            "tests/test_node27_external_contract_snapshot.py",
            "tests/test_node27_pgdata_container.py",
            "tests/test_node27_pgdata_migrate.py",
            "tests/test_node27_pgdata_migrate_oracle.py",
        ),
    ),
    PathTestRule(
        # #1644: the committed OpenAPI snapshot is the drift oracle's subject, so
        # an OpenAPI-only PR must run the drift + API-contract + 3.1-contract
        # suites. Exact set, no core-smoke fallback, no other suites.
        # #2348: plus the response-model schema parity suite, which reads the
        # committed openapi/nhms.v1.yaml (`SPEC_PATH`) and binds its hand data
        # schemas to the runtime models; no other suite compares the two, so a
        # yaml-only edit to a hand schema must run it. The preservation oracle
        # is NOT added: it never reads this document.
        "openapi/**",
        (*OPENAPI_CONTRACT_TESTS, RESPONSE_MODEL_SCHEMA_PARITY_TEST),
    ),
    PathTestRule(
        # #1644: the runtime schema owner injects every nullable node and the
        # security metadata, so a patch-owner PR must reach the drift + 3.1
        # contract suites in addition to the broad API consumers it already
        # carried.
        # #2211 quantified this module's 13 `_patch_*_openapi` implementations
        # against the six targets above. Only ONE capability suite was both
        # unreached and able to observe the module:
        # tests/test_hydro_display_mvt_scaling.py, which asserts the PATCHED
        # runtime document directly (`main.create_app().openapi()` in
        # `test_runtime_openapi_documents_the_national_identity_tile_route` and
        # `test_runtime_openapi_documents_both_424_codes_on_the_canonical_
        # national_route_only`). Measured: no-op'ing `_patch_mvt_tile_openapi`
        # reds exactly those two, and the suite was not selected before.
        # Cost +22.6s.
        # #2074: that suite is now nine partitions, and the two #2211 pins landed
        # in exactly two of them —
        # `test_runtime_openapi_documents_the_national_identity_tile_route` in
        # `..._national_routes.py` and
        # `test_runtime_openapi_documents_both_424_codes_on_the_canonical_national_route_only`
        # in `..._coverage_order.py`. Only those two are listed, for the same
        # measured reason the other #2211 candidates were rejected: the remaining
        # seven partitions never read the document this module produces, so they
        # cannot red on a patch change however much tile surface they cover.
        #
        # The rest of the #2211 candidates were REJECTED on measured evidence,
        # not on cost. This module's only observable output is the OpenAPI
        # document (its sole production importer is apps/api/main.py's schema
        # hook), so a suite that never reads that document cannot be an oracle
        # for it however public its routes are: no-op mutants of
        # `_patch_precip_openapi`, `_patch_runtime_openapi` and the combined
        # layer-metadata / forecast-series / station-series trio red only
        # tests/test_openapi_drift.py and tests/test_openapi_31_contract.py —
        # tests/test_precip_overlay.py (reads the STATIC openapi/nhms.v1.yaml),
        # tests/test_forecast_api.py and
        # tests/test_forecast_api_met_station_series.py (never read the schema)
        # and tests/test_runtime_mode.py (reads /openapi.json but asserts route
        # PRESENCE, which registration decides) all stay green. Routing them
        # would buy assertions that run but cannot observe the change — a cousin
        # of the #1447 ruling on constant skips. The runtime-vs-static
        # comparison the drift target already carries is the residual oracle
        # for the other twelve implementations.
        #
        # Per-capability literals, never a directory pattern: every rule here is
        # an explicit tuple so the exact-set anchor in
        # tests/test_select_ci_tests.py can pin it.
        #
        # #2074: the pattern is a glob over the whole `openapi_patching*` family.
        # The facade was split into openapi_patching_{nullable,security,
        # envelopes,parameters,ops_schemas,display_schemas,pipeline}.py, and the
        # component schemas, the parameter builders, the nullable finalizer and
        # the whole pipeline patch family now live in the owner modules, so an
        # exact-path entry would leave an owner-module-only diff selecting
        # nothing here. One entry, not eight: the selector's duplicate-pattern
        # guard forbids a second PATH_TEST_RULES entry for an already-owned
        # module, and every suite below reaches the owners through the facade,
        # which imports all seven. The glob is deliberately `openapi_patching*`
        # and not `openapi_*`: apps/api/openapi_restored_schemas.py is a
        # separate module this rule does not own.
        "apps/api/openapi_patching*.py",
        (
            "tests/test_api.py",
            # #2074: the RETAINED BASE path. Per the #2211 criterion ("reads the
            # document this module produces") it is the only API-contract
            # partition that qualifies: all four `app.openapi()` call sites of
            # the former monolith are in it. The two route-behaviour partitions
            # read the committed file only, so naming them here is exactly the
            # padding this rule's requirement forbids. They still reach this
            # module through the broad `apps/api/**` rule.
            "tests/test_api_contract.py",
            "tests/test_hydro_display_mvt_scaling_coverage_order.py",
            "tests/test_hydro_display_mvt_scaling_national_routes.py",
            "tests/test_monitoring_api.py",
            "tests/test_openapi_31_contract.py",
            "tests/test_openapi_drift.py",
            "tests/test_slurm_gateway_openapi_security.py",
            "tests/test_pipeline_ops_identity_envelope.py",
            # #2348: meets the #2211 criterion. The parity suite reads the
            # committed openapi/nhms.v1.yaml, which the drift oracle holds
            # byte-equal to the document this module patches, and binds the
            # hand-published data schemas this family writes (`_patch_hydro_run_
            # openapi`, the envelope reshaping in `openapi_patching_response_
            # models`) to the runtime models. A patch edit plus a regenerated
            # yaml reds parity; without the regeneration drift reds. The
            # preservation oracle never reads the document and is not added.
            RESPONSE_MODEL_SCHEMA_PARITY_TEST,
        ),
    ),
    PathTestRule(
        # The broad fallback for every `apps/api/**` path without a narrower
        # owner. #2074: the API-contract corpus is named here in FULL. This rule
        # is the only thing that runs the resource-route contracts (model
        # lifecycle / active / list / detail, basin version redaction, the
        # river-segment GeoJSON budget, met stations and data sources) on a diff
        # to the routes that serve them, so naming only the retained base path
        # would drop 27 of the corpus's 38 cases out of the PR lane — the #1684
        # "every collectible partition replaces the single target" failure this
        # rule cannot afford, since none of those routes has a second oracle here.
        "apps/api/**",
        (
            "tests/test_api.py",
            *API_CONTRACT_TESTS,
            "tests/test_monitoring_api.py",
        ),
    ),
    PathTestRule(
        # #2079. The catalog cache's own suite is named after the CAPABILITY, not
        # after the module, so same-name derivation (`tests/test_display_cache.py`)
        # cannot reach it, and the `apps/api/**` rule above only buys the three
        # generic API suites -- none of which exercise `_force_refresh`.
        "apps/api/display_cache.py",
        ("tests/test_display_catalog_cache.py",),
    ),
    PathTestRule(
        # #2348: the runtime response models of the 35 JSON routes. A model edit
        # changes the wire (preservation oracle, #2222 projection), the published
        # document the models generate (drift, 3.1 contract, conformance) and the
        # model side of the hand-schema binding (parity). The broad `apps/api/**`
        # rule above adds the generic API suites.
        "apps/api/response_models/**",
        (
            *RESPONSE_MODEL_PRESERVATION_TESTS,
            RESPONSE_MODEL_SCHEMA_PARITY_TEST,
            HYDRO_RUN_PUBLIC_PROJECTION_TEST,
            "tests/test_openapi_drift.py",
            "tests/test_openapi_31_contract.py",
            "tests/test_openapi_response_conformance.py",
        ),
    ),
    PathTestRule(
        # #2348: the hand-written component schemas the parity suite binds to
        # the response models. Not owned by the `openapi_patching*.py` glob (see
        # that rule), so without this entry only the broad `apps/api/**` suites
        # ran on a restored-schema-only diff and none of them reads these schemas
        # against the models.
        "apps/api/openapi_restored_schemas.py",
        (
            RESPONSE_MODEL_SCHEMA_PARITY_TEST,
            "tests/test_openapi_drift.py",
            "tests/test_openapi_31_contract.py",
            "tests/test_openapi_response_conformance.py",
        ),
    ),
    PathTestRule(
        # #2078. `/api/v1/runs` response body (envelope + `total`/`total_count`
        # duplication) is pinned only by this suite; the `apps/api/**` rule above
        # buys the three generic API suites, none of which call `list_runs`, so a
        # diff that changed the runs page shape reached CI green.
        "apps/api/routes/forecast.py",
        (
            "tests/test_forecast_api.py",
            # #2348/#2222: the route module declares the runs/latest-product/
            # forecast-series response models and hands their handler objects to
            # them; only these suites compare the wire value to master.
            *RESPONSE_MODEL_PRESERVATION_TESTS,
            HYDRO_RUN_PUBLIC_PROJECTION_TEST,
            # #1728's connection-attribution guards, MERGED here rather than left
            # in CONNECTION_ATTRIBUTION_ROUTE_PATHS: this module now has an exact
            # rule, and a duplicate pattern splits its ownership across two.
            *CONNECTION_ATTRIBUTION_TESTS,
        ),
    ),
    PathTestRule(
        "db/**",
        ("tests/test_migrations.py",),
    ),
    PathTestRule(
        # #1442 (group E). The seed's two river verification counts are
        # registered statements of the zero-text-identity oracle. `db/**` above
        # only buys tests/test_migrations.py, which never reads this module, so
        # a seed-only diff that reintroduced a text identity predicate reached
        # CI green. Narrow pattern on purpose: no other file under db/seeds/ is
        # in the register.
        "db/seeds/seed_demo.py",
        (
            # #1990 task 7.2: this path is in the forcing discovery-set
            # census (or holds a registered forcing read template), so a new
            # met.forcing_station_timeseries mention here must redden the
            # census on THIS PR rather than on the post-merge master run.
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_river_ts_text_identity_cleanup.py",
            "tests/test_seed.py",
            "tests/test_river_ts_dual_write_integration.py",
        ),
    ),
    # #1774 node-27 write-path least-privilege roles. `db/**` above only buys
    # tests/test_migrations.py, which never reads the role SQL; the runner is a
    # shell script with no same-name suite; and `infra/env/**` only buys the
    # two-node docker runtime suite. Without these three rows a change to the
    # provision SQL, the runner or a node-27 env template would ship with the
    # write-role guards unexecuted. The fourth producer, the autopipe stats
    # guard, is covered by an extra target MERGED into the existing
    # `scripts/node27_autopipeline.py` rule below, not by a row here.
    PathTestRule(
        "db/roles/node27_write_roles.sql",
        ("tests/test_node27_write_roles.py",),
    ),
    PathTestRule(
        "scripts/node27_provision_write_roles.sh",
        ("tests/test_node27_write_roles.py",),
    ),
    PathTestRule(
        "infra/env/node27-*.example",
        ("tests/test_node27_write_roles.py",),
    ),
    # #1774 round 4. Leg (iv) of the stored-expression sweep is an ALLOW-list of
    # (schema, name) pairs, and the promise made in the SQL comment, the test
    # docstring and runbook 9.6 is that a migration referencing a NEW function
    # reddens tests/test_node27_write_roles.py at PR time instead of the live
    # node-27 audit. That promise needs this row: `db/**` above only buys
    # tests/test_migrations.py, which never reads the allow-list, so a
    # migration-only PR that added e.g. `DEFAULT upper('x')` would have gone
    # green here and failed the strict audit on the node instead. The matcher is
    # fnmatch, whose `*` crosses `/`, so this pattern also covers a migration
    # parked in a subdirectory -- which is what the test-side rglob reads.
    PathTestRule(
        "db/migrations/*.sql",
        (
            "tests/test_node27_write_roles.py",
            # #1581: the parity lock derives the `hydro.run_status` member table
            # by sweeping this very glob as text, so a migration that adds an
            # enum member changes what that suite asserts -- and the "no member
            # outside the enum but `complete`" claim can only red on the
            # migration's own PR if the suite runs there. `db/**` above buys only
            # tests/test_migrations.py, which never parses the enum. 9 tests in
            # 0.29s, DB-free.
            "tests/test_hydro_status_set_parity.py",
            # entropy-burndown task 2.1a: the status-vocabulary parity suite folds
            # `hydro.run_status` and `met.cycle_status` over this same glob with
            # the parity lock's helper and compares them with the vocabulary.
            STATUS_VOCAB_PARITY_TEST,
        ),
    ),
    # #2048. The schema-ledger convergence suite rebuilds node-27's drift and runs
    # the real runner over db/migrations, with the role block `apply_migrations_
    # from_zero` lifts out of db/roles/, so a change to any of the three can break
    # it. `db/**` above buys only tests/test_migrations.py. The suite is
    # integration-marked (it skips in the PR unit lane and executes in "SQL
    # Migration Dry Run"); selecting it here keeps the routing explicit.
    PathTestRule(
        "db/migrations/**",
        (SCHEMA_LEDGER_CONVERGENCE_INTEGRATION_TEST,),
    ),
    PathTestRule(
        "db/roles/**",
        (SCHEMA_LEDGER_CONVERGENCE_INTEGRATION_TEST,),
    ),
    PathTestRule(
        # The runner's ledger/disk check and session guard are pinned by the fake
        # connection suite, which has no same-name pairing with migrate.py.
        "packages/common/migrate.py",
        (
            SCHEMA_LEDGER_CONVERGENCE_INTEGRATION_TEST,
            "tests/test_migrate_lock_timeout.py",
        ),
    ),
    # the converted lanes with no pre-existing rule to merge into. The
    # superuser-gated-READ guard scans these sources, and such a read fails
    # SILENTLY under the new non-superuser role.
    PathTestRule(
        "scripts/node27_timeseries_retention.py",
        (
            "tests/test_node27_write_roles.py",
            # #2713 (D2b): the drop takes the compression/ingest fence first and
            # renders its contention as lock-contention(55P03).
            "tests/test_timeseries_compression_fence.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_download_cycles.py",
        ("tests/test_node27_write_roles.py",),
    ),
    PathTestRule(
        "scripts/node27_ingest_run.py",
        ("tests/test_node27_write_roles.py",),
    ),
    PathTestRule(
        "infra/compose.compute.yml",
        ("tests/test_two_node_docker_runtime.py",),
    ),
    PathTestRule(
        "infra/compose.display.yml",
        ("tests/test_two_node_docker_runtime.py",),
    ),
    PathTestRule(
        "infra/env/**",
        ("tests/test_two_node_docker_runtime.py",),
    ),
    PathTestRule(
        # #2575: a template-only diff used to select nothing, so the PR lane
        # degraded to the zero-assertion collect-only smoke. These are the suites
        # that read or render the REAL templates on disk -- by the literal
        # `infra/sbatch` path, or through the production default `template_dir`
        # (services/production_closure/slurm_validation.py for the production
        # validation suite, SlurmGatewaySettings() for the m24 proof). Suites that
        # only name the directory in an env string (test_runtime_mode,
        # test_two_node_docker_runtime) are deliberately absent, and the prose
        # infra/sbatch/README.md stays unrouted.
        "infra/sbatch/*.sbatch",
        (
            "tests/test_analysis_pipeline.py",
            "tests/test_job_array.py",
            "tests/test_m24_gateway_proof.py",
            "tests/test_object_store_roots.py",
            "tests/test_orchestrator.py",
            "tests/test_production_slurm_validation.py",
            "tests/test_real_slurm_gateway.py",
            "tests/test_slurm_array_contract.py",
            "tests/test_slurm_route_contract.py",
            "tests/test_slurm_route_security_contract.py",
        ),
    ),
    # #1684 EVID-05/F: the rollout producers must select the static deployment
    # contract suite. The runbook is `docs/**` (no backend lane by default) and
    # the env examples would otherwise select only the two-node runtime suite;
    # each is an exact additive rule so the runbook/env contract reddens on the
    # PR that rewrites the wiring.
    # #2075 widened this row by one: `tests/test_env_templates.py` reads THIS
    # runbook by path (`test_the_runbook_states_the_same_pinned_terminal_stage`
    # asserts the pinned `NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc`
    # and `NHMS_REQUIRE_FORECAST_WARM_START=true` appear in it), so the
    # README/runbook/template mutual-consistency guard is selected whenever
    # this runbook is in the diff. `docs/**` does not open the ci.yml backend
    # lane, so the selection takes effect only when the lane opens for another
    # reason; a runbook-only PR relies on the master full run.
    # #2146 round 2 widened this row by one again: the probe suite `read_text`s
    # this runbook and asserts its probe section states every `VERDICT_*` name,
    # each threshold's default AND ceiling, the receipt field set taken from a
    # real `build_receipt` call, the receipt root, and the probe timer's own
    # steady-state row. Same lane caveat: selected when the backend lane runs
    # and this runbook is in the diff.
    # #2473 widened it by one more: the coverage freshness alert suite reads
    # this runbook's §11 (between the `## 11.` and `## 12.` headings) and fails
    # when a `COVERAGE_FRESHNESS_*` code the script emits is not named there.
    # #2472/#2473 round 1 added the three content readers the row had been
    # missing: the node-22 entrypoint invariant suite scans every `uv run` /
    # `uv sync` line of this runbook for a node-27 marker (a bare `uv sync`
    # added here reddens it), the Python environment truth suite pins the
    # `df -h / /home /data/GHDC` capacity check in it, and the role boundary
    # static suite pins its node-27/node-22 topology sentences.
    # #1103 split this file into an index landing page plus the
    # `docs/runbooks/production-ops/` sub-runbooks and repointed every reader
    # above at the whole tree. The two rows below carry the identical reader
    # set: the index keeps the historical path (and every anchor) alive, the
    # glob carries the body that moved.
    PathTestRule(
        "docs/runbooks/current-production-ops.md",
        PRODUCTION_OPS_RUNBOOK_TESTS,
    ),
    PathTestRule(
        PRODUCTION_OPS_SUBRUNBOOK_GLOB,
        PRODUCTION_OPS_RUNBOOK_TESTS,
    ),
    # #2426 fix pass 1: the manual-recovery runbook has a literal reader on the
    # same footing as the rows above. `tests/test_operator_reentry_confirmation
    # .py::test_the_refusal_reasons_are_exactly_the_reachable_ones_the_runbook_lists`
    # `read_text`s this file and asserts the `<!-- reentry-refusal-reasons -->`
    # anchored block names exactly the `_refused` literals the CLI can still
    # emit, so a runbook-side edit to that list is precisely what the pin exists
    # to redden. Exact and additive. Same lane caveat as the rows above:
    # `docs/**` does not open the ci.yml backend lane, so the selection takes
    # effect only when the lane opens for another reason; a runbook-only PR
    # relies on the master full run.
    PathTestRule(
        "docs/runbooks/node22-control-plane-manual-recovery.md",
        ("tests/test_operator_reentry_confirmation.py",),
    ),
    # #2916: the section-marker suite parses this runbook; `docs/**` does not open the backend lane.
    PathTestRule(
        "docs/runbooks/two-node-deployment-overview.md",
        ("tests/test_node22_runbook_section_markers.py",),
    ),
    PathTestRule(
        "infra/env/compute.example",
        (SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST,),
    ),
    # #2075: `tests/test_env_templates.py` reads BOTH of the next two files by
    # path -- it parses the `nhms-required-keys: compute.scheduler-dbfree`
    # block out of `infra/env/README.md` and asserts the template satisfies
    # every entry, key and pinned value. Without these two targets a
    # template-only or README-only PR selected a non-empty set that held ZERO
    # readers of the changed file (the #2195 shape), so the guard that exists
    # to catch the missing `NHMS_ORCHESTRATOR_TERMINAL_STAGE` would not have
    # run on the PR that removed it.
    PathTestRule(
        "infra/env/compute.scheduler-dbfree.env.example",
        # #2917: the env drift table cross-checks its example values against this template.
        (
            SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST,
            "tests/test_env_templates.py",
            "tests/test_node22_scheduler_env_drift_table.py",
        ),
    ),
    # #2913: the alert suite parses the authority table for the alert template's `EnvironmentFile=`.
    PathTestRule(
        "infra/env/README.md",
        (
            SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST,
            "tests/test_env_templates.py",
            "tests/test_node22_unit_tracking.py",
            "tests/test_node22_unit_failure_alert.py",
            # #2917: the suite parses the scheduler env live-drift table.
            "tests/test_node22_scheduler_env_drift_table.py",
            # #2918: the sweep suite matches the "Backup copies" default path against the script.
            "tests/test_node22_env_backup_sweep.py",
        ),
    ),
    # #2195: this template's owner suite reads it BY PATH and asserts its
    # content -- `tests/test_scheduler_refresh_deployment_contract.py`'s
    # `test_systemd_refresh_contract_is_db_free_daily_and_scheduler_independent`
    # `read_text`s `infra/env/compute.scheduler-provider-refresh.env.example`
    # and asserts two groups: `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true` is
    # PRESENT, and none of `DATABASE_URL=`, `PIPELINE_DATABASE_URL=`, `PGHOST=`
    # or `PGPORT=` appears. Before this row the template matched only the
    # `infra/env/**` rule above, whose sole target
    # `tests/test_two_node_docker_runtime.py` never opens this file: the
    # selection was non-empty yet held ZERO readers of the changed file, so the
    # #1182 zero-assertion warning stayed silent as well. Not folded into the
    # #1684 group above because that group's target is the static deployment
    # contract suite, and that suite's template list does not include this file
    # -- it does not read it. This row does NOT make the selection
    # reader-complete: `tests/test_node27_write_roles.py` also `read_text`s this
    # template (its `_env_templates()` globs `infra/env/*.example`) and stays
    # unselected here, because its rule glob is `infra/env/node27-*.example`
    # and widening it is out of scope for #2195.
    # #2146 widened this row by one: `tests/test_node22_refresh_timer_health.py`
    # `read_text`s this template too, and pins its
    # `NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT=` line to the parent of the
    # probe's `DEFAULT_REFRESH_RECEIPT` -- the receipt the probe grades. A
    # template-only PR that moves the receipt root must run that pin, or the
    # probe watches a path nothing writes and reports `manifest_unavailable`.
    # #1101 re-pointed this row: the reading case moved to
    # `tests/test_scheduler_refresh_deployment_contract.py` when the monolith was
    # partitioned. Only that partition reads the template, so the row stays a
    # two-target row and the exact-set pin in tests/test_select_ci_tests.py stays
    # an exact set.
    # #2532 re-pointed the probe half the same way: only the history partition
    # of the probe suite reads the template.
    PathTestRule(
        "infra/env/compute.scheduler-provider-refresh.env.example",
        (
            *SCHEDULER_REFRESH_DEPLOYMENT_TESTS,
            *NODE22_REFRESH_TIMER_HEALTH_ENV_TEMPLATE_TESTS,
        ),
    ),
    PathTestRule(
        "scripts/validate_two_node_docker_runtime.py",
        ("tests/test_two_node_docker_runtime.py",),
    ),
    PathTestRule(
        "scripts/validate_two_node_docker_source_trust.py",
        ("tests/test_two_node_docker_source_trust.py",),
    ),
    PathTestRule(
        "scripts/validate_readonly_db_boundary.py",
        READONLY_DB_VALIDATION_TESTS,
    ),
    PathTestRule(
        # #1571: the continuous entrypoint's dedicated current-authority owner
        # joins its explicit suite, same-name selector meta-suite and #1656
        # timescale rider — extended AT THE RULE SITE so the old target and the
        # supplemental routing above stay exactly as they were. The owner is
        # asserted additively (membership), never as an exact set, because
        # supplemental selection is intentional.
        "scripts/run_qhh_continuous.py",
        ("tests/test_run_qhh_continuous.py", "tests/test_qhh_entrypoint_authority_invariant.py"),
    ),
    PathTestRule(
        # #1442 (group E). Both qhh smoke scripts own a registered
        # river_timeseries statement and had no rule at all, so they fell
        # through to the core-smoke fallback — which imports neither and asserts
        # nothing about their SQL. The cleanup oracle pins SQL shapes and the
        # QHH owner now exercises catalog-driven reset/summary entrypoints.
        # wire-site invariant suite also scans them, but is not PR-selected for
        # scripts/** — see issue #1656.
        "scripts/summarize_qhh_smoke_results.py",
        ("tests/test_river_ts_text_identity_cleanup.py", "tests/test_qhh_scripts_static.py"),
    ),
    PathTestRule(
        "scripts/reset_qhh_smoke_db.py",
        (
            "tests/test_river_ts_text_identity_cleanup.py",
            "tests/test_qhh_scripts_static.py",
            # #1990 task 7.2: this path is in the forcing discovery-set
            # census (or holds a registered forcing read template), so a new
            # met.forcing_station_timeseries mention here must redden the
            # census on THIS PR rather than on the post-merge master run.
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
        ),
    ),
    PathTestRule(
        # No same-name tests/test_node27_autopipeline.py exists, so without this
        # rule the autopipe script falls through to the core-smoke fallback and
        # none of its own suites run.
        "scripts/node27_autopipeline.py",
        (
            # #2713: the busy-fence codes from the parser CLI and the forcing
            # apply are classified as skipped, asserted through full ticks.
            "tests/test_timeseries_compression_fence_writers.py",
            # #1990 task 7.2: this path is in the forcing discovery-set
            # census (or holds a registered forcing read template), so a new
            # met.forcing_station_timeseries mention here must redden the
            # census on THIS PR rather than on the post-merge master run.
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_node27_autopipeline_preflight.py",
            "tests/test_node27_autopipeline_handoff.py",
            # #2590: the published re-parse decline and its lane round trip.
            "tests/test_node27_autopipeline_published_reparse.py",
            # #1647: the `_connect` bounds and the stats-guard flag parser live
            # in their own suite, which the same-name fallback cannot find.
            "tests/test_node27_autopipeline_connection_bounds.py",
            "tests/test_pipeline_job_provenance_publisher.py",
            "tests/test_pipeline_job_provenance_importer.py",
            "tests/test_pipeline_job_provenance_copyback.py",
            "tests/test_pipeline_ops_identity_envelope.py",
            # Autopipeline now injects _attributed_connect into the provenance
            # importer; the delegated attribution suite is the classification
            # oracle for that seam.
            *CONNECTION_ATTRIBUTION_TESTS,
            "tests/test_display_publish_status_only.py",
            # #1442/#1789: the publish criterion is a registered statement of
            # the zero-text-identity oracle (group D, no sanctioned aid at all),
            # and that oracle also censuses this file so a NEW fact-table
            # statement -- or the deleted ingest join coming back -- turns red.
            # It additionally pins the ingest criterion's authority-state gate.
            # Nothing above would notice any of it.
            "tests/test_river_ts_text_identity_cleanup.py",
            # #1774: the stats-guard ANALYZE legs are what force the writer
            # role to OWN the relations, so the write-role guards must run.
            "tests/test_node27_write_roles.py",
            # #2757: the basin retirement tool imports `_basin_key_set` from this
            # script: the key it writes to AUTOPIPE_EXCLUDE_BASINS, and compares
            # the list and the manifest with, is the one derived here.
            *BASIN_RETIREMENT_TESTS,
        ),
    ),
    PathTestRule(
        "scripts/backfill_pipeline_job_provenance.py",
        (
            "tests/test_pipeline_job_provenance_publisher.py",
            "tests/test_pipeline_job_provenance_importer.py",
        ),
    ),
    PathTestRule(
        # The cron wrapper is a shell script, not a backend python path, so the
        # core-smoke fallback never arms for it; without this rule a
        # wrapper-only PR selects nothing and CI degrades to --collect-only.
        "scripts/node27_autopipe_cron.sh",
        (
            "tests/test_node27_autopipeline_preflight.py",
            # #2013: a SECOND reader of the same wrapper, added by the prewarm
            # suite -- it parses the `${AUTOPIPE_MVT_PREWARM_WORKERS:-8}` and
            # `${AUTOPIPE_MVT_PREWARM_ZOOMS:-3,4,5}` fallbacks and asserts they
            # equal the module defaults, which is the only in-repo guard against
            # cron/module drift. The preflight suite carries no such assertion,
            # so before this entry a wrapper-only PR flipping `:-8` to `:-2`
            # merged green. Same discipline as the comment below: targets track
            # real references found by `grep -rln 'node27_autopipe_cron.sh'
            # tests/`, filtered to suites that actually READ the file. That grep
            # has two further hits, both correctly out: one names the wrapper in
            # a docstring only, the other is this row's own complement pin in
            # tests/test_select_ci_tests.py.
            "tests/test_node27_mvt_prewarm.py",
        ),
    ),
    # Shell wrappers with committed guard suites (#1138). Like the autopipe
    # cron rule above, none of these are backend python paths, so without an
    # explicit mapping a wrapper-only PR would select nothing; targets were
    # derived from `grep -rln '<script>.sh' tests/` and must track real
    # references. Wrappers with no guard suite intentionally have no rule here
    # and arm the core-smoke fallback via _is_backend_shell_path.
    # #1101: both wrappers are `read_text`-ed by the deployment-contract
    # partition (the systemd contract case reads the runner wrapper, the
    # installer lifecycle case reads the installer), so the split moved these two
    # targets rather than widening them.
    PathTestRule(
        "scripts/scheduler_file_provider_refresh_once.sh",
        SCHEDULER_REFRESH_DEPLOYMENT_TESTS,
    ),
    # #2294: the installer row also carries the two suites that run it
    # against the harness's fake systemctl (failure paths, mutations).
    PathTestRule(
        "scripts/install_node22_scheduler_file_provider_refresh.sh",
        SCHEDULER_REFRESH_INSTALLER_TESTS,
    ),
    # #2146 round 2: the refresh RUNNER had no explicit row -- only the
    # same-name derivation, which cannot know about a second reader. The probe
    # suite `read_text`s it for the `run_id` filename shape its history
    # fallback filters on, and imports it to pin `SCHEMA_VERSION` against the
    # probe's `REFRESH_RECEIPT_SCHEMA_VERSION`: the probe rejects any receipt
    # whose `schema_version` differs, so an unpinned runner schema bump would
    # kill the whole manifest arm silently. Round 4 adds two more imports: the
    # probe's trusted-outcome set is pinned as a subset of `OUTCOMES`, and its
    # history listing cap as above `MAX_HISTORY`.
    # #1101: this row was the runner's only surviving named route once the
    # same-name derivation died with the monolith, so it carries the WHOLE
    # fifteen-suite corpus -- a runner change can land in any partition.
    # #1099: the path is now a re-export facade over scripts/scheduler_refresh/.
    # It keeps its own row (it is still the `-m` entrypoint, still holds the
    # argparse surface and `main`, and is still the patch target the whole
    # corpus writes to), and the ten owner modules get the identical reach
    # immediately below.
    PathTestRule(
        SCHEDULER_REFRESH_OWNER_PATH,
        SCHEDULER_REFRESH_RUNNER_TESTS,
    ),
    # #1099: one row per owner module -- see SCHEDULER_REFRESH_PACKAGE_MODULES
    # for why these are enumerated rather than globbed, and why each carries the
    # runner's whole reach. The probe suite rides along because it reads the
    # history-receipt literals that now live in runner.py and receipt.py.
    PathTestRule("scripts/scheduler_refresh/classification.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/config.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/constants.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/cutover_declaration.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/identity.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/precommit_gate.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/providers.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/receipt.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/receipt_validation.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/runner.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule(
        # #1102: the manual publisher had NO row of its own -- its entire route was
        # the selector's `tests/test_<module>.py` derivation onto the 3218-line
        # monolith. That derivation died with the monolith (no partition is named
        # `test_publish_scheduler_file_registry.py`), so without this row a
        # publisher-only diff would select nothing from the corpus and degrade to
        # the zero-assertion `--collect-only` smoke. It carries the WHOLE seven-suite
        # corpus: every partition drives `publish_all_basin_scheduler_registry` or
        # `main` out of this module, and it is the `monkeypatch.setattr` target of
        # 50 of the corpus's 52 patch sites (the other two name the refresh
        # facade), so a change to any of its seams can land in any partition.
        # #1100 will split this module; its owner
        # rows inherit this reach the same way scripts/scheduler_refresh/ did above.
        "scripts/publish_scheduler_file_registry.py",
        PUBLISH_SCHEDULER_REGISTRY_TESTS,
    ),
    # #1100: one row per owner module -- see PUBLISH_REGISTRY_PACKAGE_MODULES for
    # why these are enumerated rather than globbed, and why each carries the
    # whole publisher corpus.
    PathTestRule("scripts/publish_registry/calibration.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/cli.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/constants.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/model_version.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/publisher.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/radiation.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/registry_rows.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/selection.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/workspace.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    # #1611: the state-index copyback replay tool's own suite was partitioned
    # into four files and the 1378-line monolith deleted with no shim, which
    # killed the same-name derivation that had been this script's ONLY route.
    # An explicit row is the replacement: a dropped target here is a warning,
    # but a dropped ROUTE would have been silent.
    PathTestRule(
        STATE_INDEX_COPYBACK_REPLAY_OWNER_PATH,
        STATE_INDEX_COPYBACK_REPLAY_TESTS,
    ),
    # C3 verifies the scheduler manifest's shipping schema/checksum primitives.
    # After cold-family retirement that consumer contract is gone, while the
    # node-22 refresh-timer probe still copies `DEFAULT_MAX_MANIFEST_AGE_HOURS`
    # as its own `CONSUMER_MAX_MANIFEST_AGE_HOURS` (D4 forbids the probe importing
    # repo packages) and derives both threshold ceilings from it. The probe suite
    # asserts the two constants are equal, so a drop in the consumer's bound must
    # run it -- otherwise the probe keeps grading `ok` for a manifest the consumer
    # has already fail-closed on.
    # #2532: the probe suite is five partitions; the row carries all of them.
    # #2738: the merged-registry publish tool drives the real
    # `publish_scheduler_registry_manifest` (commit observer, preimage
    # compare-and-swap, validation) end to end; its partitions ride this row.
    PathTestRule(
        "services/orchestrator/scheduler_file_providers.py",
        (*NODE22_REFRESH_TIMER_HEALTH_TESTS, *MERGED_REGISTRY_PUBLISH_TOOL_TESTS),
    ),
    # #1627 / ADR 0009: the loop-spelling measurement is the backing the
    # array-runner spec names for admitting path_modes.py's db-free
    # `_safe_preserve_final_component` arm — it measures, with real symlinks and
    # a real `os.lstat`, that the <=3.12 and 3.13+ spellings of a parent-chain
    # loop are NEUTRAL at the dereference. That claim is a property of THIS
    # module's function, so an edit to it must re-measure rather than wait for
    # the post-merge master run. A per-file row, not a widening of the broad
    # `services/orchestrator/**` list: the suite's subject is one function in
    # this one module, so routing it from the directory list would make every
    # orchestrator PR pay for a measurement of an unrelated PR class.
    # DB-free, 2 tests in 0.25s.
    PathTestRule(
        "services/orchestrator/scheduler_config/path_modes.py",
        ("tests/test_preserve_final_component_loop_spelling.py",),
    ),
    # #2917: the env drift table calls four `ProductionSchedulerConfig` field
    # `default_factory`s defined here; the module has no same-name suite and no
    # exact row, so a default changed in this file must reach the table check.
    PathTestRule(
        "services/orchestrator/scheduler_config/config.py",
        ("tests/test_node22_scheduler_env_drift_table.py",),
    ),
    # #2401: the newest-truth guard on the completed-type terminal skips lives in
    # its own module and is consumed by the candidate-state decision.  Its
    # requirement suite (real-journal loops + decision-level recency rules) is
    # named after neither file, so same-name derivation cannot reach it. Per-file
    # rows, not a widening of the broad `services/orchestrator/**` list.
    # DB-free, 21 tests in ~10s.
    PathTestRule(
        "services/orchestrator/scheduler_state_terminal_recency.py",
        ("tests/test_scheduler_terminal_recency.py",),
    ),
    PathTestRule(
        "services/orchestrator/scheduler_state_decision.py",
        ("tests/test_scheduler_terminal_recency.py",),
    ),
    # #2188: these two rows are systemd units, NOT `#1138` shell wrappers (that
    # block's targets were derived by grepping tests/ for `*.sh` references;
    # `infra/systemd/**` is a different surface, and the wrapper run resumes
    # just below with `scripts/node27_download_once.sh`). They sit next to the
    # wrapper/installer rows because they are the same refresh family with the
    # same owner suite.
    # `tests/test_scheduler_refresh_deployment_contract.py`
    # (`test_systemd_refresh_contract_is_db_free_daily_and_scheduler_independent`)
    # `read_text`s BOTH files: on the `.service` it asserts
    # `ExecStart=/scratch/frd_muziyao/NWM/scripts/scheduler_file_provider_refresh_once.sh`,
    # `TimeoutStartSec=21600`, that `PrivateTmp=true` is ABSENT,
    # `UnsetEnvironment=DATABASE_URL PIPELINE_DATABASE_URL`, `Before=` on the
    # scheduler service and that `ExecCondition` is ABSENT; on
    # the `.timer` it asserts `OnCalendar=*-*-* 02:15:00 UTC`,
    # `RandomizedDelaySec=30m` and `Persistent=false`.
    # Both are outside the `#2173` glob `infra/systemd/nhms-node27-*.service`
    # (node-22 units), so neither row carries the sibling lane pin.
    # #2146 widened this row by one: `tests/test_node22_refresh_timer_health.py`
    # `read_text`s THIS unit too and asserts the probe unit's
    # `UnsetEnvironment=` line is byte-equal to this one's -- node-22 is
    # permanently DB-free and the probe must clear the same libpq selector set.
    # Editing this service's selector list without editing the probe's would
    # red that assertion, so the probe suite is a literal reader of this path.
    # It also reads this unit's `TimeoutStartSec=` and pins the probe's default
    # stopped-dwell at it plus two hours.
    # #2532: the probe suite is five partitions; the row carries all of them.
    # #2914: the alert suite's wiring test reads this unit's `[Unit]` `OnFailure=` line.
    PathTestRule(
        "infra/systemd/nhms-scheduler-file-provider-refresh.service",
        (
            *SCHEDULER_REFRESH_DEPLOYMENT_TESTS,
            *NODE22_REFRESH_TIMER_HEALTH_TESTS,
            "tests/test_node22_unit_tracking.py",
            "tests/test_node22_unit_failure_alert.py",
        ),
    ),
    PathTestRule(
        "infra/systemd/nhms-scheduler-file-provider-refresh.timer",
        (*SCHEDULER_REFRESH_DEPLOYMENT_TESTS, "tests/test_node22_unit_tracking.py"),
    ),
    # #2146: the refresh-lane health probe's installer and its two units.
    # The probe suite (five `tests/test_node22_refresh_timer_health_*.py`
    # partitions since #2532) reads all three by path --
    # it `read_text`s both units (`Type=oneshot`, `TimeoutStartSec=`, no
    # `PrivateTmp` directive, the `ExecStart` script path, the
    # `UnsetEnvironment=` line byte-equal to the refresh service's,
    # `OnCalendar=hourly`, `Persistent=true`) and it runs the installer as a
    # subprocess against a fake systemctl, asserting the four protected units
    # are only ever read.
    # #2532: the suite is five partitions and every row below carries all of
    # them. The probe's own `scripts/node22_refresh_timer_health.py` needed no
    # row while the same-name rule routed it to the monolith; with the monolith
    # gone that derivation resolves to nothing, so it gets the explicit row.
    PathTestRule(
        NODE22_REFRESH_TIMER_HEALTH_OWNER_PATH,
        NODE22_REFRESH_TIMER_HEALTH_TESTS,
    ),
    PathTestRule(
        "scripts/install_node22_refresh_timer_health.sh",
        NODE22_REFRESH_TIMER_HEALTH_TESTS,
    ),
    # #2914: the alert suite's wiring test reads this unit's `[Unit]` `OnFailure=` line.
    PathTestRule(
        "infra/systemd/nhms-node22-refresh-timer-health.service",
        (
            *NODE22_REFRESH_TIMER_HEALTH_TESTS,
            "tests/test_node22_unit_tracking.py",
            "tests/test_node22_unit_failure_alert.py",
        ),
    ),
    PathTestRule(
        "infra/systemd/nhms-node22-refresh-timer-health.timer",
        (*NODE22_REFRESH_TIMER_HEALTH_TESTS, "tests/test_node22_unit_tracking.py"),
    ),
    # #2570: the DB-free scheduler stall probe and its two units, the node-22
    # dual of the refresh probe above. `tests/test_node22_scheduler_stall_health.py`
    # `read_text`s both units (`Type=oneshot`, the `ExecStart` interpreter and
    # script path, `StandardOutput/StandardError=journal`, `UMask=0077`, no
    # `PrivateTmp` and no `EnvironmentFile` DIRECTIVE, the libpq
    # `UnsetEnvironment=` set, `OnCalendar=*:03/15`, `RandomizedDelaySec=60`,
    # `Unit=`, and that NEITHER file names `nhms-compute-scheduler` in any
    # ordering or dependency directive). A unit-only diff selected nothing
    # before this row, which degrades the PR lane to a zero-assertion
    # `--collect-only` smoke on exactly the file whose content is the
    # assertion.
    # #2914: the alert suite's wiring test reads this unit's `[Unit]` `OnFailure=` line.
    PathTestRule(
        "infra/systemd/nhms-node22-scheduler-stall-health.service",
        (
            "tests/test_node22_scheduler_stall_health.py",
            "tests/test_node22_unit_tracking.py",
            "tests/test_node22_unit_failure_alert.py",
        ),
    ),
    PathTestRule(
        "infra/systemd/nhms-node22-scheduler-stall-health.timer",
        ("tests/test_node22_scheduler_stall_health.py", "tests/test_node22_unit_tracking.py"),
    ),
    # #2910: `tests/test_node22_unit_tracking.py` reads every file deployed on
    # node-22 by path -- it hashes each against `infra/systemd/node22/MANIFEST.json`
    # (the twelve top-level oneshots get the suite appended on their own exact
    # rows) and scans the `node22/` copies for secret values and
    # `EnvironmentFile=` paths outside the `infra/env/README.md` authority table.
    # `fnmatch`'s `*` crosses `/`, so the glob also reaches the drop-in
    # directories. The evidence-retention timer and the snapshot receipt (whose
    # sha256 table the suite compares) had no row at all.
    # #2911: the drift probe suite also reads the real `MANIFEST.json` (and the
    # tracked files it hashes) through the probe's validator.
    # #2913: the alert suite reads `MANIFEST.json` and scans every file it lists for `%h/NWM`.
    PathTestRule(
        "infra/systemd/node22/*",
        (
            "tests/test_node22_unit_tracking.py",
            "tests/test_node22_unit_drift_check.py",
            "tests/test_node22_unit_failure_alert.py",
        ),
    ),
    PathTestRule(
        "infra/systemd/nhms-scheduler-evidence-retention.timer",
        ("tests/test_node22_unit_tracking.py",),
    ),
    PathTestRule(
        "docs/runbooks/receipts/2026-10-16-node22-live-units-snapshot.md",
        ("tests/test_node22_unit_tracking.py",),
    ),
    # #2913: alert suite reads the template, runs the handler, reads the example; no drift suite (master full run).
    PathTestRule(
        "infra/systemd/nhms-node22-unit-failure-alert@.service",
        ("tests/test_node22_unit_failure_alert.py", "tests/test_node22_unit_tracking.py"),
    ),
    PathTestRule(
        "scripts/node22_unit_failure_alert_once.sh",
        ("tests/test_node22_unit_failure_alert.py",),
    ),
    PathTestRule(
        "infra/env/node22-unit-failure-alert.example",
        ("tests/test_node22_unit_failure_alert.py",),
    ),
    # #2918: a `.sh` has no same-name derivation; this row is the sweep's only route to its suite.
    PathTestRule(
        "scripts/ops/node22_env_backup_sweep.sh",
        ("tests/test_node22_env_backup_sweep.py",),
    ),
    # #2911: pin for the node-22 unit drift probe, same reasoning as the stall
    # probe pin below (the same-name derivation also reaches this suite).
    PathTestRule(
        "scripts/node22_unit_drift_check.py",
        ("tests/test_node22_unit_drift_check.py",),
    ),
    # The probe module itself. The same-name derivation
    # (`scripts/<x>.py` -> `tests/test_<x>.py`) already reaches this suite, so
    # unlike the three rows above this one is a PIN rather than the only route:
    # it survives a suite rename that would silently break the derivation, and
    # it makes the probe's route auditable next to the unit rows it belongs
    # with. It adds no target the derivation would not have added.
    PathTestRule(
        "scripts/node22_scheduler_stall_health.py",
        ("tests/test_node22_scheduler_stall_health.py",),
    ),
    # The D4 parity edge. The probe is stdlib-only by design (it is staged and
    # run from outside the deployed checkout), so it carries its own copy of
    # the governed pass filename predicate, its prefix and suffixes, and the
    # writer's `MAX_EVIDENCE_BYTES`. The parity test that pins those constants
    # to this module lives in the probe suite, and the importer closure only
    # applies when the CHANGED path is itself a test file (the changed-test
    # importer-closure branch of `select_tests` in `scripts/select_ci_tests.py`)
    # -- a production module path is routed by PATH_TEST_RULES alone. Without
    # this row the parity test would not run on
    # the very diff that breaks parity, which makes it an after-the-fact
    # detector rather than a gate. Additive: `services/orchestrator/**` keeps
    # routing this path to its own targets (no `stop_on_match` on either rule).
    PathTestRule(
        "services/orchestrator/scheduler_evidence.py",
        ("tests/test_node22_scheduler_stall_health.py",),
    ),
    # The second D4 parity edge, same reasoning. The probe reads the
    # no-progress tracker itself (the module's `load_state(dir_fd)` is on the
    # circuit's WRITE path), so it carries its own copy of the tracker's
    # `STATE_SCHEMA_VERSION` and `STATE_FILENAME`, pinned by the suite's
    # tracker parity test. Without this row a PR bumping the schema version
    # merges green and the probe then grades `probe_failed` on every tick --
    # precedence 1, masking all ten other verdicts on this lane. The same class
    # of unrouted reader edge already cost #2146 a round
    # (`tests/test_select_ci_tests.py`, NODE22_REFRESH_READER_EDGES). Additive
    # like the row above.
    PathTestRule(
        "services/orchestrator/scheduler_no_progress.py",
        ("tests/test_node22_scheduler_stall_health.py",),
    ),
    # The third D4 parity edge (#2662 fix pass). The probe reads a skipped row
    # with `status == "excluded"` as a permanent exclusion, never in-flight.
    # Three modules own that contract and none is imported at module scope by
    # the probe suite: scheduler_candidates.py writes the row, scheduler_lineage.py
    # owns its reason, scheduler_evidence_payload.py decides whether `status`
    # survives the summary tier. One node id each, additive like the rows above.
    PathTestRule(
        "services/orchestrator/scheduler_candidates.py",
        ("tests/test_node22_scheduler_stall_health.py::test_e6_the_lineage_exclusion_fixture_row_is_the_row_the_scheduler_writes",),
    ),
    PathTestRule(
        "services/orchestrator/scheduler_lineage.py",
        ("tests/test_node22_scheduler_stall_health.py::test_e6_the_lineage_exclusion_fixture_row_is_the_row_the_scheduler_writes",),
    ),
    PathTestRule(
        "services/orchestrator/scheduler_evidence_payload.py",
        ("tests/test_node22_scheduler_stall_health.py::test_e6_the_lineage_exclusion_fixture_row_is_the_row_the_scheduler_writes",),
    ),
    PathTestRule(
        "scripts/node27_download_once.sh",
        ("tests/test_node27_download_cycles.py",),
    ),
    PathTestRule(
        "scripts/node27_timeseries_compression_once.sh",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_node27_timeseries_compression_supervisor.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_timeseries_compression_runner_config.py",
            "tests/test_node27_timeseries_compression_wrappers.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_wrapper_pythonpath.py",
        ),
    ),
    PathTestRule(
        # #2713: the compression/ingest advisory fence. Its keys, try/acquire/
        # release semantics and every consumer seam: the compression and
        # retention runners (exclusive side) and the three ingest writers plus
        # the autopipe skip classification (shared side), with the wire-site
        # invariant that fails on a new unfenced writer.
        "packages/common/timeseries_compression_fence.py",
        (
            "tests/test_timeseries_compression_fence.py",
            "tests/test_timeseries_compression_fence_writers.py",
            "tests/test_timeseries_compression_fence_wire_site_invariant.py",
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_retention.py",
            "tests/test_output_parser.py",
            "tests/test_output_parser_cli.py",
            "tests/test_output_parser_dual_write.py",
            "tests/test_forcing_domain_handoff_apply.py",
            "tests/test_forcing_producer.py",
            "tests/test_forcing_producer_cli.py",
            "tests/test_timescale_write_guard_wired.py",
            "tests/test_node27_autopipeline_handoff.py",
        ),
    ),
    PathTestRule(
        "packages/common/node27_timeseries_discovery.py",
        (
            "tests/test_node27_timeseries_discovery.py",
            "tests/test_node27_lifecycle_contract.py",
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_supervisor.py",
            "tests/test_node27_timeseries_retention.py",
            # #2713: the fence's lifecycle set, family keys and the writers'
            # canonical fence names all derive from these tuples, and the
            # wire-site invariant scans for writes to exactly that set.
            "tests/test_timeseries_compression_fence.py",
            "tests/test_timeseries_compression_fence_wire_site_invariant.py",
            "tests/test_timeseries_compression_fence_writers.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_timeseries_compression.py",
        (
            "tests/test_node27_timeseries_compression.py",
            # #2713: the fence-wait knob, the fence-first compress session and
            # the deferred_contended / deferred receipt semantics.
            "tests/test_timeseries_compression_fence.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_timeseries_compression_runner_config.py",
            "tests/test_node27_timeseries_compression_wrappers.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            # #1647: `_CHUNK_IDENT_RE` is pinned byte-equal to the autopipeline
            # `_STATS_GUARD_IDENT_RE` from that suite, so loosening the pattern
            # here reds there — mirror of the autopipeline row above.
            "tests/test_node27_autopipeline_connection_bounds.py",
            # #1774: this lane runs as a non-superuser; a superuser-gated
            # READ added here would fail SILENTLY.
            "tests/test_node27_write_roles.py",
            "tests/test_node27_lifecycle_contract.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_timeseries_compression_supervisor.py",
        ("tests/test_node27_lifecycle_contract.py",),
    ),
    PathTestRule(
        "scripts/node27_timeseries_compression_capture.py",
        (
            "tests/test_node27_timeseries_discovery.py",
            # #1990 task 7.2: this path is in the forcing discovery-set
            # census (or holds a registered forcing read template), so a new
            # met.forcing_station_timeseries mention here must redden the
            # census on THIS PR rather than on the post-merge master run.
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
        ),
    ),
    PathTestRule(
        "infra/systemd/nhms-node27-timeseries-compression.service",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_timeseries_compression_runner_config.py",
            "tests/test_node27_timeseries_compression_wrappers.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_timeseries_retention.py",
        ),
    ),
    PathTestRule(
        "infra/systemd/nhms-node27-timeseries-retention.timer",
        (
            "tests/test_node27_timeseries_retention.py",
        ),
    ),
    PathTestRule(
        # #2013: same shape as the retention `.timer` row above. A timer-only
        # diff matched NOTHING before this rule -- `infra/**` is not a backend
        # python path and `_is_backend_shell_path` is scoped to `scripts/**.sh`,
        # so not even the core-smoke fallback armed and CI degraded to
        # --collect-only. Both targets really read this file: the preflight
        # suite asserts `OnUnitActiveSec=10min` is present, and the prewarm
        # suite parses the same directive into seconds and pins the budget
        # inequality `DEFAULT_DEADLINE_SECONDS + DEFAULT_TIMEOUT_SECONDS <=
        # tick`, so shortening the tick must red here rather than after merge.
        "infra/systemd/nhms-node27-autopipe.timer",
        (
            "tests/test_node27_autopipeline_preflight.py",
            "tests/test_node27_mvt_prewarm.py",
        ),
    ),
    PathTestRule(
        # #1712: the unit file carries the `StandardError=journal` lane and the
        # `OnFailure=` alert wiring, both pinned by unit-file tests. Without an
        # explicit row it is infra/** non-python, matches nothing, and a
        # unit-only PR selected zero tests. Narrow on purpose — the `.timer`
        # sibling is timer-schedule coverage, not something the unit body can break.
        "infra/systemd/nhms-node27-timeseries-retention.service",
        ("tests/test_node27_timeseries_retention.py",),
    ),
    PathTestRule(
        # #2032: same shape as the retention `.service` row above. `infra/**`
        # is not a backend python path and `_is_backend_shell_path` is scoped
        # to `scripts/**.sh`, so a unit-only diff matches NOTHING and CI
        # degrades to --collect-only. The suite really reads this file: the
        # `ExecStartPre` < `ExecStart` ordering and the append log paths.
        # #2170: the sibling-lane pin in `tests/test_node27_timeseries_retention.py`
        # is a glob reader over `infra/systemd/nhms-node27-*.service`, so a
        # path-exact unit rule that does not target it makes targeted PR CI
        # constructively skip the pin -- it only reds on master's full run.
        "infra/systemd/nhms-node27-mvt-cache-retention.service",
        (
            *NODE27_MVT_CACHE_RETENTION_TESTS,
            "tests/test_node27_timeseries_retention.py",
        ),
    ),
    PathTestRule(
        # #2173: the general case of the two rows above. The sibling-lane pin in
        # `tests/test_node27_timeseries_retention.py` is a GLOB reader over
        # exactly `infra/systemd/nhms-node27-*.service`, so the rule is aligned
        # to the same glob instead of chasing it one path-exact row at a time --
        # eight of ten units had no row naming the pin, six had no row at all
        # and degraded to --collect-only. Matches accumulate (`selected.update`
        # below, no `stop_on_match`), so every path-exact unit rule above and
        # below keeps its own targets and the retention `.service` selection
        # stays exactly the pin suite. A unit created later is covered without a
        # per-unit rule. `.timer` files are outside the pin's glob and are
        # deliberately not covered here.
        "infra/systemd/nhms-node27-*.service",
        ("tests/test_node27_timeseries_retention.py",),
    ),
    PathTestRule(
        # #2180: the glob row above gives every unit the sibling-lane pin, but
        # that pin asserts ONLY the `StandardError=append:…systemd.err` lane
        # set -- it never reads this unit's directives, so before this row an
        # autopipe-unit-only diff ran zero assertions about the unit's body.
        # `tests/test_node27_autopipeline_preflight.py:19` resolves this exact
        # path and `:1111-1119` asserts the `scripts/node27_autopipe_cron.sh`
        # ExecStart, `NODE27_AUTOPIPE_BOOTSTRAP_LOG=…/bootstrap.log`, and that
        # `infra/env/display.env` is absent (the data-plane / display-plane
        # boundary). Matches accumulate (`selected.update`, no `stop_on_match`),
        # so the pin suite arrives from the glob row and is deliberately not
        # repeated here.
        "infra/systemd/nhms-node27-autopipe.service",
        ("tests/test_node27_autopipeline_preflight.py",),
    ),
    PathTestRule(
        # #2180: `tests/test_node27_download_cycles.py:18` resolves this exact
        # path and `:495` asserts the `scripts/node27_download_once.sh`
        # ExecStart -- swapping the wrapper the unit runs is invisible to the
        # lane pin. Pin suite comes from the glob row by accumulation, not
        # repeated here.
        "infra/systemd/nhms-node27-download.service",
        ("tests/test_node27_download_cycles.py",),
    ),
    PathTestRule(
        # #2180: `tests/test_node27_frontier_stall_alert.py:1427` resolves this
        # exact path and `:1553-1562` asserts
        # `Environment=NODE27_FRONTIER_ALERT_ENV_INJECTED=1`,
        # `EnvironmentFile=%h/NWM/infra/env/node27-frontier-alert.env` and
        # `TimeoutStartSec=900` -- deleting the sentinel `Environment=` line is
        # invisible to the lane pin. Pin suite comes from the glob row by
        # accumulation, not repeated here.
        "infra/systemd/nhms-node27-frontier-alert.service",
        ("tests/test_node27_frontier_stall_alert.py",),
    ),
    PathTestRule(
        # #2529: the residency lane's owner suite resolves all three paths and
        # asserts their bodies (the `OnFailure=` handler line, both
        # `EnvironmentFile=` lines, journal stdio, `OnCalendar=*:15/30`, every
        # knob documented commented-out). The `.service` also gets the sibling
        # lane pin from the #2173 glob row by accumulation; the `.timer` and the
        # env template match no other reader of their body (the
        # `infra/env/node27-*.example` glob only buys the write-roles DSN scan).
        "infra/systemd/nhms-node27-parse-failure-residency-alert.service",
        ("tests/test_node27_parse_failure_residency_alert.py",),
    ),
    PathTestRule(
        "infra/systemd/nhms-node27-parse-failure-residency-alert.timer",
        ("tests/test_node27_parse_failure_residency_alert.py",),
    ),
    PathTestRule(
        "infra/env/node27-parse-failure-residency-alert.example",
        ("tests/test_node27_parse_failure_residency_alert.py",),
    ),
    PathTestRule(
        # #2504 D6: the coverage refresh reads NODE27_TIMESERIES_RETENTION_WINDOW_DAYS
        # from THIS template's deployed copy (the autopipe cron sources it).
        # `tests/test_node27_autopipeline_preflight.py` runs the real cron
        # wrapper with this key in a strict-source env file and asserts the
        # libpq-ambient filter lets it through; the globs matching this path
        # (write-roles DSN scan, docker runtime pin) never read that line.
        "infra/env/node27-ingest.example",
        ("tests/test_node27_autopipeline_preflight.py",),
    ),
    PathTestRule(
        # #2504 D6: `configured_retention_window_days` is shared by the
        # retention runner and the coverage refresh.
        # `tests/test_node27_timeseries_retention.py::test_runner_window_agrees_with_the_shared_resolver`
        # is the cross-consumer agreement pin (the resolver must never accept a
        # window the runner refuses); a storage-only diff otherwise selects only
        # its same-name suite and the core-smoke baseline.
        "packages/common/storage.py",
        (
            "tests/test_storage.py",
            "tests/test_node27_timeseries_retention.py",
        ),
    ),
    PathTestRule(
        # #2180: `tests/test_node27_raw_retention.py:317-322` resolves this
        # exact path and `:325-341` asserts
        # `ExecStartPre=/usr/bin/mkdir -p /home/nwm/node27-raw-retention-logs`,
        # `StandardOutput=append:…/systemd.log`, and that the `ExecStartPre=`
        # line precedes `ExecStart=` (reordering them breaks the log-dir
        # bootstrap and the lane pin cannot see it). Pin suite comes from the
        # glob row by accumulation, not repeated here.
        "infra/systemd/nhms-node27-raw-retention.service",
        (
            "tests/test_node27_raw_retention.py",
            # #2360: asserts this unit's `OnFailure=` line.
            "tests/test_node27_raw_retention_canonical_deployment.py",
        ),
    ),
    PathTestRule(
        # #2360: the canonical system unit's timer must tick with this one (one
        # cutoff date for a mirror and its PNGs); the deployment suite compares
        # the two `OnCalendar=` lines. No other suite reads this timer.
        "infra/systemd/nhms-node27-raw-retention.timer",
        ("tests/test_node27_raw_retention_canonical_deployment.py",),
    ),
    PathTestRule(
        # #2360: the system alert template must run the SAME handler; the
        # deployment suite compares its `ExecStart=` with this template's. The
        # retention suite (its other reader) arrives from the `#2173` glob row.
        "infra/systemd/nhms-node27-unit-failure-alert@.service",
        ("tests/test_node27_raw_retention_canonical_deployment.py",),
    ),
    PathTestRule(
        # #2360: node-27 SYSTEM units (canonical retention + its timer + the
        # system alert template). Outside the `#2173` glob on purpose: they are
        # not nwm user units and carry no `systemd.err` lane. The deployment
        # suite reads every file here and pins the directory's exact file set,
        # so the row is a directory glob, not per-file.
        "infra/systemd/system/*",
        ("tests/test_node27_raw_retention_canonical_deployment.py",),
    ),
    PathTestRule(
        # #2360: root installer for the canonical system unit. `scripts/**.sh`
        # only arms core smoke; the deployment suite drives its refusal path and
        # its env rendering.
        "scripts/node27_canonical_retention_install.sh",
        ("tests/test_node27_raw_retention_canonical_deployment.py",),
    ),
    PathTestRule(
        # #2360: the unit-failure alert handler (user + system journal scope).
        # Before this row a handler-only diff fell to core smoke: its real
        # readers are the retention suite's alert-wrapper rows (incl. the
        # journal-scope switch) and the working-set suite's governance run.
        "scripts/node27_unit_failure_alert_once.sh",
        (
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_working_set.py",
            "tests/test_node27_raw_retention_canonical_deployment.py",
        ),
    ),
    PathTestRule(
        # #2180: two suites read this unit by path.
        # `tests/test_node27_timeseries_compression.py:31` + `:1964-1976`
        # asserts the supervisor `--enforce` invocation and its
        # `--run-plan-path` / `--ledger-path` / `--receipt-path` /
        # `--finalizer-state-path` / `--wall-seconds 900` options, the
        # `ExecStopPost=` + `--finalize-only` finalizer lane and
        # `TimeoutStartSec=920`;
        # `tests/test_node27_timeseries_compression_supervisor.py:1233-1238`
        # asserts the
        # `EnvironmentFile=/home/nwm/NWM/infra/env/node27-timeseries-compression-replay.env`
        # digest pin. Both are targets. Pin suite comes from the glob row by
        # accumulation, not repeated here.
        "infra/systemd/nhms-node27-timeseries-compression-replay.service",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_supervisor.py",
        ),
    ),
    PathTestRule(
        # #2032: the same suite parses `OnCalendar=*-*-* 04:05:00 UTC` and
        # `Persistent=true` out of the timer, so the schedule is assertable at
        # PR time rather than at `systemctl --user list-timers`.
        "infra/systemd/nhms-node27-mvt-cache-retention.timer",
        NODE27_MVT_CACHE_RETENTION_TESTS,
    ),
    PathTestRule(
        # #2180: same shape as the mvt-cache-retention `.timer` row above and
        # the same zero-selection hole -- `infra/**` is not a backend python
        # path, `_is_backend_shell_path` is scoped to `scripts/**.sh`, and the
        # `#2173` pin glob is `*.service`, so a timer-only diff matched NOTHING
        # and CI degraded to --collect-only. The suite really reads this file:
        # `tests/test_node27_download_cycles.py:19` resolves the timer by path
        # and `:496` asserts `OnUnitActiveSec=30min`, so lengthening the tick
        # reds at PR time instead of at `systemctl --user list-timers`.
        "infra/systemd/nhms-node27-download.timer",
        ("tests/test_node27_download_cycles.py",),
    ),
    PathTestRule(
        # #2180: the compression suite reads this timer by path and asserts
        # `OnCalendar=*-*-* 04:25:00 UTC` plus
        # `Unit=nhms-node27-timeseries-compression.service`. Compression.timer
        # never selects the retention suite. Live-evidence/capture only
        # `read_bytes` this timer into a fixture and assert nothing about its
        # content -- not targets. The `#2173` pin glob is `*.service`, so this
        # row is the whole selection for a timer-only diff.
        "infra/systemd/nhms-node27-timeseries-compression.timer",
        (
            "tests/test_node27_timeseries_compression.py",
        ),
    ),
    PathTestRule(
        # #2188: the display API unit runs on node-27 but is named OUTSIDE the
        # `#2173` pin glob `infra/systemd/nhms-node27-*.service`, so it matched
        # nothing at all (`infra/**` is not a backend python path and
        # `_is_backend_shell_path` is scoped to `scripts/**.sh`) and a
        # unit-only diff degraded to a zero-assertion --collect-only smoke.
        # `tests/test_hydro_display_mvt_scaling.py:184-190` (#2074 renumbered the
        # citation when the suite was partitioned; the reading case stayed on this
        # path, which is one reason the base path survives the split)
        # (`test_systemd_workers_receive_shared_file_cache_default`) `read_text`s
        # this exact path and asserts the two directives that carry the public
        # display entrypoint's cache/worker contract:
        # `export NHMS_MVT_FILE_CACHE_DIR="${NHMS_MVT_FILE_CACHE_DIR:-/home/nwm/.cache/nhms/mvt}"`
        # and `--workers "${NHMS_DISPLAY_WORKERS:-2}"`.
        # Not in the `#2173` glob => this unit takes NO sibling lane pin, so
        # `tests/test_node27_timeseries_retention.py` must NOT appear in this
        # row's targets (the lane pin never reads this unit's body anyway).
        "infra/systemd/nhms-display-api.service",
        ("tests/test_hydro_display_mvt_scaling.py",),
    ),
    PathTestRule(
        "schemas/timeseries_compression_receipt.schema.json",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_node27_lifecycle_contract.py",
            # #2713: the schema 2.2 deferral fields and their version gates.
            "tests/test_timeseries_compression_fence.py",
        ),
    ),
    PathTestRule(
        "schemas/examples/timeseries_compression_receipt.example.json",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_lifecycle_contract.py",
        ),
    ),
    PathTestRule(
        "schemas/timeseries_retention_receipt.schema.json",
        ("tests/test_node27_lifecycle_contract.py",),
    ),
    # entropy-burndown task 2.1a: the status-vocabulary parity suite asserts this
    # schema's status enum equals `HYDRO_RUN_STATUS_ORDER`; without the row the
    # path selected nothing.
    PathTestRule("schemas/run_status.schema.json", (STATUS_VOCAB_PARITY_TEST,)),
    PathTestRule(
        # #2032: the `infra/env/node27-*.example` glob above only buys
        # tests/test_node27_write_roles.py, which never reads this template.
        # This row is what makes the cache-root warning, the health `jq`
        # criterion and the two rollback gates assertable at PR time. Additive:
        # the glob row still matches and both target sets are unioned.
        "infra/env/node27-mvt-cache-retention.example",
        NODE27_MVT_CACHE_RETENTION_TESTS,
    ),
    PathTestRule(
        # #2104, same ground as the #2032 row above: the two globs that match
        # this template (`infra/env/node27-*.example` -> write-roles, which
        # only scans every template for DSN users and credential placeholders;
        # `infra/env/**` -> the docker runtime pin, which never opens it) assert
        # nothing about this file's body, so a template-only PR selected a
        # non-empty set with zero readers of the documented criterion -- the
        # #2195 shape. Both targets below really read this path:
        # `tests/test_node27_raw_retention.py::_documented_operator_jq_program`
        # EXTRACTS the documented `jq` program out of this file and runs it
        # against a real summary, so a typo in the program reds there and
        # nowhere else, and
        # `tests/test_node27_mvt_cache_retention_wrapper.py::test_the_raw_retention_env_example_points_at_this_runner`
        # pins the sibling-runner cross-reference in it. Additive: the glob rows
        # still match and every target set is unioned.
        "infra/env/node27-raw-retention.example",
        (
            "tests/test_node27_raw_retention.py",
            *NODE27_MVT_CACHE_RETENTION_TESTS,
        ),
    ),
    PathTestRule(
        "infra/env/node27-timeseries-compression.example",
        (
            "tests/test_node27_timeseries_compression.py",
            # #2713: pins the FENCE_WAIT_MS default literal in this file.
            "tests/test_timeseries_compression_fence.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_timeseries_compression_runner_config.py",
            "tests/test_node27_timeseries_compression_wrappers.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_lifecycle_contract.py",
        ),
    ),
    PathTestRule(
        "docs/runbooks/tier-node27-timeseries-storage.md",
        (
            *NODE27_PGDATA_WORKLOAD_TESTS,
            *C4_PRODUCTION_ACCEPTANCE_TESTS,
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_timeseries_compression_runner_config.py",
            "tests/test_node27_timeseries_compression_wrappers.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_lifecycle_contract.py",
        ),
    ),
    PathTestRule(
        # #2417 fix pass 1: the curve statement's derivation from its public owner
        # moved out of scripts/node27_timeseries_compression_benchmark.py so the
        # OFFLINE verifier stops importing a capture CLI to get at it. Same-name
        # derivation cannot see the new module (there is no
        # tests/test_forecast_curve_capture.py) and the shared `packages/common/**`
        # add-on is core-smoke only, so without this entry a change to the derivation
        # would reach CI with NEITHER of its two real consumers executed — a
        # narrowing against what the benchmark script's same-name rule used to give.
        "packages/common/forecast_curve_capture.py",
        (
            "tests/test_node27_timeseries_compression_benchmark.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
        ),
    ),
    PathTestRule(
        "packages/common/node27_timeseries_compression_budget.py",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_timeseries_compression_runner_config.py",
            "tests/test_node27_timeseries_compression_wrappers.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_wrapper_pythonpath.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_timeseries_budget_preflight.py",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_timeseries_compression_runner_config.py",
            "tests/test_node27_timeseries_compression_wrappers.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_wrapper_pythonpath.py",
        ),
    ),
    PathTestRule(
        "packages/common/node27_timeseries_lifecycle_lock.py",
        (
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_timeseries_decompression_replay.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_timeseries_retention_once.sh",
        (
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_wrapper_pythonpath.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_raw_retention_once.sh",
        ("tests/test_node27_wrapper_pythonpath.py",),
    ),
    PathTestRule(
        # #2032: the MVT cache-retention wrapper. `scripts/**.sh` only arms the
        # core-smoke fallback when no rule matches, so without this row a
        # wrapper-only diff would reach the PR lane with the env-file refusal,
        # flock-skip and rc-propagation assertions unexecuted. Deliberately not
        # routed to tests/test_node27_wrapper_pythonpath.py: this wrapper
        # exports no PYTHONPATH (its runner imports nothing from the repo).
        "scripts/node27_mvt_cache_retention_once.sh",
        NODE27_MVT_CACHE_RETENTION_TESTS,
    ),
    PathTestRule(
        # #2627: the basemap stage's suite is a sibling partition the same-name
        # derivation cannot find; both are unioned with it.
        "scripts/node27_mvt_cache_retention.py",
        (
            *NODE27_MVT_CACHE_RETENTION_TESTS,
            "tests/test_node27_mvt_cache_retention_basemap.py",
            # BASEMAP_LAYERS is pinned to the route's TiandituLayer there.
            "tests/test_basemap_proxy.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_frontier_stall_alert_once.sh",
        ("tests/test_node27_frontier_stall_alert.py",),
    ),
    PathTestRule(
        "scripts/run_qhh_backend_smoke.sh",
        ("tests/test_qhh_scripts_static.py",),
    ),
    PathTestRule(
        # #1571: the cycle wrapper's dedicated current-authority owner joins the
        # three pre-existing targets additively (asserted by membership, never
        # as an exact set — supplemental selection is intentional).
        "scripts/run_qhh_cycle.sh",
        (
            "tests/test_run_qhh_continuous.py",
            "tests/test_role_boundary_static.py",
            "tests/test_qhh_scripts_static.py",
            "tests/test_qhh_entrypoint_authority_invariant.py",
        ),
    ),
    PathTestRule(
        # #1571: `.python-version` is the single producer for the repository
        # default-Python oracle. Not a backend Python path, so without this rule
        # a pin-only PR selects nothing and CI degrades to collect-only.
        ".python-version",
        (PYTHON_ENVIRONMENT_TRUTH_TEST,),
    ),
    PathTestRule(
        # #1571: the generated-root instruction SOURCE (instructions/agents/
        # shared.md) is the producer that governs the byte-exact CLAUDE.md /
        # AGENTS.md projection too; a source-only diff must reach the oracle
        # that pins both semantic clauses. Non-stop: `docs/**`-style generated
        # roots themselves remain unrouted by design.
        # #1571 local-repair: the shared source's node-22 deferred-environment
        # clause is asserted by the node-22 owner, so it joins additively
        # alongside the existing Python-environment owner.
        "instructions/agents/shared.md",
        (PYTHON_ENVIRONMENT_TRUTH_TEST, *NODE22_ENTRYPOINT_INVARIANT_TESTS),
    ),
    PathTestRule(
        # #1571: the two-node Docker runbook is `infra/**`, which already opens
        # the backend lane; the rule converts that collect-only lane into real
        # assertions. Exact path, deliberately NOT a glob over infrakdown —
        # other runbooks must not start the backend lane (inventory scope).
        "infra/README.two-node-docker.md",
        (TWO_NODE_DOCKER_RUNBOOK_ENV_TEST,),
    ),
    PathTestRule(
        QHH_CYCLE_SBATCH,
        ("tests/test_qhh_scripts_static.py",),
    ),
    PathTestRule(
        QHH_DIAGNOSTIC_README,
        # #1571 local-repair: the Production Replacement lines carry node-22
        # exact-interpreter semantics that QHH-static does not assert, so the
        # node-22 owner joins additively alongside the existing QHH-static
        # target. Extended AT THE RULE SITE, non-stop: the README keeps both
        # owners and no other producer's selection moves.
        ("tests/test_qhh_scripts_static.py", *NODE22_ENTRYPOINT_INVARIANT_TESTS),
    ),
    PathTestRule(
        "scripts/local_pg.sh",
        ("tests/test_qhh_scripts_static.py",),
    ),
    PathTestRule(
        # #1571: the gateway unit's deferred-venv ExecStart is uniquely asserted
        # by the node-22 owner. infra/** already starts the backend lane, so
        # without this rule a unit-only PR selected nothing and CI degraded to
        # collect-only; the exact rule converts that lane into real assertions.
        # #1684 EVID-05: the unit's active EnvironmentFile / no-inline-secret
        # contract is asserted by the static deployment suite, which joins the
        # rule alongside the node-22 owner.
        NODE22_SLURM_GATEWAY_UNIT,
        (SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST, *NODE22_ENTRYPOINT_INVARIANT_TESTS),
    ),
    PathTestRule(
        # #1571: the evidence-retention unit's single exact ExecStart is
        # uniquely asserted by the node-22 owner.
        NODE22_RETENTION_UNIT,
        # #2914: the alert suite's wiring test reads this unit's `[Unit]` `OnFailure=` line.
        (
            *NODE22_ENTRYPOINT_INVARIANT_TESTS,
            "tests/test_node22_unit_tracking.py",
            "tests/test_node22_unit_failure_alert.py",
        ),
    ),
    PathTestRule(
        # The journal archive service and timer jointly define one mutation
        # entrypoint. Each changed unit must run its active-runtime invariant
        # and both archive/retention behavioral partitions.
        NODE22_JOURNAL_RETENTION_SERVICE,
        # #2914: the alert suite's wiring test reads this unit's `[Unit]` `OnFailure=` line.
        (
            *NODE22_ENTRYPOINT_INVARIANT_TESTS,
            *JOURNAL_RETENTION_TESTS,
            "tests/test_node22_unit_tracking.py",
            "tests/test_node22_unit_failure_alert.py",
        ),
    ),
    PathTestRule(
        NODE22_JOURNAL_RETENTION_TIMER,
        (*NODE22_ENTRYPOINT_INVARIANT_TESTS, *JOURNAL_RETENTION_TESTS, "tests/test_node22_unit_tracking.py"),
    ),
    PathTestRule(
        # #2653: a unit-only diff would otherwise select nothing (infra/**
        # non-python) and degrade to the zero-assertion collect-only smoke.
        NODE22_STATE_INDEX_CAPACITY_SERVICE,
        # #2914: the alert suite's wiring test reads this unit's `[Unit]` `OnFailure=` line.
        (
            *NODE22_ENTRYPOINT_INVARIANT_TESTS,
            NODE22_STATE_INDEX_CAPACITY_WATCH_TEST,
            "tests/test_node22_unit_tracking.py",
            "tests/test_node22_unit_failure_alert.py",
        ),
    ),
    PathTestRule(
        NODE22_STATE_INDEX_CAPACITY_TIMER,
        (
            *NODE22_ENTRYPOINT_INVARIANT_TESTS,
            NODE22_STATE_INDEX_CAPACITY_WATCH_TEST,
            "tests/test_node22_unit_tracking.py",
        ),
    ),
    PathTestRule(
        # #2653: a PIN, not the only route -- the same-name derivation already
        # reaches the suite; this row survives a suite rename.
        "scripts/node22_state_index_capacity_watch.py",
        (NODE22_STATE_INDEX_CAPACITY_WATCH_TEST,),
    ),
    PathTestRule(
        # #2653 reader edge: the watch calls `repair_state_index`, matches the
        # error classes and their `reason` strings, and reuses
        # `_private_directory`. Additive to the same-name repair suite.
        "scripts/scheduler_state_index_repair.py",
        (NODE22_STATE_INDEX_CAPACITY_WATCH_TEST,),
    ),
    PathTestRule(
        # #2653 reader edge: the watch retries only this module's
        # `provider_preimage_changed` reason. Additive: this module has no other
        # PATH_TEST_RULES row, its same-name suite still derives. #2753: the
        # provision step's output-registry check is this module's directory
        # check, held against the destination lock in that suite.
        "packages/common/provider_atomic.py",
        (NODE22_STATE_INDEX_CAPACITY_WATCH_TEST, PROVISION_OUTPUT_REGISTRY_PARENT_TEST),
    ),
    PathTestRule(
        # #1571: the repair script's usage string is uniquely asserted by the
        # node-22 owner. A rule suppresses the unknown-backend core-smoke
        # fallback (matched=True), so the script's CURRENT CORE_SMOKE selection
        # is preserved EXPLICITLY here — without these targets an exact rule
        # would silently drop them. scripts/** adds the #1656 timescale rider
        # supplementally. Owner joins additively, never replacing core smoke.
        NODE22_REPAIR_SCRIPT,
        (*CORE_SMOKE_TESTS, *NODE22_ENTRYPOINT_INVARIANT_TESTS),
    ),
    PathTestRule(
        "scripts/ops/node22-run-cycle-once.sh",
        ("tests/test_production_scheduler.py",),
    ),
    PathTestRule(
        # #1729 / #1480 / #2621: the runner is the executor the disposable-DB
        # suites drive; the grammar suite is its same-name owner.
        "scripts/ops/node27_oneshot_sql.py",
        (
            "tests/test_node27_oneshot_sql.py",
            "tests/test_node27_1729_evidence_basin_delete_integration.py",
            "tests/test_node27_1480_seed_provenance_backfill_integration.py",
            "tests/test_node27_2621_rename_leftovers_delete_integration.py",
        ),
    ),
    PathTestRule(
        # #1729: the delete / backup / rollback trio runs only through the
        # one-shot runner; the marker-grammar suite parses every file and the
        # disposable-DB suite executes them (ci.yml's `database:` lane).
        "scripts/ops/node27_1729_delete_evidence_basin*.sql",
        ("tests/test_node27_oneshot_sql.py", "tests/test_node27_1729_evidence_basin_delete_integration.py"),
    ),
    PathTestRule(
        # #1480: same shape as the #1729 trio for the backfill / rollback pair.
        "scripts/ops/node27_1480_backfill_seed_station_provenance*.sql",
        ("tests/test_node27_oneshot_sql.py", "tests/test_node27_1480_seed_provenance_backfill_integration.py"),
    ),
    PathTestRule(
        # #2621: the rename-leftover delete / rollback pair, same shape as #1729:
        # the grammar suite pins its markers, column guard and replica windows,
        # the disposable-DB suite executes both files (ci.yml's `database:` lane).
        "scripts/ops/node27_2621_delete_rename_leftovers*.sql",
        ("tests/test_node27_oneshot_sql.py", "tests/test_node27_2621_rename_leftovers_delete_integration.py"),
    ),
    PathTestRule(
        # #2621: the catalog/manifest audit. Its same-name unit suite still
        # derives; the real-PG suite drives the production `list_basins` it
        # reuses, so it rides the database lane too.
        "scripts/basin_catalog_manifest_audit.py",
        ("tests/test_basin_catalog_manifest_audit_integration.py",),
    ),
    PathTestRule(
        # #2699: the weight purge tool. Its same-name fake-database suite still
        # derives; the classification rule and the COPY (DELETE ... RETURNING)
        # are executed only by the real-PG suite, so it rides the database lane.
        "scripts/node27_purge_superseded_weights.py",
        ("tests/test_node27_purge_superseded_weights_integration.py",),
    ),
    PathTestRule(
        # #1823: the display-API wrapper's entropy reach is now the sixteen
        # partitions, spliced in rather than globbed so the routed set is the
        # reviewed tuple.
        "scripts/ops/start-display-api.sh",
        (
            "tests/test_two_node_docker_runtime.py",
            # #2282: the checkout-anchored restart sweep's process harness.
            "tests/test_start_display_api_restart_anchor.py",
            *ENTROPY_AUDIT_TESTS,
        ),
    ),
    PathTestRule(
        # #1823: the audit owner's corpus. Every partition drives `build_report`
        # or the CLI against this module, so the whole corpus rides the rule.
        # #1842 sank the twenty-one owner modules out of it; this path stays the
        # console entrypoint and the attribute-broadcast facade the whole corpus
        # still patches, and the owner modules get the identical reach below.
        ENTROPY_AUDIT_OWNER_PATH,
        ENTROPY_AUDIT_TESTS,
    ),
    # #1842: one row per owner module -- see ENTROPY_AUDIT_PACKAGE_MODULES for
    # why these are enumerated rather than globbed, and why each carries the
    # whole sixteen-partition corpus.
    PathTestRule("scripts/governance/entropy_audit/archive_status.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/check_env_and_tokens.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/check_line_references.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/check_paths_and_api.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/check_stale_routes.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/check_topology.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/constants.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/facade_guard.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/findings.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/repo_files.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/report.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/route_governing_text.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/route_mentions.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/schema.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/scoped_context.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/structural_budget.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/structural_growth.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/structural_sources.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/structural_surface.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/topology_context_rules.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/topology_display_env.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/topology_predicates.py", ENTROPY_AUDIT_TESTS),
    PathTestRule(
        # #1823: the baseline writer's own cases live in the two
        # `..._baseline_writer_*` partitions, but its output is the baseline the
        # report/allowlist/budget partitions read, so the rule keeps the whole
        # corpus the monolith gave it rather than narrowing reach at the split.
        "scripts/governance/write_entropy_baseline.py",
        ENTROPY_AUDIT_TESTS,
    ),
    PathTestRule(
        # The checked-in node-22 executable is a compatibility CLI; its owner
        # modules and split contract suites are routed explicitly because no
        # same-name test file exists.
        "scripts/node22_scheduler_journal_retention.py",
        (
            "tests/test_scheduler_journal_retention_planning.py",
            "tests/test_scheduler_journal_retention_archive.py",
        ),
    ),
    PathTestRule(
        # #2564: the node-22 evidence retention policy has no same-name suite;
        # route its own suite and the heartbeat-mtime x retention cross suite,
        # which runs this script's `run_retention` on the reader's root.
        "scripts/node22_scheduler_evidence_retention.py",
        (
            "tests/test_scheduler_evidence_retention.py",
            "tests/test_scheduler_evidence_retention_reservation_mtime.py",
            # #2917: the env drift table asserts `config_from_env`'s unset defaults.
            "tests/test_node22_scheduler_env_drift_table.py",
        ),
    ),
    PathTestRule(
        "scripts/select_ci_tests.py",
        (SELECTOR_META_GUARD_TEST,),
    ),
    # #2948 (entropy-burndown task 6.1): the selector's split-out siblings and
    # its parent/head snapshot script route exactly like the facade. Exact
    # strings, no glob: a sibling matches before it exists (fnmatch on the
    # path string), and without its row a sibling edit would arm the
    # unknown-backend core-smoke fallback and a `.sh` edit the
    # `_is_backend_shell_path` fallback instead of selecting this suite.
    PathTestRule("scripts/select_ci_tests_tables.py", (SELECTOR_META_GUARD_TEST,)),
    PathTestRule("scripts/select_ci_tests_rules.py", (SELECTOR_META_GUARD_TEST,)),
    PathTestRule("scripts/select_ci_tests_imports.py", (SELECTOR_META_GUARD_TEST,)),
    PathTestRule("scripts/ci_selector_snapshot.sh", (SELECTOR_META_GUARD_TEST,)),
    # #1650 self-routing: ci.yml's top-level concurrency and the backend
    # paths-filter ARE the contract tests/select_ci_tests.py pins, so a
    # workflow-only PR must select the meta-guard suite and not collapse to
    # core smoke. Backed by ci.yml's own `backend` paths-filter entry; both
    # legs together make a workflow-only PR run these assertions.
    PathTestRule(
        ".github/workflows/ci.yml",
        (
            SELECTOR_META_GUARD_TEST,
            # #2573: the locked-install guard and the native-PROJ regression.
            CI_LOCKED_INSTALL_TEST,
            NATIVE_PROJ_ISOLATION_TEST,
            # #2044 / #2710: the watcher's wiring meta-test reads ci.yml's name,
            # the `unit-test` job name, its shard matrix and the per-shard
            # timeout-minutes.
            FULL_REGRESSION_WATCH_TEST,
            # #2710: the shard matrix wiring (static name, `strategy.job-total`,
            # no command substitution into pytest) and the real-tree partition
            # completeness guard.
            FULL_REGRESSION_SHARD_TEST,
            # #2709: the floating lane's suite pins that ci.yml keeps its three
            # locked installs and never calls the floating resolve.
            FLOATING_RESOLVE_TEST,
        ),
    ),
    PathTestRule(
        # #2044: the watcher workflow and its script. The exact ci.yml backend
        # literal starts the targeted gate for a workflow-only PR.
        FULL_REGRESSION_WATCH_WORKFLOW_PATH,
        (FULL_REGRESSION_WATCH_TEST,),
    ),
    PathTestRule(
        FULL_REGRESSION_WATCH_SCRIPT_PATH,
        (FULL_REGRESSION_WATCH_TEST,),
    ),
    PathTestRule(
        # #2710: the shard partition script.
        FULL_REGRESSION_SHARD_SCRIPT_PATH,
        (FULL_REGRESSION_SHARD_TEST,),
    ),
    PathTestRule(
        # #2710: the shard duration table. Exact ci.yml backend literal.
        FULL_REGRESSION_SHARD_DURATIONS_PATH,
        (FULL_REGRESSION_SHARD_TEST,),
    ),
    PathTestRule(
        # #2709: the floating-resolve workflow (exact ci.yml backend literal)
        # and its report script.
        FLOATING_RESOLVE_WORKFLOW_PATH,
        (FLOATING_RESOLVE_TEST,),
    ),
    PathTestRule(
        FLOATING_RESOLVE_SCRIPT_PATH,
        (FLOATING_RESOLVE_TEST,),
    ),
    PathTestRule(
        # #2602: the hard-gate job meta-test. Exact ci.yml backend literal.
        GOVERNANCE_WORKFLOW_PATH,
        (GOVERNANCE_HARD_GATE_WORKFLOW_TEST,),
    ),
    PathTestRule(
        # #2648: the frozen line-reference baseline. Exact ci.yml backend literal.
        LINE_REFERENCE_BASELINE_PATH,
        (LINE_REFERENCE_TEST,),
    ),
    PathTestRule(
        # #1860: the calibration declaration's assertion-level consumers. The
        # exact backend filter entry starts the targeted gate; this rule
        # converts that lane into real assertions — never the core-smoke
        # fallback or a zero-assertion collect-only run.
        CALIBRATION_OVERRIDES_PATH,
        CALIBRATION_OVERRIDES_CONSUMER_TESTS,
    ),
    PathTestRule(
        # #2261: the review-gate issue memory's structural guard. The exact
        # ci.yml backend filter entry starts the targeted gate for an
        # accounting-only PR; this rule turns that lane into real assertions
        # instead of the zero-assertion collect-only collapse.
        REVIEW_GATE_ISSUE_MEMORY_PATH,
        REVIEW_GATE_ISSUE_MEMORY_CONSUMER_TESTS,
    ),
    PathTestRule(
        # #2261: the memory's tracked writer routes to both memory suites.
        REVIEW_GATE_CLI_PATH,
        (REVIEW_GATE_CLI_TEST, REVIEW_GATE_ISSUE_MEMORY_TEST),
    ),
    PathTestRule(
        # #2477: the attribution audit's own suite.
        LOOP_LOG_AUDIT_PATH,
        (LOOP_LOG_AUDIT_TEST,),
    ),
    PathTestRule(
        # #2477: the frozen ledger; the exact ci.yml backend literal starts
        # the targeted gate, this rule turns it into the real-log guard.
        LOOP_LOG_PATH,
        (LOOP_LOG_AUDIT_TEST, SELECTOR_META_GUARD_TEST),
    ),
    PathTestRule(
        # #1646: a pytest-config change must re-prove the thread-exception
        # policy (the file carries the exact filter and the no-timeout
        # decision) and still keep core smoke plus the selector meta-guard.
        # #1894 additionally registers the dedicated disposable-Dockerker,
        # whose gate contract is asserted without executing its real rows.
        "pyproject.toml",
        (
            *CORE_SMOKE_TESTS,
            *THREAD_EXCEPTION_POLICY_TESTS,
            "tests/test_node27_docker_collection_gate.py",
            # #1765: `tmp_path_retention_policy` lives in the same table and is
            # asserted here (parsed key + the running session's resolved ini).
            PYTHON_ENVIRONMENT_TRUTH_TEST,
            SELECTOR_META_GUARD_TEST,
            # #2632: a dependency change can move SQLAlchemy's default
            # PostgreSQL DBAPI; the helper suite re-proves the engine driver.
            SQLALCHEMY_URL_HELPER_TEST,
            # #2573: a dependency change can bring a second bundled PROJ back.
            NATIVE_PROJ_ISOLATION_TEST,
        ),
    ),
    PathTestRule(
        # #1646: a dependency-lock change could add pytest-timeout, so the lock
        # rule must also run the policy suite (which asserts no such package is
        # resolved) alongside core smoke and the selector meta-guard.
        # #2632: and the SQLAlchemy driver helper suite, as for pyproject.toml.
        # #2573: and the native-PROJ isolation regression, as for pyproject.toml.
        "uv.lock",
        (
            *CORE_SMOKE_TESTS,
            *THREAD_EXCEPTION_POLICY_TESTS,
            SELECTOR_META_GUARD_TEST,
            SQLALCHEMY_URL_HELPER_TEST,
            NATIVE_PROJ_ISOLATION_TEST,
        ),
    ),
    # #1562 structural split owners.  Additive (non-stop) on purpose: the broad
    # `services/orchestrator/**` rule below the stop rules already carries the
    # integration suites for these owners, and these narrow rules only attach
    # the dedicated focused suite.  Without them an owner-only PR would run the
    # integration suites but never this suite's own assertions.
    PathTestRule(
        "services/orchestrator/chain_forced_resubmit.py",
        FORCED_RESUBMIT_SURFACE_TESTS,
    ),
    PathTestRule(
        "services/orchestrator/chain_array_evidence.py",
        FORCED_RESUBMIT_SURFACE_TESTS,
    ),
    # #1684 shared-auth owner-to-focused-suite mappings (EVID-01). Additive
    # (non-stop) on purpose: the broad `apps/api/**` / `services/orchestrator/**`
    # rules keep their existing riders, `packages/common/**` keeps its #1744
    # core-smoke baseline and the #1656 timescale rider; these rows only attach
    # the focused contracts an owner-only PR previously could not reach.
    PathTestRule(
        # #2757: the basin retirement tool builds its explicit deactivate
        # decision with `trusted_internal_policy_decision`; its partitions
        # assert the decision the store receives.
        "packages/common/auth_policy.py",
        (AUTH_POLICY_TEST, *BASIN_RETIREMENT_TESTS),
    ),
    PathTestRule(
        "packages/common/request_auth.py",
        (
            SLURM_AUTH_CORE_TEST,
            SLURM_AUTH_FULLMOUNT_TEST,
            SLURM_AUTH_CLIENT_TEST,
            SLURM_AUTH_DEPLOYMENT_TEST,
        ),
    ),
    PathTestRule(
        "packages/common/openapi_auth_security.py",
        (SLURM_OPENAPI_SECURITY_TEST,),
    ),
    # Retained PGDATA owners keep only their real command, migration, oracle,
    # evidence, and C4 acceptance consumers.
    PathTestRule(
        "packages/common/node27_pgdata_command.py",
        (
            "tests/test_node27_pgdata_command.py",
            "tests/test_node27_pgdata_migrate.py",
            "tests/test_node27_pgdata_migrate_oracle.py",
            *C4_PRODUCTION_ACCEPTANCE_TESTS,
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_container.py",
        (
            "tests/test_node27_pgdata_container.py",
            "tests/test_node27_pgdata_migrate.py",
            "tests/test_node27_pgdata_migrate_oracle.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_evidence.py",
        (
            "tests/test_node27_pgdata_evidence.py",
            "tests/test_node27_pgdata_migrate.py",
            "tests/test_node27_pgdata_migrate_oracle.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_host.py",
        (
            "tests/test_node27_pgdata_migrate.py",
            "tests/test_node27_pgdata_migrate_oracle.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_migrate.py",
        (
            "tests/test_node27_pgdata_migrate.py",
            "tests/test_node27_pgdata_migrate_oracle.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        "scripts/node27_pgdata_migrate.py",
        (
            "tests/test_node27_pgdata_migrate.py",
            "tests/test_node27_pgdata_migrate_oracle.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_pgdata_migrate.py",
        ("tests/test_node27_pgdata_migrate.py",),
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_pgdata_migrate_oracle.py",
        ("tests/test_node27_pgdata_migrate_oracle.py",),
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_resource_governance_collection.py",
        (
            "tests/test_node27_resource_governance.py",
            "tests/test_node27_working_set.py",
            "tests/test_node27_maintenance_output_integration.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_resource_governance.py",
        (
            "tests/test_node27_resource_governance.py",
            "tests/test_node27_working_set.py",
            "tests/test_node27_maintenance_output_integration.py",
        ),
    ),
    PathTestRule(
        "infra/env/node27-resource-governance.example",
        (
            "tests/test_node27_resource_governance.py",
            "tests/test_node27_working_set.py",
        ),
    ),
    PathTestRule(
        "infra/systemd/nhms-node27-resource-governance.service",
        ("tests/test_node27_resource_governance.py",),
    ),
    PathTestRule(
        "infra/systemd/nhms-node27-resource-governance.timer",
        ("tests/test_node27_resource_governance.py",),
    ),
    PathTestRule(
        "scripts/node27_resource_governance_once.sh",
        (*CORE_SMOKE_TESTS, "tests/test_node27_resource_governance.py"),
    ),
    PathTestRule(
        "apps/api/auth.py",
        (
            AUTH_POLICY_TEST,
            "tests/test_role_boundary_static.py",
        ),
    ),
    PathTestRule(
        "services/orchestrator/chain_slurm_client.py",
        (SLURM_AUTH_CLIENT_TEST,),
    ),
    PathTestRule(
        "services/orchestrator/scheduler_gateway.py",
        (SLURM_AUTH_DEPLOYMENT_TEST,),
    ),
    PathTestRule(
        "apps/api/routes/pipeline.py",
        (
            *CONNECTION_ATTRIBUTION_TESTS,
            "tests/test_pipeline_ops_identity_envelope.py",
            # #2348: the eleven ops routes' handler objects vs their models.
            *RESPONSE_MODEL_PRESERVATION_TESTS,
            # #2385/#2387: the read-blocked sentinel suite drives
            # `POST /runs/{run_id}/retry` through TestClient for both consumer
            # ends -- the 409 `RETRY_EVIDENCE_INVALID` refusal body and the
            # recorded-failure delta on the provenance walk -- so a change to
            # this route's except table or its 503 assembly must run it.
            "tests/test_file_journal_read_blocked_consumers.py",
            # #2308: the ONLY oracle for the cancel side's two-shape invariant --
            # the gateway error rendered on the response body and left raw in the
            # persisted `slurm_cancellation_gap` / `cancel_failed` event, asserted
            # in one test. The broad `apps/api/**` rule buys the three generic API
            # suites, none of which call `cancel_run`.
            "tests/test_retry_cancel_consistency.py",
            # #2568: the ONLY HTTP-level oracle for `POST /runs/{run_id}/retry`
            # (whole file: the route's 409/503 bodies, the file-lane render and the
            # recorded submission event are all driven through TestClient here).
            "tests/test_retry.py",
            # #2576: this module is in the selector suite's GUARDED_MODULE_CLOSURES
            # (direct-only mode), so the rule carries its whole DIRECT non-gated
            # importer set, derived from the tracked tree. The two direct importers
            # not named here (tests/test_api_contract_pipeline_ops.py,
            # tests/test_monitoring_api.py) arrive through the broad `apps/api/**`
            # rule. tests/test_openapi_31_contract.py imports this module at file
            # level, so it is a direct importer in its own right, not a precip
            # rider leaking into a CONNECTION_ATTRIBUTION peer.
            "tests/test_e2e_m3.py",
            "tests/test_openapi_31_contract.py",
            "tests/test_pipeline_logs_artifacts.py",
            SLURM_OPENAPI_SECURITY_TEST,
        ),
    ),
    PathTestRule(
        # #2385: the manual-retry source selector's THIRD caller. `_preview`
        # runs outside `main()`'s try, so the selector's new refusal has to be
        # turned into a receipt entry here; the sentinel suite is the only
        # oracle for that receipt shape and exit code.
        "scripts/node22_manual_retry_failed_runs.py",
        ("tests/test_file_journal_read_blocked_consumers.py",),
    ),
    # #2348: the four route members also declare response models, so they carry
    # the preservation oracle. A store module's diff does NOT reach the oracle
    # through a route rule: selection is per changed path, so every store whose
    # production builder the oracle drives names it on its own rule --
    # forecast_store.py, best_available.py, state_manager.py (their exact rules)
    # and object_store_forcing.py (below) -- plus the model registry facade and
    # its `model_registry_*.py` owners on the store-path rule right after this
    # one: the registry samples fake the store, not the `sanitize_*` projections
    # the route applies (see CONNECTION_ATTRIBUTION_STORE_PATHS).
    # tests/test_select_ci_tests.py derives that set from the oracle's imports
    # plus the builders it names.
    *(
        PathTestRule(path, (*CONNECTION_ATTRIBUTION_TESTS, *RESPONSE_MODEL_PRESERVATION_TESTS))
        for path in CONNECTION_ATTRIBUTION_ROUTE_PATHS
    ),
    *(
        PathTestRule(
            path,
            # #2757: the basin retirement tool deactivates through the store's
            # lifecycle operation and decides from the shape it returns; its fake
            # store mirrors that shape, so a change to the facade or an owner
            # (the lifecycle owner above all) must run its partitions.
            (
                *CONNECTION_ATTRIBUTION_TESTS,
                *RESPONSE_MODEL_PRESERVATION_TESTS,
                *MODEL_REGISTRY_IMPORTER_TESTS,
                *BASIN_RETIREMENT_TESTS,
            ),
        )
        for path in CONNECTION_ATTRIBUTION_STORE_PATHS
    ),
    PathTestRule(
        # #2612: the station-flag flip hook. Its direct non-gated importer suites
        # (the `_atomic` flip partition and the cutover history suite; the
        # selector suite's GUARDED_MODULE_CLOSURES derives them, one-hop adds
        # none) plus the `_mvt_set` partition, which reaches this module only
        # through tests/direct_grid_display_cutover_flip_helpers.py -- an edge the
        # guard cannot derive, so it is pinned by name there.
        "packages/common/station_set_flip.py",
        (
            *DIRECT_GRID_DISPLAY_CUTOVER_FLIP_TESTS,
            "tests/test_direct_grid_display_cutover_history.py",
        ),
    ),
    PathTestRule(
        # A #1728 connection-attribution store (see CONNECTION_ATTRIBUTION_STORE_PATHS)
        # that also holds `read_station_forcing_csv`, the builder behind the
        # station-series payload the #2348 preservation oracle compares with
        # master (reached through tests/test_forecast_api_met_station_series.py's
        # client).
        "packages/common/object_store_forcing.py",
        (*CONNECTION_ATTRIBUTION_TESTS, *RESPONSE_MODEL_PRESERVATION_TESTS),
    ),

    PathTestRule(
        "apps/api/errors.py",
        (API_ERROR_LOGGING_TEST, "tests/test_display_mvt_cold_admission.py"),
    ),
    PathTestRule(
        # #2098: the route-reachability composition owner. This module imports
        # `precip_router` into `_BUSINESS_ROUTERS`, and `register_role_aware_routes`
        # walks that tuple to `include_router` each one; drop the entry and both
        # published endpoints — /api/v1/precip/{source}/{cycle}/index and
        # .../{valid_time}.png — leave the route table entirely.
        # tests/test_precip_overlay.py is the most direct behavioural oracle (see its
        # `test_dropping_precip_router_...` mutation proof); the cut also reds
        # tests/test_openapi_drift.py (its whole-document static/runtime comparison — the
        # committed openapi/nhms.v1.yaml carries both precip paths) and
        # tests/test_openapi_31_contract.py (its BASELINE_NULLABLE_COUNT counts the two
        # routes' typed 404s, as that constant's own comment says). Naming all three is
        # over-justification, not under-coverage. The three broad `apps/api/**` suites
        # exercise no precip route, so before this entry a registry-only diff reached
        # the targeted lane with a plausible five-suite selection and no precipitation
        # oracle at all (#1182's zero-assertion warning cannot fire on a non-empty
        # selection).
        # #1728's connection-attribution guards are MERGED here rather than left in
        # CONNECTION_ATTRIBUTION_ROUTE_PATHS: this module now has an exact rule, and
        # a duplicate pattern splits its ownership across two
        # (test_path_rule_duplicate_patterns_are_allowlisted_decisions). Same shape
        # #2078 used for apps/api/routes/forecast.py.
        "apps/api/route_registry.py",
        (*CONNECTION_ATTRIBUTION_TESTS, *PRECIP_SURFACE_TESTS),
    ),
    PathTestRule(
        "apps/api/main.py",
        (
            API_ERROR_LOGGING_TEST,
            "tests/test_display_mvt_cold_admission.py",
            # #2098: the runtime-OpenAPI composition owner. `_patch_openapi_schema`
            # calls `_patch_precip_openapi(schema)`, which pops the generated
            # `PrecipIndexResponse` component and rewrites the index operation onto
            # the shared `allOf: [SuccessEnvelope, {data}]` envelope that the
            # hand-maintained openapi/nhms.v1.yaml carries. Drop that call site and
            # the runtime schema drifts from the committed document at both places,
            # and of the suites this rule selects only tests/test_openapi_drift.py
            # reds (see its `test_dropping_the_precip_openapi_patch_...` mutation
            # proof). That is a claim about THIS call site, not "the repo's only
            # static/runtime oracle": tests/test_api_contract.py compares
            # openapi/nhms.v1.yaml against `app.openapi()` too, but at no
            # precipitation path. Neither the #1704 error-logging rider above nor the
            # three broad `apps/api/**` suites assert on the precipitation schema, and
            # tests/test_openapi_31_contract.py applies openapi_patching's patch
            # functions directly — a list that omits `_patch_precip_openapi`.
            *PRECIP_SURFACE_TESTS,
        ),
    ),
)
