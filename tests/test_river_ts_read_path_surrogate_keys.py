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

The hybrid pushdown rule these pins encode (user-adjudicated, #1341): every
in-boundary fact read keeps a redundant TEXT predicate on exactly ``run_id`` /
``river_network_version_id`` / ``variable``, each AND-ed with its key or enum
counterpart, for as long as compression segments compressed chunks by the text
columns. So the pins are two-sided:

* positive — each sanctioned text predicate sits in the SAME conjunction as
  its counterpart (asserted as one adjacency substring, not two independent
  ``in`` checks, which would pass with the pair split across the query);
* negative — the referenced text column set EQUALS the sanctioned subset that
  surface may carry, so both a forbidden column appearing and a pushdown aid
  quietly vanishing are red.

Surfaces whose identity arrives by join rather than as a bound constant (the
two national legs and the national identity probe) can carry only ``variable``:
a join equality is not pushdown material, and a text fact join is forbidden
outright.
"""

from __future__ import annotations

import re
from pathlib import Path

from packages.common import display_coverage
from services.production_closure.scale_validation import QUERY_TARGETS
from services.tiles.mvt import _mvt_tile_order_by, postgis_tile_sql
from tests.test_sql_shape_helpers import (
    FORBIDDEN_TEXT_FACT_COLUMNS,
    SANCTIONED_TEXT_PUSHDOWN_COLUMNS,
    outer_predicates,
    sql_from_python,
    sql_literals,
    text_fact_columns,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MVT_SOURCE = (REPO_ROOT / "services" / "tiles" / "mvt.py").read_text(encoding="utf-8")
HYDRO_DISPLAY_SOURCE = (REPO_ROOT / "apps" / "api" / "routes" / "hydro_display.py").read_text(
    encoding="utf-8"
)
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

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

# The sanctioned pushdown pairs, spelled the way each surface binds them.
# Written as ONE substring per pair so the assertion is about adjacency in a
# single conjunction, not about two literals existing somewhere in the query.
BOUND_PARAM_PUSHDOWN_PAIRS = (
    ("run_id", "ts.run_id = :run_id AND ts.run_key ="),
    (
        "river_network_version_id",
        "ts.river_network_version_id = :river_network_version_id AND ts.river_network_version_key =",
    ),
    ("variable", "ts.variable = :variable AND ts.variable_e ="),
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


def _assert_text_fact_columns(sql: str, alias: str, expected: set[str], label: str) -> None:
    """The surface's text fact-column references are EXACTLY ``expected``.

    Equality rather than "none of the forbidden ones": it is red both when a
    forbidden column (``basin_version_id`` / ``river_segment_id`` / ``unit`` /
    ``quality_flag``) reappears and when a sanctioned pushdown aid is silently
    dropped, which would reintroduce the compressed-chunk collapse this change
    was amended to avoid.
    """
    assert expected <= set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS), f"{label}: expectation exceeds the sanctioned set"
    assert text_fact_columns(sql, alias) == expected, label


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

    _assert_text_fact_columns(source_cte, "ts", set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS), "hydro tile")


def test_hydro_tile_carries_all_three_pushdown_aids_paired_with_their_keys() -> None:
    """The tile point lookup binds every sanctioned column as a constant.

    It is the one in-boundary surface that can carry all three, so it is where
    the pairing rule is pinned hardest: each text conjunct must be immediately
    followed by its key/enum counterpart in the same ``AND`` chain. A pair
    split apart still reads the same rows but stops being self-evidently a
    no-op, which is the whole argument for allowing the text predicate at all.
    """
    outer = outer_predicates(_source_cte("hydro"))

    for column, pair in BOUND_PARAM_PUSHDOWN_PAIRS:
        assert pair in outer, column
    # Non-vacuity: the key predicates are the row-selection authority and the
    # basin key — the one identity with no sanctioned text partner — is there
    # on its own.
    assert "AND ts.basin_version_key = AND" in outer
    assert "AND ts.valid_time = :valid_time" in outer


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
        # Tail anchor is the NEXT function, not `_valid_time_discovery` (which
        # is defined further down): the wider slice over-spans into
        # `national_discharge_valid_times` and `_coverage_datetime`, so a cast
        # smuggled into either would be attributed to `valid_times_for_layer`
        # here. Same anchor as `_valid_times_branch_sql` below and as
        # tests/test_migrations.py.
        "valid_times": _slice(MVT_SOURCE, "def valid_times_for_layer", "def national_discharge_valid_times"),
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


def _national_legs() -> tuple[str, str]:
    national_cte = _source_cte("hydro-national")
    typed = _slice(national_cte, "typed_values AS (", "untyped_ranked AS (")
    untyped = _slice(national_cte, "untyped_ranked AS (", "selected_values AS (")
    return typed, untyped


def test_national_tile_switches_both_union_all_legs_to_keys() -> None:
    typed, untyped = _national_legs()

    for name, leg in (("typed_values", typed), ("untyped_ranked", untyped)):
        assert "ON ts.run_key = lr.run_key" in leg, name
        assert "AND ts.river_network_version_key = lr.river_network_version_key" in leg, name
        assert "ON seg.river_segment_key = ts.river_segment_key" in leg, name
        assert "ts.variable_e = (" in leg, name
        assert ENUM_VARIABLE_RESOLUTION in leg, name
        assert "ts.valid_time = :valid_time" in leg, name
        # Identity arrives from latest_runs, so `variable` is the only
        # sanctioned aid this surface can bind as a constant; run/network must
        # join on keys only.
        _assert_text_fact_columns(leg, "ts", {"variable"}, name)
        assert "ts.variable = :variable AND ts.variable_e =" in outer_predicates(leg), name

    # latest_runs hands both legs the keys AND the text it will echo back out,
    # from the authority join it was already doing.
    national_cte = _source_cte("hydro-national")
    latest_runs = _slice(national_cte, "WITH latest_runs AS MATERIALIZED (", "network_stream_max AS")
    assert "h.run_id, mi.river_network_version_id," in latest_runs
    assert "h.run_key, rnv.river_network_version_key" in latest_runs
    assert "JOIN core.river_network_version rnv" in latest_runs


def test_national_null_key_visibility_cannot_split_between_the_two_zoom_branches() -> None:
    """z<9 and z>=9 must see exactly the same fact rows for one national identity.

    ``typed_values`` (z>=9 branch) and ``untyped_ranked`` (z<9 branch) are the
    same read under two zoom guards. If one leg kept the text predicates, a
    legacy NULL-key row would render at one zoom and vanish at the other for
    the same tile — the split this change exists to prevent. Comparing the two
    legs' complete set of fact-table column references, rather than a list of
    literals, is what makes a one-sided edit red.
    """
    typed, untyped = _national_legs()

    typed_columns = set(re.findall(r"\bts\.[a-z_]+", typed))
    untyped_columns = set(re.findall(r"\bts\.[a-z_]+", untyped))
    assert typed_columns == untyped_columns, (typed_columns ^ untyped_columns)
    assert "ts.run_key" in typed_columns
    assert not any(column.endswith(("_id",)) for column in typed_columns), typed_columns
    # The one sanctioned pushdown aid is on BOTH legs or neither: carrying it
    # on one side only is the same visibility split by another route, since a
    # text `variable` predicate can narrow away rows the other leg keeps.
    assert "ts.variable" in typed_columns

    # Non-vacuity: the legs really are the two zoom branches, not a copy-paste
    # of one clause into a variable.
    assert ":z >= 9" in typed
    assert ":z < 9" in untyped


def test_national_identity_probe_uses_the_same_key_shape_as_the_data_legs() -> None:
    probe = _identity_stats_cte("hydro-national")

    assert "lr.run_key = ts.run_key" in probe
    assert "lr.river_network_version_key = ts.river_network_version_key" in probe
    assert "ts.variable_e = (" in probe
    assert "h.run_key, rnv.river_network_version_key" in probe
    assert "JOIN core.river_network_version rnv" in probe
    _assert_text_fact_columns(probe, "ts", {"variable"}, "national identity probe")
    assert "ts.variable = :variable AND ts.variable_e =" in outer_predicates(probe)


def test_national_output_and_ordering_stay_on_restored_text_expressions() -> None:
    """Integer key order is not text order — the response array must not reorder."""
    assert _mvt_tile_order_by("hydro-national") == "river_network_version_id, river_segment_id"
    assert _mvt_tile_order_by("hydro") == "river_network_version_id, river_segment_id"

    national_cte = _source_cte("hydro-national")
    # The ordered columns are produced from the authority tables' text, not
    # from the keys the fact predicates use.
    assert "lr.river_network_version_id," in national_cte
    assert "SELECT seg.river_segment_id," in national_cte
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


def _valid_times_branch_sql() -> tuple[str, str]:
    """The named-identity and no-identity SQL of ``valid_times_for_layer``.

    Extracted through Python's parser, not by slicing text: the function's
    prose comments contain apostrophes, and a raw SQL tokenizer run over them
    silently swallows the queries (see the oracle module's self-tests).
    """
    valid_times = _slice(MVT_SOURCE, "def valid_times_for_layer", "def national_discharge_valid_times")
    literals = sql_literals(valid_times)
    assert len(literals) == 2, literals
    named, no_named = literals
    return named, no_named


def test_valid_times_named_identity_branch_pairs_every_pushdown_aid_with_its_key() -> None:
    """The #1378 shape: strict key prefix of 000051, plus the transitional aids.

    This is the exact query node-27 measured flipping to a 598,280-cost full
    decompression when it ran on keys alone, so the pairing is pinned here on
    the unqualified spelling the single-table query uses.
    """
    named, _no_named = _valid_times_branch_sql()
    outer = outer_predicates(named)

    assert "WHERE run_id = :run_id AND run_key =" in outer
    assert "AND river_network_version_id = :river_network_version_id AND river_network_version_key =" in outer
    assert "AND variable = :variable AND variable_e =" in outer
    # basin_version_id has no sanctioned text partner: key only.
    assert "AND basin_version_key = AND" in outer
    assert "ORDER BY valid_time DESC" in outer
    for forbidden in FORBIDDEN_TEXT_FACT_COLUMNS:
        assert re.search(rf"\b{forbidden}\b", outer) is None, forbidden


def test_valid_times_no_named_identity_branch_also_filters_the_enum_column() -> None:
    """The unnamed branch has no production caller, but it is inside the boundary.

    Leaving it on ``variable = :variable`` alone would violate the delta's
    "keys are the row-selection authority" wording and would silently start
    returning NULL-key rows that the named branch excludes.
    """
    _named, no_named = _valid_times_branch_sql()
    outer = outer_predicates(no_named)

    assert "AND variable_e = (" in no_named
    assert ENUM_VARIABLE_RESOLUTION in no_named
    # Whole-query equality: this branch is short enough to pin exactly, which
    # is the strongest form of "no predicate crept in".
    assert outer == (
        "SELECT DISTINCT valid_time FROM hydro.river_timeseries "
        "WHERE variable = :variable AND variable_e = "
        "ORDER BY valid_time DESC LIMIT :limit"
    )
    # No identity predicate at all on this branch, text or key — it is the
    # "any run" discovery shape and must not grow one by accident.
    for column in ("run_id", "run_key", *FORBIDDEN_TEXT_FACT_COLUMNS):
        assert re.search(rf"\b{column}\b", outer) is None, column


def test_existence_probe_switches_to_keys_without_touching_its_404_contract() -> None:
    probe = _slice(
        HYDRO_DISPLAY_SOURCE,
        "def _require_hydro_mvt_source_identity",
        "def _require_run_source_identity",
    )

    assert "FROM hydro.river_timeseries" in probe
    assert "AND run_key = (" in probe
    assert RUN_KEY_RESOLUTION in probe
    assert "AND basin_version_key = (" in probe
    assert BASIN_KEY_RESOLUTION in probe
    assert "AND river_network_version_key = (" in probe
    assert NETWORK_KEY_RESOLUTION in probe
    assert "AND variable_e = (" in probe
    assert "AND valid_time = :valid_time" in probe

    outer = outer_predicates(sql_from_python(probe))
    assert outer == (
        "SELECT 1 FROM hydro.river_timeseries "
        "WHERE run_id = :run_id AND run_key = "
        "AND basin_version_key = "
        "AND river_network_version_id = :river_network_version_id AND river_network_version_key = "
        "AND variable = :variable AND variable_e = "
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
    assert "WHERE rt.variable = 'q_down'\n              AND rt.variable_e = 'q_down'::hydro.river_variable" in sql

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

    _assert_text_fact_columns(sql, "rt", set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS), "coverage river scan")


def test_coverage_river_scan_pairs_its_pushdown_aids_and_joins_on_keys_only() -> None:
    """The coverage scan's aids are constants; its join to candidate_runs is not.

    A ``cr.run_id = rt.run_id`` join equality looks like the same thing but is
    not: it cannot be pushed into a compressed chunk, and it is exactly the
    text fact join the delta forbids. The pushdown value here comes from the
    ``scan_*`` constants, so the aids live inside those guards.
    """
    outer = outer_predicates(display_coverage._REFRESH_SQL)

    assert "WHERE rt.variable = 'q_down' AND rt.variable_e = 'q_down'::hydro.river_variable" in outer
    assert "(rt.run_id = %(scan_run_id)s AND rt.run_key = )" in outer
    assert (
        "(rt.river_network_version_id = %(scan_river_network_version_id)s AND rt.river_network_version_key = )"
    ) in outer
    # basin_version_id is not sanctioned, so its guard is key-only.
    assert "OR rt.basin_version_key = )" in outer
    # Key-only join into the fact table.
    assert (
        "JOIN candidate_runs cr ON cr.run_key = rt.run_key "
        "AND cr.basin_version_key = rt.basin_version_key "
        "AND cr.river_network_version_key = rt.river_network_version_key WHERE"
    ) in outer


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
