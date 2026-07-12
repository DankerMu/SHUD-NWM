"""Wire-site invariant tests for the compressed-chunk write guard.

Mirrors #850 cand-E / #851 R3: a shape-first invariant that greps for every
production ``DELETE FROM {schema}.{table}`` targeting a guarded hypertable
and asserts the surrounding module ALSO calls
``check_batch_targets_uncompressed``. This closes the ordering-contract gap
that per-path unit tests alone cannot cover: a future contributor who adds
a fourth write path but forgets the guard call would slip past the wired
tests (which only cover the three known paths) — this AST-scan catches the
new site by structure, not by manual audit.

The scan is *derived from* :data:`packages.common.timescale_write_guard.HYPERTABLES_GUARDED`
so adding a new guarded hypertable + wiring it up does not require editing
this test; only removing a wired guard call OR adding an unwired DELETE
site causes a failure.

Scan surface:

* ``workers/**/*.py``
* ``packages/common/**/*.py``

Whitelisted non-wiring modules (documented as intentionally unwired):

* ``packages/common/timescale_write_guard.py`` — the guard module itself.
* ``db/seeds/seed_demo.py`` — seeds fresh empty demo DB; never targets
  compressed chunks in production. Docstring documents the exemption.
* ``scripts/reset_qhh_smoke_db.py`` — smoke-DB reset script; analogous
  non-wiring rationale to seed_demo (fresh DB reset, never touches
  production compressed data).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from packages.common.timescale_write_guard import HYPERTABLES_GUARDED

REPO_ROOT = Path(__file__).resolve().parents[1]

_GUARD_MODULE_PATH = REPO_ROOT / "packages" / "common" / "timescale_write_guard.py"

# Modules documented as intentionally unwired. Any addition MUST come with a
# module-docstring justification (see seed_demo.py:1-21 as the template).
_INTENTIONALLY_UNWIRED_MODULES: frozenset[Path] = frozenset(
    {
        _GUARD_MODULE_PATH,
        REPO_ROOT / "db" / "seeds" / "seed_demo.py",
        REPO_ROOT / "scripts" / "reset_qhh_smoke_db.py",
    }
)

_EXCLUDED_DIR_PARTS: frozenset[str] = frozenset(
    {"__pycache__", "tests", ".venv", "node_modules"}
)


def _scan_roots() -> tuple[Path, ...]:
    """The scoped source tree the scan walks.

    workers/** is where SHUD ingest workers live; packages/common/** hosts
    the shared apply site. Display API (``apps/api``), frontend
    (``apps/frontend``), and unrelated packages are OUT of scope for the
    guard by ADR 0001.
    """
    return (
        REPO_ROOT / "workers",
        REPO_ROOT / "packages" / "common",
    )


def _iter_python_sources() -> list[Path]:
    files: list[Path] = []
    for root in _scan_roots():
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part in _EXCLUDED_DIR_PARTS for part in path.parts):
                continue
            if path in _INTENTIONALLY_UNWIRED_MODULES:
                continue
            files.append(path)
    return files


def _module_contains_delete_from(tree: ast.AST, schema: str, table: str) -> bool:
    """Return True if ``tree`` contains any literal ``DELETE FROM {schema}.{table}``.

    Scans ALL string literals in the AST (``ast.Constant`` with str value,
    and f-string components in ``ast.JoinedStr``) since production DELETEs
    are literal strings passed to ``cursor.execute`` / helper wrappers.
    """
    needle = f"DELETE FROM {schema}.{table}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if needle in node.value:
                return True
        elif isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    if needle in value.value:
                        return True
    return False


def _module_calls_guard(tree: ast.AST) -> bool:
    """Return True if the module contains any ``check_batch_targets_uncompressed`` call.

    Matches either an unqualified call (``check_batch_targets_uncompressed(...)``)
    or an attribute-access call (``mod.check_batch_targets_uncompressed(...)``);
    an ``import ... as _guard`` renaming still binds the resulting name to the
    guard function, so we also scan for any :class:`ast.Name` whose id starts
    with the original identifier — but the primary detector is the
    ImportFrom / attribute-call pattern used at all three known wire sites.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "check_batch_targets_uncompressed":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "check_batch_targets_uncompressed":
                return True
    return False


def _module_imports_guard(tree: ast.AST) -> bool:
    """Belt-and-suspenders: the module MUST also import from the guard module.

    Any wire site that calls ``check_batch_targets_uncompressed`` should
    import it from ``packages.common.timescale_write_guard`` — a bare call
    without the import would fail at Python parse-time, but explicitly
    asserting the import shape catches "wired the call but stubbed the
    import" bugs.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "packages.common.timescale_write_guard":
                for alias in node.names:
                    if alias.name == "check_batch_targets_uncompressed":
                        return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "packages.common.timescale_write_guard":
                    return True
    return False


def _wire_site_hits() -> dict[tuple[str, str], set[Path]]:
    """For each guarded pair, return the modules that DELETE from it."""
    hits: dict[tuple[str, str], set[Path]] = {pair: set() for pair in HYPERTABLES_GUARDED}
    for path in _iter_python_sources():
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for schema, table in HYPERTABLES_GUARDED:
            if _module_contains_delete_from(tree, schema, table):
                hits[(schema, table)].add(path)
    return hits


# ---------------------------------------------------------------------------
# C1 — Every guarded hypertable's DELETE site co-locates a guard call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hypertable",
    sorted(HYPERTABLES_GUARDED),
    ids=lambda pair: f"{pair[0]}.{pair[1]}",
)
def test_every_guarded_hypertable_has_a_guarded_delete_site(
    hypertable: tuple[str, str],
) -> None:
    """For each ``(schema, table)`` in HYPERTABLES_GUARDED, every production
    module that issues ``DELETE FROM {schema}.{table}`` MUST also call
    ``check_batch_targets_uncompressed`` and import it from the shared
    helper module.
    """
    schema, table = hypertable
    hits = _wire_site_hits()[hypertable]
    assert hits, (
        f"No production module was found DELETing from {schema}.{table}; "
        "either the wire test is stale or the write path was removed. "
        "Update HYPERTABLES_GUARDED / this test in sync."
    )
    for module_path in sorted(hits):
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        assert _module_calls_guard(tree), (
            f"{module_path.relative_to(REPO_ROOT)} issues "
            f"DELETE FROM {schema}.{table} but does NOT call "
            "check_batch_targets_uncompressed. Wire the guard before the DELETE."
        )
        assert _module_imports_guard(tree), (
            f"{module_path.relative_to(REPO_ROOT)} calls the guard but does "
            "not import it from packages.common.timescale_write_guard. "
            "Divergent per-path implementations are forbidden (design D5)."
        )


# ---------------------------------------------------------------------------
# C1 — No off-manifest write site DELETEs a guarded hypertable
# ---------------------------------------------------------------------------


_EXPECTED_WIRE_MODULES: frozenset[Path] = frozenset(
    {
        REPO_ROOT / "workers" / "output_parser" / "parser.py",
        REPO_ROOT / "workers" / "forcing_producer" / "store.py",
        REPO_ROOT / "packages" / "common" / "forcing_domain_handoff_apply.py",
    }
)


def test_no_off_manifest_write_site_deletes_guarded_hypertable() -> None:
    """The union of wire hits across ALL guarded pairs MUST equal the three
    expected modules. A fourth hit is a hard fail — either a new wire site
    slipped in unaudited, or an old module regressed to DELETE a guarded
    hypertable. Either way the invariant fails until reviewed.
    """
    all_hits: set[Path] = set()
    for hits in _wire_site_hits().values():
        all_hits.update(hits)
    unexpected = all_hits - _EXPECTED_WIRE_MODULES
    assert not unexpected, (
        "Unexpected module(s) DELETE from a guarded hypertable — audit the "
        "new wire site and either add it here or wire the guard:\n"
        + "\n".join(sorted(str(path.relative_to(REPO_ROOT)) for path in unexpected))
    )
    missing = _EXPECTED_WIRE_MODULES - all_hits
    assert not missing, (
        "Expected wire site(s) no longer DELETE a guarded hypertable — "
        "verify the write path was intentionally removed and update this "
        "invariant:\n"
        + "\n".join(sorted(str(path.relative_to(REPO_ROOT)) for path in missing))
    )


def test_intentionally_unwired_modules_are_documented() -> None:
    """Sanity check: every module in ``_INTENTIONALLY_UNWIRED_MODULES`` MUST
    exist on disk (so a rename doesn't silently drop it from the whitelist)
    and MUST NOT accidentally call the guard (which would signal that the
    "intentional" label is stale).
    """
    for path in _INTENTIONALLY_UNWIRED_MODULES:
        assert path.exists(), f"Whitelisted non-wiring module missing: {path}"
        if path == _GUARD_MODULE_PATH:
            continue  # The guard module defines the function.
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert not _module_calls_guard(tree), (
            f"{path.relative_to(REPO_ROOT)} is on the intentionally-unwired "
            "whitelist but now calls the guard. Either remove it from the "
            "whitelist or drop the guard call."
        )
