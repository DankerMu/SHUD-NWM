"""Real-PostgreSQL shape test for the autovacuum output probe (#1769).

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_node27_maintenance_output_integration.py

Runs in a per-test throwaway database. Cumulative statistics are published
asynchronously, so the setup forces a flush and polls with a bound; autovacuum
itself may process the over-threshold table between generation and probe, which
can only remove that row, so its assertion regenerates modifications once.
"""

from __future__ import annotations

import time
from typing import Any

import psycopg2
import pytest

from packages.common.node27_cold_governance_collection import collect_postgres
from scripts import node27_resource_governance as governance

pytestmark = pytest.mark.integration

SCHEMA = "it1769"
_FLOAT_OR_NONE = (float, type(None))
_ROW_TYPES: dict[str, tuple[type, ...]] = {
    "schema": (str,),
    "relation": (str,),
    "relpages": (int,),
    "reltuples": (float,),
    "n_live_tup": (int,),
    "n_dead_tup": (int,),
    "n_mod_since_analyze": (int,),
    "vacuum_threshold": (float,),
    "analyze_threshold": (float,),
    "last_autovacuum_age_seconds": _FLOAT_OR_NONE,
    "last_vacuum_age_seconds": _FLOAT_OR_NONE,
    "last_autoanalyze_age_seconds": _FLOAT_OR_NONE,
    "last_analyze_age_seconds": _FLOAT_OR_NONE,
}


def _flush_and_wait(cursor: Any, database_url: str, *, relation: str, live: int) -> None:
    try:
        cursor.execute("SELECT pg_stat_force_next_flush()")
    except psycopg2.Error:
        pass  # pre-PG15 or not permitted: fall back to the bounded poll below
    cursor.execute("SELECT 1")
    deadline = time.monotonic() + 30
    while True:
        probe = psycopg2.connect(database_url)
        try:
            with probe.cursor() as probe_cursor:
                probe_cursor.execute(
                    "SELECT n_live_tup FROM pg_stat_all_tables WHERE schemaname = %s AND relname = %s",
                    (SCHEMA, relation),
                )
                row = probe_cursor.fetchone()
        finally:
            probe.close()
        if row is not None and row[0] >= live:
            return
        if time.monotonic() > deadline:
            pytest.fail(f"statistics for {SCHEMA}.{relation} were not published within 30 s")
        time.sleep(0.2)


def _rows_by_relation(database_url: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    result = collect_postgres(database_url)
    assert result["status"] == "ok"
    section = result["maintenance_output"]
    assert section["status"] == "ok", section
    return section, {row["relation"]: row for row in section["rows"] if row["schema"] == SCHEMA}


def test_maintenance_output_probe_shape_thresholds_and_exclusions(throwaway_database_url: str) -> None:
    connection = psycopg2.connect(throwaway_database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE SCHEMA {SCHEMA}")
            # Exclusion target: autovacuum is told to skip it (compressed-chunk shape); dead tuples far over.
            cursor.execute(f"CREATE TABLE {SCHEMA}.excluded (id integer) WITH (autovacuum_enabled = false)")
            cursor.execute(f"INSERT INTO {SCHEMA}.excluded SELECT generate_series(1, 2000)")
            cursor.execute(f"DELETE FROM {SCHEMA}.excluded")
            # Reloptions override: analyze threshold 500 + 0.1 * reltuples instead of the cluster 50 + 0.1 * reltuples.
            cursor.execute(
                f"CREATE TABLE {SCHEMA}.reloptioned (id integer) "
                "WITH (autovacuum_analyze_threshold = 500, autovacuum_analyze_scale_factor = 0.1)"
            )
            cursor.execute(f"INSERT INTO {SCHEMA}.reloptioned SELECT generate_series(1, 1000)")
            cursor.execute(f"ANALYZE {SCHEMA}.reloptioned")
            cursor.execute(f"INSERT INTO {SCHEMA}.reloptioned SELECT generate_series(1, 5000)")
            # Zero-statistics class: rows exist, never vacuumed/analyzed, below every threshold.
            cursor.execute(f"CREATE TABLE {SCHEMA}.zero_stats (id integer)")
            cursor.execute(f"INSERT INTO {SCHEMA}.zero_stats SELECT generate_series(1, 18)")
            _flush_and_wait(cursor, throwaway_database_url, relation="reloptioned", live=6000)
            _flush_and_wait(cursor, throwaway_database_url, relation="zero_stats", live=18)
            cursor.execute(
                "SELECT n_dead_tup FROM pg_stat_all_tables WHERE schemaname = %s AND relname = 'excluded'",
                (SCHEMA,),
            )
            # Without the exclusion this relation would qualify (dead tuples over 50 + 0.2 * reltuples).
            assert cursor.fetchone()[0] > 50

            section, rows = _rows_by_relation(throwaway_database_url)
            if "reloptioned" not in rows:
                # Autoanalyze won the race; regenerate modifications once and probe again.
                cursor.execute(f"INSERT INTO {SCHEMA}.reloptioned SELECT generate_series(1, 20000)")
                _flush_and_wait(cursor, throwaway_database_url, relation="reloptioned", live=26000)
                section, rows = _rows_by_relation(throwaway_database_url)
    finally:
        connection.close()

    assert "excluded" not in rows

    reloptioned = rows["reloptioned"]
    assert reloptioned["analyze_threshold"] == pytest.approx(500 + 0.1 * max(reloptioned["reltuples"], 0))
    assert reloptioned["n_mod_since_analyze"] > reloptioned["analyze_threshold"]
    assert reloptioned["analyze_threshold"] != pytest.approx(50 + 0.1 * max(reloptioned["reltuples"], 0))
    assert reloptioned["last_analyze_age_seconds"] is not None

    zero = rows["zero_stats"]
    assert zero["relpages"] == 0
    assert zero["reltuples"] < 0
    assert zero["n_live_tup"] == 18
    assert zero["last_analyze_age_seconds"] is None
    assert zero["last_autoanalyze_age_seconds"] is None
    assert zero["vacuum_threshold"] == pytest.approx(50.0)

    for row in rows.values():
        assert set(row) == set(_ROW_TYPES)
        for key, types in _ROW_TYPES.items():
            assert isinstance(row[key], types), (key, row[key])
    assert len(section["rows"]) <= 50

    summary = section["summary"]
    for key in ("max_last_autovacuum_age_seconds", "max_last_autoanalyze_age_seconds"):
        assert isinstance(summary[key], _FLOAT_OR_NONE)
    for key in ("over_vacuum_threshold_count", "over_analyze_threshold_count", "relation_count"):
        assert isinstance(summary[key], int)
    assert summary["over_analyze_threshold_count"] >= 1

    # The evaluator consumes the real shape without raising and names the zero-stat table.
    receipt = {"postgres": {"status": "ok", "maintenance_output": section}}
    findings = governance._maintenance_output_recommendations(receipt["postgres"], governance.AuditThresholds())
    assert ("TABLE_ZERO_STATISTICS", f"{SCHEMA}.zero_stats") in {
        (item["code"], item["evidence"].get("relation")) for item in findings
    }
    assert "MAINTENANCE_OUTPUT_UNAVAILABLE" not in {item["code"] for item in findings}
