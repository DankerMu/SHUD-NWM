"""Complete physical-group certification for #1895 hot/cold lanes.

Origin-only tablespace or a caller-supplied residency string cannot certify a
lane. Shipping catalog/group owners resolve every intersecting CatalogChunk
and its reachable heap/index/TOAST members.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from packages.common.compressed_chunk_cold_residency import (
    COLD_TABLESPACE_NAME,
    SOURCE_TABLESPACE_NAME,
    CatalogChunk,
    ResidencyGroup,
    classify_residency,
    recompressed_group_is_complete,
)
from packages.common.compressed_chunk_cold_runtime_catalog import (
    BoundInventories,
    collect_residency_group,
    load_catalog_chunk,
    require_inventory,
)
from packages.common.node27_issue1895_sql import normalize_candidate_chunk_name
from packages.common.node27_issue1895_types import Issue1895ReadinessError

Execute = Callable[..., Sequence[Mapping[str, Any]]]
LoadChunk = Callable[..., CatalogChunk]
CollectGroup = Callable[..., ResidencyGroup]

MAX_LANE_CHUNKS = 64
HOT_STATE = "hot_uncompressed_source"
COLD_STATE = "cold_compressed_target"
INTERSECTING_CHUNKS_SQL = """
SELECT chunk_schema, chunk_name, range_start, range_end, is_compressed
FROM timescaledb_information.chunks v
JOIN _timescaledb_catalog.chunk ch ON ch.schema_name = v.chunk_schema AND ch.table_name = v.chunk_name
JOIN _timescaledb_catalog.hypertable ht ON ht.id = ch.hypertable_id
JOIN pg_namespace pn ON pn.nspname = ht.schema_name
JOIN pg_class p ON p.relnamespace = pn.oid AND p.relname = ht.table_name
WHERE hypertable_schema = 'hydro'
  AND hypertable_name = 'river_timeseries'
  AND p.oid = %s AND ht.id = %s AND NOT ch.dropped
  AND range_end > %s::timestamptz
  AND range_start < %s::timestamptz
ORDER BY range_start, chunk_name
"""


def uncompressed_source_group_is_complete(group: ResidencyGroup) -> bool:
    """Hot: uncompressed origin plus every reachable member on pg_default."""

    if group.blocker or not group.members:
        return False
    if group.is_compressed or group.compressed_oid is not None:
        return False
    if any(member.kind == "compressed_heap" for member in group.members):
        return False
    origin = next((member for member in group.members if member.kind == "origin_heap"), None)
    if origin is None or origin.tablespace != SOURCE_TABLESPACE_NAME:
        return False
    if classify_residency(group.members) != "all_source":
        return False
    return all(member.tablespace == SOURCE_TABLESPACE_NAME for member in group.members)


def classify_complete_groups(
    groups: Sequence[ResidencyGroup],
    *,
    kind: str,
    max_chunks: int = MAX_LANE_CHUNKS,
) -> dict[str, Any]:
    """Certify intersecting groups. No residency string is consulted."""

    if kind not in {"hot", "cold"}:
        raise Issue1895ReadinessError(
            "lane kind is closed",
            code="LANE_KIND_INVALID",
            stage="catalog",
        )
    if not groups:
        raise Issue1895ReadinessError(
            "lane has no intersecting chunks",
            code="LANE_EMPTY",
            stage="catalog",
        )
    if len(groups) > max_chunks:
        raise Issue1895ReadinessError(
            "intersecting chunk set exceeds the bound",
            code="LANE_CHUNK_BOUND",
            stage="catalog",
        )
    names: list[str] = []
    seen_oids: set[int] = set()
    seen_names: set[str] = set()
    for group in groups:
        if group.blocker:
            raise Issue1895ReadinessError(
                "intersecting group is blocked",
                code="LANE_GROUP_BLOCKED",
                stage="catalog",
            )
        name = normalize_candidate_chunk_name(group.origin_name)
        if name in seen_names or group.origin_oid in seen_oids:
            raise Issue1895ReadinessError(
                "intersecting chunk identity is duplicated",
                code="LANE_CHUNK_DUPLICATE",
                stage="catalog",
            )
        seen_names.add(name)
        seen_oids.add(group.origin_oid)
        names.append(name)
        residency = classify_residency(group.members)
        if residency in {"mixed", "unknown"}:
            raise Issue1895ReadinessError(
                "chunk group residency is mixed or unknown",
                code="LANE_MIXED",
                stage="catalog",
            )
        if kind == "hot":
            if not uncompressed_source_group_is_complete(group):
                raise Issue1895ReadinessError(
                    "hot group is not uncompressed complete source",
                    code="LANE_HOT_NOT_SOURCE",
                    stage="catalog",
                )
        else:
            if not recompressed_group_is_complete(group, target=COLD_TABLESPACE_NAME):
                raise Issue1895ReadinessError(
                    "cold group is not compressed complete target",
                    code="LANE_COLD_NOT_TARGET",
                    stage="catalog",
                )
    unique = tuple(dict.fromkeys(names))
    return {
        "candidate_chunk_names": unique,
        "state": HOT_STATE if kind == "hot" else COLD_STATE,
        "compressed": kind == "cold",
        "tablespace": SOURCE_TABLESPACE_NAME if kind == "hot" else COLD_TABLESPACE_NAME,
        "candidate_count": len(unique),
    }


def observe_intersecting_groups(
    execute: Execute,
    *,
    inventories: BoundInventories,
    window_start: str,
    window_end: str,
    kind: str,
    load_chunk: LoadChunk = load_catalog_chunk,
    collect_group: CollectGroup = collect_residency_group,
    max_chunks: int = MAX_LANE_CHUNKS,
) -> dict[str, Any]:
    """Resolve every window chunk through shipping catalog/group owners."""

    try:
        inventory = inventories.for_hypertable("hydro", "river_timeseries")
        require_inventory(execute, inventory, "hydro", "river_timeseries")
        rows = list(
            execute(
                INTERSECTING_CHUNKS_SQL,
                (inventory.parent_oid, inventory.hypertable_id, window_start, window_end),
            )
        )
    except Issue1895ReadinessError:
        raise
    except Exception:
        raise Issue1895ReadinessError(
            "intersecting chunk discovery failed",
            code="LANE_CATALOG_FAILED",
            stage="catalog",
        ) from None
    if len(rows) > max_chunks:
        raise Issue1895ReadinessError(
            "intersecting chunk set exceeds the bound",
            code="LANE_CHUNK_BOUND",
            stage="catalog",
        )
    groups: list[ResidencyGroup] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise Issue1895ReadinessError(
                "chunk discovery row is not a mapping",
                code="LANE_CHUNK_ROW_INVALID",
                stage="catalog",
            )
        origin_schema = str(row.get("chunk_schema") or "").strip()
        origin_name = str(row.get("chunk_name") or "").strip()
        if not origin_schema or not origin_name:
            raise Issue1895ReadinessError(
                "chunk discovery row is missing origin identity",
                code="LANE_CHUNK_ROW_INVALID",
                stage="catalog",
            )
        try:
            chunk = load_chunk(
                execute,
                inventory=inventory,
                hypertable_schema="hydro",
                hypertable_name="river_timeseries",
                origin_schema=origin_schema,
                origin_name=origin_name,
            )
            group = collect_group(execute, chunk)
        except Issue1895ReadinessError:
            raise
        except Exception:
            raise Issue1895ReadinessError(
                "shipping catalog group resolution failed",
                code="LANE_GROUP_RESOLVE_FAILED",
                stage="catalog",
            ) from None
        groups.append(group)
    return classify_complete_groups(groups, kind=kind, max_chunks=max_chunks)
