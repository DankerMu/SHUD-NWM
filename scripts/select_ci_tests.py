from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

CORE_SMOKE_TESTS: tuple[str, ...] = (
    "tests/test_api.py",
    "tests/test_gateway.py",
    "tests/test_migrations.py",
    "tests/test_orchestration_chain.py",
    "tests/test_production_scheduler.py",
)

PATH_TEST_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "workers/data_adapters/**",
        (
            "tests/test_gfs_adapter.py",
            "tests/test_ifs_adapter.py",
            "tests/test_era5_adapter.py",
            "tests/test_data_adapter_resolution.py",
            "tests/test_production_scheduler.py",
        ),
    ),
    (
        "workers/forcing_producer/**",
        (
            "tests/test_forcing_producer.py",
            "tests/test_production_met_validation.py",
            "tests/test_worker_chain_smoke.py",
        ),
    ),
    (
        "workers/shud_runtime/**",
        (
            "tests/test_shud_runtime.py",
            "tests/test_runtime_mode.py",
            "tests/test_runtime_ic_header.py",
        ),
    ),
    (
        "workers/model_registry/**",
        (
            "tests/test_model_registration.py",
            "tests/test_model_registry_basin_versions.py",
            "tests/test_model_registry_list_basins.py",
        ),
    ),
    ("workers/output_parser/**", ("tests/test_output_parser.py",)),
    (
        "workers/flood_frequency/return_period_cleanup.py",
        ("tests/test_return_period_cleanup.py",),
    ),
    (
        "workers/flood_frequency/return_period.py",
        ("tests/test_return_period.py",),
    ),
    (
        "workers/flood_frequency/cli.py",
        ("tests/test_flood_frequency.py", "tests/test_return_period.py", "tests/test_return_period_cleanup.py"),
    ),
    (
        "workers/flood_frequency/frequency.py",
        ("tests/test_flood_frequency.py",),
    ),
    (
        "workers/flood_frequency/hindcast.py",
        ("tests/test_flood_frequency.py",),
    ),
    (
        "workers/flood_frequency/config.py",
        ("tests/test_flood_frequency.py",),
    ),
    (
        "services/orchestrator/**",
        (
            "tests/test_orchestrator.py",
            "tests/test_orchestration_chain.py",
            "tests/test_production_scheduler.py",
            "tests/test_scheduler_backfill.py",
            "tests/test_warm_start_chaining.py",
        ),
    ),
    (
        "services/slurm_gateway/**",
        (
            "tests/test_gateway.py",
            "tests/test_gateway_reconcile.py",
            "tests/test_slurm_gateway_app.py",
            "tests/test_slurm_route_contract.py",
        ),
    ),
    (
        "services/tile_publisher/**",
        (
            "tests/test_tile_publisher.py",
            "tests/test_forcing_copyback_backfill.py",
            "tests/test_static_serving.py",
        ),
    ),
    (
        "services/tiles/mvt.py",
        (
            "tests/test_flood_alerts_api.py",
            "tests/test_migrations.py",
            "tests/test_openapi_drift.py",
        ),
    ),
    (
        "services/production_closure/**",
        (
            "tests/test_production_readiness_validation.py",
            "tests/test_production_ops_validation.py",
            "tests/test_production_object_store_validation.py",
            "tests/test_production_slurm_validation.py",
            "tests/test_production_scale_validation.py",
        ),
    ),
    ("packages/common/object_store.py", ("tests/test_object_store_roots.py", "tests/test_storage.py")),
    (
        "packages/common/forecast_store.py",
        (
            "tests/test_forecast_api.py",
            "tests/test_forecast_store_product_quality_sql.py",
            "tests/test_list_search_contract.py",
            "tests/test_migrations.py",
            "tests/test_model_registry_list_basins.py",
        ),
    ),
    ("packages/common/state_manager.py", ("tests/test_state_manager.py", "tests/test_state_qc.py")),
    ("packages/common/state_cli.py", ("tests/test_state_manager.py", "tests/test_state_qc.py")),
    ("packages/common/redaction.py", ("tests/test_redaction.py",)),
    ("apps/api/**", ("tests/test_api.py", "tests/test_api_contract.py", "tests/test_monitoring_api.py")),
    ("db/**", ("tests/test_migrations.py",)),
    ("infra/compose.compute.yml", ("tests/test_two_node_docker_runtime.py",)),
    ("infra/compose.display.yml", ("tests/test_two_node_docker_runtime.py",)),
    ("infra/env/**", ("tests/test_two_node_docker_runtime.py",)),
    (
        "scripts/validate_two_node_docker_runtime.py",
        ("tests/test_two_node_docker_runtime.py",),
    ),
    (
        "scripts/validate_two_node_docker_source_trust.py",
        ("tests/test_two_node_docker_source_trust.py",),
    ),
    (
        "scripts/audit_return_period_indexes.py",
        ("tests/test_return_period_index_audit.py", "tests/test_select_ci_tests.py"),
    ),
    ("scripts/validate_readonly_db_boundary.py", ("tests/test_readonly_db_validation.py",)),
    ("scripts/run_qhh_continuous.py", ("tests/test_run_qhh_continuous.py",)),
    ("scripts/select_ci_tests.py", ("tests/test_select_ci_tests.py",)),
    ("pyproject.toml", CORE_SMOKE_TESTS),
    ("uv.lock", CORE_SMOKE_TESTS),
)


def select_tests(changed_paths: Iterable[str], *, repo_root: Path = Path(".")) -> list[str]:
    selected: set[str] = set()
    changed = [path.strip().replace("\\", "/") for path in changed_paths if path.strip()]
    unknown_backend_python = False

    for path in changed:
        if path.startswith("tests/") and path.endswith(".py"):
            selected.add(path)
            continue
        matched = False
        for pattern, tests in PATH_TEST_RULES:
            if fnmatch.fnmatch(path, pattern):
                selected.update(tests)
                matched = True

        if _is_backend_python_path(path) and not matched:
            unknown_backend_python = True

    if unknown_backend_python:
        selected.update(CORE_SMOKE_TESTS)

    return sorted(path for path in selected if (repo_root / path).is_file())


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
