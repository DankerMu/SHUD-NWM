"""The forcing renderer's own contract (#1990, task 7.2a).

Unit oracle for ``packages/common/forcing_ts_render.py``: variant selection,
table-name binding through the D1 constants, and the four refusals. The
discovery-set census is ``tests/test_forcing_ts_template_census.py``; the shape
oracle over the nine registered templates — byte identity, store routing, and
I1–I5 — is ``tests/test_forcing_read_path_store_routing.py`` (cut (b), 7.2).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from packages.common import forcing_ts_render
from packages.common.forcing_ts_render import (
    FORCING_STORES,
    FORCING_TABLE,
    FORCING_TABLE_LEGACY,
    FORCING_TABLE_TOKEN,
    ForcingTemplateError,
    ForcingTemplatePair,
    RenderedForcingSql,
    render_forcing_ts_sql,
)

LEGACY_BODY = f"""
SELECT fst.station_id, fst.variable, fst.valid_time, fst.value, fst.unit, fst.quality_flag,
       fst.source_id, fst.basin_version_id
FROM {FORCING_TABLE_TOKEN} fst
WHERE fst.forcing_version_id = %(forcing_version_id)s
  AND fst.variable = ANY(%(variables)s)
"""

NARROW_BODY = f"""
SELECT ms.station_id, fst.variable_e::text AS variable, fst.valid_time, fst.value,
       fst.unit_e::text AS unit, fst.quality_flag_e::text AS quality_flag,
       fv.source_id, ms.basin_version_id
FROM {FORCING_TABLE_TOKEN} fst
JOIN met.forcing_version fv ON fv.forcing_version_key = fst.forcing_version_key
JOIN met.met_station ms ON ms.station_key = fst.station_key
WHERE fv.forcing_version_id = %(forcing_version_id)s
  AND fst.variable_e = ANY(%(variables)s)
"""

PAIR = ForcingTemplatePair(legacy=LEGACY_BODY, narrow=NARROW_BODY)


# ---------------------------------------------------------------------------
# D1 — the deployability property
# ---------------------------------------------------------------------------


def test_both_table_constants_spell_the_deployed_name_today() -> None:
    """The pin that keeps master deployable to node-27 until the migration lands.

    ``met.forcing_station_timeseries_legacy`` is created by task 7.3's expand
    migration and DOES NOT EXIST YET, so the legacy store must name the relation
    that is actually deployed. River's equivalent wiring shipped three days ahead
    of its migration and left master fail-closed on HTTP 500 in between; this
    assertion is what refuses to repeat that.

    TASK 7.3 FLIPS ``FORCING_TABLE_LEGACY`` AND THIS TEST IN THE SAME COMMIT AS
    THE RENAME. Flipping either one alone is the bug both of them exist to catch.
    """
    assert FORCING_TABLE == "met.forcing_station_timeseries"
    assert FORCING_TABLE_LEGACY == "met.forcing_station_timeseries"


def test_the_two_variants_render_byte_identically_while_the_constants_agree() -> None:
    """D1's payoff: with the constants equal, store routing is a provable no-op.

    This is what upgrades the reader wiring in task 7.2 from "semantically
    identical" to byte-identical rendered SQL — the strongest pin available WHILE
    THE CONSTANTS AGREE, and one only D1 makes reachable. Asserted on a pair whose
    two bodies are the same text, so the ONLY thing that could differ is the bound
    table name.

    TASK 7.3 DELETES THIS TEST, in the same commit as the rename. It goes red
    there by construction — legacy then binds ``…_legacy`` and narrow binds the
    canonical name, so the two renders are supposed to differ. DO NOT "fix" it by
    aligning the constants again: that would undo the flip and leave the legacy
    reader pointed at the narrow table, which is the precise failure D1 exists to
    prevent. The pin that SURVIVES 7.3 is
    :func:`test_the_seven_three_flip_is_the_one_line_it_claims_to_be`.
    """
    same = ForcingTemplatePair(legacy=LEGACY_BODY, narrow=LEGACY_BODY)
    assert render_forcing_ts_sql(same, "legacy").sql == render_forcing_ts_sql(same, "narrow").sql


def test_the_seven_three_flip_is_the_one_line_it_claims_to_be(monkeypatch) -> None:
    """Simulate the migration commit: only the legacy render must move.

    The constant is read at call time precisely so this holds without any caller
    being re-imported. If this test ever needs a second edit to pass, the "one
    line, in the migration's own commit" claim has stopped being true.
    """
    monkeypatch.setattr(forcing_ts_render, "FORCING_TABLE_LEGACY", "met.forcing_station_timeseries_legacy")

    legacy = render_forcing_ts_sql(PAIR, "legacy")
    narrow = render_forcing_ts_sql(PAIR, "narrow")
    assert "FROM met.forcing_station_timeseries_legacy fst" in legacy.sql
    assert "FROM met.forcing_station_timeseries fst" in narrow.sql
    assert "_legacy" not in narrow.sql


# ---------------------------------------------------------------------------
# Selection and binding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", FORCING_STORES)
def test_rendering_binds_the_constant_and_leaves_no_token(store) -> None:
    rendered = render_forcing_ts_sql(PAIR, store)
    expected = FORCING_TABLE_LEGACY if store == "legacy" else FORCING_TABLE
    assert f"FROM {expected} fst" in rendered.sql
    assert FORCING_TABLE_TOKEN not in rendered.sql
    assert rendered.store == store


@pytest.mark.parametrize(
    ("store", "body", "expected_table"),
    [
        ("legacy", LEGACY_BODY, FORCING_TABLE_LEGACY),
        ("narrow", NARROW_BODY, FORCING_TABLE),
    ],
)
def test_rendering_is_token_substitution_and_nothing_else(store, body, expected_table) -> None:
    """Invariant I6 as an EQUALITY, which every other assertion here stops short of.

    The rest of this module tests containment (``in`` / ``count`` / ``not in``),
    and containment cannot see a line that goes missing. If someone later ports
    river's ``_rename_table``, adds comment stripping, normalises whitespace or
    drops a line from the narrow path, every containment assertion still passes
    while the rendered statement has silently stopped being the registered one.
    This is the assertion that says the renderer SELECTS a variant and replaces a
    token, and does nothing else to the text a reader registered.
    """
    assert render_forcing_ts_sql(PAIR, store).sql == body.replace(FORCING_TABLE_TOKEN, expected_table)


def test_the_variants_are_two_independent_templates_not_one_minus_lines() -> None:
    """Invariant I6, made structural by :class:`ForcingTemplatePair`.

    The narrow variant is not the legacy one with lines deleted: it predicates on
    keys and enums, and it takes ``source_id`` from a joined
    ``met.forcing_version`` and ``basin_version_id`` from a joined
    ``met.met_station`` — two joins the legacy variant does not have, because the
    legacy fact row stores both columns itself.
    """
    legacy = render_forcing_ts_sql(PAIR, "legacy").sql
    narrow = render_forcing_ts_sql(PAIR, "narrow").sql

    assert "fst.basin_version_id" in legacy
    assert "fst.basin_version_id" not in narrow
    assert "JOIN met.met_station" in narrow
    assert "JOIN met.forcing_version" in narrow
    assert "JOIN met.met_station" not in legacy
    # Not derivable by deletion in either direction: each variant carries lines
    # the other does not.
    assert len(narrow.split("\n")) > len(legacy.split("\n"))


def test_every_token_occurrence_is_bound() -> None:
    """A self-join names the table twice; a single ``replace`` of the first would
    leave an unbound token in an executed statement."""
    body = f"SELECT 1 FROM {FORCING_TABLE_TOKEN} a JOIN {FORCING_TABLE_TOKEN} b ON a.station_id = b.station_id"
    rendered = render_forcing_ts_sql(ForcingTemplatePair(legacy=body, narrow=body), "legacy")
    assert rendered.sql.count(FORCING_TABLE_LEGACY) == 2
    assert FORCING_TABLE_TOKEN not in rendered.sql


def test_the_result_is_frozen() -> None:
    rendered = render_forcing_ts_sql(PAIR, "legacy")
    assert isinstance(rendered, RenderedForcingSql)
    with pytest.raises(FrozenInstanceError):
        rendered.sql = "SELECT 1"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The refusals. All of them raise ForcingTemplateError (a ValueError), never a
# bare assert: these run in production read paths, where `python -O` would strip
# an assert and ship the statement the check exists to stop.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", ["", "narow", "LEGACY", "both", None])
def test_an_unknown_store_is_refused_loudly(store) -> None:
    """A silent fallback to ``legacy`` on a typo would route a narrow-stored
    version's read at the legacy table and return zero rows, which reads as "no
    data" rather than as an error."""
    with pytest.raises(ForcingTemplateError, match="unknown timeseries store"):
        render_forcing_ts_sql(PAIR, store, entry="probe")


def test_the_error_names_the_entry() -> None:
    with pytest.raises(ForcingTemplateError, match="^forecast_store:station_series:"):
        render_forcing_ts_sql(PAIR, "sideways", entry="forecast_store:station_series")


@pytest.mark.parametrize("store", FORCING_STORES)
def test_a_template_without_the_token_is_refused(store) -> None:
    """A registered fact-table template that never names the fact table is
    misregistered, and rendering it would hand back a statement the renderer had
    no effect on."""
    body = "SELECT 1 FROM met.forcing_version WHERE forcing_version_id = %(forcing_version_id)s"
    with pytest.raises(ForcingTemplateError, match="carries no .* token"):
        render_forcing_ts_sql(ForcingTemplatePair(legacy=body, narrow=body), store, entry="probe")


@pytest.mark.parametrize(
    "spelling",
    [
        "met.forcing_station_timeseries",
        '"met"."forcing_station_timeseries"',
        "MET . forcing_station_timeseries",
        "met.forcing_station_timeseries_legacy",
    ],
)
@pytest.mark.parametrize("store", FORCING_STORES)
def test_a_hardcoded_table_name_is_refused_however_it_is_spelled(spelling, store) -> None:
    """D1's guard, over the CLASS of qualified spellings.

    A substring check for the canonical spelling sees neither the quoted nor the
    spaced form, and either would survive task 7.3's flip still pointing at
    whichever table the author happened to type. The ``_legacy`` case is what
    keeps the guard biting AFTER 7.3.
    """
    body = f"SELECT 1 FROM {FORCING_TABLE_TOKEN} a JOIN {spelling} b ON a.station_id = b.station_id"
    with pytest.raises(ForcingTemplateError, match="spells the fact table literally"):
        render_forcing_ts_sql(ForcingTemplatePair(legacy=body, narrow=body), store, entry="probe")


@pytest.mark.parametrize(
    "spelling",
    [
        "forcing_station_timeseries",
        "FORCING_STATION_TIMESERIES",
        '"forcing_station_timeseries"',
        "forcing_station_timeseries_legacy",
    ],
)
@pytest.mark.parametrize("store", FORCING_STORES)
def test_an_unqualified_table_name_is_refused_too(spelling, store) -> None:
    """The compensation river gets for free and forcing has to spell out.

    The census deliberately does NOT count the bare token — several sites spell
    the table unqualified on purpose (a manifest's logical name, a column list),
    and counting those would move most per-file numbers for no gain. River can
    afford the same blindness because its renderer is "counter permissive, walk
    strict, disagreement refuses"
    (``river_ts_render.fact_table_name_occurrences``), so an unqualified read is
    refused at render time. Nothing here plays that role, so without this refusal
    a template written ``FROM forcing_station_timeseries`` escapes the census AND
    the D1 guard, and after 7.3 resolves through ``search_path`` to the NARROW
    table with none of the legacy columns.
    """
    body = f"SELECT 1 FROM {FORCING_TABLE_TOKEN} a JOIN {spelling} b ON a.station_id = b.station_id"
    with pytest.raises(ForcingTemplateError, match="names the fact table without a schema"):
        render_forcing_ts_sql(ForcingTemplatePair(legacy=body, narrow=body), store, entry="probe")


def test_the_qualified_spelling_keeps_its_own_reason() -> None:
    """The two refusals stay distinct: a qualified literal is the D1 violation and
    says so, rather than being swallowed by the broader bare-token check that
    also matches it."""
    body = f"SELECT 1 FROM {FORCING_TABLE_TOKEN} a JOIN met.forcing_station_timeseries b ON a.x = b.x"
    with pytest.raises(ForcingTemplateError, match="spells the fact table literally"):
        render_forcing_ts_sql(ForcingTemplatePair(legacy=body, narrow=body), "legacy", entry="probe")


def test_the_unqualified_guard_does_not_fire_on_names_that_merely_start_with_it() -> None:
    """Non-vacuity, and river's rationale for the trailing ``\\b`` verbatim: an
    index name is not a read of the table, and refusing one would make a template
    that carries an index hint unregisterable."""
    body = (
        f"SELECT 1 FROM {FORCING_TABLE_TOKEN} fst "
        "-- planner note: forcing_station_timeseries_valid_time_idx\n"
        "WHERE fst.forcing_version_id = %(forcing_version_id)s"
    )
    rendered = render_forcing_ts_sql(ForcingTemplatePair(legacy=body, narrow=body), "legacy", entry="probe")
    assert "forcing_station_timeseries_valid_time_idx" in rendered.sql


def test_the_literal_guard_does_not_fire_on_the_other_met_tables() -> None:
    """Non-vacuity: the narrow variant MUST name ``met.forcing_version`` and
    ``met.met_station``, so a guard that refused any ``met.`` reference would make
    every narrow template unregisterable."""
    assert render_forcing_ts_sql(PAIR, "narrow").sql.count("JOIN met.") == 2


# Must-preserve M3 — no `#1342` transitional-aid marker in any forcing file — is
# deliberately NOT asserted here, and not anywhere else in the forcing set.
# Stating it as a test means writing the marker's verbatim tag into a forcing
# file, which is the very thing M3 forbids: the issue's acceptance check is a
# repository grep for that exact phrase, and it must not have this module as its
# only hit. The guarantee is structural and upstream of any assertion anyway:
# this renderer defines no marker, parses no comment and deletes no line, so
# there is nothing for a marker to do here.
