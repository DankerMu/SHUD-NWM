"""Shared fake catalog/runtime helpers for #1893 unit tests."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from packages.common.compressed_chunk_cold_residency import CatalogChunk, CatalogRelation
from packages.common.compressed_chunk_cold_runtime_catalog import (
    BoundInventories,
    ColumnDescriptor,
    HypertableInventory,
    _inventory_digest,
)

WATERMARK = datetime(2026, 7, 11, 12, tzinfo=UTC)
LAG = 604800
CUTOFF = datetime(2026, 7, 4, 12, tzinfo=UTC)
RANGE_START = datetime(2026, 6, 27, 12, tzinfo=UTC)


def river_columns() -> tuple[ColumnDescriptor, ...]:
    names = (
        "run_id",
        "basin_version_id",
        "river_network_version_id",
        "river_segment_id",
        "valid_time",
        "lead_time_hours",
        "variable",
        "value",
        "unit",
        "quality_flag",
        "created_at",
        "run_key",
        "river_network_version_key",
        "basin_version_key",
        "river_segment_key",
        "variable_e",
        "unit_e",
        "quality_flag_e",
    )
    types = {
        "valid_time": "timestamp with time zone",
        "created_at": "timestamp with time zone",
        "lead_time_hours": "integer",
        "value": "double precision",
        "run_key": "integer",
        "river_network_version_key": "integer",
        "basin_version_key": "integer",
        "river_segment_key": "integer",
        "variable_e": "hydro.river_variable",
        "unit_e": "hydro.river_unit",
        "quality_flag_e": "hydro.river_quality_flag",
    }
    nullable = {
        "lead_time_hours",
        "run_key",
        "river_network_version_key",
        "basin_version_key",
        "river_segment_key",
        "variable_e",
        "unit_e",
        "quality_flag_e",
    }
    return tuple(
        ColumnDescriptor(
            attnum=attnum,
            name=name,
            type_name=types.get(name, "text"),
            not_null=name not in nullable,
            identity="",
            generated="",
        )
        for attnum, name in enumerate(names, start=1)
    )


def forcing_columns() -> tuple[ColumnDescriptor, ...]:
    names = (
        "forcing_version_id",
        "basin_version_id",
        "station_id",
        "valid_time",
        "source_id",
        "variable",
        "value",
        "unit",
        "native_resolution",
        "quality_flag",
    )
    types = {"valid_time": "timestamp with time zone", "value": "double precision"}
    nullable = {"native_resolution"}
    return tuple(
        ColumnDescriptor(
            attnum=attnum,
            name=name,
            type_name=types.get(name, "text"),
            not_null=name not in nullable,
            identity="",
            generated="",
        )
        for attnum, name in enumerate(names, start=1)
    )


def inventory_for(schema: str, name: str, columns: Sequence[ColumnDescriptor]) -> HypertableInventory:
    return HypertableInventory(schema=schema, name=name, columns=tuple(columns), digest=_inventory_digest(columns))


def bound_inventories() -> BoundInventories:
    river = inventory_for("hydro", "river_timeseries", river_columns())
    forcing = inventory_for("met", "forcing_station_timeseries", forcing_columns())
    digest = hashlib.sha256(f"{river.digest}:{forcing.digest}".encode("utf-8")).hexdigest()
    return BoundInventories(river=river, forcing=forcing, digest=digest)


def chunk(
    *,
    schema: str = "hydro",
    name: str = "river_timeseries",
    origin_oid: int = 10,
    origin_name: str = "_hyper_1_1_chunk",
    compressed_oid: int | None = 20,
    compressed_name: str | None = "compress_hyper_2_2_chunk",
    range_start: datetime = RANGE_START,
    range_end: datetime = CUTOFF,
    is_compressed: bool = True,
) -> CatalogChunk:
    return CatalogChunk(
        hypertable_schema=schema,
        hypertable_name=name,
        origin_oid=origin_oid,
        origin_schema="_timescaledb_internal",
        origin_name=origin_name,
        compressed_oid=compressed_oid,
        compressed_schema=None if compressed_oid is None else "_timescaledb_internal",
        compressed_name=compressed_name,
        range_start=range_start,
        range_end=range_end,
        is_compressed=is_compressed,
    )


def rel(
    oid: int,
    schema: str,
    name: str,
    relkind: str,
    tablespace: str,
    nbytes: int,
    *,
    toast_oid: int | None = None,
    heap_oid: int | None = None,
) -> CatalogRelation:
    return CatalogRelation(oid, schema, name, relkind, tablespace, nbytes, toast_oid, heap_oid)


def complete_relations(
    *,
    origin_oid: int = 10,
    compressed_oid: int = 20,
    origin_name: str = "_hyper_1_1_chunk",
    compressed_name: str = "compress_hyper_2_2_chunk",
    origin_space: str = "pg_default",
    other_space: str | None = None,
    extra_indexes: int = 0,
) -> tuple[CatalogRelation, ...]:
    compressed_space = other_space or origin_space
    internal = "_timescaledb_internal"
    members = [
        rel(origin_oid, internal, origin_name, "r", origin_space, 8192, toast_oid=origin_oid + 20),
        rel(origin_oid + 5, internal, f"{origin_oid}_pkey", "i", origin_space, 16384, heap_oid=origin_oid),
        rel(compressed_oid, internal, compressed_name, "r", compressed_space, 4096, toast_oid=compressed_oid + 20),
        rel(
            compressed_oid + 5,
            internal,
            f"{compressed_name}_idx",
            "i",
            compressed_space,
            8192,
            heap_oid=compressed_oid,
        ),
        rel(origin_oid + 20, "pg_toast", f"pg_toast_{origin_oid}", "t", origin_space, 32768),
        rel(
            origin_oid + 21,
            "pg_toast",
            f"pg_toast_{origin_oid}_index",
            "i",
            origin_space,
            8192,
            heap_oid=origin_oid + 20,
        ),
        rel(compressed_oid + 20, "pg_toast", f"pg_toast_{compressed_oid}", "t", compressed_space, 16384),
        rel(
            compressed_oid + 21,
            "pg_toast",
            f"pg_toast_{compressed_oid}_index",
            "i",
            compressed_space,
            8192,
            heap_oid=compressed_oid + 20,
        ),
    ]
    for index in range(extra_indexes):
        members.append(
            rel(
                9000 + origin_oid + index,
                internal,
                f"{index}_quoted_idx",
                "i",
                origin_space,
                8192,
                heap_oid=origin_oid,
            )
        )
    return tuple(members)


def parity_aggregate(
    inventory: HypertableInventory,
    *,
    row_count: int = 2,
    checksum_xor: int = 1,
    checksum_sum: int = 3,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "row_count": row_count,
        "checksum_xor": checksum_xor,
        "checksum_sum": checksum_sum,
    }
    for index, _column in enumerate(inventory.columns):
        row[f"nn_{index}"] = row_count
    return row


def parity_row(inventory: HypertableInventory, token: str = "Prow") -> dict[str, Any]:
    del token
    return parity_aggregate(inventory)


def _chunk_row(item: CatalogChunk) -> dict[str, Any]:
    return {
        "hypertable_schema": item.hypertable_schema,
        "hypertable_name": item.hypertable_name,
        "chunk_schema": item.origin_schema,
        "chunk_name": item.origin_name,
        "range_start": item.range_start,
        "range_end": item.range_end,
        "is_compressed": item.is_compressed,
    }


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.description: list[tuple[str]] | None = None
        self._rows: list[Mapping[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.connection.executed.append((sql, params))
        self._rows, names = self.connection.dispatch(sql, params)
        self.description = [(name,) for name in names]

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]

    def fetchone(self) -> dict[str, Any] | None:
        return dict(self._rows[0]) if self._rows else None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.autocommit = True
        self.fail_sql: str | None = None
        self.fail_exc: BaseException | None = None
        self.chunks: dict[str, CatalogChunk] = {}
        self.relations: dict[int, CatalogRelation] = {}
        self.tablespace_location = "/home/postgres/pgdata/tablespaces/nhms_cold"
        self.attached: dict[tuple[str, str], list[str]] = {}
        self.server_version = "15.2"
        self.timescaledb_version = "2.10.2"
        self.inventories = bound_inventories()
        self.before_compression_total_bytes = 1000
        self.compression_bytes: dict[str, int] = {}
        self.parity_rows: list[dict[str, Any]] = []
        self.commit_hook: Any = None
        self.after_lock_hook: Any = None
        self.origin_oids: set[int] = set()
        self.parent_oids: dict[tuple[str, str], int] = {
            ("hydro", "river_timeseries"): 1001,
            ("met", "forcing_station_timeseries"): 2001,
        }

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        if self.commit_hook is not None:
            self.commit_hook(self)
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True

    def load_group(self, item: CatalogChunk, relations: Sequence[CatalogRelation]) -> None:
        self.chunks[item.origin_name] = item
        self.origin_oids.add(item.origin_oid)
        self.compression_bytes.setdefault(item.origin_name, self.before_compression_total_bytes)
        for relation in relations:
            self.relations[relation.oid] = relation
        if not self.parity_rows:
            inventory = self.inventories.for_hypertable(item.hypertable_schema, item.hypertable_name)
            self.parity_rows = [parity_row(inventory)]

    def dispatch(self, sql: str, params: Any) -> tuple[list[Mapping[str, Any]], list[str]]:
        if self.fail_sql and self.fail_sql in sql:
            assert self.fail_exc is not None
            raise self.fail_exc
        text = " ".join(sql.split())
        if "FROM pg_attribute" in text:
            schema, name = params
            inventory = self.inventories.for_hypertable(schema, name)
            rows = []
            for column in inventory.columns:
                typtype = "e" if column.type_name.startswith("hydro.") else "b"
                rows.append(
                    {
                        "attnum": column.attnum,
                        "attname": column.name,
                        "type_name": column.type_name,
                        "attnotnull": column.not_null,
                        "attidentity": column.identity,
                        "attgenerated": column.generated,
                        "typtype": typtype,
                    }
                )
            return rows, ["attnum", "attname", "type_name", "attnotnull", "attidentity", "attgenerated", "typtype"]
        if "pg_class c" in text and "c.relname = %s" in text and "ANY" not in text:
            schema, name = params
            parent_oid = self.parent_oids.get((schema, name))
            if parent_oid is not None:
                return [{"oid": parent_oid}], ["oid"]
            for relation in self.relations.values():
                if relation.schema == schema and relation.name == name:
                    return [{"oid": relation.oid}], ["oid"]
            for item in self.chunks.values():
                if item.origin_schema == schema and item.origin_name == name:
                    return [{"oid": item.origin_oid}], ["oid"]
                if item.compressed_schema == schema and item.compressed_name == name and item.compressed_oid:
                    return [{"oid": item.compressed_oid}], ["oid"]
            return [], ["oid"]
        if "_timescaledb_catalog.chunk AS origin" in text:
            origin_schema, origin_name = params
            for item in self.chunks.values():
                if item.origin_schema == origin_schema and item.origin_name == origin_name and item.compressed_oid:
                    return (
                        [{"schema_name": item.compressed_schema, "table_name": item.compressed_name}],
                        ["schema_name", "table_name"],
                    )
            return [], ["schema_name", "table_name"]
        if "pg_relation_size" in text:
            oids = list(params[0])
            rows = []
            for oid in oids:
                relation = self.relations[oid]
                rows.append(
                    {
                        "oid": relation.oid,
                        "schema": relation.schema,
                        "name": relation.name,
                        "relkind": relation.relkind,
                        "tablespace": relation.tablespace,
                        "bytes": relation.bytes,
                        "toast_oid": relation.toast_oid,
                        "heap_oid": relation.heap_oid,
                    }
                )
            return rows, ["oid", "schema", "name", "relkind", "tablespace", "bytes", "toast_oid", "heap_oid"]
        if "FROM pg_index" in text:
            heap_oids = set(params[0])
            rows = [
                {"indexrelid": relation.oid}
                for relation in self.relations.values()
                if relation.relkind == "i" and relation.heap_oid in heap_oids
            ]
            return rows, ["indexrelid"]
        if "timescaledb_information.dimensions" in text:
            return (
                [{"column_name": "valid_time", "column_type": "timestamp with time zone", "dimension_type": "Time"}],
                ["column_name", "column_type", "dimension_type"],
            )
        if "timescaledb_information.chunks" in text and "range_end <=" in text:
            schema, name, cutoff, limit = params
            rows = []
            for item in sorted(self.chunks.values(), key=lambda value: (value.range_end, value.origin_oid)):
                if item.hypertable_schema == schema and item.hypertable_name == name and item.range_end <= cutoff:
                    rows.append(_chunk_row(item))
            names = [
                "hypertable_schema",
                "hypertable_name",
                "chunk_schema",
                "chunk_name",
                "range_start",
                "range_end",
                "is_compressed",
            ]
            return rows[: int(limit)], names
        if "timescaledb_information.chunks" in text:
            schema, name, origin_schema, origin_name = params
            for item in self.chunks.values():
                if (
                    item.hypertable_schema == schema
                    and item.hypertable_name == name
                    and item.origin_schema == origin_schema
                    and item.origin_name == origin_name
                ):
                    row = _chunk_row(item)
                    return [row], list(row.keys())
            return [], ["hypertable_schema"]
        if "chunk_compression_stats" in text:
            origin_name = params[2] if isinstance(params, tuple | list) and len(params) >= 3 else None
            value = self.compression_bytes.get(str(origin_name), self.before_compression_total_bytes)
            return (
                [{"before_compression_total_bytes": value}],
                ["before_compression_total_bytes"],
            )
        if "pg_tablespace_location" in text:
            return [{"pg_tablespace_location": self.tablespace_location}], ["pg_tablespace_location"]
        if "server_version" in text:
            return (
                [{"server_version": self.server_version, "timescaledb_version": self.timescaledb_version}],
                ["server_version", "timescaledb_version"],
            )
        if "_timescaledb_catalog.tablespace" in text:
            schema, name = params
            names = self.attached.get((schema, name), [])
            return [{"tablespace_name": item} for item in names], ["tablespace_name"]
        if "hashtextextended" in text or "checksum_xor" in text:
            if not self.parity_rows:
                return [], ["row_count", "checksum_xor", "checksum_sum"]
            return self.parity_rows, list(self.parity_rows[0].keys())
        if (
            text.startswith("LOCK TABLE")
            or "SET TABLESPACE" in text
            or text.startswith("SET LOCAL")
            or "decompress_chunk" in text
            or "compress_chunk" in text
        ):
            if "ACCESS SHARE" in text:
                return [], []
            self._apply_mutation(text)
            return [], []
        return [], []

    def _move_named(self, sql: str, tablespace: str) -> None:
        for oid, relation in list(self.relations.items()):
            quoted = f'"{relation.name}"'
            if quoted in sql:
                self.relations[oid] = CatalogRelation(
                    relation.oid,
                    relation.schema,
                    relation.name,
                    relation.relkind,
                    tablespace,
                    relation.bytes,
                    relation.toast_oid,
                    relation.heap_oid,
                )

    def _apply_mutation(self, sql: str) -> None:
        if sql.startswith("LOCK TABLE") and self.after_lock_hook is not None:
            self.after_lock_hook(self)
        if "SET TABLESPACE" in sql:
            target = "nhms_cold" if "nhms_cold" in sql else "pg_default"
            self._move_named(sql, target)
        if "decompress_chunk" in sql:
            for item in list(self.chunks.values()):
                if item.origin_name not in sql:
                    continue
                self.chunks[item.origin_name] = CatalogChunk(
                    item.hypertable_schema,
                    item.hypertable_name,
                    item.origin_oid,
                    item.origin_schema,
                    item.origin_name,
                    None,
                    None,
                    None,
                    item.range_start,
                    item.range_end,
                    False,
                )
                keep_heaps = {item.origin_oid, item.origin_oid + 20}
                for oid, relation in list(self.relations.items()):
                    owner = relation.oid if relation.relkind in {"r", "t"} else relation.heap_oid
                    if owner in {item.compressed_oid, (item.compressed_oid or 0) + 20}:
                        self.relations.pop(oid, None)
                        continue
                    if owner in keep_heaps:
                        self.relations[oid] = CatalogRelation(
                            relation.oid,
                            relation.schema,
                            relation.name,
                            relation.relkind,
                            "nhms_cold",
                            relation.bytes,
                            relation.toast_oid,
                            relation.heap_oid,
                        )
        if "compress_chunk" in sql:
            for item in list(self.chunks.values()):
                if item.origin_name not in sql:
                    continue
                new_oid = item.origin_oid + 89
                self.chunks[item.origin_name] = CatalogChunk(
                    item.hypertable_schema,
                    item.hypertable_name,
                    item.origin_oid,
                    item.origin_schema,
                    item.origin_name,
                    new_oid,
                    "_timescaledb_internal",
                    f"compress_hyper_{new_oid}",
                    item.range_start,
                    item.range_end,
                    True,
                )
                self.relations[new_oid] = rel(
                    new_oid,
                    "_timescaledb_internal",
                    f"compress_hyper_{new_oid}",
                    "r",
                    "nhms_cold",
                    4096,
                    toast_oid=new_oid + 100,
                )
                self.relations[new_oid + 100] = rel(
                    new_oid + 100, "pg_toast", f"pg_toast_{new_oid}", "t", "nhms_cold", 16384
                )
                self.relations[new_oid + 101] = rel(
                    new_oid + 101,
                    "pg_toast",
                    f"pg_toast_{new_oid}_index",
                    "i",
                    "nhms_cold",
                    8192,
                    heap_oid=new_oid + 100,
                )
                self.relations[new_oid + 5] = rel(
                    new_oid + 5,
                    "_timescaledb_internal",
                    f"compress_hyper_{new_oid}_idx",
                    "i",
                    "nhms_cold",
                    8192,
                    heap_oid=new_oid,
                )
