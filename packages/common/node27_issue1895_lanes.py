"""Four-lane GFS/IFS × hot/cold discovery for the #1895 product-curve oracle."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from packages.common.node27_issue1895_catalog import (
    COLD_STATE,
    HOT_STATE,
    MAX_LANE_CHUNKS,
    classify_complete_groups,
)
from packages.common.node27_issue1895_query import (
    iso_utc,
    parse_issue_time,
    record_explicit_cycle_curve,
    scenario_for_source,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError

LANE_NAMES: tuple[str, ...] = ("gfs_hot", "ifs_hot", "gfs_cold", "ifs_cold")
SOURCES: tuple[str, ...] = ("GFS", "IFS")
COLD_SEARCH_BOUND = 64
READY_RUN_STATUSES: tuple[str, ...] = ("succeeded", "parsed", "published")
IDENTITY_FIELDS: tuple[str, ...] = (
    "run_id",
    "model_id",
    "basin_id",
    "basin_version_id",
    "river_network_version_id",
    "source_id",
    "cycle_time",
)

HISTORICAL_IDENTITY_SQL = """
SELECT h.run_id, h.model_id, h.basin_version_id, h.source_id, h.cycle_time, h.status,
       mi.river_network_version_id, bv.basin_id
FROM hydro.hydro_run h
JOIN core.basin_version bv ON bv.basin_version_id = h.basin_version_id
JOIN core.model_instance mi ON mi.model_id = h.model_id
WHERE bv.basin_id = %s
  AND h.run_type = 'forecast'
  AND h.status IN ('succeeded', 'parsed', 'published')
  AND LOWER(h.source_id) = LOWER(%s)
  AND h.cycle_time IS NOT NULL
  AND h.run_id <> %s
ORDER BY h.cycle_time DESC, h.run_id DESC
LIMIT %s
"""

EXACT_IDENTITY_SQL = """
SELECT h.run_id, h.model_id, h.basin_version_id, h.source_id, h.cycle_time, h.status,
       mi.river_network_version_id, bv.basin_id
FROM hydro.hydro_run h
JOIN core.basin_version bv ON bv.basin_version_id = h.basin_version_id
JOIN core.model_instance mi ON mi.model_id = h.model_id
WHERE bv.basin_id = %s
  AND h.run_id = %s
  AND h.model_id = %s
  AND h.cycle_time = %s::timestamptz
  AND h.run_type = 'forecast'
  AND h.status IN ('succeeded', 'parsed', 'published')
  AND LOWER(h.source_id) = LOWER(%s)
LIMIT 2
"""

WINDOW_ROW_PROOF_SQL = """
SELECT COUNT(*) AS row_count
FROM hydro.river_timeseries rt
JOIN hydro.hydro_run h ON h.run_key = rt.run_key
WHERE h.run_id = %s
  AND h.model_id = %s
  AND rt.river_segment_id = %s
  AND rt.valid_time >= %s
  AND rt.valid_time <= %s
"""


def lane_source(name: str) -> str:
    if name not in LANE_NAMES:
        raise Issue1895ReadinessError(
            "lane name is closed",
            code="LANE_NAME_INVALID",
            stage="lanes",
        )
    return "GFS" if name.startswith("gfs_") else "IFS"


def lane_kind(name: str) -> str:
    if name not in LANE_NAMES:
        raise Issue1895ReadinessError(
            "lane name is closed",
            code="LANE_NAME_INVALID",
            stage="lanes",
        )
    return "hot" if name.endswith("_hot") else "cold"


def _text(value: object, *, code: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise Issue1895ReadinessError(
            f"{field} is missing",
            code=code,
            stage="lanes",
        )
    return text


def identity_from_row(row: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    status = str(row.get("status") or row.get("run_status") or "").strip()
    if status not in READY_RUN_STATUSES:
        raise Issue1895ReadinessError(
            "identity is not display-ready",
            code="LANE_STATUS_INVALID",
            stage="lanes",
        )
    cycle = parse_issue_time(row["cycle_time"] if "cycle_time" in row else row["issue_time"])
    observed_source = str(row.get("source_id") or source).strip().upper()
    if observed_source != source:
        raise Issue1895ReadinessError(
            "identity source drifted from the requested lane",
            code="LANE_SOURCE_MISMATCH",
            stage="lanes",
        )
    return {
        "run_id": _text(row.get("run_id"), code="LANE_IDENTITY_INVALID", field="run_id"),
        "model_id": _text(row.get("model_id"), code="LANE_IDENTITY_INVALID", field="model_id"),
        "basin_id": _text(row.get("basin_id"), code="LANE_IDENTITY_INVALID", field="basin_id"),
        "basin_version_id": _text(
            row.get("basin_version_id"),
            code="LANE_IDENTITY_INVALID",
            field="basin_version_id",
        ),
        "river_network_version_id": _text(
            row.get("river_network_version_id"),
            code="LANE_IDENTITY_INVALID",
            field="river_network_version_id",
        ),
        "source_id": source,
        "cycle_time": iso_utc(cycle),
        "run_status": status,
        "scenario": scenario_for_source(source),
    }


def identity_projection(identity: Mapping[str, Any]) -> dict[str, str]:
    return {field: str(identity[field]) for field in IDENTITY_FIELDS}


def assert_shared_network(identities: Sequence[Mapping[str, Any]]) -> None:
    if len(identities) < 2:
        return
    first = identities[0]
    for item in identities[1:]:
        for field in ("basin_id", "basin_version_id", "river_network_version_id"):
            if str(item.get(field) or "") != str(first.get(field) or ""):
                raise Issue1895ReadinessError(
                    "GFS/IFS identities do not share basin/version/network",
                    code="LANE_NETWORK_MISMATCH",
                    stage="lanes",
                )


def assert_state_for_kind(classified: Mapping[str, Any], *, kind: str) -> None:
    state = classified.get("state")
    if kind == "hot" and state != HOT_STATE:
        raise Issue1895ReadinessError(
            "hot lane is not uncompressed full source",
            code="LANE_HOT_NOT_SOURCE",
            stage="lanes",
        )
    if kind == "cold" and state != COLD_STATE:
        raise Issue1895ReadinessError(
            "cold lane is not compressed complete target",
            code="LANE_COLD_NOT_TARGET",
            stage="lanes",
        )


def bind_lane(
    *,
    name: str,
    identity: Mapping[str, Any],
    segment_id: str,
    classified: Mapping[str, Any],
) -> dict[str, Any]:
    kind = lane_kind(name)
    source = lane_source(name)
    assert_state_for_kind(classified, kind=kind)
    bound = record_explicit_cycle_curve(
        basin_version_id=str(identity["basin_version_id"]),
        segment_id=segment_id,
        river_network_version_id=str(identity["river_network_version_id"]),
        issue_time=str(identity["cycle_time"]),
        run_id=str(identity["run_id"]),
        model_id=str(identity["model_id"]),
        source=source,
    )
    return {
        "name": name,
        "kind": kind,
        "source": source,
        "state": classified["state"],
        "identity": dict(identity),
        "query": bound,
        "candidate_chunk_names": list(classified["candidate_chunk_names"]),
        "candidate_count": int(classified["candidate_count"]),
        "tablespace": classified["tablespace"],
        "compressed": bool(classified["compressed"]),
        "query_digest": bound["query_digest"],
    }


def freeze_lanes(lanes: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    if set(lanes) != set(LANE_NAMES):
        raise Issue1895ReadinessError(
            "frozen pin is missing the four closed lanes",
            code="LANE_SET_INVALID",
            stage="lanes",
        )
    frozen = {}
    for name in LANE_NAMES:
        lane = lanes[name]
        identity = lane["identity"]
        query = lane["query"]
        frozen[name] = {
            "run_id": identity["run_id"],
            "model_id": identity["model_id"],
            "basin_id": identity["basin_id"],
            "basin_version_id": identity["basin_version_id"],
            "river_network_version_id": identity["river_network_version_id"],
            "cycle_time": identity["cycle_time"],
            "source_id": identity.get("source_id") or lane.get("source"),
            "scenario": identity.get("scenario") or query["scenario"],
            "segment_id": query["segment_id"],
            "timeseries_segment_id": query["timeseries_segment_id"],
            "window_start": query["window_start"],
            "window_end": query["window_end"],
            "api_path": query["api_path"],
            "api_query": query["api_query"],
            "query_digest": lane["query_digest"],
            "state": lane["state"],
            "candidate_chunk_names": list(lane["candidate_chunk_names"]),
        }
    return frozen


def assert_frozen_unchanged(frozen: Mapping[str, Any], observed: Mapping[str, Mapping[str, Any]]) -> None:
    expected = freeze_lanes(observed)
    if json.dumps(frozen, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise Issue1895ReadinessError(
            "lane pin/query digest/state drifted after sampling",
            code="LANE_STATE_DRIFT",
            stage="lanes",
        )


def select_cold_identity(
    candidates: Sequence[Mapping[str, Any]],
    *,
    hot_run_id: str,
    source: str,
    bound: int = COLD_SEARCH_BOUND,
) -> Mapping[str, Any]:
    if bound != COLD_SEARCH_BOUND:
        raise Issue1895ReadinessError(
            "cold search bound drifted from 64",
            code="LANE_COLD_BOUND",
            stage="lanes",
        )
    if len(candidates) > bound:
        raise Issue1895ReadinessError(
            "cold identity search exceeded the 64-row bound",
            code="LANE_COLD_BOUND",
            stage="lanes",
        )
    for row in candidates:
        run_id = str(row.get("run_id") or "").strip()
        if not run_id or run_id == hot_run_id:
            continue
        return identity_from_row(row, source=source)
    raise Issue1895ReadinessError(
        "no display-ready historical cold identity remains",
        code="LANE_COLD_EMPTY",
        stage="lanes",
    )


def prove_window_rows(row_count: object) -> int:
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count <= 0:
        raise Issue1895ReadinessError(
            "curve window has no rows",
            code="LANE_NO_ROWS",
            stage="lanes",
        )
    return row_count


__all__ = (
    "COLD_SEARCH_BOUND",
    "COLD_STATE",
    "EXACT_IDENTITY_SQL",
    "HISTORICAL_IDENTITY_SQL",
    "HOT_STATE",
    "IDENTITY_FIELDS",
    "LANE_NAMES",
    "MAX_LANE_CHUNKS",
    "READY_RUN_STATUSES",
    "SOURCES",
    "WINDOW_ROW_PROOF_SQL",
    "assert_frozen_unchanged",
    "assert_shared_network",
    "assert_state_for_kind",
    "bind_lane",
    "classify_complete_groups",
    "freeze_lanes",
    "identity_from_row",
    "identity_projection",
    "lane_kind",
    "lane_source",
    "prove_window_rows",
    "select_cold_identity",
)
