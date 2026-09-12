"""Disposable-database coverage of narrow writes and compressed-key access."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from packages.common.object_store import LocalObjectStore
from packages.common.timescale_write_guard import CompressedChunkWriteError
from tests.integration_helpers import apply_migrations_from_zero
from tests.test_river_ts_text_identity_cleanup import (
    _parser_river_statements,
)
from workers.output_parser.parser import (
    OutputParser,
    OutputParserConfig,
    PsycopgOutputParserRepository,
    RiverTimeseriesRow,
    RunIdentityKeys,
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
    result = _parse(throwaway_database_url, tmp_path / "object-store")
    try:
        yield connection, result
    finally:
        connection.close()


_REPLAY_INSERT = """
    INSERT INTO hydro.river_timeseries (
        valid_time, lead_time_hours, value,
        run_key, river_network_version_key, basin_version_key, river_segment_key,
        variable_e, unit_e, quality_flag_e
    )
    VALUES (%(valid_time)s, %(lead_time_hours)s, %(value)s,
            %(run_key)s, %(river_network_version_key)s, %(basin_version_key)s, %(river_segment_key)s,
            %(variable_e)s, %(unit_e)s, %(quality_flag_e)s)
    ON CONFLICT (run_key, river_segment_key, variable_e, valid_time)
    DO UPDATE SET
        lead_time_hours = EXCLUDED.lead_time_hours,
        value = EXCLUDED.value,
        river_network_version_key = EXCLUDED.river_network_version_key,
        basin_version_key = EXCLUDED.basin_version_key,
        unit_e = EXCLUDED.unit_e,
        quality_flag_e = EXCLUDED.quality_flag_e
"""


def test_parse_and_replay_write_only_narrow_facts(
    parsed_run: Any, throwaway_database_url: str, tmp_path: Path
) -> None:
    connection, result = parsed_run
    assert result.rows_written == 12
    facts = _rows(connection, "SELECT * FROM hydro.river_timeseries ORDER BY valid_time, river_segment_key")
    assert len(facts) == 12
    assert facts[0]["value"] == 1.0
    assert (facts[0]["run_key"], facts[0]["basin_version_key"],
            facts[0]["river_network_version_key"], facts[0]["river_segment_key"]) == (4001, 5001, 6001, 7001)
    assert _scalar(connection, "SELECT count(*) FROM hydro.river_timeseries_legacy") == 0
    _parse(throwaway_database_url, tmp_path / "replay")
    assert _scalar(connection, "SELECT count(*) FROM hydro.river_timeseries") == 12
    assert _scalar(connection, "SELECT timeseries_store FROM hydro.hydro_run WHERE run_id = %s",
                   (_RUN_ID,)) == "narrow"
    target = dict(facts[0], value=42.0, lead_time_hours=None, quality_flag_e="qc_warning")
    with connection.cursor() as cursor:
        cursor.execute(_REPLAY_INSERT, target)
    updated = _rows(connection, "SELECT value, lead_time_hours, quality_flag_e FROM hydro.river_timeseries "
                    "ORDER BY valid_time, river_segment_key")[0]
    assert updated == {"value": 42.0, "lead_time_hours": None, "quality_flag_e": "qc_warning"}
    assert _scalar(connection, "SELECT count(*) FROM hydro.river_timeseries") == 12


def test_legacy_refusal_preserves_facts_and_parse_timestamp(
    parsed_run: Any, throwaway_database_url: str, tmp_path: Path
) -> None:
    from workers.output_parser.parser import LegacyStoreWriteRefused

    connection, _result = parsed_run
    before = _rows(connection, "SELECT * FROM hydro.river_timeseries ORDER BY valid_time, river_segment_key")
    stamp = _scalar(connection, "SELECT parsed_at FROM hydro.hydro_run WHERE run_id = %s", (_RUN_ID,))
    with connection.cursor() as cursor:
        cursor.execute("UPDATE hydro.hydro_run SET timeseries_store = 'legacy' WHERE run_id = %s", (_RUN_ID,))
    with pytest.raises(LegacyStoreWriteRefused):
        _parse(throwaway_database_url, tmp_path / "refused")
    assert _rows(connection, "SELECT * FROM hydro.river_timeseries ORDER BY valid_time, river_segment_key") == before
    assert _scalar(connection, "SELECT parsed_at FROM hydro.hydro_run WHERE run_id = %s", (_RUN_ID,)) == stamp


def test_expand_replay_does_not_reclassify_a_new_parse(parsed_run: Any) -> None:
    from packages.common.migrate import MIGRATIONS_DIR

    connection, _result = parsed_run
    migration = (MIGRATIONS_DIR / "000059_river_timeseries_narrow_expand.sql").read_text()
    with connection.cursor() as cursor:
        cursor.execute(migration)
        cursor.execute(migration)
    assert _scalar(connection, "SELECT timeseries_store FROM hydro.hydro_run WHERE run_id = %s",
                   (_RUN_ID,)) == "narrow"
    assert _scalar(connection, "SELECT count(*) FROM hydro.river_timeseries") == 12
    assert _scalar(connection, "SELECT count(*) FROM pg_indexes WHERE schemaname = 'hydro' "
                   "AND tablename = 'river_timeseries'") == 3
    assert _scalar(connection, "SELECT time_interval FROM timescaledb_information.dimensions "
                   "WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries'") == timedelta(days=1)



_AID_OLD_RUN_ID = "run_probe_aid_old"
_AID_NEW_RUN_ID = "run_probe_aid_new"
# Two chunks apart at the hypertable's default 7-day chunk_time_interval
# (000006 creates hydro.river_timeseries with no explicit interval), so the new
# run's rows cannot land in the compressed chunk by accident.
_AID_NEW_START_TIME = _START_TIME + timedelta(days=14)


def _plan_text(cursor: Any, sql: str, params: tuple[Any, ...]) -> str:
    """``EXPLAIN (COSTS OFF)`` output as one string.

    Row unpacking is factory-agnostic on purpose: this module connects with
    ``RealDictCursor`` while the repository under test uses psycopg2's default
    tuple cursor, and the same helper is used from a plain connection here.
    """
    cursor.execute(f"EXPLAIN (COSTS OFF) {sql}", params)
    return "\n".join(
        str(next(iter(row.values())) if isinstance(row, dict) else row[0]) for row in cursor.fetchall()
    )


def _index_cond_lines(plan: str) -> list[str]:
    return [line.strip() for line in plan.splitlines() if line.strip().startswith("Index Cond:")]


def _seed_compressed_history_and_new_run(connection: Any) -> dict[str, Any]:
    """One compressed chunk of an OLD run's rows plus a registered, empty NEW run.

    This is the production shape the #1681 regression needs: history already
    compressed, and a freshly registered run whose probe must prove "no rows"
    across it.
    """
    # ``_seed_authority``'s own ``output_uri`` belongs to the module's default
    # run, which this fixture never parses; the two runs below are the ones
    # under test and carry their own.
    _seed_authority(connection, output_uri=f"{_OBJECT_STORE_PREFIX}/runs/{_RUN_ID}/output/")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO hydro.hydro_run
                (run_id, run_type, scenario_id, model_id, basin_version_id, cycle_time,
                 start_time, end_time, status, run_manifest_uri, output_uri)
            VALUES (%s, 'forecast', 'sc', 'm1', 'bv1', %s, %s, %s, 'succeeded', 's3://m', %s)
            """,
            (
                _AID_OLD_RUN_ID,
                _START_TIME,
                _START_TIME,
                _START_TIME + timedelta(hours=_HOURS),
                f"{_OBJECT_STORE_PREFIX}/runs/{_AID_OLD_RUN_ID}/output/",
            ),
        )
        # Fully normalized fact rows: the compressed chunk must look exactly
        # like production history, not like a pre-#1340 sentinel batch.
        cursor.execute(
            """
            INSERT INTO hydro.river_timeseries (
                valid_time, lead_time_hours, value,
                run_key, river_network_version_key, basin_version_key, river_segment_key,
                variable_e, unit_e, quality_flag_e
            )
            SELECT %s::timestamptz + (h * INTERVAL '1 hour'), h, 1.0,
                   (SELECT run_key FROM hydro.hydro_run WHERE run_id = %s),
                   (SELECT river_network_version_key FROM core.river_network_version
                     WHERE river_network_version_id = 'rnv1'),
                   (SELECT basin_version_key FROM core.basin_version WHERE basin_version_id = 'bv1'),
                   rs.river_segment_key, 'q_down', 'm3/s', 'ok'
            FROM core.river_segment rs, generate_series(0, %s) h
            WHERE rs.river_network_version_id = 'rnv1'
            """,
            (_START_TIME, _AID_OLD_RUN_ID, _HOURS - 1),
        )
        cursor.execute(
            """
            SELECT compress_chunk(format('%I.%I', chunk_schema, chunk_name)::regclass)
            FROM timescaledb_information.chunks
            WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries'
            ORDER BY range_start
            """
        )
        # `run_key` is GENERATED ALWAYS AS IDENTITY (000050): never in the
        # column list, always read back.
        cursor.execute(
            """
            INSERT INTO hydro.hydro_run
                (run_id, run_type, scenario_id, model_id, basin_version_id, cycle_time,
                 start_time, end_time, status, run_manifest_uri, output_uri)
            VALUES (%s, 'forecast', 'sc', 'm1', 'bv1', %s, %s, %s, 'succeeded', 's3://m', %s)
            RETURNING run_key
            """,
            (
                _AID_NEW_RUN_ID,
                _AID_NEW_START_TIME,
                _AID_NEW_START_TIME,
                _AID_NEW_START_TIME + timedelta(hours=_HOURS),
                f"{_OBJECT_STORE_PREFIX}/runs/{_AID_NEW_RUN_ID}/output/",
            ),
        )
        new_run_key = next(iter(cursor.fetchone().values()))

    compressed = _scalar(
        connection,
        """
        SELECT count(*) FROM timescaledb_information.chunks
        WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries' AND is_compressed
        """,
    )
    assert compressed >= 1, "fixture premise: the old run's chunk must be compressed"
    assert (
        _scalar(
            connection,
            "SELECT count(*) FROM hydro.river_timeseries WHERE run_key = %s",
            (new_run_key,),
        )
        == 0
    ), "fixture premise: the new run starts with zero fact rows"

    return {
        "new_run_id": _AID_NEW_RUN_ID,
        "new_run_key": new_run_key,
        "river_network_version_key": _scalar(
            connection,
            "SELECT river_network_version_key FROM core.river_network_version "
            "WHERE river_network_version_id = 'rnv1'",
        ),
        "basin_version_key": _scalar(
            connection, "SELECT basin_version_key FROM core.basin_version WHERE basin_version_id = 'bv1'"
        ),
        "segment_keys": {
            row["river_segment_id"]: row["river_segment_key"]
            for row in _rows(
                connection,
                "SELECT river_segment_id, river_segment_key FROM core.river_segment "
                "WHERE river_network_version_id = 'rnv1'",
            )
        },
    }


@pytest.fixture()
def compressed_history(throwaway_database_url: str) -> Any:
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        yield connection, _seed_compressed_history_and_new_run(connection)
    finally:
        connection.close()


def _new_run_rows(start: datetime, segment_keys: dict[str, int]) -> tuple[Any, ...]:
    return tuple(
        RiverTimeseriesRow(
            run_id=_AID_NEW_RUN_ID,
            basin_version_id="bv1",
            river_network_version_id="rnv1",
            river_segment_id=segment_id,
            valid_time=start + timedelta(hours=hour),
            lead_time_hours=hour,
            variable="q_down",
            value=float(hour + 1),
            unit="m3/s",
        )
        for hour in range(_HOURS)
        for segment_id in sorted(segment_keys)
    )


def _new_run_identity(context: dict[str, Any]) -> RunIdentityKeys:
    return RunIdentityKeys(
        run_key=context["new_run_key"],
        river_network_version_key=context["river_network_version_key"],
        basin_version_key=context["basin_version_key"],
    )


@pytest.mark.timescaledb_210
def test_probe_uses_the_compressed_run_key_index(
    compressed_history: Any, throwaway_database_url: str
) -> None:
    """The unbounded probe reaches compressed history by its run_key segment."""
    _connection, context = compressed_history
    probe = _parser_river_statements()[0]

    assert probe.count("%s") == 3

    keys = (context["new_run_key"], context["river_network_version_key"], "q_down")
    # NOT this module's ``_connect``: that one sets autocommit, under which
    # ``SET LOCAL`` warns and does nothing.
    plain = psycopg2.connect(throwaway_database_url)
    try:
        with plain.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            plan = _plan_text(cursor, probe, keys)
        plain.rollback()
    finally:
        plain.close()

    assert "Index Scan using compress_hyper_" in plan, plan
    assert any("run_key =" in line for line in _index_cond_lines(plan)), plan
    assert "Seq Scan on compress_hyper" not in plan, plan



@pytest.mark.timescaledb_210
def test_new_run_writes_past_a_compressed_chunk_and_replays_idempotently(
    compressed_history: Any, throwaway_database_url: str
) -> None:
    """The end-to-end shape the regression broke: a new run ingests, twice.

    The probe and window read cross the compressed chunk on both passes (the
    second one takes the other branch — rows now exist — so the MATERIALIZED
    window read runs too), while the guarded DELETE/INSERT stay in the
    uncompressed chunk.
    """
    connection, context = compressed_history
    rows = _new_run_rows(_AID_NEW_START_TIME, context["segment_keys"])
    repository = PsycopgOutputParserRepository(database_url=throwaway_database_url)

    repository.upsert_river_timeseries(
        rows,
        batch_size=64,
        run_identity=_new_run_identity(context),
        segment_keys=context["segment_keys"],
    )
    written = _scalar(
        connection, "SELECT count(*) FROM hydro.river_timeseries WHERE run_key = %s",
        (context["new_run_key"],),
    )
    assert written == len(rows)

    repository.upsert_river_timeseries(
        rows,
        batch_size=64,
        run_identity=_new_run_identity(context),
        segment_keys=context["segment_keys"],
    )
    assert (
        _scalar(
            connection, "SELECT count(*) FROM hydro.river_timeseries WHERE run_key = %s",
            (context["new_run_key"],),
        )
        == len(rows)
    ), "replaying the same window must replace, not duplicate"
    # The old run's compressed history is untouched by either pass.
    assert _scalar(
        connection, "SELECT count(*) FROM hydro.river_timeseries "
        "JOIN hydro.hydro_run USING (run_key) WHERE run_id = %s", (_AID_OLD_RUN_ID,),
    ) == _SEGMENTS * _HOURS


@pytest.mark.timescaledb_210
def test_new_run_targeting_the_compressed_chunk_still_fails_the_guard_closed(
    compressed_history: Any, throwaway_database_url: str
) -> None:
    """The aid must not have bought throughput with silent partial writes.

    Narrowing the two reads by valid_time (the rejected alternative) would have
    turned "the replacement window reaches a compressed chunk" from a closed
    failure into surviving rows nobody deletes. The aid narrows nothing, so
    this stays exactly as it was: a batch aimed at compressed storage raises
    and writes nothing.
    """
    connection, context = compressed_history
    rows = _new_run_rows(_START_TIME, context["segment_keys"])
    repository = PsycopgOutputParserRepository(database_url=throwaway_database_url)

    with pytest.raises(CompressedChunkWriteError) as exc_info:
        repository.upsert_river_timeseries(
            rows,
            batch_size=64,
            run_identity=_new_run_identity(context),
            segment_keys=context["segment_keys"],
        )
    assert "hydro.river_timeseries" in str(exc_info.value)
    assert (
        _scalar(
            connection, "SELECT count(*) FROM hydro.river_timeseries WHERE run_key = %s",
            (context["new_run_key"],),
        )
        == 0
    ), "the guard must fail the batch closed, leaving no rows behind"


@pytest.mark.parametrize("failure", ["induced", "missing_owner"])
def test_expand_failure_rolls_back_and_replay_preserves_narrow_parse(
    throwaway_database_url: str, tmp_path: Path, failure: str,
) -> None:
    from uuid import uuid4

    from packages.common.migrate import MIGRATIONS_DIR, split_sql_statements

    apply_migrations_from_zero(throwaway_database_url, through="000058")
    sql = (MIGRATIONS_DIR / "000059_river_timeseries_narrow_expand.sql").read_text()
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection, output_uri="s3://nhms/runs/run_dual_write/output")
        before_oid = _scalar(connection, "SELECT 'hydro.river_timeseries'::regclass::oid")
        # Fail after rename/create/indexes, not before expand begins.
        if failure == "missing_owner":
            # Never drop or mutate a shared cluster role. Change only this
            # disposable migration's owner target to a verified absent name.
            owner = f"i7_absent_{uuid4().hex}"
            assert _scalar(connection, "SELECT count(*) FROM pg_roles WHERE rolname=%s", (owner,)) == 0
            replacement = f"ALTER TABLE hydro.river_timeseries OWNER TO {owner};"
            expected_error = psycopg2.errors.UndefinedObject
            expected_message = f'role "{owner}" does not exist'
        else:
            replacement = "RAISE EXCEPTION 'induced expand failure';"
            expected_error = psycopg2.errors.RaiseException
            expected_message = "induced expand failure"
        broken = sql.replace(
            "ALTER TABLE hydro.river_timeseries OWNER TO nhms_ingest_rw;",
            replacement,
        )
        with pytest.raises(expected_error, match=expected_message):
            with connection.cursor() as cursor:
                for statement in split_sql_statements(broken):
                    cursor.execute(statement)
        assert _scalar(connection, "SELECT 'hydro.river_timeseries'::regclass::oid") == before_oid
        assert _scalar(connection, "SELECT to_regclass('hydro.river_timeseries_legacy')") is None
        assert _scalar(connection, "SELECT count(*) FROM information_schema.columns "
                       "WHERE table_schema='hydro' AND table_name='hydro_run' "
                       "AND column_name='timeseries_store'") == 0
        apply_migrations_from_zero(throwaway_database_url)
        _parse(throwaway_database_url, tmp_path)
        facts = _rows(connection, "SELECT * FROM hydro.river_timeseries ORDER BY river_segment_key, valid_time")
        assert len(facts) == _SEGMENTS * _HOURS
        with connection.cursor() as cursor:
            for statement in split_sql_statements(sql):
                cursor.execute(statement)
        assert _scalar(connection, "SELECT timeseries_store FROM hydro.hydro_run WHERE run_id=%s",
                       (_RUN_ID,)) == "narrow"
        assert _rows(connection, "SELECT * FROM hydro.river_timeseries ORDER BY river_segment_key, valid_time") == facts
    finally:
        connection.close()


def test_legacy_decline_reopens_only_after_authority_becomes_narrow(
    throwaway_database_url: str,
) -> None:
    from scripts.node27_autopipeline import _declined_runs

    apply_migrations_from_zero(throwaway_database_url)
    connection = psycopg2.connect(throwaway_database_url)
    try:
        _seed_authority(connection, output_uri="s3://nhms/runs/run_dual_write/output")
        with connection.cursor() as cursor:
            cursor.execute("UPDATE hydro.hydro_run SET timeseries_store='legacy' WHERE run_id=%s", (_RUN_ID,))
            cursor.execute(
                "INSERT INTO ops.ingest_recompute_decline "
                "(run_id, init_state_id, product_mtime, reason_code, detail) "
                "VALUES (%s, '', 1, 'legacy_store_refused', 'fixture')", (_RUN_ID,),
            )
            assert _declined_runs(cursor, [_RUN_ID], None) == {_RUN_ID}
            cursor.execute("UPDATE hydro.hydro_run SET timeseries_store='narrow' WHERE run_id=%s", (_RUN_ID,))
            assert _declined_runs(cursor, [_RUN_ID], None) == set()
    finally:
        connection.rollback()
        connection.close()


def test_expand_classifies_preexisting_authority_without_overrides(throwaway_database_url: str) -> None:
    from packages.common.migrate import MIGRATIONS_DIR

    apply_migrations_from_zero(throwaway_database_url, through="000058")
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection, output_uri="s3://nhms/runs/run_dual_write/output")
        with connection.cursor() as cursor:
            cursor.execute("UPDATE hydro.hydro_run SET status='running', parsed_at=now() WHERE run_id=%s", (_RUN_ID,))
            cursor.execute("""
                INSERT INTO hydro.hydro_run
                    (run_id, run_type, model_id, basin_version_id, start_time, end_time, status, run_manifest_uri)
                SELECT v.run_id, 'forecast', 'm1', 'bv1', %s, %s, v.status, 's3://manifest'
                FROM (VALUES ('published_only', 'published'), ('running_only', 'running')) v(run_id, status)
            """, (_START_TIME, _START_TIME + timedelta(hours=3)))
        expected = [
            {"run_id": "published_only", "timeseries_store": "legacy"},
            {"run_id": "run_dual_write", "timeseries_store": "legacy"},
            {"run_id": "running_only", "timeseries_store": "narrow"},
        ]
        apply_migrations_from_zero(throwaway_database_url)
        for _ in range(2):
            assert _rows(connection, "SELECT run_id, timeseries_store FROM hydro.hydro_run ORDER BY run_id") == expected
            with connection.cursor() as cursor:
                cursor.execute((MIGRATIONS_DIR / "000059_river_timeseries_narrow_expand.sql").read_text())
    finally:
        connection.close()


@pytest.mark.timescaledb_210
def test_expand_preserves_preexisting_compressed_legacy_catalog(throwaway_database_url: str) -> None:
    from tests.integration_helpers import insert_river_timeseries_dual_written

    apply_migrations_from_zero(throwaway_database_url, through="000058")
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection, output_uri="s3://nhms/runs/run_dual_write/output")
        with connection.cursor() as cursor:
            insert_river_timeseries_dual_written(cursor, [
                (_RUN_ID, "bv1", "rnv1", "seg-1", _START_TIME, 0, "q_down", 7.0, "m3/s", "ok"),
            ])
            cursor.execute("SELECT compress_chunk(c) FROM show_chunks('hydro.river_timeseries') c")

        def snapshot(table: str) -> dict[str, Any]:
            return {
                "relation": _rows(
                    connection, "SELECT oid, relowner FROM pg_class WHERE oid=%s::regclass", (f"hydro.{table}",),
                ),
                "indexes": _rows(
                    connection, "SELECT indexrelid FROM pg_index WHERE indrelid=%s::regclass ORDER BY indexrelid",
                    (f"hydro.{table}",),
                ),
                "chunks": _rows(
                    connection, "SELECT chunk_schema, chunk_name, is_compressed FROM timescaledb_information.chunks "
                    "WHERE hypertable_schema='hydro' AND hypertable_name=%s ORDER BY chunk_name", (table,),
                ),
                "settings": _rows(
                    connection, "SELECT attname, segmentby_column_index, orderby_column_index, "
                    "orderby_asc, orderby_nullsfirst "
                    "FROM timescaledb_information.compression_settings WHERE hypertable_schema='hydro' "
                    "AND hypertable_name=%s ORDER BY attname", (table,),
                ),
            }

        before = snapshot("river_timeseries")
        assert before["chunks"] and all(row["is_compressed"] for row in before["chunks"])
        apply_migrations_from_zero(throwaway_database_url)
        assert snapshot("river_timeseries_legacy") == before
        assert _scalar(
            connection, "SELECT pg_get_userbyid(relowner) FROM pg_class "
            "WHERE oid='hydro.river_timeseries'::regclass",
        ) == "nhms_ingest_rw"
        assert _rows(
            connection, "SELECT column_name, udt_name FROM information_schema.columns WHERE table_schema='hydro' "
            "AND table_name='river_timeseries' ORDER BY ordinal_position",
        ) == [
            {"column_name": name, "udt_name": kind} for name, kind in (
                ("run_key", "int4"), ("basin_version_key", "int4"), ("river_network_version_key", "int4"),
                ("river_segment_key", "int4"), ("valid_time", "timestamptz"), ("lead_time_hours", "int4"),
                ("variable_e", "river_variable"), ("value", "float8"), ("unit_e", "river_unit"),
                ("quality_flag_e", "river_quality_flag"), ("created_at", "timestamptz"),
            )
        ]
        assert snapshot("river_timeseries")["settings"] == [
            dict(attname=name, segmentby_column_index=segment, orderby_column_index=order,
                 orderby_asc=asc, orderby_nullsfirst=nulls)
            for name, segment, order, asc, nulls in (
                ("river_segment_key", 2, None, None, None), ("run_key", 1, None, None, None),
                ("valid_time", None, 2, True, False), ("variable_e", None, 1, True, False),
            )
        ]
    finally:
        connection.close()


@pytest.mark.parametrize("column", ["variable_e", "unit_e", "quality_flag_e"])
def test_out_of_vocabulary_enum_literal_rejects_entire_narrow_write(parsed_run: Any, column: str) -> None:
    connection, _ = parsed_run
    before = _rows(connection, "SELECT * FROM hydro.river_timeseries ORDER BY river_segment_key, valid_time")
    with connection.cursor() as cursor:
        with pytest.raises(psycopg2.errors.InvalidTextRepresentation):
            cursor.execute(f"""
                INSERT INTO hydro.river_timeseries
                    (run_key, basin_version_key, river_network_version_key, river_segment_key,
                     valid_time, variable_e, value, unit_e, quality_flag_e)
                VALUES (4001,5001,6001,7001,%s,'q_down',9,'m3/s','ok'),
                       (4001,5001,6001,7001,%s,
                        {'%s' if column == 'variable_e' else "'q_down'"},
                        10, {'%s' if column == 'unit_e' else "'m3/s'"},
                        {'%s' if column == 'quality_flag_e' else "'ok'"})
            """, (_START_TIME + timedelta(days=20), _START_TIME + timedelta(days=21), "outside_vocabulary"))
    assert _rows(connection, "SELECT * FROM hydro.river_timeseries ORDER BY river_segment_key, valid_time") == before


@pytest.mark.parametrize("first", ["legacy_store_refused", "HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED"])
def test_real_decline_conflict_polarity_and_narrow_reentry(
    throwaway_database_url: str, tmp_path: Path, first: str,
) -> None:
    from scripts.node27_autopipeline import (
        _already_ingested_runs,
        _decline_key,
        _declined_runs,
        _record_recompute_decline,
    )

    apply_migrations_from_zero(throwaway_database_url)
    connection = psycopg2.connect(throwaway_database_url)
    try:
        _seed_authority(connection, output_uri="s3://nhms/runs/run_dual_write/output")
        with connection.cursor() as cursor:
            cursor.execute("UPDATE hydro.hydro_run SET timeseries_store='legacy' WHERE run_id=%s", (_RUN_ID,))
        connection.commit()
        product = tmp_path / "runs" / _RUN_ID / "output" / "fixture.rivqdown"
        product.parent.mkdir(parents=True)
        product.write_text("1,2\n")
        key = _decline_key(tmp_path, _RUN_ID)
        assert key is not None
        compressed = "HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED"

        def record(reason: str) -> None:
            _record_recompute_decline(throwaway_database_url, run_id=_RUN_ID,
                                      init_state_id=key[0], product_mtime=key[1], reason_code=reason, detail=reason)

        record(first)
        record(compressed if first == "legacy_store_refused" else "legacy_store_refused")
        with connection.cursor() as cursor:
            cursor.execute("SELECT reason_code FROM ops.ingest_recompute_decline WHERE run_id=%s", (_RUN_ID,))
            assert cursor.fetchone() == ("legacy_store_refused",)
            assert _declined_runs(cursor, [_RUN_ID], tmp_path) == {_RUN_ID}
            cursor.execute("UPDATE hydro.hydro_run SET timeseries_store='narrow' WHERE run_id=%s", (_RUN_ID,))
        connection.commit()
        with connection.cursor() as cursor:
            assert _declined_runs(cursor, [_RUN_ID], tmp_path) == set()
        connection.commit()
        record(compressed)
        with connection.cursor() as cursor:
            cursor.execute("SELECT reason_code FROM ops.ingest_recompute_decline WHERE run_id=%s", (_RUN_ID,))
            assert cursor.fetchone() == (compressed,)
            assert _declined_runs(cursor, [_RUN_ID], tmp_path) == {_RUN_ID}
        assert _already_ingested_runs(throwaway_database_url, [_RUN_ID], object_store_root=tmp_path) == {_RUN_ID}
        connection.commit()
        with pytest.raises(RuntimeError, match="stale"):
            record("legacy_store_refused")
        import os
        os.utime(product, (key[1] + 10, key[1] + 10))
        with connection.cursor() as cursor:
            assert _declined_runs(cursor, [_RUN_ID], tmp_path) == set()
    finally:
        connection.close()


def test_seed_database_roundtrips_exact_river_authorities_postexpand(throwaway_database_url: str) -> None:
    from db.seeds import seed_demo
    from tests.test_seed import _expected_river_seed_samples

    apply_migrations_from_zero(throwaway_database_url)
    connection = psycopg2.connect(throwaway_database_url)
    try:
        # Distinct identity domains make even cross-column key swaps visible.
        with connection.cursor() as cursor:
            for table, column, start in _IDENTITY_RESTARTS:
                cursor.execute(f"ALTER TABLE {table} ALTER COLUMN {column} RESTART WITH {start}")
        seed_demo.seed_database(connection)
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT h.run_id, rs.river_segment_id, rt.variable_e, rt.lead_time_hours,
                       rt.valid_time, rt.value, rt.unit_e, rt.quality_flag_e,
                       bv.basin_version_id, rnv.river_network_version_id,
                       h.basin_version_id, rs.river_network_version_id, h.timeseries_store
                FROM hydro.river_timeseries rt
                JOIN hydro.hydro_run h USING (run_key)
                JOIN core.river_segment rs USING (river_segment_key)
                JOIN core.basin_version bv ON bv.basin_version_key=rt.basin_version_key
                JOIN core.river_network_version rnv ON rnv.river_network_version_key=rt.river_network_version_key
            """)
            rows = cursor.fetchall()
            expected = _expected_river_seed_samples(after_met=True)
            assert len(rows) == len(expected)
            assert {row[:4]: row[4:8] for row in rows} == expected
            assert {row[8:] for row in rows} == {
                (seed_demo.BASIN_VERSION_ID, seed_demo.RIVER_NETWORK_VERSION_ID,
                 seed_demo.BASIN_VERSION_ID, seed_demo.RIVER_NETWORK_VERSION_ID, "narrow")
            }
            assert {(row[2], row[6]) for row in rows} == {("q_down", "m3/s"), ("y_stage", "m")}
    finally:
        connection.rollback()
        connection.close()
