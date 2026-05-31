from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from scripts import validate_two_node_docker_runtime as docker_runtime
from services.production_closure.readonly_db_validation import merge_readonly_db_source_evidence
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


def test_docker_preflight_without_embedded_bundle_id_blocks_even_under_current_run() -> None:
    run_id = _run_id("preflight-no-id")
    config = _seed_pass_bundle(run_id)
    preflight = _read(config.run_dir / "docker-preflight" / "summary.json")
    preflight.pop("evidence_run_id")
    _write(config.run_dir / "docker-preflight" / "summary.json", preflight)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    docker_preflight = summary["lane_summaries"]["docker_preflight"]
    assert docker_preflight["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_MISSING" in _codes(docker_preflight["blockers"])


def test_docker_preflight_current_run_id_can_pass_lane() -> None:
    run_id = _run_id("preflight-current-id")
    config = _seed_pass_bundle(run_id)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["docker_preflight"]["status"] == STATUS_PASS


def test_stale_no_id_docker_preflight_copied_under_current_run_blocks() -> None:
    old_run_id = _run_id("old-preflight")
    old_config = _seed_pass_bundle(old_run_id)
    stale_preflight = _read(old_config.run_dir / "docker-preflight" / "summary.json")
    stale_preflight.pop("evidence_run_id")

    new_run_id = _run_id("new-preflight")
    new_config = _seed_pass_bundle(new_run_id)
    _write(new_config.run_dir / "docker-preflight" / "summary.json", stale_preflight)

    summary = validate_two_node_e2e_evidence(new_config)

    docker_preflight = summary["lane_summaries"]["docker_preflight"]
    assert summary["status"] == STATUS_BLOCKED
    assert docker_preflight["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_MISSING" in _codes(docker_preflight["blockers"])


def test_docker_security_producer_summary_with_verified_children_passes() -> None:
    run_id = _run_id("docker-verified")
    config = _seed_pass_bundle(run_id)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    docker_lane = summary["lane_summaries"]["docker_security"]
    assert docker_lane["status"] == STATUS_PASS
    redacted = docker_lane["redacted_evidence"]
    assert redacted["schema_version"] == "nhms.two_node_docker.security_summary.v1"
    assert set(redacted["source_artifacts"]) == {"source_trust", "static", "smoke"}


def test_real_static_helper_output_feeds_security_summary_and_final_pass() -> None:
    run_id = _run_id("docker-real-static")
    config = _seed_pass_bundle(run_id)
    docker_security_dir = config.run_dir / "docker-security"
    static_result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=Path("infra/compose.display.yml"),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )
    assert static_result.status == STATUS_PASS
    static_report = docker_runtime.write_static_report(
        static_result,
        docker_security_dir / "static-compose-env-check.json",
        REPO_ROOT,
    )
    docker_runtime.write_docker_security_summary(
        output=docker_security_dir / "summary.json",
        repo_root=REPO_ROOT,
        evidence_run_id=run_id,
        source_trust_report=[
            docker_security_dir / "two-node-docker-source-trust.json",
            docker_security_dir / "two-node-docker-source-trust-display.json",
        ],
        static_report=static_report,
        smoke_report=docker_security_dir / "docker-smoke.json",
    )

    summary = validate_two_node_e2e_evidence(config)

    docker_lane = summary["lane_summaries"]["docker_security"]
    assert summary["status"] == STATUS_PASS
    assert docker_lane["status"] == STATUS_PASS


@pytest.mark.parametrize("mutation", ["missing_role_env_labels", "unsafe_role_env_labels"])
def test_docker_security_source_trust_empty_roles_without_safe_role_env_proof_blocks(mutation: str) -> None:
    run_id = _run_id(f"docker-source-trust-empty-roles-{mutation}")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    artifact = docker_security["source_artifacts"]["source_trust"][0]
    artifact_path = Path(artifact["path"])
    payload = _read(artifact_path)
    payload["roles"] = []
    if mutation == "missing_role_env_labels":
        payload["checked_paths"] = [
            record for record in payload["checked_paths"] if not str(record["label"]).endswith("role env")
        ]
    else:
        for record in payload["checked_paths"]:
            if str(record["label"]).endswith("role env"):
                record["mode"] = "0644"
    _write(artifact_path, payload)
    artifact["sha256"] = _sha256_file(artifact_path)
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    docker_lane = summary["lane_summaries"]["docker_security"]
    assert summary["status"] == STATUS_BLOCKED
    assert docker_lane["status"] == STATUS_BLOCKED
    if mutation == "missing_role_env_labels":
        expected_code = "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_REQUIRED_LABEL_MISSING"
    else:
        expected_code = "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_ROLE_ENV_MODE_INVALID"
    role_env_blockers = {
        (blocker["code"], blocker["label"])
        for blocker in docker_lane["blockers"]
        if blocker.get("label") in {"compute role env", "display role env"}
    }
    assert (expected_code, "compute role env") in role_env_blockers


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


def test_docker_preflight_missing_df_h_evidence_blocks() -> None:
    run_id = _run_id("preflight-missing-df-h")
    config = _seed_pass_bundle(run_id)
    preflight = _read(config.run_dir / "docker-preflight" / "summary.json")
    preflight["commands"].pop("df_h")
    _write(config.run_dir / "docker-preflight" / "summary.json", preflight)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_DOCKER_PREFLIGHT_COMMAND_EVIDENCE_MISSING" in _codes(
        summary["lane_summaries"]["docker_preflight"]["blockers"]
    )


def test_docker_preflight_pass_with_producer_blockers_blocks() -> None:
    run_id = _run_id("preflight-producer-blockers")
    config = _seed_pass_bundle(run_id)
    preflight = _read(config.run_dir / "docker-preflight" / "summary.json")
    preflight["blockers"] = [{"code": "LOW_DISK_SPACE"}]
    _write(config.run_dir / "docker-preflight" / "summary.json", preflight)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_DOCKER_PREFLIGHT_PRODUCER_BLOCKERS_PRESENT" in _codes(
        summary["lane_summaries"]["docker_preflight"]["blockers"]
    )


def test_docker_preflight_pass_with_low_free_bytes_blocks() -> None:
    run_id = _run_id("preflight-low-free")
    config = _seed_pass_bundle(run_id)
    preflight = _read(config.run_dir / "docker-preflight" / "summary.json")
    preflight["disk"]["tmpdir"]["free_bytes"] = 512
    _write(config.run_dir / "docker-preflight" / "summary.json", preflight)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_DOCKER_PREFLIGHT_LOW_DISK_SPACE" in _codes(
        summary["lane_summaries"]["docker_preflight"]["blockers"]
    )


@pytest.mark.parametrize("path_key", ["tmpdir", "evidence_root"])
def test_docker_preflight_pass_with_unsafe_recorded_path_blocks(path_key: str) -> None:
    run_id = _run_id(f"preflight-unsafe-{path_key}")
    config = _seed_pass_bundle(run_id)
    preflight = _read(config.run_dir / "docker-preflight" / "summary.json")
    preflight[path_key] = "/tmp/not-approved"
    _write(config.run_dir / "docker-preflight" / "summary.json", preflight)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_RECORDED_PATH_OUTSIDE_APPROVED_ROOTS" in _codes(
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


def test_docker_security_static_child_missing_final_required_proof_blocks() -> None:
    run_id = _run_id("docker-static-proof-missing")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    artifact = docker_security["source_artifacts"]["static"]
    artifact_path = Path(artifact["path"])
    payload = _read(artifact_path)
    payload.pop("docker_socket_present")
    _write(artifact_path, payload)
    artifact["sha256"] = _sha256_file(artifact_path)
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    docker_lane = summary["lane_summaries"]["docker_security"]
    assert summary["status"] == STATUS_BLOCKED
    assert docker_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_DOCKER_STATIC_CHILD_PROOF_MISSING" in _codes(docker_lane["blockers"])


def test_docker_security_sourceless_summary_blocks_not_passes() -> None:
    run_id = _run_id("docker-sourceless")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    docker_security.pop("source_artifacts")
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    docker_lane = summary["lane_summaries"]["docker_security"]
    assert summary["status"] == STATUS_BLOCKED
    assert docker_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACTS_MISSING" in _codes(docker_lane["blockers"])


@pytest.mark.parametrize(
    ("mutator", "expected_status", "expected_code"),
    [
        ("missing_child", STATUS_BLOCKED, "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_MISSING"),
        ("child_blocked", STATUS_BLOCKED, "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_NOT_PASS"),
        (
            "outside_root",
            STATUS_BLOCKED,
            "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_OUTSIDE_APPROVED_ROOT",
        ),
        ("hash_mismatch", STATUS_BLOCKED, "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_HASH_MISMATCH"),
    ],
)
def test_docker_security_source_artifact_contract_blocks_false_pass(
    mutator: str,
    expected_status: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"docker-child-{mutator}")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    static_artifact = docker_security["source_artifacts"]["static"]
    static_path = Path(static_artifact["path"])
    if mutator == "missing_child":
        static_path.unlink()
    elif mutator == "child_blocked":
        static_payload = _read(static_path)
        static_payload["status"] = STATUS_BLOCKED
        _write(static_path, static_payload)
        static_artifact["sha256"] = _sha256_file(static_path)
    elif mutator == "outside_root":
        static_artifact["path"] = "/tmp/nhms-forged-static.json"
    else:
        static_artifact["sha256"] = "0" * 64
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    docker_lane = summary["lane_summaries"]["docker_security"]
    assert summary["status"] == expected_status
    assert docker_lane["status"] == expected_status
    assert expected_code in _codes(docker_lane["blockers"])


def test_docker_security_child_with_explicit_stale_run_id_blocks_even_under_current_run() -> None:
    run_id = _run_id("docker-child-stale")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    static_artifact = docker_security["source_artifacts"]["static"]
    static_path = Path(static_artifact["path"])
    static_payload = _read(static_path)
    static_payload["evidence_run_id"] = "older-bundle"
    _write(static_path, static_payload)
    static_artifact["sha256"] = _sha256_file(static_path)
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    docker_lane = summary["lane_summaries"]["docker_security"]
    assert summary["status"] == STATUS_BLOCKED
    assert docker_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_STALE_OR_UNSCOPED" in _codes(
        docker_lane["blockers"]
    )


def test_docker_security_source_trust_without_current_run_id_blocks_even_under_current_run() -> None:
    run_id = _run_id("docker-source-trust-no-id")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    artifact = docker_security["source_artifacts"]["source_trust"][0]
    artifact_path = Path(artifact["path"])
    payload = _read(artifact_path)
    payload.pop("evidence_run_id", None)
    _write(artifact_path, payload)
    artifact["sha256"] = _sha256_file(artifact_path)
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    docker_lane = summary["lane_summaries"]["docker_security"]
    assert summary["status"] == STATUS_BLOCKED
    assert docker_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_STALE_OR_UNSCOPED" in _codes(
        docker_lane["blockers"]
    )


@pytest.mark.parametrize("mutation", ["missing", "empty", "missing_label"])
def test_docker_security_source_trust_checked_paths_contract_blocks(mutation: str) -> None:
    run_id = _run_id(f"docker-source-trust-{mutation}")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    artifact = docker_security["source_artifacts"]["source_trust"][0]
    artifact_path = Path(artifact["path"])
    payload = _read(artifact_path)
    if mutation == "missing":
        payload.pop("checked_paths")
    elif mutation == "empty":
        payload["checked_paths"] = []
    else:
        payload["checked_paths"] = [
            record for record in payload["checked_paths"] if record["label"] != "display compose source"
        ]
    _write(artifact_path, payload)
    artifact["sha256"] = _sha256_file(artifact_path)
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    docker_lane = summary["lane_summaries"]["docker_security"]
    assert summary["status"] == STATUS_BLOCKED
    assert docker_lane["status"] == STATUS_BLOCKED
    assert _codes(docker_lane["blockers"]) & {
        "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_CHECKED_PATHS_MISSING",
        "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_REQUIRED_LABEL_MISSING",
    }


@pytest.mark.parametrize(
    ("record_patch", "expected_code"),
    [
        ({"trusted_owner": False}, "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_OWNER_UNTRUSTED"),
        ({"is_symlink": True}, "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_SYMLINK"),
        (
            {"expected_kind": "directory", "is_regular": False, "is_directory": True},
            "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_KIND_MISMATCH",
        ),
        ({"group_writable": True}, "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_GROUP_WRITABLE"),
        ({"world_writable": True}, "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_WORLD_WRITABLE"),
        ({"mode": "0644"}, "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_ROLE_ENV_MODE_INVALID"),
    ],
)
def test_docker_security_source_trust_unsafe_checked_path_record_blocks(
    record_patch: dict[str, Any],
    expected_code: str,
) -> None:
    run_id = _run_id("docker-source-trust-unsafe")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    artifact = docker_security["source_artifacts"]["source_trust"][1]
    artifact_path = Path(artifact["path"])
    payload = _read(artifact_path)
    record = next(item for item in payload["checked_paths"] if item["label"] == "display role env")
    record.update(record_patch)
    _write(artifact_path, payload)
    artifact["sha256"] = _sha256_file(artifact_path)
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    docker_lane = summary["lane_summaries"]["docker_security"]
    assert summary["status"] == STATUS_BLOCKED
    assert docker_lane["status"] == STATUS_BLOCKED
    assert expected_code in _codes(docker_lane["blockers"])


@pytest.mark.parametrize(
    ("artifact_name", "mutation", "expected_status", "expected_code"),
    [
        (
            "source_trust",
            {"blockers": [{"code": "UNTRUSTED_OWNER"}]},
            STATUS_BLOCKED,
            "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_PRODUCER_BLOCKERS_PRESENT",
        ),
        (
            "static",
            {"findings": [{"code": "DISPLAY_FORBIDDEN_MOUNT"}]},
            STATUS_FAIL,
            "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_PRODUCER_FINDINGS_PRESENT",
        ),
        (
            "static",
            {"HostConfig": {"Privileged": True}},
            STATUS_FAIL,
            "TWO_NODE_E2E_DOCKER_DISPLAY_FORBIDDEN_CAPABILITY",
        ),
        (
            "smoke",
            {"commands": None},
            STATUS_BLOCKED,
            "TWO_NODE_E2E_DOCKER_SMOKE_LIVE_COMMAND_EVIDENCE_MISSING",
        ),
    ],
)
def test_docker_security_child_subcontracts_reject_unsafe_pass(
    artifact_name: str,
    mutation: dict[str, Any],
    expected_status: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"docker-child-{artifact_name}")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    artifact = docker_security["source_artifacts"][artifact_name]
    if isinstance(artifact, list):
        artifact = artifact[0]
    artifact_path = Path(artifact["path"])
    payload = _read(artifact_path)
    if "commands" in mutation and mutation["commands"] is None:
        payload.pop("commands", None)
    else:
        _deep_update(payload, mutation)
    _write(artifact_path, payload)
    artifact["sha256"] = _sha256_file(artifact_path)
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    docker_lane = summary["lane_summaries"]["docker_security"]
    assert summary["status"] == expected_status
    assert docker_lane["status"] == expected_status
    assert expected_code in _codes(docker_lane["blockers"]) | _codes(docker_lane["findings"])


@pytest.mark.parametrize(
    "raw_evidence",
    [
        {"HostConfig": {"Privileged": True}},
        {"HostConfig": {"NetworkMode": "host"}},
        {"HostConfig": {"PidMode": "host"}},
        {"HostConfig": {"IpcMode": "host"}},
        {"HostConfig": {"CapAdd": ["SYS_ADMIN"]}},
        {"Mounts": [{"Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": False}]},
        {"Mounts": [{"Source": "/scratch/private-data", "Destination": "/private", "RW": False}]},
        {"Mounts": [{"Source": "/scratch/private/workspace", "Destination": "/workspace", "RW": False}]},
        {"Mounts": [{"Source": "/", "Destination": "/host", "RW": False}]},
        {"Config": {"Env": ["WORKSPACE_ROOT=/workspace"]}},
        {"host_config": {"privileged": True}},
        {"config": {"env": ["DOCKER_HOST=unix:///var/run/docker.sock"]}},
        {"mounts": [{"source": "/scratch/private-data", "target": "/private", "read_only": True}]},
        {"pid": "host"},
        {"ipc": "host"},
        {"cap_drop": []},
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


def test_docker_security_published_mount_missing_readonly_proof_blocks() -> None:
    run_id = _run_id("docker-mount-unknown")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    docker_security["published_artifacts_readonly"] = True
    docker_security["writable_published_artifact_mount"] = False
    docker_security["compose_service"] = {
        "volumes": [
            {
                "type": "bind",
                "source": "/srv/nhms/published",
                "target": "/var/lib/nhms/published",
            }
        ]
    }
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    docker_lane = summary["lane_summaries"]["docker_security"]
    assert summary["status"] == STATUS_BLOCKED
    assert docker_lane["status"] == STATUS_BLOCKED
    assert {
        "TWO_NODE_E2E_DOCKER_DISPLAY_PROOF_MISSING",
        "TWO_NODE_E2E_DOCKER_STATIC_CHILD_PROOF_MISSING",
    } & _codes(docker_lane["blockers"])


def test_docker_security_published_mount_explicit_readonly_passes() -> None:
    run_id = _run_id("docker-mount-ro")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    docker_security["compose_service"] = {
        "volumes": [
            {
                "type": "bind",
                "source": "/srv/nhms/published",
                "target": "/var/lib/nhms/published",
                "read_only": True,
            }
        ]
    }
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["docker_security"]["status"] == STATUS_PASS


def test_docker_security_published_mount_explicit_writable_fails() -> None:
    run_id = _run_id("docker-mount-rw")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    docker_security["compose_service"] = {
        "volumes": [
            {
                "type": "bind",
                "source": "/srv/nhms/published",
                "target": "/var/lib/nhms/published",
                "read_only": False,
            }
        ]
    }
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)

    summary = validate_two_node_e2e_evidence(config)

    docker_lane = summary["lane_summaries"]["docker_security"]
    assert summary["status"] == STATUS_FAIL
    assert docker_lane["status"] == STATUS_FAIL
    assert "TWO_NODE_E2E_DOCKER_DISPLAY_FORBIDDEN_CAPABILITY" in _codes(docker_lane["findings"])


@pytest.mark.parametrize(
    "proof_patch",
    [
        {"docker_socket_present": False, "security": {"docker_socket_present": True}},
        {
            "docker_socket_present": False,
            "docker_inspect": [
                {"Mounts": [{"Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": False}]}
            ],
        },
        {
            "published_artifacts_readonly": True,
            "docker_inspect": [
                {
                    "Mounts": [
                        {"Source": "/srv/nhms/published", "Destination": "/var/lib/nhms/published", "RW": True}
                    ]
                }
            ],
        },
    ],
)
def test_docker_security_nested_unsafe_proofs_override_top_level_safe(proof_patch: dict[str, Any]) -> None:
    run_id = _run_id("docker-nested-unsafe")
    config = _seed_pass_bundle(run_id)
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    _deep_update(docker_security, proof_patch)
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


@pytest.mark.parametrize("mode", [None, "simulated", "fixture", "production"])
def test_readonly_db_pass_requires_live_validation_provenance_mode(mode: str | None) -> None:
    run_id = _run_id(f"db-mode-{mode or 'missing'}")
    config = _seed_pass_bundle(run_id)
    db_summary = _read(config.run_dir / "db" / "readonly-db-boundary" / "summary.json")
    if mode is None:
        db_summary["validation_provenance"].pop("mode", None)
    else:
        db_summary["validation_provenance"]["mode"] = mode
    db_summary["validation_provenance"]["live_readonly_proof"] = True
    _write(config.run_dir / "db" / "readonly-db-boundary" / "summary.json", db_summary)

    summary = validate_two_node_e2e_evidence(config)

    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert summary["status"] == STATUS_BLOCKED
    assert readonly_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_READONLY_DB_LIVE_MODE_MISSING" in _codes(readonly_lane["blockers"])


@pytest.mark.parametrize("proof_state", ["false", "missing"])
def test_readonly_db_pass_requires_live_readonly_proof(proof_state: str) -> None:
    run_id = _run_id(f"db-live-proof-{proof_state}")
    config = _seed_pass_bundle(run_id)
    db_summary = _read(config.run_dir / "db" / "readonly-db-boundary" / "summary.json")
    db_summary["validation_provenance"]["mode"] = "live"
    if proof_state == "missing":
        db_summary["validation_provenance"].pop("live_readonly_proof", None)
    else:
        db_summary["validation_provenance"]["live_readonly_proof"] = False
    _write(config.run_dir / "db" / "readonly-db-boundary" / "summary.json", db_summary)

    summary = validate_two_node_e2e_evidence(config)

    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert summary["status"] == STATUS_BLOCKED
    assert readonly_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_READONLY_DB_LIVE_PROOF_MISSING" in _codes(readonly_lane["blockers"])


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        ("remove_source_artifacts", "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACTS_MISSING"),
        ("missing_path", "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_PATH_MISSING"),
        ("missing_sha", "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_SHA_MISSING"),
        ("hash_mismatch", "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_HASH_MISMATCH"),
        ("stale_source_run", "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_RUN_ID_MISMATCH"),
        ("unsafe_path", "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_PATH_UNSAFE"),
        ("missing_ifs", "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_SOURCE_COVERAGE_MISSING"),
    ],
)
def test_readonly_db_final_pass_requires_merged_source_artifact_provenance(
    mutator: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"db-source-artifact-{mutator}")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    source_artifacts = db_summary["validation_provenance"]["source_artifacts"]
    if mutator == "remove_source_artifacts":
        db_summary["validation_provenance"].pop("source_artifacts")
    elif mutator == "missing_path":
        source_artifacts[0]["artifacts"]["summary.json"].pop("path")
    elif mutator == "missing_sha":
        source_artifacts[0]["artifacts"]["summary.json"].pop("sha256")
    elif mutator == "hash_mismatch":
        source_artifacts[0]["artifacts"]["summary.json"]["sha256"] = "0" * 64
    elif mutator == "stale_source_run":
        source_artifacts[0]["artifacts"]["summary.json"]["run_id"] = "older-source-run"
    elif mutator == "unsafe_path":
        source_artifacts[0]["artifacts"]["summary.json"]["path"] = "/tmp/readonly-summary.json"
    elif mutator == "missing_ifs":
        db_summary["validation_provenance"]["source_artifacts"] = [
            artifact for artifact in source_artifacts if artifact["sources"] == ["GFS"]
        ]
    _write(lane / "summary.json", db_summary)

    summary = validate_two_node_e2e_evidence(config)

    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert summary["status"] == STATUS_BLOCKED
    assert readonly_lane["status"] == STATUS_BLOCKED
    assert expected_code in _codes(readonly_lane["blockers"])


def test_readonly_db_final_pass_requires_merged_source_flag() -> None:
    run_id = _run_id("db-source-flag")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    db_summary["validation_provenance"]["merged_source_evidence"] = False
    _write(lane / "summary.json", db_summary)

    summary = validate_two_node_e2e_evidence(config)

    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert summary["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_READONLY_DB_MERGED_SOURCE_EVIDENCE_MISSING" in _codes(readonly_lane["blockers"])


def test_readonly_db_source_artifact_coverage_is_payload_proven() -> None:
    run_id = _run_id("db-source-payload-mismatch")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    source_artifacts = db_summary["validation_provenance"]["source_artifacts"]
    gfs_record = next(artifact for artifact in source_artifacts if artifact["sources"] == ["GFS"])
    ifs_record = next(artifact for artifact in source_artifacts if artifact["sources"] == ["IFS"])
    gfs_dir = Path(gfs_record["source_dir"])
    ifs_dir = Path(ifs_record["source_dir"])
    gfs_summary = _read(gfs_dir / "summary.json")
    ifs_summary = _read(ifs_dir / "summary.json")

    ifs_summary["display_identity"] = copy.deepcopy(gfs_summary["display_identity"])
    ifs_summary["route_smoke"] = copy.deepcopy(gfs_summary["route_smoke"])
    _write(ifs_dir / "summary.json", ifs_summary)
    _write(ifs_dir / "route_smoke.json", ifs_summary["route_smoke"])
    for filename in ("summary.json", "route_smoke.json"):
        ifs_record["artifacts"][filename] = _readonly_source_artifact(
            ifs_dir / filename,
            ifs_summary["run_id"],
        )
    _write(lane / "summary.json", db_summary)

    summary = validate_two_node_e2e_evidence(config)

    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert summary["status"] == STATUS_BLOCKED
    assert readonly_lane["status"] == STATUS_BLOCKED
    assert _codes(readonly_lane["blockers"]) & {
        "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_SOURCE_MISMATCH",
        "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_DUPLICATE_SOURCE",
    }


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
        latest["response_identity"].pop("model_id")
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
    "mutation",
    [
        {"write_dependency_constructed": True, "write_executed": False},
        {"write_dependency_constructed": False, "write_executed": True},
        {"write_dependency_constructed": None, "write_executed": False},
        {"write_dependency_constructed": False, "write_executed": None},
    ],
)
def test_readonly_db_manual_action_no_write_fields_are_independent(mutation: dict[str, Any]) -> None:
    run_id = _run_id("db-manual-no-write")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    action = db_summary["manual_action_probes"][0]
    for key, value in mutation.items():
        if value is None:
            action.pop(key, None)
        else:
            action[key] = value
    _write(lane / "summary.json", db_summary)

    summary = validate_two_node_e2e_evidence(config)

    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert summary["status"] == STATUS_BLOCKED
    assert readonly_lane["status"] == STATUS_BLOCKED
    assert (
        "TWO_NODE_E2E_READONLY_DB_MANUAL_ACTION_WRITE_PROOF_FAILED" in _codes(readonly_lane["blockers"])
        or "TWO_NODE_E2E_READONLY_DB_MANUAL_ACTION_NO_WRITE_PROOF_MISSING" in _codes(readonly_lane["blockers"])
    )


def test_readonly_db_final_accepts_live_producer_manual_action_shape() -> None:
    run_id = _run_id("db-manual-producer-shape")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    source_dirs = [
        Path(source_artifact["source_dir"])
        for source_artifact in db_summary["validation_provenance"]["source_artifacts"]
    ]
    for source_artifact in db_summary["validation_provenance"]["source_artifacts"]:
        source_dir = Path(source_artifact["source_dir"])
        source_summary = _read(source_dir / "summary.json")
        action_run_id = str(source_summary["display_identity"]["run_id"])
        source_summary["manual_action_probes"] = _readonly_manual_actions(
            include_action=False,
            run_id=action_run_id,
        )
        source_summary["route_smoke"] = [
            _route_without_embedded_identity(route) for route in source_summary["route_smoke"]
        ]
        _write(source_dir / "summary.json", source_summary)
        _write(source_dir / "route_smoke.json", source_summary["route_smoke"])

    merge_readonly_db_source_evidence(
        evidence_root=config.evidence_root,
        run_id=run_id,
        source_dirs=source_dirs,
        force=True,
    )

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["readonly_db"]["status"] == STATUS_PASS


def test_readonly_db_missing_table_update_or_delete_blocks() -> None:
    run_id = _run_id("db-missing-table-ops")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    target = next(probe for probe in db_summary["permission_probes"] if probe["target"] == "hydro.hydro_run")
    target["operations"] = [operation for operation in target["operations"] if operation["operation"] == "INSERT"]
    _write(lane / "summary.json", db_summary)
    _write(lane / "permission_probes.json", db_summary["permission_probes"])

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert "TWO_NODE_E2E_READONLY_DB_PERMISSION_OPERATION_COVERAGE_MISSING" in _codes(
        readonly_lane["blockers"]
    )


def test_readonly_db_missing_schema_ddl_blocks() -> None:
    run_id = _run_id("db-missing-ddl")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    target = next(probe for probe in db_summary["permission_probes"] if probe["target"] == "ops.*")
    target["operations"] = []
    _write(lane / "summary.json", db_summary)
    _write(lane / "permission_probes.json", db_summary["permission_probes"])

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert "TWO_NODE_E2E_READONLY_DB_PERMISSION_OPERATIONS_MISSING" in _codes(readonly_lane["blockers"])
    assert "TWO_NODE_E2E_READONLY_DB_PERMISSION_OPERATION_COVERAGE_MISSING" in _codes(
        readonly_lane["blockers"]
    )


@pytest.mark.parametrize(
    ("catalog_field", "catalog_value"),
    [
        ("table_privileges", {"insert": True}),
        ("column_privileges", {"update": ["status"]}),
        (
            "sequence_privileges",
            [{"sequence_name": "hydro_run_id_seq", "usage": True, "mutating_privilege_allowed": True}],
        ),
    ],
)
def test_readonly_db_mutating_catalog_fields_fail(catalog_field: str, catalog_value: Any) -> None:
    run_id = _run_id(f"db-catalog-{catalog_field}")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    target = next(probe for probe in db_summary["permission_probes"] if probe["target"] == "hydro.hydro_run")
    target[catalog_field] = catalog_value
    _write(lane / "summary.json", db_summary)
    _write(lane / "permission_probes.json", db_summary["permission_probes"])

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_FAIL
    assert "TWO_NODE_E2E_READONLY_DB_MUTATING_CATALOG_FIELD" in _codes(
        summary["lane_summaries"]["readonly_db"]["findings"]
    )


def test_readonly_db_clean_reachable_roles_empty_operations_pass() -> None:
    run_id = _run_id("db-reachable-empty")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    target = next(probe for probe in db_summary["permission_probes"] if probe["target"] == "reachable_roles")
    target["reachable_role_findings"] = []
    target["operations"] = []
    _write(lane / "summary.json", db_summary)
    _write(lane / "permission_probes.json", db_summary["permission_probes"])

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["readonly_db"]["status"] == STATUS_PASS


def test_readonly_db_full_source_bundle_with_only_gfs_route_evidence_blocks() -> None:
    run_id = _run_id("db-route-missing-source")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    db_summary["route_smoke"] = [
        route
        for route in db_summary["route_smoke"]
        if route.get("name") not in {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}
        or route.get("source") == "GFS"
    ]
    _write(lane / "summary.json", db_summary)
    _write(lane / "route_smoke.json", db_summary["route_smoke"])

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert readonly_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_READONLY_DB_ROUTE_SOURCE_COVERAGE_MISSING" in _codes(readonly_lane["blockers"])


def test_readonly_db_single_source_identity_blocks_full_scope_even_if_routes_are_present() -> None:
    run_id = _run_id("db-single-source-identity")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    db_summary["display_identity"] = {"GFS": db_summary["display_identity"]["GFS"]}
    _write(lane / "summary.json", db_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert "TWO_NODE_E2E_READONLY_DB_SOURCE_COVERAGE_MISSING" in _codes(readonly_lane["blockers"])


def test_readonly_db_ifs_route_identity_mismatch_fails() -> None:
    run_id = _run_id("db-ifs-route-mismatch")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    ifs_latest = next(
        route
        for route in db_summary["route_smoke"]
        if route.get("name") == "latest_product" and route.get("source") == "IFS"
    )
    ifs_latest["response_identity"]["model_id"] = "wrong-model"
    _write(lane / "summary.json", db_summary)
    _write(lane / "route_smoke.json", db_summary["route_smoke"])

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_FAIL
    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert readonly_lane["status"] == STATUS_FAIL
    assert "TWO_NODE_E2E_STRICT_IDENTITY_MISMATCH" in _codes(readonly_lane["findings"])


def test_readonly_db_route_response_identity_mismatch_blocks() -> None:
    run_id = _run_id("db-response-identity-mismatch")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    latest = next(
        route
        for route in db_summary["route_smoke"]
        if route.get("name") == "latest_product" and route.get("source") == "GFS"
    )
    latest["response_identity"]["model_id"] = "wrong-model"
    _write(lane / "summary.json", db_summary)
    _write(lane / "route_smoke.json", db_summary["route_smoke"])

    summary = validate_two_node_e2e_evidence(config)

    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert summary["status"] == STATUS_FAIL
    assert readonly_lane["status"] == STATUS_FAIL
    assert "TWO_NODE_E2E_STRICT_IDENTITY_MISMATCH" in _codes(readonly_lane["findings"])


def test_readonly_db_route_request_path_without_response_identity_blocks() -> None:
    run_id = _run_id("db-request-only-identity")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    latest = next(
        route
        for route in db_summary["route_smoke"]
        if route.get("name") == "latest_product" and route.get("source") == "GFS"
    )
    latest.pop("response_identity", None)
    _write(lane / "summary.json", db_summary)
    _write(lane / "route_smoke.json", db_summary["route_smoke"])

    summary = validate_two_node_e2e_evidence(config)

    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert summary["status"] == STATUS_BLOCKED
    assert readonly_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_READONLY_DB_ROUTE_STRICT_IDENTITY_INCOMPLETE" in _codes(
        readonly_lane["blockers"]
    )


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


def test_reduced_single_source_db_merge_feeds_final_partial() -> None:
    run_id = _run_id("single-source-db-merge")
    config = _seed_pass_bundle(run_id, sources=("GFS",), reduced_scope=True)
    db_summary = _read(config.run_dir / "db" / "readonly-db-boundary" / "summary.json")
    source_dirs = [
        Path(source_artifact["source_dir"])
        for source_artifact in db_summary["validation_provenance"]["source_artifacts"]
        if source_artifact["sources"] == ["GFS"]
    ]
    assert len(source_dirs) == 1
    merge_readonly_db_source_evidence(
        evidence_root=config.evidence_root,
        run_id=run_id,
        source_dirs=source_dirs,
        declared_sources=("GFS",),
        reduced_scope=True,
        force=True,
    )

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PARTIAL
    assert summary["lane_summaries"]["readonly_db"]["status"] == STATUS_PASS
    assert summary["lane_summaries"]["cross_plane"]["status"] == STATUS_PARTIAL


def test_source_lane_partial_with_strict_identity_blocker_yields_final_blocked() -> None:
    run_id = _run_id("partial-with-blocker")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    browser_summary["status"] = STATUS_PARTIAL
    browser_summary["sources"]["GFS"]["checks"]["ops_jobs"]["identity"].pop("job_id")
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    assert summary["lane_summaries"]["browser"]["status"] == STATUS_PARTIAL
    assert summary["source_scope_results"]["GFS"]["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_SOURCE_BLOCKED" in _codes(summary["blockers"])


@pytest.mark.parametrize("producer_status", [STATUS_FAIL, STATUS_BLOCKED])
def test_reduced_single_source_cross_plane_preserves_producer_fail_or_blocked(
    producer_status: str,
) -> None:
    run_id = _run_id(f"single-source-cross-{producer_status.lower()}")
    config = _seed_pass_bundle(run_id, sources=("GFS",), reduced_scope=True)
    cross_plane = _read(config.run_dir / "cross-plane" / "summary.json")
    cross_plane["status"] = producer_status
    _write(config.run_dir / "cross-plane" / "summary.json", cross_plane)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["lane_summaries"]["cross_plane"]["status"] == producer_status
    assert summary["status"] == producer_status


@pytest.mark.parametrize(
    ("lane_dir", "live_flag"),
    [
        ("api", "live_api_evidence"),
        ("browser", "live_browser_evidence"),
        ("logs", "live_log_evidence"),
    ],
)
def test_source_lane_boolean_only_live_evidence_blocks(lane_dir: str, live_flag: str) -> None:
    run_id = _run_id(f"{lane_dir}-boolean-only")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_dir / "summary.json")
    payload.pop("commands", None)
    payload[live_flag] = True
    for source in payload["sources"].values():
        for check in source["checks"].values():
            check.pop("evidence", None)
    _write(config.run_dir / lane_dir / "summary.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_dir]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert f"TWO_NODE_E2E_{lane_dir.upper()}_PRODUCER_EVIDENCE_MISSING" in _codes(lane["blockers"])


@pytest.mark.parametrize(
    ("lane_dir", "live_flag"),
    [
        ("slurm", "live_slurm_evidence"),
        ("22-compute", "live_compute_evidence"),
        ("27-display", "live_display_evidence"),
    ],
)
def test_simple_lane_boolean_only_live_evidence_blocks(lane_dir: str, live_flag: str) -> None:
    run_id = _run_id(f"{lane_dir.replace('/', '-')}-boolean-only")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_dir / "summary.json")
    payload.pop("commands", None)
    payload[live_flag] = True
    _write(config.run_dir / lane_dir / "summary.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    lane_name = {"22-compute": "compute_summary", "27-display": "display_summary"}.get(lane_dir, lane_dir)
    lane = summary["lane_summaries"][lane_name]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert f"TWO_NODE_E2E_{lane_name.upper()}_PRODUCER_EVIDENCE_MISSING" in _codes(lane["blockers"])


def test_cross_plane_boolean_only_live_evidence_blocks() -> None:
    run_id = _run_id("cross-plane-boolean-only")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / "cross-plane" / "summary.json")
    payload.pop("commands", None)
    payload["live_cross_plane_evidence"] = True
    _write(config.run_dir / "cross-plane" / "summary.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"]["cross_plane"]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_CROSS_PLANE_PRODUCER_EVIDENCE_MISSING" in _codes(lane["blockers"])


def test_browser_evidence_missing_ops_jobs_or_logs_blocks() -> None:
    run_id = _run_id("browser-missing-ops-log")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    browser_summary["sources"]["GFS"]["checks"].pop("ops_job_logs")
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    assert summary["status"] == STATUS_BLOCKED
    assert browser_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_BROWSER_CHECK_MISSING" in _codes(browser_lane["blockers"])


def test_browser_ops_job_logs_requires_job_id_binding() -> None:
    run_id = _run_id("browser-log-no-job")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    browser_summary["sources"]["GFS"]["checks"]["ops_job_logs"]["identity"].pop("job_id")
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    assert summary["status"] == STATUS_BLOCKED
    assert browser_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE" in _codes(browser_lane["blockers"])


def test_browser_ops_jobs_requires_job_id_binding() -> None:
    run_id = _run_id("browser-jobs-no-job")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    browser_summary["sources"]["GFS"]["checks"]["ops_jobs"]["identity"].pop("job_id")
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    assert summary["status"] == STATUS_BLOCKED
    assert browser_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE" in _codes(browser_lane["blockers"])


def test_manual_ops_auth_and_receipt_boundaries() -> None:
    auth_run_id = _run_id("manual-auth")
    auth_config = _seed_pass_bundle(auth_run_id)
    manual_ops = _read(auth_config.run_dir / "manual-ops" / "summary.json")
    manual_ops["production_operator_auth"]["status"] = STATUS_BLOCKED
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


def test_manual_ops_old_booleans_only_shape_blocks() -> None:
    run_id = _run_id("manual-booleans")
    config = _seed_pass_bundle(run_id)
    manual_ops = _read(config.run_dir / "manual-ops" / "summary.json")
    manual_ops.pop("schema", None)
    manual_ops.pop("production_operator_auth", None)
    manual_ops.pop("no_side_effect_proof", None)
    for action in manual_ops["display_actions"]:
        action.pop("response_evidence", None)
    for receipt in manual_ops["control_receipts"]:
        receipt.pop("provenance", None)
    manual_ops["production_operator_auth_evidence"] = True
    _write(config.run_dir / "manual-ops" / "summary.json", manual_ops)

    summary = validate_two_node_e2e_evidence(config)

    manual_lane = summary["lane_summaries"]["manual_ops"]
    assert summary["status"] == STATUS_BLOCKED
    assert manual_lane["status"] == STATUS_BLOCKED
    assert _codes(manual_lane["blockers"]) >= {
        "TWO_NODE_E2E_MANUAL_OPS_SCHEMA_MISSING",
        "TWO_NODE_E2E_MANUAL_OPS_PRODUCTION_AUTH_MISSING",
        "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_EVIDENCE_MISSING",
        "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_MISSING",
    }


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        ("empty_provenance", "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_MISSING"),
        ("missing_source", "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_SOURCE_MISSING"),
        ("wrong_source", "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_SOURCE_MISMATCH"),
        ("missing_producer", "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_PRODUCER_INVALID"),
        ("missing_redaction", "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_UNREDACTED"),
        ("missing_run_binding", "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_RUN_ID_MISSING"),
        ("wrong_run_binding", "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_RUN_ID_MISMATCH"),
        ("artifact_outside_root", "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_OUTSIDE_APPROVED_ROOT"),
        ("artifact_hash_mismatch", "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_HASH_MISMATCH"),
    ],
)
def test_manual_ops_receipt_provenance_must_be_producer_backed(
    mutator: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"manual-provenance-{mutator}")
    config = _seed_pass_bundle(run_id)
    manual_ops = _read(config.run_dir / "manual-ops" / "summary.json")
    receipt = manual_ops["control_receipts"][0]
    provenance = receipt["provenance"]
    if mutator == "empty_provenance":
        receipt["provenance"] = {}
    elif mutator == "missing_source":
        provenance.pop("source", None)
        provenance.pop("source_id", None)
    elif mutator == "wrong_source":
        provenance["source"] = "IFS" if receipt["source"] == "GFS" else "GFS"
    elif mutator == "missing_producer":
        provenance.pop("producer_node", None)
        provenance.pop("producer_role", None)
    elif mutator == "missing_redaction":
        provenance["redacted"] = False
    elif mutator == "missing_run_binding":
        provenance.pop("evidence_run_id", None)
    elif mutator == "wrong_run_binding":
        provenance["evidence_run_id"] = "older-bundle"
    elif mutator == "artifact_outside_root":
        provenance["artifact_path"] = "/tmp/manual-receipt.json"
        provenance["sha256"] = "0" * 64
    elif mutator == "artifact_hash_mismatch":
        provenance["sha256"] = "0" * 64
    _write(config.run_dir / "manual-ops" / "summary.json", manual_ops)

    summary = validate_two_node_e2e_evidence(config)

    manual_lane = summary["lane_summaries"]["manual_ops"]
    assert summary["status"] == STATUS_BLOCKED
    assert manual_lane["status"] == STATUS_BLOCKED
    assert expected_code in _codes(manual_lane["blockers"])


def test_manual_ops_receipt_provenance_with_valid_artifact_passes() -> None:
    run_id = _run_id("manual-provenance-artifact-pass")
    config = _seed_pass_bundle(run_id)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["manual_ops"]["status"] == STATUS_PASS


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        ("older_run", "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_RUN_ID_MISMATCH"),
        ("wrong_source", "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_SOURCE_MISMATCH"),
        ("wrong_action", "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_ACTION_MISMATCH"),
    ],
)
def test_manual_ops_receipt_artifact_payload_must_match_provenance(
    mutator: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"manual-artifact-{mutator}")
    config = _seed_pass_bundle(run_id)
    manual_ops = _read(config.run_dir / "manual-ops" / "summary.json")
    receipt = manual_ops["control_receipts"][0]
    artifact_path = Path(receipt["provenance"]["artifact_path"])
    artifact = _read(artifact_path)
    if mutator == "older_run":
        artifact["evidence_run_id"] = "older-bundle"
    elif mutator == "wrong_source":
        artifact["source"] = "IFS" if receipt["source"] == "GFS" else "GFS"
    else:
        artifact["action"] = "cancel"
    _write(artifact_path, artifact)
    receipt["provenance"]["sha256"] = _sha256_file(artifact_path)
    _write(config.run_dir / "manual-ops" / "summary.json", manual_ops)

    summary = validate_two_node_e2e_evidence(config)

    manual_lane = summary["lane_summaries"]["manual_ops"]
    assert summary["status"] == STATUS_BLOCKED
    assert manual_lane["status"] == STATUS_BLOCKED
    assert expected_code in _codes(manual_lane["blockers"])


def test_manual_ops_response_evidence_valid_fixture_passes() -> None:
    run_id = _run_id("manual-response-valid")
    config = _seed_pass_bundle(run_id)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["manual_ops"]["status"] == STATUS_PASS


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        ("missing", "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_EVIDENCE_MISSING"),
        ("boolean", "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_EVIDENCE_INVALID"),
        ("string", "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_EVIDENCE_INVALID"),
        ("empty", "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_EVIDENCE_INVALID"),
        ("wrong_status", "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_STATUS_INVALID"),
        ("wrong_error_code", "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_ERROR_CODE_INVALID"),
        ("missing_redaction", "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_REDACTION_MISSING"),
        ("wrong_action", "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_BINDING_MISMATCH"),
        ("wrong_source", "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_BINDING_MISMATCH"),
        ("wrong_run_id", "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_RUN_ID_MISMATCH"),
        ("missing_run_id", "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_RUN_ID_MISSING"),
        ("missing_action", "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_BINDING_MISSING"),
        ("missing_source", "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_BINDING_MISSING"),
    ],
)
def test_manual_ops_response_evidence_must_be_structured_producer_evidence(
    mutator: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"manual-response-{mutator}")
    config = _seed_pass_bundle(run_id)
    manual_ops = _read(config.run_dir / "manual-ops" / "summary.json")
    action = manual_ops["display_actions"][0]
    if mutator == "missing":
        action.pop("response_evidence", None)
    elif mutator == "boolean":
        action["response_evidence"] = True
    elif mutator == "string":
        action["response_evidence"] = "CONTROL_PLANE_MANUAL_ACTION_REQUIRED"
    elif mutator == "empty":
        action["response_evidence"] = {}
    elif mutator == "wrong_status":
        action["response_evidence"]["http_status"] = 200
    elif mutator == "wrong_error_code":
        action["response_evidence"]["error_code"] = "OK"
    elif mutator == "missing_redaction":
        action["response_evidence"].pop("body_redacted", None)
    elif mutator == "wrong_action":
        action["response_evidence"]["action"] = "cancel"
    elif mutator == "wrong_source":
        action["source"] = "GFS"
        action["response_evidence"]["source"] = "IFS"
    elif mutator == "wrong_run_id":
        action["response_evidence"]["evidence_run_id"] = "older-bundle"
    elif mutator == "missing_run_id":
        action["response_evidence"].pop("evidence_run_id", None)
    elif mutator == "missing_action":
        action["response_evidence"].pop("action", None)
    elif mutator == "missing_source":
        action["response_evidence"].pop("source", None)
    _write(config.run_dir / "manual-ops" / "summary.json", manual_ops)

    summary = validate_two_node_e2e_evidence(config)

    manual_lane = summary["lane_summaries"]["manual_ops"]
    assert summary["status"] == STATUS_BLOCKED
    assert manual_lane["status"] == STATUS_BLOCKED
    assert expected_code in _codes(manual_lane["blockers"])


def test_manual_ops_action_and_response_source_must_be_declared() -> None:
    run_id = _run_id("manual-response-undeclared-source")
    config = _seed_pass_bundle(run_id)
    manual_ops = _read(config.run_dir / "manual-ops" / "summary.json")
    action = manual_ops["display_actions"][0]
    action["source"] = "NCEP"
    action["response_evidence"]["source"] = "NCEP"
    _write(config.run_dir / "manual-ops" / "summary.json", manual_ops)

    summary = validate_two_node_e2e_evidence(config)

    manual_lane = summary["lane_summaries"]["manual_ops"]
    assert summary["status"] == STATUS_BLOCKED
    assert manual_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_SOURCE_UNDECLARED" in _codes(
        manual_lane["blockers"]
    )


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


def test_manual_ops_full_source_bundle_with_only_gfs_receipt_blocks() -> None:
    run_id = _run_id("manual-missing-source-receipt")
    config = _seed_pass_bundle(run_id)
    manual_ops = _read(config.run_dir / "manual-ops" / "summary.json")
    manual_ops["control_receipts"] = [
        receipt for receipt in manual_ops["control_receipts"] if receipt.get("source") == "GFS"
    ]
    _write(config.run_dir / "manual-ops" / "summary.json", manual_ops)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_BLOCKED
    manual_lane = summary["lane_summaries"]["manual_ops"]
    assert manual_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_MANUAL_OPS_22_RECEIPT_SOURCE_COVERAGE_MISSING" in _codes(manual_lane["blockers"])


def test_manual_ops_ifs_receipt_identity_mismatch_fails() -> None:
    run_id = _run_id("manual-ifs-receipt-mismatch")
    config = _seed_pass_bundle(run_id)
    manual_ops = _read(config.run_dir / "manual-ops" / "summary.json")
    ifs_receipt = next(receipt for receipt in manual_ops["control_receipts"] if receipt.get("source") == "IFS")
    ifs_receipt["model_id"] = "wrong-model"
    _write(config.run_dir / "manual-ops" / "summary.json", manual_ops)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_FAIL
    manual_lane = summary["lane_summaries"]["manual_ops"]
    assert manual_lane["status"] == STATUS_FAIL
    assert "TWO_NODE_E2E_STRICT_IDENTITY_MISMATCH" in _codes(manual_lane["findings"])


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


def test_deep_nested_lane_json_raises_structured_evidence_error() -> None:
    run_id = _run_id("deep-json")
    config = _seed_pass_bundle(run_id)
    nested = _deep_nested_json(320)
    (config.run_dir / "api" / "summary.json").write_text(nested, encoding="utf-8")

    with pytest.raises(TwoNodeE2EEvidenceError) as exc_info:
        validate_two_node_e2e_evidence(config)

    assert exc_info.value.error_code == "TWO_NODE_E2E_EVIDENCE_JSON_TOO_DEEP"


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
        _docker_security_summary_payload(config, run_id),
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
            required_checks=(
                "hydro_met",
                "ops",
                "ops_jobs",
                "ops_job_logs",
                *((() if len(sources) == 1 else ("source_switch",))),
            ),
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
        {
            "status": STATUS_PASS,
            "evidence_run_id": run_id,
            "live_slurm_evidence": True,
            "commands": {"squeue_probe": {"returncode": 0}},
        },
    )
    manual_lane = config.run_dir / "manual-ops"
    receipt_artifacts: dict[str, dict[str, Any]] = {}
    for identity in identities.values():
        source = identity["source"]
        receipt_artifact = manual_lane / f"receipt-{source.lower()}.json"
        receipt_payload = {
            "schema": "nhms.two_node_e2e.manual_ops.receipt.v1",
            "status": STATUS_PASS,
            "evidence_run_id": run_id,
            "producer_node": "22",
            "producer_role": "compute_control",
            "source": source,
            "run_id": identity["run_id"],
            "cycle_time": identity["cycle_time"],
            "model_id": identity["model_id"],
            "action": "retry",
            "receipt_id": f"receipt-{source.lower()}",
            "redacted": True,
        }
        _write(receipt_artifact, receipt_payload)
        receipt_artifacts[source] = _artifact_summary(receipt_artifact)
    _write(
        manual_lane / "summary.json",
        {
            "schema": "nhms.two_node_e2e.manual_ops.v1",
            "status": STATUS_PASS,
            "evidence_run_id": run_id,
            "production_operator_auth": {
                "status": STATUS_PASS,
                "auth_source": "operator_auth_curl header source under /scratch/frd_muziyao/<redacted>",
                "principal": "production-operator",
                "redacted": True,
                "secret_material_written": False,
            },
            "no_side_effect_proof": {
                "node": "27",
                "db_writes": False,
                "gateway_calls": False,
                "control_receipts_created": False,
            },
            "display_actions": [
                {
                    "node": "27",
                    "action": "retry",
                    "source": "GFS",
                    "outcome": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
                    "write_executed": False,
                    "gateway_called": False,
                    "receipt_created": False,
                    "response_evidence": {
                        "http_status": 409,
                        "error_code": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
                        "body_redacted": True,
                        "evidence_run_id": run_id,
                        "action": "retry",
                        "source": "GFS",
                    },
                },
                {
                    "node": "27",
                    "action": "cancel",
                    "source": "GFS",
                    "outcome": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
                    "write_executed": False,
                    "gateway_called": False,
                    "receipt_created": False,
                    "response_evidence": {
                        "http_status": 409,
                        "error_code": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
                        "body_redacted": True,
                        "evidence_run_id": run_id,
                        "action": "cancel",
                        "source": "GFS",
                    },
                }
            ],
            "control_receipts": [
                {
                    "node": "22",
                    "producer_role": "compute_control",
                    "action": "retry",
                    "actual": True,
                    "provenance": {
                        "producer_node": "22",
                        "producer_role": "compute_control",
                        "receipt_id": f"receipt-{identity['source'].lower()}",
                        "source": identity["source"],
                        "evidence_run_id": run_id,
                        "redacted": True,
                        "artifact_path": receipt_artifacts[identity["source"]]["path"],
                        "sha256": receipt_artifacts[identity["source"]]["sha256"],
                    },
                    **copy.deepcopy(identity),
                }
                for identity in identities.values()
            ],
        },
    )
    _write(
        config.run_dir / "22-compute" / "summary.json",
        {
            "status": "ready",
            "evidence_run_id": run_id,
            "live_compute_evidence": True,
            "commands": {"compute_summary_probe": {"returncode": 0}},
        },
    )
    _write(
        config.run_dir / "27-display" / "summary.json",
        {
            "status": "ready",
            "evidence_run_id": run_id,
            "live_display_evidence": True,
            "commands": {"display_summary_probe": {"returncode": 0}},
        },
    )
    _write(
        config.run_dir / "cross-plane" / "summary.json",
        {
            "status": STATUS_PASS,
            "evidence_run_id": run_id,
            "live_cross_plane_evidence": True,
            "commands": {"cross_plane_identity_probe": {"returncode": 0}},
            "sources": {
                source: {"status": STATUS_PASS, "identity": copy.deepcopy(identity)}
                for source, identity in identities.items()
            },
        },
    )
    return config


def _write_readonly_db_lane(config: TwoNodeE2EEvidenceConfig, identities: dict[str, dict[str, str]]) -> None:
    permission_targets = (
        ("hydro.hydro_run", "hydro_run_terminal_state"),
        ("hydro.river_timeseries", "hydro_display_timeseries"),
        ("met.forecast_cycle", "met_cycle_state"),
        ("met.forcing_station_timeseries", "met_station_timeseries"),
        ("ops.pipeline_job", "pipeline_job_state"),
        ("ops.pipeline_event", "pipeline_event_audit"),
        ("reachable_roles", "reachable_role_membership"),
        ("audited_schema_sequences", "audited_schema_sequence_catalog"),
        ("current_database", "current_database_create_catalog"),
        ("hydro.*", "schema_table_ddl"),
        ("met.*", "schema_table_ddl"),
        ("ops.*", "schema_table_ddl"),
    )
    permission_probes = []
    for target, surface in permission_targets:
        operations: list[dict[str, Any]]
        probe: dict[str, Any] = {
            "target": target,
            "surface": surface,
            "status": STATUS_PASS,
        }
        if target in {
            "hydro.hydro_run",
            "hydro.river_timeseries",
            "met.forecast_cycle",
            "met.forcing_station_timeseries",
            "ops.pipeline_job",
            "ops.pipeline_event",
        }:
            operations = [
                {
                    "operation": operation,
                    "status": STATUS_PASS,
                    "privilege_allowed": False,
                    "execution_outcome": "permission_denied",
                }
                for operation in ("INSERT", "UPDATE", "DELETE")
            ]
            probe.update(
                {
                    "table_privileges": {
                        "insert": False,
                        "update": False,
                        "delete": False,
                        "truncate": False,
                        "references": False,
                        "trigger": False,
                        "maintain": False,
                    },
                    "column_privileges": {"insert": [], "update": []},
                    "sequence_privileges": [],
                }
            )
        elif target == "reachable_roles":
            operations = []
            probe["reachable_role_findings"] = []
        elif target == "audited_schema_sequences":
            operations = [
                {
                    "operation": "AUDITED_SCHEMA_SEQUENCE_USAGE_UPDATE",
                    "status": STATUS_PASS,
                    "privilege_allowed": False,
                    "execution_outcome": "catalog_checked_no_audited_schema_sequence_mutating_privilege",
                }
            ]
            probe["sequence_privileges"] = []
        elif target == "current_database":
            operations = [
                {
                    "operation": "DATABASE_CREATE",
                    "status": STATUS_PASS,
                    "privilege_allowed": False,
                    "database_privilege_allowed": False,
                    "execution_outcome": "catalog_checked_no_database_create_privilege",
                }
            ]
            probe["database_privileges"] = {"database_name": "nhms", "create": False}
        else:
            operations = [
                {
                    "operation": "DDL_CREATE_TABLE",
                    "status": STATUS_PASS,
                    "privilege_allowed": False,
                    "schema_privilege_allowed": False,
                    "execution_outcome": "permission_denied",
                }
            ]
            probe["schema_privileges"] = {"create": False}
        probe["operations"] = operations
        permission_probes.append(probe)
    merged_route_smoke = [
        {"name": "health", "status": STATUS_PASS, "path": "/health"},
        {"name": "runtime_config", "status": STATUS_PASS, "path": "/api/v1/runtime/config"},
        {"name": "models", "status": STATUS_PASS, "path": "/api/v1/models?active=all&limit=1"},
    ]
    source_artifacts = []
    source_summaries = []
    role = {
        "current_user": "display_ro",
        "role_type": "readonly_candidate",
    }
    for source_index, identity in enumerate(identities.values()):
        source = identity["source"]
        strict_identity = copy.deepcopy(identity)
        strict_query = (
            f"source={strict_identity['source']}&cycle_time={strict_identity['cycle_time']}"
            f"&run_id={strict_identity['run_id']}&model_id={strict_identity['model_id']}"
        )
        route_identity = {"response_identity": copy.deepcopy(strict_identity)}
        source_route_smoke = [
            {"name": "health", "status": STATUS_PASS, "path": "/health"},
            {"name": "runtime_config", "status": STATUS_PASS, "path": "/api/v1/runtime/config"},
            {"name": "models", "status": STATUS_PASS, "path": "/api/v1/models?active=all&limit=1"},
            {
                "name": "stations",
                "source": strict_identity["source"],
                "status": STATUS_PASS,
                "path": f"/api/v1/met/stations?model_id={strict_identity['model_id']}&limit=1",
            },
            {
                "name": "latest_product",
                "source": strict_identity["source"],
                "status": STATUS_PASS,
                "path": f"/api/v1/mvp/qhh/latest-product?{strict_query}",
                "strict_identity": strict_identity,
                **copy.deepcopy(route_identity),
            },
            {
                "name": "pipeline_status",
                "source": strict_identity["source"],
                "status": STATUS_PASS,
                "path": f"/api/v1/pipeline/status?{strict_query}",
                "strict_identity": strict_identity,
                **copy.deepcopy(route_identity),
            },
            {
                "name": "pipeline_stages",
                "source": strict_identity["source"],
                "status": STATUS_PASS,
                "path": f"/api/v1/pipeline/stages?{strict_query}",
                "strict_identity": strict_identity,
                **copy.deepcopy(route_identity),
            },
            {
                "name": "jobs",
                "source": strict_identity["source"],
                "status": STATUS_PASS,
                "path": f"/api/v1/jobs?{strict_query}&limit=1",
                "strict_identity": strict_identity,
                **copy.deepcopy(route_identity),
            },
            {
                "name": "job_logs",
                "source": strict_identity["source"],
                "status": STATUS_PASS,
                "path": (
                    f"/api/v1/jobs/{strict_identity['job_id']}/logs?{strict_query}"
                    f"&job_id={strict_identity['job_id']}"
                ),
                "strict_identity": strict_identity,
                **copy.deepcopy(route_identity),
            },
        ]
        for route in source_route_smoke:
            if route not in merged_route_smoke:
                merged_route_smoke.append(route)
        source_lane = config.evidence_root / f"{config.run_id}-{source.lower()}" / "db" / "readonly-db-boundary"
        source_summary = {
            "schema": READONLY_DB_LIVE_SCHEMA,
            "status": STATUS_PASS,
            "run_id": f"{config.run_id}-{source.lower()}",
            "database_url": "postgresql://db.example:5432/nhms",
            "validation_provenance": {
                "mode": "live",
                "live_readonly_proof": True,
            },
            "role": role,
            "display_identity": copy.deepcopy(strict_identity),
            "route_smoke": source_route_smoke,
            "manual_action_probes": _readonly_manual_actions(),
            "permission_probes": permission_probes,
        }
        _write(source_lane / "role.json", role)
        _write(source_lane / "route_smoke.json", source_route_smoke)
        _write(source_lane / "permission_probes.json", permission_probes)
        _write(source_lane / "summary.json", source_summary)
        source_artifacts.append(
            {
                "source_index": source_index,
                "sources": [source],
                "source_dir": str(source_lane.resolve(strict=False)),
                "summary_run_id": source_summary["run_id"],
                "parent_binding": "run_id_prefix",
                "validation_provenance": {
                    "mode": "live",
                    "live_readonly_proof": True,
                },
                "artifacts": {
                    filename: _readonly_source_artifact(source_lane / filename, source_summary["run_id"])
                    for filename in ("summary.json", "role.json", "route_smoke.json", "permission_probes.json")
                },
            }
        )
        source_summaries.append(source_summary)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    _write(lane / "role.json", role)
    _write(lane / "route_smoke.json", merged_route_smoke)
    _write(lane / "permission_probes.json", permission_probes)
    _write(
        lane / "summary.json",
        {
            "schema": READONLY_DB_LIVE_SCHEMA,
            "status": STATUS_PASS,
            "run_id": config.run_id,
            "database_url": "postgresql://db.example:5432/nhms",
            "validation_provenance": {
                "mode": "live",
                "live_readonly_proof": True,
                "merged_source_evidence": True,
                "source_bundle_count": len(source_summaries),
                "source_artifacts": source_artifacts,
            },
            "role": role,
            "display_identity": copy.deepcopy(identities),
            "route_smoke": merged_route_smoke,
            "manual_action_probes": _readonly_manual_actions(),
            "permission_probes": permission_probes,
        },
    )


def _readonly_manual_actions(
    *,
    include_action: bool = True,
    run_id: str = "read-only-fixture",
) -> list[dict[str, Any]]:
    actions = [
        {
            "name": "display_retry_manual_action",
            "action": "retry",
            "method": "POST",
            "path": f"/api/v1/runs/{run_id}/retry",
            "status": STATUS_PASS,
            "http_status": 409,
            "expected_error_code": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
            "observed_error_code": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
            "write_dependency_constructed": False,
            "write_executed": False,
        },
        {
            "name": "display_cancel_manual_action",
            "action": "cancel",
            "method": "POST",
            "path": f"/api/v1/runs/{run_id}/cancel",
            "status": STATUS_PASS,
            "http_status": 409,
            "expected_error_code": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
            "observed_error_code": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
            "write_dependency_constructed": False,
            "write_executed": False,
        },
    ]
    if not include_action:
        for action in actions:
            action.pop("action", None)
    return actions


def _route_without_embedded_identity(route: dict[str, Any]) -> dict[str, Any]:
    producer_route = dict(route)
    producer_route.pop("strict_identity", None)
    producer_route.pop("identity", None)
    return producer_route


def _readonly_source_artifact(path: Path, source_run_id: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "path": str(path.resolve(strict=False)),
        "sha256": _sha256_file(path),
        "run_id": _artifact_run_id(payload) or source_run_id,
    }


def _artifact_run_id(value: Any) -> str | None:
    if isinstance(value, dict):
        raw = value.get("run_id") or value.get("evidence_run_id") or value.get("bundle_run_id")
        if raw is not None and str(raw).strip():
            return str(raw)
    if isinstance(value, list):
        for item in value:
            raw = _artifact_run_id(item)
            if raw:
                return raw
    return None


def _docker_security_summary_payload(config: TwoNodeE2EEvidenceConfig, run_id: str) -> dict[str, Any]:
    lane = config.run_dir / "docker-security"
    source_trust = lane / "two-node-docker-source-trust.json"
    static_report = lane / "static-compose-env-check.json"
    smoke_report = lane / "docker-smoke.json"
    _write(
        source_trust,
        {
            "schema": "nhms.two_node_docker.source_trust.v1",
            "status": STATUS_PASS,
            "evidence_run_id": run_id,
            "roles": ["compute"],
            "checked_paths": [
                record
                for record in _source_trust_checked_paths(lane)
                if record["label"] != "display role env"
            ],
            "blockers": [],
        },
    )
    source_trust_display = lane / "two-node-docker-source-trust-display.json"
    _write(
        source_trust_display,
        {
            "schema": "nhms.two_node_docker.source_trust.v1",
            "status": STATUS_PASS,
            "evidence_run_id": run_id,
            "roles": ["display"],
            "checked_paths": _source_trust_checked_paths(lane),
            "blockers": [],
        },
    )
    _write(
        static_report,
        {
            "schema_version": "nhms.two_node_docker.static_check.v1",
            "status": STATUS_PASS,
            "evidence_run_id": run_id,
            "findings": [],
            "slurm_routes_enabled": False,
            "slurm_route_available": False,
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
            "docker_socket_present": False,
            "broad_host_bind_present": False,
            "private_workspace_bind_present": False,
            "workspace_mount_present": False,
            "writable_published_artifact_mount": False,
            "display_write_capability_present": False,
            "published_artifacts_readonly": True,
            "root_filesystem_readonly": True,
            "cap_drop_all": True,
        },
    )
    _write(
        smoke_report,
        {
            "schema_version": "nhms.two_node_docker.app_smoke.v1",
            "status": STATUS_PASS,
            "evidence_run_id": run_id,
            "image_tag": "nhms-app:test",
            "dockerfile": "infra/docker/Dockerfile.app",
            "commands": {
                "image_absence_probe": {"returncode": 0},
                "display_startup_start": {"returncode": 0},
                "display_startup_probe": {"returncode": 0},
            },
        },
    )
    return {
        "schema_version": "nhms.two_node_docker.security_summary.v1",
        "status": STATUS_PASS,
        "evidence_run_id": run_id,
        "live_docker_evidence": True,
        "runtime_config": {
            "service_role": "display_readonly",
            "display_readonly": True,
            "slurm_routes_enabled": False,
        },
        "source_artifacts": {
            "source_trust": [_artifact_summary(source_trust), _artifact_summary(source_trust_display)],
            "static": _artifact_summary(static_report),
            "smoke": _artifact_summary(smoke_report),
        },
        "source_statuses": {"source_trust": STATUS_PASS, "static": STATUS_PASS, "smoke": STATUS_PASS},
        "slurm_routes_unavailable": True,
        "slurm_routes_enabled": False,
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
    }


def _artifact_summary(path: Path) -> dict[str, Any]:
    return {"path": str(path.resolve(strict=False)), "sha256": _sha256_file(path)}


def _source_trust_checked_paths(root: Path) -> list[dict[str, Any]]:
    labels = {
        "trust path component": "directory",
        "checkout root": "directory",
        "infra directory": "directory",
        "compute compose source": "file",
        "display compose source": "file",
        "env source directory": "directory",
        "systemd source directory": "directory",
        "compute systemd unit source": "file",
        "display systemd unit source": "file",
        "compute role env": "file",
        "display role env": "file",
    }
    records = []
    for label, expected_kind in labels.items():
        is_directory = expected_kind == "directory"
        records.append(
            {
                "label": label,
                "path": str(root / label.replace(" ", "-")),
                "expected_kind": expected_kind,
                "exists": True,
                "trusted_owner": True,
                "is_symlink": False,
                "is_directory": is_directory,
                "is_regular": not is_directory,
                "group_writable": False,
                "world_writable": False,
                "mode": "0600" if label.endswith("role env") else ("0755" if is_directory else "0644"),
            }
        )
    return records


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
                    check: {
                        "status": STATUS_PASS,
                        "identity": copy.deepcopy(identity),
                        "evidence": {
                            "request": {
                                "method": "GET",
                                "path": f"/producer/{source.lower()}/{check}",
                            },
                            "response": {"status_code": 200},
                        },
                    }
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


def _deep_nested_json(depth: int) -> str:
    return "{" + '"x":{' * depth + '"status":"PASS"' + "}" * depth + "}"
