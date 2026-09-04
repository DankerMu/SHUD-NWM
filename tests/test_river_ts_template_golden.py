"""The equivalence oracle for #1980's template normalisation.

Task 1.1 rewrites the layout of every river read template: aids move off
``WHERE`` lines onto their own ``AND`` line, mvt's three 1:N markers become
eleven 1:1 ones, display_coverage's prose paragraph becomes per-aid markers, and
two ``OR (…)`` disjunctions are re-bracketed. That is a lot of hand editing on
SQL nobody can execute in a unit test, and the claim being made about it is
"zero behaviour change".

This file is the machine-checkable form of that claim. Before any template was
touched, every registered entry's text was captured at base ``51f9d273`` and
committed as ``tests/fixtures/river_ts_templates_51f9d273.json`` in the canonical
chain form (:func:`packages.common.river_ts_render.sql_chains`). The oracle then
asserts that the LEGACY variant — the post-normalisation template with the table
name substituted back — reproduces the golden chains exactly.

What that does and does not allow
---------------------------------

The chain form is invariant under exactly the three things the normalisation is
permitted to change and nothing else:

* whitespace / indentation — collapsed;
* comment placement, including the markers themselves — removed;
* the ORDER of conjuncts within one AND-chain — each chain is a sorted multiset,
  which is what licenses "the ``WHERE`` line takes the next key conjunct".

What it covers is the CONJUNCT MULTISET OF EVERY PREDICATE CHAIN, and inside
that scope everything is red: a dropped or added conjunct, a changed parameter
name, a changed comparison operator, a predicate that moved between chains (a
lateral body's conjunct hoisted to the outer ``WHERE`` changes two chains), a
re-bracketed disjunction that actually changes the truth table (the ``OR (…)``
body is its own chain), a join that lost an ``ON`` conjunct. The counter-examples
at the bottom prove each of those bites, because a golden that cannot be made red
would certify the edit rather than check it.

What it does NOT cover — stated because an over-claimed oracle is worse than a
narrow one (review #1996, C11): the SELECT list, ``FROM``/``JOIN`` targets and
aliases, ``LIMIT`` / ``ORDER BY`` / ``GROUP BY``, CTE names. Those are outside a
predicate chain, so mutating them is GREEN here. #1980 changes none of them —
every changed line in the production diff is a ``WHERE`` / ``AND`` / ``OR (``
conjunct line — and the sibling pins that do cover them are elsewhere
(``tests/test_river_ts_read_path_surrogate_keys.py`` embeds ``LIMIT 1`` in its
pinned substrings, ``tests/test_hydro_display_mvt_scaling.py`` pins ``JOIN
core.river_segment rs``).
"""

from __future__ import annotations

import json

import pytest

from packages.common.river_ts_render import (
    RIVER_TABLE,
    RIVER_TABLE_LEGACY,
    render_river_ts_sql,
    sql_chains,
)
from tests.river_ts_template_registry import (
    GOLDEN_BASE_SHA,
    GOLDEN_FIXTURE,
    GOLDEN_SHA256,
    NON_TEMPLATE_MENTIONS,
    REGISTERED_TEMPLATE_PATHS,
    REGISTRY,
    entry_by_key,
    golden_sha256,
)

GOLDEN = json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))


def _legacy_chains(template: str, key: str) -> tuple[tuple[str, ...], ...]:
    """The legacy variant's chains with the physical table name substituted back.

    The rename is the ONE textual difference the legacy variant is allowed to
    have from the normalised template, so it is undone here rather than excluded
    from the comparison — a template that stopped naming the table at all would
    otherwise compare equal.
    """
    rendered = render_river_ts_sql(template, "legacy", entry=key)
    assert (RIVER_TABLE_LEGACY in rendered.sql) == (RIVER_TABLE in template), key
    return sql_chains(rendered.sql.replace(RIVER_TABLE_LEGACY, RIVER_TABLE))


def test_the_golden_was_captured_at_the_change_base() -> None:
    """Provenance, so a regenerated-against-itself fixture is visible.

    A golden re-captured from the POST-edit tree would pass every assertion below
    while proving nothing. The recorded SHA says which tree it came from — but it
    is a field INSIDE the file, so a regeneration copies it forward unchanged and
    nothing notices. The byte hash is the half that notices: re-capturing the
    fixture turns this red until someone edits the pin, which is a one-line diff
    on a reviewed line rather than a silent 1000-line data change (review #1996,
    C10).
    """
    assert GOLDEN["base_sha"].startswith(GOLDEN_BASE_SHA)
    assert len(GOLDEN["base_sha"]) == 40
    assert golden_sha256() == GOLDEN_SHA256, (
        "the golden fixture's bytes changed; if that was a deliberate re-capture, "
        "update GOLDEN_SHA256 in tests/river_ts_template_registry.py and say why"
    )


def test_the_golden_covers_exactly_the_registered_entries() -> None:
    """A new entry without a golden, or a golden without an entry, is red.

    Without this the equivalence check is only as complete as the fixture: an
    entry added to the register but not to the golden would simply not be
    compared, which is the failure mode a "for every entry in the golden" loop
    has and this one does not.
    """
    assert set(GOLDEN["entries"]) == {entry.key for entry in REGISTRY}
    assert len({entry.key for entry in REGISTRY}) == len(REGISTRY), "duplicate registry key"


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda entry: entry.key)
def test_every_registered_legacy_variant_reproduces_the_golden_chains(entry) -> None:
    golden = GOLDEN["entries"][entry.key]
    assert golden["path"] == entry.path
    assert golden["kind"] == entry.kind
    assert golden["params"] == entry.params

    chains = _legacy_chains(entry.source(), entry.key)

    expected = tuple(tuple(chain) for chain in golden["chains"])
    assert len(chains) == len(expected), (
        f"{entry.key}: {len(chains)} chains after normalisation, {len(expected)} at {GOLDEN_BASE_SHA}"
    )
    for index, (actual_chain, expected_chain) in enumerate(zip(chains, expected, strict=True)):
        assert actual_chain == expected_chain, f"{entry.key}: chain {index} changed"


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda entry: entry.key)
def test_every_registered_entry_declares_its_own_table_mentions(entry) -> None:
    """The per-entry half of registry closure (the per-file sums live in the owning oracles)."""
    text = entry.source()

    assert text.count(RIVER_TABLE) == entry.mentions, entry.key
    assert (entry.mentions == 0) == (entry.kind == "fragment"), entry.key


def test_every_registered_file_declares_its_non_template_mentions() -> None:
    """Closure's bookkeeping half: the offsets are enumerated, not inferred.

    Registry closure is an EQUALITY between a file's census and the mentions its
    registered templates carry. The difference is never zero for four files —
    an index-metadata literal, two error-message strings, the parser's two write
    statements and the renderer's own table-name constants all name the table
    without being a read template — so the offsets are listed with a reason each.
    An inequality would go slack the first time someone added a mention; this
    stays exact.
    """
    assert set(NON_TEMPLATE_MENTIONS) >= set(REGISTERED_TEMPLATE_PATHS)
    # packages/common/river_ts_render.py holds no template at all: it is
    # registered purely so a statement sneaking into the shared helper is red.
    assert "packages/common/river_ts_render.py" in NON_TEMPLATE_MENTIONS
    assert not any(entry.path == "packages/common/river_ts_render.py" for entry in REGISTRY)


def test_the_register_is_grouped_by_reader_module_in_stable_order() -> None:
    """Wave 2 appends whole blocks; the block order is path-sorted so they cannot collide."""
    assert list(REGISTERED_TEMPLATE_PATHS) == sorted(REGISTERED_TEMPLATE_PATHS)
    seen: list[str] = []
    for entry in REGISTRY:
        if entry.path not in seen:
            seen.append(entry.path)
    assert seen == list(REGISTERED_TEMPLATE_PATHS), "a reader module's entries are not contiguous"


# ---------------------------------------------------------------------------
# The oracle's own contract: what the chain form must and must not notice
# ---------------------------------------------------------------------------

_SPECIMEN = """
    SELECT ts.value
    FROM hydro.river_timeseries ts
    JOIN core.river_segment rs
      ON rs.river_segment_key = ts.river_segment_key
     AND rs.river_network_version_id = :river_network_version_id
    WHERE ts.run_key = (
              SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id
          )
      -- transitional compressed-chunk pushdown aid, remove with #1342
      AND ts.run_id = :run_id
      AND ts.valid_time = :valid_time
"""


def test_reindenting_and_recommenting_a_template_leaves_the_chains_alone() -> None:
    """Non-vacuity: the three permitted changes really are invisible."""
    reflowed = _SPECIMEN.replace("\n      ", "\n            ").replace(
        "-- transitional compressed-chunk pushdown aid, remove with #1342",
        "-- moved elsewhere, said differently",
    )

    assert sql_chains(reflowed) == sql_chains(_SPECIMEN)


def test_reordering_conjuncts_inside_one_chain_leaves_the_chains_alone() -> None:
    """The whole licence #1980 needs: the ``WHERE`` line may take a different conjunct."""
    reordered = _SPECIMEN.replace(
        "      AND ts.run_id = :run_id\n      AND ts.valid_time = :valid_time\n",
        "      AND ts.valid_time = :valid_time\n      AND ts.run_id = :run_id\n",
    )

    assert reordered != _SPECIMEN
    assert sql_chains(reordered) == sql_chains(_SPECIMEN)


def test_dropping_a_conjunct_changes_the_chains() -> None:
    mutated = _SPECIMEN.replace("      AND ts.valid_time = :valid_time\n", "")

    assert sql_chains(mutated) != sql_chains(_SPECIMEN)


def test_changing_a_parameter_name_changes_the_chains() -> None:
    mutated = _SPECIMEN.replace("ts.valid_time = :valid_time", "ts.valid_time = :valid_time_end")

    assert sql_chains(mutated) != sql_chains(_SPECIMEN)


def test_moving_a_conjunct_out_of_a_join_into_the_where_changes_the_chains() -> None:
    """The migration the multiset alone would miss.

    ``ON a AND b`` / ``WHERE c`` and ``ON a`` / ``WHERE b AND c`` hold the same
    conjuncts overall, and for an INNER join they even mean the same thing — but
    for the lateral bodies this oracle really guards they do not, and #1980 is
    not allowed to move a predicate between chains either way. Chains are
    therefore compared positionally, each as its own multiset.
    """
    mutated = _SPECIMEN.replace(
        "     AND rs.river_network_version_id = :river_network_version_id\n", ""
    ).replace(
        "      AND ts.valid_time = :valid_time\n",
        "      AND ts.valid_time = :valid_time\n      AND rs.river_network_version_id = :river_network_version_id\n",
    )

    assert sorted(sum(sql_chains(mutated), ())) == sorted(sum(sql_chains(_SPECIMEN), ()))
    assert sql_chains(mutated) != sql_chains(_SPECIMEN)


def test_a_lateral_body_is_its_own_chain_and_never_merges_with_the_outer_where() -> None:
    sql = """
        SELECT 1
        FROM tile_segments seg
        CROSS JOIN LATERAL (
            SELECT ts.value
            FROM hydro.river_timeseries ts
            WHERE ts.river_segment_key = seg.river_segment_key
              AND ts.valid_time = :valid_time
            LIMIT 1
        ) v
        WHERE seg.stream_type IS NOT NULL
    """

    chains = sql_chains(sql)

    assert ("seg.stream_type IS NOT NULL",) in chains
    assert ("ts.river_segment_key = seg.river_segment_key", "ts.valid_time = :valid_time") in chains


def test_an_or_disjunct_body_is_its_own_chain() -> None:
    """Fixture decision 6: the rewritten guards' truth table is pinned per disjunct."""
    sql = (
        "SELECT 1 FROM hydro.river_timeseries rt\n"
        "WHERE (%(scan_run_id)s IS NULL\n"
        "       OR (\n"
        "           rt.run_id = %(scan_run_id)s AND\n"
        "           rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(scan_run_id)s)))\n"
    )

    chains = sql_chains(sql)

    assert (
        "rt.run_id = %(scan_run_id)s",
        "rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(scan_run_id)s)",
    ) in chains


def test_a_fragment_with_no_keyword_of_its_own_is_one_chain() -> None:
    fragment = "rt.river_segment_key = %s\n  AND rt.river_segment_id = %s\n  AND rt.variable = 'q_down'"

    assert sql_chains(fragment) == (
        ("rt.river_segment_id = %s", "rt.river_segment_key = %s", "rt.variable = 'q_down'"),
    )


# ---------------------------------------------------------------------------
# The chain boundary re-pin (#1980 fixture decision 12, round-3 L4)
#
# `_REGION_START` gained `HAVING` and lost the `ON` of `DISTINCT ON`, so the
# golden was re-captured from a pristine `git archive 51f9d273` tree with the new
# normaliser and `GOLDEN_SHA256` re-pinned. The measured difference is exactly
# four entries: two HAVING chains appear (`display_coverage:refresh`,
# `forecast_store:latest_product_fallback`) and three `DISTINCT ON (…)`
# select-list pseudo-chains disappear (`forecast_store:analysis_segment_rows`
# once, `mvt:postgis_tile_sql_hydro_national` twice) — 216 chains before, 215
# after.
#
# Both directions are counter-exampled below, because a boundary re-pin that
# nobody can demonstrate is a boundary nobody can review: the point of adding
# HAVING is that deleting one goes RED, and the point of dropping DISTINCT ON is
# that editing one stays GREEN.
# ---------------------------------------------------------------------------

_HAVING_LINE = "            HAVING COUNT(DISTINCT variable) = %(variable_count)s\n"


def test_deleting_a_whole_having_line_reddens_the_golden() -> None:
    """Decision 12's first counter-example: HAVING chains are now collected.

    Before the re-pin this deletion was INVISIBLE — the golden never looked at a
    HAVING body, so dropping the ``COUNT(DISTINCT variable) = %(variable_count)s``
    guard (which is what makes a coverage row mean "every variable arrived")
    passed the equivalence oracle.
    """
    entry = entry_by_key("display_coverage:refresh")
    template = entry.source()
    assert template.count(_HAVING_LINE) == 1

    mutated = template.replace(_HAVING_LINE, "")

    golden = tuple(tuple(chain) for chain in GOLDEN["entries"][entry.key]["chains"])
    assert _legacy_chains(template, entry.key) == golden
    assert _legacy_chains(mutated, entry.key) != golden


def test_editing_a_distinct_on_select_list_leaves_the_golden_green() -> None:
    """Decision 12's second counter-example: a select list is not a predicate chain.

    ``SELECT DISTINCT ON (rt.valid_time)`` is a de-duplication key, and the
    golden is a PREDICATE-CHAIN oracle (decision 6) — it is deliberately blind
    outside WHERE / ON / HAVING / sub-query / OR-disjunct chains, and the sibling
    substring pins cover select lists. Reading the ``ON`` of ``DISTINCT ON`` as a
    chain opener made the golden police a select list as if it were a predicate,
    which is a claim it cannot honestly make about the other entries.
    """
    entry = entry_by_key("forecast_store:analysis_segment_rows")
    template = entry.source()
    assert "SELECT DISTINCT ON (rt.valid_time)" in template

    mutated = template.replace("SELECT DISTINCT ON (rt.valid_time)", "SELECT DISTINCT ON (rt.valid_time, rt.value)")

    golden = tuple(tuple(chain) for chain in GOLDEN["entries"][entry.key]["chains"])
    assert mutated != template
    assert _legacy_chains(template, entry.key) == golden
    assert _legacy_chains(mutated, entry.key) == golden


def test_the_golden_holds_the_measured_chain_total() -> None:
    """20 entries / 215 chains, stated so a re-capture cannot quietly change the shape."""
    assert len(GOLDEN["entries"]) == 20
    assert sum(len(entry["chains"]) for entry in GOLDEN["entries"].values()) == 215
    assert len(GOLDEN["entries"]["display_coverage:refresh"]["chains"]) == len(
        _legacy_chains(entry_by_key("display_coverage:refresh").source(), "display_coverage:refresh")
    )
