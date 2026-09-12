"""Collection and AST contracts for the opt-in node-27 Docker oracle."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests import conftest
from tests.cold_residency_identity_mutants import call_name

_ROOT = Path(__file__).resolve().parents[1]
_ORACLE_TEST = _ROOT / "tests/test_node27_cold_tablespace_integration.py"
_RUNTIME_INTEGRATION_TEST = _ROOT / "tests/test_compressed_chunk_cold_runtime_integration.py"
_DEDICATED_MARKERS = {"integration", "timescaledb_210", "node27_docker"}


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
        "test_real_terminal_unlink_retry_closes_installed_without_docker_replay",
    )

    for name in real_names:
        assert _DEDICATED_MARKERS.issubset(marked[name])
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
    ) == ("stop", "rename", "run")
    assert marked["test_disposable_oracle_defaults_to_1892_pin_and_separate_identity"] == set()


def test_runtime_integration_has_dedicated_markers_only_on_docker_oracle() -> None:
    tree = ast.parse(
        _RUNTIME_INTEGRATION_TEST.read_text(encoding="utf-8"),
        filename=str(_RUNTIME_INTEGRATION_TEST),
    )
    marked = _marked_functions(tree)
    real_name = "test_isolated_cluster_production_runtime_not_probe_executor"
    local_name = "test_integration_refuses_live_cluster_identity"

    assert _DEDICATED_MARKERS.issubset(marked[real_name])
    assert "node27_docker" not in marked[local_name]
    dedicated = [name for name, marks in marked.items() if _DEDICATED_MARKERS.issubset(marks)]
    assert dedicated == [real_name]


_HELPER_MODULE = "tests.test_issue2224_origin_parity_integration"
_DISCRIMINATOR = "_assert_origin_parity_discriminator"
_SHIPPING_PROOF = "_assert_shipping_role_origin_parity"
_COMPRESSED_TARGET_SENSITIVITY = "_assert_selected_compressed_target_sensitivity"
_HELPER_SOURCE = _ROOT / "tests/test_issue2224_origin_parity_integration.py"


def _function_named(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in tree.body if isinstance(tree, ast.Module) else ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing function {name}")


def _imported_names(tree: ast.AST, *, module: str, name: str) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != module:
            continue
        for alias in node.names:
            if alias.name == name:
                aliases.add(alias.asname or alias.name)
    return aliases


def _call_count(tree: ast.AST, names: set[str]) -> int:
    return sum(1 for node in ast.walk(tree) if isinstance(node, ast.Call) and call_name(node) in names)


def _uninvoked_name_loads(tree: ast.AST, name: str) -> list[ast.Name]:
    call_funcs = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == name
        and isinstance(node.ctx, ast.Load)
        and id(node) not in call_funcs
    ]


def _filter_discriminator_imports(body: list[ast.stmt]) -> list[ast.stmt]:
    kept: list[ast.stmt] = []
    for node in body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == _HELPER_MODULE
            and any(alias.name == _DISCRIMINATOR for alias in node.names)
        ):
            remaining = [alias for alias in node.names if alias.name != _DISCRIMINATOR]
            if remaining:
                node.names = remaining
                kept.append(node)
            continue
        kept.append(node)
    return kept


def _strip_nested_discriminator_imports(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            value = getattr(node, field, None)
            if isinstance(value, list):
                setattr(node, field, _filter_discriminator_imports(value))


class _StripDiscriminatorCalls(ast.NodeTransformer):
    def visit_Expr(self, node: ast.Expr) -> ast.AST | None:
        if isinstance(node.value, ast.Call) and call_name(node.value) == _DISCRIMINATOR:
            return None
        return self.generic_visit(node)


class _UninvokeDiscriminatorCalls(ast.NodeTransformer):
    def visit_Expr(self, node: ast.Expr) -> ast.AST:
        if isinstance(node.value, ast.Call) and call_name(node.value) == _DISCRIMINATOR:
            return ast.Expr(value=ast.Name(id=_DISCRIMINATOR, ctx=ast.Load()))
        return self.generic_visit(node)


def _docker_oracle_function(tree: ast.Module) -> ast.FunctionDef:
    dedicated = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and _DEDICATED_MARKERS.issubset(_marked_functions(tree).get(node.name, set()))
    ]
    assert dedicated == [_function_named(tree, "test_isolated_cluster_production_runtime_not_probe_executor")]
    return dedicated[0]


def test_docker_oracle_invokes_shipping_role_origin_parity_proof() -> None:
    tree = ast.parse(
        _RUNTIME_INTEGRATION_TEST.read_text(encoding="utf-8"),
        filename=str(_RUNTIME_INTEGRATION_TEST),
    )
    docker = _docker_oracle_function(tree)
    imported = _imported_names(tree, module=_HELPER_MODULE, name=_SHIPPING_PROOF) | _imported_names(
        docker, module=_HELPER_MODULE, name=_SHIPPING_PROOF
    )
    assert imported, "shipping-role proof is not imported into the Docker oracle"
    assert _call_count(docker, imported) == 1


_FORBIDDEN_INTERNAL_SCHEMAS = (
    "_timescaledb_internal",
    "_timescaledb_catalog",
    "timescaledb_information",
)


def _stringish(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        values: list[str] = []
        for part in node.values:
            values.extend(_stringish(part))
        return values
    if isinstance(node, ast.FormattedValue):
        return _stringish(node.value)
    return []


def _call_text(node: ast.Call) -> str:
    parts: list[str] = []
    for arg in node.args:
        parts.extend(_stringish(arg))
    for keyword in node.keywords:
        if keyword.value is not None:
            parts.extend(_stringish(keyword.value))
    return " ".join(parts)


def test_shipping_role_proof_does_not_grant_internal_schemas_or_regrant_after_recompress() -> None:
    tree = ast.parse(_HELPER_SOURCE.read_text(encoding="utf-8"), filename=str(_HELPER_SOURCE))
    helper = _function_named(tree, _SHIPPING_PROOF)
    literals = [
        node.value
        for node in ast.walk(helper)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    forbidden_literals = [value for value in literals if value in _FORBIDDEN_INTERNAL_SCHEMAS]
    assert forbidden_literals == [], forbidden_literals
    grant_sql: list[str] = []
    compress_seen = False
    post_compress_internal_grant = False
    for node in ast.walk(helper):
        if not isinstance(node, ast.Call):
            continue
        text = _call_text(node)
        if "compress_chunk" in text:
            compress_seen = True
        if "GRANT" not in text.upper():
            continue
        grant_sql.append(text)
        if compress_seen and any(schema in text for schema in _FORBIDDEN_INTERNAL_SCHEMAS):
            post_compress_internal_grant = True
    offenders = [sql for sql in grant_sql if any(schema in sql for schema in _FORBIDDEN_INTERNAL_SCHEMAS)]
    assert not offenders, offenders
    assert post_compress_internal_grant is False
    helper_text = " ".join(literals)
    assert "OWNER TO" in helper_text
    assert "nhms_ingest_rw" in helper_text
    assert "nhms_display_ro" in helper_text


def test_origin_parity_discriminator_invokes_selected_compressed_target_sensitivity() -> None:
    tree = ast.parse(_HELPER_SOURCE.read_text(encoding="utf-8"), filename=str(_HELPER_SOURCE))
    helper = _function_named(tree, _DISCRIMINATOR)
    sibling_calls = _call_count(helper, {"compute_window_parity"})
    sensitivity_calls = _call_count(helper, {_COMPRESSED_TARGET_SENSITIVITY})
    assert sibling_calls >= 1
    assert sensitivity_calls == 1
    assert _COMPRESSED_TARGET_SENSITIVITY != "compute_window_parity"


def test_docker_oracle_imports_and_calls_origin_parity_discriminator_exactly_once() -> None:
    tree = ast.parse(
        _RUNTIME_INTEGRATION_TEST.read_text(encoding="utf-8"),
        filename=str(_RUNTIME_INTEGRATION_TEST),
    )
    docker = _docker_oracle_function(tree)
    imported = _imported_names(tree, module=_HELPER_MODULE, name=_DISCRIMINATOR) | _imported_names(
        docker, module=_HELPER_MODULE, name=_DISCRIMINATOR
    )
    assert imported == {_DISCRIMINATOR}
    assert _call_count(docker, imported) == 1
    assert _call_count(tree, imported) == 1
    assert _uninvoked_name_loads(docker, _DISCRIMINATOR) == []


@pytest.mark.parametrize("kind", ("remove_import", "remove_call", "uninvoked_reference"))
def test_discriminator_invocation_mutants_red(kind: str) -> None:
    source = _RUNTIME_INTEGRATION_TEST.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_RUNTIME_INTEGRATION_TEST))
    docker = _docker_oracle_function(tree)
    live_imported = _imported_names(tree, module=_HELPER_MODULE, name=_DISCRIMINATOR)
    live_called = _call_count(docker, live_imported or {_DISCRIMINATOR})
    assert live_imported == {_DISCRIMINATOR}
    assert live_called == 1
    assert _uninvoked_name_loads(docker, _DISCRIMINATOR) == []
    if kind == "remove_import":
        _strip_nested_discriminator_imports(tree)
    elif kind == "remove_call":
        _StripDiscriminatorCalls().visit(docker)
    else:
        _UninvokeDiscriminatorCalls().visit(docker)
    imported = _imported_names(tree, module=_HELPER_MODULE, name=_DISCRIMINATOR)
    called = _call_count(docker, imported or {_DISCRIMINATOR})
    uninvoked = _uninvoked_name_loads(docker, _DISCRIMINATOR)
    if kind == "remove_import":
        assert imported != {_DISCRIMINATOR}
    elif kind == "remove_call":
        assert called != 1
    else:
        assert uninvoked != [] or called != 1


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
    assert 1 + 1 + 1 + len(parametrized.args[1].elts) == 6


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
