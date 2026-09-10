#!/usr/bin/env python3
"""Bounded read-only four-lane product-curve performance oracle.

Default path opens a private nhms_display_ro session and the local display
API. Tests inject connect/opener/probe/lane fakes and must not open a real
database or HTTP socket. Mixed injected/live configuration fails closed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from packages.common.node27_issue1895_display_runtime import DISPLAY_ENV_PATH
from packages.common.node27_issue1895_lanes import LANE_NAMES, assert_frozen_unchanged, freeze_lanes
from packages.common.node27_issue1895_performance import (
    ARTIFACT,
    HTTP_BODY_LIMIT_BYTES,
    HTTP_TIMEOUT_SECONDS,
    build_performance_receipt,
    evaluate_named_lane,
    format_refusal,
    refuse_secret_output,
    run_warmup_and_accepted,
    validate_performance_receipt,
)
from packages.common.node27_issue1895_performance_live import (
    close_performance_connection,
    discover_four_lanes,
    local_display_origin,
    make_api_probe,
    make_sql_probe,
    observe_lane_states,
    open_readonly_performance_connection,
    prove_readonly_session,
    publish_performance_artifacts,
    resolve_live_dsn,
    validate_basin_id,
    validate_segment_id,
    validate_sha,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError

SqlProbe = Callable[[int], Mapping[str, Any]]
ApiProbe = Callable[[int], Mapping[str, Any]]
LaneProbes = Mapping[str, Mapping[str, Any]]


def evaluate_oracle(
    *,
    lanes: Mapping[str, Mapping[str, Any]],
    identity: Mapping[str, Any],
    sql_probes: Mapping[str, SqlProbe],
    api_probes: Mapping[str, ApiProbe],
) -> dict[str, Any]:
    frozen = freeze_lanes(lanes)
    evaluated: dict[str, Any] = {}
    for name in LANE_NAMES:
        lane = lanes[name]
        query = lane["query"]
        sql_warmup, sql_accepted = run_warmup_and_accepted(sql_probes[name])
        api_warmup, api_accepted = run_warmup_and_accepted(api_probes[name])
        evaluated[name] = evaluate_named_lane(
            name=name,
            sql_warmup=sql_warmup,
            sql_accepted=sql_accepted,
            api_warmup=api_warmup,
            api_accepted=api_accepted,
            candidate_chunk_names=lane["candidate_chunk_names"],
            segment_id=query["timeseries_segment_id"],
            window_start=query["window_start"],
            window_end=query["window_end"],
            api_path=query["api_path"],
        )
        lane_identity = lane["identity"]
        evaluated[name]["run_id"] = lane_identity["run_id"]
        evaluated[name]["model_id"] = lane_identity["model_id"]
        evaluated[name]["cycle_time"] = lane_identity["cycle_time"]
        evaluated[name]["basin_id"] = lane_identity["basin_id"]
        evaluated[name]["basin_version_id"] = lane_identity["basin_version_id"]
        evaluated[name]["river_network_version_id"] = lane_identity["river_network_version_id"]
        evaluated[name]["source_id"] = lane_identity.get("source_id") or query["source"]
        evaluated[name]["scenario"] = lane_identity.get("scenario") or query["scenario"]
        evaluated[name]["segment_id"] = query["segment_id"]
        evaluated[name]["timeseries_segment_id"] = query["timeseries_segment_id"]
        evaluated[name]["window_start"] = query["window_start"]
        evaluated[name]["window_end"] = query["window_end"]
        evaluated[name]["api_path"] = query["api_path"]
        evaluated[name]["api_query"] = query["api_query"]
        evaluated[name]["query_digest"] = lane["query_digest"]
        evaluated[name]["candidate_chunk_names"] = list(lane["candidate_chunk_names"])
    document = refuse_secret_output(
        build_performance_receipt(lanes=evaluated, identity=identity, frozen=frozen, status="PASS")
    )
    return validate_performance_receipt(document)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt-path", required=True, type=Path)
    parser.add_argument("--basin-id", required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--reviewed-sha", required=True)
    parser.add_argument("--display-origin", default=None)
    parser.add_argument("--display-env", type=Path, default=DISPLAY_ENV_PATH)
    return parser


def _injection_state(
    *,
    sql_probes: Mapping[str, SqlProbe] | None,
    api_probes: Mapping[str, ApiProbe] | None,
    lanes: Mapping[str, Mapping[str, Any]] | None,
    connect: Callable[[str], Any] | None,
    opener: Any | None,
    connection: Any | None,
    load_chunk: Any | None = None,
    collect_group: Any | None = None,
) -> str:
    injected = [
        sql_probes is not None,
        api_probes is not None,
        lanes is not None,
        connect is not None,
        opener is not None,
        connection is not None,
        load_chunk is not None,
        collect_group is not None,
    ]
    if all(injected[:3]) and not any(injected[3:]):
        return "full-probes"
    if not any(injected):
        return "live"
    if connect is not None and connection is not None:
        raise Issue1895ReadinessError(
            "mixed injected and live performance configuration is refused",
            code="INJECTION_MIXED",
            stage="performance",
        )
    probes_absent = sql_probes is None and api_probes is None and lanes is None
    catalog_ok = (load_chunk is None) == (collect_group is None)
    if not catalog_ok:
        raise Issue1895ReadinessError(
            "mixed injected and live performance configuration is refused",
            code="INJECTION_MIXED",
            stage="performance",
        )
    if connection is not None and opener is not None and probes_absent:
        return "live-fakes"
    if connect is not None and opener is not None and probes_absent:
        return "live-connect"
    raise Issue1895ReadinessError(
        "mixed injected and live performance configuration is refused",
        code="INJECTION_MIXED",
        stage="performance",
    )


def _require_four_lanes(lanes: Mapping[str, Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    if set(lanes) != set(LANE_NAMES):
        raise Issue1895ReadinessError(
            "injected lanes are not the four closed product lanes",
            code="LANE_SET_INVALID",
            stage="performance",
        )
    return {name: lanes[name] for name in LANE_NAMES}


def main(
    argv: list[str] | None = None,
    *,
    sql_probes: Mapping[str, SqlProbe] | None = None,
    api_probes: Mapping[str, ApiProbe] | None = None,
    lanes: Mapping[str, Mapping[str, Any]] | None = None,
    connect: Callable[[str], Any] | None = None,
    opener: Any | None = None,
    connection: Any | None = None,
    load_chunk: Any | None = None,
    collect_group: Any | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    owned_connection: Any | None = None
    try:
        mode = _injection_state(
            sql_probes=sql_probes,
            api_probes=api_probes,
            lanes=lanes,
            connect=connect,
            opener=opener,
            connection=connection,
            load_chunk=load_chunk,
            collect_group=collect_group,
        )
        head_sha = validate_sha(args.head_sha, label="head_sha")
        reviewed_sha = validate_sha(args.reviewed_sha, label="reviewed_sha")
        if head_sha != reviewed_sha:
            raise Issue1895ReadinessError(
                "head_sha is not REVIEWED_SHA",
                code="INPUT_SHA_MISMATCH",
                stage="performance",
            )
        basin_id = validate_basin_id(args.basin_id)
        segment_id = validate_segment_id(args.segment_id)
        origin = local_display_origin(display_env_path=args.display_env, expected_display_env=args.display_env)
        if args.display_origin is not None and args.display_origin != origin:
            raise Issue1895ReadinessError(
                "display-origin conflicts with the pinned display.env port",
                code="INPUT_ORIGIN_CONFLICT",
                stage="performance",
            )
        readonly_proof: dict[str, Any]
        live_lanes: dict[str, Mapping[str, Any]]
        live_sql: dict[str, SqlProbe]
        live_api: dict[str, ApiProbe]
        live_connection: Any | None = None
        if mode == "full-probes":
            if lanes is None or sql_probes is None or api_probes is None:
                raise Issue1895ReadinessError(
                    "full probe injection is incomplete",
                    code="INJECTION_MIXED",
                    stage="performance",
                )
            readonly_proof = {"transaction_read_only": True, "current_user": "nhms_display_ro"}
            live_lanes = _require_four_lanes(lanes)
            live_sql = dict(sql_probes)
            live_api = dict(api_probes)
            if set(live_sql) != set(LANE_NAMES) or set(live_api) != set(LANE_NAMES):
                raise Issue1895ReadinessError(
                    "injected probes are not the four closed product lanes",
                    code="LANE_SET_INVALID",
                    stage="performance",
                )
        else:
            live_connection = connection
            if live_connection is None:
                dsn = resolve_live_dsn(display_env_path=args.display_env, environ=environ)
                owned_connection = open_readonly_performance_connection(dsn, connect=connect)
                live_connection = owned_connection
            readonly_proof = prove_readonly_session(live_connection)
            live_lanes = discover_four_lanes(
                live_connection,
                basin_id=basin_id,
                segment_id=segment_id,
                origin=origin,
                opener=opener,
                load_chunk=load_chunk,
                collect_group=collect_group,
            )
            live_sql = {}
            live_api = {}
            for name in LANE_NAMES:
                lane = live_lanes[name]
                query = lane["query"]
                live_sql[name] = make_sql_probe(
                    live_connection,
                    explain_sql=query["explain_sql"],
                    parameters=query["parameters"],
                )
                live_api[name] = make_api_probe(
                    origin=origin,
                    path=query["api_path"],
                    query=query["api_query"],
                    segment_id=query["segment_id"],
                    issue_time=query["issue_time"],
                    scenario=query["scenario"],
                    source=query["source"],
                    window_start=query["window_start"],
                    window_end=query["window_end"],
                    opener=opener,
                )
        identity = {
            "head_sha": head_sha,
            "reviewed_sha": reviewed_sha,
            "api_origin": origin,
            "timeout_seconds": HTTP_TIMEOUT_SECONDS,
            "body_limit_bytes": HTTP_BODY_LIMIT_BYTES,
            "artifact": ARTIFACT,
            "basin_id": basin_id,
            "segment_id": segment_id,
            "readonly": readonly_proof,
        }
        document = evaluate_oracle(
            lanes=live_lanes,
            identity=identity,
            sql_probes=live_sql,
            api_probes=live_api,
        )
        if mode != "full-probes":
            if live_connection is None:
                raise Issue1895ReadinessError(
                    "live lane re-observation is missing a connection",
                    code="INJECTION_MIXED",
                    stage="performance",
                )
            observed = observe_lane_states(
                live_connection,
                live_lanes,
                load_chunk=load_chunk,
                collect_group=collect_group,
            )
            assert_frozen_unchanged(document["frozen"], observed)
        else:
            freeze_lanes(live_lanes)
        publish_performance_artifacts(args.receipt_path, document)
    except Issue1895ReadinessError as error:
        print(format_refusal(error), file=sys.stderr)
        return 1
    finally:
        if owned_connection is not None:
            close_performance_connection(owned_connection)
        elif connection is not None:
            close_performance_connection(connection)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
