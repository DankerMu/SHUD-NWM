"""Real-database integration tests for the river_timeseries dual write (#1340).

Run with the repo's standard opt-in against a throwaway database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_river_ts_dual_write_integration.py

``throwaway_database_url`` (tests/conftest.py) creates and drops a
uniquely-named database per TEST, so nothing here can touch a live one.

Scope: what only a real database can answer — that the seven surrogate columns
actually land non-NULL, that the ``ON CONFLICT DO UPDATE`` mirror fires on a
drifted row (design D3: production DELETE-replaces first, so this branch is
reached by replaying the INSERT without the preceding DELETE), that the #1339
equality audit sees zero new divergence, that the backfill runner still counts
only legacy sentinel rows, and that the seed's ``y_stage``/``m`` branch — the
only writer of those enum values — round-trips through the enum columns.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from packages.common.object_store import LocalObjectStore
from tests.integration_helpers import apply_migrations_from_zero
from workers.output_parser.parser import (
    OutputParser,
    OutputParserConfig,
    PsycopgOutputParserRepository,
)

pytestmark = pytest.mark.integration

NORMALIZED_COLUMNS = (
    "run_key",
    "river_network_version_key",
    "basin_version_key",
    "river_segment_key",
    "variable_e",
    "unit_e",
    "quality_flag_e",
)

_RUN_ID = "run_dual_write"
_START_TIME = datetime(2026, 6, 1, tzinfo=UTC)
_SEGMENTS = 4
_HOURS = 3
_OBJECT_STORE_PREFIX = "s3://nhms"


def _connect(database_url: str) -> Any:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    return connection


def _scalar(connection: Any, sql: str, params: Any = None) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    return None if row is None else next(iter(row.values()))


def _rows(connection: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def _verify(connection: Any) -> dict[str, Any]:
    return _rows(connection, "SELECT * FROM hydro.verify_river_identity_normalization()")[0]


def _segment_id(index: int) -> str:
    return f"seg-{index}"


# Every authority table's IDENTITY starts at 1, so a one-row-per-table fixture
# would give run_key = basin_version_key = river_network_version_key = 1 and a
# permutation of the four keys would be unobservable. Restarting each identity
# in its own decade forces pairwise-distinct keys for this fixture's rows.
_IDENTITY_RESTARTS = (
    ("hydro.hydro_run", "run_key", 4001),
    ("core.basin_version", "basin_version_key", 5001),
    ("core.river_network_version", "river_network_version_key", 6001),
    ("core.river_segment", "river_segment_key", 7001),
)


def _seed_authority(connection: Any, *, output_uri: str) -> None:
    """Authority rows the writer resolves keys from. It never creates them."""
    with connection.cursor() as cursor:
        for table, column, start in _IDENTITY_RESTARTS:
            cursor.execute(f"ALTER TABLE {table} ALTER COLUMN {column} RESTART WITH {start}")
        cursor.execute("INSERT INTO core.basin VALUES ('b1', 'B1', NULL, NULL, now()) ON CONFLICT DO NOTHING")
        cursor.execute(
            """
            INSERT INTO core.basin_version
                (basin_version_id, basin_id, version_label, geom, active_flag)
            VALUES ('bv1', 'b1', 'v1',
                    ST_SetSRID(ST_GeomFromText('MULTIPOLYGON(((0 0,0 1,1 1,0 0)))'), 4490), true)
            ON CONFLICT DO NOTHING
            """
        )
        cursor.execute(
            """
            INSERT INTO core.river_network_version
                (river_network_version_id, basin_version_id, version_label, segment_count)
            VALUES ('rnv1', 'bv1', 'v1', %s) ON CONFLICT DO NOTHING
            """,
            (_SEGMENTS,),
        )
        cursor.execute(
            """
            INSERT INTO core.river_segment
                (river_segment_id, river_network_version_id, segment_order, properties_json)
            SELECT 'seg-' || g, 'rnv1', g, '{"shud_output_river": "true"}'::jsonb
            FROM generate_series(1, %s) g
            ON CONFLICT DO NOTHING
            """,
            (_SEGMENTS,),
        )
        cursor.execute(
            """
            INSERT INTO core.model_instance
                (model_id, basin_version_id, river_network_version_id, mesh_version_id,
                 calibration_version_id, shud_code_version, model_package_uri)
            VALUES ('m1', 'bv1', 'rnv1', 'mv1', 'cal1', '1.0', 's3://x')
            ON CONFLICT DO NOTHING
            """
        )
        cursor.execute(
            """
            INSERT INTO hydro.hydro_run
                (run_id, run_type, scenario_id, model_id, basin_version_id, cycle_time,
                 start_time, end_time, status, run_manifest_uri, output_uri)
            VALUES (%s, 'forecast', 'sc', 'm1', 'bv1', %s, %s, %s, 'succeeded', 's3://m', %s)
            ON CONFLICT DO NOTHING
            """,
            (_RUN_ID, _START_TIME, _START_TIME, _START_TIME + timedelta(hours=_HOURS), output_uri),
        )


def _write_rivqdown(root: Path) -> LocalObjectStore:
    """A ``.rivqdown`` whose column count matches the seeded segment count."""
    store = LocalObjectStore(root, _OBJECT_STORE_PREFIX)
    header = ",".join(["time", *(_segment_id(index) for index in range(1, _SEGMENTS + 1))])
    lines = [header]
    for hour in range(_HOURS):
        timestamp = (_START_TIME + timedelta(hours=hour)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(",".join([timestamp, *(str(86400 * (hour + 1) * n) for n in range(1, _SEGMENTS + 1))]))
    store.write_bytes_atomic(
        f"runs/{_RUN_ID}/output/demo.rivqdown",
        ("\n".join(lines) + "\n").encode("utf-8"),
    )
    return store


def _parse(database_url: str, root: Path) -> Any:
    store = _write_rivqdown(root)
    parser = OutputParser(
        config=OutputParserConfig(
            object_store_root=root,
            object_store_prefix=_OBJECT_STORE_PREFIX,
            batch_size=64,
        ),
        repository=PsycopgOutputParserRepository(database_url=database_url),
        object_store=store,
    )
    return parser.parse_run(_RUN_ID)


@pytest.fixture()
def parsed_run(throwaway_database_url: str, tmp_path: Path) -> Any:
    """A migrated database with authority rows and one parsed run."""
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    _seed_authority(connection, output_uri=f"{_OBJECT_STORE_PREFIX}/runs/{_RUN_ID}/output/")
    before = _verify(connection)
    result = _parse(throwaway_database_url, tmp_path / "object-store")
    try:
        yield connection, before, result
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Scenario: new rows carry a complete, consistent surrogate identity
# ---------------------------------------------------------------------------


def test_parsed_rows_have_all_seven_surrogate_columns_populated(parsed_run: Any) -> None:
    connection, _before, result = parsed_run
    assert result.rows_written == _SEGMENTS * _HOURS

    total = _scalar(connection, "SELECT count(*) FROM hydro.river_timeseries WHERE run_id = %s", (_RUN_ID,))
    assert total == _SEGMENTS * _HOURS

    for column in NORMALIZED_COLUMNS:
        assert (
            _scalar(
                connection,
                f"SELECT count(*) FROM hydro.river_timeseries WHERE run_id = %s AND {column} IS NULL",
                (_RUN_ID,),
            )
            == 0
        ), f"{column} must be populated by the dual write"


def test_surrogate_keys_match_the_authority_rows_and_enums_match_the_text(parsed_run: Any) -> None:
    connection, _before, _result = parsed_run

    # Premise of the comparison below: the four keys differ from each other on
    # every written row, so swapping any two of them would be caught.
    for row in _rows(
        connection,
        """
        SELECT DISTINCT run_key, basin_version_key, river_network_version_key, river_segment_key
        FROM hydro.river_timeseries WHERE run_id = %s
        """,
        (_RUN_ID,),
    ):
        assert len(set(row.values())) == 4, f"fixture keys must be pairwise distinct, got {row}"

    mismatched = _scalar(
        connection,
        """
        SELECT count(*)
        FROM hydro.river_timeseries t
        JOIN hydro.hydro_run h ON h.run_id = t.run_id
        JOIN core.basin_version bv ON bv.basin_version_id = t.basin_version_id
        JOIN core.river_network_version rnv
          ON rnv.river_network_version_id = t.river_network_version_id
        JOIN core.river_segment rs
          ON rs.river_segment_id = t.river_segment_id
         AND rs.river_network_version_id = t.river_network_version_id
        WHERE t.run_id = %s
          AND (t.run_key IS DISTINCT FROM h.run_key
            OR t.basin_version_key IS DISTINCT FROM bv.basin_version_key
            OR t.river_network_version_key IS DISTINCT FROM rnv.river_network_version_key
            OR t.river_segment_key IS DISTINCT FROM rs.river_segment_key
            OR t.variable_e::text IS DISTINCT FROM t.variable
            OR t.unit_e::text IS DISTINCT FROM t.unit
            OR t.quality_flag_e::text IS DISTINCT FROM t.quality_flag)
        """,
        (_RUN_ID,),
    )
    assert mismatched == 0


def test_equality_audit_counters_do_not_move_when_new_rows_land(parsed_run: Any) -> None:
    connection, before, _result = parsed_run
    after = _verify(connection)

    assert after["rows_total"] == before["rows_total"] + _SEGMENTS * _HOURS
    for column in NORMALIZED_COLUMNS:
        assert after[f"null_{column}"] - before[f"null_{column}"] == 0
    assert after["equality_audit_divergent"] - before["equality_audit_divergent"] == 0


def test_reparse_is_idempotent_and_keeps_the_surrogate_columns_filled(
    parsed_run: Any, throwaway_database_url: str, tmp_path: Path
) -> None:
    """The production path DELETE-replaces, so a re-parse must not leave any
    NULL-key leftovers behind."""
    connection, _before, _result = parsed_run
    _parse(throwaway_database_url, tmp_path / "object-store-2")

    assert _scalar(connection, "SELECT count(*) FROM hydro.river_timeseries") == _SEGMENTS * _HOURS
    assert _verify(connection)["null_run_key"] == 0


# ---------------------------------------------------------------------------
# Scenario: conflict updates cannot re-introduce text<->surrogate drift
# ---------------------------------------------------------------------------


_REPLAY_INSERT = """
    INSERT INTO hydro.river_timeseries (
        run_id, basin_version_id, river_network_version_id, river_segment_id,
        valid_time, lead_time_hours, variable, value, unit, quality_flag,
        run_key, river_network_version_key, basin_version_key, river_segment_key,
        variable_e, unit_e, quality_flag_e
    )
    VALUES (%(run_id)s, %(basin_version_id)s, %(river_network_version_id)s, %(river_segment_id)s,
            %(valid_time)s, %(lead_time_hours)s, %(variable)s, %(value)s, %(unit)s, %(quality_flag)s,
            %(run_key)s, %(river_network_version_key)s, %(basin_version_key)s, %(river_segment_key)s,
            %(variable)s, %(unit)s, %(quality_flag)s)
    ON CONFLICT (run_id, river_network_version_id, river_segment_id, variable, valid_time)
    DO UPDATE SET
        basin_version_id = EXCLUDED.basin_version_id,
        lead_time_hours = EXCLUDED.lead_time_hours,
        value = EXCLUDED.value,
        unit = EXCLUDED.unit,
        quality_flag = EXCLUDED.quality_flag,
        basin_version_key = EXCLUDED.basin_version_key,
        unit_e = EXCLUDED.unit_e,
        quality_flag_e = EXCLUDED.quality_flag_e
"""


def test_conflict_update_mirrors_the_text_refresh_into_the_surrogate_columns(parsed_run: Any) -> None:
    """Replay the writer's INSERT without the preceding DELETE, against a row
    whose refreshable text columns have drifted away from their surrogates."""
    connection, _before, _result = parsed_run

    # A second basin_version so basin_version_id/key can genuinely change.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO core.basin_version
                (basin_version_id, basin_id, version_label, geom, active_flag)
            VALUES ('bv2', 'b1', 'v2',
                    ST_SetSRID(ST_GeomFromText('MULTIPOLYGON(((0 0,0 1,1 1,0 0)))'), 4490), false)
            ON CONFLICT DO NOTHING
            """
        )
    bv2_key = _scalar(
        connection, "SELECT basin_version_key FROM core.basin_version WHERE basin_version_id = 'bv2'"
    )

    target = _rows(
        connection,
        """
        SELECT * FROM hydro.river_timeseries
        WHERE run_id = %s ORDER BY valid_time, river_segment_id LIMIT 1
        """,
        (_RUN_ID,),
    )[0]

    # Pre-drift the existing row: text says bv2/qc_warning, surrogates still
    # say bv1/ok. This is exactly the drift 000050:244-247 predicted.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE hydro.river_timeseries
            SET basin_version_id = 'bv2', quality_flag = 'qc_warning'
            WHERE run_id = %(run_id)s AND river_network_version_id = %(river_network_version_id)s
              AND river_segment_id = %(river_segment_id)s AND variable = %(variable)s
              AND valid_time = %(valid_time)s
            """,
            target,
        )
    assert _verify(connection)["equality_audit_divergent"] >= 1

    replay = dict(target)
    replay.update(
        {
            "basin_version_id": "bv2",
            "basin_version_key": bv2_key,
            "quality_flag": "qc_warning",
            "value": float(target["value"]) + 42.0,
        }
    )
    with connection.cursor() as cursor:
        cursor.execute(_REPLAY_INSERT, replay)

    updated = _rows(
        connection,
        """
        SELECT * FROM hydro.river_timeseries
        WHERE run_id = %(run_id)s AND river_network_version_id = %(river_network_version_id)s
          AND river_segment_id = %(river_segment_id)s AND variable = %(variable)s
          AND valid_time = %(valid_time)s
        """,
        target,
    )
    assert len(updated) == 1, "the replay must UPDATE the existing row, not insert a second one"
    row = updated[0]

    # Refreshed text columns and their surrogate mirrors moved together.
    assert row["basin_version_id"] == "bv2"
    assert row["basin_version_key"] == bv2_key
    assert row["quality_flag"] == "qc_warning"
    assert row["quality_flag_e"] == "qc_warning"
    assert row["unit"] == target["unit"]
    assert row["unit_e"] == target["unit"]
    assert float(row["value"]) == pytest.approx(float(target["value"]) + 42.0)

    # Identity columns unchanged on both sides.
    for column in ("run_id", "river_network_version_id", "river_segment_id", "variable", "valid_time"):
        assert row[column] == target[column]
    for column in ("run_key", "river_network_version_key", "river_segment_key"):
        assert row[column] == target[column]
    assert row["variable_e"] == target["variable_e"]

    # And the audit is clean again: the mirror closed the drift.
    assert _verify(connection)["equality_audit_divergent"] == 0


def test_out_of_vocabulary_enum_literal_fails_the_statement_closed(parsed_run: Any) -> None:
    """A literal outside the enum's value set is rejected by the column type —
    the closed-world property the writer relies on instead of app-side maps."""
    connection, _before, _result = parsed_run
    target = _rows(
        connection,
        "SELECT * FROM hydro.river_timeseries WHERE run_id = %s ORDER BY valid_time LIMIT 1",
        (_RUN_ID,),
    )[0]
    replay = dict(target)
    replay.update({"unit": "m3 s-1", "valid_time": target["valid_time"] + timedelta(hours=99)})

    with pytest.raises(psycopg2.errors.InvalidTextRepresentation):
        with connection.cursor() as cursor:
            cursor.execute(_REPLAY_INSERT, replay)

    assert _scalar(
        connection,
        "SELECT count(*) FROM hydro.river_timeseries WHERE valid_time = %s",
        (replay["valid_time"],),
    ) == 0


# ---------------------------------------------------------------------------
# Scenario: dual write coexists with the #1339 backfill lane
# ---------------------------------------------------------------------------


def _run_backfill(database_url: str, tmp_path: Path) -> dict[str, Any]:
    from scripts import node27_river_identity_backfill as backfill

    tmp_path.mkdir(parents=True, exist_ok=True)
    env = {
        "DATABASE_URL": database_url,
        "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "1",
        "NODE27_RIVER_IDENTITY_BACKFILL_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_RIVER_IDENTITY_BACKFILL_LOCK_PATH": str(tmp_path / "runner.lock"),
        "NODE27_RIVER_IDENTITY_BACKFILL_BATCH_PAGES": "4",
        "NODE27_RIVER_IDENTITY_BACKFILL_BATCH_SLEEP_MS": "0",
    }
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        backfill.main(["--enforce"], now_utc=datetime.now(UTC) + timedelta(days=3650))
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))


def test_backfill_runner_only_claims_legacy_sentinel_rows(
    parsed_run: Any, throwaway_database_url: str, tmp_path: Path
) -> None:
    """Dual-written rows are not sentinel candidates; only pre-#1340 rows are,
    and the runner leaves the dual-written surrogates byte-identical."""
    connection, _before, _result = parsed_run
    legacy_time = _START_TIME + timedelta(days=1)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO hydro.river_timeseries
                (run_id, basin_version_id, river_network_version_id, river_segment_id,
                 valid_time, variable, value, unit, quality_flag)
            SELECT %s, 'bv1', 'rnv1', 'seg-' || s, %s::timestamptz + (h * INTERVAL '1 hour'),
                   'q_down', 1.0, 'm3/s', 'ok'
            FROM generate_series(1, %s) s, generate_series(0, %s) h
            """,
            (_RUN_ID, legacy_time, _SEGMENTS, _HOURS - 1),
        )
    legacy_rows = _SEGMENTS * _HOURS
    assert _scalar(connection, "SELECT count(*) FROM hydro.river_timeseries WHERE run_key IS NULL") == legacy_rows

    dual_written_before = _rows(
        connection,
        """
        SELECT valid_time, river_segment_id, run_key, river_network_version_key,
               basin_version_key, river_segment_key, variable_e, unit_e, quality_flag_e
        FROM hydro.river_timeseries WHERE run_key IS NOT NULL
        ORDER BY valid_time, river_segment_id
        """,
    )

    receipt = _run_backfill(throwaway_database_url, tmp_path / "backfill")

    assert receipt["totals"]["candidate_rows"] == legacy_rows
    assert receipt["totals"]["updated_rows"] == legacy_rows
    assert receipt["totals"]["pending_rows"] == 0

    dual_written_after = _rows(
        connection,
        """
        SELECT valid_time, river_segment_id, run_key, river_network_version_key,
               basin_version_key, river_segment_key, variable_e, unit_e, quality_flag_e
        FROM hydro.river_timeseries WHERE run_key IS NOT NULL
          AND valid_time < %s
        ORDER BY valid_time, river_segment_id
        """,
        (legacy_time,),
    )
    assert dual_written_after == dual_written_before
    assert _verify(connection)["equality_audit_divergent"] == 0


# ---------------------------------------------------------------------------
# Scenario: the seed writer's y_stage / m branch
# ---------------------------------------------------------------------------


def test_seed_demo_dual_writes_the_y_stage_branch(throwaway_database_url: str) -> None:
    from db.seeds.seed_demo import seed_database

    apply_migrations_from_zero(throwaway_database_url)
    seed_connection = psycopg2.connect(throwaway_database_url)
    seed_connection.autocommit = True
    try:
        seed_database(seed_connection)
    finally:
        seed_connection.close()

    connection = _connect(throwaway_database_url)
    try:
        assert _scalar(connection, "SELECT count(*) FROM hydro.river_timeseries WHERE variable = 'y_stage'") > 0
        for column in NORMALIZED_COLUMNS:
            assert (
                _scalar(connection, f"SELECT count(*) FROM hydro.river_timeseries WHERE {column} IS NULL") == 0
            ), f"seed left {column} NULL"

        assert _scalar(
            connection,
            """
            SELECT count(*) FROM hydro.river_timeseries
            WHERE variable_e::text IS DISTINCT FROM variable
               OR unit_e::text IS DISTINCT FROM unit
               OR quality_flag_e::text IS DISTINCT FROM quality_flag
            """,
        ) == 0
        assert _scalar(
            connection,
            """
            SELECT count(*) FROM hydro.river_timeseries t
            JOIN core.river_segment rs
              ON rs.river_segment_id = t.river_segment_id
             AND rs.river_network_version_id = t.river_network_version_id
            WHERE t.river_segment_key IS DISTINCT FROM rs.river_segment_key
            """,
        ) == 0
        assert _verify(connection)["equality_audit_divergent"] == 0
    finally:
        connection.close()
