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

# The key/enum column each text identity column is transitional FOR. A retained
# text aid is only ever allowed to narrow, which is exactly the statement "it is
# AND-ed with this column in the same conjunction" — see
# :func:`assert_aid_is_conjoined_with_its_counterpart`.
TEXT_AID_COUNTERPARTS: dict[str, str] = {
    "run_id": "run_key",
    "river_network_version_id": "river_network_version_key",
    "river_segment_id": "river_segment_key",
    "basin_version_id": "basin_version_key",
    "variable": "variable_e",
    # Not identity predicates on any switched surface, but 000050 gives them a
    # twin too, and the map is asserted total against TEXT_IDENTITY_COLUMNS so a
    # column cannot be classified without naming what makes it redundant.
    "unit": "unit_e",
    "quality_flag": "quality_flag_e",
}


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


def strip_all_subqueries(sql: str) -> str:
    """Return ``sql`` with EVERY parenthesized sub-``SELECT`` removed.

    The blunt companion to :func:`strip_scalar_subqueries`, added for #1442's
    bare-fragment face. Those call sites resolve identity with an ``IN``
    sub-select — ``run_key IN (SELECT run_key FROM hydro.hydro_run WHERE run_id
    = %s)`` — which is deliberately NOT comparison position, so the precise
    stripper keeps it and the authority table's own ``WHERE run_id = %s`` reads
    as a regression.

    Only safe where the fragment has no CTE and no derived table, because this
    deletes those too. That is exactly the shape of a WHERE fragment or a single
    flat statement; anything with a ``WITH`` must keep using
    :func:`outer_predicates`, or its body vanishes and its pins go vacuous.
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
        if character == "(" and _SUBQUERY_START.match(sql, index):
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


def assert_text_fact_columns(
    sql: str,
    alias: str,
    expected: set[str],
    label: str,
    allowed: tuple[str, ...] = SANCTIONED_TEXT_PUSHDOWN_COLUMNS,
) -> None:
    """The surface's text fact-column references are EXACTLY ``expected``.

    Equality rather than "none of the forbidden ones": it is red both when a
    forbidden column (``basin_version_id`` / ``river_segment_id`` / ``unit`` /
    ``quality_flag``) reappears and when a sanctioned pushdown aid is silently
    dropped, which would reintroduce the compressed-chunk collapse this change
    was amended to avoid.

    ``allowed`` is the ceiling the expectation itself is checked against, so a
    future edit cannot widen a surface just by widening its expectation. It is
    the constant-bound sanctioned set everywhere except the two national legs,
    whose correlated lateral probe may additionally bind ``river_segment_id``
    (see :data:`LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS`).

    Lives here rather than in one consumer (it was private to
    ``tests/test_river_ts_read_path_surrogate_keys.py`` until #1442) because the
    out-of-boundary cleanup oracle asserts the very same invariant on a second
    set of surfaces: two copies of a ceiling check are two ceilings.
    """
    assert expected <= set(allowed), f"{label}: expectation exceeds the allowed pushdown set"
    assert text_fact_columns(sql, alias) == expected, label


def assert_aid_is_conjoined_with_its_counterpart(sql: str, alias: str, aid: str, label: str) -> None:
    """The adjacency invariant: EVERY ``alias.aid`` comparison is AND-ed to its twin.

    A retained text predicate is sanctioned only as a REDUNDANT pushdown aid. It
    is redundant precisely when the key (or enum) predicate that supersedes it is
    AND-ed to it — then the aid can only narrow, and #1342 can delete it without
    changing the result set. An aid that drifts away from its counterpart becomes
    load-bearing, and dropping the counterpart turns the surface back into a
    text-identity consumer while every "is the text column still there" pin
    stays green.

    What is actually checked, exactly
    ---------------------------------

    On :func:`outer_predicates` output (key-resolution sub-selects stripped, so
    ``rt.run_key = (SELECT ...)`` reads as ``rt.run_key =``, which is all this
    needs; comments removed; whitespace collapsed to a single folded line, so
    re-indenting the SQL cannot change the verdict):

    * every occurrence of ``alias.aid`` **in comparison position** (followed by
      ``=`` / ``<>`` / ``!=``) must have ``alias.counterpart`` separated from it
      by exactly one ``AND`` — no other `` AND ``-separated conjunct in between.
      Either order counts: ``aid AND counterpart`` and ``counterpart AND aid``
      are the same conjunction.
    * a bare projection reference (``SELECT rt.variable``, ``GROUP BY
      rt.run_id``) is NOT an occurrence: it binds nothing, so there is nothing
      for it to be redundant to. Same operator-anchored discipline as
      ``_assert_aids_are_marked``.

    Universal, not existential (#1442 round-2, P3): the first conjoined pair used
    to satisfy the whole statement, so a surface could grow a SECOND, bare
    predicate on the same text column and stay green.

    What is NOT checked
    -------------------

    Parenthesis structure, ``OR`` branches and ``NOT`` are not analyzed — this
    operates on the flat folded text, so it cannot tell an aid inside a guard's
    ``OR`` branch from one outside it, and it cannot see that a guard's
    parentheses still close where they used to. The scan-guard fold-away shape
    is protected instead by the whole-guard verbatim pins (#1442 round-2, F5) in
    ``tests/test_river_ts_text_identity_cleanup.py`` and
    ``tests/test_qhh_latest_fallback_pushdown.py``; this invariant does not
    subsume them.

    #1341 pinned adjacency per surface as hand-written verbatim substrings (see
    ``tests/test_river_ts_read_path_surrogate_keys.py``'s coverage-scan pins).
    This is the same statement, computed instead of transcribed, so every
    surface a register renders inherits it.
    """
    counterpart = TEXT_AID_COUNTERPARTS[aid]
    outer = outer_predicates(sql)
    aid_reference = rf"\b{re.escape(alias)}\.{re.escape(aid)}\b"
    counterpart_reference = rf"\b{re.escape(alias)}\.{re.escape(counterpart)}\b"
    comparison = r"\s*(?:=|<>|!=)\s*"
    # A compared value that itself spans an ``AND`` means another conjunct sits
    # between the pair, so they are adjacent only by accident of text order.
    # `%(scan_run_id)s` contains parentheses, so "no parens" is not usable as the
    # separator test; the gap is written AND-free instead, which also keeps the
    # leftmost-match rule from picking a far counterpart over a near one.
    gap = r"(?:(?!\sAND\s).)*?"
    forward = re.compile(rf"{aid_reference}{comparison}{gap}\s+AND\s+{counterpart_reference}")
    # Anchored at the end of the text preceding the occurrence, so the
    # counterpart must be the conjunct immediately before this very aid.
    backward = re.compile(rf"{counterpart_reference}{comparison}{gap}\s+AND\s+$")

    occurrences = [match.start() for match in re.finditer(rf"{aid_reference}{comparison}", outer)]
    assert occurrences, f"{label}: text aid {alias}.{aid} is not compared anywhere"
    for position in occurrences:
        if forward.match(outer, position) is not None:
            continue
        if backward.search(outer[:position]) is not None:
            continue
        raise AssertionError(
            f"{label}: text aid {alias}.{aid} is not AND-ed with {alias}.{counterpart} "
            f"at offset {position}: ...{outer[max(0, position - 60) : position + 60]}..."
        )


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


def test_strip_all_subqueries_removes_an_in_position_key_resolution() -> None:
    """The precise stripper keeps it (not comparison position); this one must not."""
    fragment = "run_key IN (SELECT run_key FROM hydro.hydro_run WHERE run_id = ANY(%s))"

    assert "run_id = ANY(%s)" in strip_scalar_subqueries(fragment)
    assert strip_all_subqueries(fragment) == "run_key IN "


def test_strip_all_subqueries_leaves_a_bare_text_predicate_visible() -> None:
    """Non-vacuity: the regression it exists to catch must survive stripping."""
    assert strip_all_subqueries("run_id = ANY(%s)") == "run_id = ANY(%s)"


def test_strip_all_subqueries_also_eats_cte_bodies_which_is_why_it_is_fragment_only() -> None:
    """Documented hazard, pinned so the docstring cannot drift from behaviour."""
    sql = "WITH existing AS MATERIALIZED (SELECT valid_time FROM t WHERE run_id = %s) SELECT 1"

    assert "run_id = %s" not in strip_all_subqueries(sql)
    assert "run_id = %s" in outer_predicates(sql)


def test_assert_text_fact_columns_rejects_an_expectation_above_the_ceiling() -> None:
    """Widening a surface by widening its own expectation must not be possible."""
    sql = "FROM hydro.river_timeseries ts WHERE ts.river_segment_id = :segment_id"

    try:
        assert_text_fact_columns(sql, "ts", {"river_segment_id"}, "widened surface")
    except AssertionError as error:
        assert "exceeds the allowed pushdown set" in str(error)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("assert_text_fact_columns accepted a forbidden expectation")


def test_assert_text_fact_columns_is_red_when_a_sanctioned_aid_disappears() -> None:
    sql = "FROM hydro.river_timeseries ts WHERE ts.run_key = 1"

    try:
        assert_text_fact_columns(sql, "ts", {"run_id"}, "aid dropped")
    except AssertionError as error:
        assert "aid dropped" in str(error)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("assert_text_fact_columns accepted a dropped pushdown aid")


def test_adjacency_accepts_the_pair_in_either_order_across_a_reindent() -> None:
    forward = "WHERE rt.variable = 'q_down'\n  AND rt.variable_e = 'q_down'::hydro.river_variable"
    reversed_order = "WHERE rt.variable_e = 'q_down'::hydro.river_variable AND rt.variable = 'q_down'"

    for sql in (forward, reversed_order):
        assert_aid_is_conjoined_with_its_counterpart(sql, "rt", "variable", "pair")


def test_adjacency_accepts_a_pair_whose_counterpart_is_a_stripped_key_resolution() -> None:
    """The switched shape's counterpart is a sub-select, which the stripper eats."""
    sql = (
        "WHERE (%(scan_run_id)s IS NULL\n"
        "       OR (rt.run_id = %(scan_run_id)s\n"
        "           AND rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(scan_run_id)s)))"
    )

    assert_aid_is_conjoined_with_its_counterpart(sql, "rt", "run_id", "scan guard")


def test_adjacency_is_red_when_the_counterpart_is_gone() -> None:
    sql = "WHERE rt.variable = 'q_down' AND rt.valid_time >= %s"

    try:
        assert_aid_is_conjoined_with_its_counterpart(sql, "rt", "variable", "counterpart dropped")
    except AssertionError as error:
        assert "not AND-ed with rt.variable_e" in str(error)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("adjacency accepted an aid whose enum counterpart was deleted")


def test_adjacency_is_red_when_another_conjunct_separates_the_pair() -> None:
    """Same statement, different conjunction: the aid is no longer redundant.

    This is the case a plain "both columns appear in the SQL" check cannot see,
    and it is why the invariant is written as adjacency rather than presence.
    """
    sql = "WHERE rt.variable = 'q_down' AND rt.valid_time >= %s AND rt.variable_e = 'q_down'"

    try:
        assert_aid_is_conjoined_with_its_counterpart(sql, "rt", "variable", "separated pair")
    except AssertionError as error:
        assert "separated pair" in str(error)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("adjacency accepted a pair split across conjuncts")


def test_adjacency_is_red_when_a_SECOND_occurrence_of_the_aid_is_bare() -> None:
    """Universal, not existential (#1442 round-2, P3).

    One conjoined pair used to satisfy the whole statement, so a surface could
    grow a SECOND text predicate on the same column — load-bearing, with no key
    counterpart to make it redundant — and #1342 would delete a predicate that
    was narrowing the result set. Every comparison occurrence is checked, so the
    green first pair no longer covers the bare second one.
    """
    sql = (
        "WHERE rt.variable = 'q_down' AND rt.variable_e = 'q_down' "
        "AND rt.valid_time >= %s AND rt.variable <> 'q_up'"
    )

    try:
        assert_aid_is_conjoined_with_its_counterpart(sql, "rt", "variable", "bare second occurrence")
    except AssertionError as error:
        assert "bare second occurrence" in str(error)
        assert "not AND-ed with rt.variable_e" in str(error)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("adjacency accepted a second, unconjoined occurrence of the aid")


def test_adjacency_is_alias_scoped() -> None:
    """A counterpart on a DIFFERENT relation does not satisfy the fact table's aid."""
    sql = "WHERE rt.variable = 'q_down' AND other.variable_e = 'q_down'"

    try:
        assert_aid_is_conjoined_with_its_counterpart(sql, "rt", "variable", "wrong alias")
    except AssertionError as error:
        assert "wrong alias" in str(error)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("adjacency accepted a counterpart on another relation")


def test_every_text_identity_column_has_a_declared_counterpart() -> None:
    """No text column may be sanctioned somewhere with no twin to be redundant to."""
    assert set(TEXT_AID_COUNTERPARTS) == set(TEXT_IDENTITY_COLUMNS)
    assert set(LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS) <= set(TEXT_AID_COUNTERPARTS)


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
