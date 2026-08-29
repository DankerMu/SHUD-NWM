"""Catalog loading, residency snapshots, and unit-plan helpers."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from packages.common.compressed_chunk_cold_probe.cluster import connect, execute, scalar
from packages.common.compressed_chunk_cold_probe.fixture_parity import fixture_window_parity_sql
from packages.common.compressed_chunk_cold_probe.types import (
    CHUNK_INFO_SQL,
    COMPRESSED_SIBLING_SQL,
    INDEX_OIDS_SQL,
    RELATION_OID_SQL,
    RELATION_SQL,
    WAL_LIMITATION,
    ProbeConfig,
    ProbeError,
)
from packages.common.compressed_chunk_cold_residency import (
    ACCEPTED_SEQUENCE_NAME,
    COLD_TABLESPACE_NAME,
    REJECTED_SEQUENCE_NAMES,
    SOURCE_TABLESPACE_NAME,
    CatalogChunk,
    CatalogRelation,
    ResidencyGroup,
    ShellFirstPlan,
    build_shell_first_plan,
    classify_reconciliation,
    classify_residency,
    evaluate_capacity_preflight,
    move_chunk_candidate_sql,
    origin_shell_is_not_complete,
    resolve_residency_group,
    snapshot_image_identity,
)


def _synthetic_relations(tablespace: str) -> tuple[CatalogRelation, ...]:
    internal = "_timescaledb_internal"
    return (
        CatalogRelation(10, internal, "_hyper_1_1_chunk", "r", tablespace, 8192, toast_oid=30),
        CatalogRelation(15, internal, "10_23_river_timeseries_pkey", "i", tablespace, 8192, heap_oid=10),
        CatalogRelation(16, internal, "10_probe_extra_idx", "i", tablespace, 8192, heap_oid=10),
        CatalogRelation(20, internal, "compress_hyper_2_2_chunk", "r", tablespace, 4096, toast_oid=40),
        CatalogRelation(25, internal, "compress_hyper_2_2_chunk_idx", "i", tablespace, 8192, heap_oid=20),
        CatalogRelation(30, "pg_toast", "pg_toast_10", "t", tablespace, 16384),
        CatalogRelation(31, "pg_toast", "pg_toast_10_index", "i", tablespace, 8192, heap_oid=30),
        CatalogRelation(40, "pg_toast", "pg_toast_20", "t", tablespace, 8192),
        CatalogRelation(41, "pg_toast", "pg_toast_20_index", "i", tablespace, 8192, heap_oid=40),
    )


def synthetic_complete_group(*, tablespace: str = SOURCE_TABLESPACE_NAME) -> ResidencyGroup:
    chunk = CatalogChunk(
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        origin_oid=10,
        origin_schema="_timescaledb_internal",
        origin_name="_hyper_1_1_chunk",
        compressed_oid=20,
        compressed_schema="_timescaledb_internal",
        compressed_name="compress_hyper_2_2_chunk",
        range_start=datetime(2026, 6, 27, 12, tzinfo=UTC),
        range_end=datetime(2026, 7, 4, 12, tzinfo=UTC),
        is_compressed=True,
    )
    group = resolve_residency_group(chunk, _synthetic_relations(tablespace))
    if group.blocker:
        raise ProbeError(group.blocker)
    return group


def unit_plan_report() -> dict[str, Any]:
    group = synthetic_complete_group()
    plan = build_shell_first_plan(group)
    already = build_shell_first_plan(synthetic_complete_group(tablespace=COLD_TABLESPACE_NAME))
    sql = [*plan.prefix_sql, *plan.lock_sql, *plan.shell_move_sql]
    if plan.decompress_sql:
        sql.append(plan.decompress_sql)
    if plan.compress_sql:
        sql.append(plan.compress_sql)
    sql.append("COMMIT")
    return {
        "mode": "unit-plan",
        "accepted_sequence": ACCEPTED_SEQUENCE_NAME,
        "rejected_sequences": sorted(REJECTED_SEQUENCE_NAMES),
        "lock_oids": list(plan.lock_oids),
        "shell_move_oids": list(plan.shell_move_oids),
        "sql": sql,
        "already_cold_sql_empty": already.shell_move_sql == (),
        "status": "passed",
        "move_chunk_sql": move_chunk_candidate_sql("_timescaledb_internal._hyper_1_1_chunk"),
        "image": dict(snapshot_image_identity()),
    }


def _aware(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise ProbeError(f"timestamp expected, got {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _relation_oid(connection: Any, schema: str, name: str) -> int | None:
    value = scalar(connection, RELATION_OID_SQL, (schema, name))
    return None if value is None else int(value)


def load_chunks(connection: Any) -> list[CatalogChunk]:
    chunks: list[CatalogChunk] = []
    for row in execute(connection, CHUNK_INFO_SQL):
        origin_oid = _relation_oid(connection, row["chunk_schema"], row["chunk_name"])
        if origin_oid is None:
            raise ProbeError(f"origin oid missing for {row['chunk_schema']}.{row['chunk_name']}")
        sibling = execute(connection, COMPRESSED_SIBLING_SQL, (row["chunk_schema"], row["chunk_name"]))
        compressed_schema = sibling[0]["schema_name"] if sibling else None
        compressed_name = sibling[0]["table_name"] if sibling else None
        compressed_oid = None
        if compressed_schema and compressed_name:
            compressed_oid = _relation_oid(connection, compressed_schema, compressed_name)
        chunks.append(
            CatalogChunk(
                hypertable_schema=row["hypertable_schema"],
                hypertable_name=row["hypertable_name"],
                origin_oid=origin_oid,
                origin_schema=row["chunk_schema"],
                origin_name=row["chunk_name"],
                compressed_oid=compressed_oid,
                compressed_schema=compressed_schema,
                compressed_name=compressed_name,
                range_start=_aware(row["range_start"]),
                range_end=_aware(row["range_end"]),
                is_compressed=bool(row["is_compressed"]),
            )
        )
    return chunks


def load_relations(connection: Any, oids: Sequence[int]) -> list[CatalogRelation]:
    if not oids:
        return []
    return [
        CatalogRelation(
            oid=int(row["oid"]),
            schema=row["schema"],
            name=row["name"],
            relkind=row["relkind"],
            tablespace=row["tablespace"],
            bytes=int(row["bytes"]),
            toast_oid=None if row["toast_oid"] is None else int(row["toast_oid"]),
            heap_oid=None if row["heap_oid"] is None else int(row["heap_oid"]),
        )
        for row in execute(connection, RELATION_SQL, (list(oids),))
    ]


def collect_group(connection: Any, chunk: CatalogChunk) -> ResidencyGroup:
    current = next(
        (item for item in load_chunks(connection) if item.origin_oid == chunk.origin_oid),
        chunk,
    )
    oids = {current.origin_oid}
    if current.compressed_oid:
        oids.add(current.compressed_oid)
    relations = load_relations(connection, sorted(oids))
    toast = [rel.toast_oid for rel in relations if rel.toast_oid]
    if toast:
        relations.extend(load_relations(connection, toast))
    heap_oids = [rel.oid for rel in relations if rel.relkind in {"r", "t"}]
    if heap_oids:
        index_oids = [int(row["indexrelid"]) for row in execute(connection, INDEX_OIDS_SQL, (heap_oids,))]
        if index_oids:
            relations.extend(load_relations(connection, index_oids))
    unique: dict[int, CatalogRelation] = {rel.oid: rel for rel in relations}
    return resolve_residency_group(current, tuple(unique.values()))


def members_payload(group: ResidencyGroup) -> list[dict[str, Any]]:
    return [
        {
            "kind": member.kind,
            "oid": member.oid,
            "schema": member.schema,
            "name": member.name,
            "relkind": member.relkind,
            "tablespace": member.tablespace,
            "bytes": member.bytes,
        }
        for member in group.members
    ]


def bytes_by_space(group: ResidencyGroup) -> dict[str, int]:
    totals: dict[str, int] = {SOURCE_TABLESPACE_NAME: 0, COLD_TABLESPACE_NAME: 0}
    for member in group.members:
        totals[member.tablespace] = totals.get(member.tablespace, 0) + member.bytes
    return totals


def snapshot_group(group: ResidencyGroup) -> dict[str, Any]:
    return {
        "durable": {
            "hypertable": f"{group.hypertable_schema}.{group.hypertable_name}",
            "origin_oid": group.origin_oid,
            "origin": f"{group.origin_schema}.{group.origin_name}",
            "range_start": group.range_start.isoformat().replace("+00:00", "Z"),
            "range_end": group.range_end.isoformat().replace("+00:00", "Z"),
        },
        "compressed": None
        if group.compressed_oid is None
        else {
            "oid": group.compressed_oid,
            "schema": group.compressed_schema,
            "name": group.compressed_name,
        },
        "is_compressed": group.is_compressed,
        "residency": classify_residency(group.members),
        "bytes_by_tablespace": bytes_by_space(group),
        "blocker": group.blocker,
        "members": members_payload(group),
        "origin_shell_incomplete": origin_shell_is_not_complete(group),
    }


def require_migrate_plan(group: ResidencyGroup, *, target: str) -> ShellFirstPlan:
    plan = build_shell_first_plan(group, target=target)
    if plan.kind != "migrate" or not plan.shell_move_sql:
        raise ProbeError(
            f"required migrate plan for target {target!r} is {plan.kind} "
            f"with {len(plan.shell_move_sql)} shell-move statements"
        )
    return plan


def parity(connection: Any, chunk: CatalogChunk) -> dict[str, Any]:
    sql = fixture_window_parity_sql(chunk.hypertable_schema, chunk.hypertable_name)
    row = execute(connection, sql, (chunk.range_start, chunk.range_end))[0]
    return {
        "count": int(row["n"]),
        "value_sum": float(row["value_sum"]),
        "checksum": row["checksum"],
        "range_start": chunk.range_start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "range_end": chunk.range_end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


def wal_lsn(connection: Any) -> dict[str, Any]:
    lsn = scalar(connection, "SELECT pg_current_wal_lsn()::text")
    try:
        delta = scalar(connection, "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint")
    except Exception as error:  # noqa: BLE001
        delta = f"unavailable:{type(error).__name__}"
    if hasattr(delta, "__int__") and not isinstance(delta, bool):
        try:
            delta = int(delta)
        except (TypeError, ValueError):
            delta = str(delta)
    return {"lsn": lsn, "bytes_from_zero": delta, "limitation": WAL_LIMITATION}


def sibling_identity(group: ResidencyGroup) -> dict[str, Any] | None:
    if group.compressed_oid is None:
        return None
    return {
        "oid": group.compressed_oid,
        "schema": group.compressed_schema,
        "name": group.compressed_name,
    }


def fresh_observer(
    config: ProbeConfig,
    before: ResidencyGroup,
    chunk: CatalogChunk,
    before_parity: Mapping[str, Any] | None,
    *,
    source: str = SOURCE_TABLESPACE_NAME,
    target: str = COLD_TABLESPACE_NAME,
) -> dict[str, Any]:
    fresh = connect(config)
    try:
        after_chunk = reload_chunk(fresh, chunk)
        after = collect_group(fresh, after_chunk)
        after_parity = parity(fresh, after_chunk)
        recon = classify_reconciliation(
            before,
            after,
            before_parity=before_parity,
            after_parity=after_parity,
            source=source,
            target=target,
        )
        return {
            "after": after,
            "after_snapshot": snapshot_group(after),
            "after_parity": after_parity,
            "reconciliation": recon,
            "original_sibling": sibling_identity(after) == sibling_identity(before),
        }
    finally:
        fresh.close()


def try_sql(connection: Any, sql: str, params: Any = None) -> dict[str, Any]:
    started = time.monotonic()
    try:
        rows = execute(connection, sql, params)
        return {"ok": True, "sql": sql, "ms": round((time.monotonic() - started) * 1000, 3), "rows": rows}
    except Exception as error:
        try:
            connection.rollback()
        except Exception:
            pass
        return {
            "ok": False,
            "sql": sql,
            "ms": round((time.monotonic() - started) * 1000, 3),
            "error_type": type(error).__name__,
            "error": str(error).split("\n")[0],
        }


def reload_chunk(connection: Any, chunk: CatalogChunk) -> CatalogChunk:
    for item in load_chunks(connection):
        if item.origin_oid == chunk.origin_oid:
            return item
    return chunk


def _as_capacity_decision(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "approved") and hasattr(value, "as_dict"):
        return value
    if isinstance(value, Mapping):
        return evaluate_capacity_preflight(
            before_compression_total_bytes=value["before_compression_total_bytes"],
            cold_free_bytes=value["cold_free_bytes"],
            cold_reserve_bytes=value["cold_reserve_bytes"],
            hot_free_bytes=value["hot_free_bytes"],
            wal_reserve_bytes=value["wal_reserve_bytes"],
            retained_source_bytes=value["retained_source_bytes"],
        )
    raise ProbeError("capacity decision is not well-formed")


def compression_stats(connection: Any, chunk: CatalogChunk) -> dict[str, Any]:
    rows = execute(
        connection,
        """
        SELECT before_compression_total_bytes::bigint AS before_compression_total_bytes
        FROM chunk_compression_stats(%s::regclass)
        WHERE chunk_schema = %s AND chunk_name = %s
        """,
        (
            f"{chunk.hypertable_schema}.{chunk.hypertable_name}",
            chunk.origin_schema,
            chunk.origin_name,
        ),
    )
    if not rows or rows[0]["before_compression_total_bytes"] is None:
        raise ProbeError(
            f"chunk_compression_stats missing before_compression_total_bytes for {chunk.origin_name}"
        )
    return {"before_compression_total_bytes": int(rows[0]["before_compression_total_bytes"])}


def retained_source_bytes(group: ResidencyGroup) -> int:
    return int(sum(member.bytes for member in group.members))
