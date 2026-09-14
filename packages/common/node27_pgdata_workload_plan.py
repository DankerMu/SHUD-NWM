"""PGDATA-owned window/catalog candidate discovery and structural EXPLAIN checks.

Candidates bind the shipping query's actual parents (narrow ``hydro.river_timeseries``
and legacy ``hydro.river_timeseries_legacy``) and the frozen seven-day window.
Origin and physical compressed identities come from Timescale catalog
relationships, not name prefixes or cold topology. Own-candidate DecompressChunk
is allowed; unrelated chunk or relevant Seq Scan refuses. Root Shared Hit+Read
is the only buffer total. Window proof compares parsed timestamptz instants
from valid_time/_ts_meta bounds, not rendered ISO substrings. Segment proof
accepts a fact ``river_segment_key`` parameter only when an InitPlan on
``core.river_segment`` resolves that parameter to the frozen text identity.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from packages.common.evidence_io import BoundedEvidenceError, validate_json_complexity
from packages.common.node27_pgdata_workload_types import PgdataWorkloadError, refuse

PLAN_BUFFER_LIMIT = 5000
PLAN_MAX_BYTES = 262144
PLAN_MAX_DEPTH = 48
PLAN_MAX_NODES = 10_000
PLAN_MAX_ARRAY_ITEMS = 1_000
MAX_CANDIDATES = 64
NARROW_HYPERTABLE = ("hydro", "river_timeseries")
LEGACY_HYPERTABLE = ("hydro", "river_timeseries_legacy")
CHUNK_NAME_RE = re.compile(r"^[_A-Za-z][_A-Za-z0-9]*$")
_INITPLAN_RETURNS_RE = re.compile(r"InitPlan\s+\d+\s+\(\s*returns\s+(\$\d+)\s*\)", re.IGNORECASE)
_TIME_PREDICATE_RE = re.compile(
    r"\b(valid_time|_ts_meta_(?:min|max)_\d+)\s*(>=|<=|>|<|=)\s*'([^']+)'",
    re.IGNORECASE,
)
_SEGMENT_ID_EQ_RE = re.compile(r"\briver_segment_id\s*=\s*'([^']+)'", re.IGNORECASE)
_NETWORK_ID_EQ_RE = re.compile(r"\briver_network_version_id\s*=\s*'([^']+)'", re.IGNORECASE)
_SEGMENT_KEY_PARAM_RE = re.compile(
    r"\briver_segment_key\s*=\s*(\$\d+)|\b(\$\d+)\s*=\s*river_segment_key\b",
    re.IGNORECASE,
)
_OFFSET_HOURS_RE = re.compile(r"[+-]\d{2}$")
CANDIDATE_CHUNKS_SQL = """
SELECT
    v.hypertable_schema,
    v.hypertable_name,
    v.chunk_schema,
    v.chunk_name,
    v.range_start,
    v.range_end,
    v.is_compressed,
    compressed.schema_name AS compressed_schema,
    compressed.table_name AS compressed_name
FROM timescaledb_information.chunks v
JOIN _timescaledb_catalog.chunk origin
  ON origin.schema_name = v.chunk_schema
 AND origin.table_name = v.chunk_name
 AND NOT origin.dropped
LEFT JOIN _timescaledb_catalog.chunk compressed
  ON compressed.id = origin.compressed_chunk_id
 AND NOT compressed.dropped
WHERE (v.hypertable_schema, v.hypertable_name) IN (
    ('hydro', 'river_timeseries'),
    ('hydro', 'river_timeseries_legacy')
)
  AND v.range_end > %(window_start)s::timestamptz
  AND v.range_start <= %(window_end)s::timestamptz
ORDER BY v.hypertable_name, v.range_start, v.chunk_name
"""


def normalize_candidate_chunk_name(name: object) -> str:
    text = str(name or "").strip()
    if not text or "." in text or not CHUNK_NAME_RE.fullmatch(text):
        refuse("candidate chunk name must be a bare identifier", code="PLAN_CANDIDATE_NAME_INVALID", stage="plan")
    return text


def bound_explain_payload(payload: Any) -> Any:
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        refuse("EXPLAIN JSON is not serializable", code="PLAN_JSON_INVALID", stage="plan")
    if len(encoded) > PLAN_MAX_BYTES:
        refuse("EXPLAIN JSON exceeds the serialized byte ceiling", code="PLAN_JSON_TOO_LARGE", stage="plan")
    try:
        validate_json_complexity(
            payload,
            label="EXPLAIN plan",
            max_depth=PLAN_MAX_DEPTH,
            max_nodes=PLAN_MAX_NODES,
            max_array_items=PLAN_MAX_ARRAY_ITEMS,
        )
    except BoundedEvidenceError:
        refuse("EXPLAIN JSON exceeds complexity bounds", code="PLAN_JSON_TOO_COMPLEX", stage="plan")
    return payload


def _walk_plan_nodes(plan: Any) -> list[Mapping[str, Any]]:
    nodes: list[Mapping[str, Any]] = []
    stack: list[Any] = [plan]
    while stack:
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(value)
            continue
        if not isinstance(value, Mapping):
            continue
        if "Node Type" in value:
            nodes.append(value)
        stack.extend(value.values())
    return nodes


def _shared_read_blocks_value(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _root_counter(plan: Mapping[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        parsed = _shared_read_blocks_value(plan.get(key))
        if parsed is not None:
            return parsed
    buffers = plan.get("Buffers")
    if isinstance(buffers, Mapping):
        for key in keys:
            parsed = _shared_read_blocks_value(buffers.get(key))
            if parsed is not None:
                return parsed
    refuse("root Plan shared buffer counter is missing or invalid", code="PLAN_BUFFERS_INVALID", stage="plan")
    raise AssertionError("unreachable")


def _optional_root_counter(plan: Mapping[str, Any], keys: tuple[str, ...]) -> int:
    try:
        return _root_counter(plan, keys)
    except PgdataWorkloadError:
        return 0


def _root_shared_read_blocks(plan: Mapping[str, Any]) -> int:
    return _root_counter(plan, ("Shared Read Blocks", "shared_read_blocks"))


def _root_shared_hit_blocks(plan: Mapping[str, Any]) -> int:
    return _optional_root_counter(plan, ("Shared Hit Blocks", "shared_hit_blocks"))


def _relation_basename(node: Mapping[str, Any]) -> str:
    return str(node.get("Relation Name") or "").split(".")[-1]


def _node_predicate_text(node: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in ("Index Cond", "Filter", "Recheck Cond", "Chunk Quals", "Chunk Qual"):
        value = node.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def _index_cond_text(node: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in ("Index Cond", "Bitmap Index Cond", "Recheck Cond", "Chunk Quals", "Chunk Qual"):
        value = node.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def _is_index_access(node: Mapping[str, Any]) -> bool:
    node_type = str(node.get("Node Type") or "").strip().lower()
    return node_type in {"index scan", "index only scan", "bitmap index scan", "bitmap heap scan"}


def _is_seq_scan(node: Mapping[str, Any]) -> bool:
    return str(node.get("Node Type") or "").strip().lower() == "seq scan"


def _is_decompress_chunk(node: Mapping[str, Any]) -> bool:
    node_type = str(node.get("Node Type") or "").strip().lower()
    provider = "".join(ch for ch in str(node.get("Custom Plan Provider") or "").lower() if ch.isalnum())
    return node_type == "custom scan" and provider == "decompresschunk"


def _is_access_node(node: Mapping[str, Any]) -> bool:
    return _is_index_access(node) or _is_decompress_chunk(node) or _is_seq_scan(node)


def _is_relevant_relation(
    node: Mapping[str, Any],
    *,
    candidates: Sequence[str],
    expected_relations: Sequence[str],
) -> bool:
    name = _relation_basename(node)
    if name in expected_relations:
        return True
    if not name:
        return False
    try:
        return normalize_candidate_chunk_name(name) in set(candidates)
    except PgdataWorkloadError:
        return False


def _actual_rows(node: Mapping[str, Any]) -> int | None:
    for key in ("Actual Rows", "actual_rows"):
        parsed = _shared_read_blocks_value(node.get(key))
        if parsed is not None:
            return parsed
    return None


def _rows_removed(node: Mapping[str, Any]) -> int:
    total = 0
    for key in ("Rows Removed by Filter", "Rows Removed by Index Recheck"):
        parsed = _shared_read_blocks_value(node.get(key))
        if parsed is not None:
            total += parsed
    return total


def _actual_loops(node: Mapping[str, Any]) -> int:
    parsed = _shared_read_blocks_value(node.get("Actual Loops") or node.get("actual_loops"))
    return parsed if parsed is not None and parsed > 0 else 1


def _node_predicate_raw(node: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in ("Index Cond", "Filter", "Recheck Cond", "Chunk Quals", "Chunk Qual"):
        value = node.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts)


def _parse_plan_instant(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    if _OFFSET_HOURS_RE.search(text):
        text += ":00"
    if "T" not in text[:19] and " " in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _node_has_time_bound(node: Mapping[str, Any]) -> bool:
    return _TIME_PREDICATE_RE.search(_node_predicate_raw(node)) is not None


def _node_time_bound_matches(node: Mapping[str, Any], start_at: datetime, end_at: datetime) -> bool:
    start_ok = False
    end_ok = False
    matches = list(_TIME_PREDICATE_RE.finditer(_node_predicate_raw(node)))
    if not matches:
        return False
    for match in matches:
        column = match.group(1).lower()
        op = match.group(2)
        instant = _parse_plan_instant(match.group(3))
        if instant is None:
            return False
        recognized = False
        if op in {">=", "="} and instant == start_at and (column == "valid_time" or column.startswith("_ts_meta_max")):
            start_ok = True
            recognized = True
        if op in {"<=", "="} and instant == end_at and (column == "valid_time" or column.startswith("_ts_meta_min")):
            end_ok = True
            recognized = True
        if not recognized:
            return False
    return start_ok and end_ok


def _initplan_segment_params(nodes: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, str | None]]:
    found: dict[str, tuple[str, str | None]] = {}
    for node in nodes:
        subplan = str(node.get("Subplan Name") or "")
        returned = _INITPLAN_RETURNS_RE.search(subplan)
        if returned is None:
            continue
        if str(node.get("Parent Relationship") or "").strip().lower() != "initplan":
            continue
        if _relation_basename(node) != "river_segment":
            continue
        raw = _node_predicate_raw(node)
        segment = _SEGMENT_ID_EQ_RE.search(raw)
        if segment is None:
            continue
        network = _NETWORK_ID_EQ_RE.search(raw)
        found[returned.group(1)] = (segment.group(1), network.group(1) if network else None)
    return found


def _segment_key_params(node: Mapping[str, Any]) -> tuple[str, ...]:
    params: list[str] = []
    for match in _SEGMENT_KEY_PARAM_RE.finditer(_node_predicate_raw(node)):
        params.append(match.group(1) or match.group(2))
    return tuple(params)


def _segment_identity_bound(
    node: Mapping[str, Any],
    *,
    token: str,
    network_token: str,
    initplans: Mapping[str, tuple[str, str | None]],
) -> bool:
    if token in _index_cond_text(node):
        return True
    for param in _segment_key_params(node):
        identity = initplans.get(param)
        if identity is None:
            continue
        segment_id, network_id = identity
        if segment_id.casefold() != token:
            continue
        if network_token and (network_id is None or network_id.casefold() != network_token):
            continue
        return True
    return False


def load_candidate_relations(
    execute: Any,
    *,
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    """Load window-overlapping origin and physical compressed identities."""

    try:
        rows = list(execute(CANDIDATE_CHUNKS_SQL, {"window_start": window_start, "window_end": window_end}))
    except PgdataWorkloadError:
        raise
    except Exception:
        refuse("candidate discovery query failed", code="PLAN_CANDIDATES_FAILED", stage="plan")
    if len(rows) > MAX_CANDIDATES:
        refuse("candidate chunk set exceeds the bound", code="PLAN_CANDIDATES_BOUND", stage="plan")
    origins: list[str] = []
    compressed: list[str] = []
    allowed: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            refuse("candidate discovery row is not a mapping", code="PLAN_CANDIDATE_ROW_INVALID", stage="plan")
        parent = (str(row.get("hypertable_schema") or ""), str(row.get("hypertable_name") or ""))
        if parent not in {NARROW_HYPERTABLE, LEGACY_HYPERTABLE}:
            refuse("candidate belongs to an unrelated hypertable", code="PLAN_UNRELATED_PARENT", stage="plan")
        origin = normalize_candidate_chunk_name(row.get("chunk_name"))
        if origin in seen:
            refuse("intersecting chunk identity is duplicated", code="PLAN_CANDIDATE_DUPLICATE", stage="plan")
        seen.add(origin)
        origins.append(origin)
        allowed.append(origin)
        compressed_name = str(row.get("compressed_name") or "").strip()
        if compressed_name:
            physical = normalize_candidate_chunk_name(compressed_name)
            if physical not in seen:
                seen.add(physical)
                compressed.append(physical)
                allowed.append(physical)
    if not origins:
        refuse("representative query has no candidate chunks", code="PLAN_CANDIDATES_EMPTY", stage="plan")
    return {
        "origin_chunk_names": tuple(origins),
        "compressed_chunk_names": tuple(compressed),
        "candidate_chunk_names": tuple(allowed),
        "candidate_count": len(allowed),
    }


def evaluate_explain_json_plan(
    payload: Any,
    *,
    candidate_chunk_names: Sequence[str],
    expected_relations: Sequence[str] = ("river_timeseries", "river_timeseries_legacy"),
    buffer_limit: int = PLAN_BUFFER_LIMIT,
    require_segment_bound: bool = True,
    segment_id: str | None = None,
    river_network_version_id: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    allow_empty_rows: bool = False,
    filter_ratio_limit: int = 10,
) -> dict[str, Any]:
    """Parse FORMAT JSON structurally for the shipping explicit-cycle path."""

    payload = bound_explain_payload(payload)
    if isinstance(payload, list):
        if not payload or not isinstance(payload[0], Mapping):
            refuse("EXPLAIN JSON is empty", code="PLAN_JSON_INVALID", stage="plan")
        root = payload[0]
    elif isinstance(payload, Mapping):
        root = payload
    else:
        refuse("EXPLAIN JSON is not an object", code="PLAN_JSON_INVALID", stage="plan")
    plan = root.get("Plan") if "Plan" in root else root
    if not isinstance(plan, Mapping):
        refuse("EXPLAIN JSON has no plan nodes", code="PLAN_JSON_INVALID", stage="plan")
    nodes = _walk_plan_nodes(plan)
    if not nodes:
        refuse("EXPLAIN JSON has no plan nodes", code="PLAN_JSON_INVALID", stage="plan")
    candidates = tuple(dict.fromkeys(normalize_candidate_chunk_name(name) for name in candidate_chunk_names))
    if not candidates:
        refuse("representative query has no candidate chunks", code="PLAN_CANDIDATES_EMPTY", stage="plan")
    candidate_set = set(candidates)
    expected = tuple(expected_relations)
    for node in nodes:
        if not _is_seq_scan(node):
            continue
        if _is_relevant_relation(node, candidates=candidates, expected_relations=expected):
            refuse("Seq Scan on the representative fact path", code="PLAN_SEQ_SCAN", stage="plan")
    decompressed: list[str] = []
    touched: list[str] = []
    for node in nodes:
        relation = _relation_basename(node)
        schema = str(node.get("Schema") or "")
        if relation and schema in {"", "_timescaledb_internal", "hydro"}:
            try:
                name = normalize_candidate_chunk_name(relation)
            except PgdataWorkloadError:
                name = ""
            if name and (name.startswith("_hyper_") or name in candidate_set):
                if name not in candidate_set:
                    refuse("plan touched an unrelated chunk", code="PLAN_UNRELATED_CHUNK", stage="plan")
                touched.append(name)
                if _is_decompress_chunk(node):
                    decompressed.append(name)
    unique_decompressed = tuple(dict.fromkeys(decompressed))
    unique_touched = tuple(dict.fromkeys(touched))
    if window_start and window_end:
        start_at = _parse_plan_instant(window_start)
        end_at = _parse_plan_instant(window_end)
        if start_at is None or end_at is None:
            refuse("plan scanned an out-of-window chunk", code="PLAN_OUT_OF_WINDOW", stage="plan")
        for node in nodes:
            if not _node_has_time_bound(node):
                continue
            relation = _relation_basename(node)
            relevant = _is_relevant_relation(node, candidates=candidates, expected_relations=expected)
            if (
                not relevant
                and "chunk" not in str(node.get("Node Type") or "").lower()
                and relation not in candidate_set
            ):
                continue
            if not _node_time_bound_matches(node, start_at, end_at):
                refuse("plan scanned an out-of-window chunk", code="PLAN_OUT_OF_WINDOW", stage="plan")
    if require_segment_bound:
        token = str(segment_id or "").strip().lower()
        if not token:
            refuse("segment-bound access is missing from the plan identity", code="PLAN_SEGMENT_UNBOUND", stage="plan")
        network_token = str(river_network_version_id or "").strip().lower()
        initplans = _initplan_segment_params(nodes)
        bound = False
        for node in nodes:
            if not _is_relevant_relation(node, candidates=candidates, expected_relations=expected):
                continue
            if not (_is_index_access(node) or _is_decompress_chunk(node)):
                continue
            if _segment_identity_bound(node, token=token, network_token=network_token, initplans=initplans):
                bound = True
                break
        if not bound:
            refuse(
                "plan is missing a segment-bound index or decompress condition",
                code="PLAN_SEGMENT_UNBOUND",
                stage="plan",
            )
    actual_rows = _actual_rows(plan)
    if actual_rows is None:
        for node in nodes:
            actual_rows = _actual_rows(node)
            if actual_rows is not None:
                break
    if actual_rows is None:
        actual_rows = 0 if allow_empty_rows else 1
    if actual_rows == 0 and not allow_empty_rows:
        refuse("representative query returned no rows", code="PLAN_NO_ROWS", stage="plan")
    for node in nodes:
        if not _is_access_node(node):
            continue
        if not _is_relevant_relation(node, candidates=candidates, expected_relations=expected):
            continue
        returned = _actual_rows(node)
        if returned is None:
            continue
        loops = _actual_loops(node)
        removed_total = _rows_removed(node) * loops
        returned_total = returned * loops
        if returned_total <= 0:
            if not allow_empty_rows:
                refuse("relevant access node returned no rows", code="PLAN_NO_ROWS", stage="plan")
            continue
        if removed_total > returned_total * filter_ratio_limit:
            refuse("plan filter ratio exceeds the closed ceiling", code="PLAN_FILTER_RATIO", stage="plan")
    shared_hit = _root_shared_hit_blocks(plan)
    shared_read = _root_shared_read_blocks(plan)
    shared_buffers = shared_hit + shared_read
    if shared_buffers > buffer_limit:
        refuse("shared buffers exceed the 5000 ceiling", code="PLAN_BUFFERS_EXCEEDED", stage="plan")
    return {
        "expected_relations": list(expected),
        "candidate_count": len(candidates),
        "decompressed_count": len(unique_decompressed),
        "touched_count": len(unique_touched),
        "shared_hit_blocks": shared_hit,
        "shared_read_blocks": shared_read,
        "shared_buffer_blocks": shared_buffers,
        "actual_rows": actual_rows,
        "seq_scan": False,
        "all_chunk_decompression": False,
    }
