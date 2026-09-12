"""Focused #2224 tests for durable origin-relation parity."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.compressed_chunk_cold_receipt import validate_receipt
from packages.common.compressed_chunk_cold_residency import CatalogChunk, ColdResidencyError
from packages.common.compressed_chunk_cold_runtime import (
    RuntimeConfig,
    inspect_residency_group,
    migrate_residency_group,
    reconcile_named_group,
)
from packages.common.compressed_chunk_cold_runtime_catalog import (
    ColdRuntimeError,
    compute_window_parity,
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
    expected_exec_identity,
    parity_aggregate,
    target_observation,
)
from tests.cold_residency_identity_mutants import (
    FIRST_RELOAD_DRIFT_CASES,
    apply_first_reload_replacement,
    first_reload_mutation_sql,
)

_MISSING = object()


def _connect(connection: FakeConnection):
    def factory() -> FakeConnection:
        return connection

    return factory


def _loaded(connection: FakeConnection) -> tuple[FakeConnection, CatalogChunk]:
    item = chunk()
    connection.load_group(item, complete_relations())
    return connection, item


def _runtime(**overrides: Any) -> RuntimeConfig:
    return RuntimeConfig(**{**expected_exec_identity(), **overrides})


def _inspect_target() -> dict[str, Any]:
    return target_observation()


@pytest.mark.parametrize(
    ("is_compressed", "overrides", "message"),
    (
        (
            True,
            {"compressed_oid": None, "compressed_schema": None, "compressed_name": None},
            "compressed sibling",
        ),
        (
            False,
            {"compressed_oid": 20, "compressed_schema": "_timescaledb_internal", "compressed_name": "sibling"},
            "uncompressed",
        ),
        (True, {"compressed_oid": True}, "compressed sibling OID"),
        (1, {}, "is_compressed"),
        (True, {"compressed_oid": 0}, "compressed sibling OID"),
        (True, {"compressed_schema": " \t"}, "compressed sibling schema"),
        (True, {"compressed_name": "\n"}, "compressed sibling name"),
        (True, {"compressed_oid": 10}, "aliases the origin"),
        (
            True,
            {"compressed_schema": "_timescaledb_internal", "compressed_name": "_hyper_1_1_chunk"},
            "aliases the origin",
        ),
    ),
    ids=(
        "compressed-all-none",
        "uncompressed-full-triple",
        "compressed-bool-oid",
        "nonboolean-compression-state",
        "compressed-zero-oid",
        "compressed-blank-schema",
        "compressed-blank-name",
        "compressed-origin-oid",
        "compressed-origin-name",
    ),
)
def test_catalog_chunk_enforces_compression_sibling_invariant(
    is_compressed: object, overrides: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "hypertable_schema": "hydro",
        "hypertable_name": "river_timeseries",
        "origin_oid": 10,
        "origin_schema": "_timescaledb_internal",
        "origin_name": "_hyper_1_1_chunk",
        "compressed_oid": 20,
        "compressed_schema": "_timescaledb_internal",
        "compressed_name": "compress_hyper_2_2_chunk",
        "range_start": RANGE_START,
        "range_end": CUTOFF,
        "is_compressed": is_compressed,
    }
    values.update(overrides)

    with pytest.raises(ColdResidencyError, match=message):
        CatalogChunk(**values)  # type: ignore[arg-type]


def test_catalog_chunk_keeps_legal_uncompressed_chunks_all_none() -> None:
    target = CatalogChunk(
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        origin_oid=10,
        origin_schema="_timescaledb_internal",
        origin_name="_hyper_1_1_chunk",
        compressed_oid=None,
        compressed_schema=None,
        compressed_name=None,
        range_start=RANGE_START,
        range_end=CUTOFF,
        is_compressed=False,
    )

    assert target.is_compressed is False
    assert (target.compressed_oid, target.compressed_schema, target.compressed_name) == (None, None, None)


def test_window_parity_aggregate_binds_quoted_origin_oid_in_its_one_execution() -> None:
    inventory = bound_inventories().river
    target = CatalogChunk(
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        origin_oid=10,
        origin_schema='odd"origin',
        origin_name="chunk'name",
        compressed_oid=20,
        compressed_schema="_timescaledb_internal",
        compressed_name="compress_hyper_2_2_chunk",
        range_start=RANGE_START,
        range_end=CUTOFF,
        is_compressed=True,
    )
    calls: list[tuple[str, object]] = []

    def execute(sql: str, params: object = None):
        calls.append((sql, params))
        return [parity_aggregate(inventory, origin_oid_matches=True)]

    parity = compute_window_parity(execute, inventory, target)

    assert parity.row_count == 2
    assert len(calls) == 1
    sql, params = calls[0]
    assert params == (RANGE_START, CUTOFF)
    assert 'FROM "odd""origin"."chunk\'name"' in sql
    assert "to_regclass('\"odd\"\"origin\".\"chunk''name\"')::oid = 10::oid AS origin_oid_matches" in sql
    assert "valid_time >= %s AND valid_time < %s" in sql


@pytest.mark.parametrize(
    "origin_oid_matches",
    (False, None, 1, "true", _MISSING),
    ids=("false", "null", "integer", "text", "missing"),
)
def test_window_parity_refuses_nonexact_origin_oid_binding(origin_oid_matches: object) -> None:
    inventory = bound_inventories().river
    row = parity_aggregate(inventory, origin_oid_matches=True)
    if origin_oid_matches is _MISSING:
        del row["origin_oid_matches"]
    else:
        row["origin_oid_matches"] = origin_oid_matches

    with pytest.raises(ColdRuntimeError, match="origin OID binding"):
        compute_window_parity(lambda _sql, _params=None: [row], inventory, chunk())


def test_window_parity_rejects_zero_width_origin_window() -> None:
    target = replace(chunk(), range_start=CUTOFF, range_end=CUTOFF)

    with pytest.raises(ColdRuntimeError, match="window"):
        window_parity_sql(bound_inventories().river, target)


def test_window_parity_rejects_the_other_allowlisted_parent_as_origin() -> None:
    target = replace(chunk(), origin_schema="met", origin_name="forcing_station_timeseries")

    with pytest.raises(ColdRuntimeError, match="allowlisted parent"):
        window_parity_sql(bound_inventories().river, target)


def test_catalog_chunk_rejects_partial_compressed_sibling_identity() -> None:
    with pytest.raises(ColdResidencyError, match="compressed sibling identity"):
        CatalogChunk(
            "hydro",
            "river_timeseries",
            10,
            "_timescaledb_internal",
            "_hyper_1_1_chunk",
            None,
            "_timescaledb_internal",
            "compress_hyper_2_2_chunk",
            RANGE_START,
            CUTOFF,
            True,
        )


@pytest.mark.parametrize(
    "sibling_result",
    (
        [],
        [{"schema_name": None, "table_name": None}],
        [
            {"schema_name": "_timescaledb_internal", "table_name": "one"},
            {"schema_name": "_timescaledb_internal", "table_name": "two"},
        ],
    ),
    ids=("missing", "all-none", "ambiguous"),
)
def test_catalog_load_refuses_compressed_chunk_without_resolved_sibling(
    sibling_result: list[dict[str, object]],
) -> None:
    connection, item = _loaded(FakeConnection())
    original = connection.dispatch

    def dispatch(sql: str, params: Any = None):
        if "_timescaledb_catalog.chunk AS origin" in sql and params == (item.origin_schema, item.origin_name):
            return sibling_result, ["schema_name", "table_name"]
        return original(sql, params)

    connection.dispatch = dispatch  # type: ignore[method-assign]
    from packages.common.compressed_chunk_cold_runtime_catalog import load_catalog_chunk

    with pytest.raises(ColdRuntimeError, match="compressed sibling identity"):
        load_catalog_chunk(
            lambda sql, params=None: connection.dispatch(sql, params)[0],
            hypertable_schema=item.hypertable_schema,
            hypertable_name=item.hypertable_name,
            origin_schema=item.origin_schema,
            origin_name=item.origin_name,
        )


def test_catalog_load_refuses_compressed_sibling_name_without_resolved_oid() -> None:
    connection, item = _loaded(FakeConnection())
    original = connection.dispatch

    def dispatch(sql: str, params: Any = None):
        if "pg_class c" in sql and "c.relname = %s" in sql and params == (
            item.compressed_schema,
            item.compressed_name,
        ):
            return [], ["oid"]
        return original(sql, params)

    connection.dispatch = dispatch  # type: ignore[method-assign]
    from packages.common.compressed_chunk_cold_runtime_catalog import load_catalog_chunk

    with pytest.raises(ColdRuntimeError, match="compressed sibling identity"):
        load_catalog_chunk(
            lambda sql, params=None: connection.dispatch(sql, params)[0],
            hypertable_schema=item.hypertable_schema,
            hypertable_name=item.hypertable_name,
            origin_schema=item.origin_schema,
            origin_name=item.origin_name,
        )


def test_window_parity_requires_one_current_durable_origin_chunk() -> None:
    inventory = bound_inventories().river
    target = chunk()

    with pytest.raises(TypeError):
        window_parity_sql(inventory)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        compute_window_parity(lambda *_args, **_kwargs: [], inventory)  # type: ignore[call-arg]

    sql = window_parity_sql(inventory, target)
    assert 'FROM "_timescaledb_internal"."_hyper_1_1_chunk"' in sql


def test_window_parity_quotes_the_durable_origin_not_the_parent_or_sibling() -> None:
    inventory = bound_inventories().river
    target = CatalogChunk(
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        origin_oid=10,
        origin_schema='odd"origin',
        origin_name='chunk"name',
        compressed_oid=20,
        compressed_schema="_timescaledb_internal",
        compressed_name="compress_hyper_2_2_chunk",
        range_start=RANGE_START,
        range_end=CUTOFF,
        is_compressed=True,
    )

    sql = window_parity_sql(inventory, target)

    assert 'FROM "odd""origin"."chunk""name"' in sql
    assert 'FROM "hydro"."river_timeseries"' not in sql
    assert "compress_hyper_2_2_chunk" not in sql


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"origin_schema": ""}, "origin schema"),
        ({"origin_name": ""}, "origin name"),
        ({"origin_schema": "hydro", "origin_name": "river_timeseries"}, "allowlisted parent"),
        ({"origin_oid": 0}, "origin OID"),
        ({"range_start": CUTOFF + timedelta(days=1), "range_end": RANGE_START}, "window"),
    ),
)
def test_window_parity_rejects_invalid_durable_origin_identity(overrides: dict[str, object], message: str) -> None:
    target = replace(chunk(), **overrides)

    with pytest.raises(ColdRuntimeError, match=message):
        window_parity_sql(bound_inventories().river, target)


def test_window_parity_rejects_hypertable_mismatch_before_query() -> None:
    target = chunk(schema="met", name="forcing_station_timeseries")

    with pytest.raises(ColdRuntimeError, match="hypertable"):
        window_parity_sql(bound_inventories().river, target)


def test_window_parity_propagates_selected_origin_disappearance() -> None:
    inventory = bound_inventories().river
    error = RuntimeError("relation vanished")
    error.pgcode = "42P01"  # type: ignore[attr-defined]

    def execute(_sql: str, _params: Any = None):
        raise error

    with pytest.raises(RuntimeError) as raised:
        compute_window_parity(execute, inventory, chunk())
    assert raised.value is error


def test_locked_origin_name_drift_refuses_before_set_tablespace() -> None:
    connection, item = _loaded(FakeConnection())

    def after_lock(conn: FakeConnection) -> None:
        current = conn.chunks[item.origin_name]
        conn.chunks[item.origin_name] = CatalogChunk(
            current.hypertable_schema,
            current.hypertable_name,
            current.origin_oid,
            current.origin_schema,
            "_hyper_1_renamed_chunk",
            current.compressed_oid,
            current.compressed_schema,
            current.compressed_name,
            current.range_start,
            current.range_end,
            current.is_compressed,
        )

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
        config=_runtime(inspect_target=_inspect_target, expected_device_identity="8:1"),
    )
    assert observation.error_class in {"selection_race", "relation_disappeared"}
    assert observation.shell_sql_executed is False
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_migrate_non_selection_preflight_error_still_raises_when_expected_before_is_absent() -> None:
    connection, item = _loaded(FakeConnection())
    connection.tablespace_location = "/wrong"
    with pytest.raises(ColdRuntimeError, match="identity mismatch") as raised:
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
            expected_before=None,
            config=_runtime(inspect_target=_inspect_target, expected_device_identity="8:1"),
        )
    assert raised.value.error_class == "target_identity"
    assert not any("SET TABLESPACE" in sql for sql, _params in connection.executed)


def test_reconcile_origin_name_drift_is_unknown() -> None:
    connection, item = _loaded(FakeConnection())
    inspect = inspect_residency_group(connect=_connect(connection), chunk=item, inventories=bound_inventories())
    replacement = CatalogChunk(
        item.hypertable_schema,
        item.hypertable_name,
        item.origin_oid,
        item.origin_schema,
        "_hyper_1_renamed_chunk",
        item.compressed_oid,
        item.compressed_schema,
        item.compressed_name,
        item.range_start,
        item.range_end,
        item.is_compressed,
    )
    del connection.chunks[item.origin_name]
    connection.chunks[replacement.origin_name] = replacement
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


def _assert_selection_race_before_mutation(observation, connection: FakeConnection) -> None:
    assert observation.error_class == "selection_race"
    assert observation.shell_sql_executed is False
    assert observation.outcome not in {"planned", "already_cold", "migrated"}
    executed = first_reload_mutation_sql(connection)
    assert not executed, executed


@pytest.mark.parametrize("kind", FIRST_RELOAD_DRIFT_CASES)
def test_inspect_refuses_first_reload_identity_replacement_before_group_or_parity(kind: str) -> None:
    connection, selected = _loaded(FakeConnection())
    apply_first_reload_replacement(connection, selected, kind)
    observation = inspect_residency_group(
        connect=_connect(connection),
        chunk=selected,
        inventories=bound_inventories(),
    )
    _assert_selection_race_before_mutation(observation, connection)
    assert observation.before.origin_oid == selected.origin_oid
    assert observation.before.range_start == selected.range_start
    assert observation.before.range_end == selected.range_end
    assert observation.before.compressed_oid == selected.compressed_oid


@pytest.mark.parametrize("kind", FIRST_RELOAD_DRIFT_CASES)
def test_migrate_refuses_first_reload_identity_replacement_before_movement(kind: str) -> None:
    connection, selected = _loaded(FakeConnection())
    apply_first_reload_replacement(connection, selected, kind)
    observation = migrate_residency_group(
        connect=_connect(connection),
        chunk=selected,
        inventories=bound_inventories(),
        watermark=WATERMARK,
        lag_seconds=LAG,
        cold_free_bytes=10_000,
        hot_free_bytes=10_000,
        cold_reserve_bytes=100,
        wal_reserve_bytes=1,
        config=_runtime(inspect_target=_inspect_target, expected_device_identity="8:1"),
    )
    _assert_selection_race_before_mutation(observation, connection)


def test_receipt_validation_refuses_outer_durable_oid_mismatch_with_before_snapshot() -> None:
    example_path = (
        Path(__file__).resolve().parents[1]
        / "schemas/examples/timeseries_cold_residency_receipt.intent.example.json"
    )
    payload = json.loads(example_path.read_text(encoding="utf-8"))
    selected = payload["selected"][0]
    before_durable = selected["before"]["durable"]
    assert selected["durable"]["origin_oid"] != 11
    assert before_durable["origin_oid"] == selected["durable"]["origin_oid"]
    before_durable["origin_oid"] = 11
    assert selected["durable"]["origin_oid"] != before_durable["origin_oid"]
    with pytest.raises(Exception, match="durable|before|identity"):
        validate_receipt(payload)
