"""The river-segment write surface is exactly one in-place UPDATE (#2031).

`fix-national-digest-cache-identity-2031`'s governing invariant is that the
national tile cache identity rotates whenever the geometry the tile paints is
rewritten in place. It is enforced by ONE counter bump, inside ONE function:
`workers/model_registry/basins_registry_import.py::_backfill_output_segment_geometry`
issues the only `UPDATE core.river_segment` in production code and bumps
`core.river_network_version.geometry_generation` in the same transaction.

That invariant is only as good as the "only" in it. A second in-place rewrite
added anywhere under `apps/`, `services/`, `workers/`, `packages/` or `scripts/`
would move geometry with no rotation, and nothing else in the repo would notice
-- the digest tests all run against the one function that does bump. So the
boundary is pinned by scanning the source.

Two shapes are pinned, because an in-place rewrite has two spellings:

* a bare `UPDATE core.river_segment`, and
* an `INSERT ... ON CONFLICT ... DO UPDATE` on the table, of which exactly one
  exists (`qhh_production_bootstrap.py::_seed_output_segment_rows`). That upsert
  is deliberately OUTSIDE the invariant (design.md Non-goals): it rewrites
  `properties_json` -- and therefore the STORED `stream_type` -- but never
  `geom`, and it is covered today only by the trailing
  `_backfill_output_segment_geometry(..., only_missing=False)` both of its call
  sites run on the same cursor. Its `DO UPDATE SET` column list is therefore
  pinned COLUMN BY COLUMN: adding `geom = EXCLUDED.geom` to it would create a
  geometry rewrite with no rotation that the `UPDATE`-only pin above cannot see.

Row-level INSERT/DELETE of river segments is outside the invariant by design
(Non-goals) and is not scanned here.

Scanning is done over the AST's string literals rather than over raw file text:
a text regex for `INSERT ... ON CONFLICT` spans from one statement to an
unrelated one several hundred lines later (`basins_registry_import.py`'s plain
segment INSERT at :662 to the crosswalk upsert at :972), and comments or
docstrings that merely DESCRIBE a statement would count as one. Every failure
message names the offending file and line.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DIRS = ("apps", "services", "workers", "packages", "scripts")

BACKFILL_MODULE = "workers/model_registry/basins_registry_import.py"
BACKFILL_FUNCTION = "_backfill_output_segment_geometry"
UPSERT_MODULE = "workers/model_registry/qhh_production_bootstrap.py"
UPSERT_FUNCTION = "_seed_output_segment_rows"

# `\b` after `river_segment` so `core.river_segment_crosswalk` -- a different
# table with its own upsert -- is not counted as this one.
_UPDATE_RIVER_SEGMENT = re.compile(r"\bUPDATE\s+core\.river_segment\b", re.IGNORECASE)
_INSERT_RIVER_SEGMENT = re.compile(r"\bINSERT\s+INTO\s+core\.river_segment\b", re.IGNORECASE)
_ON_CONFLICT_DO_UPDATE = re.compile(r"\bON\s+CONFLICT\b[\s\S]*?\bDO\s+UPDATE\b", re.IGNORECASE)
_GENERATION_BUMP = re.compile(
    r"\bSET\s+geometry_generation\s*=\s*geometry_generation\s*\+\s*1\b", re.IGNORECASE
)


@dataclass(frozen=True)
class _SqlLiteral:
    path: str
    lineno: int
    function: str
    text: str

    def where(self) -> str:
        return f"{self.path}:{self.lineno} (in {self.function})"


def _collect(node: ast.AST, path: str, function: str, into: list[_SqlLiteral]) -> None:
    """Recursive descent that carries the INNERMOST enclosing function name down.

    `ast.walk` from each FunctionDef would attribute a nested function's literals
    to both, and re-walking the module for the top level would double-count every
    literal already seen. One pass, one owner per literal.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _collect(child, path, child.name, into)
            continue
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            into.append(_SqlLiteral(path, child.lineno, function, child.value))
        _collect(child, path, function, into)


def _sql_literals() -> list[_SqlLiteral]:
    """Every string literal in production code, tagged with its enclosing function.

    Module- and class-level literals (a SQL constant hoisted out of a function)
    are collected under ``<module>``, so hoisting a statement cannot make it
    escape the scan.
    """
    literals: list[_SqlLiteral] = []
    for directory in PRODUCTION_DIRS:
        root = REPO_ROOT / directory
        assert root.is_dir(), f"production directory {directory} is missing"
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            _collect(tree, relative, "<module>", literals)
    return literals


_LITERALS = _sql_literals()


def _matching(pattern: re.Pattern[str]) -> list[_SqlLiteral]:
    return [literal for literal in _LITERALS if pattern.search(literal.text)]


def test_the_scan_sees_the_production_tree_it_claims_to_scan() -> None:
    """Non-vacuity: an empty or truncated literal set makes every count below pass."""
    assert len(_LITERALS) > 1000, len(_LITERALS)
    scanned_files = {literal.path for literal in _LITERALS}
    assert BACKFILL_MODULE in scanned_files
    assert UPSERT_MODULE in scanned_files
    assert {path.split("/")[0] for path in scanned_files} == set(PRODUCTION_DIRS)


def test_exactly_one_in_place_river_segment_update_exists_and_it_bumps_the_generation() -> None:
    """The `UPDATE core.river_segment` half of the invariant's "only".

    A second in-place rewrite anywhere in production code would move geometry
    with no cache-key rotation, and every digest test in the repo would stay
    green -- they all exercise the one function that does bump.
    """
    updates = _matching(_UPDATE_RIVER_SEGMENT)

    assert len(updates) == 1, (
        "exactly one UPDATE core.river_segment may exist in production code; found: "
        + ", ".join(update.where() for update in updates)
    )
    assert updates[0].path == BACKFILL_MODULE, updates[0].where()
    assert updates[0].function == BACKFILL_FUNCTION, (
        f"the only UPDATE core.river_segment must live in {BACKFILL_FUNCTION}; "
        f"found at {updates[0].where()}"
    )


def test_the_generation_bump_is_issued_once_and_only_by_the_backfill() -> None:
    """One bump, in the function that holds the UPDATE it describes.

    A bump anywhere else would rotate the national keys for a write the
    invariant does not cover; a second bump in the same function would
    double-count and is just as wrong as none.
    """
    bumps = _matching(_GENERATION_BUMP)

    assert len(bumps) == 1, (
        "exactly one geometry_generation bump may exist in production code; found: "
        + ", ".join(bump.where() for bump in bumps)
    )
    assert bumps[0].path == BACKFILL_MODULE, bumps[0].where()
    assert bumps[0].function == BACKFILL_FUNCTION, bumps[0].where()
    assert "UPDATE core.river_network_version" in bumps[0].text, bumps[0].where()


def test_exactly_one_river_segment_upsert_exists_and_it_never_writes_geom() -> None:
    """The `ON CONFLICT ... DO UPDATE` half, pinned column by column.

    `_seed_output_segment_rows` rewrites `properties_json` in place on existing
    output rows -- and therefore the STORED `stream_type` -- without bumping the
    generation itself. That is a recorded Non-goal: both of its call sites run
    `_backfill_output_segment_geometry(..., only_missing=False)` on the same
    cursor immediately afterwards, which restores `Type` and bumps when it
    updates a row.

    The SET list is pinned rather than merely "no UPDATE statement exists",
    because `geom = EXCLUDED.geom` added HERE would be a geometry rewrite with
    no rotation that the UPDATE-only pin above cannot see, and neither entry
    point fails closed on it (`_qhh_output_segment_geometry_counts` counts
    `geom IS NULL` only, and this upsert never nulls geom).
    """
    upserts = [
        literal
        for literal in _LITERALS
        if _INSERT_RIVER_SEGMENT.search(literal.text) and _ON_CONFLICT_DO_UPDATE.search(literal.text)
    ]

    assert len(upserts) == 1, (
        "exactly one INSERT ... ON CONFLICT ... DO UPDATE on core.river_segment may exist; found: "
        + ", ".join(upsert.where() for upsert in upserts)
    )
    upsert = upserts[0]
    assert upsert.path == UPSERT_MODULE, upsert.where()
    assert upsert.function == UPSERT_FUNCTION, (
        f"the only core.river_segment upsert must live in {UPSERT_FUNCTION}; found at {upsert.where()}"
    )

    set_clause_match = re.search(
        r"\bDO\s+UPDATE\s+SET\b(?P<columns>[\s\S]*?)(?:\bWHERE\b|\bRETURNING\b|\Z)",
        upsert.text,
        re.IGNORECASE,
    )
    assert set_clause_match is not None, f"could not locate the DO UPDATE SET clause at {upsert.where()}"
    assignments = [
        assignment.strip()
        for assignment in set_clause_match.group("columns").split(",")
        if assignment.strip()
    ]
    columns = [assignment.split("=")[0].strip() for assignment in assignments]
    assert columns == ["segment_order", "properties_json"], (
        f"{upsert.where()}: the output-segment upsert may only rewrite segment_order and "
        f"properties_json; found {columns}. Writing geom here would move the geometry the "
        "national tiles paint without bumping core.river_network_version.geometry_generation."
    )
    # Belt and braces on the column the invariant is about: not merely absent
    # from the parsed list but absent from the clause text, so a spelling the
    # split above mis-parses cannot hide it.
    assert "geom" not in set_clause_match.group("columns").lower(), upsert.where()
