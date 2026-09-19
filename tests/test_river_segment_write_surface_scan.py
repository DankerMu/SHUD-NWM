"""The river-segment write surface is exactly one in-place UPDATE (#2031, #2154).

`fix-national-digest-cache-identity-2031`'s governing invariant is that the
tile cache identity rotates whenever the geometry the tile paints is rewritten
in place. It is enforced by ONE counter bump, inside ONE function:
`workers/model_registry/basins_registry_import.py::_backfill_output_segment_geometry`
issues the only `UPDATE core.river_segment` in production code and bumps
`core.river_network_version.geometry_generation` in the same transaction.

That invariant is only as good as the "only" in it. A second in-place rewrite
added anywhere under `apps/`, `services/`, `workers/`, `packages/`, `scripts/`
or `db/` would move geometry with no rotation, and nothing else in the repo
would notice -- the digest tests all run against the one function that does
bump. So the boundary is pinned by scanning the source.

Three write shapes are pinned (`classify_sql` is the one classifier, and is
itself tested on synthetic literals, positive and negative):

* a bare `UPDATE core.river_segment` -- exactly one, in the backfill;
* an `INSERT ... ON CONFLICT ... DO UPDATE` on the table, of which exactly one
  exists (`qhh_production_bootstrap.py::_seed_output_segment_rows`). That upsert
  rewrites `properties_json` -- and therefore the STORED `stream_type` -- but
  never `geom`. Both of its entry points run the trailing
  `_backfill_output_segment_geometry(..., only_missing=False)` on the same
  cursor and then fail closed (`QHH_OUTPUT_SEGMENT_STREAM_TYPE_INCOMPLETE`) if a
  `Type` is still missing (#2154 D3-1). Its `DO UPDATE SET` column list is
  pinned COLUMN BY COLUMN: adding `geom = EXCLUDED.geom` to it would create a
  geometry rewrite with no rotation that the `UPDATE`-only pin cannot see;
* ANY write of `geometry_generation` (#2154 D3-2), whatever its spelling -- a
  `SET` assignment with any right-hand side, a column in an `INSERT` column
  list, one assignment among several in a multi-column `SET`, or DDL naming the
  column. Exactly one DML write exists (the backfill's `+ 1`); the one DDL
  statement is the `ADD COLUMN` of `db/migrations/000057_...sql`, allowlisted by
  file name. Reads (`SELECT rnv.geometry_generation`, a `WHERE` predicate, a
  right-hand side) are not writes. The two production writers of
  `core.river_network_version` rows (`_ensure_river_network`'s INSERT and
  `_refresh_parent_version_materialization`'s UPDATE) are additionally pinned
  by name to leave the column out.

Scanned text: every string literal of every `.py` file under PRODUCTION_DIRS
(via the AST, not raw file text: a text regex for `INSERT ... ON CONFLICT` spans
from one statement to an unrelated one hundreds of lines later, and comments or
docstrings that merely DESCRIBE a statement would count as one), plus every
statement of every `db/**/*.sql` file (#2154 D3-3), comment-stripped and split
on `;`. Known limits of that split, recorded rather than papered over: a
`DO $$ ... $$` block (000037) is cut into fragments, and an
`ALTER COLUMN geom TYPE ... USING` rewrite is not one of the shapes above; a
statement spliced across several Python literals is only seen piecewise. None
changes a count today. Row-level INSERT/DELETE of river segments is outside the
invariant by design (#2154 D3-5) and is not scanned. Every failure message names
the offending file and line.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DIRS = ("apps", "services", "workers", "packages", "scripts", "db")
# Directories whose `*.sql` files are scanned as statement text. Mirrored by
# scripts/select_ci_tests.py (a meta-guard parses this binding).
SQL_DIRS = ("db",)

BACKFILL_MODULE = "workers/model_registry/basins_registry_import.py"
BACKFILL_FUNCTION = "_backfill_output_segment_geometry"
UPSERT_MODULE = "workers/model_registry/qhh_production_bootstrap.py"
UPSERT_FUNCTION = "_seed_output_segment_rows"
# The one statement allowed to name `geometry_generation` in DDL.
GENERATION_DDL_ALLOWLIST = ("db/migrations/000057_river_network_version_geometry_generation.sql",)
SQL_STATEMENT_FUNCTION = "<sql>"

RIVER_SEGMENT_UPDATE = "river_segment_update"
RIVER_SEGMENT_UPSERT = "river_segment_upsert"
GENERATION_WRITE = "geometry_generation_write"
GENERATION_DDL = "geometry_generation_ddl"

# `\b` after `river_segment` so `core.river_segment_crosswalk` -- a different
# table with its own upsert -- is not counted as this one.
_UPDATE_RIVER_SEGMENT = re.compile(r"\bUPDATE\s+core\.river_segment\b", re.IGNORECASE)
_INSERT_RIVER_SEGMENT = re.compile(r"\bINSERT\s+INTO\s+core\.river_segment\b", re.IGNORECASE)
_ON_CONFLICT_DO_UPDATE = re.compile(r"\bON\s+CONFLICT\b[\s\S]*?\bDO\s+UPDATE\b", re.IGNORECASE)
_GENERATION_BUMP = re.compile(r"\bSET\s+geometry_generation\s*=\s*geometry_generation\s*\+\s*1\b", re.IGNORECASE)
_GENERATION_WORD = re.compile(r"\bgeometry_generation\b", re.IGNORECASE)
# A SET clause's assignment list, up to the clause that ends it. The target of
# an assignment is at the start of the list or right after `,` / `(` (the
# multi-column `SET (a, b) = (...)` form); a right-hand-side read follows `=`.
_SET_CLAUSE = re.compile(r"\bSET\b(?P<assignments>[\s\S]*?)(?=\bWHERE\b|\bFROM\b|\bRETURNING\b|;|\Z)", re.IGNORECASE)
_SET_TARGET = re.compile(r"(?:\A|[,(])\s*geometry_generation\s*[=,)]", re.IGNORECASE)
_INSERT_COLUMNS = re.compile(r"\bINSERT\s+INTO\s+[\w.\"]+\s*\((?P<columns>[^)]*)\)", re.IGNORECASE)
_DDL = re.compile(r"\b(?:ALTER|CREATE)\s+TABLE\b", re.IGNORECASE)


def classify_sql(text: str) -> frozenset[str]:
    """Which pinned write shapes a piece of SQL text contains. Pure; no I/O."""
    kinds: set[str] = set()
    if _UPDATE_RIVER_SEGMENT.search(text):
        kinds.add(RIVER_SEGMENT_UPDATE)
    if _INSERT_RIVER_SEGMENT.search(text) and _ON_CONFLICT_DO_UPDATE.search(text):
        kinds.add(RIVER_SEGMENT_UPSERT)
    if _GENERATION_WORD.search(text):
        if _DDL.search(text):
            kinds.add(GENERATION_DDL)
        if any(_SET_TARGET.search(match.group("assignments")) for match in _SET_CLAUSE.finditer(text)) or any(
            _GENERATION_WORD.search(match.group("columns")) for match in _INSERT_COLUMNS.finditer(text)
        ):
            kinds.add(GENERATION_WRITE)
    return frozenset(kinds)


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


_SQL_BLOCK_COMMENT = re.compile(r"/\*[\s\S]*?\*/")
_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")


def _sql_statements(path: str, text: str) -> list[_SqlLiteral]:
    """Split a `.sql` file into comment-stripped statements, each with its line.

    Comments are blanked (block comments keep their newlines) so a migration's
    prose about a statement never counts as one, and line numbers stay true.
    """
    text = _SQL_BLOCK_COMMENT.sub(lambda match: "\n" * match.group(0).count("\n"), text)
    text = _SQL_LINE_COMMENT.sub("", text)
    statements: list[_SqlLiteral] = []
    line = 1
    for chunk in text.split(";"):
        stripped = chunk.strip()
        if stripped:
            leading = chunk[: len(chunk) - len(chunk.lstrip())].count("\n")
            statements.append(_SqlLiteral(path, line + leading, SQL_STATEMENT_FUNCTION, stripped))
        line += chunk.count("\n")
    return statements


def _sql_literals() -> list[_SqlLiteral]:
    """Every string literal in production code, tagged with its enclosing function,
    plus every statement of every `.sql` file under SQL_DIRS.

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
    for directory in SQL_DIRS:
        root = REPO_ROOT / directory
        assert root.is_dir(), f"SQL directory {directory} is missing"
        for path in sorted(root.rglob("*.sql")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            literals.extend(_sql_statements(relative, path.read_text(encoding="utf-8")))
    return literals


_LITERALS = _sql_literals()


def _classified(kind: str, literals: list[_SqlLiteral] | None = None) -> list[_SqlLiteral]:
    return [literal for literal in (_LITERALS if literals is None else literals) if kind in classify_sql(literal.text)]


def test_the_scan_sees_the_production_tree_it_claims_to_scan() -> None:
    """Non-vacuity: an empty or truncated literal set makes every count below pass."""
    assert len(_LITERALS) > 1000, len(_LITERALS)
    scanned_files = {literal.path for literal in _LITERALS}
    assert BACKFILL_MODULE in scanned_files
    assert UPSERT_MODULE in scanned_files
    assert {path.split("/")[0] for path in scanned_files} == set(PRODUCTION_DIRS)
    # `db/` is read both ways: its Python (the demo seed writes core.river_segment)
    # and its SQL statements (every migration, the allowlisted one included).
    assert "db/seeds/seed_demo.py" in scanned_files
    sql_files = {literal.path for literal in _LITERALS if literal.function == SQL_STATEMENT_FUNCTION}
    assert len([path for path in sql_files if path.startswith("db/migrations/")]) > 40, sorted(sql_files)
    assert set(GENERATION_DDL_ALLOWLIST) <= sql_files


def test_exactly_one_in_place_river_segment_update_exists_and_it_bumps_the_generation() -> None:
    """The `UPDATE core.river_segment` half of the invariant's "only".

    A second in-place rewrite anywhere in production code -- a Python literal
    or a `db/**/*.sql` statement -- would move geometry with no cache-key
    rotation, and every digest test in the repo would stay green.
    """
    updates = _classified(RIVER_SEGMENT_UPDATE)

    assert len(updates) == 1, "exactly one UPDATE core.river_segment may exist in production code; found: " + ", ".join(
        update.where() for update in updates
    )
    assert updates[0].path == BACKFILL_MODULE, updates[0].where()
    assert updates[0].function == BACKFILL_FUNCTION, (
        f"the only UPDATE core.river_segment must live in {BACKFILL_FUNCTION}; found at {updates[0].where()}"
    )


def test_the_only_geometry_generation_write_is_the_backfill_bump() -> None:
    """#2154 D3-2: any write of the column, in any spelling, counts.

    A bump anywhere else would rotate keys for a write the invariant does not
    cover; a reset (`= 0`), a constant, a second bump, or the column slipped
    into an INSERT list would each break the monotonic signal the digests read.
    """
    writes = _classified(GENERATION_WRITE)

    assert len(writes) == 1, "exactly one geometry_generation write may exist in production code; found: " + ", ".join(
        write.where() for write in writes
    )
    assert writes[0].path == BACKFILL_MODULE, writes[0].where()
    assert writes[0].function == BACKFILL_FUNCTION, writes[0].where()
    assert _GENERATION_BUMP.search(writes[0].text), f"{writes[0].where()}: the one write must be the `+ 1` bump"
    assert "UPDATE core.river_network_version" in writes[0].text, writes[0].where()


def test_geometry_generation_ddl_exists_only_in_the_allowlisted_migration() -> None:
    """The column's `ADD COLUMN` is the one named exception, pinned by file."""
    ddl = _classified(GENERATION_DDL)

    assert [literal.path for literal in ddl] == list(GENERATION_DDL_ALLOWLIST), (
        "geometry_generation DDL may only appear in "
        f"{GENERATION_DDL_ALLOWLIST}; found: " + ", ".join(literal.where() for literal in ddl)
    )
    assert re.search(r"\bADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+geometry_generation\b", ddl[0].text, re.IGNORECASE), ddl[
        0
    ].where()
    # The allowlisted file contributes DDL only, never a data write.
    assert GENERATION_WRITE not in classify_sql(ddl[0].text), ddl[0].where()


@pytest.mark.parametrize(
    ("function", "statement"),
    [
        ("_ensure_river_network", re.compile(r"\bINSERT\s+INTO\s+core\.river_network_version\b", re.IGNORECASE)),
        (
            "_refresh_parent_version_materialization",
            re.compile(r"\bUPDATE\s+core\.river_network_version\b", re.IGNORECASE),
        ),
    ],
)
def test_the_network_version_writers_leave_geometry_generation_out(function: str, statement: re.Pattern[str]) -> None:
    """#2154 item 2: import never resets or sets the counter.

    `_ensure_river_network` creates the row (the column defaults to 0) and
    `_refresh_parent_version_materialization` refreshes `segment_count` /
    `source_uri` / `checksum`; neither may name `geometry_generation`, or a
    re-import could move the counter backwards under the digests.
    """
    found = [
        literal
        for literal in _LITERALS
        if literal.path == BACKFILL_MODULE and literal.function == function and statement.search(literal.text)
    ]

    assert len(found) == 1, f"{BACKFILL_MODULE}::{function}: expected one such statement, found {len(found)}"
    assert not _GENERATION_WORD.search(found[0].text), (
        f"{found[0].where()}: {function} must not write geometry_generation"
    )


def test_exactly_one_river_segment_upsert_exists_and_it_never_writes_geom() -> None:
    """The `ON CONFLICT ... DO UPDATE` half, pinned column by column.

    `_seed_output_segment_rows` rewrites `properties_json` in place on existing
    output rows -- and therefore the STORED `stream_type` -- without bumping the
    generation itself. Both of its entry points run
    `_backfill_output_segment_geometry(..., only_missing=False)` on the same
    cursor immediately afterwards, which restores `Type` and bumps when it
    updates a row, and then fail closed if a `Type` is still missing.

    The SET list is pinned rather than merely "no UPDATE statement exists",
    because `geom = EXCLUDED.geom` added HERE would be a geometry rewrite with
    no rotation that the UPDATE-only pin above cannot see, and neither entry
    point fails closed on it (`_qhh_output_segment_geometry_counts` counts
    `geom IS NULL` only, and this upsert never nulls geom).
    """
    upserts = _classified(RIVER_SEGMENT_UPSERT)

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
        assignment.strip() for assignment in set_clause_match.group("columns").split(",") if assignment.strip()
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


# --------------------------------------------------------------------------
# #2154 B-7: the classifier on synthetic literals (production code untouched)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("SET geometry_generation = 0", GENERATION_WRITE),
        ("SET geometry_generation=geometry_generation+2", GENERATION_WRITE),
        (
            "UPDATE core.river_network_version SET segment_count = 1, geometry_generation = 5",
            GENERATION_WRITE,
        ),
        (
            "UPDATE core.river_network_version SET (segment_count, geometry_generation) = (1, 5) "
            "WHERE river_network_version_id = %s",
            GENERATION_WRITE,
        ),
        (
            "INSERT INTO core.river_network_version (river_network_version_id, basin_version_id, "
            "geometry_generation) VALUES (%s, %s, 0)",
            GENERATION_WRITE,
        ),
        (
            "INSERT INTO core.river_network_version AS t (river_network_version_id) VALUES (%s) "
            "ON CONFLICT (river_network_version_id) DO UPDATE SET geometry_generation = EXCLUDED.geometry_generation",
            GENERATION_WRITE,
        ),
        (
            "UPDATE core.river_network_version SET geometry_generation = geometry_generation + 1 "
            "WHERE river_network_version_id = %s",
            GENERATION_WRITE,
        ),
        (
            "ALTER TABLE core.river_network_version ADD COLUMN IF NOT EXISTS geometry_generation INTEGER",
            GENERATION_DDL,
        ),
        ("UPDATE core.river_segment SET geom = ST_Multi(geom)", RIVER_SEGMENT_UPDATE),
        (
            "INSERT INTO core.river_segment (river_segment_id) VALUES (%s) "
            "ON CONFLICT (river_segment_id, river_network_version_id) DO UPDATE SET geom = EXCLUDED.geom",
            RIVER_SEGMENT_UPSERT,
        ),
    ],
)
def test_classifier_flags_every_write_shape(text: str, kind: str) -> None:
    assert kind in classify_sql(text), f"{text!r} must classify as {kind}"


def test_a_sql_file_river_segment_update_is_flagged_through_the_statement_split() -> None:
    """A data-fix migration is a write surface too (#2154 D3-3)."""
    text = (
        "-- rewrite geometry: UPDATE core.river_segment in a comment is not a statement\n"
        "BEGIN;\n"
        "/* UPDATE core.river_segment SET geom = NULL; */\n"
        "UPDATE core.river_segment SET geom = ST_Multi(ST_GeomFromText('LINESTRING(0 0,1 1)', 4490));\n"
        "COMMIT;\n"
    )
    statements = _sql_statements("db/migrations/999999_synthetic.sql", text)

    flagged = _classified(RIVER_SEGMENT_UPDATE, statements)
    assert [literal.lineno for literal in flagged] == [4], [literal.where() for literal in statements]
    assert [literal.text for literal in statements] == ["BEGIN", flagged[0].text, "COMMIT"]


@pytest.mark.parametrize(
    "text",
    [
        "SELECT rnv.geometry_generation FROM core.river_network_version rnv",
        (
            "SELECT mi.run_id, rnv.geometry_generation FROM core.model_instance mi "
            "LEFT JOIN core.river_network_version rnv "
            "ON rnv.river_network_version_id = mi.river_network_version_id"
        ),
        "geometry_generation",
        "SELECT 1 FROM core.river_network_version WHERE geometry_generation = 3",
        "UPDATE core.river_network_version SET segment_count = %s WHERE geometry_generation = 0",
        "UPDATE core.river_network_version SET segment_count = geometry_generation + 1",
        ("SELECT 1 FROM core.river_network_version WHERE river_network_version_id = %s FOR NO KEY UPDATE"),
        "INSERT INTO core.river_segment_crosswalk (river_segment_id) VALUES (%s) ON CONFLICT DO UPDATE SET x = 1",
        "SELECT geom FROM core.river_segment WHERE geom IS NULL",
    ],
)
def test_classifier_leaves_reads_alone(text: str) -> None:
    assert classify_sql(text) == frozenset(), f"{text!r} is not a write"
