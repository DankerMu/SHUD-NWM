"""Renderer predicate preservation and provenance of the frozen #1980 capture.

The fixture records the original template-normalisation audit at ``51f9d273``.
Its bytes remain historical evidence, not a requirement that future production
SQL retain old bugs. Current templates may change intentionally; what the
oracle asserts is that the renderer preserves their predicates.

It changes by DECLARED DELTA and never by re-capture: a golden regenerated from
the post-edit tree would certify the edit against itself. #2007 inserted two
conjuncts that way; #1342's contract (task 6.3) removed three, and the fixture's
``delta_from`` block records which entry and why.

Chain comparison covers WHERE/ON/HAVING predicates, not SELECT lists, table
targets or ordering. The counter-examples below keep those limits explicit.

The renderer takes one store since task 6.3, so the ``("legacy", "narrow")``
parametrisations this module carried are gone and the equivalence is asserted
on the narrow render alone. ``render_river_ts_sql`` is a pure validator now —
it returns its input unchanged — so "the renderer preserves the predicates" is
proven by ``tests/test_sql_shape_helpers.py``'s ``rendered.sql == template``
and what is left here is the GOLDEN comparison: the registered templates still
carry the predicates ``51f9d273`` recorded, minus a declared delta.
"""

from __future__ import annotations

import json

import pytest

from packages.common.river_ts_render import (
    RIVER_TABLE,
    render_river_ts_sql,
    sql_chains,
)
from tests.river_ts_template_registry import (
    FORECAST_STORE_EXECUTIONS,
    FORECAST_STORE_SEGMENT_BLOCKS,
    GOLDEN_BASE_SHA,
    GOLDEN_FIXTURE,
    GOLDEN_SHA256,
    NON_TEMPLATE_MENTIONS,
    PARSER_NARROW_WRITER_KEYS,
    REGISTERED_TEMPLATE_PATHS,
    REGISTRY,
    ROUTED_SOURCE_KEYS,
    aid_comment_lines,
    entry_by_key,
    golden_sha256,
)

GOLDEN = json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))


def _narrow_chains(template: str, key: str) -> tuple[tuple[str, ...], ...]:
    """The rendered template's chains.

    It was ``_legacy_chains`` and substituted the retired physical table name
    back before comparing, because the rename was the one textual difference the
    legacy variant was allowed to have. Task 6.3 deleted that variant, so there
    is nothing to substitute and the render is the template — which is asserted,
    not assumed, so this helper cannot silently become a no-op wrapper around a
    renderer that started rewriting its input.
    """
    rendered = render_river_ts_sql(template, "narrow", entry=key)
    assert rendered.sql == template, key
    assert "hydro.river_timeseries_legacy" not in rendered.sql, key
    return sql_chains(rendered.sql)


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
    # A delta may move entries; it may not move the base they are a delta FROM,
    # because that is the claim the note makes about the other 19.
    delta = GOLDEN["delta_from"]
    assert delta["base_sha"] == GOLDEN["base_sha"]
    assert delta["entries"] == ["hydro_display:mvt_source_identity_probe"]
    assert len(delta["commit"]) == 40 and delta["reason"]
    assert golden_sha256() == GOLDEN_SHA256, (
        "the golden fixture's bytes changed; if that was a deliberate re-capture, "
        "update GOLDEN_SHA256 in tests/river_ts_template_registry.py and say why"
    )


def test_the_golden_covers_exactly_the_registered_entries() -> None:
    """Historical statements and live raw sources have distinct exact sets."""
    siblings = {entry.key for entry in REGISTRY} - ROUTED_SOURCE_KEYS - PARSER_NARROW_WRITER_KEYS
    assert len(siblings) == 1
    assert set(GOLDEN["entries"]) == siblings | {
        "publisher:qdown_discovery",
        "forcing_copyback_backfill:discover_backfill_runs",
        "display_coverage:refresh",
        "mvt:postgis_tile_sql_hydro",
        "mvt:postgis_tile_sql_hydro_national",
        "mvt:valid_times_named_identity",
        "mvt:valid_times_any_identity",
        "parser:replace_chain_probe",
        "parser:replace_chain_window",
        *(f"forecast_store:{label}" for label in FORECAST_STORE_SEGMENT_BLOCKS),
        "forecast_store:segment_identity_predicates",
        "forecast_store:latest_product_fallback",
    }
    assert {entry.key for entry in REGISTRY} == siblings | ROUTED_SOURCE_KEYS | PARSER_NARROW_WRITER_KEYS
    assert len(REGISTRY) == 13
    assert ROUTED_SOURCE_KEYS == {
        "publisher:qdown_discovery",
        "forcing_copyback_backfill:discover_backfill_runs",
        "display_coverage:refresh",
        "forecast_store:segment_rows_source",
        "forecast_store:latest_product_river_source",
        "mvt:postgis_tile_sql_hydro",
        "mvt:hydro_national_identity_source",
        "mvt:hydro_national_data_source",
        "mvt:valid_times_named_identity",
        "mvt:valid_times_any_identity",
    }
    assert PARSER_NARROW_WRITER_KEYS == {
        "parser:replace_chain_probe",
        "parser:replace_chain_window",
    }
    assert len(GOLDEN["entries"]) == 20
    assert set(FORECAST_STORE_EXECUTIONS) == {
        *FORECAST_STORE_SEGMENT_BLOCKS, "latest_product_fallback",
    }
    assert len({entry.key for entry in REGISTRY}) == len(REGISTRY), "duplicate registry key"


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda entry: entry.key)
def test_the_renderer_preserves_every_current_template_predicate(entry) -> None:
    """Every registered template still says what ``51f9d273`` recorded.

    It was ``test_legacy_renderer_preserves_...`` and rendered
    ``entry.source("legacy")``; task 6.3 leaves one store, so the rendering half
    is now an identity (pinned inside ``_narrow_chains``) and what remains is the
    golden comparison. Three of the thirteen registered entries have golden data
    to compare against — the other ten are ``ROUTED_SOURCE_KEYS``, whose live
    owners are composed statements the golden records separately — and the
    sibling is the entry this cut delta'd, which is exactly why it must still be
    compared rather than dropped.
    """
    template = entry.source("narrow")

    assert _narrow_chains(template, entry.key) == sql_chains(template)
    assert aid_comment_lines(template) == (), entry.key
    if entry.key not in ROUTED_SOURCE_KEYS | PARSER_NARROW_WRITER_KEYS:
        assert _narrow_chains(template, entry.key) == tuple(
            tuple(chain) for chain in GOLDEN["entries"][entry.key]["chains"]
        )
    elif entry.key in PARSER_NARROW_WRITER_KEYS:
        # Declared delta is exactly one obsolete run_id aid, not permission
        # to discard any other historical key/window predicate. That delta is
        # #1985's (I7) and predates task 6.3; it is unchanged here.
        historical = GOLDEN["entries"][entry.key]["chains"]
        assert sum(chain.count("run_id = %s") for chain in historical) == 1
        assert sql_chains(template) == tuple(
            tuple(predicate for predicate in chain if predicate != "run_id = %s")
            for chain in historical
        )


@pytest.mark.parametrize(
    ("key", "factory_name", "route_alias"),
    (
        ("forecast_store:segment_rows_source", "_segment_rows_source_template", "h"),
        ("forecast_store:latest_product_river_source", "_latest_product_river_source_template", "cr"),
    ),
)
def test_routed_registry_render_matches_actual_store_source(key, factory_name, route_alias) -> None:
    """The registry entry and the production factory are the same text.

    ``route_alias`` survives the ``store`` parametrisation's removal because the
    per-alias ABSENCE of a routing predicate is the assertion task 6.3 turns this
    into: the column is about to be dropped by the 6.2 migration, so a render
    that still spelled it would fail at execute() rather than here.
    """
    from packages.common import forecast_store

    entry = entry_by_key(key)
    rendered = render_river_ts_sql(entry.source("narrow"), "narrow", entry=key)
    expected = render_river_ts_sql(getattr(forecast_store, factory_name)("narrow"), "narrow", entry=key)
    assert f"{route_alias}.timeseries_store" not in rendered.sql
    assert "timeseries_store" not in rendered.sql
    assert rendered == expected
    with pytest.raises(ValueError, match="Invalid river timeseries store"):
        getattr(forecast_store, factory_name)("legacy")


def test_hydro_routed_registry_matches_the_authored_source() -> None:
    """Three aids until task 6.3; zero now, and zero is what is asserted."""
    from services.tiles import mvt

    entry = entry_by_key("mvt:postgis_tile_sql_hydro")
    raw = entry.source("narrow")
    assert raw == mvt._hydro_source_template("narrow")
    assert raw.count(RIVER_TABLE) == 1
    assert aid_comment_lines(raw) == ()
    rendered = render_river_ts_sql(raw, "narrow").sql
    assert "timeseries_store" not in rendered
    assert "hydro.river_timeseries_legacy" not in rendered
    assert "UNION ALL" not in rendered
    with pytest.raises(ValueError, match="Unsupported river timeseries store"):
        mvt._hydro_source_template("legacy")


@pytest.mark.parametrize("probe", ("identity", "data"))
def test_national_routed_registry_matches_the_authored_source(probe) -> None:
    """3 and 4 aids until task 6.3; zero now, and the refusal is still injection-safe."""
    from services.tiles import mvt

    entry = entry_by_key(f"mvt:hydro_national_{probe}_source")
    factory = getattr(mvt, f"_hydro_national_{probe}_source_template")
    raw = entry.source("narrow")
    assert raw == factory("narrow")
    assert raw.count(RIVER_TABLE) == 1
    assert aid_comment_lines(raw) == ()
    rendered = render_river_ts_sql(raw, "narrow").sql
    assert "timeseries_store" not in rendered
    assert "hydro.river_timeseries_legacy" not in rendered
    assert "UNION ALL" not in rendered
    assert "LIMIT" not in rendered
    for refused in ("legacy", "legacy' OR true --"):
        with pytest.raises(ValueError, match="Unsupported river timeseries store"):
            factory(refused)


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda entry: entry.key)
def test_every_registered_entry_declares_its_own_table_mentions(entry) -> None:
    """The per-entry half of registry closure (the per-file sums live in the owning oracles)."""
    text = entry.source("narrow")

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
      -- a comment the chain form is required to ignore
      AND ts.run_id = :run_id
      AND ts.valid_time = :valid_time
"""


def test_reindenting_and_recommenting_a_template_leaves_the_chains_alone() -> None:
    """Non-vacuity: the three permitted changes really are invisible."""
    reflowed = _SPECIMEN.replace("\n      ", "\n            ").replace(
        "-- a comment the chain form is required to ignore",
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
    mutated = _SPECIMEN.replace("     AND rs.river_network_version_id = :river_network_version_id\n", "").replace(
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

_HAVING_SPECIMEN = """
    SELECT rt.run_key, COUNT(DISTINCT rt.variable_e) AS variables
    FROM hydro.river_timeseries rt
    WHERE rt.valid_time >= %(start)s
    GROUP BY rt.run_key
    HAVING COUNT(DISTINCT variable) = %(variable_count)s
"""
_HAVING_LINE = "    HAVING COUNT(DISTINCT variable) = %(variable_count)s\n"


def test_deleting_a_whole_having_line_changes_the_chains() -> None:
    """Decision 12's first counter-example: HAVING chains are collected.

    Before the chain-boundary re-pin this deletion was INVISIBLE — the normaliser
    never looked at a HAVING body, so dropping the
    ``COUNT(DISTINCT variable) = %(variable_count)s`` guard (which is what makes a
    coverage row mean "every variable arrived") passed the equivalence oracle.

    It used to be demonstrated on ``historical_display_coverage_sql()``, the
    frozen pre-store display-coverage DML. That fixture went with task 6.3's
    deletion of the legacy read path, and the property was never about that
    statement: it is about the normaliser's chain boundary, so it is stated on a
    specimen that carries a HAVING and nothing else of interest.
    """
    assert _HAVING_SPECIMEN.count(_HAVING_LINE) == 1
    chains = sql_chains(_HAVING_SPECIMEN)
    assert ("COUNT(DISTINCT variable) = %(variable_count)s",) in chains

    mutated = _HAVING_SPECIMEN.replace(_HAVING_LINE, "")

    assert sql_chains(mutated) != chains
    assert ("COUNT(DISTINCT variable) = %(variable_count)s",) not in sql_chains(mutated)


def test_the_golden_still_records_the_having_chain_it_was_re_pinned_for() -> None:
    """Non-vacuity for the boundary re-pin: the fixture really holds HAVING chains.

    The counter-example above proves the NORMALISER collects them; this proves
    the frozen capture was taken with that normaliser, which is the half that
    would go quiet if the golden were ever regenerated with an older one.
    """
    for key in ("display_coverage:refresh", "forecast_store:latest_product_fallback"):
        chains = [tuple(chain) for chain in GOLDEN["entries"][key]["chains"]]
        assert ("COUNT(DISTINCT variable) = %(variable_count)s",) in chains, key


def test_editing_a_distinct_on_select_list_leaves_the_golden_green() -> None:
    """Decision 12's second counter-example: a select list is not a predicate chain.

    ``SELECT DISTINCT ON (rt.valid_time)`` is a de-duplication key, and the
    golden is a PREDICATE-CHAIN oracle (decision 6) — it is deliberately blind
    outside WHERE / ON / HAVING / sub-query / OR-disjunct chains, and the sibling
    substring pins cover select lists. Reading the ``ON`` of ``DISTINCT ON`` as a
    chain opener made the golden police a select list as if it were a predicate,
    which is a claim it cannot honestly make about the other entries.
    """
    template, _parameters = FORECAST_STORE_EXECUTIONS["analysis_segment_rows"]()
    assert "SELECT DISTINCT ON (rt.valid_time)" in template
    mutated = template.replace("SELECT DISTINCT ON (rt.valid_time)", "SELECT DISTINCT ON (rt.valid_time, rt.value)")
    assert mutated != template
    # Composed statements are not renderer inputs. This remains a chain-boundary
    # counterexample, while routing/selection owners separately pin the output.
    assert sql_chains(mutated) == sql_chains(template)


def test_the_golden_holds_the_measured_chain_total() -> None:
    """20 entries / 215 chains, stated so a re-capture cannot quietly change the shape.

    Both numbers are re-measured from the post-delta artifact rather than carried
    over: task 6.3's delta removed three CONJUNCTS from one chain, which changes
    neither count, and saying so here is what makes "three lines, one chain" a
    checkable claim instead of an assertion in a commit message.

    The third assertion — a length cross-check against
    ``historical_display_coverage_sql()`` — went with that fixture (deletion D2).
    Its job is done better by the entry-set pin above, which names every key.
    """
    assert len(GOLDEN["entries"]) == 20
    assert sum(len(entry["chains"]) for entry in GOLDEN["entries"].values()) == 215
    assert len(GOLDEN["entries"]["hydro_display:mvt_source_identity_probe"]["chains"]) == 5
