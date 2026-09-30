"""#1446 coverage-refresh guard and live-caller plans of the #1341 surrogate-key switch.

Partition of ``tests/test_river_ts_read_path_surrogate_keys_integration.py``
(#2490, pure move); the opt-in, the throwaway-database contract and the scope
statement are the base file's.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from sqlalchemy import text
from sqlalchemy.orm import Session

from apps.api.routes import hydro_display
from packages.common.display_coverage import (
    DisplayCoverageRefreshRefused,
    refresh_run_display_coverage,
)
from packages.common.river_ts_render import render_river_ts_sql
from services.tiles.mvt import (
    postgis_tile_sql,
    valid_times_for_layer,
)
from tests.integration_helpers import (
    post_expand_forecast_database as post_expand_forecast_database,
)
from tests.river_ts_read_path_surrogate_keys_integration_helpers import (
    _ALL_LEGACY_RUN_ID,
    _BASIN_ID,
    _BASIN_VERSION_ID,
    _KEYED_RUN_ID,
    _LEGACY_RUN_ID,
    _NARROW_STORE_MAP,
    _NATIONAL_TILE_PARAMS,
    _NETWORK_ID,
    _SEGMENTS,
    _VARIABLE,
    _assert_all_legacy_preconditions,
    _coverage,
    _expand,
    _identity_params,
    _rows,
)
from tests.river_ts_read_path_surrogate_keys_integration_helpers import (
    seeded as seeded,
)

pytestmark = pytest.mark.integration


_ZEROED_COVERAGE = {
    "segment_count": 0,
    "river_sample_count": 0,
    "river_valid_time_start": None,
    "river_valid_time_end": None,
    "min_lead_time_hours": None,
    "max_lead_time_hours": None,
}


def test_refreshing_coverage_for_an_all_null_key_run_is_refused_not_zeroed(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """#1446: the guard holds on a real database, against the real hazard shape.

    Re-scanning a run whose rows are all pre-#1340 does not merely fail to find
    them: ``coverage`` is built ``FROM candidate_runs`` with a LEFT JOIN to the
    river rollup, so the run still produces an upsert row — with
    ``COALESCE(..., 0)`` counts and NULL valid-time bounds. Before #1446 that
    row overwrote the correct values the text era had materialized, dropping
    the run out of latest-product readiness and off the national tile.

    The conditional ``DO UPDATE ... WHERE`` now skips it. This is the only test
    that executes that clause: the unit suite's cursor is a fake and cannot
    evaluate SQL.
    """
    url, _session = seeded
    _session.rollback()
    post_expand_forecast_database(_NARROW_STORE_MAP)
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        before = _assert_all_legacy_preconditions(connection)

        with pytest.raises(DisplayCoverageRefreshRefused) as excinfo:
            refresh_run_display_coverage(connection, _ALL_LEGACY_RUN_ID)

        assert excinfo.value.run_id == _ALL_LEGACY_RUN_ID
        assert excinfo.value.existing_segment_count == len(_SEGMENTS)

        # Whole-row skip (design D1): nothing moved, `refreshed_at` included —
        # which is what keeps the run stale and rescanned until #1408 heals it.
        assert _coverage(connection, _ALL_LEGACY_RUN_ID) == before
    finally:
        connection.close()


def test_forced_refresh_of_an_all_null_key_run_performs_the_zeroing(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """The escape hatch still works — and is the ONLY way to zero the row."""
    url, _session = seeded
    _session.rollback()
    post_expand_forecast_database(_NARROW_STORE_MAP)
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        before = _assert_all_legacy_preconditions(connection)

        assert refresh_run_display_coverage(connection, _ALL_LEGACY_RUN_ID, force=True) is True

        after = _coverage(connection, _ALL_LEGACY_RUN_ID)
        assert {key: after[key] for key in _ZEROED_COVERAGE} == _ZEROED_COVERAGE
        assert after["refreshed_at"] > before["refreshed_at"]
    finally:
        connection.close()


def test_an_existing_zero_row_is_still_rewritten_by_an_empty_scan(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """The guard's third disjunct: a row that already reads 0 is not protected.

    Chained after the force, because that is exactly the state that matters —
    once a run has been zeroed deliberately, ordinary refreshes must keep
    bumping its ``refreshed_at`` so it stops being reported stale forever.
    """
    url, _session = seeded
    _session.rollback()
    post_expand_forecast_database(_NARROW_STORE_MAP)
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        assert refresh_run_display_coverage(connection, _ALL_LEGACY_RUN_ID, force=True) is True
        zeroed = _coverage(connection, _ALL_LEGACY_RUN_ID)
        assert zeroed["segment_count"] == 0

        # No force this time: the row reads 0, so the guard lets it through.
        assert refresh_run_display_coverage(connection, _ALL_LEGACY_RUN_ID) is True

        after = _coverage(connection, _ALL_LEGACY_RUN_ID)
        assert {key: after[key] for key in _ZEROED_COVERAGE} == _ZEROED_COVERAGE
        assert after["refreshed_at"] > zeroed["refreshed_at"]
    finally:
        connection.close()


def test_first_refresh_with_no_existing_row_writes_zero(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """A first refresh is an INSERT and never reaches the DO UPDATE guard.

    The guard must not make an empty run unmaterializable: without a stored
    row there is nothing to protect, so the zero row is written exactly as
    before #1446.
    """
    url, _session = seeded
    _session.rollback()
    post_expand_forecast_database(_NARROW_STORE_MAP)
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM hydro.run_display_coverage WHERE run_id = %s",
                (_ALL_LEGACY_RUN_ID,),
            )
        connection.commit()
        assert _coverage(connection, _ALL_LEGACY_RUN_ID) is None

        assert refresh_run_display_coverage(connection, _ALL_LEGACY_RUN_ID) is True

        after = _coverage(connection, _ALL_LEGACY_RUN_ID)
        assert {key: after[key] for key in _ZEROED_COVERAGE} == _ZEROED_COVERAGE
    finally:
        connection.close()


def test_keyed_run_refresh_is_never_refused(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """Non-vacuity for the guard: a run whose scan finds segments is unaffected.

    Refreshed twice in a row — the second call is the one that would trip a
    guard written against "has an existing row" rather than against "the fresh
    scan is empty".
    """
    url, _session = seeded
    _session.rollback()
    post_expand_forecast_database(_NARROW_STORE_MAP)
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        assert refresh_run_display_coverage(connection, _KEYED_RUN_ID) is True
        first = _coverage(connection, _KEYED_RUN_ID)
        assert first["segment_count"] == len(_SEGMENTS)

        assert refresh_run_display_coverage(connection, _KEYED_RUN_ID) is True
        second = _coverage(connection, _KEYED_RUN_ID)
        assert second["segment_count"] == len(_SEGMENTS)
        assert second["refreshed_at"] > first["refreshed_at"]
    finally:
        connection.close()


def _capture_valid_times_statement(session: Session, *, named: bool) -> tuple[str, dict[str, Any]]:
    """Observe the real caller at its database boundary, still executing it."""
    statements: list[tuple[str, dict[str, Any]]] = []

    class RecordingSession:
        def execute(self, statement: Any, parameters: dict[str, Any]) -> Any:
            statements.append((str(statement), parameters))
            return session.execute(statement, parameters)

    identity = (
        dict(run_id=_KEYED_RUN_ID, basin_version_id=_BASIN_VERSION_ID,
             river_network_version_id=_NETWORK_ID) if named else {}
    )
    valid_times_for_layer(RecordingSession(), "discharge", **identity)
    assert len(statements) == 1
    return statements[0]


def _bound_oracle_sql(cursor: Any, sql: str, parameters: Any) -> str:
    """Use the production driver/compiler bind dialect, not a SQL rewriter."""
    from sqlalchemy.dialects import postgresql

    if isinstance(parameters, tuple) or "%(" in sql:
        return cursor.mogrify(sql, parameters).decode()
    compiled = text(sql).compile(dialect=postgresql.dialect())
    return cursor.mogrify(str(compiled), {
        name: parameters[name] for name in compiled.params
    }).decode()


def _coverage_oracle_statement() -> tuple[str, dict[str, Any]]:
    from packages.common import display_coverage

    parameters = {
        "horizon": display_coverage.QHH_LATEST_EXPECTED_HORIZON_HOURS,
        "basin_id": None, "run_id": None,
        "variables": list(display_coverage.MVP_STATION_VARIABLES),
        "variable_count": len(display_coverage.MVP_STATION_VARIABLES),
        "force": False,
        # #2504: unbound cutoff = the pre-#2504 guard.
        "expired_cutoff": None,
        **dict.fromkeys(display_coverage._SCAN_PARAM_KEYS),
    }
    return display_coverage._REFRESH_SQL, parameters


def test_complete_registry_prepares_against_expanded_catalog(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """PREPARE every live registry source in its actual caller's SQL context."""
    import re

    from tests.river_ts_template_registry import REGISTRY
    from tests.test_river_ts_text_identity_cleanup import (
        _latest_product_fallback_execution,
        _segment_block_executions,
    )

    url, session = seeded
    _expand(session, post_expand_forecast_database)
    national_params = hydro_display._postgis_tile_params(
        _identity_params(_KEYED_RUN_ID) | _NATIONAL_TILE_PARAMS,
        z=9, x=398, y=197, layer="hydro-national",
    )
    contexts = {
        "display_coverage:refresh": _coverage_oracle_statement(),
        "forecast_store:latest_product_river_source": _latest_product_fallback_execution(),
        # #2424 D1: the correlated probe only exists inside the discovery statement.
        "forecast_store:latest_cycle_fact_probe": _segment_block_executions()["per_source_latest_cycles"],
        "mvt:hydro_national_identity_source": (postgis_tile_sql("hydro-national"), national_params),
        "mvt:hydro_national_data_source": (postgis_tile_sql("hydro-national"), national_params),
    }
    # All remaining entries are complete standalone SELECTs; none gets a
    # synthetic candidate_runs/lr/seg alias or a hand-written replacement query.
    standalone = {
        "hydro_display:mvt_source_identity_probe",
        "forecast_store:segment_rows_source",
        "forcing_copyback_backfill:discover_backfill_runs",
        "publisher:qdown_discovery",
        "mvt:postgis_tile_sql_hydro",
        "mvt:valid_times_named_identity",
        "mvt:valid_times_any_identity",
        "parser:replace_chain_probe",
        "parser:replace_chain_window",
    }
    assert {entry.key for entry in REGISTRY} == set(contexts) | standalone
    params = _identity_params(_KEYED_RUN_ID) | {
        "river_segment_id": _SEGMENTS[0][0], "source_id": "gfs",
        "limit": 100, "basin_id": _BASIN_ID,
    }
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            for index, entry in enumerate(REGISTRY):
                rendered = render_river_ts_sql(entry.source("narrow"), "narrow", entry=entry.key).sql
                if entry.key in contexts:
                    sql, parameters = contexts[entry.key]
                    assert " ".join(rendered.split()) in " ".join(sql.split()), entry.key
                else:
                    sql = rendered
                    if entry.params == "positional":
                        cursor.execute(
                            "SELECT h.run_key, rnv.river_network_version_key "
                            "FROM hydro.hydro_run h JOIN core.model_instance mi USING (model_id) "
                            "JOIN core.river_network_version rnv USING (river_network_version_id) "
                            "WHERE h.run_id=%s", (_KEYED_RUN_ID,),
                        )
                        run_key, network_key = cursor.fetchone()
                        parameters = (run_key, network_key, _VARIABLE)
                    else:
                        names = set(re.findall(r"%\((\w+)\)s|(?<!:):(\w+)", sql))
                        parameters = {left or right: None for left, right in names} | params
                prepared = f"i7_registry_{index}"
                bound = _bound_oracle_sql(cursor, sql, parameters)
                cursor.execute(f"PREPARE {prepared} AS {bound}")
                cursor.execute("SELECT statement FROM pg_prepared_statements WHERE name=%s", (prepared,))
                assert cursor.fetchone()[0] == f"PREPARE {prepared} AS {bound}", entry.key
                cursor.execute(f"DEALLOCATE {prepared}")


def test_live_caller_plans_scan_the_narrow_store_and_never_the_legacy_one(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """EXPLAIN actual national, refresh, named and any-identity statements.

    The planner is the only oracle that can tell "the legacy table is no longer
    named in the SQL" (a text pin, already held by the unit suite) from "the
    legacy table is no longer read" -- a view, a rule or a stale prepared plan
    could still route there. #1342's contract (task 6.3) makes that the whole
    point, so this replaces the mixed-store variant that used to require BOTH
    physical stores in every plan.
    """
    url, session = seeded
    _expand(session, post_expand_forecast_database)
    national_params = hydro_display._postgis_tile_params(
        _identity_params(_KEYED_RUN_ID) | _NATIONAL_TILE_PARAMS,
        z=9, x=398, y=197, layer="hydro-national",
    )
    statements = {
        "national": (postgis_tile_sql("hydro-national"), national_params),
        "coverage": _coverage_oracle_statement(),
        "named": _capture_valid_times_statement(session, named=True),
        "any": _capture_valid_times_statement(session, named=False),
    }

    def relations(node: dict[str, Any]) -> set[str]:
        found = {node["Relation Name"]} if "Relation Name" in node else set()
        for child in node.get("Plans", []):
            found.update(relations(child))
        return found

    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            physical = {}
            for table in ("river_timeseries_legacy", "river_timeseries"):
                cursor.execute(
                    "SELECT chunk_name FROM timescaledb_information.chunks "
                    "WHERE hypertable_schema='hydro' AND hypertable_name=%s", (table,),
                )
                chunks = {row[0] for row in cursor.fetchall()}
                # Non-vacuity: both hypertables still hold chunks a plan could
                # reach for, so the exclusion below is a routing fact and not
                # an artefact of an empty legacy table.
                assert chunks, table
                physical[table] = chunks | {table}
            for label, (sql, parameters) in statements.items():
                cursor.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + _bound_oracle_sql(cursor, sql, parameters))
                plan = cursor.fetchone()[0][0]["Plan"]
                scanned = relations(plan)
                assert scanned & physical["river_timeseries"], (label, plan)
                assert not scanned & physical["river_timeseries_legacy"], (label, plan)


def test_actual_publisher_and_copyback_discover_the_narrow_facts(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None], tmp_path: Path,
) -> None:
    from services.tile_publisher.forcing_copyback_backfill import discover_backfill_runs
    from services.tile_publisher.publisher import TilePublisher
    from workers.data_adapters.base import cycle_id_for

    url, session = seeded
    _expand(session, post_expand_forecast_database)
    session.execute(text("""
        INSERT INTO met.data_source (source_id, source_name, source_type, status, adapter_name)
        VALUES ('gfs', 'GFS integration source', 'forecast', 'enabled', 'gfs')
        ON CONFLICT (source_id) DO NOTHING
    """))
    session.execute(text("UPDATE hydro.hydro_run SET source_id='gfs'"))
    session.commit()
    expected = {_KEYED_RUN_ID, _LEGACY_RUN_ID}
    publisher = TilePublisher(workspace_root=tmp_path, object_store_root=tmp_path / "objects")
    cycles = [
        cycle_id_for(row["source_id"], row["cycle_time"])
        for row in _rows(session, "SELECT DISTINCT source_id, cycle_time FROM hydro.hydro_run", {})
    ]

    def discover_publisher() -> list[dict[str, Any]]:
        return [row for cycle in cycles for row in publisher._discover_qdown_runs(session, cycle)]

    publisher_rows = discover_publisher()
    assert {row["run_id"] for row in publisher_rows} == expected
    assert {row["run_id"] for row in discover_backfill_runs(session)} == expected
    session.rollback()
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            # Non-vacuity: both discoveries answer from the narrow fact table
            # and nothing else, so emptying it must empty them.
            cursor.execute("DELETE FROM hydro.river_timeseries")
    assert discover_publisher() == []
    assert discover_backfill_runs(session) == []
