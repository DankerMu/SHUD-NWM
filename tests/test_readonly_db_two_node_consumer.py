"""#2484 Must-preserve: the two-node readonly DB lane consumes the new route smoke unchanged.

The route records come from the real ``run_display_route_smoke`` over real success
envelopes (``Z`` echo against a ``+00:00`` request, discovery's basin in
``display_identity``). They replace ``route_smoke`` / ``display_identity`` in the
per-source bundles that ``tests/test_two_node_e2e_evidence.py`` seeds, the real
per-source merge rebuilds the final readonly lane (adding ``lane_statuses``), and the
governed entrypoint ``validate_two_node_e2e_evidence`` judges it.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from services.production_closure.readonly_db_validation import (
    ReadonlyDbValidationConfig,
    RouteHttpResponse,
    merge_readonly_db_source_evidence,
    run_display_route_smoke,
)
from services.production_closure.two_node_e2e_evidence import (
    STATUS_FAIL,
    STATUS_PASS,
    validate_two_node_e2e_evidence,
)
from tests.test_readonly_db_validation import _passing_route_requester, _route_name_for_path
from tests.test_two_node_e2e_evidence import _codes, _read, _run_id, _seed_pass_bundle, _write

DISCOVERED_BASIN_ID = "basins_qhh"


def _requested_identity(strict_identity: dict[str, Any]) -> dict[str, Any]:
    """Discovery's shape of the bundle identity: ``+00:00`` cycle time plus the run's basin."""
    assert strict_identity["cycle_time"] == "2026-05-29T00:00:00Z"
    return {**strict_identity, "cycle_time": "2026-05-29T00:00:00+00:00", "basin_id": DISCOVERED_BASIN_ID}


def _route_smoke(identity: dict[str, Any], requester: Any = _passing_route_requester) -> list[dict[str, Any]]:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=Path(__file__).resolve().parents[1] / "artifacts" / "test-readonly-db-validation",
        run_id=_run_id("two-node-consumer-smoke"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    return run_display_route_smoke(config, identity, route_requester=requester)


def _seed_bundle_from_new_route_smoke(run_id: str) -> Any:
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    strict_identities = _read(config.run_dir / "run.json")["strict_identities"]
    source_artifacts = _read(lane / "summary.json")["validation_provenance"]["source_artifacts"]
    source_dirs = [Path(artifact["source_dir"]) for artifact in source_artifacts]
    for source_dir in source_dirs:
        source_summary = _read(source_dir / "summary.json")
        identity = _requested_identity(strict_identities[source_summary["display_identity"]["source"]])
        source_summary["route_smoke"] = _route_smoke(identity)
        source_summary["display_identity"] = copy.deepcopy(identity)
        source_summary["lane_statuses"] = {"deny_write": STATUS_PASS, "read_routes": STATUS_PASS}
        _write(source_dir / "summary.json", source_summary)
        _write(source_dir / "route_smoke.json", source_summary["route_smoke"])
    merge_readonly_db_source_evidence(
        evidence_root=config.evidence_root,
        run_id=run_id,
        source_dirs=source_dirs,
        force=True,
    )
    return config


def _readonly_route_issue_codes(lane_summary: dict[str, Any]) -> set[str]:
    codes = _codes(lane_summary.get("blockers", [])) | _codes(lane_summary.get("findings", []))
    return {code for code in codes if code.startswith("TWO_NODE_E2E_READONLY_DB_ROUTE_")}


def test_the_two_node_readonly_lane_accepts_route_smoke_from_real_envelopes() -> None:
    config = _seed_bundle_from_new_route_smoke(_run_id("db-new-route-smoke"))
    merged = _read(config.run_dir / "db" / "readonly-db-boundary" / "summary.json")

    summary = validate_two_node_e2e_evidence(config)

    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert _readonly_route_issue_codes(readonly_lane) == set()
    assert readonly_lane["status"] == STATUS_PASS
    assert merged["lane_statuses"] == {"deny_write": STATUS_PASS, "read_routes": STATUS_PASS}
    assert {source: identity["basin_id"] for source, identity in merged["display_identity"].items()} == {
        "GFS": DISCOVERED_BASIN_ID,
        "IFS": DISCOVERED_BASIN_ID,
    }
    identity_bound = [route for route in merged["route_smoke"] if "response_identity" in route]
    assert {(route["name"], route["source"]) for route in identity_bound} == {
        (name, source)
        for name in ("latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs")
        for source in ("GFS", "IFS")
    }
    assert all(route["response_identity"]["cycle_time"] == "2026-05-29T00:00:00Z" for route in identity_bound)


@pytest.mark.parametrize("route_name", ["jobs", "latest_product"])
def test_an_echo_mismatch_route_reaches_the_two_node_lane_as_a_child_not_pass(route_name: str) -> None:
    config = _seed_bundle_from_new_route_smoke(_run_id(f"db-new-route-mismatch-{route_name}"))
    lane = config.run_dir / "db" / "readonly-db-boundary"
    strict_identities = _read(config.run_dir / "run.json")["strict_identities"]

    def tampered_requester(method: str, path: str) -> RouteHttpResponse:
        response = _passing_route_requester(method, path)
        if _route_name_for_path(path) == route_name:
            body = copy.deepcopy(response.body)
            echo = body["data"] if route_name == "latest_product" else body["identity"]
            echo["run_id"] = "hydro-gfs-other-run"
            return RouteHttpResponse(status_code=200, body=body)
        return response

    failing = next(
        route
        for route in _route_smoke(_requested_identity(strict_identities["GFS"]), tampered_requester)
        if route["name"] == route_name
    )
    assert failing["status"] == STATUS_FAIL
    db_summary = _read(lane / "summary.json")
    db_summary["route_smoke"] = [
        {**failing, "source": "GFS"} if (route.get("name"), route.get("source")) == (route_name, "GFS") else route
        for route in db_summary["route_smoke"]
    ]
    _write(lane / "summary.json", db_summary)
    _write(lane / "route_smoke.json", db_summary["route_smoke"])

    summary = validate_two_node_e2e_evidence(config)

    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert readonly_lane["status"] != STATUS_PASS
    child_not_pass = [
        blocker
        for blocker in readonly_lane["blockers"]
        if blocker.get("code") == "TWO_NODE_E2E_READONLY_DB_ROUTE_CHILD_NOT_PASS"
    ]
    assert [(blocker.get("route"), blocker.get("child_status")) for blocker in child_not_pass] == [
        (route_name, STATUS_FAIL)
    ]
