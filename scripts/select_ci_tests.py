from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# The selector's own suite carries tracked-tree-derived meta-guards (same-name
# script pairs, container-contract closure, guarded-module importer closure). A
# PR that adds or moves a test file can invalidate any of them, so every changed
# test suite under `tests/` drags this suite along (~6s) instead of letting the
# guards go unrun on exactly the change class they exist for.
SELECTOR_META_GUARD_TEST = "tests/test_select_ci_tests.py"
# pytest's own collection rule, mirrored: BOTH default `python_files` patterns,
# matched against the BASENAME (which is how pytest itself matches a slash-free
# pattern). Two hand-derivations of this predicate were wrong in a row — first
# path-shaped (`tests/test_*.py`, which reads `tests/pkg/test_y.py` as a support
# module), then a single pattern (which reads `tests/x_test.py` as one). Both
# misclassifications cost a real suite its self-selection AND the meta-guard
# accumulation. tests/test_select_ci_tests.py now anchors this list to what
# pytest actually collects, so the next drift reddens instead of shipping.
CHANGED_TEST_SUITE_BASENAME_PATTERNS: tuple[str, ...] = ("test_*.py", "*_test.py")


def is_test_suite_path(path: str) -> bool:
    """True iff pytest would collect tests from ``path`` by name.

    The single classification decision in this module: it feeds changed-test
    self-selection AND the meta-guard accumulation, and the selector's test
    suite imports it rather than restating the patterns, so no caller can drift
    from pytest independently of the anchor test.
    """
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatch(name, pattern) for pattern in CHANGED_TEST_SUITE_BASENAME_PATTERNS)


CORE_SMOKE_TESTS: tuple[str, ...] = (
    "tests/test_api.py",
    "tests/test_gateway.py",
    "tests/test_migrations.py",
    "tests/test_orchestration_chain.py",
    "tests/test_production_scheduler.py",
)


@dataclass(frozen=True)
class PathTestRule:
    pattern: str
    tests: tuple[str, ...]
    stop_on_match: bool = False
    only_when_any_changed: tuple[str, ...] = ()


ORCHESTRATOR_MANIFEST_SURFACE_TESTS: tuple[str, ...] = (
    "tests/test_orchestration_chain.py::test_static_chain_type_module_import_resolves_hints_without_heavy_runtime_imports",
    "tests/test_orchestration_chain.py::test_chain_type_exports_preserve_legacy_identity_and_dataclass_contracts",
    "tests/test_orchestration_chain.py::test_model_run_forcing_package_manifest_identity_reaches_runtime_manifest",
    "tests/test_orchestration_chain.py::test_psycopg_find_forcing_context_populates_package_manifest_metadata",
    "tests/test_production_scheduler.py::test_scheduler_routes_ready_canonical_candidate_to_slurm_forcing_without_local_producer",
    "tests/test_production_scheduler.py::test_scheduler_does_not_replace_candidate_identity_from_local_forcing_result",
    "tests/test_production_scheduler.py::test_runtime_manifest_assembly_uses_shud_output_count_not_gis_segment_count",
)


ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS: tuple[str, ...] = (
    "services/orchestrator/chain_types.py",
    "services/orchestrator/chain_manifests.py",
    "services/orchestrator/chain.py",
    "services/orchestrator/scheduler.py",
)

DIRECT_GRID_E2E_TESTS: tuple[str, ...] = (
    "tests/test_direct_grid_e2e.py",
)

DIRECT_GRID_CONTRACT_TESTS: tuple[str, ...] = (
    "tests/test_forcing_producer.py::test_direct_grid_contract_valid_nested_manifest_still_parses",
    "tests/test_forcing_producer.py::test_direct_grid_contract_rejects_explicit_root_direct_grid_when_root_authority_disabled",
    "tests/test_forcing_producer.py::test_direct_grid_contract_missing_manifest_field_raises_structured_error",
    "tests/test_forcing_producer.py::test_direct_grid_contract_missing_station_field_raises_structured_error",
    "tests/test_forcing_producer.py::test_direct_grid_contract_duplicate_shud_forcing_index_is_rejected",
    "tests/test_forcing_producer.py::test_direct_grid_contract_duplicate_forcing_filename_is_rejected",
    "tests/test_forcing_producer.py::test_direct_grid_contract_source_scope_must_be_nonempty_and_apply_to_current_source",
    "tests/test_forcing_producer.py::test_direct_grid_contract_station_coordinates_must_be_in_wgs84_bounds",
    "tests/test_forcing_producer.py::test_direct_grid_contract_station_longitude_is_normalized_for_shud_output",
    "tests/test_forcing_producer.py::test_direct_grid_contract_unsupported_top_level_mode_fails_before_nested_direct_grid",
)

DIRECT_GRID_SURFACE_TESTS: tuple[str, ...] = DIRECT_GRID_E2E_TESTS + DIRECT_GRID_CONTRACT_TESTS

# Non-gated top-level importers of workers/forcing_producer/direct_grid_contract.py
# that the focused DIRECT_GRID_SURFACE_TESTS node ids never reach (#1455). The
# contract module is owned by a stop_on_match rule, so the
# `workers/forcing_producer/**` rule below is unreachable for it and these have
# to ride the stop rule itself. Kept as a separate tuple appended at the rule
# site so DIRECT_GRID_SURFACE_TESTS keeps meaning "the compact e2e fixture" for
# every other reader.
DIRECT_GRID_CONTRACT_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_direct_grid_variant_registration.py",
    "tests/test_legacy_reactivation_guard.py",
    "tests/test_mapping_builder_binding.py",
    "tests/test_mapping_builder_cli.py",
    "tests/test_mapping_builder_integration.py",
)

DIRECT_GRID_SURFACE_PATH_PATTERNS: tuple[str, ...] = (
    "workers/forcing_producer/direct_grid_contract.py",
    "openspec/changes/direct-grid-forcing/**",
)

FILE_JOURNAL_READ_STATE_TESTS: tuple[str, ...] = (
    "tests/test_file_orchestration_journal.py",
    "tests/test_file_orchestration_migration.py",
    "tests/test_orchestration_chain.py::test_psycopg_candidate_state_limits_jobs_and_reads_events_for_candidate_scope",
    "tests/test_orchestration_chain.py::test_psycopg_candidate_state_latest_truth_timestamp_selects_terminal_success",
    "tests/test_orchestration_chain.py::test_psycopg_active_slurm_jobs_includes_cycle_run_array_job_for_filtered_model",
    "tests/test_orchestration_chain.py::test_psycopg_active_slurm_jobs_includes_queued_pipeline_rows",
    "tests/test_orchestration_chain.py::test_psycopg_has_active_pipeline_includes_queued_pipeline_rows",
    "tests/test_orchestration_chain.py::test_psycopg_find_forcing_context_populates_package_manifest_metadata",
    "tests/test_production_scheduler.py::test_fresh_cycle_with_active_slurm_job_does_not_double_submit",
    "tests/test_production_scheduler.py::test_db_free_injected_collaborators_plan_without_unimplemented_provider_blocker",
    "tests/test_production_scheduler.py::test_db_free_injected_factory_ready_candidate_submit_blocks_without_factory_call",
    "tests/test_production_scheduler.py::test_db_free_journal_write_block_forces_retention_dry_run_before_deletion",
    "tests/test_production_scheduler.py::test_db_free_injected_factory_active_slurm_status_sync_blocks_without_factory_call",
    "tests/test_production_scheduler.py::test_db_free_injected_factory_cancel_active_slurm_blocks_without_factory_call",
    "tests/test_production_scheduler.py::test_db_free_from_env_raw_ready_canonical_zero_submits_convert_without_download_source_cycle",
    "tests/test_production_scheduler.py::test_db_free_from_env_raw_missing_blocks_canonical_zero_without_submission",
    "tests/test_production_scheduler.py::test_db_free_from_env_raw_invalid_blocks_without_submission",
    "tests/test_production_scheduler.py::test_db_free_scheduler_fake_slurm_submission_writes_file_journal_without_database_url",
    "tests/test_source_cycle_raw_manifest.py",
)

# #1455 at-site extensions for the four orchestrator modules whose stop rules
# make the `services/orchestrator/**` rule unreachable. Each tuple is appended
# to ONE rule below with `(*SHARED_TESTS, *THIS)`; the shared constants
# themselves stay untouched, because they also serve patterns outside the nine
# audited directories (FILE_JOURNAL_READ_STATE_TESTS is used by
# packages/common/safe_fs.py) where selection must not move.
#
# chain.py is the widest importer surface in the audit. Everything here is a
# non-gated top-level importer whose subject IS the chain (~193s measured all
# together); the chain's cross-surface importers — production-closure,
# slurm-gateway, model-registry and forcing-producer suites — stay with the
# rules that own them and are recorded as `edge-consumer` in
# tests/test_select_ci_tests.py.
CHAIN_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_analysis_pipeline.py",
    "tests/test_chain_repository_nfs_raw_manifest.py",
    "tests/test_e2e.py",
    "tests/test_e2e_ifs.py",
    "tests/test_e2e_m3.py",
    "tests/test_file_orchestration_journal.py",
    "tests/test_ifs_forecast_integration.py",
    "tests/test_orchestrator.py",
    "tests/test_partial_success.py",
    "tests/test_pipeline_logs_artifacts.py",
    "tests/test_warm_start.py",
    "tests/test_warm_start_chaining.py",
)

SCHEDULER_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_cli_publish_qdown.py",
    "tests/test_scheduler_backfill.py",
    "tests/test_scheduler_backfill_predecessor.py",
    "tests/test_scheduler_timing.py",
    "tests/test_source_scoped_dispatch.py",
)

ORCHESTRATOR_CLI_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_cli_cleanup_frontier.py",
    "tests/test_cli_publish_qdown.py",
    "tests/test_retention_frontier.py",
    "tests/test_scheduler_backfill.py",
)

FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_file_orchestration_journal_read_cache.py",
    "tests/test_scheduler_backfill.py",
)

FILE_JOURNAL_READ_STATE_PATH_PATTERNS: tuple[str, ...] = (
    "packages/common/safe_fs.py",
    "services/orchestrator/chain_repository_state.py",
    "services/orchestrator/file_orchestration_journal.py",
    "services/orchestrator/file_orchestration_migration.py",
    "services/orchestrator/cli.py",
    "services/orchestrator/scheduler.py",
    "services/orchestrator/scheduler_core.py",
    "services/orchestrator/scheduler_runtime.py",
)


# tests/test_sql_shape_helpers.py is both a test module and the SQL-shape
# ORACLE the #1341 read-path negative pins are written against
# (`strip_scalar_subqueries`). Its own self-tests run because it self-selects,
# but a helper-only diff would otherwise leave the three consumer files
# unselected — and a silently over-eager stripper makes those pins vacuous
# without failing anything here. The rule pulls the consumers in with it.
SQL_SHAPE_ORACLE_TESTS: tuple[str, ...] = (
    "tests/test_sql_shape_helpers.py",
    "tests/test_river_ts_read_path_surrogate_keys.py",
    "tests/test_display_coverage_refresh.py",
    "tests/test_migrations.py",
)


CHANGED_TEST_FILE_RULES: tuple[PathTestRule, ...] = (
    PathTestRule(
        "tests/test_sql_shape_helpers.py",
        SQL_SHAPE_ORACLE_TESTS,
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
# importer suites instead. Exact paths, no globs: five entries, and
# tests/test_select_ci_tests.py DERIVES the required sets from the tracked tree
# (never freezes them), so a new importer suite reddens the closure guard naming
# the module and the missing suite. `tests/integration_helpers.py` and
# `tests/conftest.py` are deliberately absent — an issue-#1487 scope carve-out
# recorded with its measured partial coverage in that suite's allowlist.
SUPPORT_MODULE_TEST_RULES: tuple[PathTestRule, ...] = (
    PathTestRule(
        "tests/fixtures/mapping_builder/in_memory_grid_snapshot.py",
        (
            "tests/test_mapping_builder_algorithm.py",
            "tests/test_mapping_builder_binding.py",
            "tests/test_mapping_builder_cli.py",
            "tests/test_mapping_builder_evidence.py",
            "tests/test_mapping_builder_integration.py",
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
        "tests/river_identity_backfill_fakes.py",
        (
            "tests/test_node27_river_identity_backfill.py",
            "tests/test_node27_river_identity_backfill_receipt.py",
        ),
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
            "tests/test_scheduler_file_provider_refresh.py",
            "tests/test_state_manager.py",
            "tests/test_run_tree_copyback.py",
            "tests/test_source_cycle_raw_manifest.py",
            "tests/test_publish_scheduler_file_registry.py",
        ),
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
            "tests/test_integration_gate.py",
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
)


PATH_TEST_RULES: tuple[PathTestRule, ...] = (
    PathTestRule(
        ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS[0],
        ORCHESTRATOR_MANIFEST_SURFACE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS[1],
        ORCHESTRATOR_MANIFEST_SURFACE_TESTS,
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
        DIRECT_GRID_SURFACE_PATH_PATTERNS[0],
        # Extended AT THE RULE SITE, not by editing DIRECT_GRID_SURFACE_TESTS:
        # the shared constant also serves the openspec-change pattern below,
        # whose selection must not move (#1455 scopes to the nine directories).
        (*DIRECT_GRID_SURFACE_TESTS, *DIRECT_GRID_CONTRACT_IMPORTER_TESTS),
        stop_on_match=True,
    ),
    PathTestRule(
        DIRECT_GRID_SURFACE_PATH_PATTERNS[1],
        DIRECT_GRID_SURFACE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        # Extended AT THE RULE SITE (#1192), not by editing the shared constant:
        # safe_fs.py owns tests/test_safe_fs.py, but the constant also serves the
        # seven journal patterns below, whose selection must not move. A separate
        # stop_on_match rule for safe_fs.py would instead SHIFT selection --
        # first match wins, so the journal closure would drop out entirely. The
        # result here is the union: today's journal closure PLUS the helper's own
        # suite, which a safe_fs-only change previously could not reach at all.
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[0],
        (*FILE_JOURNAL_READ_STATE_TESTS, "tests/test_safe_fs.py"),
        stop_on_match=True,
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
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[3],
        FILE_JOURNAL_READ_STATE_TESTS,
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
        # The three object-store-root additions (#1455) are non-gated top-level
        # importer suites of the adapters that this rule already owns:
        # test_object_store_roots.py builds ERA5/GFS/IFS adapters directly, and
        # the bbox pair consumes workers/data_adapters/region.py. They are cheap
        # (0.8-2.4s) and every adapter shares the object-store root convention,
        # so they ride the directory list rather than three narrow rules.
        "workers/data_adapters/**",
        (
            "tests/test_gfs_adapter.py",
            "tests/test_ifs_adapter.py",
            "tests/test_era5_adapter.py",
            "tests/test_data_adapter_resolution.py",
            "tests/test_production_scheduler.py",
            "tests/test_object_store_roots.py",
            "tests/test_grid_registry_bbox.py",
            "tests/test_producer_bbox_preflight.py",
            "tests/test_e2e.py",
            "tests/test_e2e_ifs.py",
        ),
    ),
    PathTestRule(
        # base.py's remaining non-gated importers are orchestration-layer suites
        # borrowing its cycle-identity helpers (cycle_id_for / format_cycle_time
        # / CycleDiscovery); those stay with the rules that own them (#1455
        # `edge-consumer` routings). test_state_clone_cutover_hook.py is the one
        # with no owning rule anywhere, so a base.py change would otherwise never
        # run it. Narrow on purpose — only a base.py PR pays for it.
        "workers/data_adapters/base.py",
        ("tests/test_state_clone_cutover_hook.py",),
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
        ),
    ),
    PathTestRule(
        "workers/shud_runtime/**",
        (
            "tests/test_shud_runtime.py",
            "tests/test_runtime_mode.py",
            "tests/test_runtime_ic_header.py",
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
            "tests/test_basins_package_publication.py",
            "tests/test_basins_registry_import.py",
            "tests/test_basins_reingest.py",
            "tests/test_direct_grid_variant_registration.py",
            "tests/test_hhe_mvt_binding.py",
            "tests/test_publish_scheduler_file_registry.py",
            "tests/test_qhh_production_bootstrap.py",
            "tests/test_qhh_scripts_static.py",
        ),
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
        ),
    ),
    PathTestRule(
        # #1455: parser.py alone carries these two importers (46s + 3s), so they
        # stay off the directory list and are paid only by a parser.py PR.
        "workers/output_parser/parser.py",
        (
            "tests/test_analysis_pipeline.py",
            "tests/test_timescale_write_guard_wired.py",
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
            "tests/test_production_scheduler.py",
            "tests/test_scheduler_backfill.py",
            "tests/test_warm_start_chaining.py",
            "tests/test_cli_cleanup_frontier.py",
            "tests/test_cli_publish_qdown.py",
            "tests/test_file_orchestration_journal.py",
            "tests/test_file_orchestration_journal_read_cache.py",
            "tests/test_file_orchestration_migration.py",
            "tests/test_live_monitoring.py",
            "tests/test_monitoring_api.py",
            "tests/test_pipeline_persistence.py",
            "tests/test_publish_scheduler_file_registry.py",
            "tests/test_reconcile_sacct_parse.py",
            "tests/test_replay_lineage.py",
            "tests/test_retention.py",
            "tests/test_retention_frontier.py",
            "tests/test_retry.py",
            "tests/test_retry_cancel_consistency.py",
            "tests/test_run_identity.py",
            "tests/test_run_tree_copyback.py",
            "tests/test_scheduler_backfill_predecessor.py",
            "tests/test_scheduler_file_provider_refresh.py",
            "tests/test_scheduler_generation.py",
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
            "tests/test_gateway_reconcile.py",
            "tests/test_slurm_gateway_app.py",
            "tests/test_slurm_route_contract.py",
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
        # tests/test_gateway_reconcile.py is a one-hop member too but already
        # rides the `services/slurm_gateway/**` rule; it is not repeated here.
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
            "tests/test_static_serving.py",
            "tests/test_cli_publish_qdown.py",
        ),
    ),
    PathTestRule(
        "services/tiles/mvt.py",
        (
            "tests/test_api_contract.py",
            "tests/test_display_publish_status_only.py",
            "tests/test_migrations.py",
            "tests/test_openapi_drift.py",
            # The #1341 surrogate-key / transitional-pushdown shape pins live
            # here; an mvt.py diff that quietly drops a predicate pairing must
            # not reach CI green without them.
            "tests/test_river_ts_read_path_surrogate_keys.py",
        ),
    ),
    # The other two #1341 switched surfaces. Both are covered by broad rules
    # (packages/common/** and the API route rules) that do not include the
    # read-path shape pins, so without these entries a diff that drops a
    # pushdown pairing or reintroduces a text fact predicate in either file
    # reaches CI green unchallenged.
    PathTestRule(
        "apps/api/routes/hydro_display.py",
        ("tests/test_river_ts_read_path_surrogate_keys.py",),
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
            "tests/test_production_object_store_validation.py",
            "tests/test_production_slurm_validation.py",
            "tests/test_production_scale_validation.py",
            "tests/test_production_e2e_validation.py",
            "tests/test_production_met_validation.py",
            "tests/test_readonly_db_validation.py",
            "tests/test_two_node_e2e_evidence.py",
        ),
    ),
    PathTestRule(
        "packages/common/object_store.py",
        (
            "tests/test_object_store_roots.py",
            "tests/test_storage.py",
        ),
    ),
    PathTestRule(
        "packages/common/forecast_store.py",
        (
            "tests/test_forecast_api.py",
            "tests/test_list_search_contract.py",
            "tests/test_migrations.py",
            "tests/test_model_registry_list_basins.py",
            "tests/test_qhh_latest_fallback_pushdown.py",
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
            "tests/test_display_coverage_refresh.py",
            "tests/test_display_coverage_parallel.py",
            "tests/test_forecast_api.py",
            "tests/test_river_ts_read_path_surrogate_keys.py",
        ),
    ),
    PathTestRule(
        "packages/common/state_manager.py",
        (
            "tests/test_state_manager.py",
            "tests/test_state_qc.py",
        ),
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
        ("tests/test_redaction.py",),
    ),
    PathTestRule(
        "packages/common/node27_container_contract.py",
        (
            "tests/test_node27_external_contract_snapshot.py",
            "tests/test_node27_timeseries_compression_benchmark.py",
            "tests/test_node27_timeseries_compression_capture.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_node27_timeseries_compression_supervisor.py",
            "tests/test_node27_timeseries_decompression_replay.py",
        ),
    ),
    PathTestRule(
        "packages/common/node27_external_contract_snapshot.json",
        ("tests/test_node27_external_contract_snapshot.py",),
    ),
    PathTestRule(
        "apps/api/**",
        (
            "tests/test_api.py",
            "tests/test_api_contract.py",
            "tests/test_monitoring_api.py",
        ),
    ),
    PathTestRule(
        "db/**",
        ("tests/test_migrations.py",),
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
        "scripts/validate_two_node_docker_runtime.py",
        ("tests/test_two_node_docker_runtime.py",),
    ),
    PathTestRule(
        "scripts/validate_two_node_docker_source_trust.py",
        ("tests/test_two_node_docker_source_trust.py",),
    ),
    PathTestRule(
        "scripts/validate_readonly_db_boundary.py",
        ("tests/test_readonly_db_validation.py",),
    ),
    PathTestRule(
        "scripts/run_qhh_continuous.py",
        ("tests/test_run_qhh_continuous.py",),
    ),
    PathTestRule(
        # No same-name tests/test_node27_autopipeline.py exists, so without this
        # rule the autopipe script falls through to the core-smoke fallback and
        # none of its own suites run.
        "scripts/node27_autopipeline.py",
        (
            "tests/test_node27_autopipeline_preflight.py",
            "tests/test_node27_autopipeline_handoff.py",
            "tests/test_display_publish_status_only.py",
        ),
    ),
    PathTestRule(
        # The cron wrapper is a shell script, not a backend python path, so the
        # core-smoke fallback never arms for it; without this rule a
        # wrapper-only PR selects nothing and CI degrades to --collect-only.
        "scripts/node27_autopipe_cron.sh",
        ("tests/test_node27_autopipeline_preflight.py",),
    ),
    # Shell wrappers with committed guard suites (#1138). Like the autopipe
    # cron rule above, none of these are backend python paths, so without an
    # explicit mapping a wrapper-only PR would select nothing; targets were
    # derived from `grep -rln '<script>.sh' tests/` and must track real
    # references. Wrappers with no guard suite intentionally have no rule here
    # and arm the core-smoke fallback via _is_backend_shell_path.
    PathTestRule(
        "scripts/scheduler_file_provider_refresh_once.sh",
        ("tests/test_scheduler_file_provider_refresh.py",),
    ),
    PathTestRule(
        "scripts/install_node22_scheduler_file_provider_refresh.sh",
        ("tests/test_scheduler_file_provider_refresh.py",),
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
            "tests/test_node27_wrapper_pythonpath.py",
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
        "scripts/node27_frontier_stall_alert_once.sh",
        ("tests/test_node27_frontier_stall_alert.py",),
    ),
    PathTestRule(
        "scripts/run_qhh_backend_smoke.sh",
        ("tests/test_qhh_scripts_static.py",),
    ),
    PathTestRule(
        "scripts/run_qhh_cycle.sh",
        (
            "tests/test_run_qhh_continuous.py",
            "tests/test_role_boundary_static.py",
            "tests/test_qhh_scripts_static.py",
        ),
    ),
    PathTestRule(
        "scripts/local_pg.sh",
        ("tests/test_qhh_scripts_static.py",),
    ),
    PathTestRule(
        "scripts/ops/node22-run-cycle-once.sh",
        ("tests/test_production_scheduler.py",),
    ),
    PathTestRule(
        "scripts/ops/start-display-api.sh",
        (
            "tests/test_two_node_docker_runtime.py",
            "tests/test_entropy_audit_script.py",
        ),
    ),
    PathTestRule(
        "scripts/governance/audit_repo_entropy.py",
        ("tests/test_entropy_audit_script.py",),
    ),
    PathTestRule(
        "scripts/governance/write_entropy_baseline.py",
        ("tests/test_entropy_audit_script.py",),
    ),
    PathTestRule(
        "scripts/select_ci_tests.py",
        (SELECTOR_META_GUARD_TEST,),
    ),
    PathTestRule(
        "pyproject.toml",
        CORE_SMOKE_TESTS,
    ),
    PathTestRule(
        "uv.lock",
        CORE_SMOKE_TESTS,
    ),
)


def select_tests(changed_paths: Iterable[str], *, repo_root: Path = Path(".")) -> list[str]:
    selected: set[str] = set()
    changed = [path.strip().replace("\\", "/") for path in changed_paths if path.strip()]
    unknown_backend_path = False

    for path in changed:
        if path.startswith("tests/") and path.endswith(".py"):
            is_test_suite = is_test_suite_path(path)
            matched_changed_test = False
            for rule in CHANGED_TEST_FILE_RULES:
                if rule.only_when_any_changed and not _any_path_matches(changed, rule.only_when_any_changed):
                    continue
                if fnmatch.fnmatch(path, rule.pattern):
                    selected.update(rule.tests)
                    matched_changed_test = True
                    if rule.stop_on_match:
                        break
            if not matched_changed_test:
                matched_support_module = False
                # Support-module routing (#1487) is reachable only here: after
                # the CHANGED_TEST_FILE_RULES loop found nothing, and only for a
                # non-suite path. The two domains are disjoint today — every
                # CHANGED_TEST_FILE_RULES pattern is a `test_*.py` basename, so
                # a path that reaches this branch never matched one anyway — but
                # the ordering is what keeps the redirect contract above the
                # authority if that ever stops being true.
                if not is_test_suite:
                    for rule in SUPPORT_MODULE_TEST_RULES:
                        if fnmatch.fnmatch(path, rule.pattern):
                            selected.update(rule.tests)
                            # The meta-guard rider is not cargo: a routed
                            # support-module PR can invalidate the tree-derived
                            # meta-guards (including the closure guard that
                            # governs this very rule), and that suite exists to
                            # run on exactly the PR class that can.
                            selected.add(SELECTOR_META_GUARD_TEST)
                            matched_support_module = True
                if not matched_support_module:
                    # A `tests/` Python file that `is_test_suite_path` does not
                    # call a suite (conftest.py, integration_helpers.py, a
                    # fixtures/ builder) is not collectible: `pytest -q <it>`
                    # returns NO_TESTS_COLLECTED (exit 5), which ci.yml's
                    # `check=True` renders as a misleading red carrying zero
                    # assertion information (#1453). Such a path maps to the
                    # meta-guard suite instead, so every emitted target is a
                    # collectible test file; the meta-guard-only collapse then
                    # arms ci.yml's full-tree collect-only smoke (#1454) over the
                    # import surface such a support module can break.
                    selected.add(path if is_test_suite else SELECTOR_META_GUARD_TEST)
            # Unconditional, redirect or not: a redirect fires exactly when a
            # changed test file is swapped for focused nodes, which is also when
            # the meta-guards most need to run.
            if is_test_suite:
                selected.add(SELECTOR_META_GUARD_TEST)
            continue
        matched = False
        for rule in PATH_TEST_RULES:
            if fnmatch.fnmatch(path, rule.pattern):
                selected.update(rule.tests)
                matched = True
                if rule.stop_on_match:
                    break

        same_name_test = _same_name_script_test(path)
        if same_name_test is not None and _test_target_exists(same_name_test, repo_root=repo_root):
            selected.add(same_name_test)
            matched = True

        if (_is_backend_python_path(path) or _is_backend_shell_path(path)) and not matched:
            unknown_backend_path = True

    if unknown_backend_path:
        selected.update(CORE_SMOKE_TESTS)

    selected_paths = sorted(selected)
    # A selected target pointing at a deleted/renamed test file used to vanish
    # here in silence, so the selection could shrink (even to empty) with no
    # trace. Dropping stays the behavior; the drop is now announced. The target
    # can come from a rule OR from a changed test file that self-selects (a
    # routine deletion), so the wording stays provenance-neutral.
    missing = [path for path in selected_paths if not _test_target_exists(path, repo_root=repo_root)]
    # Several `::`-qualified node ids can pin the same missing file; announce
    # once per file. Return-list filtering below stays per-target.
    warned: set[str] = set()
    for path in missing:
        test_file = path.split("::", 1)[0]
        if test_file in warned:
            continue
        warned.add(test_file)
        message = f"selected test target does not exist and was dropped: {test_file}"
        print(f"select_ci_tests: WARNING: {message}", file=sys.stderr)
        # stdout carries the selected test list (consumed by `pytest -q $(...)`
        # command substitution locally), so the annotation is emitted only under
        # a real Actions runner, where ci.yml passes data via --github-output.
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning title=Dropped CI test target::{message}")
    dropped = set(missing)
    return [path for path in selected_paths if path not in dropped]


def changed_paths_from_git(base_ref: str) -> list[str]:
    subprocess.run(
        ["git", "fetch", "--no-tags", "origin", f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}"],
        check=True,
    )
    result = subprocess.run(
        ["git", "diff", "--name-only", f"origin/{base_ref}...HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def _is_backend_python_path(path: str) -> bool:
    return path.endswith(".py") and path.startswith(("apps/api/", "packages/", "services/", "workers/", "scripts/"))


def _is_backend_shell_path(path: str) -> bool:
    # scripts/**/*.sh is backend surface since #1138: the ci.yml `backend`
    # paths-filter matches it, so an unmapped wrapper must arm the core-smoke
    # fallback here instead of yielding an empty (collect-only) selection.
    # Deliberately scoped to scripts/: other .sh surfaces (infra/, frontend)
    # keep their own filters and have no pytest guard convention.
    return path.endswith(".sh") and path.startswith("scripts/")


def _same_name_script_test(path: str) -> str | None:
    """Derive the same-name test file for a changed ``scripts/**/*.py`` path.

    Scoped to ``scripts/`` on purpose: other backend prefixes keep their
    explicit-rule / core-smoke-fallback behavior even when a same-name test
    exists. Returns the candidate target only; the caller must confirm it
    exists before treating the path as a known mapping.
    """
    if not (path.startswith("scripts/") and path.endswith(".py")):
        return None
    return f"tests/test_{PurePosixPath(path).stem}.py"


def _any_path_matches(paths: Sequence[str], patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for path in paths for pattern in patterns)


def _test_target_exists(target: str, *, repo_root: Path) -> bool:
    test_path = target.split("::", 1)[0]
    return (repo_root / test_path).is_file()


def _write_github_output(tests: Sequence[str], *, output_path: Path) -> None:
    # `meta_guard_only` is a SHAPE property of the FINAL (post missing-target
    # filter) selection, not a claim about evidence provenance: it is true iff
    # the only target left is the selector's own suite. That covers the PR whose
    # single backend change deletes a `tests/test_*.py` (self-selection dropped
    # by the filter, meta-guard survives) and the #1453 support-module mapping —
    # both lost the full-tree import smoke they used to get — and it also fires
    # for selector-development PRs whose diff-specific target simply IS this
    # suite. That last class is accepted rather than special-cased: the cost is
    # one extra collection pass on exactly the PR class that changes the gate.
    meta_guard_only = list(tests) == [SELECTOR_META_GUARD_TEST]
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(f"count={len(tests)}\n")
        handle.write(f"tests={' '.join(tests)}\n")
        handle.write(f"tests_json={json.dumps(list(tests), separators=(',', ':'))}\n")
        handle.write(f"meta_guard_only={'true' if meta_guard_only else 'false'}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Select focused pytest files for CI from changed paths.")
    parser.add_argument("--base-ref", help="Base branch name used to compute changed paths.")
    parser.add_argument("--changed-file", type=Path, help="File containing one changed path per line.")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--github-output", type=Path, help="Write count/tests fields for GitHub Actions.")
    args = parser.parse_args(argv)

    if args.changed_file:
        changed = args.changed_file.read_text(encoding="utf-8").splitlines()
    elif args.base_ref:
        changed = changed_paths_from_git(args.base_ref)
    else:
        changed = sys.stdin.read().splitlines()

    tests = select_tests(changed, repo_root=args.repo_root)
    for test in tests:
        print(test)
    if args.github_output:
        _write_github_output(tests, output_path=args.github_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
