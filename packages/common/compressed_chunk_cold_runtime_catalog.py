"""Production catalog, inventory, and window-parity helpers for cold residency.

TimescaleDB 2.10.2 compatible. This module never imports probe-private helpers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from packages.common.compressed_chunk_cold_residency import (
    ALLOWED_HYPERTABLES,
    CatalogChunk,
    CatalogRelation,
    ColdResidencyError,
    DurableIdentity,
    ResidencyGroup,
    ResidencyMember,
    classify_residency,
    json_ready,
    origin_shell_is_not_complete,
    qualified_ident,
    quote_ident,
    resolve_residency_group,
)

RELATION_OID_SQL = """
SELECT c.oid FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s
"""
COMPRESSED_SIBLING_SQL = """
SELECT sibling.schema_name, sibling.table_name
FROM _timescaledb_catalog.chunk AS origin
JOIN _timescaledb_catalog.chunk AS sibling
  ON sibling.id = origin.compressed_chunk_id
WHERE origin.schema_name = %s AND origin.table_name = %s
  AND NOT origin.dropped AND NOT sibling.dropped
"""
RELATION_SQL = """
SELECT c.oid, n.nspname AS schema, c.relname AS name, c.relkind,
       COALESCE(ts.spcname, 'pg_default') AS tablespace,
       pg_relation_size(c.oid) AS bytes,
       NULLIF(c.reltoastrelid, 0) AS toast_oid,
       i.indrelid AS heap_oid
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_tablespace ts ON ts.oid = c.reltablespace
LEFT JOIN pg_index i ON i.indexrelid = c.oid
WHERE c.oid = ANY(%s)
ORDER BY c.oid
"""
INDEX_OIDS_SQL = "SELECT indexrelid FROM pg_index WHERE indrelid = ANY(%s) ORDER BY indexrelid"
COLUMN_INVENTORY_SQL = """
SELECT a.attnum,
       a.attname,
       format_type(a.atttypid, a.atttypmod) AS type_name,
       a.attnotnull,
       a.attidentity,
       a.attgenerated,
       t.typtype
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_type t ON t.oid = a.atttypid
WHERE n.nspname = %s AND c.relname = %s
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""
TIME_DIMENSION_SQL = """
SELECT column_name, column_type, dimension_type
FROM timescaledb_information.dimensions
WHERE hypertable_schema = %s AND hypertable_name = %s
ORDER BY dimension_number
"""
CHUNK_WINDOW_SQL = """
SELECT hypertable_schema, hypertable_name, chunk_schema, chunk_name,
       range_start, range_end, is_compressed
FROM timescaledb_information.chunks
WHERE hypertable_schema = %s AND hypertable_name = %s
  AND is_compressed = true
  AND range_end <= %s
ORDER BY range_end ASC, chunk_schema ASC, chunk_name ASC
LIMIT %s
"""
CHUNK_BY_ORIGIN_SQL = """
SELECT hypertable_schema, hypertable_name, chunk_schema, chunk_name,
       range_start, range_end, is_compressed
FROM timescaledb_information.chunks
WHERE hypertable_schema = %s AND hypertable_name = %s
  AND chunk_schema = %s AND chunk_name = %s
"""
COMPRESSION_STATS_SQL = """
SELECT before_compression_total_bytes::bigint AS before_compression_total_bytes
FROM chunk_compression_stats(%s::regclass)
WHERE chunk_schema = %s AND chunk_name = %s
"""
TABLESPACE_LOCATION_SQL = """
SELECT pg_tablespace_location(oid) FROM pg_tablespace WHERE spcname = %s
"""
ENGINE_IDENTITY_SQL = """
SELECT current_setting('server_version') AS server_version,
       (SELECT extversion FROM pg_extension WHERE extname = 'timescaledb') AS timescaledb_version
"""
HYPERTABLE_ATTACH_SQL = """
SELECT ts.tablespace_name
FROM _timescaledb_catalog.tablespace ts
JOIN _timescaledb_catalog.hypertable ht ON ht.id = ts.hypertable_id
WHERE ht.schema_name = %s AND ht.table_name = %s
"""

_SUPPORTED_TYPE_PREFIXES = (
    "smallint",
    "integer",
    "bigint",
    "real",
    "double precision",
    "numeric",
    "boolean",
    "text",
    "character",
    "character varying",
    "varchar",
    "name",
    "date",
    "time ",
    "timestamp",
    "interval",
    "uuid",
)
_UNSUPPORTED_TYPE_MARKERS = (
    "json",
    "xml",
    "bytea",
    "record",
    "tsvector",
    "tsquery",
    "pg_lsn",
    "polygon",
    "path",
    "circle",
    "point",
    "line",
    "box",
    "lseg",
    "cidr",
    "inet",
    "macaddr",
    "bit",
    "money",
    "txid",
    "any",
)


class ColdRuntimeError(ColdResidencyError):
    """Fail-closed production runtime error before or during a residency move."""

    def __init__(self, message: str, *, error_class: str = "runtime", stage: str = "runtime") -> None:
        super().__init__(message)
        self.error_class = error_class
        self.stage = stage


@dataclass(frozen=True)
class ColumnDescriptor:
    attnum: int
    name: str
    type_name: str
    not_null: bool
    identity: str
    generated: str


@dataclass(frozen=True)
class HypertableInventory:
    schema: str
    name: str
    columns: tuple[ColumnDescriptor, ...]
    digest: str


@dataclass(frozen=True)
class BoundInventories:
    river: HypertableInventory
    forcing: HypertableInventory
    digest: str

    def for_hypertable(self, schema: str, name: str) -> HypertableInventory:
        if (schema, name) == ("hydro", "river_timeseries"):
            return self.river
        if (schema, name) == ("met", "forcing_station_timeseries"):
            return self.forcing
        raise ColdRuntimeError(
            f"hypertable {schema}.{name} is not allowlisted",
            error_class="inventory_drift",
            stage="inventory",
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "hypertables": {
                "hydro.river_timeseries": _inventory_payload(self.river),
                "met.forcing_station_timeseries": _inventory_payload(self.forcing),
            },
        }


def _inventory_payload(inventory: HypertableInventory) -> dict[str, Any]:
    return {
        "schema": inventory.schema,
        "name": inventory.name,
        "digest": inventory.digest,
        "columns": [
            {
                "attnum": column.attnum,
                "name": column.name,
                "type_name": column.type_name,
                "not_null": column.not_null,
                "identity": column.identity,
                "generated": column.generated,
            }
            for column in inventory.columns
        ],
    }


@dataclass(frozen=True)
class WindowParity:
    row_count: int
    non_null_counts: tuple[tuple[str, int], ...]
    checksum: str
    inventory_digest: str
    range_start: datetime
    range_end: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_count": self.row_count,
            "non_null_counts": {name: count for name, count in self.non_null_counts},
            "checksum": self.checksum,
            "inventory_digest": self.inventory_digest,
            "range_start": range_start_iso(self.range_start),
            "range_end": range_start_iso(self.range_end),
        }


def window_parity_from_dict(payload: Mapping[str, Any]) -> WindowParity:
    counts = payload.get("non_null_counts")
    if not isinstance(counts, Mapping):
        raise ColdRuntimeError("before parity is corrupt", error_class="corrupt_intent", stage="startup")
    try:
        return WindowParity(
            row_count=int(payload["row_count"]),
            non_null_counts=tuple((str(name), int(count)) for name, count in counts.items()),
            checksum=str(payload["checksum"]),
            inventory_digest=str(payload["inventory_digest"]),
            range_start=_aware(_parse_iso(payload["range_start"]), label="range_start"),
            range_end=_aware(_parse_iso(payload["range_end"]), label="range_end"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ColdRuntimeError("before parity is corrupt", error_class="corrupt_intent", stage="startup") from error


def _parse_iso(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ColdRuntimeError("timestamp must be ISO-8601", error_class="corrupt_intent", stage="startup")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def residency_group_from_snapshot(payload: Mapping[str, Any]) -> ResidencyGroup:
    durable = payload.get("durable")
    if not isinstance(durable, Mapping):
        raise ColdRuntimeError(
            "before snapshot is missing durable identity",
            error_class="corrupt_intent",
            stage="startup",
        )
    compressed = payload.get("compressed")
    members_raw = payload.get("members")
    if not isinstance(members_raw, Sequence) or isinstance(members_raw, (str, bytes)):
        raise ColdRuntimeError("before snapshot is missing members", error_class="corrupt_intent", stage="startup")
    members: list[ResidencyMember] = []
    for item in members_raw:
        if not isinstance(item, Mapping):
            raise ColdRuntimeError("before snapshot member is corrupt", error_class="corrupt_intent", stage="startup")
        try:
            members.append(
                ResidencyMember(
                    kind=item["kind"],  # type: ignore[arg-type]
                    oid=int(item["oid"]),
                    schema=str(item["schema"]),
                    name=str(item["name"]),
                    relkind=str(item["relkind"]),
                    tablespace=str(item["tablespace"]),
                    bytes=int(item["bytes"]),
                    heap_oid=None if item.get("heap_oid") is None else int(item["heap_oid"]),
                    toast_oid=None if item.get("toast_oid") is None else int(item["toast_oid"]),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ColdRuntimeError(
                "before snapshot member is corrupt",
                error_class="corrupt_intent",
                stage="startup",
            ) from error
    compressed_oid = None
    compressed_schema = None
    compressed_name = None
    if isinstance(compressed, Mapping):
        try:
            compressed_oid = int(compressed["oid"])
            compressed_schema = None if compressed.get("schema") is None else str(compressed["schema"])
            compressed_name = None if compressed.get("name") is None else str(compressed["name"])
        except (KeyError, TypeError, ValueError) as error:
            raise ColdRuntimeError(
                "before snapshot sibling is corrupt",
                error_class="corrupt_intent",
                stage="startup",
            ) from error
    try:
        return ResidencyGroup(
            hypertable_schema=str(durable["hypertable_schema"]),
            hypertable_name=str(durable["hypertable_name"]),
            origin_oid=int(durable["origin_oid"]),
            origin_schema=str(durable["origin_schema"]),
            origin_name=str(durable["origin_name"]),
            compressed_oid=compressed_oid,
            compressed_schema=compressed_schema,
            compressed_name=compressed_name,
            range_start=_aware(_parse_iso(durable["range_start"]), label="range_start"),
            range_end=_aware(_parse_iso(durable["range_end"]), label="range_end"),
            is_compressed=bool(payload.get("is_compressed")),
            members=tuple(members),
            blocker=None if payload.get("blocker") is None else str(payload["blocker"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ColdRuntimeError("before snapshot is corrupt", error_class="corrupt_intent", stage="startup") from error


Execute = Callable[..., list[Mapping[str, Any]]]


def range_start_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _aware(value: Any, *, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ColdRuntimeError(f"{label} must be timestamptz", error_class="catalog", stage="catalog")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ColdRuntimeError(f"{label} must be timezone-aware", error_class="catalog", stage="catalog")
    return value.astimezone(UTC)


def _inventory_digest(columns: Sequence[ColumnDescriptor]) -> str:
    payload = [
        {
            "attnum": column.attnum,
            "name": column.name,
            "type_name": column.type_name,
            "not_null": column.not_null,
            "identity": column.identity,
            "generated": column.generated,
        }
        for column in columns
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _type_is_supported(type_name: str, typtype: str) -> bool:
    lowered = type_name.lower()
    if any(marker in lowered for marker in _UNSUPPORTED_TYPE_MARKERS):
        return False
    if typtype in {"e", "b", "d"} and any(lowered.startswith(prefix) for prefix in _SUPPORTED_TYPE_PREFIXES):
        return True
    return typtype == "e"


def _canonical_sql_expr(column: ColumnDescriptor) -> str:
    ident = quote_ident(column.name)
    lowered = column.type_name.lower()
    type_tag = lowered.replace("'", "''")
    if lowered.startswith("timestamp"):
        rendered = f"to_char(timezone('UTC', {ident}), 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
    elif "bytea" in lowered:
        rendered = f"encode({ident}, 'hex')"
    else:
        rendered = f"{ident}::text"
    return (
        f"CASE WHEN {ident} IS NULL THEN 'N|{type_tag}|' "
        f"ELSE 'P|{type_tag}|' || length({rendered}::text)::text || '|' || {rendered} END"
    )


def window_parity_sql(inventory: HypertableInventory) -> str:
    if not inventory.columns:
        raise ColdRuntimeError(
            f"{inventory.schema}.{inventory.name} has no user columns",
            error_class="inventory_drift",
            stage="inventory",
        )
    rel = qualified_ident(inventory.schema, inventory.name)
    token = " || chr(31) || ".join(_canonical_sql_expr(column) for column in inventory.columns)
    nn_select = ", ".join(
        f"count({quote_ident(column.name)})::bigint AS nn_{index}" for index, column in enumerate(inventory.columns)
    )
    row_hash = f"hashtextextended({token}, 0)"
    return (
        "SELECT count(*)::bigint AS row_count, "
        f"{nn_select}, "
        f"coalesce(bit_xor({row_hash}), 0)::bigint AS checksum_xor, "
        f"coalesce(sum({row_hash}), 0)::numeric AS checksum_sum "
        f"FROM {rel} "
        "WHERE valid_time >= %s AND valid_time < %s"
    )


def _aggregate_checksum(*, row_count: int, xor_bits: str, sum_value: object, non_null: Sequence[int]) -> str:
    payload = f"{row_count}:{xor_bits}:{sum_value}:{','.join(str(item) for item in non_null)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_window_parity(
    execute: Execute,
    inventory: HypertableInventory,
    *,
    range_start: datetime,
    range_end: datetime,
) -> WindowParity:
    sql = window_parity_sql(inventory)
    if "string_agg" in sql.lower() or " AS token" in sql:
        raise ColdRuntimeError("parity SQL must be a one-row aggregate", error_class="parity", stage="parity")
    rows = execute(sql, (range_start, range_end))
    if len(rows) != 1:
        raise ColdRuntimeError(
            f"parity query returned {len(rows)} rows, expected one aggregate row",
            error_class="parity",
            stage="parity",
        )
    row = rows[0]
    if any(key in row for key in ("token", "valid_time")):
        raise ColdRuntimeError("parity query returned row-shaped payloads", error_class="parity", stage="parity")
    if "row_count" not in row or "checksum_xor" not in row or "checksum_sum" not in row:
        raise ColdRuntimeError("parity query returned row-shaped payloads", error_class="parity", stage="parity")
    row_count = int(row["row_count"])
    non_null = [int(row.get(f"nn_{index}") or 0) for index, _column in enumerate(inventory.columns)]
    checksum = _aggregate_checksum(
        row_count=row_count,
        xor_bits=str(row["checksum_xor"]),
        sum_value=row["checksum_sum"],
        non_null=non_null,
    )
    return WindowParity(
        row_count=row_count,
        non_null_counts=tuple((column.name, non_null[index]) for index, column in enumerate(inventory.columns)),
        checksum=checksum,
        inventory_digest=inventory.digest,
        range_start=range_start.astimezone(UTC),
        range_end=range_end.astimezone(UTC),
    )


def derive_hypertable_inventory(execute: Execute, schema: str, name: str) -> HypertableInventory:
    rows = execute(COLUMN_INVENTORY_SQL, (schema, name))
    if not rows:
        raise ColdRuntimeError(
            f"column inventory missing for {schema}.{name}",
            error_class="inventory_drift",
            stage="inventory",
        )
    columns: list[ColumnDescriptor] = []
    seen_names: set[str] = set()
    previous_attnum = 0
    for row in rows:
        attnum = int(row["attnum"])
        col_name = str(row["attname"])
        type_name = str(row["type_name"])
        if attnum <= previous_attnum:
            raise ColdRuntimeError(
                f"{schema}.{name} column attnum order drifted",
                error_class="inventory_drift",
                stage="inventory",
            )
        previous_attnum = attnum
        if col_name in seen_names:
            raise ColdRuntimeError(
                f"{schema}.{name} duplicated column {col_name}",
                error_class="inventory_drift",
                stage="inventory",
            )
        seen_names.add(col_name)
        if not _type_is_supported(type_name, str(row["typtype"])):
            raise ColdRuntimeError(
                f"{schema}.{name}.{col_name} has unsupported type {type_name}",
                error_class="unsupported_column",
                stage="inventory",
            )
        columns.append(
            ColumnDescriptor(
                attnum=attnum,
                name=col_name,
                type_name=type_name,
                not_null=bool(row["attnotnull"]),
                identity=str(row["attidentity"] or ""),
                generated=str(row["attgenerated"] or ""),
            )
        )
    names = [column.name for column in columns]
    if "valid_time" not in names:
        raise ColdRuntimeError(
            f"{schema}.{name} is missing valid_time",
            error_class="inventory_drift",
            stage="inventory",
        )
    return HypertableInventory(
        schema=schema,
        name=name,
        columns=tuple(columns),
        digest=_inventory_digest(columns),
    )


def validate_time_dimension(execute: Execute, schema: str, name: str) -> None:
    rows = execute(TIME_DIMENSION_SQL, (schema, name))
    time_dims = [row for row in rows if str(row.get("dimension_type") or "").lower() == "time"]
    if len(time_dims) != 1:
        raise ColdRuntimeError(
            f"{schema}.{name} must have exactly one open time dimension",
            error_class="inventory_drift",
            stage="inventory",
        )
    column_name = str(time_dims[0]["column_name"])
    column_type = str(time_dims[0]["column_type"]).lower()
    if column_name != "valid_time":
        raise ColdRuntimeError(
            f"{schema}.{name} time dimension is {column_name}, not valid_time",
            error_class="inventory_drift",
            stage="inventory",
        )
    if "timestamp with time zone" not in column_type and column_type != "timestamptz":
        raise ColdRuntimeError(
            f"{schema}.{name}.valid_time must be timestamptz, got {column_type}",
            error_class="inventory_drift",
            stage="inventory",
        )


def derive_bound_inventories(execute: Execute) -> BoundInventories:
    inventories: dict[tuple[str, str], HypertableInventory] = {}
    for schema, name in (("hydro", "river_timeseries"), ("met", "forcing_station_timeseries")):
        validate_time_dimension(execute, schema, name)
        inventories[(schema, name)] = derive_hypertable_inventory(execute, schema, name)
    river = inventories[("hydro", "river_timeseries")]
    forcing = inventories[("met", "forcing_station_timeseries")]
    digest = hashlib.sha256(f"{river.digest}:{forcing.digest}".encode("utf-8")).hexdigest()
    return BoundInventories(river=river, forcing=forcing, digest=digest)


def _relation_oid(execute: Execute, schema: str, name: str) -> int | None:
    rows = execute(RELATION_OID_SQL, (schema, name))
    if not rows:
        return None
    return int(next(iter(rows[0].values())))


def load_catalog_chunk(
    execute: Execute,
    *,
    hypertable_schema: str,
    hypertable_name: str,
    origin_schema: str,
    origin_name: str,
) -> CatalogChunk:
    rows = execute(CHUNK_BY_ORIGIN_SQL, (hypertable_schema, hypertable_name, origin_schema, origin_name))
    if len(rows) != 1:
        raise ColdRuntimeError(
            f"origin chunk {origin_schema}.{origin_name} missing or ambiguous",
            error_class="relation_disappeared",
            stage="catalog",
        )
    return _row_to_chunk(execute, rows[0])


def _row_to_chunk(execute: Execute, row: Mapping[str, Any]) -> CatalogChunk:
    origin_schema = str(row["chunk_schema"])
    origin_name = str(row["chunk_name"])
    origin_oid = _relation_oid(execute, origin_schema, origin_name)
    if origin_oid is None:
        raise ColdRuntimeError(
            f"origin oid missing for {origin_schema}.{origin_name}",
            error_class="relation_disappeared",
            stage="catalog",
        )
    sibling = execute(COMPRESSED_SIBLING_SQL, (origin_schema, origin_name))
    compressed_schema = str(sibling[0]["schema_name"]) if sibling else None
    compressed_name = str(sibling[0]["table_name"]) if sibling else None
    compressed_oid = None
    if compressed_schema and compressed_name:
        compressed_oid = _relation_oid(execute, compressed_schema, compressed_name)
    return CatalogChunk(
        hypertable_schema=str(row["hypertable_schema"]),
        hypertable_name=str(row["hypertable_name"]),
        origin_oid=origin_oid,
        origin_schema=origin_schema,
        origin_name=origin_name,
        compressed_oid=compressed_oid,
        compressed_schema=compressed_schema,
        compressed_name=compressed_name,
        range_start=_aware(row["range_start"], label="range_start"),
        range_end=_aware(row["range_end"], label="range_end"),
        is_compressed=bool(row["is_compressed"]),
    )


def load_relations(execute: Execute, oids: Sequence[int]) -> list[CatalogRelation]:
    if not oids:
        return []
    return [
        CatalogRelation(
            oid=int(row["oid"]),
            schema=str(row["schema"]),
            name=str(row["name"]),
            relkind=str(row["relkind"]),
            tablespace=str(row["tablespace"]),
            bytes=int(row["bytes"]),
            toast_oid=None if row["toast_oid"] is None else int(row["toast_oid"]),
            heap_oid=None if row["heap_oid"] is None else int(row["heap_oid"]),
        )
        for row in execute(RELATION_SQL, (list(oids),))
    ]


def collect_residency_group(execute: Execute, chunk: CatalogChunk) -> ResidencyGroup:
    current = chunk
    oids = {current.origin_oid}
    if current.compressed_oid:
        oids.add(current.compressed_oid)
    relations = load_relations(execute, sorted(oids))
    toast = [rel.toast_oid for rel in relations if rel.toast_oid]
    if toast:
        relations.extend(load_relations(execute, toast))
    heap_oids = [rel.oid for rel in relations if rel.relkind in {"r", "t"}]
    if heap_oids:
        index_oids = [int(row["indexrelid"]) for row in execute(INDEX_OIDS_SQL, (heap_oids,))]
        if index_oids:
            relations.extend(load_relations(execute, index_oids))
    unique = {rel.oid: rel for rel in relations}
    return resolve_residency_group(current, tuple(unique.values()))


def load_eligible_chunks(
    execute: Execute,
    *,
    schema: str,
    name: str,
    cutoff: datetime,
    limit: int,
    max_bytes: int,
) -> list[CatalogChunk]:
    if (schema, name) not in ALLOWED_HYPERTABLES:
        raise ColdRuntimeError(
            f"{schema}.{name} is not allowlisted",
            error_class="ineligible_hypertable",
            stage="selection",
        )
    rows = execute(CHUNK_WINDOW_SQL, (schema, name, cutoff, limit + 1))
    if len(rows) > limit:
        raise ColdRuntimeError(
            "catalog discovery exceeds the row/candidate ceiling",
            error_class="bound",
            stage="catalog",
        )
    captured = 2
    chunks: list[CatalogChunk] = []
    for row in rows:
        captured += len(json.dumps(json_ready(dict(row)), sort_keys=True, separators=(",", ":")).encode())
        if captured > max_bytes:
            raise ColdRuntimeError("catalog discovery exceeds the byte ceiling", error_class="bound", stage="catalog")
        chunks.append(_row_to_chunk(execute, row))
    return chunks


def compression_before_bytes(execute: Execute, chunk: CatalogChunk) -> int:
    rows = execute(
        COMPRESSION_STATS_SQL,
        (f"{chunk.hypertable_schema}.{chunk.hypertable_name}", chunk.origin_schema, chunk.origin_name),
    )
    if not rows or rows[0]["before_compression_total_bytes"] is None:
        raise ColdRuntimeError(
            f"chunk_compression_stats missing for {chunk.origin_name}",
            error_class="capacity",
            stage="capacity",
        )
    return int(rows[0]["before_compression_total_bytes"])


def tablespace_catalog_location(execute: Execute, name: str) -> str:
    rows = execute(TABLESPACE_LOCATION_SQL, (name,))
    if not rows or not rows[0] or next(iter(rows[0].values())) in {None, ""}:
        raise ColdRuntimeError(
            f"tablespace {name} catalog location is missing",
            error_class="target_identity",
            stage="target_identity",
        )
    return str(next(iter(rows[0].values())))


def attached_tablespaces(execute: Execute, schema: str, name: str) -> tuple[str, ...]:
    return tuple(str(row["tablespace_name"]) for row in execute(HYPERTABLE_ATTACH_SQL, (schema, name)))


def engine_versions(execute: Execute) -> tuple[str, str]:
    rows = execute(ENGINE_IDENTITY_SQL)
    if not rows:
        raise ColdRuntimeError("engine identity is unavailable", error_class="engine_identity", stage="preflight")
    server = str(rows[0]["server_version"])
    timescale = str(rows[0]["timescaledb_version"])
    return server, timescale


def durable_payload(identity: DurableIdentity) -> dict[str, Any]:
    return {
        "hypertable_schema": identity.hypertable_schema,
        "hypertable_name": identity.hypertable_name,
        "origin_oid": identity.origin_oid,
        "origin_schema": identity.origin_schema,
        "origin_name": identity.origin_name,
        "range_start": identity.range_start,
        "range_end": identity.range_end,
    }


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
            "heap_oid": member.heap_oid,
            "toast_oid": member.toast_oid,
        }
        for member in group.members
    ]


def snapshot_group(group: ResidencyGroup) -> dict[str, Any]:
    return json_ready(
        {
            "durable": durable_payload(group.durable_identity),
            "compressed": None
            if group.compressed_oid is None
            else {
                "oid": group.compressed_oid,
                "schema": group.compressed_schema,
                "name": group.compressed_name,
            },
            "is_compressed": group.is_compressed,
            "residency": classify_residency(group.members),
            "blocker": group.blocker,
            "members": members_payload(group),
            "origin_shell_incomplete": origin_shell_is_not_complete(group),
        }
    )


def retained_source_bytes(group: ResidencyGroup) -> int:
    return int(sum(member.bytes for member in group.members))


def relation_oid(execute: Execute, schema: str, name: str) -> int:
    rows = execute(RELATION_OID_SQL, (schema, name))
    if not rows:
        raise ColdRuntimeError(f"{schema}.{name} disappeared", error_class="relation_disappeared", stage="revalidate")
    return int(rows[0]["oid"])


def lock_allowlisted_parents(execute: Execute) -> tuple[str, ...]:
    ordered = sorted(
        ((relation_oid(execute, schema, name), schema, name) for schema, name in ALLOWED_HYPERTABLES),
        key=lambda item: (item[0], item[1], item[2]),
    )
    statements = tuple(
        f"LOCK TABLE {qualified_ident(schema, name)} IN ACCESS SHARE MODE" for _oid, schema, name in ordered
    )
    for statement in statements:
        execute(statement)
    return statements


def ranked_candidates_from_execute(
    execute: Execute,
    *,
    cutoff: datetime,
    per_table_limit: int,
    max_catalog_bytes: int,
) -> list[tuple[int, datetime, str, str, int, CatalogChunk]]:
    ranked: list[tuple[int, datetime, str, str, int, CatalogChunk]] = []
    for schema, name in (("hydro", "river_timeseries"), ("met", "forcing_station_timeseries")):
        chunks = load_eligible_chunks(
            execute,
            schema=schema,
            name=name,
            cutoff=cutoff,
            limit=per_table_limit,
            max_bytes=max_catalog_bytes,
        )
        for rank, chunk in enumerate(chunks):
            ranked.append(
                (rank, chunk.range_end, chunk.hypertable_schema, chunk.hypertable_name, chunk.origin_oid, chunk)
            )
    ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]))
    return ranked
