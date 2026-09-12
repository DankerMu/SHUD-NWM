"""Focused #2224 isolated-Timescale origin-parity proof helpers and test."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from packages.common.compressed_chunk_cold_residency import quote_ident, quote_literal
from packages.common.compressed_chunk_cold_runtime_catalog import (
    compute_window_parity,
    load_catalog_chunk,
    load_eligible_chunks,
    window_parity_sql,
)

_CUTOFF = datetime(2026, 7, 4, tzinfo=UTC)


def _relation_nodes(value: object) -> set[tuple[str, str]]:
    """Collect exact physical relation identities from an EXPLAIN JSON value."""

    found: set[tuple[str, str]] = set()
    if isinstance(value, dict):
        schema = value.get("Schema")
        relation = value.get("Relation Name")
        if isinstance(schema, str) and isinstance(relation, str):
            found.add((schema, relation))
        for child in value.values():
            found.update(_relation_nodes(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_relation_nodes(child))
    return found


def _explain_relations(rows: list[dict[str, object]]) -> set[tuple[str, str]]:
    """Parse psycopg's JSON EXPLAIN result without relying on rendered text."""

    assert len(rows) == 1
    row = rows[0]
    assert len(row) == 1
    payload = next(iter(row.values()))
    if isinstance(payload, str):
        payload = json.loads(payload)
    return _relation_nodes(payload)


def _observe_optional_only_aggregate(execute: Any, *, sql: str, target: Any) -> dict[str, object]:
    """Keep the TS 2.10.2 ONLY comparison from deciding direct-origin validity."""

    try:
        rows = execute(sql, (target.range_start, target.range_end))
    except Exception as error:  # ONLY can be unsupported on the pinned engine.
        return {"status": "unavailable", "error_type": type(error).__name__}
    if len(rows) != 1:
        return {"status": "rejected", "row_count": len(rows)}
    return {"status": "observed", "aggregate": dict(rows[0])}


def _assert_plan_reads_only_selected_origin(
    execute: Any,
    *,
    sql: str,
    selected: Any,
    all_chunks: list[Any],
) -> None:
    plan_rows = execute("EXPLAIN (VERBOSE, FORMAT JSON) " + sql, (selected.range_start, selected.range_end))
    relation_nodes = _explain_relations(plan_rows)
    selected_origin = (selected.origin_schema, selected.origin_name)
    selected_compressed = (
        None
        if not selected.compressed_schema or not selected.compressed_name
        else (selected.compressed_schema, selected.compressed_name)
    )
    allowed = {selected_origin}
    if selected_compressed is not None:
        allowed.add(selected_compressed)
    forbidden = {(chunk.hypertable_schema, chunk.hypertable_name) for chunk in all_chunks}
    for chunk in all_chunks:
        if chunk.origin_oid == selected.origin_oid:
            continue
        forbidden.add((chunk.origin_schema, chunk.origin_name))
        if chunk.compressed_schema and chunk.compressed_name:
            forbidden.add((chunk.compressed_schema, chunk.compressed_name))
    assert relation_nodes & allowed, f"plan has no selected relation: {relation_nodes}"
    assert not relation_nodes & forbidden, f"plan reads forbidden relations: {relation_nodes & forbidden}"
    assert relation_nodes <= allowed, f"plan reads unrelated relation nodes: {relation_nodes - allowed}"


def _reload_origin(execute: Any, chunk: Any, inventory: Any) -> Any:
    return load_catalog_chunk(
        execute,
        inventory=inventory,
        hypertable_schema=chunk.hypertable_schema,
        hypertable_name=chunk.hypertable_name,
        origin_schema=chunk.origin_schema,
        origin_name=chunk.origin_name,
    )


def _decompress_update_recompress(
    execute: Any, chunk: Any, sql: str, params: tuple[object, ...], inventory: Any
) -> Any:
    origin = quote_literal(f"{chunk.origin_schema}.{chunk.origin_name}")
    execute(f"SELECT decompress_chunk({origin}::regclass)")
    execute(sql, params)
    execute(f"SELECT compress_chunk({origin}::regclass)")
    current = _reload_origin(execute, chunk, inventory)
    assert current.is_compressed is True
    assert current.compressed_oid is not None
    return current


def _first_row_time(execute: Any, run_key: int, value: float) -> object:
    rows = execute(
        """
        SELECT valid_time
        FROM hydro.river_timeseries
        WHERE run_key = %s AND value = %s
        ORDER BY valid_time
        LIMIT 1
        """,
        (run_key, value),
    )
    assert len(rows) == 1
    return rows[0]["valid_time"]


def _assert_selected_compressed_target_sensitivity(
    inventories: Any,
    execute: Any,
    *,
    selected: Any,
    sibling: Any,
    all_chunks: Any,
) -> None:
    inventory = inventories.for_hypertable(selected.hypertable_schema, selected.hypertable_name)
    selected = _reload_origin(execute, selected, inventory)
    sibling = _reload_origin(execute, sibling, inventory)
    assert selected.is_compressed is True
    assert sibling.is_compressed is True
    before = compute_window_parity(execute, inventory, selected).as_dict()
    assert before["row_count"] == 24
    direct_sql = window_parity_sql(inventory, selected)
    selected_time = _first_row_time(execute, 1, 1.0)
    selected = _decompress_update_recompress(
        execute,
        selected,
        """
        UPDATE hydro.river_timeseries
        SET value = 1.5
        WHERE run_key = 1
          AND basin_version_key = 1
          AND river_network_version_key = 1
          AND river_segment_key = 1
          AND valid_time = %s
          AND variable_e = 'q_down'
          AND value = 1.0
          AND unit_e = 'm3/s'
        """,
        (selected_time,),
        inventory,
    )
    after = compute_window_parity(execute, inventory, selected).as_dict()
    assert after["row_count"] == before["row_count"]
    assert after["non_null_counts"] == before["non_null_counts"]
    assert after["checksum"] != before["checksum"]
    selected = _decompress_update_recompress(
        execute,
        selected,
        """
        UPDATE hydro.river_timeseries
        SET value = 1.0
        WHERE run_key = 1
          AND basin_version_key = 1
          AND river_network_version_key = 1
          AND river_segment_key = 1
          AND valid_time = %s
          AND variable_e = 'q_down'
          AND value = 1.5
          AND unit_e = 'm3/s'
        """,
        (selected_time,),
        inventory,
    )
    restored = compute_window_parity(execute, inventory, selected).as_dict()
    assert restored == before
    sibling_time = _first_row_time(execute, 2, 2.0)
    sibling = _decompress_update_recompress(
        execute,
        sibling,
        """
        UPDATE hydro.river_timeseries
        SET value = 2.5
        WHERE run_key = 2
          AND basin_version_key = 1
          AND river_network_version_key = 1
          AND river_segment_key = 1
          AND valid_time = %s
          AND variable_e = 'q_down'
          AND value = 2.0
          AND unit_e = 'm3/s'
        """,
        (sibling_time,),
        inventory,
    )
    assert sibling.is_compressed is True
    assert compute_window_parity(execute, inventory, selected).as_dict() == before
    _assert_plan_reads_only_selected_origin(
        execute,
        sql=direct_sql,
        selected=selected,
        all_chunks=all_chunks(),
    )


def _assert_role_identity(execute: Any, role: str) -> None:
    rows = execute("SELECT current_user AS current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
    assert len(rows) == 1
    assert rows[0]["current_user"] == role
    assert rows[0]["rolsuper"] is False


def _sqlstate(error: BaseException) -> str | None:
    value = getattr(error, "pgcode", None)
    return str(value) if value else None


def _assert_display_deny_write(execute: Any) -> None:
    statements = (
        """
        INSERT INTO hydro.river_timeseries (
            run_key, basin_version_key, river_network_version_key, river_segment_key,
            valid_time, variable_e, value, unit_e, quality_flag_e
        ) VALUES (9, 1, 1, 1, TIMESTAMPTZ '2026-06-27 00:00:00+00', 'q_down', 0.0, 'm3/s', 'ok')
        """,
        (
            "UPDATE hydro.river_timeseries SET value = 0.0 "
            "WHERE run_key = 1 AND valid_time = TIMESTAMPTZ '2026-06-27 00:00:00+00'"
        ),
        "ALTER TABLE hydro.river_timeseries ADD COLUMN shipping_denied boolean",
    )
    for sql in statements:
        try:
            execute(sql)
        except Exception as error:
            assert _sqlstate(error) == "42501", error
        else:
            raise AssertionError(f"display role was allowed to execute {sql.strip()}")


def _assert_shipping_role_origin_parity(inventories: Any, execute: Any) -> None:
    execute("CREATE ROLE nhms_ingest_rw NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOLOGIN")
    execute("CREATE ROLE nhms_display_ro NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOLOGIN")
    execute("ALTER TABLE hydro.river_timeseries OWNER TO nhms_ingest_rw")
    execute("ALTER TABLE met.forcing_station_timeseries OWNER TO nhms_ingest_rw")
    for schema in ("hydro", "met"):
        execute(f"GRANT USAGE ON SCHEMA {quote_ident(schema)} TO nhms_ingest_rw, nhms_display_ro")
        execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {quote_ident(schema)} TO nhms_ingest_rw, nhms_display_ro")
        execute(f"GRANT INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {quote_ident(schema)} TO nhms_ingest_rw")
    candidates = load_eligible_chunks(
        execute,
        inventory=inventories.river,
        schema="hydro",
        name="river_timeseries",
        cutoff=_CUTOFF + timedelta(days=21),
        limit=8,
        max_bytes=16 * 1024**2,
    )
    selected = _reload_origin(
        execute, sorted(candidates, key=lambda item: (item.range_start, item.origin_oid))[0], inventories.river
    )
    inventory = inventories.for_hypertable(selected.hypertable_schema, selected.hypertable_name)
    original_compressed = (selected.compressed_oid, selected.compressed_schema, selected.compressed_name)
    expected = compute_window_parity(execute, inventory, selected).as_dict()
    direct_sql = window_parity_sql(inventory, selected)

    def all_hydro_chunks() -> list[Any]:
        rows = execute(
            """
            SELECT chunk_schema, chunk_name
            FROM timescaledb_information.chunks
            WHERE hypertable_schema = %s AND hypertable_name = %s
            ORDER BY chunk_schema, chunk_name
            """,
            ("hydro", "river_timeseries"),
        )
        return [
            load_catalog_chunk(
                execute,
                inventory=inventories.river,
                hypertable_schema="hydro",
                hypertable_name="river_timeseries",
                origin_schema=str(row["chunk_schema"]),
                origin_name=str(row["chunk_name"]),
            )
            for row in rows
        ]

    for role in ("nhms_ingest_rw", "nhms_display_ro"):
        execute(f"SET ROLE {quote_ident(role)}")
        try:
            _assert_role_identity(execute, role)
            current = _reload_origin(execute, selected, inventory)
            assert compute_window_parity(execute, inventory, current).as_dict() == expected
            _assert_plan_reads_only_selected_origin(
                execute,
                sql=direct_sql,
                selected=current,
                all_chunks=all_hydro_chunks(),
            )
            if role == "nhms_display_ro":
                _assert_display_deny_write(execute)
        finally:
            execute("RESET ROLE")

    origin = quote_literal(f"{selected.origin_schema}.{selected.origin_name}")
    execute(f"SELECT decompress_chunk({origin}::regclass)")
    execute(f"SELECT compress_chunk({origin}::regclass)")
    recompressed = _reload_origin(execute, selected, inventory)
    assert recompressed.is_compressed is True
    assert (
        recompressed.compressed_oid,
        recompressed.compressed_schema,
        recompressed.compressed_name,
    ) != original_compressed
    expected_after = compute_window_parity(execute, inventory, recompressed).as_dict()
    assert expected_after["row_count"] == expected["row_count"]
    for role in ("nhms_ingest_rw", "nhms_display_ro"):
        execute(f"SET ROLE {quote_ident(role)}")
        try:
            _assert_role_identity(execute, role)
            current = _reload_origin(execute, recompressed, inventory)
            assert compute_window_parity(execute, inventory, current).as_dict() == expected_after
            assert current.compressed_oid == recompressed.compressed_oid
        finally:
            execute("RESET ROLE")


def test_optional_only_observation_does_not_propagate_errors_or_empty_results() -> None:
    target = SimpleNamespace(range_start=_CUTOFF - timedelta(days=7), range_end=_CUTOFF)

    def raises(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("ONLY unsupported")

    assert _observe_optional_only_aggregate(raises, sql="SELECT 1", target=target) == {
        "status": "unavailable",
        "error_type": "RuntimeError",
    }
    assert _observe_optional_only_aggregate(lambda *_args, **_kwargs: [], sql="SELECT 1", target=target) == {
        "status": "rejected",
        "row_count": 0,
    }


def test_plan_proof_uses_verbose_explain_and_rejects_unqualified_timescale_2102_nodes() -> None:
    selected = SimpleNamespace(
        origin_oid=10,
        origin_schema="_timescaledb_internal",
        origin_name="selected_origin",
        compressed_schema="_timescaledb_internal",
        compressed_name="selected_compressed",
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        range_start=_CUTOFF - timedelta(days=7),
        range_end=_CUTOFF,
    )
    non_verbose_plan = [
        {
            "QUERY PLAN": [
                {
                    "Plan": {
                        "Node Type": "Custom Scan",
                        "Custom Plan Provider": "DecompressChunk",
                        "Relation Name": "selected_origin",
                        "Plans": [
                            {
                                "Node Type": "Seq Scan",
                                "Relation Name": "selected_compressed",
                            }
                        ],
                    }
                }
            ]
        }
    ]
    calls: list[tuple[str, tuple[object, object]]] = []

    def execute(statement: str, parameters: tuple[object, object]) -> list[dict[str, object]]:
        calls.append((statement, parameters))
        return non_verbose_plan

    with pytest.raises(AssertionError, match=r"plan has no selected relation: set\(\)"):
        _assert_plan_reads_only_selected_origin(
            execute,
            sql="SELECT 1",
            selected=selected,
            all_chunks=[selected],
        )

    assert _explain_relations(non_verbose_plan) == set()
    assert calls == [
        (
            "EXPLAIN (VERBOSE, FORMAT JSON) SELECT 1",
            (selected.range_start, selected.range_end),
        )
    ]


def test_plan_proof_rejects_an_unknown_relation_not_returned_by_all_chunks() -> None:
    selected = SimpleNamespace(
        origin_oid=10,
        origin_schema="_timescaledb_internal",
        origin_name="selected_origin",
        compressed_schema="_timescaledb_internal",
        compressed_name="selected_compressed",
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        range_start=_CUTOFF - timedelta(days=7),
        range_end=_CUTOFF,
    )
    plan = [
        {
            "QUERY PLAN": [
                {
                    "Plan": {
                        "Node Type": "Append",
                        "Plans": [
                            {
                                "Node Type": "Seq Scan",
                                "Schema": "_timescaledb_internal",
                                "Relation Name": "selected_origin",
                            },
                            {
                                "Node Type": "Seq Scan",
                                "Schema": "public",
                                "Relation Name": "unknown_relation",
                            },
                        ],
                    }
                }
            ]
        }
    ]

    with pytest.raises(AssertionError, match="unrelated relation nodes"):
        _assert_plan_reads_only_selected_origin(
            lambda *_args, **_kwargs: plan,
            sql="SELECT 1",
            selected=selected,
            all_chunks=[selected],
        )


def test_plan_proof_rejects_a_recursive_unrelated_third_chunk() -> None:
    selected = SimpleNamespace(
        origin_oid=10,
        origin_schema="_timescaledb_internal",
        origin_name="selected_origin",
        compressed_schema="_timescaledb_internal",
        compressed_name="selected_compressed",
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        range_start=_CUTOFF - timedelta(days=7),
        range_end=_CUTOFF,
    )
    sibling = SimpleNamespace(
        origin_oid=11,
        origin_schema="_timescaledb_internal",
        origin_name="sibling_origin",
        compressed_schema="_timescaledb_internal",
        compressed_name="sibling_compressed",
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
    )
    third = SimpleNamespace(
        origin_oid=12,
        origin_schema="_timescaledb_internal",
        origin_name="third_origin",
        compressed_schema="_timescaledb_internal",
        compressed_name="third_compressed",
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
    )
    plan = [
        {
            "QUERY PLAN": [
                {
                    "Plan": {
                        "Node Type": "Append",
                        "Plans": [
                            {
                                "Node Type": "Seq Scan",
                                "Schema": "_timescaledb_internal",
                                "Relation Name": "selected_origin",
                            },
                            {
                                "Node Type": "Seq Scan",
                                "Schema": "_timescaledb_internal",
                                "Relation Name": "third_origin",
                            },
                        ],
                    }
                }
            ]
        }
    ]

    with pytest.raises(AssertionError, match="forbidden relations"):
        _assert_plan_reads_only_selected_origin(
            lambda *_args, **_kwargs: plan,
            sql="SELECT 1",
            selected=selected,
            all_chunks=[selected, sibling, third],
        )


def _assert_origin_parity_discriminator(inventories: Any, execute: Any) -> None:
    """Prove production parity uses direct transparent origin reads, not ONLY."""

    candidates = load_eligible_chunks(
        execute,
        inventory=inventories.river,
        schema="hydro",
        name="river_timeseries",
        cutoff=_CUTOFF + timedelta(days=21),
        limit=8,
        max_bytes=16 * 1024**2,
    )
    assert len(candidates) >= 3
    selected, sibling, variant = sorted(candidates, key=lambda item: (item.range_start, item.origin_oid))[:3]
    selected = load_catalog_chunk(
        execute,
        inventory=inventories.river,
        hypertable_schema=selected.hypertable_schema,
        hypertable_name=selected.hypertable_name,
        origin_schema=selected.origin_schema,
        origin_name=selected.origin_name,
    )
    sibling = load_catalog_chunk(
        execute,
        inventory=inventories.river,
        hypertable_schema=sibling.hypertable_schema,
        hypertable_name=sibling.hypertable_name,
        origin_schema=sibling.origin_schema,
        origin_name=sibling.origin_name,
    )
    variant = load_catalog_chunk(
        execute,
        inventory=inventories.river,
        hypertable_schema=variant.hypertable_schema,
        hypertable_name=variant.hypertable_name,
        origin_schema=variant.origin_schema,
        origin_name=variant.origin_name,
    )

    def all_hydro_chunks() -> list[Any]:
        rows = execute(
            """
            SELECT chunk_schema, chunk_name
            FROM timescaledb_information.chunks
            WHERE hypertable_schema = %s AND hypertable_name = %s
            ORDER BY chunk_schema, chunk_name
            """,
            ("hydro", "river_timeseries"),
        )
        return [
            load_catalog_chunk(
                execute,
                inventory=inventories.river,
                hypertable_schema="hydro",
                hypertable_name="river_timeseries",
                origin_schema=str(row["chunk_schema"]),
                origin_name=str(row["chunk_name"]),
            )
            for row in rows
        ]

    inventory = inventories.for_hypertable(selected.hypertable_schema, selected.hypertable_name)
    direct_sql = window_parity_sql(inventory, selected)
    only_sql = direct_sql.replace(" FROM ", " FROM ONLY ", 1)
    assert only_sql != direct_sql
    assert " FROM ONLY " not in direct_sql

    def aggregate(sql: str, target: object) -> dict[str, object]:
        rows = execute(sql, (target.range_start, target.range_end))
        assert len(rows) == 1
        return dict(rows[0])

    direct_before = aggregate(direct_sql, selected)
    production_before = compute_window_parity(execute, inventory, selected).as_dict()
    assert production_before["row_count"] == 24
    assert direct_before["row_count"] == 24
    assert direct_before["row_count"] == production_before["row_count"]
    assert direct_before["origin_oid_matches"] is True
    only_observation = _observe_optional_only_aggregate(execute, sql=only_sql, target=selected)
    only_before = only_observation.get("aggregate")
    if only_observation["status"] == "observed" and isinstance(only_before, dict):
        if only_before["row_count"] == production_before["row_count"]:
            assert direct_before == only_before
    assert " FROM ONLY " not in direct_sql

    sibling_parity = compute_window_parity(execute, inventory, sibling).as_dict()
    assert sibling_parity["row_count"] == 7200
    assert sibling_parity != production_before

    variant_parity = compute_window_parity(execute, inventory, variant).as_dict()
    assert variant_parity["row_count"] == production_before["row_count"] == 24
    assert variant_parity["non_null_counts"] == production_before["non_null_counts"]
    assert variant_parity["checksum"] != production_before["checksum"]

    _assert_plan_reads_only_selected_origin(
        execute,
        sql=direct_sql,
        selected=selected,
        all_chunks=all_hydro_chunks(),
    )
    _assert_selected_compressed_target_sensitivity(
        inventories,
        execute,
        selected=selected,
        sibling=sibling,
        all_chunks=all_hydro_chunks,
    )
    selected = _reload_origin(execute, selected, inventory)
    production_before = compute_window_parity(execute, inventory, selected).as_dict()
    # These two discriminators are intentionally separate: changing a target
    # row must change that target's checksum, while a future sibling must not
    # affect the selected compressed target or its direct-origin plan.
    future_start = variant.range_end + timedelta(days=1)
    execute(
        """
        INSERT INTO hydro.river_timeseries (
            run_key, basin_version_key, river_network_version_key, river_segment_key,
            valid_time, variable_e, value, unit_e, quality_flag_e
        )
        SELECT 4, 1, 1, 1, %s + (g * interval '1 hour'), 'q_down', 9.0, 'm3/s', 'ok'
        FROM generate_series(0, 23) AS g
        """,
        (future_start,),
    )
    future_rows = all_hydro_chunks()
    future = next(chunk for chunk in future_rows if chunk.range_start <= future_start < chunk.range_end)
    assert future.is_compressed is False
    assert (future.compressed_oid, future.compressed_schema, future.compressed_name) == (None, None, None)
    future_before = compute_window_parity(execute, inventory, future).as_dict()
    assert future_before["row_count"] == 24
    execute(
        """
        UPDATE hydro.river_timeseries
        SET value = 10.0
        WHERE run_key = 4
          AND basin_version_key = 1
          AND river_network_version_key = 1
          AND river_segment_key = 1
          AND valid_time = %s
          AND variable_e = 'q_down'
          AND value = 9.0
          AND unit_e = 'm3/s'
        """,
        (future_start,),
    )
    future_after = compute_window_parity(execute, inventory, future).as_dict()
    assert future_after["row_count"] == future_before["row_count"] == 24
    assert future_after["non_null_counts"] == future_before["non_null_counts"]
    assert future_after["checksum"] != future_before["checksum"]
    assert compute_window_parity(execute, inventory, selected).as_dict() == production_before
    _assert_plan_reads_only_selected_origin(
        execute,
        sql=direct_sql,
        selected=selected,
        all_chunks=future_rows,
    )
