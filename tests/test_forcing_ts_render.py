"""The forcing renderer's own contract (#1990, task 7.2a; narrow-only since #1993).

Unit oracle for ``packages/common/forcing_ts_render.py``: table-name binding
through the D1 constant, and the refusals — including the refusal of every store
but ``narrow``. The discovery-set census is
``tests/test_forcing_ts_template_census.py``; the shape oracle over the nine
registered templates is ``tests/test_forcing_read_path_store_routing.py``.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from packages.common import forcing_ts_render
from packages.common.forcing_ts_render import (
    FORCING_STORES,
    FORCING_TABLE,
    FORCING_TABLE_TOKEN,
    ForcingTemplateError,
    ForcingTemplatePair,
    RenderedForcingSql,
    render_forcing_ts_sql,
)
from tests.forcing_ts_template_registry import REPO_ROOT

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

PAIR = ForcingTemplatePair(narrow=NARROW_BODY)


# ---------------------------------------------------------------------------
# D1 — the deployability property
# ---------------------------------------------------------------------------


def test_the_table_constant_spells_the_relation_the_migration_created() -> None:
    """The pin that keeps master deployable to node-27 (#1991, must-preserve M1).

    ``db/migrations/000061_forcing_station_timeseries_narrow_expand.sql`` creates
    the narrow table under the canonical name. The migration file is read here
    rather than trusted: this test's whole value is that the constant and the DDL
    agree, and asserting the constant against a second copy of the same string
    would only prove the string was typed twice.
    """
    assert FORCING_TABLE == "met.forcing_station_timeseries"

    migration = (
        REPO_ROOT / "db/migrations/000061_forcing_station_timeseries_narrow_expand.sql"
    ).read_text(encoding="utf-8")
    assert f"CREATE TABLE IF NOT EXISTS {FORCING_TABLE} (" in migration


def test_narrow_is_the_only_store_and_the_pair_has_one_field() -> None:
    """#1993's first cut, as a shape: one store, one template per reader.

    ``ForcingTemplatePair`` survives as a frozen dataclass with the single field
    ``narrow`` so the nine construction sites and the class-name-based discovery
    kept their shape; a second field coming back is a legacy template coming
    back.
    """
    assert FORCING_STORES == ("narrow",)
    assert [field.name for field in fields(ForcingTemplatePair)] == ["narrow"]
    with pytest.raises(TypeError):
        ForcingTemplatePair(legacy=NARROW_BODY, narrow=NARROW_BODY)  # type: ignore[call-arg]
    with pytest.raises(FrozenInstanceError):
        PAIR.narrow = "SELECT 1"  # type: ignore[misc]
    assert not hasattr(forcing_ts_render, "FORCING_TABLE_LEGACY")


def test_a_rename_is_the_one_line_it_claims_to_be(monkeypatch) -> None:
    """The constant is read at call time, so a rename needs no caller re-imported."""
    monkeypatch.setattr(forcing_ts_render, "FORCING_TABLE", "met.forcing_station_timeseries_renamed")

    assert "FROM met.forcing_station_timeseries_renamed fst" in render_forcing_ts_sql(PAIR, "narrow").sql


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


def test_rendering_binds_the_constant_and_leaves_no_token() -> None:
    rendered = render_forcing_ts_sql(PAIR, "narrow")
    assert f"FROM {FORCING_TABLE} fst" in rendered.sql
    assert FORCING_TABLE_TOKEN not in rendered.sql
    assert rendered.store == "narrow"


def test_rendering_is_token_substitution_and_nothing_else() -> None:
    """An EQUALITY, which every other assertion here stops short of.

    The rest of this module tests containment (``in`` / ``count`` / ``not in``),
    and containment cannot see a line that goes missing. If someone later ports
    river's ``_rename_table``, adds comment stripping, normalises whitespace or
    drops a line, every containment assertion still passes while the rendered
    statement has silently stopped being the registered one. This is the
    assertion that says the renderer replaces a token and does nothing else to
    the text a reader registered.
    """
    assert render_forcing_ts_sql(PAIR, "narrow").sql == NARROW_BODY.replace(FORCING_TABLE_TOKEN, FORCING_TABLE)


def test_every_token_occurrence_is_bound() -> None:
    """A self-join names the table twice; a single ``replace`` of the first would
    leave an unbound token in an executed statement."""
    body = f"SELECT 1 FROM {FORCING_TABLE_TOKEN} a JOIN {FORCING_TABLE_TOKEN} b ON a.station_key = b.station_key"
    rendered = render_forcing_ts_sql(ForcingTemplatePair(narrow=body), "narrow")
    assert rendered.sql.count(FORCING_TABLE) == 2
    assert FORCING_TABLE_TOKEN not in rendered.sql


def test_the_result_is_frozen() -> None:
    rendered = render_forcing_ts_sql(PAIR, "narrow")
    assert isinstance(rendered, RenderedForcingSql)
    with pytest.raises(FrozenInstanceError):
        rendered.sql = "SELECT 1"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The refusals. All of them raise ForcingTemplateError (a ValueError), never a
# bare assert: these run in production read paths, where `python -O` would strip
# an assert and ship the statement the check exists to stop.
# ---------------------------------------------------------------------------


def test_the_legacy_store_is_refused() -> None:
    """The store the expand window rendered has no template any more (#1993).

    Named on its own rather than left to the typo sweep below: ``"legacy"`` was a
    VALID store for the whole expand window, so it is the one stale value a
    caller can plausibly still pass, and silently answering it with the narrow
    statement would read the wrong table for the question asked.
    """
    with pytest.raises(ForcingTemplateError, match=r"probe: unknown timeseries store 'legacy'.*\['narrow'\]"):
        render_forcing_ts_sql(PAIR, "legacy", entry="probe")


@pytest.mark.parametrize("store", ["", "narow", "LEGACY", "both", None])
def test_an_unknown_store_is_refused_loudly(store) -> None:
    """A typo must not be answered with a statement nobody asked for."""
    with pytest.raises(ForcingTemplateError, match="unknown timeseries store"):
        render_forcing_ts_sql(PAIR, store, entry="probe")


def test_the_error_names_the_entry() -> None:
    with pytest.raises(ForcingTemplateError, match="^forecast_store:station_series:"):
        render_forcing_ts_sql(PAIR, "sideways", entry="forecast_store:station_series")


def test_a_template_without_the_token_is_refused() -> None:
    """A registered fact-table template that never names the fact table is
    misregistered, and rendering it would hand back a statement the renderer had
    no effect on."""
    body = "SELECT 1 FROM met.forcing_version WHERE forcing_version_id = %(forcing_version_id)s"
    with pytest.raises(ForcingTemplateError, match="carries no .* token"):
        render_forcing_ts_sql(ForcingTemplatePair(narrow=body), "narrow", entry="probe")


@pytest.mark.parametrize(
    "spelling",
    [
        "met.forcing_station_timeseries",
        '"met"."forcing_station_timeseries"',
        "MET . forcing_station_timeseries",
        "met.forcing_station_timeseries_legacy",
    ],
)
def test_a_hardcoded_table_name_is_refused_however_it_is_spelled(spelling) -> None:
    """D1's guard, over the CLASS of qualified spellings.

    A substring check for the canonical spelling sees neither the quoted nor the
    spaced form. The ``_legacy`` case is the renamed table, which no template
    may name at all.
    """
    body = f"SELECT 1 FROM {FORCING_TABLE_TOKEN} a JOIN {spelling} b ON a.station_key = b.station_key"
    with pytest.raises(ForcingTemplateError, match="spells the fact table literally"):
        render_forcing_ts_sql(ForcingTemplatePair(narrow=body), "narrow", entry="probe")


@pytest.mark.parametrize(
    "spelling",
    [
        "forcing_station_timeseries",
        "FORCING_STATION_TIMESERIES",
        '"forcing_station_timeseries"',
    ],
)
def test_an_unqualified_table_name_is_refused_too(spelling) -> None:
    """The compensation river gets for free and forcing has to spell out.

    The census deliberately does NOT count the bare token — several sites spell
    the table unqualified on purpose (a manifest's logical name, a column list),
    and counting those would move most per-file numbers for no gain. River can
    afford the same blindness because its renderer is "counter permissive, walk
    strict, disagreement refuses"
    (``river_ts_render.fact_table_name_occurrences``), so an unqualified read is
    refused at render time. Nothing here plays that role, so without this refusal
    a template written ``FROM forcing_station_timeseries`` escapes the census AND
    the D1 guard, and resolves through ``search_path``.
    """
    body = f"SELECT 1 FROM {FORCING_TABLE_TOKEN} a JOIN {spelling} b ON a.station_key = b.station_key"
    with pytest.raises(ForcingTemplateError, match="names the fact table without a schema"):
        render_forcing_ts_sql(ForcingTemplatePair(narrow=body), "narrow", entry="probe")


def test_the_qualified_spelling_keeps_its_own_reason() -> None:
    """The two refusals stay distinct: a qualified literal is the D1 violation and
    says so, rather than being swallowed by the broader bare-token check that
    also matches it."""
    body = f"SELECT 1 FROM {FORCING_TABLE_TOKEN} a JOIN met.forcing_station_timeseries b ON a.x = b.x"
    with pytest.raises(ForcingTemplateError, match="spells the fact table literally"):
        render_forcing_ts_sql(ForcingTemplatePair(narrow=body), "narrow", entry="probe")


def test_the_unqualified_guard_does_not_fire_on_names_that_merely_start_with_it() -> None:
    """Non-vacuity, and river's rationale for the trailing ``\\b`` verbatim: an
    index name is not a read of the table, and refusing one would make a template
    that carries an index hint unregisterable."""
    body = (
        f"SELECT 1 FROM {FORCING_TABLE_TOKEN} fst "
        "-- planner note: forcing_station_timeseries_valid_time_idx\n"
        "WHERE fst.forcing_version_key = %(forcing_version_key)s"
    )
    rendered = render_forcing_ts_sql(ForcingTemplatePair(narrow=body), "narrow", entry="probe")
    assert "forcing_station_timeseries_valid_time_idx" in rendered.sql


def test_the_literal_guard_does_not_fire_on_the_other_met_tables() -> None:
    """Non-vacuity: a template MUST name ``met.forcing_version`` and
    ``met.met_station``, so a guard that refused any ``met.`` reference would make
    every template unregisterable."""
    assert render_forcing_ts_sql(PAIR, "narrow").sql.count("JOIN met.") == 2


# Must-preserve M3 — no `#1342` transitional-aid marker in any forcing file — is
# deliberately NOT asserted here, and not anywhere else in the forcing set.
# Stating it as a test means writing the marker's verbatim tag into a forcing
# file, which is the very thing M3 forbids: the issue's acceptance check is a
# repository grep for that exact phrase, and it must not have this module as its
# only hit. The guarantee is structural and upstream of any assertion anyway:
# this renderer defines no marker, parses no comment and deletes no line, so
# there is nothing for a marker to do here.
