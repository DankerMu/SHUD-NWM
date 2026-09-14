"""PGDATA-owned explicit-cycle SQL/API workload producer.

Public owner for retained before/after measurements. Isolated receipts never
imply live acceptance. Capture uses the shipping forecast_series adapter; the
CLI always runs one warmup plus 20 accepted SQL and API samples.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from packages.common.node27_pgdata_workload_io import utc_now
from packages.common.node27_pgdata_workload_measure import (
    evaluate_api_samples,
    evaluate_sql_samples,
    load_window_candidates,
    make_api_probe,
    make_sql_probe,
    prove_authoritative_run_identity,
    run_warmup_and_accepted,
)
from packages.common.node27_pgdata_workload_query import (
    CanonicalExplicitCycleIdentity,
    parse_issue_time,
    record_explicit_cycle_curve,
    scenario_for_source,
    timeseries_segment_id,
)
from packages.common.node27_pgdata_workload_types import PgdataWorkloadError, refuse
from packages.common.redaction import redact_payload

ARTIFACT = "nhms-pgdata-workload"
SCHEMA_VERSION = "1.0"


def _expected_identity(captured: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "issue_time": captured["issue_time"],
        "run_id": captured["run_id"],
        "model_id": captured["model_id"],
        "timeseries_segment_id": captured["timeseries_segment_id"],
        "river_network_version_id": captured["river_network_version_id"],
        "basin_version_id": captured["basin_version_id"],
        "source": captured["source"],
        "scenario": captured["scenario"],
        "window_end": captured["window_end"],
    }


def capture_workload_query(
    *,
    basin_version_id: str,
    segment_id: str,
    river_network_version_id: str,
    issue_time: str | datetime,
    run_id: str,
    model_id: str,
    source: str,
) -> dict[str, Any]:
    return record_explicit_cycle_curve(
        basin_version_id=basin_version_id,
        segment_id=segment_id,
        river_network_version_id=river_network_version_id,
        issue_time=issue_time,
        run_id=run_id,
        model_id=model_id,
        source=source,
    )


def measure_workload(
    *,
    connection: Any,
    origin: str,
    captured: Mapping[str, Any],
    evidence_kind: str,
    reviewed_sha: str,
    opener: Any | None = None,
    clock: Callable[[], float] | None = None,
    now: Callable[[], datetime] | None = None,
    sql_probe: Callable[[int], Mapping[str, Any]] | None = None,
    api_probe: Callable[[int], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run complete SQL/API measurement. Capture-only is never a PASS path."""

    if evidence_kind not in {"isolated", "live"}:
        refuse("evidence kind must be isolated or live", code="INPUT_KIND_INVALID", stage="input")
    if (sql_probe is None) != (api_probe is None):
        refuse(
            "mixed injected and live performance configuration is refused", code="INJECTION_MIXED", stage="performance"
        )
    expected = _expected_identity(captured)
    candidates = load_window_candidates(
        connection,
        window_start=str(captured["window_start"]),
        window_end=str(captured["window_end"]),
    )
    prove_authoritative_run_identity(connection, captured=captured)
    timer = clock or __import__("time").monotonic
    sql = sql_probe or make_sql_probe(connection, captured=captured, expected=expected, clock=timer)
    api = api_probe or make_api_probe(
        origin=origin,
        path=str(captured["api_path"]),
        query=str(captured["api_query"]),
        segment_id=str(captured["segment_id"]),
        issue_time=str(captured["issue_time"]),
        scenario=str(captured["scenario"]),
        source=str(captured["source"]),
        window_start=str(captured["window_start"]),
        window_end=str(captured["window_end"]),
        opener=opener,
        clock=timer,
        run_id=str(captured["run_id"]),
        model_id=str(captured["model_id"]),
    )
    sql_warmup, sql_accepted = run_warmup_and_accepted(sql)
    api_warmup, api_accepted = run_warmup_and_accepted(api)
    sql_eval = evaluate_sql_samples(
        warmup=sql_warmup,
        accepted=sql_accepted,
        candidate_chunk_names=candidates["candidate_chunk_names"],
        segment_id=str(captured["timeseries_segment_id"]),
        window_start=str(captured["window_start"]),
        window_end=str(captured["window_end"]),
    )
    api_eval = evaluate_api_samples(warmup=api_warmup, accepted=api_accepted, path=str(captured["api_path"]))
    instant = (now or utc_now)().astimezone(UTC).isoformat().replace("+00:00", "Z")
    document = {
        "artifact": ARTIFACT,
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "evidence_kind": evidence_kind,
        "isolated": evidence_kind == "isolated",
        "live": evidence_kind == "live",
        "measured_at": instant,
        "reviewed_sha": reviewed_sha,
        "identity": {
            "basin_version_id": captured["basin_version_id"],
            "river_network_version_id": captured["river_network_version_id"],
            "segment_id": captured["segment_id"],
            "timeseries_segment_id": captured["timeseries_segment_id"],
            "issue_time": captured["issue_time"],
            "run_id": captured["run_id"],
            "model_id": captured["model_id"],
            "source": captured["source"],
            "scenario": captured["scenario"],
            "window_start": captured["window_start"],
            "window_end": captured["window_end"],
            "query_digest": captured["query_digest"],
            "api_origin": origin,
            "api_path": captured["api_path"],
        },
        "sql": sql_eval,
        "api": api_eval,
        "candidates": {
            "origin_chunk_names": list(candidates["origin_chunk_names"]),
            "compressed_chunk_names": list(candidates["compressed_chunk_names"]),
            "candidate_chunk_names": list(candidates["candidate_chunk_names"]),
            "candidate_count": candidates["candidate_count"],
        },
        "query": {
            "sql": captured["sql"],
            "explain_sql": captured["explain_sql"],
            "parameters": captured["parameters"],
            "query_digest": captured["query_digest"],
        },
        "samples": {
            "sql_warmup_ms": sql_warmup["duration_ms"],
            "api_warmup_ms": api_warmup["duration_ms"],
            "sql_accepted_ms": sql_eval["accepted_durations_ms"],
            "api_accepted_ms": api_eval["accepted_durations_ms"],
        },
    }
    return redact_payload(document)


__all__ = (
    "ARTIFACT",
    "CanonicalExplicitCycleIdentity",
    "PgdataWorkloadError",
    "SCHEMA_VERSION",
    "capture_workload_query",
    "measure_workload",
    "parse_issue_time",
    "scenario_for_source",
    "timeseries_segment_id",
)
