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

import pytest

from packages.common.river_ts_render import (
    PUSHDOWN_AID_MARKER,
    RIVER_TABLE,
    RIVER_TABLE_LEGACY,
    SANCTIONED_TEXT_PUSHDOWN_COLUMNS,
    TEXT_AID_COUNTERPARTS,
    TEXT_IDENTITY_COLUMNS,
    RiverTemplateError,
    _assert_bound_per_scope,
    _assert_key_predicates_retained,
    _assert_no_fact_text_identity,
    _bind_store,
    _strip_aids,
    aid_conjunct,
    assert_structurally_intact,
    fact_table_attribution,
    fact_table_name_occurrences,
    fact_table_text_identity_columns,
    render_river_ts_sql,
    render_union_all,
    store_binding_form,
    store_binding_forms,
    store_binding_plan,
)

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
# render_union_all
# ---------------------------------------------------------------------------


def test_the_union_combinator_refuses_a_positional_template() -> None:
    """Fixture decision 4: two branches would silently need the caller's tuple twice."""
    with pytest.raises(RiverTemplateError, match="named-parameter templates only"):
        render_union_all(POSITIONAL_TEMPLATE, ("legacy", "narrow"), {}, entry="positional")


def test_the_union_combinator_binds_one_store_per_branch() -> None:
    rendered = render_union_all(
        NAMED_TEMPLATE,
        ("legacy", "narrow"),
        {"run_id": "run_a", "valid_time": "2026-05-07T00:00:00Z"},
        entry="named",
    )

    assert rendered.sql.count("UNION ALL") == 1
    assert rendered.sql.count("timeseries_store = 'legacy'") == 1
    assert rendered.sql.count("timeseries_store = 'narrow'") == 1
    # The legacy branch keeps its aid; the narrow branch does not.
    legacy_branch, narrow_branch = rendered.sql.split("UNION ALL")
    assert RIVER_TABLE_LEGACY in legacy_branch
    assert "ts.run_id = :run_id" in legacy_branch
    assert RIVER_TABLE_LEGACY not in narrow_branch
    assert "ts.run_id" not in narrow_branch
    # Both branches resolve the run through the authority table under the SAME
    # parameter name, which is what lets one caller mapping bind the statement.
    assert legacy_branch.count(":run_id") == 2
    assert narrow_branch.count(":run_id") == 1


def test_the_union_combinator_shares_the_parameters_across_branches() -> None:
    """Orchestrator ruling on D1: the caller's mapping goes through UNCHANGED.

    Both dialects allow a named placeholder to repeat, so two branches can spell
    ``:run_id`` and bind one value. The rejected alternative renamed each branch's
    parameters to ``<name>_<store>``, which would have made every I3 caller build
    a mapping keyed by store — a contract leak for a per-branch window nothing
    asks for.
    """
    params = {"run_id": "run_a", "valid_time": "2026-05-07T00:00:00Z"}

    rendered = render_union_all(NAMED_TEMPLATE, ("legacy", "narrow"), params, entry="named")

    assert rendered.params == params
    # No branch-suffixed name leaked into the statement, in either dialect.
    assert ":run_id_" not in rendered.sql
    assert ":valid_time_" not in rendered.sql
    assert "%(" not in rendered.sql
    # One name, bound once by the caller, read by both branches.
    assert rendered.sql.count(":valid_time") == len(rendered.branches) == 2


def test_the_union_combinator_leaves_the_callers_mapping_alone() -> None:
    """Returned as a copy: a caller that adds a key to the result must not mutate its own dict."""
    params = {"run_id": "run_a", "valid_time": "2026-05-07T00:00:00Z"}

    rendered = render_union_all(NAMED_TEMPLATE, ("legacy", "narrow"), params, entry="named")
    rendered.params["run_id"] = "clobbered"

    assert params["run_id"] == "run_a"


def test_the_union_combinator_shares_psycopg_named_parameters_too() -> None:
    """The other named dialect: %(name)s repeats across branches just as :name does."""
    template = """
        SELECT ts.value
        FROM hydro.river_timeseries ts
        WHERE ts.run_key = (
                  SELECT run_key FROM hydro.hydro_run WHERE run_id = %(run_id)s
              )
          -- transitional compressed-chunk pushdown aid, remove with #1342
          AND ts.run_id = %(run_id)s
          AND ts.variable_e = %(variable_e)s
    """

    rendered = render_union_all(
        template, ("legacy", "narrow"), {"run_id": "a", "variable_e": "q_down"}, entry="psycopg"
    )

    assert rendered.params == {"run_id": "a", "variable_e": "q_down"}
    assert "%(run_id_legacy)s" not in rendered.sql
    assert rendered.sql.count("%(variable_e)s") == 2


def test_the_union_combinator_refuses_a_parameter_it_was_not_given() -> None:
    with pytest.raises(RiverTemplateError, match=r"not given values for \['valid_time'\]"):
        render_union_all(NAMED_TEMPLATE, ("legacy", "narrow"), {"run_id": "run_a"}, entry="named")


def test_the_union_combinator_binds_through_a_hydro_run_alias_when_the_scope_has_one() -> None:
    """The cheap form: the fact table's own scope already joins the authority table."""
    template = f"""
        SELECT h.run_id
        FROM hydro.hydro_run h
        JOIN hydro.river_timeseries r
          ON r.run_key = h.run_key
        WHERE h.status = :status
          {MARKER}
          AND r.variable = :variable
    """

    assert store_binding_form(template) == "alias"
    rendered = render_union_all(template, ("narrow",), {"status": "published", "variable": "q_down"}, entry="alias")
    assert rendered.branches == (("alias",),)
    assert "AND h.timeseries_store = 'narrow'" in rendered.sql


def test_the_union_combinator_falls_back_to_an_exists_when_the_scope_has_no_alias() -> None:
    """The correlated form, for the templates that never name ``hydro_run`` beside the fact table.

    Reported rather than chosen invisibly (:func:`store_binding_form`): the two
    forms are different plans, and which one a template gets is a property of the
    template a reviewer has to be able to read off.
    """
    assert store_binding_form(NAMED_TEMPLATE) == "exists"

    rendered = render_union_all(NAMED_TEMPLATE, ("narrow",), {"run_id": "a", "valid_time": "b"}, entry="exists")

    assert rendered.branches == (("exists",),)
    assert (
        "EXISTS (SELECT 1 FROM hydro.hydro_run store_route "
        "WHERE store_route.run_key = ts.run_key AND store_route.timeseries_store = 'narrow')"
    ) in rendered.sql


def test_the_exists_binding_qualifies_run_key_when_the_fact_table_has_no_alias() -> None:
    """The correlation must name the OUTER relation, or it degenerates to ``x = x``.

    ``mvt``'s two valid_times branches and ``hydro_display``'s probe give the fact
    table no alias. An unqualified ``store_route.run_key = run_key`` inside the
    sub-select resolves ``run_key`` against the SUB-SELECT's own ``hydro_run``
    (inner scope wins in PostgreSQL), so the predicate is trivially true and the
    branch silently returns EVERY run — the exact "mixed store" bug the whole
    combinator exists to prevent. The table name qualifies it instead, and it must
    be the PHYSICAL name of that branch.
    """
    template = f"""
        SELECT DISTINCT valid_time
        FROM hydro.river_timeseries
        WHERE variable_e = :variable_e
          {MARKER}
          AND variable = :variable
    """

    rendered = render_union_all(template, ("legacy", "narrow"), {"variable_e": "q", "variable": "q"}, entry="bare")

    assert (
        "store_route.run_key = hydro.river_timeseries_legacy.run_key "
        "AND store_route.timeseries_store = 'legacy'"
    ) in rendered.sql
    assert (
        "store_route.run_key = hydro.river_timeseries.run_key AND store_route.timeseries_store = 'narrow'"
    ) in rendered.sql
    assert "store_route.run_key = run_key" not in rendered.sql


def test_the_union_combinator_refuses_an_unknown_store() -> None:
    with pytest.raises(RiverTemplateError, match="unknown timeseries store"):
        render_union_all(NAMED_TEMPLATE, ("legacy", "sharded"), {"run_id": "a", "valid_time": "b"}, entry="named")


def test_the_union_combinator_refuses_an_empty_store_list() -> None:
    with pytest.raises(RiverTemplateError, match="at least one store"):
        render_union_all(NAMED_TEMPLATE, (), {"run_id": "a", "valid_time": "b"}, entry="named")


# ---------------------------------------------------------------------------
# Round-1 review (#1996): every scope that reads the fact table gets bound
# ---------------------------------------------------------------------------


def _bind(template: str, store: str) -> tuple[tuple[str, ...], str]:
    """The store binding on its own — the scope walk, without the aid deletion.

    Exercised directly because the walk is what the round-1 findings were about:
    going through :func:`render_union_all` would make every one of these tests
    also depend on the template being renderable, which is a different contract.
    """
    return _bind_store(template, store, "<test>")


C1_SELF_JOIN = f"""
    SELECT a.value
    FROM hydro.river_timeseries a
    JOIN hydro.river_timeseries b
      ON b.run_key = a.run_key
    WHERE a.valid_time = :valid_time
      {MARKER}
      AND a.variable = :variable
"""

C1_SCALAR_SUBQUERY = f"""
    SELECT rt.value
    FROM hydro.river_timeseries rt
    WHERE rt.value = (
              SELECT MAX(inner_ts.value)
              FROM hydro.river_timeseries inner_ts
              WHERE inner_ts.run_key = rt.run_key
                {MARKER}
                AND inner_ts.variable = :variable
          )
      AND rt.valid_time = :valid_time
"""


def test_a_self_join_binds_the_store_on_both_references() -> None:
    """One predicate for one of two reads is the fail-open, not a partial success.

    Before the round-1 fix the walk stopped at the first fact scope, so ``b`` was
    left free to resolve against the OTHER store while the return value reported
    the branch bound (review #1996, C1).
    """
    forms, bound = _bind(C1_SELF_JOIN, "narrow")

    assert fact_table_attribution(C1_SELF_JOIN).reference_count == 2
    assert len(forms) == 2
    assert bound.count("timeseries_store = 'narrow'") == 2
    assert "store_route.run_key = a.run_key" in bound
    assert "store_route.run_key = b.run_key" in bound


def test_a_scalar_subquery_reference_binds_inside_its_own_subquery() -> None:
    """The inner read is a scope of its own; its predicate has to land there, not outside."""
    forms, bound = _bind(C1_SCALAR_SUBQUERY, "narrow")

    assert len(forms) == 2
    assert bound.count("timeseries_store = 'narrow'") == 2
    # The inner binding is inside the sub-select's brackets, correlated on the
    # INNER alias; the outer one is outside them, correlated on the outer alias.
    closes_subquery = bound.index("AND rt.valid_time = :valid_time")
    assert bound.index("store_route.run_key = inner_ts.run_key") < closes_subquery
    assert bound.index("store_route.run_key = rt.run_key") > closes_subquery


def test_the_union_combinator_binds_every_reference_of_a_multi_scope_template() -> None:
    """Three reads, three predicates per branch — the mvt national shape, in miniature."""
    template = f"""
        WITH probe AS (
            SELECT ts.run_key
            FROM hydro.river_timeseries ts
            WHERE ts.valid_time = :valid_time
              {MARKER}
              AND ts.variable = :variable
        ),
        second AS (
            SELECT other.value
            FROM hydro.river_timeseries other
            WHERE other.valid_time = :valid_time
        )
        SELECT third.value
        FROM hydro.river_timeseries third
        WHERE third.valid_time = :valid_time
    """

    rendered = render_union_all(
        template, ("legacy", "narrow"), {"valid_time": "t", "variable": "q_down"}, entry="three"
    )

    assert fact_table_attribution(template).reference_count == 3
    assert rendered.branches == (("exists",) * 3, ("exists",) * 3)
    for store, branch in zip(("legacy", "narrow"), rendered.branch_sql):
        assert branch.count(f"timeseries_store = '{store}'") == 3


def test_a_fact_scope_with_no_chain_to_bind_on_is_refused() -> None:
    with pytest.raises(RiverTemplateError, match="has no chain of its own to bind the store on"):
        _bind("SELECT value FROM hydro.river_timeseries", "narrow")


def test_a_hydro_run_alias_inside_a_scalar_subquery_is_not_in_scope() -> None:
    """``hr`` is declared in the sub-select; naming it outside is PostgreSQL 42P01.

    The alias search used to read the whole bracket level, sub-selects included,
    and emitted ``AND hr.timeseries_store = …`` in the outer WHERE (review #1996,
    C2). Masking nested groups is what makes "this scope" mean this scope.
    """
    template = f"""
        SELECT ts.value
        FROM hydro.river_timeseries ts
        WHERE ts.run_key = (
                  SELECT hr.run_key FROM hydro.hydro_run hr WHERE hr.run_id = :run_id
              )
          {MARKER}
          AND ts.run_id = :run_id
    """

    assert store_binding_form(template) == "exists"
    assert store_binding_forms(template) == ("exists",)
    _forms, bound = _bind(template, "narrow")
    assert "hr.timeseries_store" not in bound


def test_a_hydro_run_alias_in_another_union_branch_is_not_in_scope() -> None:
    """UNION branches share a bracket level but not their aliases."""
    template = """
        SELECT h.run_id FROM hydro.hydro_run h WHERE h.status = :status
        UNION ALL
        SELECT rt.run_id FROM hydro.river_timeseries rt WHERE rt.valid_time = :valid_time
    """

    assert store_binding_forms(template) == ("exists",)
    _forms, bound = _bind(template, "narrow")
    assert "h.timeseries_store" not in bound


def test_a_leading_comment_does_not_slide_the_binding_into_another_branch() -> None:
    """Offsets must come from ONE coordinate system (review #1996, C3).

    The fact reference used to be located in comment-STRIPPED text and the chain
    in the original; each comment collapses to a single character, so a long
    comment ahead of the table slid the two apart far enough to bind the store
    predicate into the first UNION branch — referencing an alias that does not
    exist there and leaving the fact-table branch scanning both stores.
    """
    template = "\n    -- " + "x" * 300 + """
    SELECT o.value FROM other o WHERE o.flag = :flag
    UNION ALL
    SELECT rt.value FROM hydro.river_timeseries rt WHERE rt.run_key = :run_key
"""

    _forms, bound = _bind(template, "narrow")

    head, _, tail = bound.partition("UNION ALL")
    assert "timeseries_store" not in head
    assert tail.count("timeseries_store = 'narrow'") == 1
    assert "store_route.run_key = rt.run_key" in tail


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


def test_the_union_combinator_refuses_a_mixed_dialect_template() -> None:
    template = f"""
        SELECT ts.value
        FROM hydro.river_timeseries ts
        WHERE ts.run_key = %(run_key)s
          {MARKER}
          AND ts.run_id = :run_id
    """
    with pytest.raises(RiverTemplateError, match="mixes"):
        render_union_all(template, ("legacy", "narrow"), {"run_key": "k", "run_id": "r"}, entry="mixed")


# ---------------------------------------------------------------------------
# Round-2 review (#1996): the chain search is bounded and its kind is fixed
# ---------------------------------------------------------------------------


def test_a_fact_read_in_a_segment_without_a_where_is_refused_not_bound_next_door() -> None:
    """A sibling UNION segment's WHERE is not this segment's WHERE (round-2 H1).

    Searching to the end of the bracket level found segment 2's chain and spliced
    segment 1's predicate into it: segment 2 gets a second, redundant filter and
    segment 1 — the one that actually needed bounding — reads BOTH stores. Two
    wrongs that a total binding count reports as one right.
    """
    template = """
        SELECT rt.run_id FROM hydro.river_timeseries rt
        UNION ALL
        SELECT o.run_id FROM other o WHERE o.flag = :flag
    """

    with pytest.raises(RiverTemplateError, match="has no chain of its own to bind the store on"):
        _bind(template, "narrow")


def test_each_union_segment_is_bound_inside_its_own_segment() -> None:
    """Positional, not by count: the predicate has to land between ITS reference and its segment end."""
    template = """
        SELECT a.run_id FROM hydro.river_timeseries a WHERE a.valid_time = :valid_time
        UNION ALL
        SELECT b.run_id FROM hydro.river_timeseries b WHERE b.valid_time = :valid_time
    """

    forms, bound = _bind(template, "narrow")

    assert len(forms) == 2
    separator = bound.index("UNION ALL")
    first, second = bound[:separator], bound[separator:]
    assert first.count("timeseries_store = 'narrow'") == 1
    assert second.count("timeseries_store = 'narrow'") == 1
    assert "store_route.run_key = a.run_key" in first
    assert "store_route.run_key = b.run_key" in second


def test_a_from_reference_binds_in_the_where_even_when_a_later_join_has_an_on() -> None:
    """The chain kind follows the reference form; a later join's ON is not a candidate."""
    template = """
        SELECT ts.value
        FROM hydro.river_timeseries ts
        LEFT JOIN core.river_segment rs
          ON rs.river_segment_key = ts.river_segment_key
        WHERE ts.valid_time = :valid_time
    """

    _forms, bound = _bind(template, "narrow")

    on_at = bound.index("ON rs.river_segment_key")
    where_at = bound.index("WHERE ts.valid_time")
    predicate_at = bound.index("timeseries_store = 'narrow'")
    assert on_at < where_at < predicate_at


def test_a_join_reference_binds_in_its_own_on_even_when_the_query_has_a_where() -> None:
    template = """
        SELECT h.run_id
        FROM hydro.hydro_run h
        JOIN hydro.river_timeseries r
          ON r.run_key = h.run_key
        WHERE h.status = :status
    """

    forms, bound = _bind(template, "narrow")

    assert forms == ("alias",)
    assert bound.index("timeseries_store = 'narrow'") < bound.index("WHERE h.status")


def test_a_join_written_with_using_is_refused() -> None:
    """No ON of its own, and the outer WHERE is not a substitute (round-2 H2)."""
    template = """
        SELECT o.run_id
        FROM other o
        JOIN hydro.river_timeseries r USING (run_key)
        WHERE o.flag = :flag
    """

    with pytest.raises(RiverTemplateError, match="has no chain of its own to bind the store on"):
        _bind(template, "narrow")


def test_a_join_whose_next_chain_belongs_to_another_join_is_refused() -> None:
    template = """
        SELECT o.run_id
        FROM other o
        JOIN hydro.river_timeseries r USING (run_key)
        JOIN more m ON m.k = o.k
        WHERE o.flag = :flag
    """

    with pytest.raises(RiverTemplateError, match="has no chain of its own to bind the store on"):
        _bind(template, "narrow")


@pytest.mark.parametrize("join", ["LEFT JOIN", "LEFT OUTER JOIN", "RIGHT JOIN", "RIGHT OUTER JOIN", "FULL OUTER JOIN"])
def test_an_outer_join_onto_the_fact_table_is_refused_outright(join: str) -> None:
    """Neither placement is equivalent, so there is no correct answer to give (round-2 H2).

    In the ``ON`` chain the store predicate does not remove rows — an unmatched
    left row is NULL-extended and survives, so both branches emit it and the
    UNION doubles it. In the ``WHERE`` it removes exactly those NULL-extended
    rows, turning the outer join into an inner one. A reader that needs this
    rewrites the read as a sub-query; the renderer refuses rather than picking.
    """
    template = f"""
        SELECT h.run_id, r.value
        FROM hydro.hydro_run h
        {join} hydro.river_timeseries r
          ON r.run_key = h.run_key
        WHERE h.status = :status
    """

    with pytest.raises(RiverTemplateError, match="preserved side of an outer join"):
        _bind(template, "narrow")


def _lateral_template(join: str) -> str:
    return f"""
        SELECT seg.river_segment_key, v.value
        FROM tile_segments seg
        {join} (
            SELECT ts.value
            FROM hydro.river_timeseries ts
            WHERE ts.river_segment_key = seg.river_segment_key
              {MARKER}
              AND ts.river_segment_id = seg.river_segment_id
        ) v ON true
        WHERE seg.stream_type IS NULL
    """


@pytest.mark.parametrize(
    "join",
    ["LEFT JOIN LATERAL", "LEFT OUTER JOIN LATERAL", "RIGHT JOIN LATERAL", "FULL OUTER JOIN LATERAL"],
)
def test_a_fact_read_inside_an_outer_joined_lateral_is_refused(join: str) -> None:
    """The lateral's own WHERE is only a safe place under CROSS/INNER JOIN LATERAL.

    Binding inside the lateral filters the SUB-QUERY, which is correct — but under
    an outer join a driving row that matches in NEITHER store is still
    NULL-extended and survives, in BOTH branches, so the UNION emits it twice.
    The refusal that covers a direct ``LEFT JOIN hydro.river_timeseries`` cannot
    see this: the reference sits at the lateral's own bracket level, where nothing
    says how the parent opened it.
    """
    with pytest.raises(RiverTemplateError, match="opens with an OUTER JOIN"):
        _bind(_lateral_template(join), "narrow")


@pytest.mark.parametrize("join", ["CROSS JOIN LATERAL", "JOIN LATERAL", "INNER JOIN LATERAL"])
def test_a_fact_read_inside_a_non_outer_lateral_binds_inside_that_lateral(join: str) -> None:
    """Non-vacuity, and the registry's own shape: mvt's national probes are CROSS JOIN LATERAL.

    Asserted by position — the predicate has to be inside the lateral's brackets,
    in the lateral's own WHERE, not hoisted to the outer query where it would
    reference an alias that is out of scope there.
    """
    forms, bound = _bind(_lateral_template(join), "narrow")

    assert forms == ("exists",)
    predicate_at = bound.index("timeseries_store = 'narrow'")
    assert bound.index("WHERE ts.river_segment_key") < predicate_at < bound.index(") v ON true")
    assert "store_route.run_key = ts.run_key" in bound


def test_an_inner_join_onto_the_fact_table_is_not_mistaken_for_an_outer_one() -> None:
    """Non-vacuity for the refusal above, and for the `LEFT(col, n)` spelling."""
    template = """
        SELECT h.run_id, LEFT(h.run_id, 4) AS prefix
        FROM hydro.hydro_run h
        INNER JOIN hydro.river_timeseries r
          ON r.run_key = h.run_key
        WHERE h.status = :status
    """

    forms, _bound = _bind(template, "narrow")

    assert forms == ("alias",)


# ---------------------------------------------------------------------------
# Round-2 review (#1996): an independent reference counter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "template"),
    [
        (
            "comma join",
            """
            SELECT a.value
            FROM hydro.river_timeseries a, hydro.river_timeseries b
            WHERE a.run_key = b.run_key
            """,
        ),
        (
            "FROM ONLY",
            """
            SELECT value
            FROM ONLY hydro.river_timeseries
            WHERE valid_time = :valid_time
            """,
        ),
        (
            "quoted identifier",
            """
            SELECT value
            FROM "hydro"."river_timeseries"
            WHERE valid_time = :valid_time
            """,
        ),
        (
            "a second read with no FROM or JOIN in front of it",
            """
            SELECT rt.value
            FROM hydro.river_timeseries rt
            WHERE rt.valid_time = :valid_time
              AND rt.run_key IN (TABLE hydro.river_timeseries)
            """,
        ),
    ],
)
def test_a_reference_form_the_walk_does_not_model_is_refused(label: str, template: str) -> None:
    """Fail-closed on the unmodelled, rather than binding what it recognises and
    leaving the rest scanning both stores (round-2 H3).

    The counter is deliberately ignorant: it counts the NAME in comment- and
    string-blanked text, with no idea what a FROM clause is, so it can disagree
    with the structural walk. Quoted identifiers are refused rather than parsed —
    no registered template uses them, and accepting a spelling nothing exercises
    is how an unbound read gets shipped.
    """
    with pytest.raises(RiverTemplateError, match="refusing to bind a store on a partly-understood statement"):
        _bind(template, "narrow")


def test_the_independent_counter_ignores_comments_and_string_literals() -> None:
    sql = """
        SELECT value                      -- reads hydro.river_timeseries
        FROM hydro.river_timeseries
        WHERE note <> 'hydro.river_timeseries'
    """

    assert fact_table_name_occurrences(sql) == 1


def test_the_independent_counter_sees_a_quoted_identifier_and_not_a_column_qualifier() -> None:
    assert fact_table_name_occurrences('SELECT 1 FROM "hydro"."river_timeseries"') == 1
    # What the unaliased EXISTS binding emits: a column reference, not a read.
    assert fact_table_name_occurrences("... store_route.run_key = hydro.river_timeseries.run_key ...") == 0


# ---------------------------------------------------------------------------
# Round-2 review (#1996): the post-condition is per scope
# ---------------------------------------------------------------------------


def test_the_post_condition_rejects_two_predicates_in_one_scope_and_none_in_another() -> None:
    """The shape a total count cannot distinguish from a correct binding (round-2 H5).

    The forgery binds the right NUMBER of predicates — two references, two
    predicates — and puts both in the WHERE, leaving the ON chain that governs
    ``b`` unbound. What catches it is the ANCHOR post-condition: ``b``'s
    predicate has to follow ``b``'s chain's last conjunct, and in the forgery
    nothing does. That is a stronger check than the per-scope count it replaced
    (which is kept below as the re-walk's backstop), and it is stronger in the
    right direction: it names the chain the predicate was supposed to be in
    rather than counting what ended up somewhere.
    """
    template = """
        SELECT a.value
        FROM hydro.river_timeseries a
        JOIN hydro.river_timeseries b
          ON b.run_key = a.run_key
        WHERE a.valid_time = :valid_time
    """
    forged = """
        SELECT a.value
        FROM hydro.river_timeseries a
        JOIN hydro.river_timeseries b
          ON b.run_key = a.run_key
        WHERE a.valid_time = :valid_time
          AND EXISTS (SELECT 1 FROM hydro.hydro_run store_route
                      WHERE store_route.run_key = a.run_key AND store_route.timeseries_store = 'narrow')
          AND EXISTS (SELECT 1 FROM hydro.hydro_run store_route
                      WHERE store_route.run_key = b.run_key AND store_route.timeseries_store = 'narrow')
    """

    assert forged.count("timeseries_store = 'narrow'") == 2

    with pytest.raises(RiverTemplateError, match="did not land immediately after its chain's last conjunct"):
        _assert_bound_per_scope(template, forged, "narrow", "forged", store_binding_plan(template, entry="forged"))


def test_the_post_condition_rejects_a_branch_with_more_predicates_than_references() -> None:
    """The count arm, reached when the anchors are satisfied and one is spurious."""
    template = """
        SELECT a.value
        FROM hydro.river_timeseries a
        WHERE a.valid_time = :valid_time
    """
    forged = """
        SELECT a.value
        FROM hydro.river_timeseries a
        WHERE a.valid_time = :valid_time
          AND EXISTS (SELECT 1 FROM hydro.hydro_run store_route
                      WHERE store_route.run_key = a.run_key AND store_route.timeseries_store = 'narrow')
          AND EXISTS (SELECT 1 FROM hydro.hydro_run store_route
                      WHERE store_route.run_key = a.run_key AND store_route.timeseries_store = 'narrow')
    """

    with pytest.raises(RiverTemplateError, match=r"bound 2 store predicate\(s\) for 1 fact-table reference"):
        _assert_bound_per_scope(template, forged, "narrow", "forged", store_binding_plan(template, entry="forged"))


# ---------------------------------------------------------------------------
# Round-3 redesign (#1996 retro): the binder is a POSITIVE WHITELIST
#
# One test per row of the fixture's "Refused / decided shapes" table. Three
# rounds of review each found the same class — a fact read bound in a context
# where the binding is not semantics-preserving under UNION ALL — because the
# rule was an accretion of refused spellings and each fix closed exactly the
# spelling it was shown. These probes are that table; the registry sweep in
# `tests/test_sql_shape_helpers.py` is the false-positive control that says the
# whitelist still admits every real read path.
# ---------------------------------------------------------------------------


def test_probe_a_a_fact_read_on_the_nullable_side_of_an_outer_join_is_refused() -> None:
    """Fixture probe A: the read is in a SUB-QUERY, one level below the outer join.

    The refusal that covers a direct ``LEFT JOIN hydro.river_timeseries`` cannot
    see this — the reference sits at the sub-query's own bracket level, where
    nothing says how the parent opened it — so the context check reads the
    ANCESTOR OPENER instead of the sibling text (round-3 L2-1).
    """
    template = """
        SELECT h.run_id, s.value
        FROM hydro.hydro_run h
        LEFT JOIN (
            SELECT rt.run_key, rt.value
            FROM hydro.river_timeseries rt
            WHERE rt.valid_time = :valid_time
        ) s ON s.run_key = h.run_key
        WHERE h.status = :status
    """

    with pytest.raises(RiverTemplateError, match="opens with an OUTER JOIN"):
        _bind(template, "narrow")


def test_probe_b_a_fact_read_inside_an_outer_joins_on_expression_is_refused() -> None:
    """Fixture probe B: the bracket's own opener is ``ON EXISTS (``, which looks innocent.

    ``ON`` does not open a clause, it CONTINUES the join that precedes it, so the
    opener is read back over it to the ``LEFT JOIN``. Without that step the
    opener is ``ON EXISTS`` — no outer join in sight — and the read binds inside
    an outer join's condition, where the store predicate cannot remove a row:
    an unmatched left row is NULL-extended in BOTH branches and the union emits
    it twice.
    """
    template = """
        SELECT h.run_id, x.tag
        FROM hydro.hydro_run h
        LEFT JOIN other x
          ON EXISTS (
                 SELECT 1
                 FROM hydro.river_timeseries rt
                 WHERE rt.run_key = x.run_key
                   AND rt.valid_time = :valid_time
             )
        WHERE h.status = :status
    """

    with pytest.raises(RiverTemplateError, match="opens with an OUTER JOIN"):
        _bind(template, "narrow")


def test_probe_c_an_outer_joined_lateral_is_refused_through_a_redundant_bracket() -> None:
    """Fixture probe C: the same LATERAL, two levels down.

    The round-2 rule looked at "the parent level's text before the ``(``" and
    was therefore defeated by one redundant pair of brackets — reported as out of
    scope at the time, and the reason the retro ruled this a redesign rather than
    another spelling. The whole ancestor chain is checked, so depth buys nothing.
    """
    template = """
        SELECT seg.river_segment_key, p.value
        FROM tile_segments seg
        LEFT JOIN LATERAL (
            SELECT q.value
            FROM (
                SELECT rt.value
                FROM hydro.river_timeseries rt
                WHERE rt.river_segment_key = seg.river_segment_key
                  AND rt.valid_time = :valid_time
            ) q
        ) p ON TRUE
        WHERE seg.stream_type IS NOT NULL
    """

    with pytest.raises(RiverTemplateError, match="opens with an OUTER JOIN"):
        _bind(template, "narrow")


def test_probe_l_a_fact_read_inside_not_exists_is_refused() -> None:
    """Fixture probe L: negation inverts the predicate's sense.

    Narrowing the sub-select to one store makes it find FEWER rows, so
    ``NOT EXISTS`` becomes TRUE for more outer rows — each branch keeps rows the
    single-store statement drops, and the union keeps them twice.
    """
    template = """
        SELECT h.run_id
        FROM hydro.hydro_run h
        WHERE h.status = :status
          AND NOT EXISTS (
              SELECT 1
              FROM hydro.river_timeseries rt
              WHERE rt.run_key = h.run_key
                AND rt.valid_time = :valid_time
          )
    """

    with pytest.raises(RiverTemplateError, match="NEGATED context"):
        _bind(template, "narrow")


def test_probe_m_a_fact_read_inside_not_in_is_refused() -> None:
    """Fixture probe M: the same inversion, spelled ``NOT IN (SELECT …)``."""
    template = """
        SELECT h.run_id
        FROM hydro.hydro_run h
        WHERE h.status = :status
          AND h.run_key NOT IN (
              SELECT rt.run_key
              FROM hydro.river_timeseries rt
              WHERE rt.valid_time = :valid_time
          )
    """

    with pytest.raises(RiverTemplateError, match="NEGATED context"):
        _bind(template, "narrow")


@pytest.mark.parametrize("operator", ["EXCEPT", "EXCEPT ALL", "INTERSECT", "INTERSECT ALL"])
def test_probe_n_a_fact_read_in_the_right_operand_of_except_or_intersect_is_refused(operator: str) -> None:
    """Fixture probe N: the right operand's rows move the result the wrong way.

    ``A EXCEPT B`` shrinks as ``B`` grows. Splitting ``B`` by store gives each
    branch a SMALLER ``B``, so each branch keeps rows the whole-data statement
    subtracts — and the union then keeps them twice over.
    """
    template = f"""
        SELECT h.run_key
        FROM hydro.hydro_run h
        WHERE h.status = :status
        {operator}
        SELECT rt.run_key
        FROM hydro.river_timeseries rt
        WHERE rt.valid_time = :valid_time
    """

    with pytest.raises(RiverTemplateError, match="RIGHT operand of"):
        _bind(template, "narrow")


def test_a_fact_read_in_a_union_all_operand_is_still_bound() -> None:
    """Non-vacuity for probe N: ``UNION`` keeps every operand's rows, so it is safe.

    A blanket "no set operator above the reference" rule would refuse this, and
    this is the shape the combinator itself PRODUCES — it would refuse its own
    output.
    """
    template = """
        SELECT h.run_key
        FROM hydro.hydro_run h
        WHERE h.status = :status
        UNION ALL
        SELECT rt.run_key
        FROM hydro.river_timeseries rt
        WHERE rt.valid_time = :valid_time
    """

    forms, bound = _bind(template, "narrow")

    assert forms == ("exists",)
    assert bound.index("rt.valid_time = :valid_time") < bound.index("timeseries_store = 'narrow'")


def test_probe_o_a_data_modifying_statement_is_refused_as_a_union_operand() -> None:
    """Fixture probe O: a UNION ALL operand has to be a READ.

    ``display_coverage:refresh`` — a ``WITH … INSERT INTO … ON CONFLICT …
    RETURNING run_id`` — was rendered as a UNION ALL operand and passed the whole
    registry sweep, because the sweep asserted binding PLACEMENT only (round-3
    L1-1). Duplicating a write into two branches executes it twice.
    """
    template = """
        INSERT INTO hydro.run_display_coverage (run_id, segment_count)
        SELECT rt.run_id, COUNT(*)
        FROM hydro.river_timeseries rt
        WHERE rt.valid_time = :valid_time
        GROUP BY rt.run_id
        RETURNING run_id
    """

    with pytest.raises(RiverTemplateError, match="read statements only"):
        render_union_all(template, ("legacy", "narrow"), {"valid_time": "t"}, entry="dml")


def test_a_data_modifying_cte_is_refused_even_though_the_outer_statement_reads() -> None:
    """The depth the fixture's "top-level" wording would have missed.

    ``WITH moved AS (INSERT … RETURNING …) SELECT …`` is a data-modifying CTE: it
    executes once per branch, so the write happens twice while the statement
    reads like a SELECT. The keyword scan therefore runs at every depth.
    """
    template = """
        WITH moved AS (
            INSERT INTO hydro.river_timeseries_archive (run_key)
            SELECT run_key FROM staging
            RETURNING run_key
        )
        SELECT rt.value
        FROM hydro.river_timeseries rt
        WHERE rt.run_key IN (SELECT run_key FROM moved)
          AND rt.valid_time = :valid_time
    """

    with pytest.raises(RiverTemplateError, match="read statements only"):
        render_union_all(template, ("legacy", "narrow"), {"valid_time": "t"}, entry="dml-cte")


def test_a_data_modifying_keyword_inside_a_literal_or_comment_is_not_a_write() -> None:
    """Non-vacuity for probe O: the keyword scan reads CODE, not prose.

    Every registered template is full of prose about the migration; a scan over
    raw text would refuse them wholesale, and a rule that refuses everything is
    indistinguishable from no rule at all.
    """
    template = """
        SELECT rt.value, 'DELETE happens in the parser' AS note
        FROM hydro.river_timeseries rt
        -- The INSERT side of this table is the write guard's problem (#1985).
        WHERE rt.valid_time = :valid_time
    """

    rendered = render_union_all(template, ("legacy", "narrow"), {"valid_time": "t"}, entry="prose")

    assert rendered.branches == (("exists",), ("exists",))


def test_probe_u_a_declared_union_unsafe_template_is_refused_with_its_reason() -> None:
    """Fixture probe U: statement-level equivalence is DECLARED, never inferred.

    Whether a result decomposes per store depends on what the consumer does with
    the rows, which is not in the text. So the caller declares it, and the
    renderer's refusal carries the declaration's own reason rather than a generic
    objection a reader would have to go and interpret.
    """
    reason = "SELECT DISTINCT … ORDER BY … LIMIT: a per-branch top-N is not the global top-N"

    with pytest.raises(RiverTemplateError, match="per-branch top-N is not the global top-N"):
        render_union_all(
            NAMED_TEMPLATE,
            ("legacy", "narrow"),
            {"run_id": "a", "valid_time": "b"},
            entry="unsafe",
            union_safe=False,
            union_unsafe_reason=reason,
        )


def test_a_union_unsafe_declaration_with_no_reason_still_refuses() -> None:
    """Fail-closed on a half-made declaration, and say that it is half-made."""
    with pytest.raises(RiverTemplateError, match="no reason was declared"):
        render_union_all(
            NAMED_TEMPLATE,
            ("legacy",),
            {"run_id": "a", "valid_time": "b"},
            entry="unsafe",
            union_safe=False,
        )


def test_a_reason_declared_alongside_union_safe_is_refused_as_a_contradiction() -> None:
    """The declaration's invariant, enforced where it is consumed.

    A reason attached to a SAFE entry is either a stale reason someone forgot to
    delete when they flipped the flag, or a flag someone forgot to flip when they
    wrote the reason. Either way the entry's declared meaning is ambiguous, so it
    is refused rather than resolved by preferring one field.
    """
    with pytest.raises(RiverTemplateError, match="the reason exists only to explain a refusal"):
        render_union_all(
            NAMED_TEMPLATE,
            ("legacy",),
            {"run_id": "a", "valid_time": "b"},
            entry="contradiction",
            union_safe=True,
            union_unsafe_reason="left over from when this was unsafe",
        )


def test_probe_s_the_chain_ends_before_a_statement_terminator() -> None:
    """Fixture probe S: a predicate spliced after ``;`` is not in the statement.

    ``_chain_end`` stopped only at the region-stop keyword family, which has no
    ``;``, so the splice landed past the terminator and no structural fault named
    the result (round-3 L1-3).
    """
    template = """
        SELECT rt.value
        FROM hydro.river_timeseries rt
        WHERE rt.valid_time = :valid_time;
    """

    _forms, bound = _bind(template, "narrow")

    assert bound.index("timeseries_store = 'narrow'") < bound.index(";")
    assert_structurally_intact(bound, "<test>")


@pytest.mark.parametrize("lock", ["FOR UPDATE", "FOR NO KEY UPDATE", "FOR SHARE", "FOR KEY SHARE"])
def test_probe_s_the_chain_ends_before_a_row_locking_clause(lock: str) -> None:
    """Fixture probe S, second half: a lock clause is not a predicate.

    ``… WHERE x = :v FOR UPDATE`` spliced blindly becomes ``FOR UPDATE AND
    h.timeseries_store = '…'``, which is a syntax error the database reports and
    nothing textual noticed.
    """
    template = f"""
        SELECT rt.value
        FROM hydro.river_timeseries rt
        WHERE rt.valid_time = :valid_time
        {lock}
    """

    _forms, bound = _bind(template, "narrow")

    assert bound.index("timeseries_store = 'narrow'") < bound.index(lock)
    assert_structurally_intact(bound, "<test>")


@pytest.mark.parametrize(
    "chain",
    [
        "WHERE rt.run_id = :run_id OR rt.valid_time = :valid_time",
        "WHERE rt.run_id = :run_id AND rt.a = :a OR rt.valid_time = :valid_time",
    ],
    ids=["bare-or", "or-after-and"],
)
def test_a_governing_chain_that_is_a_top_level_disjunction_is_refused(chain: str) -> None:
    """The one hazard about what the predicate ASSOCIATES with, not where it lands.

    ``AND`` binds tighter than ``OR``, so appending ``AND <store>`` to ``WHERE a
    OR b`` parses as ``a OR (b AND <store>)``: the ``a`` rows are unfiltered,
    survive in BOTH branches and the union emits them twice. Nothing else caught
    it — ``_conjuncts`` splits on top-level ``AND`` only, so ``a OR b`` is ONE
    conjunct and the anchor post-condition saw a well-formed ``<anchor> AND
    <predicate>``, and no structural fault matches a correctly-spelled
    conjunction either (round-4 self-review).
    """
    template = f"""
        SELECT rt.value
        FROM hydro.river_timeseries rt
        {chain}
    """

    with pytest.raises(RiverTemplateError, match="top-level DISJUNCTION"):
        _bind(template, "narrow")


def test_a_join_condition_that_is_a_top_level_disjunction_is_refused() -> None:
    """Same precedence hazard in a JOIN's own ON chain."""
    template = """
        SELECT x.k, rt.value
        FROM x
        JOIN hydro.river_timeseries rt ON rt.run_key = x.run_key OR rt.basin_version_key = x.bk
        WHERE x.flag = :flag
    """

    with pytest.raises(RiverTemplateError, match="top-level DISJUNCTION"):
        _bind(template, "narrow")


def test_a_bracketed_disjunction_still_binds() -> None:
    """Non-vacuity, and the register's own shape — D5 brackets every ``OR``.

    ``forecast_store:latest_product_fallback`` and ``display_coverage:refresh``
    write their ``OR (rt.run_id = … AND rt.run_key = (…))`` disjunctions inside
    brackets, which makes them ONE conjunct that associates correctly. Refusing
    every ``OR`` would refuse them, so the check reads the MASKED chain, where a
    bracketed group is blanked out.
    """
    template = """
        SELECT rt.value
        FROM hydro.river_timeseries rt
        WHERE rt.valid_time = :valid_time AND (rt.run_id = :run_id OR rt.run_key = :run_key)
    """

    forms, bound = _bind(template, "narrow")

    assert forms == ("exists",)
    assert bound.index("rt.run_key = :run_key)") < bound.index("timeseries_store = 'narrow'")


@pytest.mark.parametrize(
    "correlation",
    [
        "ON h.run_key = rt.run_key OR h.run_key IS NULL\n        WHERE",
        "ON h.run_id = rt.run_id_x\n        WHERE NOT h.run_key = rt.run_key AND",
    ],
    ids=["or-disjunct", "bare-not"],
)
def test_a_correlation_that_is_not_an_established_conjunct_does_not_earn_the_alias_form(
    correlation: str,
) -> None:
    """Condition 4 asked whether the equality APPEARS, not whether it HOLDS.

    ``h.run_key = rt.run_key OR h.run_key IS NULL`` and ``NOT h.run_key =
    rt.run_key`` both contain the correlation as a substring and neither
    establishes it, so ``h`` is not this fact row's run and
    ``h.timeseries_store = '<store>'`` routes by the wrong one — probe J's exact
    hazard, spelled with an operator instead of a link table (round-4
    self-review). The correlation must now BE a top-level conjunct of one of the
    scope's chains; anything else falls back to the correlated EXISTS.
    """
    template = f"""
        SELECT rt.value
        FROM hydro.river_timeseries rt
        JOIN hydro.hydro_run h {correlation} rt.valid_time = :valid_time
    """

    forms, bound = _bind(template, "narrow")

    assert forms == ("exists",)
    assert "h.timeseries_store" not in bound
    assert "EXISTS" in bound


@pytest.mark.parametrize("lock", ["FOR UPDATE", "FOR NO KEY UPDATE", "FOR SHARE", "FOR KEY SHARE"])
def test_a_locking_read_is_refused_as_a_union_operand(lock: str) -> None:
    """Probe S at the STATEMENT seam: the chain-end rule is not the whole answer.

    ``_chain_end`` keeps the store predicate ahead of the lock clause, which is
    what the probe above pins — and the resulting statement is still one
    PostgreSQL rejects outright: a locking clause is allowed on neither a UNION
    result nor a UNION input. Before this refusal existed, ``FOR UPDATE`` /
    ``FOR NO KEY UPDATE`` were caught only incidentally, by ``_DATA_MODIFYING``
    matching the ``UPDATE`` token, and answered with a message calling the
    statement data-modifying; ``FOR SHARE`` / ``FOR KEY SHARE`` carry no such
    token and were rendered straight through into a union that cannot execute
    (round-4 finding).
    """
    template = f"""
        SELECT rt.value
        FROM hydro.river_timeseries rt
        WHERE rt.valid_time = %(valid_time)s
        {lock}
    """

    with pytest.raises(RiverTemplateError, match="accepts unlocked reads only"):
        render_union_all(template, ("legacy", "narrow"), {"valid_time": "t"}, entry="lock")


def test_a_terminated_statement_is_refused_as_a_union_operand() -> None:
    """A ``;`` inside a parenthesised branch is a syntax error, not a union.

    Each branch is wrapped as ``(\n…\n)``, so the terminator ends up INSIDE the
    parentheses. The binder placed its predicate correctly (probe S) and the
    structural check saw no fault — ``;`` is not followed by ``AND`` — so the
    function returned invalid SQL (round-4 finding). Refusing is the fail-closed
    reading: a template is handed over as one statement, and ``SELECT 1; SELECT
    2`` is not something a single UNION ALL can duplicate at all.
    """
    template = """
        SELECT rt.value
        FROM hydro.river_timeseries rt
        WHERE rt.valid_time = %(valid_time)s;
    """

    with pytest.raises(RiverTemplateError, match="accepts ONE unterminated statement"):
        render_union_all(template, ("legacy", "narrow"), {"valid_time": "t"}, entry="semi")


def test_a_right_join_outside_the_subquery_holding_the_fact_read_is_refused() -> None:
    """The preserved-other-side check has to read the ANCESTRY, not one segment.

    One bracket is enough to hide the shape: the sub-query's opener is a bare
    ``FROM`` (no outer join for the opener-chain rule to find) and its own
    segment holds no join at all, so the read classified and bound. Meanwhile the
    ``other``-only rows are NULL-extended in BOTH branches and the union emits
    them twice — the exact failure class the retro named, one level down
    (round-4 finding).
    """
    template = """
        SELECT s.value, o.tag
        FROM (
            SELECT rt.run_key, rt.value
            FROM hydro.river_timeseries rt
            WHERE rt.valid_time = :valid_time
        ) s
        RIGHT JOIN other o ON o.run_key = s.run_key
        WHERE o.flag = :flag
    """

    with pytest.raises(RiverTemplateError, match="preserves the OTHER relation"):
        _bind(template, "narrow")


def test_a_left_join_outside_that_subquery_still_binds() -> None:
    """Non-vacuity for the ancestry check: only RIGHT/FULL preserve the other side."""
    template = """
        SELECT s.value, o.tag
        FROM (
            SELECT rt.run_key, rt.value
            FROM hydro.river_timeseries rt
            WHERE rt.valid_time = :valid_time
        ) s
        LEFT JOIN other o ON o.run_key = s.run_key
        WHERE o.flag = :flag
    """

    forms, bound = _bind(template, "narrow")

    assert forms == ("exists",)
    assert bound.index("rt.valid_time = :valid_time") < bound.index("timeseries_store = 'narrow'")


def test_probe_j_a_hydro_run_alias_reached_through_a_link_table_does_not_earn_the_alias_form() -> None:
    """Fixture probe J: the alias is in scope, and it is the WRONG RUN.

    ``h`` is joined to the LINK table's parent key, not to ``rt``, so
    ``h.timeseries_store = '…'`` routes by the parent run while looking perfectly
    correct — the docstring's own named hazard, which the "is there a hydro_run
    alias in scope" test could not see (round-3 L2-2). The correlation
    ``<h>.run_key = <a>.run_key`` is now required, and without it the reference
    falls back to the correlated EXISTS on its OWN key.
    """
    template = """
        SELECT rt.value
        FROM hydro.river_timeseries rt
        JOIN hydro.run_link l ON l.child_run_key = rt.run_key
        JOIN hydro.hydro_run h ON h.run_key = l.parent_run_key
        WHERE rt.valid_time = :valid_time
    """

    assert store_binding_forms(template) == ("exists",)

    _forms, bound = _bind(template, "narrow")

    assert "h.timeseries_store" not in bound
    assert "store_route.run_key = rt.run_key" in bound


@pytest.mark.parametrize(
    "correlation",
    ["h.run_key = rt.run_key", "rt.run_key = h.run_key"],
)
def test_a_correlated_hydro_run_alias_does_earn_the_alias_form_either_way_round(correlation: str) -> None:
    """Non-vacuity for probe J, and the registry's own shape written both ways.

    All eight ``forecast_store`` segment blocks write the correlation in the
    ``hydro_run`` JOIN's ON while the fact reference's own chain is the WHERE, so
    the correlation is looked for across the SCOPE's chains, not only the
    reference's own.
    """
    template = f"""
        SELECT h.cycle_time
        FROM hydro.river_timeseries rt
        JOIN hydro.hydro_run h ON {correlation}
        WHERE rt.valid_time = :valid_time
    """

    assert store_binding_forms(template) == ("alias",)

    _forms, bound = _bind(template, "narrow")

    assert "AND h.timeseries_store = 'narrow'" in bound


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


def test_probe_e_a_fact_read_inside_an_exists_binds_in_the_exists_own_where() -> None:
    """Fixture probe E: bound INSIDE the sub-query, asserted by position.

    Hoisted to the outer ``WHERE`` the predicate would still count, still be
    exactly one, and reference an alias that is out of scope there — so the
    assertion is where it landed, between the sub-query's own last conjunct and
    the outer chain's next one.
    """
    template = """
        SELECT h.run_id
        FROM hydro.hydro_run h
        WHERE h.status = :status
          AND EXISTS (
              SELECT 1
              FROM hydro.river_timeseries rt
              WHERE rt.run_key = h.run_key
                AND rt.valid_time = :valid_time
          )
          AND h.cycle_time IS NOT NULL
    """

    forms, bound = _bind(template, "narrow")

    assert forms == ("exists",)
    predicate_at = bound.index("timeseries_store = 'narrow'")
    assert bound.index("rt.valid_time = :valid_time") < predicate_at < bound.index("h.cycle_time IS NOT NULL")


def test_probe_e_a_fact_read_inside_an_in_select_binds_in_that_selects_own_where() -> None:
    """Fixture probe E, the ``IN (SELECT …)`` spelling — same positional proof."""
    template = """
        SELECT h.run_id
        FROM hydro.hydro_run h
        WHERE h.status = :status
          AND h.run_key IN (
              SELECT rt.run_key
              FROM hydro.river_timeseries rt
              WHERE rt.valid_time = :valid_time
          )
          AND h.cycle_time IS NOT NULL
    """

    forms, bound = _bind(template, "narrow")

    assert forms == ("exists",)
    predicate_at = bound.index("timeseries_store = 'narrow'")
    assert bound.index("rt.valid_time = :valid_time") < predicate_at < bound.index("h.cycle_time IS NOT NULL")


def test_a_not_materialized_cte_is_not_a_negated_context() -> None:
    """The one exception the loose ``NOT`` rule needs, and why it is safe.

    ``NOT MATERIALIZED`` is a CTE materialisation hint: it says nothing about
    truth values. Without the exception every ``mvt`` tile statement
    (``source_rows AS NOT MATERIALIZED (``) refuses, which is a false refusal of
    a real read path rather than caution — and the paired probe below shows the
    exception is narrow.
    """
    template = """
        WITH source_rows AS NOT MATERIALIZED (
            SELECT rt.value
            FROM hydro.river_timeseries rt
            WHERE rt.valid_time = :valid_time
        )
        SELECT value FROM source_rows
    """

    forms, bound = _bind(template, "narrow")

    assert forms == ("exists",)
    assert bound.index("rt.valid_time = :valid_time") < bound.index("timeseries_store = 'narrow'")


def test_a_materialized_cte_whose_body_is_negated_is_still_refused() -> None:
    """The exception is for the two words together, not for the word ``NOT``."""
    template = """
        WITH probe AS (
            SELECT h.run_id
            FROM hydro.hydro_run h
            WHERE NOT EXISTS (
                SELECT 1 FROM hydro.river_timeseries rt WHERE rt.run_key = h.run_key
            )
        )
        SELECT run_id FROM probe
    """

    with pytest.raises(RiverTemplateError, match="NEGATED context"):
        _bind(template, "narrow")


def test_an_outer_join_earlier_in_the_same_segment_does_not_refuse_an_inner_fact_join() -> None:
    """The false-refusal control for condition 2 — and the copyback shape.

    An outer join to a DIFFERENT relation is not a hazard: the fact table is
    inner-joined, so a driving row with no fact row disappears in both branches
    and a run's rows live in exactly one store. Refusing this would refuse a real
    read path for the sake of a keyword's mere presence.
    """
    template = """
        SELECT h.run_id, rt.value
        FROM hydro.hydro_run h
        LEFT JOIN met.forcing_version fv ON fv.forcing_version_id = h.forcing_version_id
        JOIN hydro.river_timeseries rt ON rt.run_key = h.run_key
        WHERE h.status = :status
    """

    forms, bound = _bind(template, "narrow")

    assert forms == ("alias",)
    assert "AND h.timeseries_store = 'narrow'" in bound


def test_the_left_string_function_inside_a_chain_does_not_trip_the_outer_join_rule() -> None:
    """The reason the outer-join regex requires ``JOIN`` (round-2 H6, still true).

    ``LEFT(col, n)`` / ``RIGHT(col, n)`` are string functions. A bare
    ``LEFT|RIGHT|FULL`` alternation would refuse this statement and would also
    report ``AND LEFT(rt.run_id, 3) = :prefix`` as a dangling connective.
    """
    template = """
        SELECT rt.value
        FROM hydro.river_timeseries rt
        WHERE LEFT(rt.run_id, 3) = :prefix
          AND RIGHT(rt.run_id, 2) = :suffix
          AND rt.valid_time = :valid_time
    """

    forms, bound = _bind(template, "narrow")

    assert forms == ("exists",)
    assert_structurally_intact(bound, "<test>")


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
    """The faults that make the ``_chain_end`` mutants red rather than silent."""
    with pytest.raises(RiverTemplateError, match=re.escape(label)):
        assert_structurally_intact(sql, "<test>")


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


# ---------------------------------------------------------------------------
# Self-review of the round-3 redesign: two shapes that BOUND under the five
# conditions as first written, and are not in the whitelist's intent.
#
# The whitelist's own promise is that a shape it cannot prove equivalent is
# refused, so these are closed here rather than reported. Neither appears in the
# register (no entry uses RIGHT / FULL JOIN, no entry outer-joins hydro_run), so
# closing them costs no read path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("join", ["RIGHT JOIN", "RIGHT OUTER JOIN", "FULL JOIN", "FULL OUTER JOIN"])
def test_a_right_or_full_join_in_the_facts_own_segment_is_refused(join: str) -> None:
    """The preserved-side check only sees the keyword immediately before the reference.

    Here the fact table is the FROM target and the outer join comes AFTER it, so
    that check finds nothing — but the join preserves ``other``, whose rows have
    a NULL-extended fact side. The store predicate in the fact's own WHERE is
    NULL for those rows, so every branch drops them and the union loses rows the
    single-store statement returns.
    """
    template = f"""
        SELECT rt.value, o.tag
        FROM hydro.river_timeseries rt
        {join} other o ON o.run_key = rt.run_key
        WHERE o.flag = :flag
    """

    with pytest.raises(RiverTemplateError, match="preserves the OTHER relation"):
        _bind(template, "narrow")


def test_a_right_join_after_an_inner_fact_join_is_refused_too() -> None:
    """The mirror hazard: the o-only rows survive in BOTH branches and double.

    ``((x JOIN fact) RIGHT JOIN o)`` — the fact's ON is evaluated before the
    right join, so the store predicate filters the left side while ``o``'s
    unmatched rows are preserved by each branch independently.
    """
    template = """
        SELECT x.k, rt.value, o.tag
        FROM x
        JOIN hydro.river_timeseries rt ON rt.run_key = x.run_key
        RIGHT JOIN other o ON o.k = x.k
        WHERE x.flag = :flag
    """

    with pytest.raises(RiverTemplateError, match="preserves the OTHER relation"):
        _bind(template, "narrow")


def test_a_left_join_after_the_fact_reference_still_binds() -> None:
    """Non-vacuity, and the register's own shape — the fact side is preserved.

    ``publisher`` and ``forcing_copyback_backfill`` both ``LEFT JOIN
    met.forcing_version``. Refusing every outer join in the segment would refuse
    them, which is why the refusal is spelled ``RIGHT`` / ``FULL`` and not
    "any outer join".
    """
    template = """
        SELECT rt.value, o.tag
        FROM hydro.river_timeseries rt
        LEFT JOIN other o ON o.run_key = rt.run_key
        WHERE rt.valid_time = :valid_time
    """

    forms, bound = _bind(template, "narrow")

    assert forms == ("exists",)
    assert bound.index("rt.valid_time = :valid_time") < bound.index("timeseries_store = 'narrow'")


def test_an_outer_joined_hydro_run_alias_does_not_earn_the_alias_form() -> None:
    """``NULL = '<store>'`` is NULL, so the cheap form would drop rows in every branch.

    The correlation is present and the alias is in scope — the two things the
    alias form asks for — but ``h`` may be NULL-extended, and then
    ``h.timeseries_store = 'narrow'`` is NULL and the row disappears from BOTH
    branches. The correlated EXISTS on the fact reference's own key is always
    correct, so the alias simply does not earn the cheap form.
    """
    template = """
        SELECT rt.value, h.cycle_time
        FROM hydro.river_timeseries rt
        LEFT JOIN hydro.hydro_run h ON h.run_key = rt.run_key
        WHERE rt.valid_time = :valid_time
    """

    assert store_binding_forms(template) == ("exists",)

    _forms, bound = _bind(template, "narrow")

    assert "h.timeseries_store" not in bound
    assert "store_route.run_key = rt.run_key" in bound
