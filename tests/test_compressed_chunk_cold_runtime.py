"""Unit tests for the production cold-residency runtime owner."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.compressed_chunk_cold_residency import ACCEPTED_SEQUENCE_NAME, evaluate_capacity_preflight
from packages.common.compressed_chunk_cold_runtime import (
    CommitAckLost,
    RuntimeConfig,
    inspect_residency_group,
    migrate_residency_group,
    preflight_target_identity,
    ranked_candidates,
    reconcile_named_group,
)
from packages.common.compressed_chunk_cold_runtime_catalog import (
    ColdRuntimeError,
    compute_window_parity,
    derive_bound_inventories,
    derive_hypertable_inventory,
    window_parity_sql,
)
from tests.cold_residency_fakes import (
    CUTOFF,
    LAG,
    RANGE_START,
    WATERMARK,
    FakeConnection,
    bound_inventories,
    chunk,
    complete_relations,
    forcing_columns,
    river_columns,
)


def _connect(connection: FakeConnection):
    def factory() -> FakeConnection:
        return connection

    return factory


def _loaded(connection: FakeConnection, item=None, space: str = "pg_default") -> tuple[FakeConnection, object]:
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


def test_production_runtime_does_not_import_probe_modules() -> None:
    import packages.common.compressed_chunk_cold_receipt as receipt
    import packages.common.compressed_chunk_cold_runtime as runtime
    import packages.common.compressed_chunk_cold_runtime_catalog as catalog

    for module in (runtime, catalog, receipt):
        assert "compressed_chunk_cold_probe" not in Path(module.__file__).name
        imported = getattr(module, "__name__", "")
        assert "compressed_chunk_cold_probe" not in imported
        source = "\n".join(
            line
            for line in Path(module.__file__).read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
            and '"""' not in line
            and "never imports" not in line
        )
        assert "import packages.common.compressed_chunk_cold_probe" not in source
        assert "from packages.common.compressed_chunk_cold_probe" not in source


def test_accepted_sequence_remains_shell_first() -> None:
    assert ACCEPTED_SEQUENCE_NAME == "shell_first_decompress_recompress_atomic"


def test_both_real_schema_inventories_bind_valid_time_as_timestamptz() -> None:
    connection = FakeConnection()
    inventories = derive_bound_inventories(lambda sql, params=None: connection.dispatch(sql, params)[0])
    assert [column.name for column in inventories.river.columns] == [column.name for column in river_columns()]
    assert [column.name for column in inventories.forcing.columns] == [column.name for column in forcing_columns()]
    river_valid = next(column for column in inventories.river.columns if column.name == "valid_time")
    forcing_valid = next(column for column in inventories.forcing.columns if column.name == "valid_time")
    assert river_valid.type_name == "timestamp with time zone"
    assert forcing_valid.type_name == "timestamp with time zone"
    assert inventories.digest


def test_unsupported_column_fails_closed() -> None:
    connection = FakeConnection()
    original = connection.dispatch

    def dispatch(sql: str, params: Any = None):
        rows, names = original(sql, params)
        if "FROM pg_attribute" in sql:
            rows.append(
                {
                    "attnum": 99,
                    "attname": "geom",
                    "type_name": "bytea",
                    "attnotnull": False,
                    "attidentity": "",
                    "attgenerated": "",
                    "typtype": "b",
                }
            )
        return rows, names

    connection.dispatch = dispatch  # type: ignore[method-assign]
    with pytest.raises(ColdRuntimeError, match="unsupported type"):
        derive_hypertable_inventory(
            lambda sql, params=None: connection.dispatch(sql, params)[0],
            "hydro",
            "river_timeseries",
        )


def test_inventory_drift_reordered_column_fails_closed() -> None:
    connection = FakeConnection()
    original = connection.dispatch

    def dispatch(sql: str, params: Any = None):
        rows, names = original(sql, params)
        if "FROM pg_attribute" in sql:
            rows = list(reversed(rows))
        return rows, names

    connection.dispatch = dispatch  # type: ignore[method-assign]
    with pytest.raises(ColdRuntimeError, match="attnum order"):
        derive_hypertable_inventory(
            lambda sql, params=None: connection.dispatch(sql, params)[0],
            "hydro",
            "river_timeseries",
        )


def test_window_parity_sql_covers_every_derived_column_and_half_open_window() -> None:
    inventory = bound_inventories().river
    sql = window_parity_sql(inventory)
    assert "valid_time >= %s AND valid_time < %s" in sql
    for column in inventory.columns:
        assert f'"{column.name}"' in sql


def test_window_parity_is_a_bounded_multiset_not_whole_table() -> None:
    connection = FakeConnection()
    inventory = bound_inventories().river
    connection.parity_rows = [
        {
            "row_count": 2,
            "checksum_xor": 7,
            "checksum_sum": 11,
            **{f"nn_{index}": 2 for index in range(len(inventory.columns))},
        }
    ]
    first = compute_window_parity(
        lambda sql, params=None: connection.dispatch(sql, params)[0],
        inventory,
        range_start=RANGE_START,
        range_end=CUTOFF,
    )
    connection.parity_rows = [
        {
            "row_count": 2,
            "checksum_xor": 7,
            "checksum_sum": 11,
            **{f"nn_{index}": 2 for index in range(len(inventory.columns))},
        }
    ]
    second = compute_window_parity(
        lambda sql, params=None: connection.dispatch(sql, params)[0],
        inventory,
        range_start=RANGE_START,
        range_end=CUTOFF,
    )
    assert first.checksum == second.checksum
    assert first.row_count == 2
    sql = window_parity_sql(inventory)
    assert "string_agg" not in sql.lower()
    assert " AS token" not in sql
    assert "hashtextextended" in sql
    assert "bit_xor" in sql


def test_window_parity_rejects_row_shaped_payloads() -> None:
    inventory = bound_inventories().river

    def execute(sql: str, params: Any = None):
        del sql, params
        return [{"token": "row", "valid_time": RANGE_START}]

    with pytest.raises(ColdRuntimeError, match="row-shaped"):
        compute_window_parity(execute, inventory, range_start=RANGE_START, range_end=CUTOFF)


def test_window_parity_rejects_more_than_one_aggregate_row() -> None:
    inventory = bound_inventories().river

    def execute(sql: str, params: Any = None):
        del sql, params
        row = {"row_count": 1, "checksum_xor": 1, "checksum_sum": 1}
        return [row, dict(row)]

    with pytest.raises(ColdRuntimeError, match="expected one aggregate row"):
        compute_window_parity(execute, inventory, range_start=RANGE_START, range_end=CUTOFF)


def test_capacity_equality_passes_and_one_byte_short_refuses() -> None:
    equal = evaluate_capacity_preflight(
        before_compression_total_bytes=1000,
        cold_free_bytes=1100,
        cold_reserve_bytes=100,
        hot_free_bytes=1,
        wal_reserve_bytes=1,
        retained_source_bytes=8192,
    )
    assert equal.approved is True
    assert equal.required_cold_bytes == 1100
    assert equal.required_hot_bytes == 1
    cold_short = evaluate_capacity_preflight(
        before_compression_total_bytes=1000,
        cold_free_bytes=1099,
        cold_reserve_bytes=100,
        hot_free_bytes=1,
        wal_reserve_bytes=1,
        retained_source_bytes=8192,
    )
    assert cold_short.approved is False
    hot_short = evaluate_capacity_preflight(
        before_compression_total_bytes=1000,
        cold_free_bytes=1100,
        cold_reserve_bytes=100,
        hot_free_bytes=0,
        wal_reserve_bytes=1,
        retained_source_bytes=8192,
    )
    assert hot_short.approved is False


def test_target_identity_drift_refuses_before_movement_sql() -> None:
    connection, item = _loaded(FakeConnection())
    connection.tablespace_location = "/wrong"
    with pytest.raises(ColdRuntimeError, match="identity mismatch"):
        preflight_target_identity(
            lambda sql, params=None: connection.dispatch(sql, params)[0],
            RuntimeConfig(inspect_target=_inspect_target),
        )
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)
    del item


def test_hypertable_attach_is_refused() -> None:
    connection, _item = _loaded(FakeConnection())
    connection.attached[("hydro", "river_timeseries")] = ["nhms_cold"]
    with pytest.raises(ColdRuntimeError, match="attached"):
        preflight_target_identity(
            lambda sql, params=None: connection.dispatch(sql, params)[0],
            RuntimeConfig(inspect_target=_inspect_target),
        )


def test_already_cold_is_a_no_write_observation() -> None:
    connection, item = _loaded(FakeConnection(), space="nhms_cold")
    observation = inspect_residency_group(
        connect=_connect(connection),
        chunk=item,
        inventories=bound_inventories(),
    )
    assert observation.outcome == "already_cold"
    assert observation.shell_sql_executed is False
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_mixed_group_is_blocked_not_migrated() -> None:
    connection = FakeConnection()
    item = chunk()
    connection.load_group(item, complete_relations(other_space="nhms_cold"))
    observation = inspect_residency_group(connect=_connect(connection), chunk=item, inventories=bound_inventories())
    assert observation.outcome == "blocked"
    assert observation.reconciliation == "mixed"


def test_normal_migrate_uses_shell_first_and_fresh_observer() -> None:
    connection, item = _loaded(FakeConnection())
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
        config=RuntimeConfig(inspect_target=lambda: {
            "container_name": "nhms-db",
            "container_bind": "/data/GHDC/nhms-cold-tablespace",
            "host_path": "/data/GHDC/nhms-cold-tablespace",
            "device_identity": "",
        }),
    )
    assert observation.outcome == "migrated"
    assert observation.reconciliation == "complete_target"
    sql = [statement for statement, _params in connection.executed]
    joined = "\n".join(sql)
    assert "SET LOCAL lock_timeout" in joined
    assert "SET LOCAL statement_timeout" in joined
    assert "LOCK TABLE" in joined
    share = [statement for statement in sql if "ACCESS SHARE" in statement]
    assert any("hydro" in statement and "river_timeseries" in statement for statement in share)
    assert any("met" in statement and "forcing_station_timeseries" in statement for statement in share)
    heaps = [
        idx
        for idx, statement in enumerate(sql)
        if statement.startswith("LOCK TABLE") and "ACCESS EXCLUSIVE" in statement
    ]
    parents = [idx for idx, statement in enumerate(sql) if "ACCESS SHARE" in statement]
    assert heaps and parents and max(heaps) < min(parents)
    assert "forcing_station_timeseries" in share[0]
    assert "river_timeseries" in share[1]
    assert "SET TABLESPACE" in joined
    assert "decompress_chunk" in joined
    assert "compress_chunk" in joined
    assert not any("compress_hyper_2_2_chunk" in statement and "SET TABLESPACE" in statement for statement in sql)
    assert not any("pg_toast" in statement and "SET TABLESPACE" in statement for statement in sql)


def test_production_met_before_hydro_parent_oids_lock_ascending() -> None:
    connection, item = _loaded(FakeConnection())
    assert connection.parent_oids[("met", "forcing_station_timeseries")] < connection.parent_oids[
        ("hydro", "river_timeseries")
    ]
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
    assert observation.outcome == "migrated"
    share = [statement for statement, _params in connection.executed if "ACCESS SHARE" in statement]
    assert "met" in share[0] and "hydro" in share[1]


def test_hydro_oid_before_met_still_locks_by_actual_oid() -> None:
    connection, item = _loaded(FakeConnection())
    connection.parent_oids = {
        ("hydro", "river_timeseries"): 1001,
        ("met", "forcing_station_timeseries"): 2001,
    }
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
    assert observation.outcome == "migrated"
    share = [statement for statement, _params in connection.executed if "ACCESS SHARE" in statement]
    assert "hydro" in share[0] and "met" in share[1]


def test_capacity_one_byte_short_refuses_before_movement_sql() -> None:
    connection, item = _loaded(FakeConnection())
    observation = migrate_residency_group(
        connect=_connect(connection),
        chunk=item,
        inventories=bound_inventories(),
        watermark=WATERMARK,
        lag_seconds=LAG,
        cold_free_bytes=1099,
        hot_free_bytes=10_000,
        cold_reserve_bytes=100,
        wal_reserve_bytes=1,
        config=RuntimeConfig(inspect_target=lambda: {
            "container_name": "nhms-db",
            "container_bind": "/data/GHDC/nhms-cold-tablespace",
            "host_path": "/data/GHDC/nhms-cold-tablespace",
            "device_identity": "",
        }),
    )
    assert observation.outcome == "refused"
    assert observation.shell_sql_executed is False
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_statement_timeout_rolls_back() -> None:
    connection, item = _loaded(FakeConnection())
    connection.fail_sql = "decompress_chunk"
    error = RuntimeError("canceling statement due to statement timeout")
    error.pgcode = "57014"  # type: ignore[attr-defined]
    connection.fail_exc = error
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
        config=RuntimeConfig(inspect_target=lambda: {
            "container_name": "nhms-db",
            "container_bind": "/data/GHDC/nhms-cold-tablespace",
            "host_path": "/data/GHDC/nhms-cold-tablespace",
            "device_identity": "",
        }),
    )
    assert observation.error_class == "statement_timeout"
    assert connection.rolled_back is True


def test_lock_timeout_does_not_claim_shell_sql_executed() -> None:
    connection, item = _loaded(FakeConnection())
    connection.fail_sql = "LOCK TABLE"
    error = RuntimeError("canceling statement due to lock timeout")
    error.pgcode = "55P03"  # type: ignore[attr-defined]
    connection.fail_exc = error
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
        config=RuntimeConfig(inspect_target=lambda: {
            "container_name": "nhms-db",
            "container_bind": "/data/GHDC/nhms-cold-tablespace",
            "host_path": "/data/GHDC/nhms-cold-tablespace",
            "device_identity": "",
        }),
    )
    assert observation.error_class == "lock_timeout"
    assert observation.shell_sql_executed is False
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_relation_disappearance_is_classified() -> None:
    connection, item = _loaded(FakeConnection())
    connection.fail_sql = "LOCK TABLE"
    error = RuntimeError("relation vanished")
    error.pgcode = "42P01"  # type: ignore[attr-defined]
    connection.fail_exc = error
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
        config=RuntimeConfig(inspect_target=lambda: {
            "container_name": "nhms-db",
            "container_bind": "/data/GHDC/nhms-cold-tablespace",
            "host_path": "/data/GHDC/nhms-cold-tablespace",
            "device_identity": "",
        }),
    )
    assert observation.error_class == "relation_disappeared"


def test_commit_ack_loss_reconciles_without_replay() -> None:
    connection, item = _loaded(FakeConnection())

    def lose(_conn: FakeConnection) -> None:
        raise CommitAckLost()

    connection.commit_hook = lose
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
        config=RuntimeConfig(inspect_target=lambda: {
            "container_name": "nhms-db",
            "container_bind": "/data/GHDC/nhms-cold-tablespace",
            "host_path": "/data/GHDC/nhms-cold-tablespace",
            "device_identity": "",
        }),
        lose_commit_ack=True,
    )
    assert observation.commit_ack_lost is True
    assert observation.replayed is False
    assert observation.reconciliation in {"complete_target", "complete_source", "unknown", "mixed"}


def test_ranked_candidates_interleave_by_per_hypertable_rank() -> None:
    connection = FakeConnection()
    river_old = chunk(origin_oid=10, origin_name="_hyper_1_1_chunk", range_end=CUTOFF)
    river_new = chunk(
        origin_oid=11,
        origin_name="_hyper_1_2_chunk",
        compressed_oid=21,
        compressed_name="compress_hyper_2_3_chunk",
        range_start=CUTOFF,
        range_end=CUTOFF + timedelta(days=7),
    )
    met_old = chunk(
        schema="met",
        name="forcing_station_timeseries",
        origin_oid=30,
        origin_name="_hyper_4_1_chunk",
        compressed_oid=40,
        compressed_name="compress_hyper_5_1_chunk",
        range_end=CUTOFF,
    )
    connection.load_group(river_old, complete_relations())
    connection.load_group(
        river_new,
        complete_relations(
            origin_oid=11,
            compressed_oid=21,
            origin_name="_hyper_1_2_chunk",
            compressed_name="compress_hyper_2_3_chunk",
        ),
    )
    connection.load_group(
        met_old,
        complete_relations(
            origin_oid=30,
            compressed_oid=40,
            origin_name="_hyper_4_1_chunk",
            compressed_name="compress_hyper_5_1_chunk",
        ),
    )
    ranked = ranked_candidates(
        _connect(connection),
        cutoff=CUTOFF + timedelta(days=7),
        per_table_limit=10,
        max_catalog_bytes=10_000_000,
    )
    identities = [(item[2], item[3], item[0]) for item in ranked]
    assert identities[0][0] == "hydro"
    assert identities[1][0] == "met"
    assert identities[0][2] == identities[1][2] == 0


def test_max_members_is_enforced() -> None:
    connection = FakeConnection()
    item = chunk()
    connection.load_group(item, complete_relations(extra_indexes=8))
    with pytest.raises(ColdRuntimeError, match="maximum member"):
        inspect_residency_group(
            connect=_connect(connection),
            chunk=item,
            inventories=bound_inventories(),
            config=RuntimeConfig(max_members=4),
        )


def test_reconcile_named_group_unknown_when_origin_disappears() -> None:
    connection = FakeConnection()
    observation = reconcile_named_group(
        connect=_connect(connection),
        inventories=bound_inventories(),
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        origin_schema="_timescaledb_internal",
        origin_name="_hyper_1_1_chunk",
        range_start=RANGE_START,
        range_end=CUTOFF,
        origin_oid=10,
    )
    assert observation.reconciliation == "unknown"


def test_locked_inventory_drift_refuses_before_set_tablespace() -> None:
    connection, item = _loaded(FakeConnection())
    original = connection.dispatch
    seen_lock = {"value": False}

    def dispatch(sql: str, params: Any = None):
        if sql.startswith("LOCK TABLE"):
            seen_lock["value"] = True
        rows, names = original(sql, params)
        if seen_lock["value"] and "FROM pg_attribute" in sql:
            rows = list(rows) + [
                {
                    "attnum": 99,
                    "attname": "extra_col",
                    "type_name": "text",
                    "attnotnull": False,
                    "attidentity": "",
                    "attgenerated": "",
                    "typtype": "b",
                }
            ]
        return rows, names

    connection.dispatch = dispatch  # type: ignore[method-assign]
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
    assert observation.shell_sql_executed is False
    assert observation.error_class == "inventory_drift"
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_locked_member_drift_refuses_before_set_tablespace() -> None:
    connection, item = _loaded(FakeConnection())

    def after_lock(conn: FakeConnection) -> None:
        extra = complete_relations(extra_indexes=1)[-1]
        conn.relations[extra.oid] = extra

    connection.after_lock_hook = after_lock
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
    assert observation.shell_sql_executed is False
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_locked_parity_drift_refuses_before_set_tablespace() -> None:
    connection, item = _loaded(FakeConnection())
    inventory = bound_inventories().river
    connection.parity_rows = [
        {
            "row_count": 2,
            "checksum_xor": 1,
            "checksum_sum": 3,
            **{f"nn_{index}": 2 for index in range(len(inventory.columns))},
        }
    ]
    original = connection.dispatch
    seen_lock = {"value": False}

    def dispatch(sql: str, params: Any = None):
        if sql.startswith("LOCK TABLE"):
            seen_lock["value"] = True
            if seen_lock["value"]:
                connection.parity_rows = [
                    {
                        "row_count": 3,
                        "checksum_xor": 9,
                        "checksum_sum": 12,
                        **{f"nn_{index}": 3 for index in range(len(inventory.columns))},
                    }
                ]
        return original(sql, params)

    connection.dispatch = dispatch  # type: ignore[method-assign]
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
    assert observation.error_class == "parity"
    assert observation.shell_sql_executed is False
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_reconcile_replacement_sibling_is_unknown() -> None:
    connection, item = _loaded(FakeConnection())
    inspect = inspect_residency_group(connect=_connect(connection), chunk=item, inventories=bound_inventories())
    replacement = complete_relations(compressed_oid=99, compressed_name="compress_replaced")
    connection.relations = {rel.oid: rel for rel in replacement if rel.oid != 20}
    for rel in replacement:
        connection.relations[rel.oid] = rel
    connection.chunks[item.origin_name] = chunk(compressed_oid=99, compressed_name="compress_replaced")
    observation = reconcile_named_group(
        connect=_connect(connection),
        inventories=bound_inventories(),
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        origin_schema="_timescaledb_internal",
        origin_name="_hyper_1_1_chunk",
        range_start=RANGE_START,
        range_end=CUTOFF,
        origin_oid=10,
        before=inspect.before,
        before_parity=inspect.before_parity,
    )
    assert observation.reconciliation == "unknown"


def test_reconcile_without_before_evidence_is_unknown() -> None:
    connection, item = _loaded(FakeConnection())
    observation = reconcile_named_group(
        connect=_connect(connection),
        inventories=bound_inventories(),
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        origin_schema="_timescaledb_internal",
        origin_name="_hyper_1_1_chunk",
        range_start=RANGE_START,
        range_end=CUTOFF,
        origin_oid=10,
    )
    assert observation.reconciliation == "unknown"
    del item


def test_enforce_requires_explicit_device_identity() -> None:
    connection, item = _loaded(FakeConnection())
    with pytest.raises(ColdRuntimeError, match="device identity"):
        preflight_target_identity(
            lambda sql, params=None: connection.dispatch(sql, params)[0],
            RuntimeConfig(inspect_target=_inspect_target),
            require_device_identity=True,
        )
    del item


def test_runtime_config_propagates_disposable_container_name() -> None:
    from packages.common.compressed_chunk_cold_tick import runtime_config
    from scripts.node27_cold_residency import RunnerConfig

    config = RunnerConfig(
        database_url="postgresql://unused",
        lag_seconds=604800,
        per_tick_bound=1,
        receipt_path=Path("/unused/receipt.json"),
        intent_path=Path("/unused/.receipt.json.intent"),
        lock_path=Path("/unused/runner.lock"),
        lifecycle_lock_path=Path("/unused/lifecycle.lock"),
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
        expected_container_bind="/unused/cold",
        expected_host_path="/unused/cold",
        expected_container_name="nhms-1893-isolated",
        expected_device_identity="isolated",
        inspect_target=lambda: {
            "container_name": "nhms-1893-isolated",
            "container_bind": "/unused/cold",
            "host_path": "/unused/cold",
            "device_identity": "isolated",
        },
    )
    runtime = runtime_config(config)
    assert runtime.expected_container_name == "nhms-1893-isolated"
    connection, item = _loaded(FakeConnection())
    identity = preflight_target_identity(
        lambda sql, params=None: connection.dispatch(sql, params)[0],
        runtime,
    )
    assert identity.device_identity == "isolated"
    mismatched = runtime_config(
        config.__class__(
            **{
                **config.__dict__,
                "inspect_target": lambda: {
                    "container_name": "nhms-db",
                    "container_bind": "/unused/cold",
                    "host_path": "/unused/cold",
                    "device_identity": "isolated",
                },
            }
        )
    )
    with pytest.raises(ColdRuntimeError, match="container identity drifted"):
        preflight_target_identity(
            lambda sql, params=None: connection.dispatch(sql, params)[0],
            mismatched,
        )
    del item


class _StepClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.01
        return self.value


def test_injected_clock_records_non_zero_inspect_duration() -> None:
    connection, item = _loaded(FakeConnection())
    observation = inspect_residency_group(
        connect=_connect(connection),
        chunk=item,
        inventories=bound_inventories(),
        config=RuntimeConfig(clock=_StepClock()),
    )
    assert observation.timing is not None
    assert observation.timing["total_ms"] == 10
    assert observation.timing["shell_move_ms"] is None
    assert observation.timing["decompress_ms"] is None


def test_injected_clock_records_mutation_stage_durations() -> None:
    connection, item = _loaded(FakeConnection())
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
        config=RuntimeConfig(inspect_target=_inspect_target, expected_device_identity="8:1", clock=_StepClock()),
    )
    assert observation.outcome == "migrated"
    assert observation.timing is not None
    assert observation.timing["total_ms"] > 0
    assert observation.timing["heap_lock_wait_ms"] == 10
    assert observation.timing["shell_move_ms"] == 10
    assert observation.timing["decompress_ms"] == 10
    assert observation.timing["recompress_ms"] == 10
    assert observation.timing["commit_ms"] == 10
    assert observation.timing["fresh_reconciliation_ms"] == 10


def test_post_commit_inventory_drift_is_unknown_not_complete_target() -> None:
    connection, item = _loaded(FakeConnection())
    original_commit = connection.commit

    def after_commit(owner: FakeConnection) -> None:
        original = owner.dispatch

        def drifted(sql: str, params: Any = None):
            rows, names = original(sql, params)
            if "FROM pg_attribute" in sql:
                extra = {
                    "attnum": 99,
                    "attname": "extra_col",
                    "type_name": "text",
                    "attnotnull": False,
                    "attidentity": "",
                    "attgenerated": "",
                    "typtype": "b",
                }
                rows = [*rows, extra]
            return rows, names

        owner.dispatch = drifted  # type: ignore[method-assign]

    connection.commit_hook = after_commit
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
    assert observation.reconciliation == "unknown"
    assert observation.outcome != "migrated"
    del original_commit
