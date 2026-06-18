from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.redaction import redact_payload, redact_text

TARGET_SCHEMA = "flood"
TARGET_TABLE = "return_period_result"
REPORT_SCHEMA = "nhms.flood.return_period_index_audit.v1"
MANUAL_SQL_SCHEMA = "nhms.flood.return_period_index_maintenance.manual_sql.v1"

ROOT_RELATION_SIZE_SQL = """
SELECT
  n.nspname AS table_schema,
  c.relname AS table_name,
  pg_relation_size(c.oid) AS table_bytes,
  pg_indexes_size(c.oid) AS indexes_bytes,
  pg_total_relation_size(c.oid) AS total_bytes,
  pg_size_pretty(pg_relation_size(c.oid)) AS table_size,
  pg_size_pretty(pg_indexes_size(c.oid)) AS indexes_size,
  pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'flood'
  AND c.relname = 'return_period_result';
""".strip()

INDEX_INVENTORY_SQL = """
SELECT
  idx.indexrelid::regclass::text AS index_name,
  ic.relname AS indexrelname,
  pg_get_indexdef(idx.indexrelid) AS indexdef,
  idx.indisprimary AS is_primary,
  idx.indisunique AS is_unique,
  idx.indisvalid AS is_valid,
  idx.indisready AS is_ready,
  idx.indpred IS NOT NULL AS is_partial,
  pg_get_expr(idx.indpred, idx.indrelid) AS predicate,
  pg_relation_size(idx.indexrelid) AS index_bytes,
  pg_size_pretty(pg_relation_size(idx.indexrelid)) AS index_size
FROM pg_index idx
JOIN pg_class tc ON tc.oid = idx.indrelid
JOIN pg_namespace tn ON tn.oid = tc.relnamespace
JOIN pg_class ic ON ic.oid = idx.indexrelid
WHERE tn.nspname = 'flood'
  AND tc.relname = 'return_period_result'
ORDER BY pg_relation_size(idx.indexrelid) DESC, ic.relname;
""".strip()

INDEX_USAGE_SQL = """
SELECT
  schemaname AS table_schema,
  relname AS table_name,
  indexrelname AS index_name,
  idx_scan,
  idx_tup_read,
  idx_tup_fetch
FROM pg_stat_user_indexes
WHERE schemaname = 'flood'
  AND relname = 'return_period_result'
ORDER BY idx_scan ASC, indexrelname;
""".strip()

TIMESCALE_CHUNK_SIZE_SQL = """
SELECT
  chunk_schema,
  chunk_name,
  range_start,
  range_end,
  pg_relation_size(format('%I.%I', chunk_schema, chunk_name)::regclass) AS chunk_table_bytes,
  pg_indexes_size(format('%I.%I', chunk_schema, chunk_name)::regclass) AS chunk_indexes_bytes,
  pg_total_relation_size(format('%I.%I', chunk_schema, chunk_name)::regclass) AS chunk_total_bytes,
  pg_size_pretty(pg_total_relation_size(format('%I.%I', chunk_schema, chunk_name)::regclass)) AS chunk_total_size
FROM timescaledb_information.chunks
WHERE hypertable_schema = 'flood'
  AND hypertable_name = 'return_period_result'
ORDER BY pg_total_relation_size(format('%I.%I', chunk_schema, chunk_name)::regclass) DESC
LIMIT 200;
""".strip()

TIMESCALE_CHUNK_INDEX_SIZE_SQL = """
SELECT
  chunks.chunk_schema,
  chunks.chunk_name,
  indexes.relname AS chunk_index_name,
  pg_relation_size(indexes.oid) AS chunk_index_bytes,
  pg_size_pretty(pg_relation_size(indexes.oid)) AS chunk_index_size,
  pg_get_indexdef(indexes.oid) AS chunk_indexdef
FROM timescaledb_information.chunks chunks
JOIN pg_class chunk_table
  ON chunk_table.oid = format('%I.%I', chunks.chunk_schema, chunks.chunk_name)::regclass
JOIN pg_index pgidx ON pgidx.indrelid = chunk_table.oid
JOIN pg_class indexes ON indexes.oid = pgidx.indexrelid
WHERE chunks.hypertable_schema = 'flood'
  AND chunks.hypertable_name = 'return_period_result'
ORDER BY pg_relation_size(indexes.oid) DESC, chunks.chunk_schema, chunks.chunk_name, indexes.relname
LIMIT 500;
""".strip()

PRE_POST_SIZE_EVIDENCE_SQL = """
SELECT current_database() AS database_name, pg_size_pretty(pg_database_size(current_database())) AS database_size;

SELECT
  pg_size_pretty(pg_relation_size('flood.return_period_result'::regclass)) AS table_size,
  pg_size_pretty(pg_indexes_size('flood.return_period_result'::regclass)) AS indexes_size,
  pg_size_pretty(pg_total_relation_size('flood.return_period_result'::regclass)) AS total_size;

SELECT
  idx.indexrelid::regclass::text AS index_name,
  pg_size_pretty(pg_relation_size(idx.indexrelid)) AS index_size,
  pg_get_indexdef(idx.indexrelid) AS indexdef
FROM pg_index idx
WHERE idx.indrelid = 'flood.return_period_result'::regclass
ORDER BY pg_relation_size(idx.indexrelid) DESC;
""".strip()


@dataclass(frozen=True)
class KnownIndex:
    migration: str
    decision: str
    hot_paths: tuple[str, ...]
    reason: str
    replacement: str | None = None


@dataclass(frozen=True)
class ProbeInputs:
    run_id: str = "sample-run-id"
    duration: str = "1h"
    valid_time: str = "2026-06-18T00:00:00Z"
    basin_version_id: str = "sample-basin-version"
    river_network_version_id: str = "sample-river-network-version"
    segment_id: str = "sample-segment"
    min_lon: float = 90.0
    min_lat: float = 30.0
    max_lon: float = 110.0
    max_lat: float = 40.0
    limit: int = 200


KNOWN_INDEXES: dict[str, KnownIndex] = {
    "return_period_result_pkey": KnownIndex(
        migration="000015/000017",
        decision="keep",
        hot_paths=("identity", "writer_upsert", "timeline", "map"),
        reason="Primary key enforces versioned return-period identity; do not drop during index bloat cleanup.",
    ),
    "return_period_result_summary_idx": KnownIndex(
        migration="000015",
        decision="keep",
        hot_paths=("flood-alert summary",),
        reason="Supports run/max_over_window/usable warning-level grouping used by summary counts.",
    ),
    "return_period_result_ranking_idx": KnownIndex(
        migration="000015",
        decision="keep",
        hot_paths=("ranking/segments",),
        reason="Supports ordered peak ranking and segment list without a valid_time predicate.",
    ),
    "return_period_result_valid_time_ranking_idx": KnownIndex(
        migration="000015",
        decision="keep",
        hot_paths=("ranking/segments",),
        reason="Supports ranking and segment list when the route filters a concrete valid_time.",
    ),
    "return_period_result_timeline_idx": KnownIndex(
        migration="000015",
        decision="keep",
        hot_paths=("timeline",),
        reason="Supports per-segment timeline lookup by run/network/segment/max_over_window/valid_time.",
    ),
    "return_period_result_map_idx": KnownIndex(
        migration="000015",
        decision="keep",
        hot_paths=("GeoJSON fallback tile",),
        reason="Supports bounded GeoJSON fallback lookup by run/duration/valid_time/max_over_window.",
    ),
    "return_period_result_valid_time_discovery_idx": KnownIndex(
        migration="000020",
        decision="keep",
        hot_paths=("valid-time discovery",),
        reason="Supports return-period valid-time discovery ordered by valid_time DESC.",
    ),
    "return_period_result_mvt_selected_identity_lookup_idx": KnownIndex(
        migration="000021",
        decision="keep",
        hot_paths=("MVT selected identity",),
        reason="Supports canonical MVT row lookup by selected run/basin/network/duration/time/segment identity.",
    ),
    "return_period_result_mvt_selected_identity_valid_time_discovery_idx": KnownIndex(
        migration="000021",
        decision="keep",
        hot_paths=("valid-time discovery", "MVT selected identity"),
        reason="Supports selected-identity valid-time discovery for MVT layers.",
    ),
    "return_period_result_run_quality_idx": KnownIndex(
        migration="000031",
        decision="investigate",
        hot_paths=("latest-ready-run quality behavior", "legacy quality fallback"),
        reason=(
            "Historically supported run-quality discovery on return_period_result; after 000034/000036 "
            "quality should prefer flood.run_product_quality, so keep only if EXPLAIN evidence still needs it."
        ),
        replacement="flood.run_product_quality quality joins and hydro_run_latest_ready_run_idx where available",
    ),
}

MIGRATION_NOTES: tuple[dict[str, Any], ...] = (
    {
        "migration": "000015",
        "scope": "return_period_result identity and API hot-path indexes",
        "known_indexes": [
            "return_period_result_summary_idx",
            "return_period_result_ranking_idx",
            "return_period_result_valid_time_ranking_idx",
            "return_period_result_timeline_idx",
            "return_period_result_map_idx",
        ],
    },
    {
        "migration": "000020",
        "scope": "valid-time discovery",
        "known_indexes": ["return_period_result_valid_time_discovery_idx"],
    },
    {
        "migration": "000021",
        "scope": "MVT selected identity lookup and selected-identity valid-time discovery",
        "known_indexes": [
            "return_period_result_mvt_selected_identity_lookup_idx",
            "return_period_result_mvt_selected_identity_valid_time_discovery_idx",
        ],
    },
    {
        "migration": "000031",
        "scope": "search/discovery performance and legacy return-period quality lookup",
        "known_indexes": ["return_period_result_run_quality_idx"],
    },
    {
        "migration": "000034",
        "scope": "flood.run_product_quality materialization",
        "known_indexes": [],
        "note": (
            "No flood.return_period_result index is added by 000034; this migration shifts latest-ready-run "
            "quality behavior toward flood.run_product_quality evidence."
        ),
    },
)

NULL_PARTIAL_INDEX_NAMES = {
    "return_period_result_null_return_period_run_idx",
    "return_period_result_null_warning_level_run_idx",
}


class ReturnPeriodIndexAuditError(RuntimeError):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def normalize_index_name(index_name: object) -> str:
    text = str(index_name or "").strip()
    if "." in text:
        text = text.rsplit(".", maxsplit=1)[-1]
    return text.strip('"')


def classify_index(row: Mapping[str, Any]) -> dict[str, Any]:
    name = normalize_index_name(row.get("indexrelname") or row.get("index_name"))
    indexdef = str(row.get("indexdef") or "")
    predicate = str(row.get("predicate") or "")
    definition_text = f"{indexdef}\n{predicate}".lower()
    is_null_partial = name in NULL_PARTIAL_INDEX_NAMES or (
        bool(row.get("is_partial"))
        and ("return_period is null" in definition_text or "warning_level is null" in definition_text)
    )
    if is_null_partial:
        return {
            "index_name": name,
            "decision": "investigate",
            "operator_candidate": "drop",
            "priority": "high",
            "migration": "legacy/unknown",
            "hot_paths": [],
            "reason": (
                "NULL-oriented partial index is not part of the documented summary, ranking, timeline, map, "
                "MVT, valid-time, or run-quality hot paths. Treat as a drop candidate only after before/after "
                "EXPLAIN evidence confirms no local workload still depends on it."
            ),
        }
    known = KNOWN_INDEXES.get(name)
    if known is not None:
        return {
            "index_name": name,
            "decision": known.decision,
            "operator_candidate": None,
            "priority": "normal" if known.decision == "keep" else "review",
            "migration": known.migration,
            "hot_paths": list(known.hot_paths),
            "reason": known.reason,
            "replacement": known.replacement,
        }
    return {
        "index_name": name,
        "decision": "investigate",
        "operator_candidate": None,
        "priority": "review",
        "migration": "unknown",
        "hot_paths": [],
        "reason": "Index is not in the approved known-index map; require catalog definition and EXPLAIN evidence.",
    }


def classify_indexes(index_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [classify_index(row) for row in index_rows]


def generate_hot_path_probes(inputs: ProbeInputs | None = None) -> list[dict[str, Any]]:
    probe_inputs = inputs or ProbeInputs()
    sample_bindings = redact_payload(
        {
            "run_id": probe_inputs.run_id,
            "duration": probe_inputs.duration,
            "valid_time": probe_inputs.valid_time,
            "basin_version_id": probe_inputs.basin_version_id,
            "river_network_version_id": probe_inputs.river_network_version_id,
            "segment_id": probe_inputs.segment_id,
            "min_lon": probe_inputs.min_lon,
            "min_lat": probe_inputs.min_lat,
            "max_lon": probe_inputs.max_lon,
            "max_lat": probe_inputs.max_lat,
            "limit": probe_inputs.limit,
            "usable_flags": ["ok", "partial_sample", "monotonicity_corrected"],
        }
    )
    probes = [
        (
            "flood-alert-summary",
            "flood-alert summary",
            "Warning-level summary plus segment and usable-curve counts.",
            """
EXPLAIN (ANALYZE, BUFFERS)
SELECT warning_level, COUNT(*) AS count
FROM flood.return_period_result
WHERE run_id = :run_id
  AND max_over_window = :max_over_window
  AND (:min_return_period IS NULL OR return_period >= :min_return_period)
  AND quality_flag = ANY(:usable_flags)
  AND warning_level IS NOT NULL
GROUP BY warning_level;

EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*) AS count
FROM (
  SELECT river_network_version_id, river_segment_id
  FROM flood.return_period_result
  WHERE run_id = :run_id
    AND max_over_window = :max_over_window
  GROUP BY river_network_version_id, river_segment_id
) AS versioned_segments;

EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*) AS count
FROM (
  SELECT river_network_version_id, river_segment_id
  FROM flood.return_period_result
  WHERE run_id = :run_id
    AND max_over_window = :max_over_window
    AND quality_flag = ANY(:usable_flags)
  GROUP BY river_network_version_id, river_segment_id
) AS versioned_segments;
""".strip(),
        ),
        (
            "ranking-segments",
            "ranking/segments",
            "Count and ordered peak ranking/segment list, including optional valid_time and warning filters.",
            """
EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*) AS count
FROM flood.return_period_result r
WHERE r.run_id = :run_id
  AND (:valid_time IS NULL OR r.valid_time = :valid_time)
  AND r.max_over_window = :max_over_window
  AND (:min_return_period IS NULL OR r.return_period >= :min_return_period)
  AND (:warning_levels_empty OR r.warning_level = ANY(:warning_levels))
  AND r.quality_flag = ANY(:usable_flags);

EXPLAIN (ANALYZE, BUFFERS)
SELECT r.river_segment_id, r.basin_version_id, r.q_value, r.return_period,
       r.warning_level, r.duration, r.valid_time, r.river_network_version_id
FROM flood.return_period_result r
WHERE r.run_id = :run_id
  AND (:valid_time IS NULL OR r.valid_time = :valid_time)
  AND r.max_over_window = :max_over_window
  AND (:min_return_period IS NULL OR r.return_period >= :min_return_period)
  AND (:warning_levels_empty OR r.warning_level = ANY(:warning_levels))
  AND r.quality_flag = ANY(:usable_flags)
ORDER BY r.return_period DESC NULLS LAST, r.q_value DESC, r.river_network_version_id, r.river_segment_id,
         r.valid_time
LIMIT :limit OFFSET :offset;
""".strip(),
        ),
        (
            "timeline",
            "timeline",
            "Segment timeline lookup and fallback ordered by valid_time.",
            """
EXPLAIN (ANALYZE, BUFFERS)
SELECT river_segment_id, valid_time, q_value, return_period, warning_level, model_id,
       river_network_version_id, basin_version_id, duration
FROM flood.return_period_result
WHERE run_id = :run_id
  AND river_segment_id = :segment_id
  AND river_network_version_id = :river_network_version_id
  AND max_over_window = false
ORDER BY valid_time
LIMIT :limit;

EXPLAIN (ANALYZE, BUFFERS)
SELECT river_segment_id, valid_time, q_value, return_period, warning_level, model_id,
       river_network_version_id, basin_version_id, duration
FROM flood.return_period_result
WHERE run_id = :run_id
  AND river_segment_id = :segment_id
  AND river_network_version_id = :river_network_version_id
ORDER BY max_over_window, valid_time
LIMIT :limit;
""".strip(),
        ),
        (
            "geojson-fallback-tile",
            "GeoJSON fallback tile",
            "Bounded GeoJSON fallback selection with bbox prefilter.",
            """
EXPLAIN (ANALYZE, BUFFERS)
SELECT r.river_segment_id, r.basin_version_id, r.river_network_version_id, r.return_period,
       r.warning_level, r.q_value, r.q_unit, r.quality_flag
FROM flood.return_period_result r
JOIN core.river_segment rs
  ON rs.river_segment_id = r.river_segment_id
 AND rs.river_network_version_id = r.river_network_version_id
WHERE r.run_id = :run_id
  AND r.duration = :duration
  AND r.valid_time = :valid_time
  AND r.max_over_window = false
  AND (:return_period IS NULL OR r.return_period >= :return_period)
  AND rs.geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 4490)
ORDER BY r.river_network_version_id, r.river_segment_id
LIMIT :limit;
""".strip(),
        ),
        (
            "mvt-selected-identity",
            "MVT selected identity",
            "Canonical MVT source-row lookup for selected run/basin/network identity.",
            """
EXPLAIN (ANALYZE, BUFFERS)
SELECT r.river_network_version_id || '::' || r.river_segment_id AS feature_id,
       r.river_segment_id AS segment_id,
       r.river_segment_id,
       r.river_network_version_id,
       r.basin_version_id,
       r.q_value AS value,
       r.q_unit AS unit,
       r.quality_flag,
       r.return_period,
       r.warning_level,
       r.run_id,
       r.duration,
       r.valid_time
FROM flood.return_period_result r
JOIN core.river_segment rs
  ON rs.river_segment_id = r.river_segment_id
 AND rs.river_network_version_id = r.river_network_version_id
WHERE r.run_id = :run_id
  AND r.basin_version_id = :basin_version_id
  AND r.river_network_version_id = :river_network_version_id
  AND r.duration = :duration
  AND r.valid_time = :valid_time
  AND r.max_over_window = false;
""".strip(),
        ),
        (
            "valid-time-discovery",
            "valid-time discovery",
            "Selected and unselected return-period valid-time discovery ordered by latest first.",
            """
EXPLAIN (ANALYZE, BUFFERS)
SELECT DISTINCT valid_time
FROM flood.return_period_result
WHERE run_id = :run_id
  AND basin_version_id = :basin_version_id
  AND river_network_version_id = :river_network_version_id
  AND duration = :duration
  AND max_over_window = false
ORDER BY valid_time DESC
LIMIT :limit;

EXPLAIN (ANALYZE, BUFFERS)
SELECT DISTINCT valid_time
FROM flood.return_period_result
WHERE duration = :duration
  AND max_over_window = false
ORDER BY valid_time DESC
LIMIT :limit;
""".strip(),
        ),
        (
            "latest-ready-run-quality",
            "latest-ready-run quality behavior",
            "Latest-ready-run quality path should prefer flood.run_product_quality instead of scanning result rows.",
            """
EXPLAIN (ANALYZE, BUFFERS)
SELECT h.run_id, h.status, h.model_id, h.basin_version_id, h.source_id, h.cycle_time, h.updated_at,
       mi.river_network_version_id
FROM hydro.hydro_run h
LEFT JOIN core.model_instance mi ON mi.model_id = h.model_id
JOIN flood.run_product_quality product_quality ON product_quality.run_id = h.run_id
WHERE h.status IN ('frequency_done', 'published')
  AND product_quality.quality_state = 'ready'
ORDER BY h.cycle_time DESC, h.run_id DESC
LIMIT 1;

EXPLAIN (ANALYZE, BUFFERS)
SELECT EXISTS (
  SELECT 1
  FROM flood.return_period_result result
  WHERE result.run_id = :run_id
) AS legacy_missing_quality_table_fallback_only;
""".strip(),
        ),
    ]
    return [
        {
            "name": name,
            "hot_path": hot_path,
            "description": description,
            "parameterization": "SQL uses bind placeholders; sample values are evidence only and are not interpolated.",
            "sample_bindings": sample_bindings,
            "sql": sql,
        }
        for name, hot_path, description, sql in probes
    ]


def collect_catalog_evidence(connection: Any) -> dict[str, Any]:
    with connection.cursor() as cursor:
        root_relation = _execute_fetch_all(cursor, ROOT_RELATION_SIZE_SQL)
        index_inventory = _execute_fetch_all(cursor, INDEX_INVENTORY_SQL)
        index_usage = _execute_fetch_all(cursor, INDEX_USAGE_SQL)
        timescale_chunks = _execute_optional_fetch_all(cursor, TIMESCALE_CHUNK_SIZE_SQL)
        timescale_chunk_indexes = _execute_optional_fetch_all(cursor, TIMESCALE_CHUNK_INDEX_SIZE_SQL)
    return {
        "root_relation": {"available": True, "rows": root_relation, "sql": ROOT_RELATION_SIZE_SQL},
        "index_inventory": {"available": True, "rows": index_inventory, "sql": INDEX_INVENTORY_SQL},
        "index_usage": {"available": True, "rows": index_usage, "sql": INDEX_USAGE_SQL},
        "timescale_chunks": timescale_chunks,
        "timescale_chunk_indexes": timescale_chunk_indexes,
    }


def build_unavailable_catalog(reason: str) -> dict[str, Any]:
    safe_reason = redact_text(reason)
    return {
        "root_relation": {
            "available": False,
            "rows": [],
            "unavailable_reason": safe_reason,
            "sql": ROOT_RELATION_SIZE_SQL,
        },
        "index_inventory": {
            "available": False,
            "rows": [],
            "unavailable_reason": safe_reason,
            "sql": INDEX_INVENTORY_SQL,
        },
        "index_usage": {"available": False, "rows": [], "unavailable_reason": safe_reason, "sql": INDEX_USAGE_SQL},
        "timescale_chunks": {
            "available": False,
            "rows": [],
            "unavailable_reason": safe_reason,
            "sql": TIMESCALE_CHUNK_SIZE_SQL,
        },
        "timescale_chunk_indexes": {
            "available": False,
            "rows": [],
            "unavailable_reason": safe_reason,
            "sql": TIMESCALE_CHUNK_INDEX_SIZE_SQL,
        },
    }


def build_report(
    catalog: Mapping[str, Any],
    *,
    connection_mode: str,
    database_url: str | None = None,
    manual_artifact_requested: bool = False,
    probe_inputs: ProbeInputs | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    index_rows = _rows_from_section(catalog.get("index_inventory"))
    classifications = classify_indexes(index_rows)
    report = {
        "schema": REPORT_SCHEMA,
        "generated_at": (generated_at or datetime.now(UTC)).isoformat(),
        "target": {"schema": TARGET_SCHEMA, "table": TARGET_TABLE, "qualified_name": f"{TARGET_SCHEMA}.{TARGET_TABLE}"},
        "database": {"url": redact_text(database_url) if database_url else None},
        "connection_mode": connection_mode,
        "execution_guardrails": {
            "destructive_ddl_executed": False,
            "apply_mode_supported": False,
            "manual_artifact_requested": manual_artifact_requested,
            "warning": (
                "This audit workflow never executes DROP INDEX, REINDEX, VACUUM FULL, pg_repack, chunk rebuild, "
                "compression, or other destructive production DDL."
            ),
            "writer_mode_note": (
                "Writer/maintenance credentials do not bypass approval; this tool only emits evidence and manual SQL."
                if connection_mode in {"writer", "maintenance"}
                else "Readonly/audit credentials are sufficient for report generation and SQL template output."
            ),
        },
        "evidence": catalog,
        "classifications": classifications,
        "known_index_map": [
            {"index_name": name, **_known_index_to_dict(known)} for name, known in sorted(KNOWN_INDEXES.items())
        ],
        "migration_notes": list(MIGRATION_NOTES),
        "hot_path_probes": generate_hot_path_probes(probe_inputs),
        "pre_post_size_evidence_sql": PRE_POST_SIZE_EVIDENCE_SQL,
        "manual_maintenance": {
            "artifact_requested": manual_artifact_requested,
            "artifact_type": "SQL text only; operator must execute manually during an approved maintenance window.",
        },
    }
    return redact_payload(report)


def generate_manual_maintenance_sql(classifications: Sequence[Mapping[str, Any]] | None = None) -> str:
    drop_candidates = [
        str(item["index_name"])
        for item in classifications or []
        if item.get("operator_candidate") == "drop" or item.get("decision") == "drop"
    ]
    if not drop_candidates:
        drop_candidates = sorted(NULL_PARTIAL_INDEX_NAMES)
    candidate_sql = "\n".join(
        f"-- DROP INDEX IF EXISTS {TARGET_SCHEMA}.{_quote_identifier(index_name)};"
        for index_name in sorted(drop_candidates)
    )
    return f"""-- schema: {MANUAL_SQL_SCHEMA}
-- TARGET: {TARGET_SCHEMA}.{TARGET_TABLE}
-- DO NOT AUTO-EXECUTE.
-- This file is a manual maintenance-window planning artifact only.
-- Do not run it from application startup, migrations, CI, or the audit script.
-- Operator approval required: compare before/after size evidence and hot-path EXPLAIN output first.

\\set ON_ERROR_STOP on

-- 1. Capture BEFORE evidence. Save output with the audit report.
{PRE_POST_SIZE_EVIDENCE_SQL}

-- 2. Confirm no documented hot path depends on a candidate index.
-- Run every generated EXPLAIN (ANALYZE, BUFFERS) probe before and after each change:
--   flood-alert summary
--   ranking/segments
--   timeline
--   GeoJSON fallback tile
--   MVT selected identity
--   valid-time discovery
--   latest-ready-run quality behavior

-- 3. Maintenance-window DDL guidance.
-- Use a writer session only after approval. Keep lock_timeout short so the operation fails instead of blocking
-- production traffic indefinitely. DROP INDEX CONCURRENTLY and REINDEX CONCURRENTLY cannot run inside a
-- transaction block; ordinary DROP INDEX can run in a transaction but may take stronger locks.

-- Example transactional section for non-concurrent DDL. Review and uncomment one statement at a time.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30min';
SET LOCAL idle_in_transaction_session_timeout = '5min';

-- Candidate NULL partial indexes from pre-#488/#490 no-curve behavior. Leave commented until approved.
{candidate_sql}

COMMIT;

-- If lock_timeout or statement_timeout fires before COMMIT, run ROLLBACK, inspect pg_locks/pg_stat_activity,
-- and retry in a larger maintenance window. Do not keep retrying in a busy production window.
-- If a needed index was removed, recreate it from the audit report's pg_get_indexdef output or the original
-- migration SQL, then rerun the hot-path EXPLAIN probes before continuing.

-- 4. Space-recovery options are separate operator decisions.
-- REINDEX, VACUUM FULL, pg_repack, Timescale chunk rebuild, and compression can each take different locks and
-- have different rollback behavior. Choose one only after staging evidence confirms it is appropriate.

-- 5. Capture AFTER evidence using the same queries from step 1 and attach before/after output to the runbook.
{PRE_POST_SIZE_EVIDENCE_SQL}
""".strip() + "\n"


def write_output_file(
    path: Path,
    content: str,
    *,
    overwrite: bool = False,
    writer: Callable[[Path, str], None] | None = None,
) -> None:
    if path.exists() and not overwrite:
        raise ReturnPeriodIndexAuditError(
            "OUTPUT_EXISTS",
            f"Refusing to overwrite existing output path without --overwrite: {path}",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        if writer is not None:
            writer(temp_path, content)
        else:
            temp_path.write_text(content, encoding="utf-8")
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            if temp_path.exists():
                temp_path.unlink()
        finally:
            raise ReturnPeriodIndexAuditError(
                "OUTPUT_WRITE_FAILED",
                f"Failed to write output path {path}: {exc}",
            ) from exc


def render_report_json(report: Mapping[str, Any]) -> str:
    return json.dumps(redact_payload(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit flood.return_period_result index evidence and generate manual maintenance planning material. "
            "The command never executes destructive DDL."
        )
    )
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="PostgreSQL database URL.")
    parser.add_argument(
        "--connection-mode",
        choices=("readonly", "audit", "writer", "maintenance"),
        default="readonly",
        help="Declared operator connection mode; writer modes still generate evidence only.",
    )
    parser.add_argument("--report-out", type=Path, help="Write JSON audit report to this path.")
    parser.add_argument("--manual-sql-out", type=Path, help="Write guarded manual SQL artifact to this path.")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing report/manual SQL output paths.")
    parser.add_argument("--run-id", default=ProbeInputs.run_id, help="Sample run_id for probe binding evidence.")
    parser.add_argument("--duration", default=ProbeInputs.duration, help="Sample duration for probe binding evidence.")
    parser.add_argument(
        "--valid-time",
        default=ProbeInputs.valid_time,
        help="Sample valid_time for probe binding evidence.",
    )
    parser.add_argument("--basin-version-id", default=ProbeInputs.basin_version_id)
    parser.add_argument("--river-network-version-id", default=ProbeInputs.river_network_version_id)
    parser.add_argument("--segment-id", default=ProbeInputs.segment_id)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    probe_inputs = ProbeInputs(
        run_id=args.run_id,
        duration=args.duration,
        valid_time=args.valid_time,
        basin_version_id=args.basin_version_id,
        river_network_version_id=args.river_network_version_id,
        segment_id=args.segment_id,
    )
    catalog = _catalog_from_database_url(args.database_url)
    report = build_report(
        catalog,
        connection_mode=args.connection_mode,
        database_url=args.database_url,
        manual_artifact_requested=args.manual_sql_out is not None,
        probe_inputs=probe_inputs,
    )
    report_json = render_report_json(report)
    if args.report_out:
        write_output_file(args.report_out, report_json, overwrite=args.overwrite)
    else:
        print(report_json, end="")

    if args.manual_sql_out:
        manual_sql = generate_manual_maintenance_sql(report["classifications"])
        write_output_file(args.manual_sql_out, manual_sql, overwrite=args.overwrite)
    return 0


def _catalog_from_database_url(database_url: str | None) -> dict[str, Any]:
    if not database_url:
        return build_unavailable_catalog("DATABASE_URL not provided; generated templates only.")
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError as exc:
        return build_unavailable_catalog(f"psycopg2 unavailable: {exc}")
    try:
        with psycopg2.connect(database_url, cursor_factory=RealDictCursor) as connection:
            return collect_catalog_evidence(connection)
    except Exception as exc:  # pragma: no cover - live DB failures are environment-specific.
        return build_unavailable_catalog(redact_text(str(exc)))


def _execute_fetch_all(cursor: Any, sql: str) -> list[dict[str, Any]]:
    cursor.execute(sql)
    return [_row_to_dict(row) for row in cursor.fetchall()]


def _execute_optional_fetch_all(cursor: Any, sql: str) -> dict[str, Any]:
    try:
        return {"available": True, "rows": _execute_fetch_all(cursor, sql), "sql": sql}
    except Exception as exc:
        connection = getattr(cursor, "connection", None)
        rollback = getattr(connection, "rollback", None)
        if callable(rollback):
            rollback()
        return {"available": False, "rows": [], "unavailable_reason": redact_text(str(exc)), "sql": sql}


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    if hasattr(row, "_asdict"):
        return dict(row._asdict())
    return dict(row)


def _rows_from_section(section: object) -> list[dict[str, Any]]:
    if isinstance(section, Mapping):
        rows = section.get("rows")
        if isinstance(rows, Sequence) and not isinstance(rows, str | bytes | bytearray):
            return [_row_to_dict(row) for row in rows]
    return []


def _known_index_to_dict(known: KnownIndex) -> dict[str, Any]:
    return {
        "migration": known.migration,
        "decision": known.decision,
        "hot_paths": list(known.hot_paths),
        "reason": known.reason,
        "replacement": known.replacement,
    }


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReturnPeriodIndexAuditError as exc:
        print(f"{exc.error_code}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
