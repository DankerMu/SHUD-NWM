"""Unit contract for ``packages/common/river_ts_render.py`` (#1980, task 1.2).

The renderer is the seam every later issue of epic #1979 depends on (I2–I5 wire
the readers to it, I7 flips the store, I9 deletes the legacy half), and it works
by DELETING LINES from production SQL. Two failure modes matter and neither is
visible downstream:

* it deletes one line too many — a key predicate goes, the narrow statement
  silently returns more rows than the legacy one;
* it deletes one line too few — a text predicate survives onto a table that has
  no such column, and the read fails at runtime, in the migration window.

So the tests here are written against synthetic templates that isolate one shape
each, and the fail-closed cases are as load-bearing as the happy ones: a renderer
that quietly does its best with a mis-shaped marker is worse than one that
refuses, because #1980's whole argument is that the layout is 1:1 and mechanical.

The registered production templates are rendered in both variants by the shape
oracles (``tests/test_sql_shape_helpers.py``), not here: this file owns the
contract, that one owns the coverage.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from packages.common.river_ts_render import (
    _SUBQUERY_START,
    _WHITESPACE,
    PUSHDOWN_AID_MARKER,
    RIVER_TABLE,
    RIVER_TABLE_LEGACY,
    SANCTIONED_TEXT_PUSHDOWN_COLUMNS,
    TEXT_AID_COUNTERPARTS,
    TEXT_IDENTITY_COLUMNS,
    RiverTemplateError,
    _assert_key_predicates_retained,
    _assert_no_fact_text_identity,
    _blank_non_code,
    _in_comparison_value_position,
    _lexical_subset_violation,
    _scan_quoted,
    _strip_aids,
    aid_conjunct,
    assert_structurally_intact,
    fact_table_attribution,
    fact_table_name_occurrences,
    fact_table_text_identity_columns,
    non_code_spans,
    outer_predicates,
    render_river_ts_sql,
    sql_chains,
    strip_all_subqueries,
    strip_comments,
    strip_scalar_subqueries,
    text_fact_columns,
)
from tests.river_ts_template_registry import REGISTRY

MARKER = PUSHDOWN_AID_MARKER

NAMED_TEMPLATE = f"""
    SELECT ts.value
    FROM hydro.river_timeseries ts
    WHERE ts.run_key = (
              SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id
          )
      {MARKER}
      AND ts.run_id = :run_id
      AND ts.valid_time = :valid_time
"""

POSITIONAL_TEMPLATE = f"""
    SELECT rt.value
    FROM hydro.river_timeseries rt
    WHERE rt.run_key = %s
      {MARKER}
      AND rt.run_id = %s
      AND rt.river_network_version_key = %s
      {MARKER}
      AND rt.river_network_version_id = %s
      AND rt.valid_time >= %s
"""


# ---------------------------------------------------------------------------
# legacy: the table name and nothing else
# ---------------------------------------------------------------------------


def test_the_legacy_variant_renames_the_table_and_changes_nothing_else() -> None:
    rendered = render_river_ts_sql(NAMED_TEMPLATE, "legacy", entry="named")

    assert rendered.sql == NAMED_TEMPLATE.replace(RIVER_TABLE, RIVER_TABLE_LEGACY)
    assert rendered.removed_placeholders == ()


def test_the_legacy_variant_keeps_every_aid_and_every_marker() -> None:
    """The legacy table still carries 000047's text-column compression layout.

    Dropping an aid from the legacy branch is not a cleanup, it is the measured
    compressed-chunk collapse (#1341's 598,280-cost full decompression) applied
    to exactly the rows that are still in the old table.
    """
    rendered = render_river_ts_sql(POSITIONAL_TEMPLATE, "legacy", entry="positional")

    assert rendered.sql.count(MARKER) == 2
    assert "AND rt.run_id = %s" in rendered.sql
    assert "AND rt.river_network_version_id = %s" in rendered.sql


def test_rendering_legacy_is_idempotent_on_an_already_legacy_name() -> None:
    """``\\b`` after the canonical name refuses to match before ``_legacy``.

    Without it a double render produces ``hydro.river_timeseries_legacy_legacy``,
    a table that does not exist, and only at execute time.
    """
    once = render_river_ts_sql(NAMED_TEMPLATE, "legacy", entry="named").sql
    twice = render_river_ts_sql(once, "legacy", entry="named").sql

    assert twice == once
    assert "_legacy_legacy" not in twice


@pytest.mark.parametrize("spelling", ["HYDRO.RIVER_TIMESERIES", "Hydro.River_Timeseries"])
def test_the_legacy_rename_reads_the_table_name_case_insensitively(spelling: str) -> None:
    """SQL identifiers are case-insensitive unquoted, and the rename must be too.

    Every other fact-name pattern in the module carries ``re.IGNORECASE``, so an
    upper-cased read passed both counters and the equality guard, and then the
    LEGACY variant came back naming the CANONICAL table — the narrow table,
    which holds none of the legacy rows (#2018 round-2 F2). One case policy per
    module is the point: a pattern that disagrees with its siblings about what
    the table's name is, is a hole by construction.
    """
    template = f"SELECT rt.value FROM {spelling} rt WHERE rt.run_key = :run_key"

    rendered = render_river_ts_sql(template, "legacy", entry="upper-case")

    assert RIVER_TABLE_LEGACY in rendered.sql
    assert spelling not in rendered.sql


def test_the_case_insensitive_rename_is_still_idempotent_on_an_upper_cased_legacy_name() -> None:
    """The ``\\b`` control for the widened case policy: no ``_legacy_legacy``.

    ``re.IGNORECASE`` widens what the rename can reach, so the guard that keeps
    a second render from producing ``hydro.river_timeseries_legacy_legacy`` is
    asserted on the upper-cased spelling too.
    """
    template = "SELECT rt.value FROM hydro.RIVER_TIMESERIES_LEGACY rt WHERE rt.run_key = :run_key"

    rendered = render_river_ts_sql(template, "legacy", entry="upper-case legacy")

    assert rendered.sql == template
    assert rendered.sql.lower().count("_legacy") == 1


def test_a_fragment_has_no_table_name_so_the_rename_is_a_no_op() -> None:
    fragment = f"rt.river_segment_key = %s\n  {MARKER}\n  AND rt.river_segment_id = %s"

    assert render_river_ts_sql(fragment, "legacy", entry="fragment").sql == fragment


# ---------------------------------------------------------------------------
# narrow: the marker line and the single conjunct beneath it
# ---------------------------------------------------------------------------


def test_the_narrow_variant_removes_the_marker_line_and_its_aid_line() -> None:
    rendered = render_river_ts_sql(NAMED_TEMPLATE, "narrow", entry="named")

    assert RIVER_TABLE in rendered.sql
    assert RIVER_TABLE_LEGACY not in rendered.sql
    assert MARKER not in rendered.sql
    assert "ts.run_id" not in rendered.sql.replace("WHERE run_id = :run_id", "")
    # The key predicate the aid was redundant to, and every unrelated conjunct.
    assert "ts.run_key = (" in rendered.sql
    assert "AND ts.valid_time = :valid_time" in rendered.sql


def test_the_narrow_variant_removes_consecutive_marker_aid_blocks() -> None:
    """mvt's ``:661`` / ``:793`` / ``:840`` runs are three and four aids back to back.

    A loop that advanced by one line after a deletion would treat the second
    marker's aid as the first marker's aid; a loop that resynchronised on blank
    lines would stop at the first run. Both shipped statements with text
    predicates left over, so the run is pinned explicitly.
    """
    template = "\n".join(
        [
            "    SELECT 1",
            "    FROM hydro.river_timeseries ts",
            "    WHERE ts.run_key = lr.run_key",
            f"      {MARKER}",
            "      AND ts.run_id = lr.run_id",
            f"      {MARKER}",
            "      AND ts.river_network_version_id = lr.river_network_version_id",
            f"      {MARKER}",
            "      AND ts.river_segment_id = seg.river_segment_id",
            f"      {MARKER}",
            "      AND ts.variable = :variable",
            "      AND ts.variable_e = :variable_e",
        ]
    )

    rendered = render_river_ts_sql(template, "narrow", entry="lateral probe")

    assert MARKER not in rendered.sql
    assert fact_table_text_identity_columns(rendered.sql) == set()
    assert "WHERE ts.run_key = lr.run_key" in rendered.sql
    assert "AND ts.variable_e = :variable_e" in rendered.sql


def test_a_zero_marker_template_renders_narrow_as_itself() -> None:
    template = "SELECT 1 FROM hydro.river_timeseries rt WHERE rt.run_key = %s"

    rendered = render_river_ts_sql(template, "narrow", entry="aid-free")

    assert rendered.sql == template
    assert rendered.removed_placeholders == ()


def test_an_on_chain_aid_is_removed_like_any_other() -> None:
    """publisher's ``:2665`` aid lives in a ``JOIN … ON`` chain, not a ``WHERE``.

    The rule is per line, not per keyword, so an ON-chain aid must come out with
    the join intact — and the structural check must not read the shortened ON
    chain as a broken one.
    """
    template = f"""
        SELECT h.run_id
        FROM hydro.hydro_run h
        JOIN hydro.river_timeseries r
          ON r.run_key = h.run_key
         {MARKER}
         AND r.variable = 'q_down'
         AND r.variable_e = 'q_down'
        WHERE h.status = 'published'
    """

    rendered = render_river_ts_sql(template, "narrow", entry="publisher")

    assert "ON r.run_key = h.run_key" in rendered.sql
    assert "AND r.variable_e = 'q_down'" in rendered.sql
    assert "r.variable = 'q_down'" not in rendered.sql
    assert "WHERE h.status = 'published'" in rendered.sql


def test_an_aid_ending_in_a_trailing_and_inside_an_or_disjunct_is_removed() -> None:
    """The rewritten guard shape (design D5): ``OR (`` / marker / aid ``AND`` / key.

    The aid line carries the ``AND``, so deleting it leaves the disjunct holding
    only its key predicate — no dangling operator, same truth table.
    """
    template = (
        "SELECT 1 FROM hydro.river_timeseries rt\n"
        "WHERE (%(scan_run_id)s IS NULL\n"
        "       OR (\n"
        f"           {MARKER}\n"
        "           rt.run_id = %(scan_run_id)s AND\n"
        "           rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(scan_run_id)s)))\n"
    )

    rendered = render_river_ts_sql(template, "narrow", entry="scan guard")

    assert "rt.run_id = %(scan_run_id)s" not in rendered.sql
    assert "rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(scan_run_id)s)" in rendered.sql
    assert "OR (\n" in rendered.sql


# ---------------------------------------------------------------------------
# fail-closed
# ---------------------------------------------------------------------------


def test_a_marker_above_a_keyword_line_is_refused_by_name() -> None:
    """The pre-#1980 shape at ``hydro_display:773`` and ``mvt:511/1496/1524``.

    The aid was the first conjunct ON the ``WHERE`` line, so deleting the line
    under the marker would delete the ``WHERE`` itself. This refusal is what
    forces task 1.1's normalisation to actually happen rather than being assumed.
    """
    template = f"""
        SELECT 1
        FROM hydro.river_timeseries
        {MARKER}
        WHERE run_id = :run_id
          AND run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id)
    """

    with pytest.raises(RiverTemplateError) as excinfo:
        render_river_ts_sql(template, "narrow", entry="mvt:valid_times_named_identity")

    assert "mvt:valid_times_named_identity" in str(excinfo.value)
    assert "not exactly one aid conjunct" in str(excinfo.value)


def test_a_marker_above_a_two_conjunct_line_is_refused() -> None:
    template = f"""
        SELECT 1 FROM hydro.river_timeseries rt
        WHERE rt.run_key = %s
          {MARKER}
          AND rt.run_id = %s AND rt.variable = 'q_down'
    """

    with pytest.raises(RiverTemplateError, match="not exactly one aid conjunct"):
        render_river_ts_sql(template, "narrow", entry="two conjuncts")


def test_a_marker_above_a_non_aid_predicate_is_refused() -> None:
    """A marker over the KEY predicate would delete the row-selection authority."""
    template = f"""
        SELECT 1 FROM hydro.river_timeseries rt
        WHERE rt.valid_time = %s
          {MARKER}
          AND rt.run_key = %s
    """

    with pytest.raises(RiverTemplateError, match="not exactly one aid conjunct"):
        render_river_ts_sql(template, "narrow", entry="key predicate under marker")


def test_a_marker_on_the_last_line_is_refused() -> None:
    template = f"SELECT 1 FROM hydro.river_timeseries rt WHERE rt.run_key = %s\n  {MARKER}"

    with pytest.raises(RiverTemplateError, match="last line"):
        render_river_ts_sql(template, "narrow", entry="trailing marker")


def test_a_non_verbatim_marker_is_refused_by_name() -> None:
    """mvt's pre-#1980 ``-- aids, remove with #1342`` covered FOUR conjuncts.

    Matching on the issue tag rather than only on the verbatim string is what
    makes this red instead of invisible: a renderer that only recognised the
    verbatim marker would skip the block and leave four text predicates in a
    narrow statement.
    """
    template = """
        SELECT 1 FROM hydro.river_timeseries ts
        WHERE ts.run_key = lr.run_key
          -- transitional compressed-chunk pushdown aids, remove with #1342
          AND ts.run_id = lr.run_id
          AND ts.river_network_version_id = lr.river_network_version_id
    """

    with pytest.raises(RiverTemplateError) as excinfo:
        render_river_ts_sql(template, "narrow", entry="mvt:national")

    assert "NON-VERBATIM" in str(excinfo.value)
    assert "mvt:national" in str(excinfo.value)


def test_a_marker_sharing_a_line_with_its_aid_is_refused() -> None:
    """Inline markers were accepted before #1980 and are not deletable by line."""
    template = f"""
        SELECT 1 FROM hydro.river_timeseries rt
        WHERE rt.run_key = %s
          AND rt.run_id = %s {MARKER}
    """

    with pytest.raises(RiverTemplateError, match="NON-VERBATIM"):
        render_river_ts_sql(template, "narrow", entry="inline marker")


def test_an_unknown_store_is_refused() -> None:
    with pytest.raises(RiverTemplateError, match="unknown timeseries store"):
        render_river_ts_sql(NAMED_TEMPLATE, "sharded", entry="named")


def test_a_deletion_that_would_orphan_a_key_predicate_is_refused() -> None:
    """The renderer's own safety net, independent of the marker grammar.

    Constructed so the aid line's removal also takes the conjunct the statement
    selects rows by: the ``AND`` belongs to the NEXT line, so removing the aid
    line leaves ``WHERE`` immediately followed by ``AND``. A renderer that only
    checked "did I remove a text column" would ship it.
    """
    template = f"""
        SELECT 1 FROM hydro.river_timeseries rt
        WHERE
          {MARKER}
          rt.run_id = %s
          AND rt.run_key = %s
    """

    with pytest.raises(RiverTemplateError, match="WHERE with no predicate"):
        render_river_ts_sql(template, "narrow", entry="orphaned WHERE")


# ---------------------------------------------------------------------------
# structural check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "sql"),
    [
        ("unbalanced open", "SELECT 1 FROM t WHERE (a = 1"),
        ("unbalanced close", "SELECT 1 FROM t WHERE a = 1)"),
        ("WHERE AND", "SELECT 1 FROM t WHERE AND a = 1"),
        ("FROM AND", "SELECT 1 FROM AND a = 1"),
        ("ON AND", "SELECT 1 FROM t JOIN u ON AND t.a = u.a"),
        ("dangling AND", "SELECT 1 FROM t WHERE a = 1 AND"),
        ("dangling AND before bracket", "SELECT 1 FROM t WHERE (a = 1 AND)"),
        ("empty OR bracket", "SELECT 1 FROM t WHERE (a = 1 OR ())"),
        ("empty WHERE", "SELECT 1 FROM t WHERE"),
    ],
)
def test_the_structural_check_rejects_every_shape_a_line_deletion_can_produce(label: str, sql: str) -> None:
    with pytest.raises(RiverTemplateError):
        assert_structurally_intact(sql, label)


def test_the_structural_check_accepts_a_real_statement() -> None:
    """Non-vacuity: the check must not be red on everything."""
    assert_structurally_intact(
        "SELECT 1 FROM hydro.river_timeseries rt "
        "WHERE rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %s) "
        "AND (rt.valid_time IS NULL OR rt.valid_time >= %s)",
        "well formed",
    )


def test_the_structural_check_refuses_a_surviving_marker_but_not_on_the_legacy_variant() -> None:
    sql = f"SELECT 1 FROM hydro.river_timeseries rt\n{MARKER}\nWHERE rt.run_key = %s AND rt.run_id = %s"

    assert_structurally_intact(sql, "legacy", allow_markers=True)
    with pytest.raises(RiverTemplateError, match="marker survived"):
        assert_structurally_intact(sql, "narrow")


def test_a_parenthesis_inside_a_string_literal_does_not_unbalance_the_check() -> None:
    assert_structurally_intact(
        "SELECT ') not a bracket' FROM hydro.river_timeseries rt WHERE rt.run_key = %s", "quoted"
    )


# ---------------------------------------------------------------------------
# table-scoped attribution
# ---------------------------------------------------------------------------


def test_an_authority_subquery_on_run_id_is_not_a_fact_table_predicate() -> None:
    """display_coverage's legitimate ``hydro_run WHERE run_id`` resolution.

    A naive grep for ``run_id`` calls this a text identity predicate and is
    wrong: the column belongs to ``hydro.hydro_run``, which keeps it after #1342.
    """
    sql = (
        "SELECT 1 FROM hydro.river_timeseries rt "
        "WHERE rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(scan_run_id)s)"
    )

    assert fact_table_text_identity_columns(sql) == set()


def test_an_unaliased_single_table_statement_is_still_attributed_to_the_fact_table() -> None:
    """``hydro_display:773`` and ``mvt:1496/:1524`` give the fact table no alias.

    An alias-only check answers "no text identity column here" on a statement
    whose every predicate is one, so the unaliased case is scanned positionally.
    """
    sql = "SELECT DISTINCT valid_time FROM hydro.river_timeseries WHERE run_id = :run_id"

    assert fact_table_attribution(sql).has_unaliased_reference is True
    assert fact_table_text_identity_columns(sql) == {"run_id"}


def test_an_alias_qualified_text_predicate_is_attributed() -> None:
    sql = "SELECT 1 FROM hydro.river_timeseries ts WHERE ts.river_segment_id = seg.river_segment_id"

    assert fact_table_attribution(sql).aliases == frozenset({"ts"})
    assert fact_table_text_identity_columns(sql) == {"river_segment_id"}


def test_another_relations_text_column_is_not_attributed_to_the_fact_table() -> None:
    """The false-positive half: ``rs.river_segment_id`` is the authority's column."""
    sql = (
        "SELECT 1 FROM hydro.river_timeseries ts JOIN core.river_segment rs "
        "ON rs.river_segment_key = ts.river_segment_key WHERE rs.river_segment_id = :segment_id"
    )

    assert fact_table_text_identity_columns(sql) == set()


def test_the_legacy_table_name_is_attributed_to_the_fact_table_too() -> None:
    sql = "SELECT 1 FROM hydro.river_timeseries_legacy rt WHERE rt.run_id = %s"

    assert fact_table_text_identity_columns(sql) == {"run_id"}


@pytest.mark.parametrize(
    ("label", "reference"),
    [
        ("as_quoted", 'hydro.river_timeseries AS "r"'),
        ("bare_quoted", 'hydro.river_timeseries "r"'),
        ("legacy_as_quoted", 'hydro.river_timeseries_legacy AS "r"'),
        # A double quote opens an identifier with no whitespace in front of it,
        # so these are the SAME unmodelled form and used to walk straight past
        # the `\s+` the guard was first written with (review #2018, B/P2-1).
        ("no_ws_bare_quoted", 'hydro.river_timeseries"r"'),
        ("no_ws_as_quoted", 'hydro.river_timeseries AS"r"'),
        ("legacy_no_ws_quoted", 'hydro.river_timeseries_legacy"r"'),
    ],
)
def test_a_double_quoted_fact_alias_is_refused_instead_of_reported_clean(label: str, reference: str) -> None:
    r"""A form the alias walk cannot read must not answer "no text identity here".

    `_FACT_REFERENCE` captures bare identifiers only. `AS "r"` used to backtrack
    onto the word `AS` and attribute the predicates to a table of that name;
    a bare `"r"` reaches the walk as no alias at all and the unaliased fallback
    then misses `"r".run_id`, because `(?<![.\w])run_id` does not fire behind a
    closing quote. Both answered `set()` for a statement that predicates on
    `run_id`, and the narrow render therefore shipped it.
    """
    sql = f'SELECT "r".value FROM {reference} WHERE "r".run_id = :run_id'

    for store in ("legacy", "narrow"):
        with pytest.raises(RiverTemplateError, match=f"{label}: unmodelled fact-table reference form"):
            render_river_ts_sql(sql, store, entry=label)
    with pytest.raises(RiverTemplateError, match="unmodelled fact-table reference form"):
        fact_table_text_identity_columns(sql, entry=label)


@pytest.mark.parametrize(
    ("label", "reference"),
    [
        ("as_alias", "hydro.river_timeseries AS r"),
        ("bare_alias", "hydro.river_timeseries r"),
        ("column_alias_list", "hydro.river_timeseries r(run_key, value)"),
        ("legacy_bare_alias", "hydro.river_timeseries_legacy r"),
    ],
)
def test_an_unquoted_fact_alias_survives_the_no_whitespace_widening(label: str, reference: str) -> None:
    r"""Non-vacuity for the `\s*` widening: only the QUOTED spellings are refused.

    Relaxing both gaps to `\s*` widens what the refusal can reach, so the legal
    spellings — `AS r`, a bare `r`, a column alias list `r(a, b)`, the legacy
    name — are asserted to still be read as ordinary aliased reads. A guard that
    refused these would refuse the registry.
    """
    sql = f"SELECT r.value FROM {reference} WHERE r.run_id = :run_id"

    assert fact_table_text_identity_columns(sql, entry=label) == {"run_id"}


def test_an_unquoted_as_alias_is_the_alias_and_not_the_word_as() -> None:
    """The other half of the same regex change: `AS` is a noise word, never a name."""
    sql = "SELECT r.value FROM hydro.river_timeseries AS r WHERE r.run_id = :run_id"

    assert fact_table_attribution(sql).aliases == frozenset({"r"})
    assert fact_table_attribution(sql).has_unaliased_reference is False
    assert fact_table_text_identity_columns(sql) == {"run_id"}


def test_an_unquoted_alias_still_renders_for_both_stores() -> None:
    """Non-vacuity: the refusal must not have swallowed the ordinary aliased form."""
    template = f"""
        SELECT r.value
        FROM hydro.river_timeseries AS r
        WHERE r.run_key = :run_key
          {MARKER}
          AND r.run_id = :run_id
    """

    legacy = render_river_ts_sql(template, "legacy", entry="bare-alias")
    narrow = render_river_ts_sql(template, "narrow", entry="bare-alias")

    assert "FROM hydro.river_timeseries_legacy AS r" in legacy.sql
    assert "AND r.run_id = :run_id" in legacy.sql
    assert "run_id" not in narrow.sql


@pytest.mark.parametrize(
    ("label", "sql", "occurrences"),
    [
        (
            "fully_quoted",
            'SELECT "r".value FROM "hydro"."river_timeseries" r WHERE r.run_id = :run_id',
            1,
        ),
        (
            "half_quoted",
            'SELECT r.value FROM hydro."river_timeseries" r WHERE r.run_id = :run_id',
            1,
        ),
        (
            "from_only",
            "SELECT r.value FROM ONLY hydro.river_timeseries r WHERE r.run_id = :run_id",
            1,
        ),
        (
            "comma_join",
            "SELECT a.value FROM hydro.river_timeseries a, hydro.river_timeseries b "
            "WHERE a.run_id = :run_id AND b.run_key = a.run_key",
            2,
        ),
        (
            "in_table",
            "SELECT 1 FROM hydro.river_timeseries r "
            "WHERE r.run_key IN (TABLE hydro.river_timeseries) AND r.run_id = :run_id",
            2,
        ),
        # Round 2 (#2018 E/P2-2, lane-1 F1). Every one of these RENDERED at
        # b397f8a4 — the schema-prefix enumeration the counter was built on had
        # no entry for a space, a newline, a comment, a quoted schema, no schema
        # at all, or a different one — and the narrow variant shipped `run_id`.
        # They are here as the CLASS, not as six more entries: the counter now
        # counts the table's bare NAME as a whole token, so none of them can be
        # spelled without being counted.
        (
            "spaced_dot",
            "SELECT r.value FROM hydro . river_timeseries r WHERE r.run_id = :run_id",
            1,
        ),
        (
            "newline_dot",
            "SELECT r.value FROM hydro.\nriver_timeseries r WHERE r.run_id = :run_id",
            1,
        ),
        (
            "comment_dot",
            "SELECT r.value FROM hydro./*c*/river_timeseries r WHERE r.run_id = :run_id",
            1,
        ),
        (
            "quoted_schema",
            'SELECT r.value FROM "hydro".river_timeseries r WHERE r.run_id = :run_id',
            1,
        ),
        (
            "quoted_schema_quoted_alias",
            'SELECT "r".value FROM "hydro".river_timeseries "r" WHERE "r".run_id = :run_id',
            1,
        ),
        # The search_path spelling: no schema at all. The walk models the
        # canonical name only, so this is unmodelled — and refusing it is the
        # fail-closed answer, because whether it IS the fact table depends on a
        # session setting this module cannot see.
        (
            "unqualified",
            "SELECT rt.value FROM river_timeseries rt WHERE rt.run_id = :run_id",
            1,
        ),
        (
            "unqualified_upper",
            "SELECT rt.value FROM RIVER_TIMESERIES rt WHERE rt.run_id = :run_id",
            1,
        ),
        # Accepted over-matches. `otherhydro.river_timeseries` and
        # `"River_Timeseries"` (a DIFFERENT table in PostgreSQL: a quoted
        # identifier is case-sensitive) are refused as unmodelled rather than
        # rendered, which is the fail-closed direction — the counter is
        # permissive on purpose and the walk decides what is modelled.
        (
            "other_schema",
            "SELECT x.value FROM otherhydro.river_timeseries x WHERE x.run_id = :run_id",
            1,
        ),
        (
            "quoted_upper",
            'SELECT r.value FROM hydro."River_Timeseries" r WHERE r.run_id = :run_id',
            1,
        ),
        # Round 3 (#2018 H2b). The counter counts the NAME, so any identifier
        # EQUAL to it is a mention — a CTE and a column alias named like the
        # fact table are both counted twice (their definition and, for the CTE,
        # its use) against a walk that models nothing or one read. Refused, which
        # is the fail-closed direction and the answer a template author gets:
        # name the correlation or the output column something else. Recorded
        # here as accepted over-matches rather than left for the next round to
        # rediscover from a message about "2 time(s)".
        (
            "cte_named_fact",
            "WITH river_timeseries AS (SELECT 1 AS x) SELECT x FROM river_timeseries",
            2,
        ),
        (
            "column_alias_named_fact",
            'SELECT rt.value AS "river_timeseries" FROM hydro.river_timeseries rt WHERE rt.run_id = :run_id',
            2,
        ),
    ],
)
def test_a_reference_the_from_join_walk_cannot_count_is_refused(label: str, sql: str, occurrences: int) -> None:
    """The independent counter's disagreement is now ENFORCED, not merely available.

    ``fact_table_name_occurrences`` exists to be able to disagree with the
    ``FROM`` / ``JOIN`` walk (round-2 H3), but nothing on the render path read
    the disagreement, so every one of these shipped: the three quoted / ``ONLY``
    spellings rendered "legacy" WITHOUT the rename (the walk saw no reference at
    all, so ``_rename_table`` had nothing to rename) and narrow with their text
    predicates intact, and the two double-read spellings rendered legacy with
    only one of their two reads renamed. Refusing on ``occurrences !=
    reference_count`` closes all of them at once, naming both counts.

    The equality is only as good as the counter's reach, and TWO review rounds
    found the same class through it: a spelling of the table that BOTH sides
    miss compares 0 == 0 and is waved through. Round 1 answered with two more
    schema-prefix alternatives; round 2 then produced six more (spaced,
    newlined, commented, quoted schema, no schema, another schema). So the
    counter no longer models schemas at all — it counts the bare NAME as a whole
    token, in any case, qualified or not — while the walk stays strict. The
    occurrence count is asserted per case because it is the half that must not
    silently go blind again (fixture decision 16).

    Matched on the message, not on the exception type: ``comma_join`` and
    ``in_table`` already raised for narrow through the text-identity check, so a
    bare ``pytest.raises(RiverTemplateError)`` would have been green before the
    guard existed.
    """
    assert fact_table_name_occurrences(sql) == occurrences
    for store in ("legacy", "narrow"):
        with pytest.raises(RiverTemplateError, match=f"{label}: unmodelled fact-table reference form"):
            render_river_ts_sql(sql, store, entry=label)
    with pytest.raises(RiverTemplateError, match="unmodelled fact-table reference form"):
        fact_table_text_identity_columns(sql, entry=label)


@pytest.mark.parametrize(
    ("label", "table", "qualifier", "occurrences"),
    [
        ("schema_qualified", "hydro.river_timeseries", "hydro.river_timeseries.", 3),
        ("bare", "hydro.river_timeseries", "river_timeseries.", 3),
        ("schema_qualified_legacy", "hydro.river_timeseries_legacy", "hydro.river_timeseries_legacy.", 3),
        ("bare_legacy", "hydro.river_timeseries_legacy", "river_timeseries_legacy.", 3),
        ("quoted", "hydro.river_timeseries", 'hydro."river_timeseries".', 3),
    ],
)
def test_a_column_qualified_by_the_table_name_is_refused_so_the_qualifier_is_never_unseen(
    label: str, table: str, qualifier: str, occurrences: int
) -> None:
    r"""#2018 round-3 G2, closed by DELETION of the counter's column-qualifier lookahead.

    PostgreSQL lets an UNALIASED table be named in its own predicates, so
    ``WHERE hydro.river_timeseries.variable = %s`` is a legal read whose text
    identity column neither arm of the scan can see: the alias set is empty, and
    the unqualified fallback's ``(?<![.\w])`` lookbehind is defeated by the dot.
    The narrow render shipped ``variable``; the BARE spelling additionally
    rendered a legacy statement whose qualifier ``_rename_table`` cannot follow —
    a reference to a ``FROM`` entry that no longer exists (verifier G).

    Closed by deleting the counter's ``(?!"?\s*\.)`` lookahead rather than by
    widening the scan: with it gone the qualifier is a counted MENTION the
    ``FROM`` / ``JOIN`` walk does not model, and the equality guard that already
    exists refuses the statement for both stores. No new pattern and no new
    over-match record — the detection arm verifier G proposed would have needed
    its own, over the un-blanked ``outer`` text. The accepted price is that a
    table-name qualifier is REFUSED rather than attributed; the answer for a
    template author is the bare alias pinned in
    ``test_qualifying_the_same_columns_through_a_bare_alias_still_renders``.
    """
    sql = f"SELECT value FROM {table} WHERE {qualifier}variable = %s AND {qualifier}run_key = %s"

    assert fact_table_name_occurrences(sql) == occurrences
    assert fact_table_attribution(sql).reference_count == 1
    for store in ("legacy", "narrow"):
        with pytest.raises(RiverTemplateError, match=f"{label}: unmodelled fact-table reference form"):
            render_river_ts_sql(sql, store, entry=label)
    with pytest.raises(RiverTemplateError, match="unmodelled fact-table reference form"):
        fact_table_text_identity_columns(sql, entry=label)


def test_qualifying_the_same_columns_through_a_bare_alias_still_renders() -> None:
    """Non-vacuity for the deletion: the ALIASED spelling is what the walk models.

    The deletion is only allowed to move the table-name qualifier; an ordinary
    aliased read of either physical table must render for both stores, and the
    legacy rename must stay idempotent on the ``_legacy`` name.
    """
    sql = "SELECT rt.value FROM hydro.river_timeseries_legacy rt WHERE rt.run_key = :run_key"

    assert fact_table_name_occurrences(sql) == fact_table_attribution(sql).reference_count == 1
    assert fact_table_text_identity_columns(sql, entry="aliased-qualifier") == set()
    assert render_river_ts_sql(sql, "legacy", entry="aliased-qualifier").sql == sql
    assert render_river_ts_sql(sql, "narrow", entry="aliased-qualifier").sql == sql


def test_an_unaliased_read_with_bare_columns_is_still_attributed_and_refused() -> None:
    """The discriminating control for G2: bare columns ARE seen, qualified ones are refused.

    Same statement as the refusal pins above with the qualifier dropped. It is
    what says the qualified spellings were a hole in the SCAN and not merely
    statements the module dislikes: this one is attributed, and the narrow
    variant is refused for carrying ``variable`` — not for being unmodelled.
    """
    sql = "SELECT value FROM hydro.river_timeseries WHERE variable = %s AND run_key = %s"

    assert fact_table_text_identity_columns(sql, entry="bare-columns") == {"variable"}
    with pytest.raises(RiverTemplateError) as excinfo:
        render_river_ts_sql(sql, "narrow", entry="bare-columns")
    assert "text identity column(s) ['variable']" in str(excinfo.value)
    assert "unmodelled" not in str(excinfo.value)


def test_text_fact_columns_answers_about_the_alias_it_is_given_without_the_form_guard() -> None:
    """The single-alias helper is NOT the guarded entry point — recorded and pinned.

    :func:`text_fact_columns` answers "does THIS alias predicate on a text
    identity column" for a caller that already knows the alias (the shape
    oracles pass one per surface), so it makes no claim about how many times the
    statement reads the fact table and has nothing to be blind about. The
    whole-statement question — "does this statement predicate on the FACT
    table's text identity" — is :func:`fact_table_text_identity_columns`, and
    that one refuses an unmodelled reference form instead of answering it.
    Pinned rather than closed by adding the guard: the guard would need an
    ``entry`` this helper's four oracle call sites do not have, and would refuse
    the bare WHERE-chain fragments they legitimately pass it.
    """
    sql = 'SELECT "r".value FROM "hydro"."river_timeseries" r WHERE r.run_id = :run_id'

    assert text_fact_columns(sql, "r") == {"run_id"}
    with pytest.raises(RiverTemplateError, match="unmodelled fact-table reference form"):
        fact_table_text_identity_columns(sql, entry="single-alias")


@pytest.mark.parametrize("prefix", ["U&", "u&"])
@pytest.mark.parametrize(
    ("label", "quoted"),
    [
        ("unicode_escape_plain", r'"river_timeseries"'),
        ("unicode_escape_hidden", r'"riv\0065r_timeseries"'),
    ],
)
def test_a_unicode_escaped_identifier_is_refused_because_the_counter_cannot_see_it(
    label: str, quoted: str, prefix: str
) -> None:
    r"""The one spelling that hides the NAME from a name counter.

    PostgreSQL's ``U&"riv\0065r_timeseries"`` denotes the fact table with no
    ``river_timeseries`` token anywhere in the text, so the permissive counter
    reads 0, the walk reads 0, and the equality guard waves it through — the
    same 0 == 0 hole in a form no widening of a NAME pattern can close. The
    module therefore refuses the syntax outright, ahead of the equality: an
    over-refusal of a ``U&'…'`` literal is a template nobody has, while a
    silently rendered narrow read of the fact table is the migration-window
    failure this whole module exists to stop.

    Parametrised over both spellings of the prefix, because PostgreSQL accepts
    ``u&"…"`` exactly as it accepts ``U&"…"``: dropping the guard's
    ``re.IGNORECASE`` survived every suite and let the lowercase spelling render
    with its text identity column (#2018 round-3 H1) — the same scoping gap the
    ``E`` / ``e`` parametrisation closed one guard earlier.
    """
    sql = f'SELECT r.value FROM hydro.{prefix}{quoted} r WHERE r.run_id = :run_id'

    for store in ("legacy", "narrow"):
        with pytest.raises(RiverTemplateError, match=f"{label}: unmodelled .*Unicode-escaped"):
            render_river_ts_sql(sql, store, entry=label)
    with pytest.raises(RiverTemplateError, match="Unicode-escaped"):
        fact_table_text_identity_columns(sql, entry=label)


@pytest.mark.parametrize(
    ("label", "sql"),
    [
        (
            "trailing_limit_1",
            "SELECT rt.value FROM hydro.river_timeseries rt "
            "WHERE rt.run_key = (SELECT r2.run_key FROM hydro.river_timeseries r2 "
            "WHERE r2.variable = %s LIMIT 1)",
        ),
        (
            "mid_chain",
            "SELECT rt.value FROM hydro.river_timeseries rt "
            "WHERE rt.run_key = (SELECT r2.run_key FROM hydro.river_timeseries r2 WHERE r2.variable = %s) "
            "AND rt.valid_time = %s",
        ),
    ],
)
def test_a_fact_read_inside_a_comparison_position_sub_select_is_refused(label: str, sql: str) -> None:
    """A read the text-identity scan never sees, because the scan strips it first.

    :func:`outer_predicates` deletes comparison-position sub-selects before any
    column is attributed — it must, or the authority resolution ``run_key =
    (SELECT run_key FROM hydro.hydro_run WHERE run_id = %s)`` reads as a text
    predicate on the fact table. A sub-select that reads the FACT table in that
    position is therefore invisible to the narrow check, and ``r2.variable``
    shipped (#2018 round-2 F4). Extending the scan into those sub-selects is not
    the fix: it false-refuses the two registered statements whose authority
    sub-select is exactly this shape. The statement is refused instead — the
    counts before and after stripping must agree.
    """
    for store in ("legacy", "narrow"):
        with pytest.raises(RiverTemplateError, match="comparison-position sub-select"):
            render_river_ts_sql(sql, store, entry=label)
    with pytest.raises(RiverTemplateError, match="comparison-position sub-select"):
        fact_table_text_identity_columns(sql, entry=label)


def test_an_authority_sub_select_in_the_same_position_still_renders() -> None:
    """Non-vacuity for the sub-select guard: the AUTHORITY table's resolution is the registry's own shape."""
    template = f"""
        SELECT rt.value
        FROM hydro.river_timeseries rt
        WHERE rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(run_id)s)
          {MARKER}
          AND rt.run_id = %(run_id)s
    """

    legacy = render_river_ts_sql(template, "legacy", entry="authority")
    narrow = render_river_ts_sql(template, "narrow", entry="authority")

    assert "FROM hydro.river_timeseries_legacy rt" in legacy.sql
    assert "AND rt.run_id = %(run_id)s" in legacy.sql
    assert "rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(run_id)s)" in narrow.sql
    assert "rt.run_id" not in narrow.sql


def test_the_reference_count_guard_does_not_fire_on_a_plainly_aliased_read() -> None:
    """Non-vacuity: the two counters agree on the ordinary form, so nothing is refused.

    Asserted with the refusal message, because the narrow variant of this
    statement is refused either way — the point is that it is refused for
    carrying ``run_id``, not for being unreadable.
    """
    sql = "SELECT r.value FROM hydro.river_timeseries r WHERE r.run_id = :run_id"

    assert fact_table_name_occurrences(sql) == fact_table_attribution(sql).reference_count == 1
    assert fact_table_text_identity_columns(sql, entry="counted") == {"run_id"}
    assert "FROM hydro.river_timeseries_legacy r" in render_river_ts_sql(sql, "legacy", entry="counted").sql
    with pytest.raises(RiverTemplateError) as excinfo:
        render_river_ts_sql(sql, "narrow", entry="counted")
    assert "text identity column(s) ['run_id']" in str(excinfo.value)
    assert "unmodelled" not in str(excinfo.value)


def test_a_literal_that_spells_a_fact_read_is_not_a_second_reference() -> None:
    """Probe F for the third counter: attribution reads CODE, like its siblings.

    `fact_table_attribution` folded `strip_comments`, which leaves string bodies
    intact, so `'... FROM hydro.river_timeseries'` was counted as a reference.
    That inflated `reference_count` — the number `fact_table_name_occurrences`
    exists to be able to disagree with — and, since the literal names no alias,
    flipped `has_unaliased_reference` to True, arming the unqualified-column
    fallback on a statement that has a perfectly good alias.
    """
    template = """
        SELECT 'FROM hydro.river_timeseries' AS note, rt.value
        FROM hydro.river_timeseries rt
        WHERE rt.valid_time = :valid_time
    """

    attribution = fact_table_attribution(template)

    assert attribution.reference_count == 1
    assert attribution.aliases == frozenset({"rt"})
    assert attribution.has_unaliased_reference is False
    assert fact_table_name_occurrences(template) == attribution.reference_count

    rendered = render_river_ts_sql(template, "narrow", entry="literal-reference")

    assert "'FROM hydro.river_timeseries' AS note" in rendered.sql


@pytest.mark.parametrize("prefix", ["E", "e"])
def test_an_escape_string_literal_does_not_swallow_the_statement_after_it(prefix: str) -> None:
    """``E'a\\'b'`` ends at its own closing quote, not at the escaped one.

    The scanners read `\\'` as the end of the literal and the following text as a
    SECOND literal running to the end of the statement, so the blanked text this
    module makes every structural decision in lost the statement's own ``FROM``
    clause: attribution reported no fact-table reference at all, the narrow
    render shipped ``rt.run_id`` and the legacy render skipped the rename
    (review #2018, B/P2-2). The blanking is what made this reachable, so the pin
    covers all three answers, not just the count.

    Parametrised over both spellings of the prefix: PostgreSQL accepts ``e'…'``
    exactly as it accepts ``E'…'``, and a scanner that only knew the upper-case
    one would put the whole class back (#2018 round-2 E/P2-1).
    """
    sql = (
        f"SELECT rt.value, {prefix}'a\\'b' AS note FROM hydro.river_timeseries rt "
        "WHERE rt.run_key = :run_key AND rt.run_id = :run_id"
    )

    attribution = fact_table_attribution(sql)

    assert attribution.aliases == frozenset({"rt"})
    assert attribution.reference_count == 1
    assert fact_table_name_occurrences(sql) == 1
    with pytest.raises(RiverTemplateError, match=r"text identity column\(s\) \['run_id'\]"):
        render_river_ts_sql(sql, "narrow", entry="escape-literal")
    assert "hydro.river_timeseries_legacy rt" in render_river_ts_sql(sql, "legacy", entry="escape-literal").sql


def test_a_plain_literal_ending_in_a_backslash_ends_at_its_own_quote() -> None:
    r"""The scope half of the ``E'…'`` arm: a PLAIN literal is not escape-aware.

    With ``standard_conforming_strings`` on, ``'C:\'`` is a complete literal
    whose last character is a backslash. A scanner that honoured backslashes in
    every single-quoted run would read the closing quote as escaped, run the
    "literal" on to the end of the statement and blank the ``FROM`` clause with
    it — the exact failure the ``E`` arm was added to fix, re-introduced from the
    other side (#2018 round-2 E/P2-1). Pinned as the SPAN, not only as the
    downstream answers: the span is where the two ideas of "where does this
    literal end" would first differ.
    """
    sql = r"SELECT 'C:\' AS f, rt.run_id FROM hydro.river_timeseries rt WHERE rt.run_key = :k"

    assert non_code_spans(sql) == ((7, 12, "literal"),)
    attribution = fact_table_attribution(sql)
    assert attribution.aliases == frozenset({"rt"})
    assert attribution.reference_count == 1
    assert fact_table_name_occurrences(sql) == 1
    with pytest.raises(RiverTemplateError, match=r"text identity column\(s\) \['run_id'\]"):
        render_river_ts_sql(sql, "narrow", entry="plain-backslash")
    assert "hydro.river_timeseries_legacy rt" in render_river_ts_sql(sql, "legacy", entry="plain-backslash").sql


def test_a_keyword_ending_in_e_does_not_turn_the_next_literal_into_an_escape_string() -> None:
    r"""``LIKE'C:\'`` — the ``E`` of ``LIKE``, with no whitespace to hide behind.

    The prefix has to be a TOKEN of its own. Reading the ``E`` of ``LIKE`` as one
    makes the literal escape-aware, its closing quote escaped, and everything
    after it data — including the ``AND rt.run_id`` predicate the narrow variant
    must refuse. No whitespace is required between a keyword and a literal, so
    this is the shape the boundary check exists for.
    """
    sql = r"SELECT rt.value FROM hydro.river_timeseries rt WHERE rt.tag LIKE'C:\' AND rt.run_id = :run_id"
    start = sql.index("'C:")

    assert non_code_spans(sql) == ((start, start + 5, "literal"),)
    assert fact_table_attribution(sql).aliases == frozenset({"rt"})
    assert fact_table_name_occurrences(sql) == 1
    with pytest.raises(RiverTemplateError, match=r"text identity column\(s\) \['run_id'\]"):
        render_river_ts_sql(sql, "narrow", entry="like-prefix")


def test_an_identifier_ending_in_underscore_e_is_not_an_escape_string_prefix() -> None:
    r"""``x_e'C:\'`` — an underscore is an identifier character, so ``e`` is a tail.

    The boundary check reads the character BEFORE the ``e``; dropping the
    underscore arm and testing only ``isalnum()`` puts this back as an escape
    string. Asserted at span level because that is the whole claim here: one
    five-character literal, and the rest of the text is code.
    """
    text = r"WHERE x_e'C:\' AND rt.run_id = :run_id"
    start = text.index("'C:")

    assert non_code_spans(text) == ((start, start + 5, "literal"),)


def test_an_escape_string_with_no_backslash_and_a_doubled_quote_still_render() -> None:
    """Non-vacuity: the backslash arm must not have changed the ordinary literals.

    ``E'abc'`` carries the prefix but no escape, and ``'a''b'`` is the standard
    doubled-quote form whose escape the backslash arm must not consume.
    """
    prefixed = "SELECT rt.value, E'abc' AS note FROM hydro.river_timeseries rt WHERE rt.valid_time = :valid_time"
    doubled = "SELECT rt.value, 'a''b' AS note FROM hydro.river_timeseries rt WHERE rt.valid_time = :valid_time"

    for sql, literal in ((prefixed, "E'abc' AS note"), (doubled, "'a''b' AS note")):
        assert fact_table_attribution(sql).aliases == frozenset({"rt"})
        assert literal in render_river_ts_sql(sql, "narrow", entry="e-control").sql
        assert "hydro.river_timeseries_legacy rt" in render_river_ts_sql(sql, "legacy", entry="e-control").sql


# ---------------------------------------------------------------------------
# Round-3 review (#2018 G1, fixture decision 17): the scanner against
# PostgreSQL's lexical structure (§4.1)
#
# `non_code_spans` is COMMON-MODE — the occurrence counter, the FROM/JOIN walk,
# the rename and the structural check all read the text it blanks — so an error
# in it is NOT caught by the counter-vs-walk equality guard: both sides read the
# same phantom literal, both answer 0, and 0 == 0 is satisfied by mutual
# blindness (retro-2018.md). The remaining hole space is therefore PostgreSQL's
# LEXER, which is finite and externally specified, so every row of fixture
# decision 17's table is pinned here rather than reasoned about in a docstring:
# a direction claim with no red/green probe next to it is the process defect the
# retro converted into a rule.
# ---------------------------------------------------------------------------


def _is_code(sql: str, needle: str) -> bool:
    """Whether the first occurrence of ``needle`` sits outside every non-code span."""
    position = sql.index(needle)
    return not any(start <= position < stop for start, stop, _kind in non_code_spans(sql))


def test_a_nested_block_comment_does_not_blank_the_statements_own_from_clause() -> None:
    """T1 — PostgreSQL NESTS block comments, so ending at the first ``*/`` re-tokenises the tail.

    ``/* outer /* inner */ don't stop here */`` is ONE comment. A scanner that
    stops at the inner ``*/`` reads `` don't stop here */`` as code, the
    apostrophe of ``don't`` opens a phantom literal, and that literal runs over
    the statement's own ``FROM`` clause: the counter read 0, the walk read 0, the
    equality guard was satisfied by mutual blindness, the narrow render shipped
    ``rt.variable`` and the legacy render skipped the rename (#2018 round-3 G1,
    reproduced independently by all three review lanes).
    """
    sql = (
        "SELECT value /* outer /* inner */ don't stop here */ "
        "FROM hydro.river_timeseries rt WHERE rt.variable = %(variable)s AND rt.run_key = %(run_key)s"
    )

    assert _is_code(sql, "FROM hydro.river_timeseries")
    assert fact_table_name_occurrences(sql) == 1
    assert fact_table_attribution(sql).aliases == frozenset({"rt"})
    with pytest.raises(RiverTemplateError, match=r"text identity column\(s\) \['variable'\]"):
        render_river_ts_sql(sql, "narrow", entry="nested-comment")
    assert "hydro.river_timeseries_legacy rt" in render_river_ts_sql(sql, "legacy", entry="nested-comment").sql


def test_a_nested_block_comment_is_exactly_one_span() -> None:
    """The depth claim itself, pinned as a span rather than stated in prose.

    ``/* a /* b */ c */`` is one comment: the inner ``*/`` closes the inner
    comment only. Asserted at span level because the span is where the scanner
    and PostgreSQL's lexer first disagree — every downstream answer is derived
    from it (fixture decision 17, block-comment row).
    """
    sql = "/* a /* b */ c */ SELECT 1 FROM hydro.river_timeseries rt WHERE rt.run_key = :k"

    assert non_code_spans(sql) == ((0, 17, "comment"),)
    assert fact_table_name_occurrences(sql) == 1
    assert fact_table_attribution(sql).aliases == frozenset({"rt"})


def test_a_non_nested_block_comment_with_an_apostrophe_keeps_todays_answer() -> None:
    """Non-vacuity for the depth counter: the ORDINARY comment is unchanged.

    Same apostrophe, no nesting — one comment span, one counted read, and the
    two renders the module gave before the depth counter existed. The depth
    counter is only allowed to change the nested case.
    """
    sql = (
        "SELECT value /* don't stop here */ "
        "FROM hydro.river_timeseries rt WHERE rt.variable = %(variable)s AND rt.run_key = %(run_key)s"
    )

    assert non_code_spans(sql) == ((13, 34, "comment"),)
    assert fact_table_name_occurrences(sql) == 1
    with pytest.raises(RiverTemplateError, match=r"text identity column\(s\) \['variable'\]"):
        render_river_ts_sql(sql, "narrow", entry="plain-comment")
    assert "hydro.river_timeseries_legacy rt" in render_river_ts_sql(sql, "legacy", entry="plain-comment").sql


def test_a_phantom_literal_that_closes_on_a_later_escape_string_still_loses_the_read() -> None:
    r"""T3 — the CLASS pin: the desynchronisation re-synchronises BEFORE the end of the text.

    The nested comment's tail opens a phantom literal at ``don't``, and that
    literal CLOSES on the opening quote of the real ``E'q\'x'``: an escape string
    contributes an ODD number of quote characters, which re-pairs the scan. So
    the last span ends well before ``len(sql)`` and the unterminated-span belt in
    ``_assert_modelled_reference_forms`` never sees it — this case is answered
    only by the scanner agreeing with PostgreSQL's lexer, which is why the belt
    is recorded as a belt for the unterminated sub-case and never as the closure
    (verifier #2018 round-3 G measured this shape as the discriminator).
    """
    sql = (
        "SELECT 1 /* a /* b */ don't */ FROM hydro.river_timeseries rt "
        "WHERE rt.variable = E'q\\'x' AND rt.run_key = %(k)s"
    )

    assert _is_code(sql, "FROM hydro.river_timeseries")
    assert non_code_spans(sql)[-1][1] < len(sql)
    assert fact_table_name_occurrences(sql) == 1
    assert fact_table_attribution(sql).aliases == frozenset({"rt"})
    with pytest.raises(RiverTemplateError, match=r"text identity column\(s\) \['variable'\]"):
        render_river_ts_sql(sql, "narrow", entry="resync")
    assert "hydro.river_timeseries_legacy rt" in render_river_ts_sql(sql, "legacy", entry="resync").sql


@pytest.mark.parametrize(("label", "newline"), [("carriage_return", "\r"), ("line_feed", "\n")])
def test_a_line_comment_ends_at_a_carriage_return_as_well_as_a_line_feed(label: str, newline: str) -> None:
    r"""T4 — ``--`` runs to the end of the LINE, and PostgreSQL's non_newline is ``[^\n\r]``.

    With ``find("\n")`` alone a ``\r``-terminated comment swallows the rest of the
    statement — the ``FROM`` clause and the ``run_id`` predicate with it — so both
    counters read 0 and the narrow render shipped a text identity column. The
    ``line_feed`` id is the control that says the fix changed nothing for the
    ordinary spelling.
    """
    sql = (
        f"SELECT rt.value -- note 'x{newline}"
        "FROM hydro.river_timeseries rt WHERE rt.run_key = :k AND rt.run_id = :r"
    )

    assert _is_code(sql, "FROM hydro.river_timeseries")
    assert fact_table_name_occurrences(sql) == 1
    assert fact_table_attribution(sql).aliases == frozenset({"rt"})
    with pytest.raises(RiverTemplateError, match=r"text identity column\(s\) \['run_id'\]"):
        render_river_ts_sql(sql, "narrow", entry=label)
    assert "hydro.river_timeseries_legacy rt" in render_river_ts_sql(sql, "legacy", entry=label).sql


def test_a_carriage_return_ends_a_line_comment_for_the_sub_select_stripper_too() -> None:
    r"""The same ``--`` rule at its SECOND site: ``_LINE_COMMENT``, inside ``outer_predicates``.

    ``_in_comparison_value_position`` strips the line comment out of the text it
    has kept so far before asking "was the last token a comparison operator".
    Spelled ``--[^\n]*`` that strip eats the ``\r`` and the ``=`` behind it, the
    authority sub-select is then not recognised as comparison-position, it
    survives into ``outer_predicates``, and its ``WHERE run_id = :run_id`` — the
    AUTHORITY table's own column — is attributed to the unaliased fact table and
    refused. Two scanners with two ideas of where a line comment ends is the
    disagreement :func:`non_code_spans` exists to prevent, so the rule is applied
    at both sites and pinned once.
    """
    sql = (
        "SELECT value -- first note\r"
        "FROM hydro.river_timeseries WHERE run_key -- pick the authority run\r"
        "= (SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id)"
    )

    assert fact_table_name_occurrences(sql) == 1
    assert fact_table_attribution(sql).has_unaliased_reference is True
    assert fact_table_text_identity_columns(sql, entry="cr-authority") == set()
    assert render_river_ts_sql(sql, "narrow", entry="cr-authority").sql == sql


def test_an_apostrophe_inside_a_quoted_identifier_does_not_desynchronise_the_scan() -> None:
    r"""decision 17's quoted-identifier row, pinned on the arm it actually exercises.

    A ``"…"`` run is an IDENTIFIER: the scanner traverses it and records NO span,
    because ``"hydro"."river_timeseries"`` is a read of the fact table that must
    be counted while ``'hydro.river_timeseries'`` is data that must not. The
    round-2 quoted-identifier tests pin the COUNTER's answer, not this arm — with
    the arm deleted a ``"`` is simply skipped as ordinary code and every one of
    them stays green (verifier #2018 round-4 J2 measured 461/461 under the
    deletion mutant).

    Only an APOSTROPHE inside the quotes discriminates. With the arm gone the
    ``'`` of ``"it's value"`` opens a phantom literal that runs over the real
    ``FROM`` and closes on the odd quote of ``E'q\'x'`` — measured at head: spans
    ``((22, 85), (87, 90))``, occurrences 0, walk 0, text identity ``set()``,
    legacy rendered UN-renamed and narrow rendered shipping ``rt.variable``. That
    is the round-3 G1 class re-opened with the whole suite green, which is
    exactly what decision 17's "every row carries a pin" preamble exists to
    prevent.
    """
    sql = (
        "SELECT rt.value AS \"it's value\" FROM hydro.river_timeseries rt "
        "WHERE rt.variable = E'q\\'x' AND rt.run_key = %(k)s"
    )

    assert _is_code(sql, "FROM hydro.river_timeseries")
    assert fact_table_name_occurrences(sql) == 1
    assert fact_table_attribution(sql).reference_count == 1
    assert fact_table_attribution(sql).aliases == frozenset({"rt"})
    assert fact_table_text_identity_columns(sql, entry="quoted-apostrophe") == {"variable"}
    with pytest.raises(RiverTemplateError, match=r"text identity column\(s\) \['variable'\]"):
        render_river_ts_sql(sql, "narrow", entry="quoted-apostrophe")
    assert "hydro.river_timeseries_legacy rt" in render_river_ts_sql(sql, "legacy", entry="quoted-apostrophe").sql


def test_a_unicode_escaped_string_constant_is_refused_like_the_identifier_form() -> None:
    """decision 17's ``U&'…'`` row: the guard is the PREFIX, not the quote that follows it.

    ``_UNICODE_ESCAPED`` is ``\\bU&`` and deliberately says nothing about what
    comes next, so a Unicode-escaped STRING is refused on the same prefix as a
    Unicode-escaped identifier. Narrowing it to ``\\bU&(?=")`` — the obvious
    "only identifiers can hide the table name" tidy — is fail-open and survived
    every suite at 7be3e273 (verifier J2): the row claimed a ``U&'abc'`` control
    that no test input contained.

    The over-refusal is accepted and recorded: a template predicating on a
    ``U&'…'`` literal is refused although it hides nothing. 0/20 registered
    templates use the syntax at all.
    """
    sql = "SELECT r.value FROM hydro.river_timeseries r WHERE r.tag = U&'abc' AND r.run_key = %(k)s"

    for store in ("legacy", "narrow"):
        with pytest.raises(RiverTemplateError, match="unmodelled fact-table reference form"):
            render_river_ts_sql(sql, store, entry="unicode-escaped-string")
    with pytest.raises(RiverTemplateError, match="Unicode-escaped"):
        fact_table_text_identity_columns(sql, entry="unicode-escaped-string")


def test_a_dollar_before_an_escape_prefix_is_refused_as_outside_the_lexical_subset() -> None:
    r"""``x$e'C:\'`` — the round-3 pin FLIPPED by decision 18, and why it had to flip.

    Round 3 answered this shape by teaching :func:`_opens_escape_string` that
    ``$`` is an identifier character, so the ``e`` in ``x$e`` is a tail and the
    literal is a plain one. That answer was right about PostgreSQL and still a
    liability: it made ``$`` a rule with FOUR sites in this module, and round 4
    found the same rule wrong at two of them (#2018 I1/I2). Decision 18 removes
    the rule instead — a ``$`` in code is refused, so no site has to be right.

    The refusal here is the SUBSET one, not the unterminated belt, and that is
    the check ORDER being pinned: with the subset check moved after the belt this
    statement is still refused, but the message changes and the module claims to
    have diagnosed a terminator when what it really met was an unmodelled
    character. Its sibling ``x_e'C:\'`` (underscore, no dollar) is INSIDE the
    subset and still not an escape prefix — that pin is
    ``test_an_identifier_ending_in_underscore_e_is_not_an_escape_string_prefix``,
    which is what keeps ``_IDENTIFIER_TAIL`` non-vacuous after ``$`` left it.
    """
    sql = r"SELECT rt.value FROM hydro.river_timeseries rt WHERE x$e'C:\' AND rt.run_id = :run_id"

    for store in ("legacy", "narrow"):
        with pytest.raises(RiverTemplateError, match="outside the modelled lexical subset"):
            render_river_ts_sql(sql, store, entry="dollar-e")
    with pytest.raises(RiverTemplateError, match="outside the modelled lexical subset"):
        fact_table_text_identity_columns(sql, entry="dollar-e")


@pytest.mark.parametrize(
    ("label", "tail"),
    [
        ("unterminated_literal", "AND rt.note = 'oops"),
        ("unterminated_block_comment", "AND rt.note = 'ok' /* oops"),
    ],
)
def test_a_statement_that_ends_inside_an_unterminated_span_is_refused(label: str, tail: str) -> None:
    """The belt: if the scan never closed a span, the blanked text is not the statement's code.

    Recorded as a BELT and nothing more (fixture decision 17, verifier #2018
    round-3 G): it catches ONLY the unterminated sub-case. A phantom literal can
    close on a later odd-quote literal and re-synchronise long before the end of
    the text, which is what
    ``test_a_phantom_literal_that_closes_on_a_later_escape_string_still_loses_the_read``
    pins — that case is answered by the scanner agreeing with PostgreSQL's lexer
    and never by this check.
    """
    sql = f"SELECT rt.value FROM hydro.river_timeseries rt WHERE rt.run_key = :k {tail}"

    for store in ("legacy", "narrow"):
        with pytest.raises(RiverTemplateError, match=f"{label}: .*unterminated literal or comment"):
            render_river_ts_sql(sql, store, entry=label)
    with pytest.raises(RiverTemplateError, match="unterminated literal or comment"):
        fact_table_text_identity_columns(sql, entry=label)


def test_a_span_that_ends_at_the_end_of_the_text_is_not_the_same_as_an_unterminated_one() -> None:
    """The belt's scope: reaching the end of the text is not "never closed".

    A literal whose closing quote is the template's last character is complete,
    and PostgreSQL ends a ``--`` comment at the end of the input as readily as at
    a newline. Spelling the belt as "the last span stops at ``len(sql)``" would
    refuse both — ``WHERE rt.variable = 'q_down'`` is an ordinary template ending
    — so the check asks the scanner whether it FOUND the close instead.
    """
    for label, sql in (
        ("trailing_literal", "SELECT rt.value FROM hydro.river_timeseries rt WHERE rt.tag = 'q_down'"),
        ("trailing_line_comment", "SELECT rt.value FROM hydro.river_timeseries rt WHERE rt.run_key = :k -- note"),
    ):
        assert non_code_spans(sql)[-1][1] == len(sql)
        assert render_river_ts_sql(sql, "narrow", entry=label).sql == sql
        assert "hydro.river_timeseries_legacy rt" in render_river_ts_sql(sql, "legacy", entry=label).sql


@pytest.mark.parametrize(("label", "literal"), [("bit", "B'1010'"), ("hex", "X'FF'")])
def test_a_bit_or_hex_constant_is_a_plain_literal(label: str, literal: str) -> None:
    """decision 17: ``B'…'`` / ``X'…'`` are ordinary literals — those prefixes escape nothing.

    Pinned as the span: the prefix stays OUTSIDE it (like ``E``), the quotes are
    the span's own first and last characters, and the statement around it is
    code.
    """
    sql = f"SELECT rt.value, {literal} AS flags FROM hydro.river_timeseries rt WHERE rt.run_id = :run_id"
    start = sql.index("'")

    assert non_code_spans(sql) == ((start, start + len(literal) - 1, "literal"),)
    assert fact_table_name_occurrences(sql) == 1
    with pytest.raises(RiverTemplateError, match=r"text identity column\(s\) \['run_id'\]"):
        render_river_ts_sql(sql, "narrow", entry=label)

# ---------------------------------------------------------------------------
# Round-4 review (#2018 I1/I2, fixture decision 18): the DECLARED LEXICAL SUBSET
#
# Rounds 1–4 all hit one invariant: the module's picture of where the fact table
# is read must equal PostgreSQL's, that picture comes from a hand-rolled lexer
# BOTH counters read, and so a lexer/PostgreSQL disagreement is common-mode —
# both sides answer 0, the equality guard is satisfied by mutual blindness, and
# the narrow render ships a text-identity column. Rounds 1–3 answered by teaching
# the lexer one more §4.1 rule each time. Round 4 found the SAME rule (dollar
# quoting) wrong at two sites — an ASCII tag class against §4.1's byte class, and
# a whole second lexer in the traversal family with no dollar arm at all.
#
# Decision 18 answers by SHRINKING the module's input domain instead: the
# renderer accepts a declared subset of §4.1 and refuses everything else. The
# refused characters are `$` and any non-ASCII byte IN CODE, measured 0/20 over
# the registry (its two `$` sit inside `'^[0-9]+$'` literals, its four `—` inside
# comments), so nothing the readers use is lost. The pins below are the subset's
# contract: what is refused, what is NOT refused, and that the refusal happens
# before any traversal can mis-lex the statement.
# ---------------------------------------------------------------------------


_SUBSET_REFUSAL = "outside the modelled lexical subset"

#: The suffix every decision-18 probe ends with: a REAL read of the fact table
#: with a real text-identity predicate. Without it a "refused" verdict would be
#: indistinguishable from "there was nothing here to get wrong" — each statement
#: below is one the module MUST NOT render, because rendering it narrow ships
#: `rt.variable` (or leaves the legacy table un-renamed).
_REAL_READ = "rt.value FROM hydro.river_timeseries rt WHERE rt.variable = %(v)s AND rt.run_key = %(k)s"


def _assert_refused_as_outside_the_subset(sql: str, entry: str) -> None:
    """Both stores and the text-identity oracle refuse ``sql``, and none names the table.

    The three surfaces are asserted together because they are the three doors
    into this module and decision 18 closes all three: ``render_river_ts_sql``
    calls the guard for BOTH stores before it branches, and
    ``fact_table_text_identity_columns`` calls it as its first statement.

    The message must NOT contain the fact table's name: the statement census in
    ``tests/test_river_ts_text_identity_cleanup.py`` counts that name in this
    module's string constants, so a refusal message spelling it would add a
    phantom "read site" to the census (census-2).
    """
    for store in ("legacy", "narrow"):
        with pytest.raises(RiverTemplateError, match=_SUBSET_REFUSAL) as raised:
            render_river_ts_sql(sql, store, entry=entry)
        assert "river_timeseries" not in str(raised.value)
    with pytest.raises(RiverTemplateError, match=_SUBSET_REFUSAL) as raised:
        fact_table_text_identity_columns(sql, entry=entry)
    assert "river_timeseries" not in str(raised.value)


@pytest.mark.parametrize(
    ("label", "sql"),
    [
        ("dollar_dollar", f"SELECT $$a$$ AS note, {_REAL_READ}"),
        ("tagged", f"SELECT $q$a$q$ AS note, {_REAL_READ}"),
        (
            "non_ascii_tag",
            "SELECT $备注$don't$备注$ AS note, rt.value FROM hydro.river_timeseries rt "
            "WHERE rt.variable = E'q\\'x' AND rt.run_key = %(k)s",
        ),
        ("symbol_tag", f"SELECT $€$x$€$ AS note, {_REAL_READ}"),
        (
            "positional",
            "SELECT rt.value FROM hydro.river_timeseries rt WHERE rt.run_id = $1 AND rt.variable = $2",
        ),
        ("identifier_dollar", f"SELECT a$b$c, {_REAL_READ}"),
        ("tag_after_number", f"SELECT 1$q$x$q$ AS note, {_REAL_READ}"),
    ],
    ids=[
        "dollar_dollar",
        "tagged",
        "non_ascii_tag",
        "symbol_tag",
        "positional",
        "identifier_dollar",
        "tag_after_number",
    ],
)
def test_a_dollar_sign_in_code_is_refused_as_outside_the_lexical_subset(label: str, sql: str) -> None:
    """Every ``$`` in code is refused, whatever PostgreSQL would have made of it.

    The point of the parameter list is that the module no longer has to KNOW
    which of these opens a dollar quote. Round 3 shipped an opener pattern that
    had to be right about all seven at once, and round 4 measured it wrong about
    two:

    * ``non_ascii_tag`` — verifier I's P1. PostgreSQL's ``dolq_start`` is the
      BYTE class ``[A-Za-z\\200-\\377_]``, so ``$备注$`` is a real dollar quote;
      the module's ASCII class opened nothing, the apostrophe of ``don't`` opened
      a phantom literal over the real ``FROM``, both counters read 0, and the
      narrow render shipped ``rt.variable = E'q\\'x'``. This is also the shape
      that pins the guard ORDER: it passes the unterminated belt (its last span
      closes on the escape string's odd quote), it passes the equality guard
      (0 == 0) and it passes the sub-select delta, so the subset check is the
      ONLY check that can refuse it;
    * ``symbol_tag`` — the same class one step further: ``$€$`` is admitted by
      the byte reading of ``scan.l`` and was not closed by round 4's proposed
      ``\\w`` repair either.

    The other five are shapes the round-3 module handled correctly and now
    refuses instead: ``$$``/``$q$`` real bodies, ``$1`` positional parameters
    (a placeholder dialect no registered template uses — 0/20), ``a$b$c`` (ONE
    identifier under §4.1's ``$``-extension) and ``1$q$x$q$`` (a tag directly
    after a numeric constant). Refusing them costs nothing measured and buys the
    deletion of the rule that was wrong twice.
    """
    _assert_refused_as_outside_the_subset(sql, label)


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("line_comment", "$q$a -- b$q$"),
        ("block_comment", "$q$a /* b$q$"),
        ("unbalanced_paren", "$q$($q$"),
    ],
    ids=["line_comment", "block_comment", "unbalanced_paren"],
)
def test_a_dollar_body_hiding_a_comment_or_paren_is_refused_before_any_traversal(label: str, body: str) -> None:
    """Verifier I's I2 statements: the refusal PRECEDES the traversal family.

    These are ordinary ASCII with an ordinary ``$q$`` tag — at 7be3e273 the
    SCANNER was right about them and the second lexer family was wrong. It
    re-lexed with no dollar arm, so ``strip_comments`` read the ``--`` inside the
    body as a real comment, deleted the rest of the line, and
    ``outer_predicates`` handed the text-identity scan ``'SELECT $q$a'`` — the
    ``FROM`` and the ``rt.variable`` predicate simply gone. The narrow render
    shipped ``rt.variable``. The ``unbalanced_paren`` shape failed the other way:
    ``assert_structurally_intact`` counted the ``(`` inside the body and refused
    a legal statement for "unbalanced parentheses".

    What is pinned is the MESSAGE, not merely the refusal: all three must be
    refused as subset violations. A subset message proves the statement never
    reached ``strip_comments``, ``outer_predicates`` or the paren walk at all —
    which is how decision 18 closes I2 by deletion rather than by teaching the
    second family a dollar arm it would then have to keep in step with the
    first. An "unbalanced parentheses" message on ``unbalanced_paren``, or a
    text-identity message on the other two, means the order regressed.
    """
    sql = f"SELECT {body} AS note, rt.value FROM hydro.river_timeseries rt WHERE rt.variable = %(v)s"

    for store in ("legacy", "narrow"):
        with pytest.raises(RiverTemplateError, match=_SUBSET_REFUSAL) as raised:
            render_river_ts_sql(sql, store, entry=label)
        assert "unbalanced parentheses" not in str(raised.value)
        assert "text identity column" not in str(raised.value)
    with pytest.raises(RiverTemplateError, match=_SUBSET_REFUSAL):
        fact_table_text_identity_columns(sql, entry=label)


def test_a_dollar_sign_inside_a_literal_or_comment_stays_inside_the_subset() -> None:
    """Non-vacuity, and the registry's OWN shape: the subset is asked over blanked text.

    Two of the twenty registered templates contain a ``$`` — both inside a
    ``'^[0-9]+$'`` regex literal — and this is why decision 18 costs the readers
    nothing. A subset check spelled over the RAW text instead of the
    comment/literal-blanked text would refuse them and take the whole change with
    it, so the "0 ``$`` in code" measurement is pinned here as behaviour rather
    than quoted as a number.
    """
    literal_sql = (
        "SELECT rt.value FROM hydro.river_timeseries rt WHERE rt.run_key = %(k)s "
        "AND rt.note ~ '^[0-9]+$' -- costs $0"
    )
    comment_sql = (
        "SELECT rt.value /* $$ not a body $$ */ FROM hydro.river_timeseries rt WHERE rt.run_key = %(k)s"
    )

    for label, sql in (("literal_and_line_comment", literal_sql), ("block_comment", comment_sql)):
        assert fact_table_name_occurrences(sql) == 1
        assert fact_table_attribution(sql).aliases == frozenset({"rt"})
        assert fact_table_text_identity_columns(sql, entry=label) == set()
        assert render_river_ts_sql(sql, "narrow", entry=label).sql == sql
        assert "hydro.river_timeseries_legacy rt" in render_river_ts_sql(sql, "legacy", entry=label).sql


@pytest.mark.parametrize(
    ("label", "sql"),
    [
        (
            "identifier",
            "SELECT rt.value AS 备注 FROM hydro.river_timeseries rt "
            "WHERE rt.variable = %(v)s AND rt.run_key = %(k)s",
        ),
        (
            "symbol_before_escape_prefix",
            "SELECT €e'x\\' AS n, rt.value FROM hydro.river_timeseries rt WHERE rt.variable = %(v)s",
        ),
    ],
    ids=["identifier", "symbol_before_escape_prefix"],
)
def test_a_non_ascii_byte_in_code_is_refused_as_outside_the_lexical_subset(label: str, sql: str) -> None:
    """The other half of the subset: PostgreSQL's identifier byte classes.

    ``identifier`` is the plain case — §4.1.1 admits non-ASCII letters in an
    unquoted identifier, so ``AS 备注`` is legal SQL this module will not model.

    ``symbol_before_escape_prefix`` is the FOURTH site of the ``$``/byte-class
    rule that verifier I named: ``_opens_escape_string`` asks
    ``str.isalnum()``, which is Unicode-letter-aware (so ``é`` is covered) but
    False for a symbol like ``€`` — so the module reads ``e'`` as an escape
    prefix, the ``\\'`` as an escaped quote, and the literal runs to the end of
    the statement over the real ``FROM``. Refused by the subset rule REGARDLESS
    of how the scanner lexed it, which is the property that makes the fourth site
    not need an answer of its own. It is also the id that discriminates the check
    ORDER: with the subset check moved after the unterminated belt this statement
    is refused as "unterminated", i.e. with a diagnosis of the wrong defect.
    """
    _assert_refused_as_outside_the_subset(sql, label)


def test_non_ascii_text_inside_a_comment_or_literal_stays_inside_the_subset() -> None:
    """Non-vacuity for the byte half: 4/20 registered templates do exactly this.

    The four non-ASCII occurrences in the registry are ``—`` characters inside
    ``--`` comments. Blanked text is what the rule reads, so prose comments and
    ``'备注'`` literals are data and render normally for both stores.
    """
    comment_sql = (
        "SELECT rt.value -- 注释 — dash\n"
        "FROM hydro.river_timeseries rt WHERE rt.run_key = %(k)s"
    )
    literal_sql = "SELECT rt.value, '备注' AS note FROM hydro.river_timeseries rt WHERE rt.run_key = %(k)s"

    for label, sql in (("comment", comment_sql), ("literal", literal_sql)):
        assert fact_table_name_occurrences(sql) == 1
        assert fact_table_text_identity_columns(sql, entry=label) == set()
        assert render_river_ts_sql(sql, "narrow", entry=label).sql == sql
        assert "hydro.river_timeseries_legacy rt" in render_river_ts_sql(sql, "legacy", entry=label).sql



# ---------------------------------------------------------------------------
# decision 18, second half: the TRAVERSAL FAMILY against the scanner
#
# `non_code_spans` is not the module's only lexer. `_skip_balanced`,
# `strip_scalar_subqueries`, `strip_all_subqueries`, `strip_comments` (and
# therefore `outer_predicates` and `sql_chains`) and the paren loop inside
# `assert_structurally_intact` each walk the raw text with their own copy of the
# same rules, because they must return the ORIGINAL text with non-comment spans
# verbatim (`outer_predicates`'s exact strings are compared in
# tests/test_migrations.py and tests/test_sql_shape_helpers.py), which consuming
# blanked text cannot do. Round 4 (#2018 I2) found the family had NO dollar arm
# while the scanner had one: `$q$a -- b$q$` truncated `outer_predicates` at a
# comment that was really inside a literal, and the narrow render shipped
# `rt.variable`. The round-3 checklist had recorded this family as "fail-closed
# example measured" with no probe — the exact defect retro rule 4 forbids.
#
# Deleting the dollar arm from the scanner makes the two lexers agree by
# construction. "By construction" is what the previous round claimed too, so it
# is pinned here instead: each traversal is compared against a reference built
# from `non_code_spans` itself, over the registry corpus plus an adversarial one.
# ---------------------------------------------------------------------------


def _opaque_runs(sql: str) -> tuple[tuple[int, int, str], ...]:
    """Every run the SCANNER refuses to read as code, in document order.

    The scanner's non-code spans PLUS the double-quoted identifier runs it skips
    without recording (``non_code_spans`` deliberately reports no span for a
    quoted identifier — in PostgreSQL that is code — but it does not read INSIDE
    one either, and neither may the traversal family).

    Derived from ``non_code_spans`` and the module's own ``_scan_quoted``, never
    re-lexed from scratch: this list is the oracle for "the family agrees with
    the scanner", so it has to BE the scanner's answer, not a third opinion.
    """
    spans = {start: (stop, kind) for start, stop, kind in non_code_spans(sql)}
    runs: list[tuple[int, int, str]] = []
    index = 0
    while index < len(sql):
        if index in spans:
            stop, kind = spans[index]
            runs.append((index, stop, kind))
            index = stop
            continue
        if sql[index] == '"':
            stop = _scan_quoted(sql, index, '"')
            runs.append((index, stop, "identifier"))
            index = stop
            continue
        index += 1
    return tuple(runs)


def _reference_strip_comments(sql: str) -> str:
    """``strip_comments`` expressed as "replace each scanner COMMENT run with one space"."""
    kept: list[str] = []
    last = 0
    for start, stop, kind in _opaque_runs(sql):
        if kind != "comment":
            continue
        kept.append(sql[last:start])
        kept.append(" ")
        last = stop
    kept.append(sql[last:])
    return "".join(kept)


def _reference_strip_subqueries(sql: str, *, comparison_position_only: bool) -> str:
    """The two sub-select strippers, driven by the scanner's runs instead of their own lexer."""
    runs = {start: stop for start, stop, _kind in _opaque_runs(sql)}

    def skip_balanced(start: int) -> int:
        depth = 0
        index = start
        while index < len(sql):
            if index in runs:
                index = runs[index]
                continue
            if sql[index] == "(":
                depth += 1
            elif sql[index] == ")":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        return len(sql)

    kept: list[str] = []
    index = 0
    while index < len(sql):
        if index in runs:
            kept.append(sql[index : runs[index]])
            index = runs[index]
            continue
        if (
            sql[index] == "("
            and _SUBQUERY_START.match(sql, index)
            and (not comparison_position_only or _in_comparison_value_position(kept))
        ):
            index = skip_balanced(index)
            continue
        kept.append(sql[index])
        index += 1
    return "".join(kept)


def _outcome(call: Callable[[], object]) -> str:
    """``"ok"`` or the refusal's own reason, with the echoed text cut off."""
    try:
        call()
    except RiverTemplateError as error:
        return f"refused: {str(error).split(' -> ')[0]}"
    return "ok"


#: Statements built to make a family-only lexing divergence VISIBLE. Every entry
#: is a shape some traversal has to lex the same way the scanner does, and most
#: of them are the exact inputs one of the four review rounds used.
_ADVERSARIAL_CORPUS: tuple[tuple[str, str], ...] = (
    (
        "nested_comment_apostrophe",
        "SELECT 1 /* a /* b */ don't */ FROM hydro.river_timeseries rt WHERE rt.run_key = :k",
    ),
    (
        "escape_string",
        r"SELECT rt.value FROM hydro.river_timeseries rt WHERE rt.variable = E'q\'x' AND rt.run_key = :k",
    ),
    ("doubled_quote", "SELECT 'a''b' AS n, rt.value FROM hydro.river_timeseries rt WHERE rt.run_key = :k"),
    (
        "quoted_identifier_apostrophe",
        "SELECT rt.value AS \"it's\" FROM hydro.river_timeseries rt WHERE rt.run_key = :k",
    ),
    ("carriage_return_comment", "SELECT rt.value -- note 'x\rFROM hydro.river_timeseries rt WHERE rt.run_key = :k"),
    ("like_backslash", r"SELECT rt.value FROM hydro.river_timeseries rt WHERE rt.note LIKE'C:\' AND rt.run_key = :k"),
    ("underscore_e_backslash", r"SELECT rt.value FROM hydro.river_timeseries rt WHERE x_e'C:\' AND rt.run_key = :k"),
    (
        "paren_inside_literal",
        "SELECT rt.value FROM hydro.river_timeseries rt WHERE rt.run_key = (SELECT run_key FROM hydro.hydro_run "
        "WHERE note = '(SELECT 1' AND run_id = :r)",
    ),
    # The `_skip_balanced` discriminator: an UNBALANCED paren inside a literal
    # inside a stripped group, with real text AFTER the group's own `)`. Without
    # the trailing conjunct the mis-lexed skip and the correct one both truncate
    # at the end of the statement and the mutant is invisible.
    (
        "paren_inside_literal_inside_a_stripped_group",
        "SELECT rt.value FROM hydro.river_timeseries rt WHERE rt.run_key = (SELECT run_key FROM hydro.hydro_run "
        "WHERE note = '(x' AND run_id = :r) AND rt.valid_time = :t",
    ),
    (
        "subselect_spelled_inside_a_literal",
        "SELECT rt.value, '(SELECT 1)' AS n FROM hydro.river_timeseries rt WHERE rt.run_key = :k",
    ),
    (
        "comment_inside_parens",
        "SELECT rt.value FROM hydro.river_timeseries rt WHERE rt.run_key = (/* pick */ SELECT run_key "
        "FROM hydro.hydro_run WHERE run_id = :r)",
    ),
    (
        "line_comment_with_apostrophe_before_a_scalar_subselect",
        "SELECT rt.value FROM hydro.river_timeseries rt WHERE rt.run_key -- don't stop\n"
        "= (SELECT run_key FROM hydro.hydro_run WHERE run_id = :r)",
    ),
    (
        "block_comment_with_apostrophe_before_a_scalar_subselect",
        "SELECT rt.value FROM hydro.river_timeseries rt WHERE rt.run_key /* don't stop */ "
        "= (SELECT run_key FROM hydro.hydro_run WHERE run_id = :r)",
    ),
    (
        "block_comment_holding_an_unbalanced_paren",
        "SELECT rt.value /* ( */ FROM hydro.river_timeseries rt WHERE rt.run_key = :k",
    ),
    (
        "quoted_identifier_holding_a_comment_opener",
        'SELECT rt.value AS "a--b", rt.unit AS "c/*d" FROM hydro.river_timeseries rt WHERE rt.run_key = :k',
    ),
)

#: Verifier I's I2 statements — the ones the family and the scanner lexed
#: DIFFERENTLY at 7be3e273, where the scanner had a dollar arm and the traversals
#: had none. They are kept here, and asserted to be REFUSED rather than compared,
#: because that is precisely how decision 18 closes I2: not by teaching the
#: second family a dollar arm that would then have to be kept in step with the
#: first, but by putting every statement that could expose the difference outside
#: the module's input domain. At 7be3e273 these are accepted and the two lexers
#: disagree on them; here they never reach a traversal at all.
_REFUSED_DIVERGENCE_CORPUS: tuple[tuple[str, str], ...] = (
    (
        "line_comment_inside_a_dollar_body",
        "SELECT $q$a -- b$q$ AS note, rt.value FROM hydro.river_timeseries rt WHERE rt.variable = %(v)s",
    ),
    (
        "block_comment_inside_a_dollar_body",
        "SELECT $q$a /* b$q$ AS note, rt.value FROM hydro.river_timeseries rt WHERE rt.variable = %(v)s */",
    ),
    (
        "unbalanced_paren_inside_a_dollar_body",
        "SELECT $q$($q$ AS note, rt.value FROM hydro.river_timeseries rt WHERE rt.run_key = %(k)s",
    ),
    (
        "non_ascii_dollar_tag_over_a_real_read",
        "SELECT $备注$don't$备注$ AS note, rt.value FROM hydro.river_timeseries rt "
        "WHERE rt.variable = E'q\\'x' AND rt.run_key = %(k)s",
    ),
)


def test_every_traversal_commutes_with_the_scanner_over_the_corpus() -> None:
    """The SECOND lexer family answers "where is the code" exactly as the scanner does.

    Not asserted as ``_blank_non_code(T(sql)) == T(_blank_non_code(sql,
    keep_literal_quotes=True))``, which fixture decision 18 sketches: that
    identity is FALSE at head for any statement containing a literal (the left
    side blanks the literal's quotes, the right side keeps them), and the
    repaired variant that normalises both sides is vacuous against exactly the
    mutants it has to kill — blanking FIRST deletes the comment the mutant would
    have mis-lexed, so a stripper with no block-comment arm passes. Recorded as a
    deviation.

    Asserted instead as agreement with a reference built FROM ``non_code_spans``
    (:func:`_opaque_runs`): each traversal must delete/keep exactly the runs the
    scanner calls non-code. That is the property decision 18 actually needs — one
    notion of "code" for the counter, the walk, the rename, the structural check
    and the strippers — and it goes red the moment a family member's private
    lexer disagrees with the shared one, in EITHER direction.

    ``strip_*`` are compared verbatim (no whitespace normalisation: they return
    the original text with non-comment runs untouched). ``outer_predicates`` is
    the one traversal that normalises whitespace itself, so its reference does
    the same ``_WHITESPACE.sub`` it does. ``assert_structurally_intact`` and
    ``sql_chains`` are compared as OUTCOMES against the pre-blanked text, which
    is what their own private paren loop has to agree with.

    Corpus: all 20 registered templates, both rendered variants of each, and the
    adversarial list above. Samples outside the declared subset are excluded by
    the module's own :func:`_lexical_subset_violation`, so this test says nothing
    about statements decision 18 refuses — and the count of exclusions is
    asserted to be 0 for the registry, which is the measurement decision 18 rests
    on.
    """
    corpus: list[tuple[str, str]] = []
    registry_excluded = 0
    for entry in REGISTRY:
        source = entry.source()
        variants = [(entry.key, source)]
        for store in ("legacy", "narrow"):
            try:
                variants.append((f"{entry.key}:{store}", render_river_ts_sql(source, store, entry=entry.key).sql))
            except RiverTemplateError as error:  # pragma: no cover - a registry entry that refuses is a red elsewhere
                raise AssertionError(f"{entry.key} does not render for {store}: {error}") from error
        for label, sql in variants:
            if _lexical_subset_violation(sql) is not None:
                registry_excluded += 1
                continue
            corpus.append((label, sql))

    assert registry_excluded == 0, (
        "decision 18 rests on 0/20 registered templates using `$` or a non-ASCII byte in code"
    )
    assert len(corpus) == 3 * len(REGISTRY) == 60

    adversarial_excluded = [label for label, sql in _ADVERSARIAL_CORPUS if _lexical_subset_violation(sql) is not None]
    assert adversarial_excluded == [], f"the adversarial corpus must stay inside the subset, got {adversarial_excluded}"
    corpus.extend(_ADVERSARIAL_CORPUS)

    # The shapes that made the two lexers disagree are refused, not compared.
    # This is the half of the assertion that is RED at 7be3e273: there they are
    # accepted, and `strip_comments` then truncates the statement at a `--` that
    # is really inside a literal.
    still_accepted = [label for label, sql in _REFUSED_DIVERGENCE_CORPUS if _lexical_subset_violation(sql) is None]
    assert still_accepted == [], (
        f"these shapes reach the traversal family instead of being refused: {still_accepted!r} — "
        "decision 18 closes I2 by keeping them out of the input domain, not by teaching the family a dollar arm"
    )

    for label, sql in corpus:
        assert strip_comments(sql) == _reference_strip_comments(sql), f"strip_comments disagrees on {label}"
        assert strip_scalar_subqueries(sql) == _reference_strip_subqueries(sql, comparison_position_only=True), (
            f"strip_scalar_subqueries disagrees on {label}"
        )
        assert strip_all_subqueries(sql) == _reference_strip_subqueries(sql, comparison_position_only=False), (
            f"strip_all_subqueries disagrees on {label}"
        )
        reference_outer = _WHITESPACE.sub(
            " ",
            _reference_strip_comments(_reference_strip_subqueries(sql, comparison_position_only=True)),
        ).strip()
        assert outer_predicates(sql) == reference_outer, f"outer_predicates disagrees on {label}"

        # The paren loop inside `assert_structurally_intact` is the family's
        # fifth member and re-lexes too. Against the pre-blanked text there is
        # nothing left for it to mis-lex, so the two must agree.
        blanked = _blank_non_code(sql, keep_literal_quotes=True)
        assert _outcome(lambda: assert_structurally_intact(sql, label, allow_markers=True)) == _outcome(
            lambda: assert_structurally_intact(blanked, label, allow_markers=True)
        ), f"assert_structurally_intact disagrees with itself on blanked text for {label}"

        blanked_chains = tuple(
            tuple(_WHITESPACE.sub(" ", _blank_non_code(conjunct, keep_literal_quotes=True)) for conjunct in chain)
            for chain in sql_chains(sql)
        )
        assert blanked_chains == sql_chains(blanked), f"sql_chains disagrees on {label}"


def test_the_text_identity_vocabulary_is_total_and_disjoint() -> None:
    """The moved constant group keeps its meaning at its new home (fixture decision 2)."""
    assert set(TEXT_IDENTITY_COLUMNS) == set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS) | {
        "basin_version_id",
        "river_segment_id",
        "unit",
        "quality_flag",
    }
    assert len(TEXT_IDENTITY_COLUMNS) == len(set(TEXT_IDENTITY_COLUMNS))
    assert set(TEXT_AID_COUNTERPARTS) == set(TEXT_IDENTITY_COLUMNS)


# ---------------------------------------------------------------------------
# positional placeholder arithmetic
# ---------------------------------------------------------------------------


def test_removed_positional_placeholders_are_reported_in_template_order() -> None:
    """Deleting an aid changes the caller's tuple arity, and psycopg2 says so only at execute time."""
    rendered = render_river_ts_sql(POSITIONAL_TEMPLATE, "narrow", entry="positional")

    assert rendered.removed_placeholders == (1, 3)
    assert rendered.sql.count("%s") == 3


def test_a_named_template_reports_no_removed_placeholders() -> None:
    assert render_river_ts_sql(NAMED_TEMPLATE, "narrow", entry="named").removed_placeholders == ()


def test_a_named_placeholder_is_not_mistaken_for_a_positional_one() -> None:
    """``%(scan_run_id)s`` contains no ``%s`` substring — pinned, not assumed."""
    template = (
        f"SELECT 1 FROM hydro.river_timeseries rt\nWHERE rt.run_key = %(run_key)s\n  {MARKER}\n"
        "  AND rt.run_id = %(scan_run_id)s"
    )

    assert render_river_ts_sql(template, "narrow", entry="named psycopg").removed_placeholders == ()


# ---------------------------------------------------------------------------
# aid-line grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "  AND rt.run_id = %s",
        "  rt.run_id = %(scan_run_id)s AND",
        "  AND ts.river_segment_id = seg.river_segment_id",
        "  AND ts.variable = :variable",
        "  AND rt.variable = 'q_down'",
        "  AND run_id = %s",
    ],
)
def test_the_normalised_aid_shapes_are_recognised(line: str) -> None:
    assert aid_conjunct(line) is not None


@pytest.mark.parametrize(
    "line",
    [
        "  WHERE run_id = :run_id",
        "  AND rt.run_id = %s AND rt.variable = 'q_down'",
        "  AND rt.run_key = %s",
        "  AND rt.variable_e = 'q_down'::hydro.river_variable",
        "  OR (rt.run_id = %(scan_run_id)s",
        "  AND rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %s)",
        "",
    ],
)
def test_a_line_that_is_not_exactly_one_aid_conjunct_is_rejected(line: str) -> None:
    assert aid_conjunct(line) is None


# ---------------------------------------------------------------------------
# Round-1 review (#1996): structural faults a clause keyword used to hide
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "template"),
    [
        (
            "clause keyword after the connective",
            f"""
                SELECT rt.value
                FROM hydro.river_timeseries rt
                WHERE rt.run_key = %s AND
                {MARKER}
                rt.run_id = %s
                LIMIT 1
            """,
        ),
        (
            "aid last in an ON chain, JOIN next",
            f"""
                SELECT h.run_id
                FROM hydro.hydro_run h
                JOIN hydro.river_timeseries r
                  ON r.run_key = h.run_key AND
                  {MARKER}
                  r.variable = 'q_down'
                JOIN core.river_network_version rnv
                  ON rnv.river_network_version_key = r.river_network_version_key
                WHERE h.status = %s
            """,
        ),
        (
            "aid the only ON conjunct, JOIN next",
            f"""
                SELECT h.run_id
                FROM hydro.hydro_run h
                JOIN hydro.river_timeseries r
                  ON
                  {MARKER}
                  r.variable = 'q_down'
                JOIN core.river_network_version rnv
                  ON rnv.river_network_version_key = r.river_network_version_key
                WHERE h.status = %s
            """,
        ),
        (
            "aid last in an ON chain, LEFT JOIN next",
            f"""
                SELECT h.run_id
                FROM hydro.hydro_run h
                JOIN hydro.river_timeseries r
                  ON r.run_key = h.run_key AND
                  {MARKER}
                  r.variable = 'q_down'
                LEFT JOIN core.basin_version bv
                  ON bv.basin_version_key = r.basin_version_key
                WHERE h.status = %s
            """,
        ),
        (
            "aid last in an ON chain, WHERE next",
            f"""
                SELECT h.run_id
                FROM hydro.hydro_run h
                JOIN hydro.river_timeseries r
                  ON r.run_key = h.run_key AND
                  {MARKER}
                  r.variable = 'q_down'
                WHERE h.status = %s
            """,
        ),
        (
            "another connective after the connective",
            f"""
                SELECT rt.value
                FROM hydro.river_timeseries rt
                WHERE rt.run_key = %s AND
                {MARKER}
                rt.run_id = %s
                AND rt.valid_time = %s
            """,
        ),
    ],
)
def test_an_aid_whose_connective_sits_on_the_line_above_is_refused(label: str, template: str) -> None:
    """``AND`` left stranded by the deletion, with neither ``)`` nor end of text after it.

    ``aid_conjunct`` accepts a bare aid line whose ``AND`` sits on the PREVIOUS
    line, so the deletion can leave ``WHERE x = %s AND LIMIT 1`` or ``… AND AND
    …``. Both are syntax errors, and the original fault list — dangling ``AND``
    before a bracket or at the end of the text — matched neither (review #1996,
    C4).

    The ON-chain shapes are the round-2 half (H6): an aid that was the last, or
    the only, conjunct of a ``JOIN … ON`` leaves ``AND JOIN`` / ``ON JOIN`` /
    ``AND LEFT JOIN`` / ``AND WHERE``, none of which the clause-keyword-only
    enumeration named. One keyword family, shared with the region-stop regex,
    covers all of them.
    """
    with pytest.raises(RiverTemplateError, match="before a keyword|doubled connective"):
        render_river_ts_sql(template, "narrow", entry=label)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t WHERE x = 1 AND LIMIT 1",
        "SELECT a FROM t WHERE x = 1 OR ORDER BY a",
        "SELECT a FROM t WHERE x = 1 AND AND y = 2",
        "SELECT a FROM t WHERE x = 1 OR AND y = 2",
        "SELECT a FROM t WHERE LIMIT 1",
        "SELECT a FROM t WHERE x = 1 AND GROUP BY a",
    ],
)
def test_the_structural_check_rejects_a_chain_that_lost_its_last_conjunct(sql: str) -> None:
    with pytest.raises(RiverTemplateError):
        assert_structurally_intact(sql, "fault")


def test_the_structural_check_still_accepts_the_shapes_that_are_legal() -> None:
    """Guard against the new patterns firing on ordinary SQL."""
    for sql in (
        "SELECT a FROM t WHERE x = 1 AND y = 2 LIMIT 1",
        "SELECT a FROM t WHERE ordering = 1 AND grouping_key = 2 ORDER BY a",
        "SELECT a FROM t WHERE (x = 1 OR (y = 2 AND z = 3)) GROUP BY a",
        "SELECT a FROM t WHERE x = 1 UNION ALL SELECT a FROM u WHERE y = 2",
        # The reason the outer-join arm is spelled `(LEFT|RIGHT|FULL)\s+JOIN`
        # rather than listing the bare words: they are also string functions,
        # and a bare listing reports this line as a dangling connective
        # (round-2 H6).
        "SELECT a FROM t JOIN u ON u.k = t.k AND LEFT(r.tag, 3) = 'abc' WHERE x = 1",
        "SELECT RIGHT(a, 2) AS tail FROM t WHERE x = 1 AND LEFT(b, 1) = 'q'",
    ):
        assert_structurally_intact(sql, "legal")


# ---------------------------------------------------------------------------
# Round-1 review (#1996): the retention check no longer exempts by containment
# ---------------------------------------------------------------------------


def test_losing_the_authority_key_predicate_is_reported() -> None:
    """The aid ``run_id = :run_id`` is a SUBSTRING of the predicate it must not exempt.

    An unaliased statement spells its aid exactly as the authority sub-select's
    own required predicate, so the containment exemption let ``run_key = (SELECT
    run_key FROM hydro.hydro_run WHERE run_id = :run_id)`` exempt itself: the
    whole predicate could be deleted and every assert still passed (review #1996,
    C8). The other two checks are asserted to pass FIRST, so this test cannot be
    green for the wrong reason — the mutation is structurally clean and carries no
    text identity column, and only the retention check can see it.
    """
    template = f"""
        SELECT 1
        FROM hydro.river_timeseries
        WHERE run_key = (
                  SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id
              )
          {MARKER}
          AND run_id = :run_id
          AND valid_time = :valid_time
        LIMIT 1
    """
    narrow = render_river_ts_sql(template, "narrow", entry="probe").sql
    mutated = narrow.replace(
        """WHERE run_key = (
                  SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id
              )
          AND valid_time""",
        "WHERE valid_time",
        1,
    )
    assert mutated != narrow
    _sql, _placeholders, removed_aids = _strip_aids(template, "probe")

    assert_structurally_intact(mutated, "probe")
    _assert_no_fact_text_identity(mutated, "probe")

    with pytest.raises(RiverTemplateError, match="lost the predicate"):
        _assert_key_predicates_retained(template, mutated, removed_aids, "probe")


def test_a_conjunct_that_merely_holds_an_aid_is_still_accounted_for() -> None:
    """``EXISTS (… AND aid AND …)`` changes text when its aid goes — and must still balance.

    Exempting it wholesale is what the containment rule did; the check instead
    computes what it should look like WITHOUT the aid and requires that form to
    be present, so the guard's other conjuncts stay protected.
    """
    template = f"""
        SELECT h.run_id
        FROM hydro.hydro_run h
        WHERE EXISTS (
                  SELECT 1
                  FROM hydro.river_timeseries rt
                  WHERE rt.run_key = h.run_key
                    {MARKER}
                    AND rt.variable = 'q_down'
                    AND rt.variable_e = 'q_down'
                    AND rt.value IS NOT NULL
              )
    """
    rendered = render_river_ts_sql(template, "narrow", entry="exists-holder")

    assert "rt.variable = 'q_down'" not in rendered.sql
    assert "rt.variable_e = 'q_down'" in rendered.sql
    assert "rt.value IS NOT NULL" in rendered.sql

    stripped = rendered.sql.replace("AND rt.value IS NOT NULL", "")
    _sql, _placeholders, removed_aids = _strip_aids(template, "exists-holder")
    with pytest.raises(RiverTemplateError, match="lost the predicate"):
        _assert_key_predicates_retained(template, stripped, removed_aids, "exists-holder")


# ---------------------------------------------------------------------------
# Round-2 review (#1996): an independent reference counter
# ---------------------------------------------------------------------------


def test_the_independent_counter_ignores_comments_and_string_literals() -> None:
    sql = """
        SELECT value                      -- reads hydro.river_timeseries
        FROM hydro.river_timeseries
        WHERE note <> 'hydro.river_timeseries'
    """

    assert fact_table_name_occurrences(sql) == 1


def test_the_independent_counter_counts_a_quoted_identifier_and_a_table_name_qualifier() -> None:
    """A column qualifier spelled with the TABLE NAME is a counted mention (#2018 round-3 G2).

    It is not a second READ — but the counter's job is not to know that. The
    ``FROM`` / ``JOIN`` walk does not model a qualifier either, so counting it is
    what makes the two disagree, and the disagreement refuses a statement whose
    text identity columns the scan cannot attribute (and whose bare spelling the
    rename cannot follow). This is the counter half of that decision; the
    behavioural half is
    ``test_a_column_qualified_by_the_table_name_is_refused_so_the_qualifier_is_never_unseen``.
    """
    assert fact_table_name_occurrences('SELECT 1 FROM "hydro"."river_timeseries"') == 1
    assert fact_table_name_occurrences("... x.run_key = hydro.river_timeseries.run_key ...") == 1
    assert fact_table_name_occurrences("... x.run_key = hydro.river_timeseries_legacy.run_key ...") == 1
    # Quoted the same way: a double-quoted span is an IDENTIFIER, so the counter
    # reads the name inside it (:func:`non_code_spans` does not blank it).
    assert fact_table_name_occurrences('... x.run_key = hydro."river_timeseries"."run_key" ...') == 1


def test_the_token_counter_keeps_its_trailing_boundary() -> None:
    r"""``\b`` after the name: a SIBLING table whose name merely starts with it is not a mention.

    ``_FACT_NAME_TOKEN`` is ``\briver_timeseries(?:_legacy)?\b``. Drop the
    trailing ``\b`` and the engine backtracks out of ``(?:_legacy)?``, matching
    the ``river_timeseries`` PREFIX of ``river_timeseries_audit`` — so an
    ordinary join against a sibling table counts 2 against a walk that models 1
    and the statement is refused. Head behaviour is correct and, until this pin,
    unpinned in both directions: verifier #2018 round-4 I3 measured the mutant
    surviving all five suites (408 passed) at 7be3e273, while the SAME mutation
    is killed at 62f41fe0 — the barrier was removed by round 3's deletion of the
    qualifier lookahead, which used to catch the over-match downstream.

    The second assertion is the ``_valid_time_idx`` claim the ``_FACT_NAME_TOKEN``
    docstring makes in prose ("Not a token, and deliberately so"). It has to be a
    BARE identifier in code: the same name inside a string literal is blanked by
    the scanner and would pass under the mutant too, i.e. it would not
    discriminate.
    """
    sql = (
        "SELECT rt.value FROM hydro.river_timeseries rt "
        "JOIN hydro.river_timeseries_audit a ON a.run_key = rt.run_key "
        "WHERE rt.run_key = %(k)s"
    )

    assert fact_table_name_occurrences(sql) == 1
    assert fact_table_name_occurrences("SELECT 1 WHERE i = river_timeseries_valid_time_idx") == 0
    # Only the fact table is renamed; the sibling keeps its own name.
    legacy = render_river_ts_sql(sql, "legacy", entry="trailing-boundary").sql
    assert "hydro.river_timeseries_legacy rt" in legacy
    assert "hydro.river_timeseries_audit a" in legacy
    assert render_river_ts_sql(sql, "narrow", entry="trailing-boundary").sql == sql


def test_the_token_counter_keeps_its_leading_boundary() -> None:
    r"""``\b`` before the name: a table whose name merely ENDS with it is not a mention either.

    The twin of the trailing barrier, and the twin mutant verifier I measured
    surviving at head: without the leading ``\b`` the counter finds the name
    inside ``forcing_river_timeseries``, counts 2 against the walk's 1, and
    refuses a legal statement. Both boundaries are asserted with a render
    control, because the failure direction is a false REFUSAL — an assertion on
    the count alone would leave "and it still renders" as prose.
    """
    sql = (
        "SELECT rt.value FROM hydro.forcing_river_timeseries x "
        "JOIN hydro.river_timeseries rt ON rt.run_key = x.run_key "
        "WHERE rt.run_key = %(k)s"
    )

    assert fact_table_name_occurrences(sql) == 1
    legacy = render_river_ts_sql(sql, "legacy", entry="leading-boundary").sql
    assert "hydro.river_timeseries_legacy rt" in legacy
    assert "hydro.forcing_river_timeseries x" in legacy
    assert render_river_ts_sql(sql, "narrow", entry="leading-boundary").sql == sql


# ---------------------------------------------------------------------------
# Fixture probes over the rendered statement itself (#1996 retro)
#
# One test per row of the fixture's "Refused / decided shapes" table that the
# statement renderer owns: what counts as a NAME of the fact table (probe F /
# decision 14 — a name inside a literal or a comment is data, not a read) and
# which line-deletion artefacts the structural check must name. The registry
# sweep in `tests/test_sql_shape_helpers.py` is the false-positive control that
# says the check still admits every real read path.
# ---------------------------------------------------------------------------


def test_probe_f_the_table_name_inside_a_string_literal_is_left_verbatim() -> None:
    """Fixture probe F / decision 14: a name inside a literal is DATA.

    ``_rename_table`` substituted every occurrence, so the legacy branch shipped
    ``'reads hydro.river_timeseries_legacy'`` as a VALUE — a changed statement
    output — while the occurrence counter, which already ignored literals, saw
    nothing (round-3 L2-3).
    """
    template = """
        SELECT 'reads hydro.river_timeseries' AS note, rt.value
        FROM hydro.river_timeseries rt
        WHERE rt.valid_time = :valid_time
    """

    rendered = render_river_ts_sql(template, "legacy", entry="literal")

    assert "'reads hydro.river_timeseries' AS note" in rendered.sql
    assert "FROM hydro.river_timeseries_legacy rt" in rendered.sql
    assert rendered.sql.count(RIVER_TABLE_LEGACY) == 1


def test_probe_f_the_table_name_inside_a_comment_is_left_verbatim() -> None:
    """The other half of decision 14: comments name the canonical table on purpose."""
    template = """
        SELECT rt.value
        FROM hydro.river_timeseries rt
        -- #1342 renames hydro.river_timeseries; this comment is not SQL.
        WHERE rt.valid_time = :valid_time
    """

    rendered = render_river_ts_sql(template, "legacy", entry="comment")

    assert "-- #1342 renames hydro.river_timeseries; this comment is not SQL." in rendered.sql
    assert rendered.sql.count(RIVER_TABLE_LEGACY) == 1




@pytest.mark.parametrize(
    ("label", "sql"),
    [
        ("dangling connective before the statement terminator", "SELECT 1 FROM x WHERE a = 1 AND ;"),
        ("predicate spliced after the statement terminator", "SELECT 1 FROM x WHERE a = 1 ; AND b = 2"),
        ("a conjunct spliced into a row-count clause", "SELECT 1 FROM x WHERE a = 1 LIMIT 1 AND b = 2"),
        ("a conjunct spliced into a row-count clause", "SELECT 1 FROM x WHERE a = 1 OFFSET 10 AND b = 2"),
        ("a conjunct spliced into a row-count clause", "SELECT 1 FROM x WHERE a = 1 FETCH FIRST AND b = 2"),
    ],
)
def test_the_structural_check_names_the_terminator_and_row_count_faults(label: str, sql: str) -> None:
    """The faults a chain-end mutation produces, named rather than shipped silently."""
    with pytest.raises(RiverTemplateError, match=re.escape(label)):
        assert_structurally_intact(sql, "<test>")


@pytest.mark.parametrize("lock_clause", ["FOR UPDATE", "FOR NO KEY UPDATE", "FOR SHARE", "FOR KEY SHARE"])
def test_a_row_locking_clause_ends_the_chain_and_is_never_a_conjunct(lock_clause: str) -> None:
    """The ``FOR …`` arm of ``_KEYWORD_FAMILY``, pinned on both of its consumers.

    A row-locking clause is not a predicate. Without the arm, a chain read as
    continuing into it produces ``FOR UPDATE AND …`` — a syntax error at execute
    time that nothing textual noticed (round-3 L1-3) — and the golden form folds
    the clause into a conjunct, so moving a predicate across it is invisible to
    the equivalence oracle.

    Both consumers are asserted because the arm is ONE constant shared by them:
    the structural fault that catches a chain whose last conjunct was deleted,
    and the region stop that decides where a chain ends.
    """
    with pytest.raises(RiverTemplateError, match="dangling connective before a keyword"):
        assert_structurally_intact(f"SELECT a FROM t WHERE x = :v AND {lock_clause}", "<test>")

    assert sql_chains(f"SELECT a FROM t WHERE x = :v {lock_clause}") == (("x = :v",),)


def test_a_row_count_clause_with_no_conjunct_after_it_is_not_a_fault() -> None:
    """Non-vacuity: ``LIMIT 1`` at the end of a statement is the registry's own shape."""
    assert_structurally_intact("SELECT 1 FROM x WHERE a = 1 ORDER BY a LIMIT 1", "<test>")
    assert_structurally_intact("SELECT 1 FROM x WHERE a = 1 GROUP BY a HAVING COUNT(*) > 1 AND SUM(b) > 2", "<test>")


def test_the_structural_check_reads_code_not_string_literals() -> None:
    """A fault pattern spelled inside a literal is data (brief item 10).

    The check used to fold ``strip_comments(sql)``, which leaves literals intact,
    so a statement carrying the text of a fault was refused for carrying a value.
    """
    assert_structurally_intact("SELECT 'a = 1 AND ;' AS note FROM x WHERE a = 1", "<test>")
    assert_structurally_intact("SELECT rt.tag FROM x rt WHERE rt.tag = 'LIMIT 1 AND b'", "<test>")
