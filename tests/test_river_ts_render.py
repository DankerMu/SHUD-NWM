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

import pytest

from packages.common.river_ts_render import (
    PUSHDOWN_AID_MARKER,
    RIVER_TABLE,
    RIVER_TABLE_LEGACY,
    SANCTIONED_TEXT_PUSHDOWN_COLUMNS,
    TEXT_AID_COUNTERPARTS,
    TEXT_IDENTITY_COLUMNS,
    RiverTemplateError,
    aid_conjunct,
    assert_structurally_intact,
    fact_table_attribution,
    fact_table_text_identity_columns,
    render_river_ts_sql,
    render_union_all,
    store_binding_form,
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

    assert store_binding_form(template) == "join"
    rendered = render_union_all(template, ("narrow",), {"status": "published", "variable": "q_down"}, entry="join")
    assert rendered.branches == ("join",)
    assert "AND h.timeseries_store = 'narrow'" in rendered.sql


def test_the_union_combinator_falls_back_to_an_exists_when_the_scope_has_no_alias() -> None:
    """The correlated form, for the templates that never name ``hydro_run`` beside the fact table.

    Reported rather than chosen invisibly (:func:`store_binding_form`): the two
    forms are different plans, and which one a template gets is a property of the
    template a reviewer has to be able to read off.
    """
    assert store_binding_form(NAMED_TEMPLATE) == "exists"

    rendered = render_union_all(NAMED_TEMPLATE, ("narrow",), {"run_id": "a", "valid_time": "b"}, entry="exists")

    assert rendered.branches == ("exists",)
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
