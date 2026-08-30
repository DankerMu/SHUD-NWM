"""Opt-in isolated PG 15.2 / TimescaleDB 2.10.2 integration for production runtime."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.compressed_chunk_cold_receipt import sidecar_status, validate_receipt
from packages.common.compressed_chunk_cold_residency import (
    ACCEPTED_SEQUENCE_NAME,
    COLD_TABLESPACE_NAME,
    LIVE_CONTAINER_NAME,
    LIVE_PGDATA,
    LIVE_PORT,
    SOURCE_TABLESPACE_NAME,
    classify_residency,
    refuse_live_identity,
)
from packages.common.compressed_chunk_cold_runtime import (
    RuntimeConfig,
    inspect_residency_group,
    migrate_residency_group,
)
from packages.common.compressed_chunk_cold_runtime_catalog import derive_bound_inventories, load_eligible_chunks
from packages.common.compressed_chunk_cold_tick import run_tick
from scripts.node27_cold_residency import RunnerConfig

_WATERMARK = datetime(2026, 7, 11, tzinfo=UTC)
_CUTOFF = datetime(2026, 7, 4, tzinfo=UTC)
_LAG = 604800
_HEAD = "a" * 40


@pytest.mark.integration
@pytest.mark.timescaledb_210
def test_integration_refuses_live_cluster_identity() -> None:
    with pytest.raises(Exception, match="live"):
        refuse_live_identity(container_name=LIVE_CONTAINER_NAME, host_port=LIVE_PORT, pgdata=LIVE_PGDATA)


@pytest.mark.integration
@pytest.mark.timescaledb_210
def test_isolated_cluster_production_runtime_not_probe_executor() -> None:
    pytest.importorskip("psycopg2")
    if not Path("/.dockerenv").exists() and not Path("/var/run/docker.sock").exists():
        pytest.skip("isolated-cluster production runtime requires Docker on the node-27 oracle")

    from packages.common.compressed_chunk_cold_probe.cluster import (
        cleanup_owned,
        connect,
        docker_run_argv,
        inspect_live_image,
        prepare_work_root,
        wait_for_port,
        wait_for_sql,
    )
    from packages.common.compressed_chunk_cold_probe.cluster import (
        config_from_args as probe_config_from_args,
    )
    from packages.common.compressed_chunk_cold_probe.shell import bootstrap_extension
    from packages.common.compressed_chunk_cold_probe.types import ProbeError
    from scripts import probe_compressed_chunk_cold_tablespace as probe

    args = probe.parse_args(["--mode", "isolated-cluster", "--host-port", "55496"])
    config = probe_config_from_args(args)
    refuse_live_identity(
        container_name=config.container_name,
        host_port=config.host_port,
        pgdata=str(config.work_root / "pgdata"),
        extra_paths=[str(config.work_root)],
    )
    owned = prepare_work_root(config)
    try:
        inspect_live_image(config.docker_bin)
        run = probe._run(docker_run_argv(config), timeout=60)
        if run.returncode != 0:
            raise ProbeError(run.stderr)
        owned.created_container = True
        wait_for_port("127.0.0.1", config.host_port)
        wait_for_sql(config)
        connection = connect(config)
        try:
            bootstrap_extension(connection)
            _bootstrap_production_shaped_schema(connection)
            inventories = derive_bound_inventories(lambda sql, params=None: _execute(connection, sql, params))
            river_chunks = load_eligible_chunks(
                lambda sql, params=None: _execute(connection, sql, params),
                schema="hydro",
                name="river_timeseries",
                cutoff=_CUTOFF,
                limit=10,
                max_bytes=16 * 1024**2,
            )
            assert river_chunks
            river = river_chunks[0]
            runtime = _runtime_config(config)
            ordinary = _connect_factory(config)
            inspected = inspect_residency_group(connect=ordinary, chunk=river, inventories=inventories, config=runtime)
            assert inspected.outcome == "planned"
            assert inspected.before.compressed_oid is not None
            original_oid = inspected.before.compressed_oid
            original_name = inspected.before.compressed_name
            before_snapshot = {
                "oid": original_oid,
                "name": original_name,
                "parity": inspected.before_parity.as_dict() if inspected.before_parity is not None else None,
            }
            assert before_snapshot["parity"] is not None
            rolled = migrate_residency_group(
                connect=_fault_moving_connect(config),
                chunk=river,
                inventories=inventories,
                watermark=_WATERMARK,
                lag_seconds=_LAG,
                cold_free_bytes=10**12,
                hot_free_bytes=10**12,
                cold_reserve_bytes=1,
                wal_reserve_bytes=1,
                config=runtime,
            )
            assert rolled.shell_sql_executed is True
            assert rolled.outcome == "rolled_back"
            assert rolled.reconciliation == "complete_source"
            assert rolled.after is not None
            assert rolled.after_parity is not None
            assert rolled.after_parity.as_dict() == before_snapshot["parity"]
            assert rolled.after.compressed_oid == original_oid
            assert rolled.after.compressed_name == original_name
            assert classify_residency(rolled.after.members) == "all_source"
            assert all(
                (member.tablespace or SOURCE_TABLESPACE_NAME) == SOURCE_TABLESPACE_NAME
                for member in rolled.after.members
            )

            migrated = migrate_residency_group(
                connect=ordinary,
                chunk=river,
                inventories=inventories,
                watermark=_WATERMARK,
                lag_seconds=_LAG,
                cold_free_bytes=10**12,
                hot_free_bytes=10**12,
                cold_reserve_bytes=1,
                wal_reserve_bytes=1,
                config=runtime,
            )
            assert migrated.outcome == "migrated"
            assert migrated.reconciliation == "complete_target"
            assert migrated.after is not None
            assert classify_residency(migrated.after.members) == "already_target"
            assert all(member.tablespace == COLD_TABLESPACE_NAME for member in migrated.after.members)
            assert migrated.after_parity is not None
            assert migrated.after_parity.as_dict() == before_snapshot["parity"]
            assert migrated.after.compressed_oid != original_oid
            assert migrated.after.compressed_name != original_name
            again = migrate_residency_group(
                connect=ordinary,
                chunk=river,
                inventories=inventories,
                watermark=_WATERMARK,
                lag_seconds=_LAG,
                cold_free_bytes=10**12,
                hot_free_bytes=10**12,
                cold_reserve_bytes=1,
                wal_reserve_bytes=1,
                config=runtime,
            )
            assert again.outcome == "already_cold"
            assert again.shell_sql_executed is False
            assert ACCEPTED_SEQUENCE_NAME == "shell_first_decompress_recompress_atomic"

            work = Path(config.work_root)
            progress: dict[str, Any] = {}

            def after_group_progress(_rank: int, _payload: object) -> None:
                intent_path = work / ".receipt.json.intent"
                receipt_path = work / "receipt.json"
                intent = json.loads(intent_path.read_text(encoding="utf-8"))
                public = json.loads(receipt_path.read_text(encoding="utf-8"))
                validate_receipt(intent)
                validate_receipt(public)
                assert intent["outcome"] == "in_progress"
                assert public["outcome"] == "in_progress"
                assert stat.S_ISREG(os.lstat(intent_path).st_mode)
                assert stat.S_IMODE(os.lstat(intent_path).st_mode) == 0o600
                forcing_items = [
                    item
                    for item in intent["selected"]
                    if item.get("durable", {}).get("hypertable_schema") == "met"
                ]
                assert forcing_items
                forcing = forcing_items[0]
                assert isinstance(forcing.get("before"), dict)
                assert isinstance(forcing.get("before_parity"), dict)
                assert isinstance(forcing.get("capacity"), dict)
                assert forcing["outcome"] == "migrated"
                progress["intent"] = intent
                progress["receipt"] = public

            tick_config = RunnerConfig(
                database_url="postgresql://unused",
                lag_seconds=_LAG,
                per_tick_bound=1,
                receipt_path=work / "receipt.json",
                intent_path=work / ".receipt.json.intent",
                lock_path=work / "runner.lock",
                lifecycle_lock_path=work / "lifecycle.lock",
                enforce=True,
                statement_timeout_ms=3600000,
                wrapper_wall_seconds=3901,
                compression_wrapper_wall_seconds=3900,
                systemd_wall_seconds=7842,
                cold_reserve_bytes=1,
                wal_reserve_bytes=1,
                max_members=64,
                lock_timeout="30s",
                expected_catalog_location="/home/postgres/pgdata/tablespaces/nhms_cold",
                expected_container_bind=str(work / "cold"),
                expected_host_path=str(work / "cold"),
                expected_container_name=config.container_name,
                expected_device_identity="isolated",
                inspect_target=lambda: {
                    "container_name": config.container_name,
                    "container_bind": str(work / "cold"),
                    "host_path": str(work / "cold"),
                    "device_identity": "isolated",
                },
                cold_free_bytes=10**12,
                hot_free_bytes=10**12,
                after_group_progress=after_group_progress,
            )
            tick_receipt = run_tick(
                tick_config,
                now_utc=_WATERMARK,
                head_sha=_HEAD,
                connect=ordinary,
                fetch_watermark=lambda: _WATERMARK,
                attributed_connect=lambda *_a, **_k: connect(config, autocommit=False),
                application_name="nhms-ts-cold-residency",
                cleanup_margin_seconds=300,
                systemd_margin_seconds=40,
                max_catalog_rows=10,
                max_catalog_bytes=16 * 1024**2,
            )
            assert progress
            validate_receipt(tick_receipt)
            assert tick_receipt["outcome"] == "clean"
            forcing_final = [
                item
                for item in tick_receipt["selected"]
                if item.get("durable", {}).get("hypertable_schema") == "met"
            ]
            assert forcing_final
            assert forcing_final[0]["outcome"] == "migrated"
            assert forcing_final[0]["reconciliation"] == "complete_target"
            river_selected = [
                item
                for item in tick_receipt["selected"]
                if item.get("durable", {}).get("hypertable_schema") == "hydro"
            ]
            if river_selected:
                assert river_selected[0]["outcome"] == "already_cold"
            assert forcing_final, "already-cold river must not consume the mutation bound"
            validate_receipt(json.loads((work / "receipt.json").read_text(encoding="utf-8")))
            assert sidecar_status(work / ".receipt.json.intent") == "absent"
        finally:
            connection.close()
    finally:
        proof = cleanup_owned(owned, docker_bin=config.docker_bin)
        assert proof["identity_bound"] is True
        if owned.created_container:
            assert proof["created_container"] is True
            assert proof["container_absent"] is True
        if owned.created_work_root:
            assert proof["work_root_absent"] is True


def _runtime_config(config: Any) -> RuntimeConfig:
    cold = str(config.work_root / "cold")
    return RuntimeConfig(
        expected_catalog_location="/home/postgres/pgdata/tablespaces/nhms_cold",
        expected_container_bind=cold,
        expected_host_path=cold,
        expected_container_name=config.container_name,
        inspect_target=lambda: {
            "container_name": config.container_name,
            "container_bind": cold,
            "host_path": cold,
            "device_identity": "isolated",
        },
        expected_device_identity="isolated",
    )


def _connect_factory(config: Any):
    from packages.common.compressed_chunk_cold_probe.cluster import connect

    def factory() -> Any:
        return connect(config, autocommit=False)

    return factory


def _fault_moving_connect(config: Any):
    from packages.common.compressed_chunk_cold_probe.cluster import connect

    seen = {"n": 0}

    def factory() -> Any:
        seen["n"] += 1
        connection = connect(config, autocommit=False)
        if seen["n"] == 2:
            return _RecompressFaultConnection(connection)
        return connection

    return factory


class _RecompressFaultCursor:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def execute(self, sql: str, params: Any = None) -> Any:
        text = sql if isinstance(sql, str) else str(sql)
        if "compress_chunk" in text and "decompress_chunk" not in text:
            raise RuntimeError("injected recompress refusal")
        return self._inner.execute(sql, params)

    def __enter__(self) -> _RecompressFaultCursor:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: object) -> Any:
        return self._inner.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _RecompressFaultConnection:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def cursor(self, *args: Any, **kwargs: Any) -> _RecompressFaultCursor:
        return _RecompressFaultCursor(self._inner.cursor(*args, **kwargs))

    def close(self) -> None:
        self._inner.close()

    def commit(self) -> None:
        self._inner.commit()

    def rollback(self) -> None:
        self._inner.rollback()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _execute(connection: object, sql: str, params: object = None) -> list[dict[str, object]]:
    from packages.common.compressed_chunk_cold_probe.cluster import execute

    return execute(connection, sql, params)


def _bootstrap_production_shaped_schema(connection: object) -> None:
    from packages.common.compressed_chunk_cold_probe.cluster import execute
    from packages.common.compressed_chunk_cold_residency import quote_ident, quote_literal

    execute(
        connection,
        "CREATE TABLESPACE "
        + quote_ident(COLD_TABLESPACE_NAME)
        + " LOCATION "
        + quote_literal("/home/postgres/pgdata/tablespaces/nhms_cold"),
    )
    execute(connection, "CREATE SCHEMA hydro")
    execute(connection, "CREATE SCHEMA met")
    execute(connection, "CREATE TYPE hydro.river_variable AS ENUM ('q_down', 'y_stage')")
    execute(connection, "CREATE TYPE hydro.river_unit AS ENUM ('m3/s', 'm')")
    execute(connection, "CREATE TYPE hydro.river_quality_flag AS ENUM ('ok', 'qc_warning')")
    execute(
        connection,
        """
        CREATE TABLE hydro.river_timeseries (
          run_id TEXT NOT NULL,
          basin_version_id TEXT NOT NULL,
          river_network_version_id TEXT NOT NULL,
          river_segment_id TEXT NOT NULL,
          valid_time TIMESTAMPTZ NOT NULL,
          lead_time_hours INT,
          variable TEXT NOT NULL,
          value DOUBLE PRECISION NOT NULL,
          unit TEXT NOT NULL,
          quality_flag TEXT NOT NULL DEFAULT 'ok',
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          run_key INTEGER,
          river_network_version_key INTEGER,
          basin_version_key INTEGER,
          river_segment_key INTEGER,
          variable_e hydro.river_variable,
          unit_e hydro.river_unit,
          quality_flag_e hydro.river_quality_flag,
          PRIMARY KEY (run_id, river_network_version_id, river_segment_id, variable, valid_time)
        )
        """,
    )
    execute(
        connection,
        """
        CREATE TABLE met.forcing_station_timeseries (
          forcing_version_id TEXT NOT NULL,
          basin_version_id TEXT NOT NULL,
          station_id TEXT NOT NULL,
          valid_time TIMESTAMPTZ NOT NULL,
          source_id TEXT NOT NULL,
          variable TEXT NOT NULL,
          value DOUBLE PRECISION NOT NULL,
          unit TEXT NOT NULL,
          native_resolution TEXT,
          quality_flag TEXT NOT NULL DEFAULT 'ok',
          PRIMARY KEY (forcing_version_id, station_id, variable, valid_time)
        )
        """,
    )
    compression_settings = {
        ("hydro", "river_timeseries"): (
            "timescaledb.compress = true, "
            "timescaledb.compress_segmentby = 'run_id, river_network_version_id, river_segment_id', "
            "timescaledb.compress_orderby = 'variable, valid_time'"
        ),
        ("met", "forcing_station_timeseries"): (
            "timescaledb.compress = true, "
            "timescaledb.compress_segmentby = 'forcing_version_id, station_id', "
            "timescaledb.compress_orderby = 'variable, valid_time'"
        ),
    }
    migration = (
        Path(__file__).resolve().parents[1] / "db/migrations/000047_hypertable_compression_settings.sql"
    ).read_text(encoding="utf-8")
    assert "timescaledb.compress_segmentby = 'run_id, river_network_version_id, river_segment_id'" in migration
    assert "timescaledb.compress_segmentby = 'forcing_version_id, station_id'" in migration
    assert migration.count("timescaledb.compress_orderby = 'variable, valid_time'") == 2
    for (schema, table), options in compression_settings.items():
        execute(
            connection,
            "SELECT create_hypertable(%s, 'valid_time', chunk_time_interval => interval '7 days')",
            (f"{schema}.{table}",),
        )
        execute(connection, f"ALTER TABLE {schema}.{table} SET ({options})")
    start = datetime(2026, 6, 27, tzinfo=UTC)
    execute(
        connection,
        """
        INSERT INTO hydro.river_timeseries (
            run_id, basin_version_id, river_network_version_id, river_segment_id,
            valid_time, variable, value, unit
        )
        SELECT 'run', 'b', 'n', 's', %s + (g * interval '1 hour'), 'q_down', 1.0, 'm3/s'
        FROM generate_series(0, 23) g
        """,
        (start,),
    )
    execute(
        connection,
        """
        INSERT INTO met.forcing_station_timeseries (
            forcing_version_id, basin_version_id, station_id, valid_time, source_id, variable, value, unit
        )
        SELECT 'fv', 'b', 'st', %s + (g * interval '1 hour'), 'gfs', 'tmp', 1.0, 'K'
        FROM generate_series(0, 23) g
        """,
        (start,),
    )
    older = start + timedelta(days=7)
    execute(
        connection,
        "SELECT compress_chunk(show_chunks('hydro.river_timeseries', older_than => %s))",
        (older,),
    )
    execute(
        connection,
        "SELECT compress_chunk(show_chunks('met.forcing_station_timeseries', older_than => %s))",
        (older,),
    )
