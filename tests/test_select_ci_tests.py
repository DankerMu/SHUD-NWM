from __future__ import annotations

from pathlib import Path

from scripts.select_ci_tests import CORE_SMOKE_TESTS, main, select_tests


def test_select_tests_includes_changed_test_file(tmp_path: Path) -> None:
    test_path = tmp_path / "tests" / "test_example.py"
    test_path.parent.mkdir()
    test_path.write_text("def test_example(): pass\n", encoding="utf-8")

    assert select_tests(["tests/test_example.py"], repo_root=tmp_path) == ["tests/test_example.py"]


def test_select_tests_maps_adapter_changes_to_adapter_tests() -> None:
    selected = select_tests(
        ["workers/data_adapters/gfs_adapter.py", "workers/data_adapters/cycle_hours.py"],
        repo_root=Path("."),
    )

    assert "tests/test_gfs_adapter.py" in selected
    assert "tests/test_ifs_adapter.py" in selected
    assert "tests/test_data_adapter_resolution.py" in selected
    assert "tests/test_production_scheduler.py" in selected


def test_select_tests_maps_flood_cleanup_changes_to_cleanup_tests() -> None:
    selected = select_tests(["workers/flood_frequency/return_period_cleanup.py"], repo_root=Path("."))

    assert selected == ["tests/test_return_period_cleanup.py"]


def test_select_tests_maps_flood_cli_to_cleanup_and_frequency_tests() -> None:
    selected = select_tests(["workers/flood_frequency/cli.py"], repo_root=Path("."))

    assert selected == [
        "tests/test_flood_frequency.py",
        "tests/test_return_period.py",
        "tests/test_return_period_cleanup.py",
    ]


def test_select_tests_maps_compute_compose_to_two_node_runtime_tests() -> None:
    selected = select_tests(["infra/compose.compute.yml"], repo_root=Path("."))

    assert selected == ["tests/test_two_node_docker_runtime.py"]


def test_select_tests_maps_forecast_store_without_core_smoke_fallback() -> None:
    selected = select_tests(["packages/common/forecast_store.py"], repo_root=Path("."))
    fallback_only_tests = set(CORE_SMOKE_TESTS) - {"tests/test_migrations.py"}

    assert selected == [
        "tests/test_forecast_api.py",
        "tests/test_forecast_store_product_quality_sql.py",
        "tests/test_list_search_contract.py",
        "tests/test_migrations.py",
        "tests/test_model_registry_list_basins.py",
    ]
    assert not fallback_only_tests & set(selected)


def test_select_tests_maps_mvt_tiles_without_core_smoke_fallback() -> None:
    selected = select_tests(["services/tiles/mvt.py"], repo_root=Path("."))
    fallback_only_tests = set(CORE_SMOKE_TESTS) - {"tests/test_migrations.py"}

    assert selected == [
        "tests/test_flood_alerts_api.py",
        "tests/test_migrations.py",
        "tests/test_openapi_drift.py",
    ]
    assert not fallback_only_tests & set(selected)


def test_select_tests_maps_return_period_index_audit_without_core_smoke_fallback() -> None:
    selected = select_tests(["scripts/audit_return_period_indexes.py"], repo_root=Path("."))

    assert selected == [
        "tests/test_return_period_index_audit.py",
        "tests/test_select_ci_tests.py",
    ]
    assert not set(CORE_SMOKE_TESTS) & set(selected)


def test_select_tests_falls_back_to_core_smoke_for_unknown_backend_python_path() -> None:
    selected = select_tests(["services/new_surface/new_module.py"], repo_root=Path("."))

    for test_path in CORE_SMOKE_TESTS:
        assert test_path in selected


def test_select_tests_adds_core_smoke_for_unknown_backend_path_mixed_with_known_path() -> None:
    selected = select_tests(
        ["workers/data_adapters/gfs_adapter.py", "services/new_surface/new_module.py"],
        repo_root=Path("."),
    )

    assert "tests/test_gfs_adapter.py" in selected
    for test_path in CORE_SMOKE_TESTS:
        assert test_path in selected


def test_select_tests_ignores_docs_only_changes() -> None:
    assert select_tests(["docs/runbooks/current-production-ops.md"], repo_root=Path(".")) == []


def test_main_writes_json_github_output(tmp_path: Path) -> None:
    changed_file = tmp_path / "changed.txt"
    output_file = tmp_path / "github-output.txt"
    changed_file.write_text("infra/compose.compute.yml\n", encoding="utf-8")

    assert main(["--changed-file", str(changed_file), "--github-output", str(output_file)]) == 0

    output = output_file.read_text(encoding="utf-8")
    assert "count=1\n" in output
    assert 'tests_json=["tests/test_two_node_docker_runtime.py"]\n' in output
