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
  down, while on the display surfaces this module's default serves it would
  still be the forbidden text fact join. #1442's forecast_store segment blocks
  are the second such position (a literal-bound segment identity, design
  D10.7); their ceiling is expressed at their own oracle rather than here, so
  the default this module hands every other caller stays narrow.
* :func:`outer_predicates` — sub-selects stripped, comments removed and
  whitespace collapsed, so an "immediately followed by its key counterpart"
  adjacency assertion can be written as one exact substring and stays readable
  when the SQL is re-indented.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

from packages.common import river_ts_render
from packages.common.river_ts_render import (
    FORBIDDEN_TEXT_FACT_COLUMNS,
    LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS,
    PUSHDOWN_AID_MARKER,
    RIVER_TABLE,
    RIVER_TABLE_LEGACY,
    SANCTIONED_TEXT_PUSHDOWN_COLUMNS,
    TEXT_AID_COUNTERPARTS,
    TEXT_IDENTITY_COLUMNS,
    assert_structurally_intact,
    fact_table_attribution,
    fact_table_name_occurrences,
    fact_table_text_identity_columns,
    outer_predicates,
    render_river_ts_sql,
    render_union_all,
    store_binding_plan,
    store_predicate,
    strip_all_subqueries,
    strip_comments,
    strip_scalar_subqueries,
    text_fact_columns,
)
from tests.river_ts_template_registry import FORECAST_STORE_SEGMENT_BLOCKS, REGISTRY, entry_by_key

REPO_ROOT = Path(__file__).resolve().parents[1]

# The text-identity vocabulary and the SQL text machinery that attributes a
# column reference to a table MOVED to `packages/common/river_ts_render.py`
# (#1980, fixture decision 2). They are re-imported rather than re-declared:
# the renderer performs the very same table-scoped no-text-identity check on
# every statement it produces, `packages/` cannot import `tests/`, and two
# copies of a boundary are two boundaries. Everything below still names them
# unqualified, so no call site in this file or its four consumers changed; the
# self-tests further down keep exercising the real implementations through
# these names, which is what stops the move from turning the oracle vacuous.
#
# What did NOT move: `sql_literals` / `sql_from_python` (Python-AST helpers, not
# SQL text), and the two `assert_*` helpers below — those raise AssertionError
# by design, whereas the renderer must raise a named exception carrying the
# entry it refused.


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
    the constant-bound sanctioned set except on two adjudicated surfaces that
    pass their own wider tuple: the two national legs, whose correlated lateral
    probe may additionally bind ``river_segment_id`` (see
    :data:`LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS`), and #1442's eight
    forecast_store segment blocks, whose literal-bound ``river_segment_id`` aid
    is the measured segmentby-pruning remedy of design D10.7 (ceiling defined in
    ``tests/test_river_ts_text_identity_cleanup.py``, deliberately NOT folded
    into the shared constant so no display surface inherits it).

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


# ---------------------------------------------------------------------------
# Per-store rendering of every registered template (#1980, task 1.3)
#
# The shared half of the coverage: this module is the one both display and
# out-of-boundary oracles depend on, so "every registered template survives both
# renderings" is asserted here once instead of twice, per file, with two
# definitions of what survival means. The per-file marker/aid CENSUS stays in
# each file's owning oracle (fixture decision 7) — this is about the templates,
# not about who owns them.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda entry: entry.key)
def test_every_registered_template_renders_for_the_legacy_store(entry) -> None:
    """Legacy is the template plus a rename — every aid included.

    The legacy table keeps 000047's text-column compression layout, so an aid
    dropped from this branch is the measured compressed-chunk collapse applied to
    exactly the rows that have not moved yet.
    """
    template = entry.source()

    rendered = render_river_ts_sql(template, "legacy", entry=entry.key)

    assert rendered.sql == template.replace(RIVER_TABLE, RIVER_TABLE_LEGACY)
    assert rendered.sql.count(PUSHDOWN_AID_MARKER) == entry.expected_aids
    assert rendered.removed_placeholders == ()


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda entry: entry.key)
def test_every_registered_template_renders_for_the_narrow_store(entry) -> None:
    """Narrow keeps the canonical name and carries no text identity at all.

    ``render_river_ts_sql`` refuses rather than returns on a mis-shaped marker, a
    lost key predicate or a broken structure, so calling it IS most of the
    assertion; what is added here is the census-shaped part (the aid count) and
    the table-scoped emptiness the whole cleanup turns on.
    """
    template = entry.source()

    rendered = render_river_ts_sql(template, "narrow", entry=entry.key)

    assert PUSHDOWN_AID_MARKER not in rendered.sql
    assert RIVER_TABLE_LEGACY not in rendered.sql
    assert fact_table_text_identity_columns(rendered.sql) == set()
    # Non-vacuity: the narrow variant really is shorter by exactly the aid blocks.
    assert len(rendered.sql.split("\n")) == len(template.split("\n")) - 2 * entry.expected_aids
    if entry.params == "positional":
        # A deleted aid line takes its `%s` with it, and the caller's tuple has to
        # shrink by exactly that many. Checked against the AIDS, not against
        # `count('%s')` before minus after: that difference is computed by the same
        # deletion it is supposed to audit, so it holds for any indices the renderer
        # cares to report and cannot go red (round-2 H7-a). Guarded on the dialect,
        # not on the tuple being non-empty, so an entry that stopped reporting
        # removals is caught rather than skipped.
        assert len(rendered.removed_placeholders) == sum("%s" in aid for aid in rendered.removed_aids)
        assert rendered.removed_placeholders == POSITIONAL_INDEX_PINS[entry.key], entry.key
    else:
        assert rendered.removed_placeholders == ()
        assert rendered.removed_aids == () or all("%s" not in aid for aid in rendered.removed_aids)


#: The removed positional-placeholder indices of every positional entry, spelled
#: out. Measured at 515a3947 and pinned as literals on purpose: a formula
#: computed from the same render it checks agrees with whatever that render says
#: (round-2 H7-a), and these indices are the caller's parameter tuple — an
#: off-by-one here is a psycopg2 arity error in the migration window, or worse, a
#: silently reordered tuple that binds `valid_time` where `run_id` belonged.
POSITIONAL_INDEX_PINS: dict[str, tuple[int, ...]] = {
    "forecast_store:segment_identity_predicates": (3, 4),
    "forecast_store:latest_issue_time": (3, 4),
    "forecast_store:per_source_latest_cycles": (3, 4),
    "forecast_store:latest_analysis_issue_time": (3, 4),
    "forecast_store:analysis_segment_rows": (3, 4),
    "forecast_store:forecast_segment_rows_selected_cycles": (5, 6),
    "forecast_store:forecast_segment_rows": (3, 4),
    "forecast_store:latest_run_type_valid_time": (3, 4),
    "forecast_store:run_type_segment_rows": (3, 4),
    "parser:replace_chain_probe": (1,),
    "parser:replace_chain_window": (1,),
}


def test_every_positional_entry_has_an_index_pin() -> None:
    """No positional entry may join the register without its indices being written down."""
    assert set(POSITIONAL_INDEX_PINS) == {entry.key for entry in REGISTRY if entry.params == "positional"}


# ---------------------------------------------------------------------------
# The cross-store union whitelist, over the real register (#1980, decision 13)
#
# Two sweeps, and the split matters:
#
# * PLACEMENT is pinned by the literal table below — for every statement entry,
#   for every fact-table reference in document order, the chain kind, the
#   predicate form and the LAST CONJUNCT the store predicate must follow. Read
#   off the templates by hand and checked against them (`publisher`'s ON chain
#   ends `AND r.variable_e = 'q_down'` before `JOIN core.river_network_version`;
#   all three of `mvt`'s national reads end `AND ts.valid_time = :valid_time`
#   before their `LIMIT 1`). The previous oracle computed the expectation by
#   re-running the binder's own walk over the binder's own output, so it agreed
#   with whatever the walk did and could not go red on a splice into the wrong
#   chain (round-3 L3-1).
# * LEGALITY is partitioned by the DECLARED `union_safe`. The True partition
#   renders and asserts placement in the rendered branch; the False partition
#   asserts that `render_union_all` refuses and says why.
# ---------------------------------------------------------------------------

#: ``entry key -> per fact-table reference, in document order:
#: (chain kind, predicate form, the legacy branch's anchor conjunct, the narrow
#: branch's anchor conjunct)``.
#:
#: The two anchors differ for exactly one entry — `mvt:valid_times_any_identity`,
#: whose chain ENDS with its aid, so the narrow branch's last conjunct is the
#: enum predicate above it. Both are pinned for every entry rather than one
#: shared column, so that entry is not a special case in the test and a future
#: template whose chain ends in an aid cannot quietly re-use the wrong anchor.
UNION_PLACEMENT_PINS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "hydro_display:mvt_source_identity_probe": (
        ("where", "physical", "valid_time = :valid_time", "valid_time = :valid_time"),
    ),
    "display_coverage:refresh": (
        (
            "where",
            "exists",
            "(%(scan_display_end)s IS NULL OR rt.valid_time <= %(scan_display_end)s)",
            "(%(scan_display_end)s IS NULL OR rt.valid_time <= %(scan_display_end)s)",
        ),
    ),
    "forecast_store:latest_issue_time": (
        ("where", "alias", "h.cycle_time IS NOT NULL", "h.cycle_time IS NOT NULL"),
    ),
    "forecast_store:per_source_latest_cycles": (
        ("where", "alias", "h.cycle_time IS NOT NULL", "h.cycle_time IS NOT NULL"),
    ),
    "forecast_store:latest_analysis_issue_time": (
        ("where", "alias", "h.end_time IS NOT NULL", "h.end_time IS NOT NULL"),
    ),
    "forecast_store:analysis_segment_rows": (
        ("where", "alias", "rt.valid_time < %s", "rt.valid_time < %s"),
    ),
    "forecast_store:forecast_segment_rows_selected_cycles": (
        (
            "where",
            "alias",
            "rt.valid_time <= h.cycle_time + INTERVAL '7 days'",
            "rt.valid_time <= h.cycle_time + INTERVAL '7 days'",
        ),
    ),
    "forecast_store:forecast_segment_rows": (
        ("where", "alias", "rt.valid_time <= %s", "rt.valid_time <= %s"),
    ),
    "forecast_store:latest_run_type_valid_time": (
        ("where", "alias", "LOWER(h.run_type) = ANY(%s)", "LOWER(h.run_type) = ANY(%s)"),
    ),
    "forecast_store:run_type_segment_rows": (
        ("where", "alias", "rt.valid_time <= %s", "rt.valid_time <= %s"),
    ),
    "forecast_store:latest_product_fallback": (
        (
            "where",
            "exists",
            "(%(scan_display_end)s IS NULL OR rt.valid_time <= %(scan_display_end)s)",
            "(%(scan_display_end)s IS NULL OR rt.valid_time <= %(scan_display_end)s)",
        ),
    ),
    "forcing_copyback_backfill:discover_backfill_runs": (
        ("where", "exists", "rt.value IS NOT NULL", "rt.value IS NOT NULL"),
    ),
    "publisher:qdown_discovery": (
        ("on", "alias", "r.variable_e = 'q_down'", "r.variable_e = 'q_down'"),
    ),
    "mvt:postgis_tile_sql_hydro": (
        ("where", "exists", "ts.valid_time = :valid_time", "ts.valid_time = :valid_time"),
    ),
    "mvt:postgis_tile_sql_hydro_national": (
        ("where", "exists", "ts.valid_time = :valid_time", "ts.valid_time = :valid_time"),
        ("where", "exists", "ts.valid_time = :valid_time", "ts.valid_time = :valid_time"),
        ("where", "exists", "ts.valid_time = :valid_time", "ts.valid_time = :valid_time"),
    ),
    "mvt:valid_times_named_identity": (
        (
            "where",
            "physical",
            "variable_e = (SELECT e FROM unnest(enum_range(NULL::hydro.river_variable)) e WHERE e::text = :variable)",
            "variable_e = (SELECT e FROM unnest(enum_range(NULL::hydro.river_variable)) e WHERE e::text = :variable)",
        ),
    ),
    "mvt:valid_times_any_identity": (
        (
            "where",
            "physical",
            "variable = :variable",
            "variable_e = (SELECT e FROM unnest(enum_range(NULL::hydro.river_variable)) e WHERE e::text = :variable)",
        ),
    ),
    "parser:replace_chain_probe": (("where", "physical", "variable_e = %s", "variable_e = %s"),),
    "parser:replace_chain_window": (("where", "physical", "variable_e = %s", "variable_e = %s"),),
}

STATEMENT_ENTRIES = [entry for entry in REGISTRY if entry.kind == "statement"]
UNION_ENTRIES = [entry for entry in STATEMENT_ENTRIES if entry.params == "named"]
UNION_SAFE_ENTRIES = [entry for entry in UNION_ENTRIES if entry.union_safe]
UNION_UNSAFE_ENTRIES = [entry for entry in UNION_ENTRIES if not entry.union_safe]


def _folded(sql: str) -> str:
    """The module's own canonical fold — the pin is the ANCHOR, not the folding."""
    return river_ts_render._canonical(strip_comments(sql))


def _named_params_of(template: str) -> dict[str, object]:
    names = sorted(set(re.findall(r"%\((\w+)\)s", template)) | set(re.findall(r"(?<![:\w]):(\w+)\b", template)))
    return {name: "value" for name in names}


def test_every_statement_entry_has_a_placement_pin_per_fact_reference() -> None:
    """No statement may join the register without its placement being written down.

    Per REFERENCE, not per entry: `mvt`'s national tile SQL reads the fact table
    three times, and an entry pinned with one row would leave two reads unpinned
    — which is the shape of the original fail-open (review #1996, C1).
    """
    assert set(UNION_PLACEMENT_PINS) == {entry.key for entry in STATEMENT_ENTRIES}
    for entry in STATEMENT_ENTRIES:
        assert len(UNION_PLACEMENT_PINS[entry.key]) == entry.mentions, entry.key
        for chain_kind, predicate_form, _legacy, _narrow in UNION_PLACEMENT_PINS[entry.key]:
            assert chain_kind in {"where", "on"}, entry.key
            assert predicate_form in {"alias", "exists", "physical"}, entry.key
    assert sum(len(pins) for pins in UNION_PLACEMENT_PINS.values()) == 21


@pytest.mark.parametrize("entry", STATEMENT_ENTRIES, ids=lambda entry: entry.key)
@pytest.mark.parametrize("store", ("legacy", "narrow"))
def test_every_registered_statement_classifies_exactly_as_pinned(entry, store) -> None:
    """The whitelist's decisions, per reference, against the hand-written table.

    Over EVERY statement entry and both stores, not only the union-safe ones: the
    classification is what decides where a predicate may go, and a template whose
    chain end or predicate form moved is a defect whether or not #1980 also
    declares its statement-level union legal.
    """
    rendered = render_river_ts_sql(entry.source(), store, entry=entry.key)

    plan = store_binding_plan(rendered.sql, entry=entry.key)

    pins = UNION_PLACEMENT_PINS[entry.key]
    assert len(plan) == len(pins) == fact_table_name_occurrences(rendered.sql), entry.key
    for scope, (chain_kind, predicate_form, legacy_anchor, narrow_anchor) in zip(plan, pins, strict=True):
        assert scope.chain_kind == chain_kind, entry.key
        assert scope.predicate_form == predicate_form, entry.key
        assert scope.anchor == (legacy_anchor if store == "legacy" else narrow_anchor), entry.key
        # The chain kind is DICTATED by the reference form, never chosen.
        assert scope.chain_kind == ("on" if scope.reference_form == "JOIN" else "where"), entry.key
        # The alias form is only legal with a correlation to name.
        assert (scope.hydro_run_alias is not None) == (predicate_form == "alias"), entry.key
        assert (scope.fact_alias is None) == (predicate_form == "physical"), entry.key


def test_the_nine_correlated_join_form_entries_are_exactly_the_alias_form_ones() -> None:
    """Nine entries earn the cheap alias form, and each one really is correlated.

    Measured, and pinned as a COUNT so a template that stopped correlating
    `hydro_run` on `run_key` — and would therefore silently route by the wrong
    run — moves this number. The correlation is asserted from the template TEXT,
    not from the binder's own verdict: `<h>.run_key = <a>.run_key` (either side)
    has to be there.
    """
    alias_form: list[tuple[str, str, str]] = []
    for entry in STATEMENT_ENTRIES:
        template = entry.source()
        for scope in store_binding_plan(template, entry=entry.key):
            if scope.predicate_form == "alias":
                alias_form.append((entry.key, scope.hydro_run_alias, scope.fact_alias))

    assert len(alias_form) == 9, [key for key, _h, _a in alias_form]
    assert {key for key, _h, _a in alias_form} == {
        *(f"forecast_store:{label}" for label in FORECAST_STORE_SEGMENT_BLOCKS),
        "publisher:qdown_discovery",
    }
    for key, hydro_run_alias, fact_alias in alias_form:
        text = strip_comments(entry_by_key(key).source())
        correlations = (
            rf"\b{hydro_run_alias}\.run_key\s*=\s*{fact_alias}\.run_key\b",
            rf"\b{fact_alias}\.run_key\s*=\s*{hydro_run_alias}\.run_key\b",
        )
        assert any(re.search(pattern, text, re.IGNORECASE) for pattern in correlations), key
        assert re.search(rf"\b(?:FROM|JOIN)\s+hydro\.hydro_run\s+(?:AS\s+)?{hydro_run_alias}\b", text), key


def test_the_union_partition_is_pinned() -> None:
    """Nine named statements, two union-safe, seven declared unsafe with a reason.

    Pinned as numbers because the partition is a JUDGEMENT about each consumer,
    and moving an entry across it must be a deliberate edit with a reason
    attached, not a side effect of an unrelated change.
    """
    assert len(UNION_ENTRIES) == 9
    assert len(UNION_SAFE_ENTRIES) == 2
    assert len(UNION_UNSAFE_ENTRIES) == 7
    assert {entry.key for entry in UNION_SAFE_ENTRIES} == {
        "forcing_copyback_backfill:discover_backfill_runs",
        "publisher:qdown_discovery",
    }


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda entry: entry.key)
def test_a_union_unsafe_entry_carries_a_reason_and_a_safe_one_does_not(entry) -> None:
    """The declared property's own invariant: the reason is present iff it is needed."""
    assert (entry.union_unsafe_reason is None) == entry.union_safe, entry.key
    if not entry.union_safe:
        assert len(entry.union_unsafe_reason.strip()) > 40, entry.key


@pytest.mark.parametrize("entry", UNION_SAFE_ENTRIES, ids=lambda entry: entry.key)
def test_every_union_safe_statement_binds_where_the_pin_says(entry) -> None:
    """The False-positive control: the whitelist admits every legal entry, and places it.

    Placement is asserted in the RENDERED BRANCH against the pinned anchor: the
    store predicate must follow the conjunct the table says it follows. Nothing
    here re-derives that conjunct from the binder.
    """
    template = entry.source()
    params = _named_params_of(template)
    pins = UNION_PLACEMENT_PINS[entry.key]

    rendered = render_union_all(
        template,
        ("legacy", "narrow"),
        params,
        entry=entry.key,
        union_safe=entry.union_safe,
        union_unsafe_reason=entry.union_unsafe_reason,
    )

    assert rendered.params == params
    assert len(rendered.branch_sql) == len(rendered.branches) == 2
    for store, branch, forms in zip(("legacy", "narrow"), rendered.branch_sql, rendered.branches):
        # Bindings per branch == the branch's own fact-reference count.
        assert len(forms) == fact_table_name_occurrences(branch) == len(pins), entry.key
        assert branch.count(f"timeseries_store = '{store}'") == len(pins), entry.key
        assert_structurally_intact(branch, f"{entry.key} [{store}]", allow_markers=store == "legacy")
        folded = _folded(branch)
        for scope, (chain_kind, predicate_form, legacy_anchor, narrow_anchor) in zip(
            store_binding_plan(branch, entry=entry.key), pins, strict=True
        ):
            anchor = legacy_anchor if store == "legacy" else narrow_anchor
            predicate = store_predicate(scope, store)
            assert scope.chain_kind == chain_kind and scope.predicate_form == predicate_form, entry.key
            assert f"{anchor} AND {river_ts_render._canonical(predicate)}" in folded, (entry.key, store, anchor)


@pytest.mark.parametrize("entry", UNION_UNSAFE_ENTRIES, ids=lambda entry: entry.key)
def test_every_union_unsafe_statement_is_refused_with_its_declared_reason(entry) -> None:
    """The other half of the partition: refused, and the message says whose reason it is.

    A silent bind here is the round-3 P1: `display_coverage:refresh` — a `WITH …
    INSERT … RETURNING` — was rendered as a UNION ALL operand and the sweep
    passed, because the sweep only asked where the predicates landed.
    """
    with pytest.raises(river_ts_render.RiverTemplateError) as raised:
        render_union_all(
            entry.source(),
            ("legacy", "narrow"),
            _named_params_of(entry.source()),
            entry=entry.key,
            union_safe=entry.union_safe,
            union_unsafe_reason=entry.union_unsafe_reason,
        )

    assert entry.key in str(raised.value)
    assert entry.union_unsafe_reason in str(raised.value)


def test_the_data_modifying_registry_entry_is_refused_even_if_declared_safe() -> None:
    """Condition 1 stands on its own, not on the declaration (fixture probe O).

    `display_coverage:refresh` is refused twice over — by its declaration AND by
    being a `WITH … INSERT INTO … ON CONFLICT … RETURNING` statement. Both
    readings of decision 13 are pinned, so re-declaring it safe by accident does
    not make it a legal UNION operand.
    """
    entry = entry_by_key("display_coverage:refresh")
    template = entry.source()

    with pytest.raises(river_ts_render.RiverTemplateError, match="read statements only"):
        render_union_all(template, ("legacy", "narrow"), _named_params_of(template), entry=entry.key)


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda entry: entry.key)
def test_every_registered_template_is_counted_the_same_way_twice(entry) -> None:
    """The structural walk and the name counter must agree, entry by entry.

    The binder refuses when they disagree, so this sweep is what says the refusal
    is not silently rejecting the whole register: every registered template names
    the fact table in exactly the forms the walk models (round-2 H3).
    """
    template = entry.source()

    assert fact_table_name_occurrences(template) == fact_table_attribution(template).reference_count, entry.key


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda entry: entry.key)
def test_every_registered_templates_aid_count_matches_its_marker_count(entry) -> None:
    """1:1, which is the invariant the whole line-deletion scheme rests on."""
    template = entry.source()

    assert template.count(PUSHDOWN_AID_MARKER) == entry.expected_aids, entry.key
    assert template.count("remove with #1342") == entry.expected_aids, (
        f"{entry.key}: a non-verbatim aid marker is present"
    )


def test_the_rendered_aid_total_reconciles_with_the_per_file_census() -> None:
    """Rendered 58, in source 34 — and the difference is stated, not waved at.

    The two numbers count different things and both are load-bearing, so they are
    reconciled here rather than left to look like a contradiction:

    * **34** is the ``grep -rn "remove with #1342"`` total over the seven reader
      SOURCE files (fixture "Measured baseline"), pinned per file in each file's
      owning oracle. That is the number #1342 deletes.
    * **58** is the total over RENDERED templates, which is larger for exactly one
      reason: ``forecast_store._SEGMENT_IDENTITY_PREDICATE_SQL`` carries three
      aids in the source once and is embedded by all eight segment blocks, so
      those three aids are rendered nine times (once as the fragment entry, once
      inside each block) and appear 8 × 3 = 24 times more than they are written.

    Pinned as an identity rather than as two independent constants: if a block
    stopped embedding the fragment — the change that would silently drop its
    segmentby pruning — the arithmetic breaks here even though both totals could
    be individually re-pinned to something self-consistent.
    """
    rendered_total = sum(entry.expected_aids for entry in REGISTRY)
    fragment = entry_by_key("forecast_store:segment_identity_predicates")
    embedding_blocks = len(FORECAST_STORE_SEGMENT_BLOCKS)

    assert rendered_total == 58
    assert rendered_total - embedding_blocks * fragment.expected_aids == 34


def test_the_sanctioned_vocabulary_is_the_shared_one_not_a_private_copy() -> None:
    """The move must not have left a second definition behind (#1980, decision 2)."""
    assert SANCTIONED_TEXT_PUSHDOWN_COLUMNS is river_ts_render.SANCTIONED_TEXT_PUSHDOWN_COLUMNS
    assert FORBIDDEN_TEXT_FACT_COLUMNS is river_ts_render.FORBIDDEN_TEXT_FACT_COLUMNS
    assert LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS is river_ts_render.LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS
    assert TEXT_IDENTITY_COLUMNS is river_ts_render.TEXT_IDENTITY_COLUMNS
    assert TEXT_AID_COUNTERPARTS is river_ts_render.TEXT_AID_COUNTERPARTS
    assert text_fact_columns is river_ts_render.text_fact_columns
    assert outer_predicates is river_ts_render.outer_predicates
    source = (REPO_ROOT / "tests" / "test_sql_shape_helpers.py").read_text(encoding="utf-8")
    for name in (
        "SANCTIONED_TEXT_PUSHDOWN_COLUMNS",
        "FORBIDDEN_TEXT_FACT_COLUMNS",
        "LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS",
        "TEXT_IDENTITY_COLUMNS",
        "TEXT_AID_COUNTERPARTS",
    ):
        assert f"\n{name}" not in source, f"{name} was re-declared here instead of imported"
