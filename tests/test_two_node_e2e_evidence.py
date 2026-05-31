from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from services.production_closure.two_node_e2e_evidence import (
    READONLY_DB_LIVE_SCHEMA,
    STATUS_BLOCKED,
    STATUS_FAIL,
    STATUS_PARTIAL,
    STATUS_PASS,
    TwoNodeE2EEvidenceConfig,
    TwoNodeE2EEvidenceError,
    validate_two_node_e2e_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_complete_synthetic_real_evidence_bundle_passes_with_redaction() -> None:
    run_id = _run_id("pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    api_summary["diagnostics"] = {
        "api_token": "FINAL_E2E_SECRET",
        "dsn": "postgresql://display:FINAL_E2E_SECRET@db.example/nhms?signature=FINAL_E2E_SECRET",
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["manual_ops"]["status"] == STATUS_PASS
    assert summary["source_scope_results"]["GFS"]["status"] == STATUS_PASS
    assert summary["source_scope_results"]["IFS"]["status"] == STATUS_PASS
    evidence_text = (config.lane_dir / "summary.json").read_text(encoding="utf-8")
    assert "FINAL_E2E_SECRET" not in evidence_text
    assert "[redacted]" in evidence_text


def test_large_lane_payload_is_bounded_in_final_summary() -> None:
    run_id = _run_id("large-lane")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    api_summary["oversized_lane_payload"] = "x" * 900_000
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    evidence_text = (config.lane_dir / "summary.json").read_text(encoding="utf-8")
    assert len(evidence_text.encode("utf-8")) < 300_000
    assert "x" * 10_000 not in evidence_text
    assert summary["lane_summaries"]["api"]["redacted_evidence"]["_bounded_evidence"]["truncated"] is True


def test_missing_live_docker_db_and_browser_evidence_blocks_not_passes() -> None:
    run_id = _run_id("missing-live")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    docker_security.pop("live_docker_evidence")
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)
    (config.run_dir / "db" / "readonly-db-boundary" / "summary.json").unlink()
    (config.run_dir / "browser" / "summary.json").unlink()

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    assert summary["lane_summaries"]["docker_security"]["status"] == STATUS_BLOCKED
    assert summary["lane_summaries"]["readonly_db"]["status"] == STATUS_BLOCKED
    assert summary["lane_summaries"]["browser"]["status"] == STATUS_BLOCKED


def test_docker_display_capability_leak_fails_even_when_summary_was_partial() -> None:
    run_id = _run_id("docker-leak")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    docker_security["status"] = STATUS_PARTIAL
    docker_security["docker_socket_present"] = True
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_FAIL
    docker_lane = summary["lane_summaries"]["docker_security"]
    assert docker_lane["status"] == STATUS_FAIL
    assert _codes(docker_lane["findings"]) >= {"TWO_NODE_E2E_DOCKER_DISPLAY_FORBIDDEN_CAPABILITY"}


def test_known_producer_like_docker_preflight_without_embedded_bundle_id_or_df_h_is_accepted() -> None:
    run_id = _run_id("preflight-no-id")
    config = _seed_pass_bundle(run_id)
    preflight = _read(config.run_dir / "docker-preflight" / "summary.json")
    preflight.pop("evidence_run_id")
    preflight["commands"].pop("df_h")
    _write(config.run_dir / "docker-preflight" / "summary.json", preflight)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["docker_preflight"]["status"] == STATUS_PASS


@pytest.mark.parametrize("missing_key", ["evidence_root", "tmpdir", "docker_root_dir", "min_free_bytes", "disk"])
def test_docker_preflight_pass_missing_resource_evidence_blocks(missing_key: str) -> None:
    run_id = _run_id(f"preflight-missing-{missing_key.replace('_', '-')}")
    config = _seed_pass_bundle(run_id)
    preflight = _read(config.run_dir / "docker-preflight" / "summary.json")
    preflight.pop(missing_key)
    _write(config.run_dir / "docker-preflight" / "summary.json", preflight)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    assert summary["lane_summaries"]["docker_preflight"]["status"] == STATUS_BLOCKED


def test_docker_preflight_pass_missing_command_evidence_blocks() -> None:
    run_id = _run_id("preflight-missing-command")
    config = _seed_pass_bundle(run_id)
    preflight = _read(config.run_dir / "docker-preflight" / "summary.json")
    preflight["commands"].pop("docker_system_df")
    _write(config.run_dir / "docker-preflight" / "summary.json", preflight)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_DOCKER_PREFLIGHT_COMMAND_EVIDENCE_MISSING" in _codes(
        summary["lane_summaries"]["docker_preflight"]["blockers"]
    )


def test_docker_security_pass_missing_required_proof_blocks() -> None:
    run_id = _run_id("docker-proof-missing")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    docker_security.pop("root_filesystem_readonly")
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    docker_lane = summary["lane_summaries"]["docker_security"]
    assert docker_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_DOCKER_DISPLAY_PROOF_MISSING" in _codes(docker_lane["blockers"])


@pytest.mark.parametrize(
    "raw_evidence",
    [
        {"HostConfig": {"Privileged": True}},
        {"HostConfig": {"NetworkMode": "host"}},
        {"HostConfig": {"PidMode": "host"}},
        {"HostConfig": {"IpcMode": "host"}},
        {"HostConfig": {"CapAdd": ["SYS_ADMIN"]}},
        {"Mounts": [{"Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": False}]},
        {"Mounts": [{"Source": "/scratch/private/workspace", "Destination": "/workspace", "RW": False}]},
        {"Mounts": [{"Source": "/", "Destination": "/host", "RW": False}]},
        {"Config": {"Env": ["WORKSPACE_ROOT=/workspace"]}},
        {"Mounts": [{"Source": "/srv/nhms/published", "Destination": "/var/lib/nhms/published", "RW": True}]},
    ],
)
def test_docker_security_raw_inspect_hazards_fail(raw_evidence: dict[str, Any]) -> None:
    run_id = _run_id("docker-raw-hazard")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    docker_security["docker_inspect"] = [raw_evidence]
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_FAIL
    assert summary["lane_summaries"]["docker_security"]["status"] == STATUS_FAIL


@pytest.mark.parametrize(
    "operation_patch",
    [
        {"role": {"role_type": "writer_or_mutating"}},
        {"permission_probes": [{"operations": [{"operation": "INSERT", "privilege_allowed": True}]}]},
        {"permission_probes": [{"operations": [{"operation": "DDL_CREATE_TABLE", "execution_outcome": "succeeded"}]}]},
    ],
)
def test_readonly_db_writer_or_mutating_evidence_fails(operation_patch: dict[str, Any]) -> None:
    run_id = _run_id("db-fail")
    config = _seed_pass_bundle(run_id)
    db_summary = _read(config.run_dir / "db" / "readonly-db-boundary" / "summary.json")
    _deep_update(db_summary, operation_patch)
    _write(config.run_dir / "db" / "readonly-db-boundary" / "summary.json", db_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_FAIL
    assert summary["lane_summaries"]["readonly_db"]["status"] == STATUS_FAIL


@pytest.mark.parametrize("producer_status", [STATUS_PARTIAL, STATUS_BLOCKED])
@pytest.mark.parametrize(
    "operation_patch",
    [
        {"role": {"role_type": "writer_or_mutating"}},
        {"permission_probes": [{"operations": [{"operation": "INSERT", "privilege_allowed": True}]}]},
        {"permission_probes": [{"operations": [{"operation": "DDL_CREATE_TABLE", "execution_outcome": "succeeded"}]}]},
    ],
)
def test_readonly_db_mutating_evidence_fails_even_when_producer_did_not_pass(
    producer_status: str,
    operation_patch: dict[str, Any],
) -> None:
    run_id = _run_id(f"db-{producer_status.lower()}-fail")
    config = _seed_pass_bundle(run_id)
    db_summary = _read(config.run_dir / "db" / "readonly-db-boundary" / "summary.json")
    db_summary["status"] = producer_status
    _deep_update(db_summary, operation_patch)
    _write(config.run_dir / "db" / "readonly-db-boundary" / "summary.json", db_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_FAIL
    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert readonly_lane["summary_status"] == producer_status
    assert readonly_lane["status"] == STATUS_FAIL


@pytest.mark.parametrize(
    "mutator",
    ["sibling_role_writer", "sibling_permission_mutation", "sibling_payload_mismatch"],
)
def test_readonly_db_authoritative_sibling_evidence_is_recomputed(mutator: str) -> None:
    run_id = _run_id(f"db-sibling-{mutator}")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    if mutator == "sibling_role_writer":
        role = _read(lane / "role.json")
        role["role_type"] = "writer_or_mutating"
        _write(lane / "role.json", role)
    elif mutator == "sibling_permission_mutation":
        probes = json.loads((lane / "permission_probes.json").read_text(encoding="utf-8"))
        probes[0]["operations"][0]["privilege_allowed"] = True
        _write(lane / "permission_probes.json", probes)
    else:
        routes = json.loads((lane / "route_smoke.json").read_text(encoding="utf-8"))
        routes.pop()
        _write(lane / "route_smoke.json", routes)

    summary = validate_two_node_e2e_evidence(config)

    if mutator == "sibling_payload_mismatch":
        assert summary["status"] == STATUS_BLOCKED
        assert summary["lane_summaries"]["readonly_db"]["status"] == STATUS_BLOCKED
    else:
        assert summary["status"] == STATUS_FAIL
        assert summary["lane_summaries"]["readonly_db"]["status"] == STATUS_FAIL


@pytest.mark.parametrize(
    "mutator",
    ["route_child_failed", "route_identity_incomplete", "manual_child_failed", "permission_coverage_missing"],
)
def test_readonly_db_child_failure_or_coverage_gap_blocks(mutator: str) -> None:
    run_id = _run_id(f"db-child-{mutator}")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    if mutator == "route_child_failed":
        db_summary["route_smoke"][0]["status"] = STATUS_FAIL
    elif mutator == "route_identity_incomplete":
        latest = next(route for route in db_summary["route_smoke"] if route["name"] == "latest_product")
        latest["strict_identity"].pop("model_id")
        latest["path"] = latest["path"].replace("&model_id=basins_qhh_shud", "")
    elif mutator == "manual_child_failed":
        db_summary["manual_action_probes"][0]["status"] = STATUS_FAIL
    else:
        db_summary["permission_probes"] = [
            probe for probe in db_summary["permission_probes"] if probe["target"] != "ops.pipeline_event"
        ]
    _write(lane / "summary.json", db_summary)
    _write(lane / "route_smoke.json", db_summary["route_smoke"])
    _write(lane / "permission_probes.json", db_summary["permission_probes"])

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    assert summary["lane_summaries"]["readonly_db"]["status"] == STATUS_BLOCKED


@pytest.mark.parametrize(
    ("mutator", "expected_status"),
    [
        ("wrong_identity", STATUS_FAIL),
        ("partial_identity", STATUS_BLOCKED),
        ("historical_latest", STATUS_FAIL),
        ("mock_browser", STATUS_FAIL),
    ],
)
def test_strict_identity_historical_latest_and_mock_evidence_do_not_pass(
    mutator: str,
    expected_status: str,
) -> None:
    run_id = _run_id(mutator)
    config = _seed_pass_bundle(run_id)
    if mutator in {"wrong_identity", "partial_identity", "historical_latest"}:
        api_summary = _read(config.run_dir / "api" / "summary.json")
        latest = api_summary["sources"]["GFS"]["checks"]["latest_product"]
        if mutator == "wrong_identity":
            latest["identity"]["model_id"] = "wrong-model"
        elif mutator == "partial_identity":
            latest["identity"].pop("model_id")
        else:
            latest["historical_latest"] = True
        _write(config.run_dir / "api" / "summary.json", api_summary)
    else:
        browser_summary = _read(config.run_dir / "browser" / "summary.json")
        browser_summary["mock_browser_data"] = True
        _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == expected_status
    assert summary["status"] != STATUS_PASS


@pytest.mark.parametrize(
    ("mutator", "expected_status"),
    [
        ("missing_metadata", STATUS_BLOCKED),
        ("unsupported_schema", STATUS_BLOCKED),
        ("stale_bundle", STATUS_BLOCKED),
        ("missing_declared_sources", STATUS_BLOCKED),
        ("incomplete_identity", STATUS_BLOCKED),
        ("key_source_mismatch", STATUS_FAIL),
        ("duplicate_embedded_source", STATUS_FAIL),
    ],
)
def test_metadata_identity_contract_blocks_or_fails_before_seeding_lanes(
    mutator: str,
    expected_status: str,
) -> None:
    run_id = _run_id(f"metadata-{mutator}")
    config = _seed_pass_bundle(run_id)
    run_path = config.run_dir / "run.json"
    metadata = _read(run_path)
    if mutator == "missing_metadata":
        run_path.unlink()
    elif mutator == "unsupported_schema":
        metadata["schema"] = "nhms.two_node_e2e.run.unknown"
        _write(run_path, metadata)
    elif mutator == "stale_bundle":
        metadata["evidence_run_id"] = "older-bundle"
        _write(run_path, metadata)
    elif mutator == "missing_declared_sources":
        metadata.pop("declared_sources")
        _write(run_path, metadata)
    elif mutator == "incomplete_identity":
        metadata["strict_identities"]["GFS"].pop("model_id")
        _write(run_path, metadata)
    elif mutator == "key_source_mismatch":
        metadata["strict_identities"]["GFS"]["source"] = "IFS"
        _write(run_path, metadata)
    else:
        metadata["strict_identities"] = [
            metadata["strict_identities"]["GFS"],
            {**metadata["strict_identities"]["IFS"], "source": "GFS"},
        ]
        _write(run_path, metadata)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == expected_status
    assert summary["lane_summaries"]["metadata"]["status"] == expected_status


def test_source_lane_key_source_mismatch_does_not_collapse_siblings() -> None:
    run_id = _run_id("source-key-collapse")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    api_summary["sources"]["GFS"]["identity"]["source"] = "IFS"
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_FAIL
    assert summary["lane_summaries"]["api"]["status"] == STATUS_FAIL
    assert "TWO_NODE_E2E_STRICT_IDENTITY_MISMATCH" in _codes(summary["lane_summaries"]["api"]["findings"])


def test_both_sources_declared_with_one_missing_or_failing_never_full_passes() -> None:
    missing_run_id = _run_id("missing-source")
    missing_config = _seed_pass_bundle(missing_run_id)
    api_summary = _read(missing_config.run_dir / "api" / "summary.json")
    api_summary["sources"].pop("IFS")
    _write(missing_config.run_dir / "api" / "summary.json", api_summary)

    missing_summary = validate_two_node_e2e_evidence(missing_config)

    assert missing_summary["status"] == STATUS_BLOCKED
    assert missing_summary["status"] != STATUS_PASS

    failing_run_id = _run_id("failing-source")
    failing_config = _seed_pass_bundle(failing_run_id)
    logs_summary = _read(failing_config.run_dir / "logs" / "summary.json")
    logs_summary["sources"]["IFS"]["checks"]["job_logs"]["status"] = STATUS_FAIL
    _write(failing_config.run_dir / "logs" / "summary.json", logs_summary)

    failing_summary = validate_two_node_e2e_evidence(failing_config)

    assert failing_summary["status"] == STATUS_FAIL
    assert failing_summary["status"] != STATUS_PASS


def test_single_source_bundle_is_reduced_scope_partial_even_when_declared() -> None:
    run_id = _run_id("single-source")
    config = _seed_pass_bundle(run_id, sources=("GFS",), reduced_scope=True)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PARTIAL
    assert summary["lane_summaries"]["cross_plane"]["status"] == STATUS_PARTIAL
    assert summary["strict_identity"]["reduced_scope"] is True


def test_manual_ops_auth_and_receipt_boundaries() -> None:
    auth_run_id = _run_id("manual-auth")
    auth_config = _seed_pass_bundle(auth_run_id)
    manual_ops = _read(auth_config.run_dir / "manual-ops" / "summary.json")
    manual_ops["production_operator_auth_evidence"] = False
    _write(auth_config.run_dir / "manual-ops" / "summary.json", manual_ops)

    auth_summary = validate_two_node_e2e_evidence(auth_config)

    assert auth_summary["status"] == STATUS_BLOCKED
    assert auth_summary["lane_summaries"]["manual_ops"]["status"] == STATUS_BLOCKED

    receipt_run_id = _run_id("manual-27-receipt")
    receipt_config = _seed_pass_bundle(receipt_run_id)
    manual_ops = _read(receipt_config.run_dir / "manual-ops" / "summary.json")
    manual_ops["control_receipts"][0]["node"] = "27"
    manual_ops["control_receipts"][0]["producer_role"] = "display_readonly"
    _write(receipt_config.run_dir / "manual-ops" / "summary.json", manual_ops)

    receipt_summary = validate_two_node_e2e_evidence(receipt_config)

    assert receipt_summary["status"] == STATUS_FAIL
    assert receipt_summary["lane_summaries"]["manual_ops"]["status"] == STATUS_FAIL


@pytest.mark.parametrize(
    ("mutator", "expected_status"),
    [
        ("node22_only", STATUS_BLOCKED),
        ("missing_cancel", STATUS_BLOCKED),
        ("auth_only", STATUS_BLOCKED),
        ("side_effect_true", STATUS_FAIL),
        ("receipt_missing_identity", STATUS_BLOCKED),
        ("receipt_mismatched_identity", STATUS_FAIL),
    ],
)
def test_manual_ops_display_boundary_matrix(mutator: str, expected_status: str) -> None:
    run_id = _run_id(f"manual-{mutator}")
    config = _seed_pass_bundle(run_id)
    manual_ops = _read(config.run_dir / "manual-ops" / "summary.json")
    if mutator == "node22_only":
        for action in manual_ops["display_actions"]:
            action["node"] = "22"
    elif mutator == "missing_cancel":
        manual_ops["display_actions"] = [
            action for action in manual_ops["display_actions"] if action["action"] != "cancel"
        ]
    elif mutator == "auth_only":
        manual_ops["display_actions"] = [
            {
                "node": "27",
                "action": action,
                "outcome": "403 FORBIDDEN",
                "http_status": 403,
                "write_executed": False,
                "gateway_called": False,
                "receipt_created": False,
            }
            for action in ("retry", "cancel")
        ]
    elif mutator == "side_effect_true":
        manual_ops["display_actions"][0]["gateway_called"] = True
    elif mutator == "receipt_missing_identity":
        for field in ("source", "cycle_time", "run_id", "model_id"):
            manual_ops["control_receipts"][0].pop(field, None)
    else:
        manual_ops["control_receipts"][0]["model_id"] = "wrong-model"
    _write(config.run_dir / "manual-ops" / "summary.json", manual_ops)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == expected_status
    assert summary["lane_summaries"]["manual_ops"]["status"] == expected_status


def test_path_safety_rejects_unapproved_roots_and_symlink_run_dirs() -> None:
    with pytest.raises(TwoNodeE2EEvidenceError) as unapproved:
        TwoNodeE2EEvidenceConfig.from_env(
            evidence_root=Path("/tmp/nhms-two-node-e2e-unapproved"),
            run_id=_run_id("bad-root"),
        )
    assert unapproved.value.error_code == "TWO_NODE_E2E_EVIDENCE_ROOT_UNAPPROVED"

    run_id = _run_id("symlink")
    evidence_root = _evidence_root()
    target = evidence_root / f"{run_id}-target"
    target.mkdir(parents=True, exist_ok=True)
    run_link = evidence_root / run_id
    if run_link.exists() or run_link.is_symlink():
        run_link.unlink()
    run_link.symlink_to(target, target_is_directory=True)
    config = TwoNodeE2EEvidenceConfig.from_env(evidence_root=evidence_root, run_id=run_id, force=True)

    with pytest.raises(TwoNodeE2EEvidenceError) as symlink_error:
        validate_two_node_e2e_evidence(config)
    assert symlink_error.value.error_code == "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE"


def test_stale_pass_evidence_is_blocked_when_authoritative_files_are_missing_or_old() -> None:
    missing_run_id = _run_id("stale-missing")
    missing_config = _seed_pass_bundle(missing_run_id)
    (missing_config.run_dir / "db" / "readonly-db-boundary" / "role.json").unlink()

    missing_summary = validate_two_node_e2e_evidence(missing_config)

    assert missing_summary["status"] == STATUS_BLOCKED
    assert missing_summary["lane_summaries"]["readonly_db"]["status"] == STATUS_BLOCKED

    old_run_id = _run_id("stale-old")
    old_config = _seed_pass_bundle(old_run_id)
    api_summary = _read(old_config.run_dir / "api" / "summary.json")
    api_summary["evidence_run_id"] = "older-bundle"
    _write(old_config.run_dir / "api" / "summary.json", api_summary)

    old_summary = validate_two_node_e2e_evidence(old_config)

    assert old_summary["status"] == STATUS_BLOCKED
    assert old_summary["lane_summaries"]["api"]["status"] == STATUS_BLOCKED
    assert _codes(old_summary["lane_summaries"]["api"]["blockers"]) >= {
        "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_MISMATCH"
    }


def _seed_pass_bundle(
    run_id: str,
    *,
    sources: tuple[str, ...] = ("GFS", "IFS"),
    reduced_scope: bool = False,
) -> TwoNodeE2EEvidenceConfig:
    config = TwoNodeE2EEvidenceConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=run_id,
        declared_sources=sources,
        reduced_scope=reduced_scope,
        force=True,
    )
    identities = _identities(sources)
    _write(
        config.run_dir / "run.json",
        {
            "schema": "nhms.two_node_e2e.run.v1",
            "evidence_run_id": run_id,
            "declared_sources": list(sources),
            "reduced_scope": reduced_scope,
            "strict_identities": identities,
        },
    )
    _write(
        config.run_dir / "docker-preflight" / "summary.json",
        {
            "schema_version": "nhms.two_node_docker.preflight.v1",
            "status": STATUS_PASS,
            "evidence_run_id": run_id,
            "evidence_root": str(config.run_dir / "docker-preflight"),
            "tmpdir": str(REPO_ROOT / "artifacts" / "tmp"),
            "docker_root_dir": "/var/lib/docker",
            "min_free_bytes": 1024,
            "commands": {
                "docker_version": {"returncode": 0},
                "docker_compose_version": {"returncode": 0},
                "docker_info_docker_root": {"returncode": 0},
                "docker_system_df": {"returncode": 0},
                "df_h": {"returncode": 0},
            },
            "disk": {
                "evidence_root": {"path": str(config.run_dir / "docker-preflight"), "free_bytes": 4096},
                "tmpdir": {"path": str(REPO_ROOT / "artifacts" / "tmp"), "free_bytes": 4096},
                "docker_root": {"path": "/var/lib/docker", "free_bytes": 4096},
            },
        },
    )
    _write(
        config.run_dir / "docker-security" / "summary.json",
        {
            "status": STATUS_PASS,
            "evidence_run_id": run_id,
            "live_docker_evidence": True,
            "runtime_config": {
                "service_role": "display_readonly",
                "display_readonly": True,
                "slurm_routes_enabled": False,
            },
            "slurm_routes_unavailable": True,
            "slurm_route_available": False,
            "published_artifacts_readonly": True,
            "root_filesystem_readonly": True,
            "cap_drop_all": True,
            "docker_socket_present": False,
            "slurm_cli_present": False,
            "slurm_config_present": False,
            "slurm_socket_present": False,
            "munge_path_present": False,
            "privileged": False,
            "host_network": False,
            "host_pid": False,
            "host_ipc": False,
            "cap_add_present": False,
            "forbidden_hostconfig_hazard": False,
            "forbidden_mount_hazard": False,
            "forbidden_env_hazard": False,
            "broad_host_bind_present": False,
            "private_workspace_bind_present": False,
            "workspace_mount_present": False,
            "writable_published_artifact_mount": False,
            "display_write_capability_present": False,
        },
    )
    _write_readonly_db_lane(config, identities)
    _write(
        config.run_dir / "api" / "summary.json",
        _source_lane_payload(
            run_id,
            identities,
            required_checks=("latest_product", "series", "ops_status", "ops_stages", "jobs"),
            live_flag="live_api_evidence",
        ),
    )
    _write(
        config.run_dir / "browser" / "summary.json",
        _source_lane_payload(
            run_id,
            identities,
            required_checks=("hydro_met", "ops", *(() if len(sources) == 1 else ("source_switch",))),
            live_flag="live_browser_evidence",
        ),
    )
    _write(
        config.run_dir / "logs" / "summary.json",
        _source_lane_payload(
            run_id,
            identities,
            required_checks=("job_logs",),
            live_flag="live_log_evidence",
        ),
    )
    _write(
        config.run_dir / "slurm" / "summary.json",
        {"status": STATUS_PASS, "evidence_run_id": run_id, "live_slurm_evidence": True},
    )
    _write(
        config.run_dir / "manual-ops" / "summary.json",
        {
            "status": STATUS_PASS,
            "evidence_run_id": run_id,
            "production_operator_auth_evidence": True,
            "display_actions": [
                {
                    "node": "27",
                    "action": "retry",
                    "outcome": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
                    "write_executed": False,
                    "gateway_called": False,
                    "receipt_created": False,
                },
                {
                    "node": "27",
                    "action": "cancel",
                    "outcome": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
                    "write_executed": False,
                    "gateway_called": False,
                    "receipt_created": False,
                }
            ],
            "control_receipts": [
                {
                    "node": "22",
                    "producer_role": "compute_control",
                    "action": "retry",
                    "actual": True,
                    **copy.deepcopy(next(iter(identities.values()))),
                }
            ],
        },
    )
    _write(
        config.run_dir / "22-compute" / "summary.json",
        {"status": "ready", "evidence_run_id": run_id, "live_compute_evidence": True},
    )
    _write(
        config.run_dir / "27-display" / "summary.json",
        {"status": "ready", "evidence_run_id": run_id, "live_display_evidence": True},
    )
    _write(
        config.run_dir / "cross-plane" / "summary.json",
        {
            "status": STATUS_PASS,
            "evidence_run_id": run_id,
            "live_cross_plane_evidence": True,
            "sources": {
                source: {"status": STATUS_PASS, "identity": copy.deepcopy(identity)}
                for source, identity in identities.items()
            },
        },
    )
    return config


def _write_readonly_db_lane(config: TwoNodeE2EEvidenceConfig, identities: dict[str, dict[str, str]]) -> None:
    first_identity = copy.deepcopy(next(iter(identities.values())))
    permission_targets = (
        ("hydro.hydro_run", "hydro_run_terminal_state"),
        ("hydro.river_timeseries", "hydro_display_timeseries"),
        ("met.forecast_cycle", "met_cycle_state"),
        ("met.forcing_station_timeseries", "met_station_timeseries"),
        ("ops.pipeline_job", "pipeline_job_state"),
        ("ops.pipeline_event", "pipeline_event_audit"),
        ("reachable_roles", "reachable_role_membership"),
        ("audited_schema_sequences", "audited_schema_sequence_catalog"),
        ("nhms", "current_database_create_catalog"),
        ("hydro.*", "schema_table_ddl"),
        ("met.*", "schema_table_ddl"),
        ("ops.*", "schema_table_ddl"),
    )
    permission_probes = [
        {
            "target": target,
            "surface": surface,
            "status": STATUS_PASS,
            "operations": [
                {
                    "operation": "INSERT" if "." in target and "*" not in target else "DDL_CREATE_TABLE",
                    "status": STATUS_PASS,
                    "privilege_allowed": False,
                    "execution_outcome": "permission_denied",
                }
            ],
        }
        for target, surface in permission_targets
    ]
    strict_query = (
        f"source={first_identity['source']}&cycle_time={first_identity['cycle_time']}"
        f"&run_id={first_identity['run_id']}&model_id={first_identity['model_id']}"
    )
    route_smoke = [
        {"name": "health", "status": STATUS_PASS, "path": "/health"},
        {"name": "runtime_config", "status": STATUS_PASS, "path": "/api/v1/runtime/config"},
        {"name": "models", "status": STATUS_PASS, "path": "/api/v1/models?active=all&limit=1"},
        {
            "name": "stations",
            "status": STATUS_PASS,
            "path": f"/api/v1/met/stations?model_id={first_identity['model_id']}&limit=1",
        },
        {
            "name": "latest_product",
            "status": STATUS_PASS,
            "path": f"/api/v1/mvp/qhh/latest-product?{strict_query}",
            "strict_identity": first_identity,
        },
        {
            "name": "pipeline_status",
            "status": STATUS_PASS,
            "path": f"/api/v1/pipeline/status?{strict_query}",
            "strict_identity": first_identity,
        },
        {
            "name": "pipeline_stages",
            "status": STATUS_PASS,
            "path": f"/api/v1/pipeline/stages?{strict_query}",
            "strict_identity": first_identity,
        },
        {
            "name": "jobs",
            "status": STATUS_PASS,
            "path": f"/api/v1/jobs?{strict_query}&limit=1",
            "strict_identity": first_identity,
        },
        {
            "name": "job_logs",
            "status": STATUS_PASS,
            "path": f"/api/v1/jobs/{first_identity['job_id']}/logs?{strict_query}&job_id={first_identity['job_id']}",
            "strict_identity": first_identity,
        },
    ]
    role = {
        "evidence_run_id": config.run_id,
        "current_user": "display_ro",
        "role_type": "readonly_candidate",
    }
    lane = config.run_dir / "db" / "readonly-db-boundary"
    _write(lane / "role.json", role)
    _write(lane / "route_smoke.json", route_smoke)
    _write(lane / "permission_probes.json", permission_probes)
    _write(
        lane / "summary.json",
        {
            "schema": READONLY_DB_LIVE_SCHEMA,
            "status": STATUS_PASS,
            "run_id": config.run_id,
            "database_url": "postgresql://db.example:5432/nhms",
            "validation_provenance": {"mode": "live", "live_readonly_proof": True},
            "role": role,
            "display_identity": first_identity,
            "route_smoke": route_smoke,
            "manual_action_probes": [
                {
                    "name": "display_retry_manual_action",
                    "action": "retry",
                    "status": STATUS_PASS,
                    "http_status": 409,
                    "observed_error_code": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
                    "write_dependency_constructed": False,
                },
                {
                    "name": "display_cancel_manual_action",
                    "action": "cancel",
                    "status": STATUS_PASS,
                    "http_status": 409,
                    "observed_error_code": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
                    "write_dependency_constructed": False,
                },
            ],
            "permission_probes": permission_probes,
        },
    )


def _source_lane_payload(
    run_id: str,
    identities: dict[str, dict[str, str]],
    *,
    required_checks: tuple[str, ...],
    live_flag: str,
) -> dict[str, Any]:
    return {
        "status": STATUS_PASS,
        "evidence_run_id": run_id,
        live_flag: True,
        "sources": {
            source: {
                "status": STATUS_PASS,
                "identity": copy.deepcopy(identity),
                "checks": {
                    check: {"status": STATUS_PASS, "identity": copy.deepcopy(identity)}
                    for check in required_checks
                },
            }
            for source, identity in identities.items()
        },
    }


def _identities(sources: tuple[str, ...]) -> dict[str, dict[str, str]]:
    return {
        source: {
            "run_id": f"hydro-{source.lower()}-{uuid4().hex[:8]}",
            "source": source,
            "cycle_time": "2026-05-29T00:00:00Z",
            "model_id": "basins_qhh_shud",
            "job_id": f"job-{source.lower()}-{uuid4().hex[:8]}",
        }
        for source in sources
    }


def _deep_update(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _evidence_root() -> Path:
    return REPO_ROOT / "artifacts" / "test-two-node-e2e-evidence"


def _run_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _codes(items: list[dict[str, Any]]) -> set[str]:
    return {str(item.get("code")) for item in items}
