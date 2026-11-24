"""Every name the chain facade binds by import has a reader (#3335 item 6).

``services/orchestrator/chain.py`` is exempt from ``F401`` file-wide, so ruff
does not report an import that nothing reads.  This test does: it collects
every name bound by an ``import`` / ``from ... import`` statement in the
facade's AST (the alias when there is one; ``from __future__`` skipped) and
requires each to be one of:

* loaded somewhere in ``chain.py``'s own AST;
* a read of the chain facade reported by the facade name-resolution scan
  (``scan_tree()`` in ``tests/test_facade_name_resolution.py``: owner and test
  sources, attribute, ``from``-import, ``getattr`` and string-target forms);
* in ``NAME_LIST_READS``: a name that a test reads only through a list of
  names passed to ``getattr`` on the facade, with the test that holds the list;
* in ``MODULE_LOADING_IMPORTS``: an import kept only because other code
  depends on the module being loaded, with that consumer.

The facade is parsed by path and never imported or read here, so this file
does not count as a reader of any facade name.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

import pytest

from tests.test_facade_name_resolution import scan_tree

REPO_ROOT = Path(__file__).resolve().parents[1]
CHAIN_SOURCE = "services/orchestrator/chain.py"
FACADE = "chain"

# (name, test file, test function): names that tests read only through a list
# of names passed to ``getattr`` on the facade.  The test holding the list must
# still carry the name as a string literal.
NAME_LIST_READS: tuple[tuple[str, str, str], ...] = (
    (
        "cycle_id_for",
        "tests/test_orchestration_chain.py",
        "test_chain_worker_adapter_compat_facade_matches_owner_modules",
    ),
    (
        "evaluate_canonical_readiness",
        "tests/test_orchestration_chain.py",
        "test_chain_worker_adapter_compat_facade_matches_owner_modules",
    ),
    (
        "expected_converter_version",
        "tests/test_orchestration_chain.py",
        "test_chain_worker_adapter_compat_facade_matches_owner_modules",
    ),
    (
        "parse_cycle_time",
        "tests/test_orchestration_chain.py",
        "test_chain_worker_adapter_compat_facade_matches_owner_modules",
    ),
)

# Imports kept only because other code depends on the module being loaded:
# bound name -> the consumer.  Empty: no such import.
MODULE_LOADING_IMPORTS: dict[str, str] = {}


@lru_cache(maxsize=None)
def _chain_tree() -> ast.Module:
    path = REPO_ROOT / CHAIN_SOURCE
    return ast.parse(path.read_text(encoding="utf-8"), filename=CHAIN_SOURCE)


def import_bound_names(tree: ast.Module) -> dict[str, int]:
    """Name bound by each import statement (alias first) -> line of the statement."""

    bound: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound.setdefault(alias.asname or alias.name.partition(".")[0], node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            for alias in node.names:
                if alias.name != "*":
                    bound.setdefault(alias.asname or alias.name, node.lineno)
    return bound


def loaded_names(tree: ast.Module) -> set[str]:
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}


def facade_reads() -> set[str]:
    return {read.name for read in scan_tree().reads if read.facade == FACADE}


def _listed() -> list[str]:
    return [name for name, _, _ in NAME_LIST_READS] + list(MODULE_LOADING_IMPORTS)


def test_every_chain_import_has_a_reader() -> None:
    tree = _chain_tree()
    bound = import_bound_names(tree)
    covered = loaded_names(tree) | facade_reads() | set(_listed())
    unread = sorted((line, name) for name, line in bound.items() if name not in covered)
    if unread:
        lines = [f"{len(unread)} name(s) bound by an import in {CHAIN_SOURCE} have no reader:"]
        lines.extend(f"  line {line}: {name}" for line, name in unread)
        lines.append(
            "Delete the import. Keep it only if a test reads it through a name list (NAME_LIST_READS) "
            "or other code needs the module loaded (MODULE_LOADING_IMPORTS, with a comment naming the consumer)."
        )
        pytest.fail("\n".join(lines), pytrace=False)


def test_reader_lists_have_no_stale_entries() -> None:
    tree = _chain_tree()
    bound = import_bound_names(tree)
    loaded = loaded_names(tree)
    reads = facade_reads()
    listed = _listed()
    problems = []
    for name in listed:
        if name not in bound:
            problems.append(f"{name}: not bound by an import in {CHAIN_SOURCE}")
        if name in loaded:
            problems.append(f"{name}: loaded in {CHAIN_SOURCE} itself")
        if name in reads:
            problems.append(f"{name}: read through the facade (scan_tree)")
    duplicates = sorted({name for name in listed if listed.count(name) > 1})
    problems.extend(f"{name}: listed more than once" for name in duplicates)
    assert not problems, "stale reader-list entries:\n" + "\n".join(problems)


def _function_def(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def test_name_list_entries_are_still_held_by_their_test() -> None:
    problems = []
    for name, test_path, test_name in NAME_LIST_READS:
        tree = ast.parse((REPO_ROOT / test_path).read_text(encoding="utf-8"), filename=test_path)
        function = _function_def(tree, test_name)
        if function is None:
            problems.append(f"{name}: {test_path}::{test_name} not found")
            continue
        literals = {
            node.value for node in ast.walk(function) if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        if name not in literals:
            problems.append(f"{name}: {test_path}::{test_name} no longer holds it as a string literal")
    assert not problems, "name-list entries without their reader:\n" + "\n".join(problems)


def test_module_loading_imports_name_a_consumer() -> None:
    assert all(consumer.strip() for consumer in MODULE_LOADING_IMPORTS.values())
