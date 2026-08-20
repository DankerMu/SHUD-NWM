"""SQL-shape oracle for the #1341 read-path pins, plus its own self-tests.

Why this module exists
----------------------

Issue #1341 moved the display-boundary ``hydro.river_timeseries`` predicates
onto integer surrogate keys while the caller-supplied identity stays text. The
resulting queries therefore contain text identity predicates in two very
different roles:

* inside a key-resolution scalar sub-select — ``(SELECT run_key FROM
  hydro.hydro_run WHERE run_id = :run_id)`` — which is REQUIRED on every
  switched surface, and
* as a fact-table predicate — ``ts.basin_version_id = :basin_version_id`` —
  which the change forbids outside the sanctioned transitional set.

A bare substring check cannot tell them apart, so a "this text predicate is
gone" pin that ignores the distinction passes on code that never switched.
:func:`strip_scalar_subqueries` removes the key-resolution sub-selects, and the
negative pins then assert against what is left: the outer query's own
predicates.

Why it is a test module, not a plain helper
-------------------------------------------

Round-1 cross-review found the first version of this oracle silently broken:
its ``(\\s*SELECT`` probe also matched CTE openers (``x AS (SELECT ...)``) and
derived tables, so it deleted whole query bodies and five negative pins went
green against unswitched master source. An oracle that nothing tests is not an
oracle. It therefore lives here as ``test_*.py`` so that:

* the full-suite run collects its self-tests (a plain ``sql_shape_helpers.py``
  matches neither ``python_files`` pattern and would never run in ``pytest
  tests/``), and
* a change to it self-selects in ``scripts/select_ci_tests.py`` and exits 0
  with real assertions instead of exit-5 "no tests ran".

Its three consumer test files are additionally mapped in that script's
``CHANGED_TEST_FILE_RULES``, so a helper-only diff still runs the pins that
depend on it.

Stripping rule
--------------

Only a sub-select in **comparison-value position** is removed — that is, a
``(SELECT`` whose preceding token is a comparison operator, which is exactly
the shape of every key-resolution sub-select in this change. Everything else
is preserved on purpose:

* ``x AS (SELECT ...)`` — CTE opener. Stripping it deletes the query.
* ``FROM (SELECT ...) alias`` / ``JOIN (SELECT ...) alias`` — derived table.
* ``EXISTS (SELECT ... FROM hydro.river_timeseries ts ...)`` — the national
  identity-stats probe reads the fact table INSIDE an ``EXISTS``. Stripping
  ``EXISTS`` bodies would make that surface's negative pin vacuous, which is
  the same failure round-1 caught, one level down.

Hybrid pushdown vocabulary
--------------------------

The user-adjudicated remedy for the compressed-chunk collapse keeps a
redundant TEXT predicate on exactly ``run_id`` / ``river_network_version_id`` /
``variable``, each AND-ed with its key or enum counterpart, on every
in-boundary fact read. The pins therefore assert two things that a plain
"is the text gone" check cannot express, so both live here as shared
vocabulary:

* :data:`SANCTIONED_TEXT_PUSHDOWN_COLUMNS` / :data:`FORBIDDEN_TEXT_FACT_COLUMNS`
  / :data:`LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS` — one definition of the
  boundary, so a surface cannot quietly grow an extra text predicate by having
  its own private list. The third set exists because the boundary is
  positional: inside the national legs' correlated ``CROSS JOIN LATERAL``
  probe, ``river_segment_id`` is a per-loop constant that really does push
  down, while on every constant-binding surface it would still be the
  forbidden text fact join.
* :func:`outer_predicates` — sub-selects stripped, comments removed and
  whitespace collapsed, so an "immediately followed by its key counterpart"
  adjacency assertion can be written as one exact substring and stays readable
  when the SQL is re-indented.
"""

from __future__ import annotations

import ast
import re
import textwrap

_SUBQUERY_START = re.compile(r"\(\s*SELECT\b", re.IGNORECASE)

# The token immediately before a key-resolution sub-select is a comparison
# operator. The lookbehind keeps composite operators that merely END in one of
# these characters out: `->` / `->>` (json) and `=>` (named argument) are not
# comparisons, and treating them as such would widen the stripper again.
_COMPARISON_TAIL = re.compile(r"(?<![-=<>!])(<>|!=|<=|>=|=|<|>)\s*$")

_LINE_COMMENT = re.compile(r"--[^\n]*")

_WHITESPACE = re.compile(r"\s+")

# Text identity columns of hydro.river_timeseries, as #1341 classifies them.
#
# Sanctioned: kept as redundant transitional pushdown predicates because
# compression still segments/orders compressed chunks by them (000047:
# segmentby run_id, river_network_version_id, river_segment_id; orderby
# variable, valid_time) and TimescaleDB 2.10.2 cannot push an integer-key
# predicate through that. `river_segment_id` is a segmentby column too but is
# NOT sanctioned on these surfaces: they bind identity as a constant and reach
# the fact table once, so a `river_segment_id` predicate could only be a text
# fact join, which the delta forbids outright.
SANCTIONED_TEXT_PUSHDOWN_COLUMNS: tuple[str, ...] = (
    "run_id",
    "river_network_version_id",
    "variable",
)
FORBIDDEN_TEXT_FACT_COLUMNS: tuple[str, ...] = (
    "basin_version_id",
    "river_segment_id",
    "unit",
    "quality_flag",
)
# Position-dependent extension (#1341 round-3 P1 remedy). The two national
# legs no longer join the fact table set-based: they drive from tile_segments
# and probe it once per segment through a `CROSS JOIN LATERAL (... LIMIT 1)`.
# Inside that probe the correlated `lr.` / `seg.` values are constants for the
# duration of one loop, so `river_segment_id` behaves exactly like the other
# three aids — it reaches the compressed segmentby index and the text primary
# key — rather than being the unpushable text fact join the classification
# above rules out. It is sanctioned in that position ONLY: it stays in
# FORBIDDEN_TEXT_FACT_COLUMNS so every constant-bound surface keeps rejecting
# it, and the national pins additionally assert that no `ts.` reference of
# either leg lives outside the lateral body.
LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS: tuple[str, ...] = SANCTIONED_TEXT_PUSHDOWN_COLUMNS + ("river_segment_id",)
TEXT_IDENTITY_COLUMNS: tuple[str, ...] = SANCTIONED_TEXT_PUSHDOWN_COLUMNS + FORBIDDEN_TEXT_FACT_COLUMNS


def _scan_quoted(sql: str, start: int, quote: str) -> int:
    """Index just past the quoted run beginning at ``start`` (doubled quote escapes)."""
    index = start + 1
    length = len(sql)
    while index < length:
        if sql[index] == quote:
            if index + 1 < length and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return length


def _scan_block_comment(sql: str, start: int) -> int:
    end = sql.find("*/", start + 2)
    return len(sql) if end == -1 else end + 2


def _scan_line_comment(sql: str, start: int) -> int:
    end = sql.find("\n", start)
    return len(sql) if end == -1 else end


def _skip_balanced(sql: str, start: int) -> int:
    """Index just past the parenthesis group opening at ``start``.

    Parentheses inside strings, quoted identifiers and comments do not count,
    so ``(SELECT ... WHERE name = ')' ...)`` is consumed whole rather than cut
    at the literal.
    """
    depth = 0
    index = start
    length = len(sql)
    while index < length:
        character = sql[index]
        if character in "'\"":
            index = _scan_quoted(sql, index, character)
            continue
        if sql.startswith("--", index):
            index = _scan_line_comment(sql, index)
            continue
        if sql.startswith("/*", index):
            index = _scan_block_comment(sql, index)
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return length


def _in_comparison_value_position(kept: list[str]) -> bool:
    tail = _LINE_COMMENT.sub("", "".join(kept[-160:]))
    return _COMPARISON_TAIL.search(tail) is not None


def strip_scalar_subqueries(sql: str) -> str:
    """Return ``sql`` with every comparison-position sub-``SELECT`` removed.

    CTE openers, derived tables, ``EXISTS``/``IN`` sub-selects and ordinary
    function calls are preserved; see the module docstring for why each one
    matters. String literals, quoted identifiers and both comment styles are
    traversed rather than parsed, so a ``(SELECT`` written inside any of them
    is never mistaken for a sub-select.
    """
    kept: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        character = sql[index]
        if character in "'\"":
            end = _scan_quoted(sql, index, character)
            kept.append(sql[index:end])
            index = end
            continue
        if sql.startswith("--", index):
            end = _scan_line_comment(sql, index)
            kept.append(sql[index:end])
            index = end
            continue
        if sql.startswith("/*", index):
            end = _scan_block_comment(sql, index)
            kept.append(sql[index:end])
            index = end
            continue
        if character == "(" and _SUBQUERY_START.match(sql, index) and _in_comparison_value_position(kept):
            index = _skip_balanced(sql, index)
            continue
        kept.append(character)
        index += 1
    return "".join(kept)


def strip_comments(sql: str) -> str:
    """Return ``sql`` with both comment styles replaced by a single space.

    Uses the same traversal as :func:`strip_scalar_subqueries`, so a ``--``
    written inside a string literal is text, not a comment. Comments must go
    before any adjacency assertion: the production SQL puts a "transitional
    pushdown aid" note between a text predicate and its key counterpart, and a
    naive substring check would see the comment as separation.
    """
    kept: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        character = sql[index]
        if character in "'\"":
            end = _scan_quoted(sql, index, character)
            kept.append(sql[index:end])
            index = end
            continue
        if sql.startswith("--", index):
            index = _scan_line_comment(sql, index)
            kept.append(" ")
            continue
        if sql.startswith("/*", index):
            index = _scan_block_comment(sql, index)
            kept.append(" ")
            continue
        kept.append(character)
        index += 1
    return "".join(kept)


_LOOKS_LIKE_SQL = re.compile(r"\bSELECT\b", re.IGNORECASE)


def sql_literals(python_source: str) -> tuple[str, ...]:
    """The SQL string constants of a Python source fragment, in source order.

    Two switched surfaces (``valid_times_for_layer`` and
    ``_require_hydro_mvt_source_identity``) build their SQL inline, so their
    pins can only start from a source slice. Running the SQL tokenizer over
    raw Python is NOT safe: a ``'`` in a prose comment ("the caller's text")
    opens a string scan that swallows the query, and a ``\"\"\"`` delimiter
    reads as an empty string followed by an unterminated one, so the whole SQL
    body is skipped and every pin downstream turns vacuous — the round-1
    failure mode again. Python's own parser is the only correct way to find
    where the SQL starts and stops, so use it.

    Source order matters because ``valid_times_for_layer`` selects between its
    named-identity and no-identity SQL with an inline conditional, and the two
    branches must be pinned separately. ``ast.walk`` is breadth-first, so the
    positions are sorted explicitly rather than trusted.
    """
    tree = ast.parse(textwrap.dedent(python_source))
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _LOOKS_LIKE_SQL.search(node.value) is not None
    ]
    found.sort(key=lambda node: (node.lineno, node.col_offset))
    return tuple(node.value for node in found)


def sql_from_python(python_source: str) -> str:
    """:func:`sql_literals` newline-joined, for pins that span every branch."""
    return "\n".join(sql_literals(python_source))


def outer_predicates(sql: str) -> str:
    """The outer query's own text: sub-selects gone, comments gone, one space.

    This is the canonical form the #1341 pins assert against. Key-resolution
    sub-selects contain ``WHERE run_id = :run_id`` against the AUTHORITY table
    by design, so a pin that inspects raw source cannot distinguish switched
    code from unswitched code; and whitespace-collapsing is what lets the
    "text predicate is immediately followed by its key counterpart" invariant
    be written as one exact substring.
    """
    return _WHITESPACE.sub(" ", strip_comments(strip_scalar_subqueries(sql))).strip()


def text_fact_columns(sql: str, alias: str) -> set[str]:
    """Text identity columns of the fact table referenced by the outer query.

    ``\\b`` after the column name is load-bearing: it keeps ``ts.variable``
    from matching ``ts.variable_e`` (and ``ts.unit`` from ``ts.unit_e``), which
    would make every pin unsatisfiable rather than discriminating.

    Consumers assert this set *equals* the sanctioned columns that surface is
    allowed to carry, not merely that it excludes the forbidden ones — an
    equality catches both a forbidden column appearing and a sanctioned
    pushdown aid silently disappearing.
    """
    outer = outer_predicates(sql)
    return {
        column
        for column in TEXT_IDENTITY_COLUMNS
        if re.search(rf"\b{re.escape(alias)}\.{column}\b", outer) is not None
    }


# ---------------------------------------------------------------------------
# Self-tests: the oracle's own contract
# ---------------------------------------------------------------------------


def test_comparison_position_subquery_is_stripped() -> None:
    sql = "WHERE ts.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id)"

    stripped = strip_scalar_subqueries(sql)

    assert stripped == "WHERE ts.run_key = "
    assert "run_id = :run_id" not in stripped


def test_nested_parentheses_inside_the_subquery_are_consumed_with_it() -> None:
    sql = (
        "WHERE ts.variable_e = (SELECT e FROM unnest(enum_range(NULL::hydro.river_variable)) e "
        "WHERE e::text = :variable) AND ts.valid_time = :valid_time"
    )

    stripped = strip_scalar_subqueries(sql)

    assert stripped == "WHERE ts.variable_e =  AND ts.valid_time = :valid_time"


def test_cte_opener_is_preserved() -> None:
    """The round-1 defect: ``AS (SELECT`` was treated as a scalar sub-select.

    Deleting a CTE body deletes the very predicates the negative pins inspect,
    so every pin downstream of it passes vacuously.
    """
    sql = "WITH latest_runs AS (SELECT h.run_id FROM hydro.hydro_run h WHERE h.run_id = :run_id) SELECT 1"

    stripped = strip_scalar_subqueries(sql)

    assert stripped == sql
    assert "h.run_id = :run_id" in stripped


def test_materialized_cte_opener_is_preserved() -> None:
    sql = "source_rows AS NOT MATERIALIZED (\n    SELECT ts.run_id FROM hydro.river_timeseries ts\n)"

    assert strip_scalar_subqueries(sql) == sql


def test_derived_table_is_preserved() -> None:
    sql = "FROM hydro.river_timeseries ts JOIN (SELECT h.run_key FROM hydro.hydro_run h) lr ON lr.run_key = ts.run_key"

    assert strip_scalar_subqueries(sql) == sql


def test_exists_subquery_is_preserved_so_the_probe_pin_still_bites() -> None:
    """The national identity probe reads the fact table inside an ``EXISTS``."""
    sql = (
        "SELECT CASE WHEN EXISTS (SELECT 1 FROM hydro.river_timeseries ts "
        "WHERE ts.basin_version_id = :basin_version_id) THEN 1 ELSE 0 END"
    )

    stripped = strip_scalar_subqueries(sql)

    assert "ts.basin_version_id = :basin_version_id" in stripped


def test_in_subquery_is_preserved() -> None:
    sql = "WHERE ts.run_id IN (SELECT h.run_id FROM hydro.hydro_run h)"

    assert strip_scalar_subqueries(sql) == sql


def test_subquery_written_inside_a_string_literal_is_not_stripped() -> None:
    sql = "SELECT 'x = (SELECT 1)' AS note, ts.run_id FROM hydro.river_timeseries ts"

    assert strip_scalar_subqueries(sql) == sql


def test_parenthesis_inside_a_string_literal_does_not_end_a_stripped_subquery() -> None:
    sql = "WHERE ts.run_key = (SELECT run_key FROM t WHERE label = ') not the end') AND ts.valid_time = :vt"

    stripped = strip_scalar_subqueries(sql)

    assert stripped == "WHERE ts.run_key =  AND ts.valid_time = :vt"


def test_doubled_quote_escape_inside_a_string_literal_is_traversed() -> None:
    sql = "SELECT 'it''s = (SELECT 1)' AS note, ts.run_id FROM hydro.river_timeseries ts"

    assert strip_scalar_subqueries(sql) == sql


def test_quoted_identifier_is_traversed() -> None:
    sql = 'SELECT ts."odd ( name = (SELECT 1)" , ts.run_id FROM hydro.river_timeseries ts'

    assert strip_scalar_subqueries(sql) == sql


def test_subquery_written_inside_a_line_comment_is_not_stripped() -> None:
    sql = "-- was: ts.run_key = (SELECT run_key FROM hydro.hydro_run)\nWHERE ts.run_id = :run_id"

    assert strip_scalar_subqueries(sql) == sql


def test_subquery_written_inside_a_block_comment_is_not_stripped() -> None:
    sql = "/* was: x = (SELECT 1) */ WHERE ts.run_id = :run_id"

    assert strip_scalar_subqueries(sql) == sql


def test_a_comment_ending_in_an_operator_does_not_arm_the_stripper() -> None:
    """A comment is not a token; only real SQL may put the stripper in value position."""
    sql = "-- pushdown aid, see #1342 =\nFROM (SELECT 1) x"

    assert strip_scalar_subqueries(sql) == sql


def test_json_and_named_argument_arrows_are_not_comparisons() -> None:
    for sql in (
        "SELECT properties_json ->> (SELECT 'Type') FROM core.river_segment",
        "SELECT ST_AsMVTGeom(geom, extent => (SELECT 4096))",
    ):
        assert strip_scalar_subqueries(sql) == sql, sql


def test_unbalanced_parenthesis_terminates_instead_of_looping() -> None:
    assert strip_scalar_subqueries("WHERE a = (SELECT 1") == "WHERE a = "


def test_sql_from_python_survives_an_apostrophe_in_a_prose_comment() -> None:
    """Raw-source tokenizing is unsafe; this is the case that proves it."""
    source = (
        "def probe():\n"
        "    # resolve the caller's text identity inside the query\n"
        '    return text("""\n'
        "        SELECT 1 FROM hydro.river_timeseries\n"
        "        WHERE run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id)\n"
        '    """)\n'
    )

    extracted = sql_from_python(source)

    assert "SELECT 1 FROM hydro.river_timeseries" in extracted
    assert "caller's" not in extracted
    # And the extracted SQL is now something the tokenizer can actually reduce.
    assert strip_scalar_subqueries(extracted).count("run_id = :run_id") == 0
    # Whereas the raw Python defeats it entirely.
    assert "run_id = :run_id" in strip_scalar_subqueries(source)


def test_sql_from_python_skips_non_sql_constants() -> None:
    source = 'def f():\n    label = "not sql"\n    return text("""SELECT 1""")\n'

    assert sql_from_python(source) == "SELECT 1"


def test_sql_literals_keeps_conditional_expression_branches_in_source_order() -> None:
    """``valid_times_for_layer`` picks its SQL with an inline ``if``/``else``.

    The two branches are pinned separately, so which literal is which has to be
    positional. ``ast.walk`` visits an ``IfExp`` breadth-first (body, test,
    orelse) and would happen to agree here; sorting by position is what makes
    that not a coincidence.
    """
    source = (
        "def f(named):\n"
        "    return (\n"
        '        """SELECT a"""\n'
        "        if named\n"
        '        else """SELECT b"""\n'
        "    )\n"
    )

    assert sql_literals(source) == ("SELECT a", "SELECT b")
    assert sql_from_python(source) == "SELECT a\nSELECT b"


def test_strip_comments_removes_both_styles_but_not_string_literals() -> None:
    sql = "WHERE a = 1 -- note\n/* block */ AND b = '-- not a comment'"

    stripped = strip_comments(sql)

    assert "note" not in stripped
    assert "block" not in stripped
    assert "'-- not a comment'" in stripped


def test_outer_predicates_collapses_a_comment_between_paired_predicates() -> None:
    """The production layout puts the aid comment between the two conjuncts.

    Without comment removal plus whitespace collapsing, the pairing assertion
    could only be written as two independent substring checks, which pass on a
    query where the text predicate sits in a completely different conjunction.
    """
    sql = """
        -- transitional compressed-chunk pushdown aid, remove with #1342
        WHERE ts.run_id = :run_id
          AND ts.run_key = (
                  SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id
              )
          AND ts.valid_time = :valid_time
    """

    assert outer_predicates(sql) == "WHERE ts.run_id = :run_id AND ts.run_key = AND ts.valid_time = :valid_time"


def test_text_fact_columns_reports_only_the_alias_it_is_asked_about() -> None:
    sql = "FROM hydro.river_timeseries ts JOIN candidate_runs cr ON cr.run_key = ts.run_key WHERE ts.run_id = :run_id"

    assert text_fact_columns(sql, "ts") == {"run_id"}
    assert text_fact_columns(sql, "cr") == set()


def test_text_fact_columns_does_not_confuse_a_text_column_with_its_enum_twin() -> None:
    sql = "WHERE ts.variable_e = 'q_down' AND ts.unit_e = 'm3/s' AND ts.quality_flag_e = 'ok'"

    assert text_fact_columns(sql, "ts") == set()


def test_text_fact_columns_ignores_columns_inside_key_resolution_subqueries() -> None:
    sql = (
        "WHERE ts.basin_version_key = (SELECT basin_version_key FROM core.basin_version "
        "WHERE basin_version_id = :basin_version_id)"
    )

    assert text_fact_columns(sql, "ts") == set()


def test_sanctioned_and_forbidden_column_sets_are_disjoint_and_complete() -> None:
    assert set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS).isdisjoint(FORBIDDEN_TEXT_FACT_COLUMNS)
    assert set(TEXT_IDENTITY_COLUMNS) == set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS) | set(FORBIDDEN_TEXT_FACT_COLUMNS)
    # The sanctioned set is exactly the compression segmentby/orderby columns
    # that a constant-binding switched shape can push down. river_segment_id is
    # segmentby too, but those shapes never bind it, so it stays forbidden
    # there.
    assert set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS) == {"run_id", "river_network_version_id", "variable"}
    assert "river_segment_id" in FORBIDDEN_TEXT_FACT_COLUMNS


def test_lateral_probe_set_extends_the_sanctioned_set_by_exactly_river_segment_id() -> None:
    """The correlated-probe position is the one place river_segment_id is allowed.

    Keeping it in FORBIDDEN as well is deliberate, not a contradiction: the
    classification is positional, and every consumer that is not a lateral
    probe must keep reading it as forbidden.
    """
    assert set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS) < set(LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS)
    assert set(LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS) - set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS) == {"river_segment_id"}
    # And the widened set is still a subset of what text_fact_columns can see,
    # so a national pin written against it cannot be unsatisfiable.
    assert set(LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS) <= set(TEXT_IDENTITY_COLUMNS)


def test_production_tile_sql_keeps_its_fact_predicates_after_stripping() -> None:
    """End-to-end guard against the round-1 vacuity, on the real SUT text.

    If the stripper ever eats CTE bodies again, the fact table and its
    predicates vanish here and every consumer pin turns vacuous — this test
    fails first and names the cause.
    """
    from services.tiles.mvt import postgis_tile_sql

    for layer in ("hydro", "hydro-national"):
        stripped = strip_scalar_subqueries(postgis_tile_sql(layer))
        assert "FROM hydro.river_timeseries ts" in stripped, layer
        assert "ts.valid_time = :valid_time" in stripped, layer
        # The key-resolution sub-selects, and only those, are gone.
        assert "SELECT run_key FROM hydro.hydro_run" not in stripped, layer
        assert "enum_range" not in stripped, layer


def test_python_source_surfaces_reduce_to_real_sql_before_their_pins_run() -> None:
    """Same end-to-end guard for the two surfaces whose SQL is inline in Python.

    Their pins start from a source slice, so vacuity here would be invisible:
    if extraction returned nothing, every "text predicate is gone" assertion
    downstream would pass on any code at all.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    surfaces = {
        "valid_times_for_layer": (
            repo_root / "services" / "tiles" / "mvt.py",
            "def valid_times_for_layer",
            "def _valid_time_discovery",
        ),
        "existence probe": (
            repo_root / "apps" / "api" / "routes" / "hydro_display.py",
            "def _require_hydro_mvt_source_identity",
            "def _require_run_source_identity",
        ),
    }
    for name, (path, start_anchor, end_anchor) in surfaces.items():
        source = path.read_text(encoding="utf-8")
        slice_ = source[source.index(start_anchor) : source.index(end_anchor)]
        sql = sql_from_python(slice_)

        assert "FROM hydro.river_timeseries" in sql, name
        stripped = strip_scalar_subqueries(sql)
        assert "FROM hydro.river_timeseries" in stripped, name
        assert "SELECT run_key FROM hydro.hydro_run" not in stripped, name
        assert "enum_range" not in stripped, name
