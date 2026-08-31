"""Transaction-proof and off-pin engine tests for the production runtime owner.

These tests exercise the intermediate/terminal group-completeness proofs and
the pinned-engine refusal through the public runtime boundary. They are split
from ``test_compressed_chunk_cold_runtime.py`` only for the 1000-line file
guard; the fakes and runtime helpers they consume are shared.
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.common.compressed_chunk_cold_runtime import RuntimeConfig, migrate_residency_group
from packages.common.compressed_chunk_cold_runtime_catalog import ColdRuntimeError
from tests.cold_residency_fakes import (
    LAG,
    WATERMARK,
    FakeConnection,
    bound_inventories,
    chunk,
    complete_relations,
)


def _connect(connection: FakeConnection):
    def factory() -> FakeConnection:
        return connection

    return factory


def _loaded(connection: FakeConnection, item=None, space: str = "pg_default") -> tuple[FakeConnection, Any]:
    current = item or chunk()
    connection.load_group(current, complete_relations(origin_space=space))
    return connection, current


def _inspect_target() -> dict[str, str]:
    return {
        "container_name": "nhms-db",
        "container_bind": "/data/GHDC/nhms-cold-tablespace",
        "host_path": "/data/GHDC/nhms-cold-tablespace",
        "device_identity": "8:1",
    }


def test_off_pin_postgres_refuses_before_movement() -> None:
    connection, item = _loaded(FakeConnection())
    connection.server_version = "16.0"
    with pytest.raises(ColdRuntimeError, match="PostgreSQL version") as raised:
        migrate_residency_group(
            connect=_connect(connection),
            chunk=item,
            inventories=bound_inventories(),
            watermark=WATERMARK,
            lag_seconds=LAG,
            cold_free_bytes=10_000,
            hot_free_bytes=10_000,
            cold_reserve_bytes=100,
            wal_reserve_bytes=1,
            config=RuntimeConfig(inspect_target=_inspect_target, expected_device_identity="8:1"),
        )
    assert raised.value.error_class == "engine_identity"
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_off_pin_timescaledb_refuses_before_movement() -> None:
    connection, item = _loaded(FakeConnection())
    connection.timescaledb_version = "2.11.0"
    with pytest.raises(ColdRuntimeError, match="TimescaleDB version") as raised:
        migrate_residency_group(
            connect=_connect(connection),
            chunk=item,
            inventories=bound_inventories(),
            watermark=WATERMARK,
            lag_seconds=LAG,
            cold_free_bytes=10_000,
            hot_free_bytes=10_000,
            cold_reserve_bytes=100,
            wal_reserve_bytes=1,
            config=RuntimeConfig(inspect_target=_inspect_target, expected_device_identity="8:1"),
        )
    assert raised.value.error_class == "engine_identity"
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_blocked_post_decompress_group_rolls_back_before_compress() -> None:
    connection, item = _loaded(FakeConnection())

    def blocked_after_decompress(conn: FakeConnection) -> None:
        current = conn.chunks[item.origin_name]
        keep = {
            oid: relation
            for oid, relation in conn.relations.items()
            if relation.oid == current.origin_oid or relation.heap_oid == current.origin_oid
        }
        conn.relations = keep
        conn.chunks[item.origin_name] = current.__class__(
            current.hypertable_schema,
            current.hypertable_name,
            current.origin_oid,
            current.origin_schema,
            current.origin_name,
            None,
            None,
            None,
            current.range_start,
            current.range_end,
            False,
        )

    connection.after_decompress_hook = blocked_after_decompress
    observation = migrate_residency_group(
        connect=_connect(connection),
        chunk=item,
        inventories=bound_inventories(),
        watermark=WATERMARK,
        lag_seconds=LAG,
        cold_free_bytes=10_000,
        hot_free_bytes=10_000,
        cold_reserve_bytes=100,
        wal_reserve_bytes=1,
        config=RuntimeConfig(inspect_target=_inspect_target, expected_device_identity="8:1"),
    )
    assert observation.shell_sql_executed is True
    assert observation.outcome != "migrated"
    assert connection.rolled_back is True
    assert not any(
        "compress_chunk" in sql and "decompress_chunk" not in sql
        for sql, _params in connection.executed
    )


def test_all_cold_origin_only_post_recompress_rolls_back_before_commit() -> None:
    connection, item = _loaded(FakeConnection())

    def drop_compressed_sibling(conn: FakeConnection) -> None:
        current = conn.chunks[item.origin_name]
        keep = {
            oid: relation
            for oid, relation in conn.relations.items()
            if relation.oid == current.origin_oid
            or relation.heap_oid == current.origin_oid
            or relation.oid == current.origin_oid * 100 + 20
            or relation.heap_oid == current.origin_oid * 100 + 20
        }
        conn.relations = keep
        conn.chunks[item.origin_name] = current.__class__(
            current.hypertable_schema,
            current.hypertable_name,
            current.origin_oid,
            current.origin_schema,
            current.origin_name,
            None,
            None,
            None,
            current.range_start,
            current.range_end,
            False,
        )
        for oid, relation in list(conn.relations.items()):
            conn.relations[oid] = relation.__class__(
                relation.oid,
                relation.schema,
                relation.name,
                relation.relkind,
                "nhms_cold",
                relation.bytes,
                relation.toast_oid,
                relation.heap_oid,
            )

    connection.after_recompress_hook = drop_compressed_sibling
    observation = migrate_residency_group(
        connect=_connect(connection),
        chunk=item,
        inventories=bound_inventories(),
        watermark=WATERMARK,
        lag_seconds=LAG,
        cold_free_bytes=10_000,
        hot_free_bytes=10_000,
        cold_reserve_bytes=100,
        wal_reserve_bytes=1,
        config=RuntimeConfig(inspect_target=_inspect_target, expected_device_identity="8:1"),
    )
    assert any(
        "compress_chunk" in sql and "decompress_chunk" not in sql
        for sql, _params in connection.executed
    )
    assert connection.committed is False
    assert connection.rolled_back is True
    assert observation.outcome != "migrated"
