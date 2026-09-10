"""G2/G5 external-tablespace SQL and G7 EXPLAIN JSON plan parsing."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from packages.common.evidence_io import BoundedEvidenceError, validate_json_complexity
from packages.common.node27_issue1895_types import Issue1895ReadinessError

EXTERNAL_TABLESPACE_SQL = (
    "SELECT spcname, pg_tablespace_location(oid) AS location "
    "FROM pg_tablespace WHERE pg_tablespace_location(oid) <> '' "
    "ORDER BY location, spcname"
)

PLAN_BUFFER_LIMIT = 5000
PLAN_MAX_BYTES = 262144
PLAN_MAX_DEPTH = 48
PLAN_MAX_NODES = 10_000
PLAN_MAX_ARRAY_ITEMS = 1_000
REPRESENTATIVE_HYPERTABLE = ("hydro", "river_timeseries")
REPRESENTATIVE_WINDOW_DAYS = 7
CHUNK_NAME_RE = re.compile(r"^[_A-Za-z][_A-Za-z0-9]*$")
CANDIDATE_CHUNKS_SQL = (
    "SELECT chunk_name FROM timescaledb_information.chunks "
    "WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries' "
    "AND range_end > %(window_start)s::timestamptz "
    "AND range_start < (%(window_start)s::timestamptz + interval '7 days') "
    "ORDER BY range_start, chunk_name"
)


def assert_external_tablespace_sql(sql: str) -> None:
    """Require the location alias before ORDER BY; empty target sets stay legal."""

    normalized = " ".join(str(sql).split())
    if "pg_tablespace_location(oid) AS location" not in normalized:
        raise Issue1895ReadinessError(
            "external tablespace SQL must alias pg_tablespace_location(oid) AS location",
            code="SQL_LOCATION_ALIAS_MISSING",
            stage="sql",
        )
    order_at = normalized.find("ORDER BY location")
    alias_at = normalized.find("pg_tablespace_location(oid) AS location")
    if order_at < 0 or alias_at < 0 or alias_at > order_at:
        raise Issue1895ReadinessError(
            "ORDER BY location requires the location alias first",
            code="SQL_LOCATION_ORDER_INVALID",
            stage="sql",
        )


def parse_external_tablespace_rows(text: str, *, command_ok: bool) -> tuple[dict[str, str], ...]:
    """Parse ``spcname|location`` rows. Zero rows are legal; command failure is not."""

    if not command_ok:
        raise Issue1895ReadinessError(
            "external tablespace query failed",
            code="SQL_COMMAND_FAILED",
            stage="sql",
        )
    assert_external_tablespace_sql(EXTERNAL_TABLESPACE_SQL)
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        name, separator, location = line.partition("|")
        if not separator or not name or not location.startswith("/"):
            raise Issue1895ReadinessError(
                "external tablespace row is malformed",
                code="SQL_ROW_INVALID",
                stage="sql",
            )
        key = (name, location)
        if key in seen:
            raise Issue1895ReadinessError(
                "external tablespace row is duplicated",
                code="SQL_ROW_DUPLICATE",
                stage="sql",
            )
        seen.add(key)
        rows.append({"spcname": name, "location": location})
    return tuple(rows)


def normalize_candidate_chunk_name(name: object) -> str:
    """Accept only a bare Timescale chunk_name. Schema-qualified names refuse."""

    text = str(name or "").strip()
    if not text or "." in text or not CHUNK_NAME_RE.fullmatch(text):
        raise Issue1895ReadinessError(
            "candidate chunk name must be a bare identifier",
            code="PLAN_CANDIDATE_NAME_INVALID",
            stage="plan",
        )
    return text


def bound_explain_payload(payload: Any) -> Any:
    """Refuse oversized or overly nested EXPLAIN JSON before any node walk."""

    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise Issue1895ReadinessError(
            "EXPLAIN JSON is not serializable",
            code="PLAN_JSON_INVALID",
            stage="plan",
        ) from None
    if len(encoded) > PLAN_MAX_BYTES:
        raise Issue1895ReadinessError(
            "EXPLAIN JSON exceeds the serialized byte ceiling",
            code="PLAN_JSON_TOO_LARGE",
            stage="plan",
        )
    try:
        validate_json_complexity(
            payload,
            label="EXPLAIN plan",
            max_depth=PLAN_MAX_DEPTH,
            max_nodes=PLAN_MAX_NODES,
            max_array_items=PLAN_MAX_ARRAY_ITEMS,
        )
    except BoundedEvidenceError:
        raise Issue1895ReadinessError(
            "EXPLAIN JSON exceeds complexity bounds",
            code="PLAN_JSON_TOO_COMPLEX",
            stage="plan",
        ) from None
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
    """Query-level buffer counters live on the root Plan node.

    PostgreSQL FORMAT JSON reports Shared Hit/Read Blocks up the tree, so
    summing parent and child counters double-counts. A missing or invalid
    root counter is a closed failure; zero is a legal query total.
    """

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
    raise Issue1895ReadinessError(
        "root Plan shared buffer counter is missing or invalid",
        code="PLAN_BUFFERS_INVALID",
        stage="plan",
    )


def _root_shared_read_blocks(plan: Mapping[str, Any]) -> int:
    return _root_counter(plan, ("Shared Read Blocks", "shared_read_blocks"))


def _optional_root_counter(plan: Mapping[str, Any], keys: tuple[str, ...]) -> int:
    try:
        return _root_counter(plan, keys)
    except Issue1895ReadinessError:
        return 0


def _root_shared_hit_blocks(plan: Mapping[str, Any]) -> int:
    return _optional_root_counter(plan, ("Shared Hit Blocks", "shared_hit_blocks"))


def _root_shared_buffer_blocks(plan: Mapping[str, Any]) -> int:
    return _root_shared_hit_blocks(plan) + _root_shared_read_blocks(plan)


def _relation_basename(node: Mapping[str, Any]) -> str:
    return str(node.get("Relation Name") or "").split(".")[-1]


def _node_predicate_text(node: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in ("Index Cond", "Filter", "Recheck Cond", "Chunk Quals", "Chunk Qual"):
        value = node.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def _is_relevant_relation(
    node: Mapping[str, Any],
    *,
    candidates: Sequence[str],
    expected_relation: str,
) -> bool:
    name = _relation_basename(node)
    if name == expected_relation:
        return True
    if not name:
        return False
    try:
        return normalize_candidate_chunk_name(name) in set(candidates)
    except Issue1895ReadinessError:
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
    if parsed is None or parsed < 1:
        return 1
    return parsed


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


def evaluate_explain_json_plan(
    payload: Any,
    *,
    candidate_chunk_names: Sequence[str],
    expected_schema: str = REPRESENTATIVE_HYPERTABLE[0],
    expected_relation: str = REPRESENTATIVE_HYPERTABLE[1],
    buffer_limit: int = PLAN_BUFFER_LIMIT,
    require_segment_bound: bool | None = None,
    segment_id: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    allow_empty_rows: bool = False,
    filter_ratio_limit: int = 10,
    lane_kind: str | None = None,
) -> dict[str, Any]:
    """Parse FORMAT JSON structurally for the product curve path.

    Normal DecompressChunk of the query's own compressed candidate is allowed.
    Seq Scan is banned only on the relevant fact path. Root Shared Hit+Read
    is the query-level buffer total; parent+child sums are never used.
    """

    payload = bound_explain_payload(payload)
    if isinstance(payload, list):
        if not payload or not isinstance(payload[0], Mapping):
            raise Issue1895ReadinessError(
                "EXPLAIN JSON is empty",
                code="PLAN_JSON_INVALID",
                stage="plan",
            )
        root = payload[0]
    elif isinstance(payload, Mapping):
        root = payload
    else:
        raise Issue1895ReadinessError(
            "EXPLAIN JSON is not an object",
            code="PLAN_JSON_INVALID",
            stage="plan",
        )
    plan = root.get("Plan") if "Plan" in root else root
    if not isinstance(plan, Mapping):
        raise Issue1895ReadinessError(
            "EXPLAIN JSON has no plan nodes",
            code="PLAN_JSON_INVALID",
            stage="plan",
        )
    nodes = _walk_plan_nodes(plan)
    if not nodes:
        raise Issue1895ReadinessError(
            "EXPLAIN JSON has no plan nodes",
            code="PLAN_JSON_INVALID",
            stage="plan",
        )
    candidates = tuple(dict.fromkeys(normalize_candidate_chunk_name(name) for name in candidate_chunk_names))
    if not candidates:
        raise Issue1895ReadinessError(
            "representative query has no candidate chunks",
            code="PLAN_CANDIDATES_EMPTY",
            stage="plan",
        )
    candidate_set = set(candidates)
    for node in nodes:
        if not _is_seq_scan(node):
            continue
        if _is_relevant_relation(node, candidates=candidates, expected_relation=expected_relation):
            raise Issue1895ReadinessError(
                "Seq Scan on the representative fact path",
                code="PLAN_SEQ_SCAN",
                stage="plan",
            )
    decompressed: list[str] = []
    touched: list[str] = []
    for node in nodes:
        relation = _relation_basename(node)
        schema = str(node.get("Schema") or "")
        if relation and schema in {"", "_timescaledb_internal", expected_schema}:
            try:
                name = normalize_candidate_chunk_name(relation)
            except Issue1895ReadinessError:
                name = ""
            if name and name.startswith("_hyper_"):
                if name not in candidate_set:
                    raise Issue1895ReadinessError(
                        "plan touched an unrelated chunk",
                        code="PLAN_UNRELATED_CHUNK",
                        stage="plan",
                    )
                touched.append(name)
                if _is_decompress_chunk(node):
                    decompressed.append(name)
    unique_decompressed = tuple(dict.fromkeys(decompressed))
    unique_touched = tuple(dict.fromkeys(touched))
    if window_start and window_end:
        start_token = str(window_start).replace("+00:00", "Z")
        end_token = str(window_end).replace("+00:00", "Z")
        for node in nodes:
            predicate = _node_predicate_text(node)
            if not predicate:
                continue
            if "valid_time" in predicate and start_token not in predicate and end_token not in predicate:
                if "chunk" in str(node.get("Node Type") or "").lower() or _relation_basename(node) in candidate_set:
                    raise Issue1895ReadinessError(
                        "plan scanned an out-of-window chunk",
                        code="PLAN_OUT_OF_WINDOW",
                        stage="plan",
                    )
    kind = str(lane_kind or "").strip().lower() or None
    if require_segment_bound is None:
        require_segment_bound = bool(str(segment_id or "").strip())
    if require_segment_bound:
        token = str(segment_id or "").strip().lower()
        if not token:
            raise Issue1895ReadinessError(
                "segment-bound access is missing from the plan identity",
                code="PLAN_SEGMENT_UNBOUND",
                stage="plan",
            )
        bound = False
        if kind == "cold" and token in _index_cond_text(plan) and unique_decompressed:
            bound = True
        for node in nodes:
            if not _is_relevant_relation(node, candidates=candidates, expected_relation=expected_relation):
                continue
            cond = _index_cond_text(node)
            if token not in cond:
                continue
            if kind == "cold":
                if _is_decompress_chunk(node) or _is_index_access(node):
                    bound = True
                    break
            elif kind == "hot":
                if _is_index_access(node):
                    bound = True
                    break
            else:
                if _is_index_access(node) or _is_decompress_chunk(node):
                    bound = True
                    break
        if not bound:
            raise Issue1895ReadinessError(
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
        raise Issue1895ReadinessError(
            "representative query returned no rows",
            code="PLAN_NO_ROWS",
            stage="plan",
        )
    for node in nodes:
        if not _is_access_node(node):
            continue
        if not _is_relevant_relation(node, candidates=candidates, expected_relation=expected_relation):
            continue
        returned = _actual_rows(node)
        if returned is None:
            continue
        loops = _actual_loops(node)
        removed_total = _rows_removed(node) * loops
        returned_total = returned * loops
        if returned_total <= 0:
            if not allow_empty_rows:
                raise Issue1895ReadinessError(
                    "relevant access node returned no rows",
                    code="PLAN_NO_ROWS",
                    stage="plan",
                )
            continue
        if removed_total > returned_total * filter_ratio_limit:
            raise Issue1895ReadinessError(
                "plan filter ratio exceeds the closed ceiling",
                code="PLAN_FILTER_RATIO",
                stage="plan",
            )
    shared_hit = _root_shared_hit_blocks(plan)
    shared_read = _root_shared_read_blocks(plan)
    shared_buffers = shared_hit + shared_read
    if shared_buffers > buffer_limit:
        raise Issue1895ReadinessError(
            "shared buffers exceed the #1342 ceiling",
            code="PLAN_BUFFERS_EXCEEDED",
            stage="plan",
        )
    return {
        "expected_schema": expected_schema,
        "expected_relation": expected_relation,
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
