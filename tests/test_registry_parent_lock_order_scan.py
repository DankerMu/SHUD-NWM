"""Static pin: registry imports take the basin_version row lock first (#2491).

``import_basin_into_registry_core`` is the single write sequence both the
generic import and ``bootstrap-qhh-production`` delegate to. Its first
statement must be ``_lock_basin_version(cursor, sources.ids["basin_version_id"])``
so the parent-lock order is ``basin_version -> river_network_version`` for
every caller; the real-DB interleaving lives in
``tests/test_registry_parent_lock_order_integration.py``. Pure AST reads of the
source, no imports of the modules under scan.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_IMPORT_MODULE = _ROOT / "workers" / "model_registry" / "basins_registry_import.py"
_BOOTSTRAP_MODULE = _ROOT / "workers" / "model_registry" / "qhh_production_bootstrap.py"


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(matches) == 1, f"{path.name}: expected exactly one def {name}, found {len(matches)}"
    return matches[0]


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _calls_in_source_order(function: ast.FunctionDef) -> list[tuple[int, int, str]]:
    calls = [
        (node.lineno, node.col_offset, name)
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and (name := _called_name(node)) is not None
    ]
    return sorted(calls)


def _body_without_docstring(function: ast.FunctionDef) -> list[ast.stmt]:
    body = list(function.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return body


def test_basin_version_lock_is_the_first_statement_of_the_import_core() -> None:
    function = _function(_IMPORT_MODULE, "import_basin_into_registry_core")
    first = _body_without_docstring(function)[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call), ast.unparse(first)
    assert ast.unparse(first.value) == "_lock_basin_version(cursor, sources.ids['basin_version_id'])", (
        f"first statement of import_basin_into_registry_core is {ast.unparse(first)!r}, not the basin_version row lock"
    )


def test_basin_version_lock_precedes_every_write_helper() -> None:
    function = _function(_IMPORT_MODULE, "import_basin_into_registry_core")
    calls = _calls_in_source_order(function)
    names = [name for _line, _col, name in calls]
    assert names.count("_lock_basin_version") == 1, names
    lock_index = names.index("_lock_basin_version")
    writers = [
        name
        for name in names
        if name.startswith("_ensure_")
        or name
        in {
            "_delete_legacy_seg_rows",
            "_refresh_parent_version_materialization",
            "_backfill_output_segment_geometry",
        }
    ]
    # Vacuity guard: the scan must see the write helpers it orders against.
    assert {"_delete_legacy_seg_rows", "_refresh_parent_version_materialization", "_ensure_basin_version"} <= set(
        writers
    ), names
    before_lock = [name for name in names[:lock_index] if name in writers]
    assert not before_lock, f"write helpers run before _lock_basin_version: {before_lock}"


def test_basin_version_lock_statement_is_for_no_key_update_on_basin_version() -> None:
    function = _function(_IMPORT_MODULE, "_lock_basin_version")
    statements = [
        " ".join(node.value.split())
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "SELECT" in node.value
    ]
    assert statements == ["SELECT 1 FROM core.basin_version WHERE basin_version_id = %s FOR NO KEY UPDATE"], statements


def test_bootstrap_takes_its_basin_scope_lock_before_delegating_to_the_import_core() -> None:
    function = _function(_BOOTSTRAP_MODULE, "_bootstrap_database")
    names = [name for _line, _col, name in _calls_in_source_order(function)]
    assert "_lock_qhh_basin_scope" in names and "import_basin_into_registry_core" in names, names
    assert names.index("_lock_qhh_basin_scope") < names.index("import_basin_into_registry_core"), names
