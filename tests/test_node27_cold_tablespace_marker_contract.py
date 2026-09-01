"""Collection and AST contracts for the opt-in node-27 Docker oracle."""

from __future__ import annotations

import ast
from pathlib import Path

from tests import conftest

_ROOT = Path(__file__).resolve().parents[1]
_ORACLE_TEST = _ROOT / "tests/test_node27_cold_tablespace_integration.py"


def _marked_functions(tree: ast.Module) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        marks: set[str] = set()
        for decorator in node.decorator_list:
            value = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Attribute)
                and isinstance(value.value.value, ast.Name)
                and value.value.value.id == "pytest"
                and value.value.attr == "mark"
            ):
                marks.add(value.attr)
        result[node.name] = marks
    return result


def test_real_oracle_has_all_three_opt_in_markers_but_local_identity_tests_remain_unmarked() -> None:
    tree = ast.parse(_ORACLE_TEST.read_text(encoding="utf-8"), filename=str(_ORACLE_TEST))
    marked = _marked_functions(tree)
    real_names = (
        "test_real_disposable_cluster_installs_through_run_install",
        "test_real_post_recreate_failure_rolls_back_only_owned_state",
        "test_real_interrupted_replacement_recovers_without_install_replay",
    )

    for name in real_names:
        assert {"integration", "timescaledb_210", "node27_docker"}.issubset(marked[name])
    interrupted_name = "test_real_interrupted_replacement_recovers_without_install_replay"
    interrupted = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == interrupted_name
    )
    parametrized = next(
        decorator
        for decorator in interrupted.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "parametrize"
    )
    assert isinstance(parametrized.args[1], ast.Tuple)
    assert tuple(
        item.value for item in parametrized.args[1].elts if isinstance(item, ast.Constant)
    ) == ("prior_stopped", "prior_renamed", "replacement_created")
    assert marked["test_disposable_oracle_defaults_to_1892_pin_and_separate_identity"] == set()


def test_real_oracle_collects_five_opt_in_nodes() -> None:
    tree = ast.parse(_ORACLE_TEST.read_text(encoding="utf-8"), filename=str(_ORACLE_TEST))
    interrupted = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_real_interrupted_replacement_recovers_without_install_replay"
    )
    parametrized = next(
        decorator
        for decorator in interrupted.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "parametrize"
    )
    assert isinstance(parametrized.args[1], ast.Tuple)
    assert len(parametrized.args[1].elts) == 3
    assert 1 + 1 + len(parametrized.args[1].elts) == 5


def test_real_oracle_imports_the_public_state_machine_and_forbids_legacy_bypass_symbols() -> None:
    tree = ast.parse(_ORACLE_TEST.read_text(encoding="utf-8"), filename=str(_ORACLE_TEST))
    source = _ORACLE_TEST.read_text(encoding="utf-8")
    imported_run_install = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "packages.common.node27_cold_tablespace_install"
        and any(alias.name == "run_install" for alias in node.names)
        for node in tree.body
    )

    assert imported_run_install
    assert "wait_ready" not in source
    forbidden = (
        "recreate_with_cold_bind",
        "bootstrap_timescale_oracle",
        "CREATE TABLESPACE",
        "docker_run_argv",
    )
    assert not any(token in source for token in forbidden)


def test_node27_docker_collection_gate_is_dedicated_and_does_not_change_other_integration_semantics(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NHMS_RUN_NODE27_DOCKER", "1")
    monkeypatch.delenv("NHMS_RUN_INTEGRATION", raising=False)
    monkeypatch.delenv("NHMS_INTEGRATION_DATABASE_URL", raising=False)
    monkeypatch.delenv("NHMS_ALLOW_DATABASE_URL_INTEGRATION", raising=False)

    assert conftest._node27_docker_skip_reason() is None
    assert conftest._integration_skip_reason() is not None
    assert conftest._is_node27_docker_keywords({"integration", "timescaledb_210", "node27_docker"}) is True
    assert conftest._is_node27_docker_keywords({"integration", "node27_docker"}) is False
    assert conftest._is_node27_docker_keywords({"integration", "timescaledb_210"}) is False

    monkeypatch.delenv("NHMS_RUN_NODE27_DOCKER", raising=False)
    assert "NHMS_RUN_NODE27_DOCKER=1" in (conftest._node27_docker_skip_reason() or "")
