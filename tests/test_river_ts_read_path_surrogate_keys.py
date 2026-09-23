"""Shape pins for the display-boundary ``hydro.river_timeseries`` read switch (#1341).

Every in-boundary reader of the fact table now filters on the integer
surrogate keys and the enum columns (000050's shape, 000051's index) while its
external contract stays byte-identical: the caller still supplies text
identity, and the text comes back out through authority joins and enum casts.

What these tests are for, and what they deliberately are not:

* They pin the SQL *shape*, because the shape is the contract with the index
  and the thing a future edit can silently undo. Whether the switched queries
  actually return the same rows is answered by
  ``tests/test_river_ts_read_path_surrogate_keys_integration.py`` against a
  real PostgreSQL — a text-substring test can never answer it.
* Every text-predicate assertion runs on ``outer_predicates`` (see
  ``tests/test_sql_shape_helpers.py``): sub-selects stripped, comments removed,
  whitespace collapsed. The key-resolution sub-selects contain ``WHERE run_id =
  :run_id`` against the AUTHORITY table by design, so a raw-source pin goes
  green on code that never switched a thing.

The hybrid pushdown rule these pins used to encode (user-adjudicated, #1341)
is RETIRED. Every in-boundary fact read used to keep a redundant TEXT predicate
on ``run_id`` / ``river_network_version_id`` / ``variable``, AND-ed with its key
or enum counterpart, for as long as compression segmented compressed chunks by
the text columns. #1342's contract (task 6.3) dropped
``hydro.river_timeseries_legacy`` and the routing column with it, so there is no
text-segmented chunk left to push into and no text identity column on the
surviving table to push with. The pins are therefore one-sided now:

* the key / enum predicates are pinned literally — they are the row-selection
  authority and the contract with 000051's discovery index;
* the referenced fact-table text column set is pinned EQUAL TO THE EMPTY SET on
  every switched surface, so a reintroduced text predicate is red here before it
  reaches a table that has no such column.

The ``hydro-national`` ``CROSS JOIN LATERAL`` probe shape is kept for its own
reason, which never was the aids: inside the lateral the discovery row is a
per-loop constant, so the key equalities are index conditions instead of a
set-based join the planner cannot estimate (#1341 round 3, #1596).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common import display_coverage
from packages.common.river_ts_render import fact_table_text_identity_columns, render_river_ts_sql
from services.production_closure.scale_validation import HYDRO_MAP_PLAN_LINES_BY_STORE, QUERY_TARGETS
from services.tiles.mvt import (
    _mvt_tile_order_by,
    _valid_times_any_source_template,
    _valid_times_named_source_template,
    postgis_tile_sql,
    valid_times_for_layer,
)
from tests.river_ts_template_registry import (
    AID_MARKER_TAG,
    NON_TEMPLATE_MENTIONS,
    REGISTRY,
    assert_marker_census,
)
from tests.test_river_ts_text_identity_cleanup import _river_table_mentions
from tests.test_sql_shape_helpers import (
    FORBIDDEN_TEXT_FACT_COLUMNS,
    LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS,
    outer_predicates,
    sql_from_python,
)
from tests.test_sql_shape_helpers import assert_text_fact_columns as _assert_text_fact_columns

REPO_ROOT = Path(__file__).resolve().parents[1]
MVT_SOURCE = (REPO_ROOT / "services" / "tiles" / "mvt.py").read_text(encoding="utf-8")
# #2026: the existence probe (`_require_hydro_mvt_source_identity`) moved to the
# identity owner module when the facade was split; both `_slice` anchors below
# (that def and `def _require_run_source_identity`) are adjacent in that file.
HYDRO_DISPLAY_SOURCE = (REPO_ROOT / "apps" / "api" / "routes" / "hydro_display_identity.py").read_text(
    encoding="utf-8"
)
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"


def test_narrow_hydro_map_plan_uses_expand_contract_indexes() -> None:
    plan = "\n".join(HYDRO_MAP_PLAN_LINES_BY_STORE["narrow"])
    assert "river_ts_segment_time_key_idx on hydro.river_timeseries" in plan
    assert "river_timeseries_run_valid_idx" not in plan
    assert "variable =" not in plan
    assert HYDRO_MAP_PLAN_LINES_BY_STORE["legacy"] == QUERY_TARGETS["hydro_map"]["plan_lines"]

# The four authority resolutions, verbatim. Any switched surface that binds a
# given identity must resolve it exactly this way — one primary-key point
# lookup the planner hoists into an InitPlan.
RUN_KEY_RESOLUTION = "SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id"
BASIN_KEY_RESOLUTION = "SELECT basin_version_key FROM core.basin_version"
NETWORK_KEY_RESOLUTION = "SELECT river_network_version_key FROM core.river_network_version"
# Sargable AND out-of-vocabulary-safe. `:variable::hydro.river_variable` would
# raise 22P02 on an unknown literal, upgrading the "empty result" contract into
# a 500; enum_range simply matches nothing.
ENUM_VARIABLE_RESOLUTION = "SELECT e FROM unnest(enum_range(NULL::hydro.river_variable)) e"

# The row-selection authority of the tile point lookup, spelled the way the
# surface binds it. ONE substring per chain so the assertion is about ADJACENCY
# in a single conjunction, not about literals existing somewhere in the query.
# Each of these used to be half of a key/aid pair; the contract (task 6.3)
# deleted the text halves, and what is pinned is that the key halves survived
# the deletion in the same chain and the same order.
BOUND_PARAM_KEY_PREDICATES = (
    ("run_id", "ts.run_key = AND ts.basin_version_key ="),
    ("river_network_version_id", "ts.river_network_version_key = AND ts.variable_e ="),
    ("variable", "ts.variable_e = AND ts.valid_time = :valid_time"),
)


def _slice(source: str, start_anchor: str, end_anchor: str) -> str:
    start = source.index(start_anchor)
    end = source.index(end_anchor, start)
    assert end > start
    return source[start:end]


def _source_cte(layer: str) -> str:
    return _slice(postgis_tile_sql(layer), "source_rows AS NOT MATERIALIZED (", "source_identity_stats AS (")


def _identity_stats_cte(layer: str) -> str:
    return _slice(postgis_tile_sql(layer), "source_identity_stats AS (", "bounded_rows AS (")


# The equality-plus-ceiling assertion this file used to define privately now
# lives in ``tests/test_sql_shape_helpers.py`` (#1442): the out-of-boundary
# cleanup oracle asserts the same invariant on its own surfaces, and one
# ceiling shared beats two that can drift. Imported under the old private name
# so every call site below is unchanged.


# ---------------------------------------------------------------------------
# hydro layer (single-run discharge tile)
# ---------------------------------------------------------------------------


def test_hydro_tile_resolves_text_identity_to_keys_and_restores_text_output() -> None:
    source_cte = _source_cte("hydro")

    # Predicates: four keys plus the enum, all resolved from the caller's text.
    assert "ts.run_key = (" in source_cte
    assert RUN_KEY_RESOLUTION in source_cte
    assert "ts.basin_version_key = (" in source_cte
    assert BASIN_KEY_RESOLUTION in source_cte
    assert "ts.river_network_version_key = (" in source_cte
    assert NETWORK_KEY_RESOLUTION in source_cte
    assert "ts.variable_e = (" in source_cte
    assert ENUM_VARIABLE_RESOLUTION in source_cte
    assert "ts.valid_time = :valid_time" in source_cte

    # The two-column text join to the segment authority collapses to the one
    # key that encodes the same (segment, network) pair.
    assert "ON rs.river_segment_key = ts.river_segment_key" in source_cte
    assert "rs.river_network_version_id = ts.river_network_version_id" not in source_cte

    # Output restoration: bound params for the equality-predicated identity,
    # the authority join for the segment, enum labels for unit/quality/variable.
    for restored in (
        "(:run_id)::text AS run_id",
        "(:basin_version_id)::text AS basin_version_id",
        "(:river_network_version_id)::text AS river_network_version_id",
        "rs.river_segment_id AS segment_id",
        "ts.unit_e::text AS unit",
        "ts.quality_flag_e::text AS quality_flag",
        "ts.variable_e::text AS variable",
    ):
        assert restored in source_cte, restored

    _assert_text_fact_columns(source_cte, "ts", set(), "hydro tile")


def test_hydro_tile_carries_its_key_predicates_and_no_text_aid() -> None:
    """The tile point lookup binds every identity as a resolved key constant.

    It was the one in-boundary surface that carried all three transitional
    pushdown aids, so it is where their removal is pinned hardest: the key /
    enum chain is unbroken and adjacent, and not one text identity column is
    predicated on. A surviving aid here is a read of a column
    ``hydro.river_timeseries`` does not have (#1342 contract, task 6.3).
    """
    outer = outer_predicates(_source_cte("hydro"))

    for column, chain in BOUND_PARAM_KEY_PREDICATES:
        assert chain in outer, column
    assert "WHERE ts.run_key = AND" in outer
    assert "AND ts.valid_time = :valid_time" in outer
    assert AID_MARKER_TAG not in _source_cte("hydro")


def test_hydro_tile_feature_id_concatenation_is_unchanged() -> None:
    """``feature_id`` is a wire value: network || '::' || segment, byte for byte.

    Cross-checked against the river-network layer's concatenation, which this
    change does not touch — if the separator or the operand order ever drifts
    on one side only, the two stop agreeing here.
    """
    hydro_cte = _source_cte("hydro")
    untouched_cte = _source_cte("river-network")

    assert "((:river_network_version_id)::text || '::' || rs.river_segment_id) AS feature_id" in hydro_cte
    assert "(rs.river_network_version_id || '::' || rs.river_segment_id) AS feature_id" in untouched_cte

    national_cte = _source_cte("hydro-national")
    assert "(sv.river_network_version_id || '::' || sv.river_segment_id) AS feature_id" in national_cte

    # All three concatenate <network text> || '::' || <segment text>: same
    # separator, same operand order, no key ever reaching the wire.
    concatenations = re.findall(
        r"\(\s*[^()]*?\|\|\s*'::'\s*\|\|[^()]*?\)\s+AS feature_id",
        hydro_cte + national_cte,
    )
    assert concatenations
    for concatenation in concatenations:
        assert "_key" not in concatenation, concatenation


def test_variable_predicates_are_enum_range_matches_not_casts_on_every_switched_surface() -> None:
    """An out-of-vocabulary ``variable`` must stay an empty result, not a 22P02.

    ``:variable::hydro.river_variable`` is the tempting one-liner and it is
    exactly what must not appear: it turns an unknown literal into a SQL error
    that escapes to the caller, breaking the must-preserve behaviour of the
    text era.
    """
    surfaces = {
        "hydro tile": _source_cte("hydro"),
        "national tile": _source_cte("hydro-national"),
        "national identity probe": _identity_stats_cte("hydro-national"),
        **{
            f"valid_times {form}": render_river_ts_sql(factory("narrow"), "narrow").sql
            for form, factory in (
                ("named", _valid_times_named_source_template),
                ("any", _valid_times_any_source_template),
            )
        },
        "existence probe": _slice(
            HYDRO_DISPLAY_SOURCE,
            "def _require_hydro_mvt_source_identity",
            "def _require_run_source_identity",
        ),
    }
    for name, sql in surfaces.items():
        assert ENUM_VARIABLE_RESOLUTION in sql, name
        assert "WHERE e::text = :variable" in sql, name
        for forbidden_cast in (
            ":variable::hydro.river_variable",
            "(:variable)::hydro.river_variable",
            "%(variable)s::hydro.river_variable",
        ):
            assert forbidden_cast not in sql, f"{name}: {forbidden_cast}"


# ---------------------------------------------------------------------------
# hydro-national layer (both UNION ALL legs + the identity probe)
# ---------------------------------------------------------------------------


def _national_legs() -> tuple[tuple[str, str], ...]:
    national_cte = _source_cte("hydro-national")
    typed = _slice(national_cte, "typed_values AS (", "untyped_ranked AS (")
    untyped = _slice(national_cte, "untyped_ranked AS (", "selected_values AS (")
    return (("typed_values", typed), ("untyped_ranked", untyped))


def _lateral_probe(leg: str) -> str:
    """The leg's ``CROSS JOIN LATERAL`` body — the only place ``ts`` may appear."""
    return _slice(leg, "CROSS JOIN LATERAL (", ") v")


def _probe_body(probe: str) -> str:
    """The probe's single fact read.

    The routing wiring made this a two-branch ``UNION ALL`` told apart by
    ``lr.timeseries_store``; #1342's contract (task 6.3) deleted the column and
    the second physical table, so there is one branch and the absence of the
    other is asserted rather than assumed.
    """
    assert "UNION ALL" not in probe
    assert "timeseries_store" not in probe
    assert "hydro.river_timeseries_legacy" not in probe
    assert probe.count("LIMIT 1") == 1
    assert probe.rstrip().endswith("LIMIT 1")
    assert probe.count("FROM hydro.river_timeseries") == 1
    assert "FROM hydro.river_timeseries ts" in probe
    return probe


def _assert_probe_predicates(probe: str, expected: str) -> None:
    body = _probe_body(probe)
    assert expected in outer_predicates(body)
    _assert_text_fact_columns(
        body, "ts", set(), "lateral probe", allowed=LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS,
    )
    assert ENUM_VARIABLE_RESOLUTION in body


# The probe's whole conjunction, canonicalised (comments gone, whitespace
# collapsed, the enum sub-select stripped). ONE substring, not seven `in`
# checks: what has to hold is that every key predicate lives in the SAME `AND`
# chain of the SAME correlated probe, and that the `LIMIT 1` fence terminates
# it. Drop the fence and the planner is free to pull the subquery up into the
# set-based join this replaced. The three transitional text aids that used to
# sit between the keys and the enum are gone with #1342's contract (task 6.3),
# so their ABSENCE is part of this one substring too — an aid re-inserted
# anywhere in the chain breaks the match.
NATIONAL_LATERAL_PROBE_PREDICATES = (
    "FROM hydro.river_timeseries ts "
    "WHERE ts.run_key = lr.run_key "
    "AND ts.river_network_version_key = lr.river_network_version_key "
    "AND ts.river_segment_key = seg.river_segment_key "
    "AND ts.variable_e = "
    "AND ts.valid_time = :valid_time "
    "LIMIT 1"
)

# The identity-existence probe's conjunction, same one-substring discipline
# (#1596). It is the data legs' probe minus everything per-segment: no
# `river_segment_key` predicate. `lr` here is the probe's OWN inline discovery
# sub-select, not the `latest_runs` CTE — that CTE lives inside the
# `source_rows` sub-query's WITH and is not in scope for this sibling CTE.
NATIONAL_IDENTITY_PROBE_PREDICATES = (
    "FROM hydro.river_timeseries ts "
    "WHERE ts.run_key = lr.run_key "
    "AND ts.river_network_version_key = lr.river_network_version_key "
    "AND ts.variable_e = "
    "AND ts.valid_time = :valid_time "
    "LIMIT 1"
)
# The four columns the inline discovery hands the probe: two keys and the two
# text identities the projection echoes back out. The fifth — the candidate's
# authoritative store — went with the routing column (#1342 contract, task 6.3).
NATIONAL_IDENTITY_DISCOVERY_COLUMNS = (
    "SELECT DISTINCT ON (mi.river_network_version_id) "
    "h.run_key, rnv.river_network_version_key, "
    "h.run_id, mi.river_network_version_id "
    "FROM hydro.hydro_run h"
)


def test_national_tile_probes_the_fact_table_once_per_segment_through_a_lateral() -> None:
    """Both legs read the fact table as a per-segment correlated probe.

    The set-based ``latest_runs JOIN river_timeseries JOIN tile_segments``
    shape this replaced was a verified P1 regression: ``tile_segments`` is a
    spatial CTE the planner cannot estimate (node-27 z4: est ~50 rows vs 3,500
    actual, unchanged by ANALYZE), so it materialised the whole run slice and
    join-filtered it segment by segment — 56,567 x 3,500 comparisons, 34.7s
    against 0.77s for the pre-switch text query. Only the drive order plus the
    ``LIMIT 1`` fence make the plan deterministic, so the shape itself is
    pinned here, not merely the predicates it carries.
    """
    for name, leg in _national_legs():
        collapsed = outer_predicates(leg)

        assert (
            "FROM tile_segments seg JOIN latest_runs lr "
            "ON lr.river_network_version_id = seg.river_network_version_id "
            "CROSS JOIN LATERAL (" in collapsed
        ), name
        _assert_probe_predicates(_lateral_probe(leg), NATIONAL_LATERAL_PROBE_PREDICATES)
        # The old set-based fact join is gone, not merely supplemented.
        assert "JOIN hydro.river_timeseries" not in leg, name
        # And nothing reads the fact table outside the probe: a second, uncorrelated
        # `ts` reference would be the unestimable join creeping back in.
        assert "ts." not in leg.replace(_lateral_probe(leg), ""), name


def test_national_leg_projections_and_percent_rank_read_the_probe_result() -> None:
    """The payload comes out of the lateral alias, the ranking window with it.

    ``untyped_ranked`` ranks by value inside one national identity, so its
    window must partition on the network key ``latest_runs`` already carries
    and order by the probed value — reaching back to the fact table for either
    would be the second, uncorrelated scan the lateral exists to prevent.
    """
    (_typed_name, typed), (_untyped_name, untyped) = _national_legs()

    for name, leg in _national_legs():
        for projection in (
            "v.basin_version_key,",
            "v.value,",
            "v.unit,",
            "v.quality_flag,",
            "v.variable,",
            "v.valid_time",
        ):
            assert projection in leg, f"{name}: {projection}"

    assert "value_percent_rank" not in typed
    assert (
        "CASE WHEN v.value IS NULL THEN NULL "
        "ELSE PERCENT_RANK() OVER ( PARTITION BY lr.river_network_version_key ORDER BY v.value ) "
        "END AS value_percent_rank" in outer_predicates(untyped)
    )


def test_per_basin_hydro_layer_keeps_one_point_lookup() -> None:
    """One scalar-key read, never a national per-segment probe.

    The routing wiring made this two scalar-key branches under a ``UNION ALL``;
    #1342's contract (task 6.3) left one, and the per-basin layer must not have
    picked up the national layer's LATERAL shape on the way back down.
    """
    hydro_cte = _source_cte("hydro")

    assert "LATERAL" not in hydro_cte
    assert "LIMIT 1" not in hydro_cte
    assert "UNION ALL" not in hydro_cte
    assert "timeseries_store" not in hydro_cte
    assert "hydro.river_timeseries_legacy" not in hydro_cte
    assert "FROM hydro.river_timeseries ts" in hydro_cte
    assert hydro_cte.count("FROM hydro.river_timeseries") == 1
    _assert_text_fact_columns(hydro_cte, "ts", set(), "hydro tile")


def test_national_tile_switches_both_zoom_legs_to_keys() -> None:
    for name, leg in _national_legs():
        probe = _lateral_probe(leg)

        _assert_probe_predicates(probe, NATIONAL_LATERAL_PROBE_PREDICATES)
        assert (
            "SELECT ts.basin_version_key, ts.value, ts.unit_e::text AS unit, "
            "ts.quality_flag_e::text AS quality_flag, ts.variable_e::text AS variable, ts.valid_time"
        ) in outer_predicates(probe)

    # latest_runs hands both legs the keys AND the text it will echo back out,
    # from the authority join it was already doing.
    national_cte = _source_cte("hydro-national")
    latest_runs = _slice(national_cte, "WITH latest_runs AS MATERIALIZED (", "network_stream_max AS")
    assert "h.run_id, mi.river_network_version_id," in latest_runs
    assert "h.run_key, rnv.river_network_version_key" in latest_runs
    assert "timeseries_store" not in latest_runs
    assert "JOIN core.river_network_version rnv" in latest_runs


def test_national_null_key_visibility_cannot_split_between_the_two_zoom_branches() -> None:
    """z<9 and z>=9 must see exactly the same fact rows for one national identity.

    ``typed_values`` (z>=9 branch) and ``untyped_ranked`` (z<9 branch) are the
    same read under two zoom guards. If one leg kept the text predicates a
    NULL-key row would render at one zoom and vanish at the other for the same
    tile — the split this change exists to prevent. Comparing the two legs'
    complete set of fact-table column references, rather than a list of
    literals, is what makes a one-sided edit red — and after #1342's contract
    (task 6.3) the set is the keys, the enums and the value, with no text
    identity column on either side.
    """
    (_typed_name, typed), (_untyped_name, untyped) = _national_legs()

    expected_columns = {
        "ts.run_key", "ts.river_network_version_key", "ts.river_segment_key",
        "ts.basin_version_key", "ts.variable_e", "ts.valid_time", "ts.value",
        "ts.unit_e", "ts.quality_flag_e",
    }
    typed_columns = set(re.findall(r"\bts\.[a-z_]+", _probe_body(_lateral_probe(typed))))
    untyped_columns = set(re.findall(r"\bts\.[a-z_]+", _probe_body(_lateral_probe(untyped))))
    assert typed_columns == untyped_columns == expected_columns

    # The two probes are literally the same text, so no predicate can drift
    # between them at all.
    assert _lateral_probe(typed) == _lateral_probe(untyped)

    # Non-vacuity: the legs really are the two zoom branches, not a copy-paste
    # of one clause into a variable.
    assert ":z >= 9" in typed
    assert ":z < 9" in untyped


def test_national_identity_probe_uses_the_same_key_shape_as_the_data_legs() -> None:
    """The existence probe is a per-identity LATERAL, like the data legs (#1596).

    It used to join the fact table to its discovery sub-select set-based, on
    keys alone. Compression segments compressed chunks by the TEXT columns, so
    a join-column key equality pushes nothing down: node-27 measured 23-37s per
    compressed instant, and 38s to answer an UNCOVERED instant — a tile with no
    features at all — because the planner could no longer prove the slice empty
    without decompressing it. Inside the LATERAL the discovery row is a
    per-loop constant, so the two text aids reach the segmentby index.

    What must NOT drift: the probe still reads the fact table (the coverage
    window is a MIN/MAX over complete instants, so answering existence from it
    alone flips an interior gap's 424 into an empty-tile 200), and it carries no
    per-segment predicate — it has no per-segment correlation — which is why its
    chain is a strict prefix of the data legs'.

    The two text aids this docstring's measurement paragraph was written for are
    gone with #1342's contract (task 6.3); the LATERAL shape is kept because the
    key equalities are index conditions only when the discovery row is a
    per-loop constant.
    """
    probe = _identity_stats_cte("hydro-national")
    lateral = _slice(probe, "CROSS JOIN LATERAL (", ") hit")

    # Shape: inline 5-column discovery, cross-joined laterally to the probe,
    # the whole thing still wrapped in the EXISTS the 0/1 semantics come from.
    assert NATIONAL_IDENTITY_DISCOVERY_COLUMNS in outer_predicates(probe)
    assert "JOIN core.river_network_version rnv" in probe
    assert "SELECT CASE WHEN EXISTS (" in probe
    assert ") lr CROSS JOIN LATERAL (" in outer_predicates(probe)
    assert "JOIN hydro.river_timeseries" not in probe
    # Nothing reads the fact table outside the probe body: a second `ts`
    # reference would be the unpushable set-based join creeping back in.
    assert "ts." not in probe.replace(lateral, "")

    # The whole conjunction, one substring: every key predicate and its
    # transitional text aid in the SAME `AND` chain, terminated by the `LIMIT 1`
    # fence that keeps the planner from pulling the probe back up into a join.
    _assert_probe_predicates(lateral, NATIONAL_IDENTITY_PROBE_PREDICATES)
    assert probe.count("LIMIT 1") == 2
    assert ") hit LIMIT 1 ) THEN 1 ELSE 0 END AS source_identity_count" in outer_predicates(probe)
    assert "SELECT 1 FROM" in outer_predicates(lateral)
    # The identity probe's chain is a strict prefix of the data legs' — same
    # keys minus the per-segment one — which is what makes "same shape" a
    # measurement rather than a claim.
    assert NATIONAL_IDENTITY_PROBE_PREDICATES.removesuffix(
        "AND ts.variable_e = AND ts.valid_time = :valid_time LIMIT 1"
    ) in NATIONAL_LATERAL_PROBE_PREDICATES
    assert "river_segment_id" not in probe


def test_national_output_and_ordering_stay_on_restored_text_expressions() -> None:
    """Integer key order is not text order — the response array must not reorder."""
    assert _mvt_tile_order_by("hydro-national") == "river_network_version_id, river_segment_id"
    assert _mvt_tile_order_by("hydro") == "river_network_version_id, river_segment_id"

    national_cte = _source_cte("hydro-national")
    # The ordered columns are produced from the authority tables' text, not
    # from the keys the fact predicates use. Both now come off `tile_segments`,
    # which the lateral probe drives from; `lr` still supplies `run_id`.
    assert "seg.river_network_version_id," in national_cte
    assert "SELECT seg.river_segment_id," in national_cte
    assert "lr.run_id," in national_cte
    assert "sv.river_network_version_id," in national_cte
    assert "sv.river_segment_id AS segment_id," in national_cte
    assert "bv.basin_version_id," in national_cte
    assert "ON bv.basin_version_key = sv.basin_version_key" in national_cte
    assert "ON rs.river_segment_key = sv.river_segment_key" in national_cte

    # No ORDER BY anywhere in the tile module sorts on a surrogate key.
    for ordering in re.findall(r"ORDER BY[^\n]*", MVT_SOURCE):
        assert "_key" not in ordering, ordering


# ---------------------------------------------------------------------------
# valid_times_for_layer (both branches) and the existence probe
# ---------------------------------------------------------------------------


def test_valid_times_named_identity_branch_keeps_the_four_column_index_prefix() -> None:
    """The four-column discovery-index prefix, with no text aid left beside it.

    The branch used to be rendered per store and the legacy variant carried a
    text conjunct next to three of these four keys. #1342's contract (task 6.3)
    deleted the legacy variant and the aids; the prefix that makes 000051's
    integer discovery index usable is what had to survive, in this order.
    """
    named = render_river_ts_sql(_valid_times_named_source_template("narrow"), "narrow").sql
    outer = outer_predicates(named)
    assert "SELECT h.run_key FROM hydro.hydro_run h" in named
    assert "WHERE h.run_id = :run_id" in named
    assert "timeseries_store" not in named
    assert (
        "WHERE run_key = AND basin_version_key = AND river_network_version_key = AND variable_e ="
    ) in outer
    assert fact_table_text_identity_columns(named) == frozenset()
    for forbidden in FORBIDDEN_TEXT_FACT_COLUMNS:
        assert re.search(rf"\b{forbidden}\b", outer) is None, forbidden


def test_valid_times_no_named_identity_branch_also_filters_the_enum_column() -> None:
    """Any identity has run-key authority, never user identity filters.

    Pinned as the WHOLE collapsed statement: the branch is small enough that an
    equality says everything a list of substrings would, and it is the assertion
    that goes red if a store predicate, a text aid or an extra identity filter
    is reintroduced anywhere in it.
    """
    no_named = render_river_ts_sql(_valid_times_any_source_template("narrow"), "narrow").sql
    assert "WHERE ts.variable_e = (" in no_named
    assert ENUM_VARIABLE_RESOLUTION in no_named
    assert "WHERE e::text = :variable" in no_named
    outer = outer_predicates(no_named)
    assert outer == (
        "SELECT ts.valid_time FROM hydro.river_timeseries ts WHERE ts.variable_e = "
        "AND EXISTS ( SELECT 1 FROM hydro.hydro_run h WHERE h.run_key = ts.run_key )"
    )
    assert fact_table_text_identity_columns(no_named) == frozenset()
    for column in (
        "run_id", "basin_version_key", "river_network_version_key",
        "river_network_version_id", "timeseries_store", "status", "active", "run_display_coverage",
        *FORBIDDEN_TEXT_FACT_COLUMNS,
    ):
        assert re.search(rf"\b{column}\b", outer) is None, column


def test_valid_times_for_layer_capture_session_preserves_outer_limit_semantics_for_named_and_any_identity() -> None:
    class _Session:
        def __init__(self) -> None:
            self.executions: list[tuple[str, dict[str, Any]]] = []

        def execute(self, statement: Any, params: dict[str, Any]) -> _Session:
            self.executions.append((str(statement), params))
            return self

        def mappings(self) -> _Session:
            return self

        def all(self) -> list[dict[str, datetime]]:
            return [
                {"valid_time": datetime(2026, 6, 1, hour, tzinfo=UTC)}
                for hour in (2, 1, 0)
            ]

    for run_id, factory in (
        ("selected-run", _valid_times_named_source_template),
        (None, _valid_times_any_source_template),
    ):
        session = _Session()
        discovery = valid_times_for_layer(
            session, "discharge", run_id=run_id,
            basin_version_id="selected-basin", river_network_version_id="selected-network", limit=2,
        )
        assert len(session.executions) == 1
        sql, params = session.executions[0]
        normalized = " ".join(sql.split())
        prefix = "SELECT DISTINCT valid_time FROM ( "
        suffix = " ) source_rows ORDER BY valid_time DESC LIMIT :limit"
        assert normalized.startswith(prefix)
        assert normalized.endswith(suffix)
        # One arm, not two: the store union went with the routing column
        # (#1342 contract, task 6.3). The outer DISTINCT / ORDER BY / LIMIT are
        # what make the discovery contract, and they must stay OUTSIDE the arm.
        arm = normalized[len(prefix):-len(suffix)]
        assert "UNION ALL" not in normalized
        raw = factory("narrow")
        assert re.search(r"\b(?:DISTINCT|ORDER|LIMIT|UNION)\b", raw) is None
        rendered = render_river_ts_sql(raw, "narrow").sql
        assert arm == " ".join(rendered.split())
        assert "timeseries_store" not in arm
        assert fact_table_text_identity_columns(rendered) == frozenset()
        for operator in ("SELECT DISTINCT", "ORDER BY", "LIMIT"):
            assert normalized.count(operator) == 1
        assert params == {
            "run_id": run_id, "basin_version_id": "selected-basin",
            "river_network_version_id": "selected-network", "variable": "q_down", "limit": 3,
        }
        assert discovery.valid_times == ["2026-06-01T01:00:00Z", "2026-06-01T02:00:00Z"]
        assert discovery.limit == 2
        assert discovery.observed_count == 3
        assert discovery.truncated is True


def test_existence_probe_switches_to_keys_without_touching_its_404_contract() -> None:
    probe = _slice(
        HYDRO_DISPLAY_SOURCE,
        "def _require_hydro_mvt_source_identity",
        "def _require_run_source_identity",
    )

    assert "FROM hydro.river_timeseries" in probe
    # #1980 moved the key predicate onto the WHERE line with the aid beneath it;
    # #1342's contract (task 6.3) deleted the aid, so the key predicate is the
    # WHERE-line conjunct on its own.
    assert "WHERE run_key = (" in probe
    assert RUN_KEY_RESOLUTION in probe
    assert "AND basin_version_key = (" in probe
    assert BASIN_KEY_RESOLUTION in probe
    assert "AND river_network_version_key = (" in probe
    assert NETWORK_KEY_RESOLUTION in probe
    assert "AND variable_e = (" in probe
    assert "AND valid_time = :valid_time" in probe

    outer = outer_predicates(sql_from_python(probe))
    # Pinned as the WHOLE collapsed statement: four key/enum resolutions and the
    # instant, in this order, with no text conjunct between any of them. An
    # equality rather than five `in` checks, because the failure this guards
    # against is an EXTRA predicate, which no `in` check can see.
    assert outer == (
        "SELECT 1 FROM hydro.river_timeseries "
        "WHERE run_key = "
        "AND basin_version_key = "
        "AND river_network_version_key = "
        "AND variable_e = "
        "AND valid_time = :valid_time LIMIT 1"
    )

    # The probe still binds the same five text parameters and still raises the
    # same 404 — the switch is invisible to the route.
    for parameter in (
        '"run_id": run_id',
        '"variable": variable',
        '"valid_time": valid_time',
        '"basin_version_id": basin_version_id',
        '"river_network_version_id": river_network_version_id',
    ):
        assert parameter in probe, parameter
    assert 'code="MVT_SOURCE_IDENTITY_NOT_FOUND"' in probe


# ---------------------------------------------------------------------------
# display coverage
# ---------------------------------------------------------------------------


def test_coverage_river_scan_groups_by_keys_and_reconstructs_text_at_the_rollup() -> None:
    sql = display_coverage._REFRESH_SQL

    # candidate_runs already reads the three authority tables; the keys ride
    # down from there instead of costing a new join.
    assert "h.run_key," in sql
    assert "bv.basin_version_key," in sql
    assert "rnv.river_network_version_key," in sql

    assert "ON cr.run_key = rt.run_key" in sql
    assert "AND cr.basin_version_key = rt.basin_version_key" in sql
    assert "AND cr.river_network_version_key = rt.river_network_version_key" in sql
    # The enum predicate stands alone: the text aid that used to sit under a
    # marker line beneath it went with the column it pushed into (#1342
    # contract, task 6.3), and the marker line went with it.
    assert "WHERE rt.variable_e = 'q_down'::hydro.river_variable\n" in sql
    assert "rt.variable = 'q_down'" not in sql
    assert AID_MARKER_TAG not in sql

    # Segment counting moves to the key. Within a network the mapping is 1:1,
    # so the count is unchanged; the key is additionally unique table-wide.
    assert "COUNT(DISTINCT river_segment_key) AS segment_count" in sql
    assert "COUNT(DISTINCT river_segment_id)" not in sql.split("station_variable_identity_stats")[-1]
    assert "GROUP BY run_key, basin_version_key, river_network_version_key, river_segment_key" in sql
    assert "GROUP BY run_key, basin_version_key, river_network_version_key" in sql

    # join-and-reconstruct: the rollup emits the same three text columns the
    # `coverage` CTE joins on, so nothing downstream changes.
    assert "rollup_run.run_id," in sql
    assert "rollup_basin.basin_version_id," in sql
    assert "rollup_network.river_network_version_id," in sql
    assert "JOIN hydro.hydro_run rollup_run\n              ON rollup_run.run_key = rollup.run_key" in sql
    assert "hc.run_id = cr.run_id" in sql
    assert "hc.basin_version_id = cr.basin_version_id" in sql
    assert "hc.river_network_version_id = cr.river_network_version_id" in sql

    _assert_text_fact_columns(sql, "rt", set(), "coverage river scan")


def test_coverage_river_scan_guards_are_key_only_and_it_joins_on_keys_only() -> None:
    """Every scan guard is key-only now, and the join always was.

    A ``cr.run_id = rt.run_id`` join equality looks like a pushdown but is not:
    it cannot be pushed into a compressed chunk, and it is exactly the text fact
    join the delta forbids. The text conjuncts that used to sit inside the
    ``scan_*`` guards beside their keys were the transitional compressed-chunk
    pushdown aids; #1342's contract (task 6.3) deleted them with the text-
    segmented table, so all three guards now have the key-only shape
    ``basin_version_id`` always had.
    """
    outer = outer_predicates(display_coverage._river_sample_rows_template("narrow"))

    assert "timeseries_store" not in outer
    assert "WHERE rt.variable_e = 'q_down'::hydro.river_variable" in outer
    assert "rt.variable = 'q_down'" not in outer
    assert "( rt.run_key = )" in outer
    assert "( rt.river_network_version_key = )" in outer
    assert "OR rt.basin_version_key = )" in outer
    # Key-only join into the fact table.
    assert (
        "JOIN candidate_runs cr ON cr.run_key = rt.run_key "
        "AND cr.basin_version_key = rt.basin_version_key "
        "AND cr.river_network_version_key = rt.river_network_version_key WHERE"
    ) in outer

    with pytest.raises(ValueError, match="Invalid river timeseries store"):
        display_coverage._river_sample_rows_template("legacy")


def test_coverage_sql_binds_only_parameters_the_refresh_actually_supplies() -> None:
    """psycopg2 interpolates the whole statement, SQL comments included.

    A ``%(...)s`` token written into an explanatory comment is indistinguishable
    from a real placeholder and raises at execute time with a key error — and
    the unit tests around ``_refresh`` mock the cursor, so nothing else here
    would notice. Enumerating the tokens against the parameters ``_refresh``
    binds is the cheap way to keep that failure out of production.
    """
    supplied = {
        "horizon",
        "basin_id",
        "run_id",
        "variables",
        "variable_count",
        # #1446: the overwrite guard's explicit-zeroing bypass, bound by
        # `_refresh` on every call (False unless the caller forces).
        "force",
        # #2504 D6: the window-scoped relaxation's cutoff, bound on every call
        # (None unless the caller resolved a window and a watermark).
        "expired_cutoff",
        *display_coverage._SCAN_PARAM_KEYS,
    }
    for name, sql in (
        ("_REFRESH_SQL", display_coverage._REFRESH_SQL),
        ("_SCAN_HEADER_SQL", display_coverage._SCAN_HEADER_SQL),
    ):
        tokens = set(re.findall(r"%\(([^)]*)\)s", sql))
        assert tokens, name
        assert tokens <= supplied, f"{name} binds unsupplied parameters: {sorted(tokens - supplied)}"


def test_coverage_scan_parameters_keep_their_text_semantics() -> None:
    """The ``scan_*`` contract is unchanged: same names, same text values.

    ``_refresh`` prefetches the run's scalar TEXT identity and binds it; only
    the predicate those values feed changed. Renaming or re-typing them would
    silently break the header prefetch in ``_refresh``.
    """
    assert display_coverage._SCAN_PARAM_KEYS == (
        "scan_run_id",
        "scan_forcing_version_id",
        "scan_basin_version_id",
        "scan_river_network_version_id",
        "scan_source_id_lower",
        "scan_display_start",
        "scan_display_end",
    )
    header_sql = display_coverage._SCAN_HEADER_SQL
    for text_column in ("run_id", "basin_version_id", "river_network_version_id"):
        assert f"            {text_column},\n" in header_sql, text_column


# ---------------------------------------------------------------------------
# production closure static plan fixture
# ---------------------------------------------------------------------------


def test_hydro_map_plan_fixture_names_indexes_that_the_migration_chain_creates() -> None:
    """The static plan must name real indexes.

    It named ``river_timeseries_run_valid_idx`` — an index no migration ever
    created — which is how a fixture that nothing cross-checks rots. Checking
    the names against the migration chain, not against a hardcoded string, is
    what stops it happening again.
    """
    plan_lines = QUERY_TARGETS["hydro_map"]["plan_lines"]
    plan_text = "\n".join(plan_lines)
    migrations = "\n".join(path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS_DIR.glob("*.sql")))

    assert "river_timeseries_run_valid_idx" not in plan_text
    assert "river_ts_selected_identity_key_valid_time_idx" in plan_text
    # The segment join now rides core.river_segment's surrogate-key unique
    # constraint, created by 000050's `... IDENTITY UNIQUE` column.
    assert "river_segment_river_segment_key_key" in plan_text

    # Index names the migration chain really produces: the ones it names
    # explicitly, plus PostgreSQL's implicit `<table>_<column>_key` for each
    # UNIQUE column the chain adds (the constraint index carries no name of
    # its own in the SQL, so it has to be derived rather than grepped).
    creatable = set(re.findall(r"CREATE (?:UNIQUE )?INDEX(?: CONCURRENTLY)?(?: IF NOT EXISTS)? (\w+)", migrations))
    creatable |= {
        f"{table}_{column}_key"
        for _schema, table, column in re.findall(
            r"ALTER TABLE\s+([a-z_]+)\.([a-z_]+)\s+ADD COLUMN IF NOT EXISTS\s+(\w+)"
            r"\s+INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE",
            migrations,
        )
    }
    assert "river_ts_selected_identity_key_valid_time_idx" in creatable
    assert "river_segment_river_segment_key_key" in creatable

    for index_name in re.findall(r"Index Scan using (\w+) on", plan_text):
        assert index_name in creatable, f"{index_name} is named by the plan fixture but no migration creates it"


# ---------------------------------------------------------------------------
# Transitional-aid census and registry closure for the display three (#1980)
#
# Ownership is exclusive by design (fixture decision 7): this file owns mvt,
# hydro_display and display_coverage; tests/test_river_ts_text_identity_cleanup.py
# owns forecast_store, publisher, forcing_copyback_backfill and parser. Exactly
# one test reddens per file, and neither register can quietly grow into the
# other's files.
# ---------------------------------------------------------------------------

# Counted on the SOURCE. The count is now ZERO in all three files: #1342's
# contract (task 6.3) deleted every transitional compressed-chunk pushdown aid
# and its marker line. The register is KEPT at zero rather than deleted with the
# aids, because a zero that is asserted per file is what makes a reintroduced
# aid red in the file that grew it — a deleted register makes it invisible.
#
# What the numbers were, and what the contract removed: mvt.py 14 (the hydro
# layer's three + national identity's three and the shared data source's four +
# the valid_times branches' three and one), hydro_display.py 3 and
# display_coverage.py 3 (run_id, river_network_version_id and variable on the
# existence probe and on the coverage river scan).
DISPLAY_MARKER_AID_CENSUS: dict[str, int] = {
    # #2026: the identity probe moved to the identity owner module with the facade
    # split, so the census must count the file that actually holds the statement.
    "apps/api/routes/hydro_display_identity.py": 0,
    "packages/common/display_coverage.py": 0,
    "services/tiles/mvt.py": 0,
}


def test_the_display_readers_declare_their_marker_and_aid_count() -> None:
    """Per file: no marker line, and no aid conjunct under one."""
    for path, expected in DISPLAY_MARKER_AID_CENSUS.items():
        assert_marker_census(path, expected)


def test_the_display_readers_carry_no_transitional_aid_at_all() -> None:
    """0 of the once-registered 20; the other ten are the cleanup oracle's.

    Registered, not tree-wide: the 30 spanned this census plus the cleanup
    oracle's ``REGISTERED_SOURCES`` and nothing sweeps for an unregistered
    reader — that is the I11 discovery-set census (tasks 7.2a). Both registers
    are now zero, which is the contract's whole verification criterion 1
    restated per file.
    """
    assert sum(DISPLAY_MARKER_AID_CENSUS.values()) == 0


def test_every_display_read_site_is_registered_in_the_template_registry() -> None:
    """Registry closure for the display three (#1980 orchestrator requirement).

    These files are deliberately absent from the cleanup oracle's statement
    census, so closure is asserted here against the same counter that oracle uses
    (``_river_table_mentions``: mentions inside non-docstring string constants,
    not raw source). Both mvt valid_times branches count, and the national
    sources contribute two authored mentions, with the data source reused by
    both zoom legs. The mvt total stays exactly five.
    """
    for path, entries in _registry_entries_by_path().items():
        source = REPO_ROOT.joinpath(*path.split("/")).read_text(encoding="utf-8")
        registered = sum(entry.mentions for entry in entries)
        if path == "services/tiles/mvt.py":
            assert registered == 5
        assert registered + NON_TEMPLATE_MENTIONS[path] == _river_table_mentions(source), (
            f"{path}: {registered} mentions in registered templates + "
            f"{NON_TEMPLATE_MENTIONS[path]} declared non-template mentions != "
            f"{_river_table_mentions(source)} in the source"
        )


def _registry_entries_by_path() -> dict[str, list]:
    grouped: dict[str, list] = {}
    for entry in REGISTRY:
        if entry.path in DISPLAY_MARKER_AID_CENSUS:
            grouped.setdefault(entry.path, []).append(entry)
    assert set(grouped) == set(DISPLAY_MARKER_AID_CENSUS), "a display reader has no registered template"
    return grouped


@pytest.mark.parametrize(
    "entry",
    [entry for entry in REGISTRY if entry.path in DISPLAY_MARKER_AID_CENSUS],
    ids=lambda entry: entry.key,
)
def test_every_display_template_renders_free_of_text_identity_for_the_narrow_store(entry) -> None:
    """The display half of "render both variants for every registered template".

    ``tests/test_sql_shape_helpers.py`` runs this over the whole register; it is
    restated here on the three files this oracle owns because those are the ones
    whose 404 / tile / coverage behaviour depends on the fact-table predicates,
    and this file is where a reviewer of an mvt change looks.
    """
    narrow = render_river_ts_sql(entry.source("narrow"), "narrow", entry=entry.key)

    # Table-scoped, deliberately not a per-column substring sweep: these
    # statements also read `identity_stats.basin_version_id` and project
    # `ts.unit_e`, so an `f"ts.{column}" not in sql` loop is red on switched code
    # and would have to be weakened until it said nothing.
    assert fact_table_text_identity_columns(narrow.sql) == set()
    # Non-vacuity: what is LEFT is the key/enum authority resolution, so the
    # statement still selects the same rows through the same predicates. The
    # line-count comparison against the legacy variant that used to stand beside
    # this went with the legacy variant itself (#1342 contract, task 6.3); the
    # renderer now returns its input, so identity is the arithmetic.
    assert "_key = " in narrow.sql or "variable_e = " in narrow.sql
    assert narrow.sql == entry.source("narrow")
    with pytest.raises(Exception, match="unknown timeseries store"):
        render_river_ts_sql(entry.source("narrow"), "legacy", entry=entry.key)
