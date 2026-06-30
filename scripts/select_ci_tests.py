from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

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
    "tests/test_production_scheduler.py::test_scheduler_invokes_forcing_producer_before_orchestration_for_ready_canonical_candidate",
    "tests/test_production_scheduler.py::test_scheduler_propagates_produced_forcing_identity_to_orchestration",
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


CHANGED_TEST_FILE_RULES: tuple[PathTestRule, ...] = (
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
        ORCHESTRATOR_MANIFEST_SURFACE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS[3],
        ORCHESTRATOR_MANIFEST_SURFACE_TESTS,
    ),
    PathTestRule(
        DIRECT_GRID_SURFACE_PATH_PATTERNS[0],
        DIRECT_GRID_SURFACE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        DIRECT_GRID_SURFACE_PATH_PATTERNS[1],
        DIRECT_GRID_SURFACE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[0],
        FILE_JOURNAL_READ_STATE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[1],
        FILE_JOURNAL_READ_STATE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[2],
        FILE_JOURNAL_READ_STATE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[3],
        FILE_JOURNAL_READ_STATE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[4],
        FILE_JOURNAL_READ_STATE_TESTS,
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
        "workers/data_adapters/**",
        (
            "tests/test_gfs_adapter.py",
            "tests/test_ifs_adapter.py",
            "tests/test_era5_adapter.py",
            "tests/test_data_adapter_resolution.py",
            "tests/test_production_scheduler.py",
        ),
    ),
    PathTestRule(
        "workers/forcing_producer/**",
        (
            "tests/test_forcing_producer.py",
            "tests/test_production_met_validation.py",
            "tests/test_worker_chain_smoke.py",
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
        "workers/model_registry/**",
        (
            "tests/test_model_registration.py",
            "tests/test_model_registry_basin_versions.py",
            "tests/test_model_registry_list_basins.py",
        ),
    ),
    PathTestRule(
        "workers/output_parser/**",
        ("tests/test_output_parser.py",),
    ),
    PathTestRule(
        "services/orchestrator/**",
        (
            "tests/test_orchestrator.py",
            "tests/test_orchestration_chain.py",
            "tests/test_production_scheduler.py",
            "tests/test_scheduler_backfill.py",
            "tests/test_warm_start_chaining.py",
        ),
    ),
    PathTestRule(
        "services/slurm_gateway/**",
        (
            "tests/test_gateway.py",
            "tests/test_gateway_reconcile.py",
            "tests/test_slurm_gateway_app.py",
            "tests/test_slurm_route_contract.py",
        ),
    ),
    PathTestRule(
        "services/tile_publisher/**",
        (
            "tests/test_tile_publisher.py",
            "tests/test_forcing_copyback_backfill.py",
            "tests/test_static_serving.py",
        ),
    ),
    PathTestRule(
        "services/tiles/mvt.py",
        (
            "tests/test_api_contract.py",
            "tests/test_migrations.py",
            "tests/test_openapi_drift.py",
        ),
    ),
    PathTestRule(
        "services/production_closure/**",
        (
            "tests/test_production_readiness_validation.py",
            "tests/test_production_ops_validation.py",
            "tests/test_production_object_store_validation.py",
            "tests/test_production_slurm_validation.py",
            "tests/test_production_scale_validation.py",
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
        "scripts/governance/audit_repo_entropy.py",
        ("tests/test_entropy_audit_script.py",),
    ),
    PathTestRule(
        "scripts/governance/write_entropy_baseline.py",
        ("tests/test_entropy_audit_script.py",),
    ),
    PathTestRule(
        "scripts/select_ci_tests.py",
        ("tests/test_select_ci_tests.py",),
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
    unknown_backend_python = False

    for path in changed:
        if path.startswith("tests/") and path.endswith(".py"):
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
                selected.add(path)
            continue
        matched = False
        for rule in PATH_TEST_RULES:
            if fnmatch.fnmatch(path, rule.pattern):
                selected.update(rule.tests)
                matched = True
                if rule.stop_on_match:
                    break

        if _is_backend_python_path(path) and not matched:
            unknown_backend_python = True

    if unknown_backend_python:
        selected.update(CORE_SMOKE_TESTS)

    return sorted(path for path in selected if _test_target_exists(path, repo_root=repo_root))


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


def _any_path_matches(paths: Sequence[str], patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for path in paths for pattern in patterns)


def _test_target_exists(target: str, *, repo_root: Path) -> bool:
    test_path = target.split("::", 1)[0]
    return (repo_root / test_path).is_file()


def _write_github_output(tests: Sequence[str], *, output_path: Path) -> None:
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(f"count={len(tests)}\n")
        handle.write(f"tests={' '.join(tests)}\n")
        handle.write(f"tests_json={json.dumps(list(tests), separators=(',', ':'))}\n")


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
