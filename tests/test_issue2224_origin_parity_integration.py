"""Focused #2224 isolated-Timescale origin-parity proof helpers and test."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

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
    plan_rows = execute("EXPLAIN (FORMAT JSON) " + sql, (selected.range_start, selected.range_end))
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
    forbidden = {
        (chunk.hypertable_schema, chunk.hypertable_name)
        for chunk in all_chunks
    }
    for chunk in all_chunks:
        if chunk.origin_oid == selected.origin_oid:
            continue
        forbidden.add((chunk.origin_schema, chunk.origin_name))
        if chunk.compressed_schema and chunk.compressed_name:
            forbidden.add((chunk.compressed_schema, chunk.compressed_name))
    assert relation_nodes & allowed, f"plan has no selected relation: {relation_nodes}"
    assert not relation_nodes & forbidden, f"plan reads forbidden relations: {relation_nodes & forbidden}"
    assert relation_nodes <= allowed, f"plan reads unrelated relation nodes: {relation_nodes - allowed}"


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
        hypertable_schema=selected.hypertable_schema,
        hypertable_name=selected.hypertable_name,
        origin_schema=selected.origin_schema,
        origin_name=selected.origin_name,
    )
    sibling = load_catalog_chunk(
        execute,
        hypertable_schema=sibling.hypertable_schema,
        hypertable_name=sibling.hypertable_name,
        origin_schema=sibling.origin_schema,
        origin_name=sibling.origin_name,
    )
    variant = load_catalog_chunk(
        execute,
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
    # These two discriminators are intentionally separate: changing a target
    # row must change that target's checksum, while a future sibling must not
    # affect the selected compressed target or its direct-origin plan.
    future_start = variant.range_end + timedelta(days=1)
    execute(
        """
        INSERT INTO hydro.river_timeseries (
            run_id, basin_version_id, river_network_version_id, river_segment_id,
            valid_time, variable, value, unit
        )
        SELECT 'future-sibling', 'b', 'n', 's', %s + (g * interval '1 hour'), 'q_down', 9.0, 'm3/s'
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
        WHERE run_id = 'future-sibling'
          AND basin_version_id = 'b'
          AND river_network_version_id = 'n'
          AND river_segment_id = 's'
          AND valid_time = %s
          AND variable = 'q_down'
          AND value = 9.0
          AND unit = 'm3/s'
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


