from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

import pytest

import services.production_closure.two_node_e2e_evidence as two_node_e2e_evidence
from scripts import validate_two_node_docker_runtime as docker_runtime
from services.artifacts.reader import published_log_uri
from services.production_closure import (
    two_node_e2e_api_lane,
    two_node_e2e_browser_lane,
    two_node_e2e_docker_preflight,
    two_node_e2e_docker_security,
    two_node_e2e_logs_lane,
    two_node_e2e_metadata_lane,
    two_node_e2e_readonly_db_lane,
    two_node_e2e_simple_live_lane,
)
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
NON_AUTHORITATIVE_WRAPPER_KEYS = (
    "metadata",
    "wrapper",
    "collector",
    "context",
    "diagnostics",
    "debug",
    "extra",
    "notes",
)
COMPACT_CYCLE_TIME = "2026052900"
ISO_OFFSET_CYCLE_TIME = "2026-05-29T00:00:00+00:00"
SHIFTED_CYCLE_TIME = "2026-05-29T01:00:00Z"


def _module_symbol_exists(module: Any, dotted_symbol: str) -> bool:
    current = module
    for part in dotted_symbol.split("."):
        if not hasattr(current, part):
            return False
        current = getattr(current, part)
    return True


def _expected_shared_contracts() -> dict[str, dict[str, tuple[str, ...] | str]]:
    final_lanes = tuple(two_node_e2e_evidence.FINAL_REQUIRED_LANES)
    producer_verification = two_node_e2e_evidence.TWO_NODE_E2E_SHARED_CONTRACT_VERIFICATION_PRODUCER
    metadata_verification = two_node_e2e_evidence.TWO_NODE_E2E_SHARED_CONTRACT_VERIFICATION_METADATA
    safety_verification = two_node_e2e_evidence.TWO_NODE_E2E_SHARED_CONTRACT_VERIFICATION_SAFETY
    return {
        "lane-result-adapter": {
            "consumers": final_lanes,
            "guard_symbols": (
                "LaneEvaluation",
                "LaneEvaluation.to_summary",
                "validate_two_node_e2e_evidence",
                "FINAL_REQUIRED_LANES",
                "STATUS_PASS",
                "STATUS_PARTIAL",
                "STATUS_FAIL",
                "STATUS_BLOCKED",
            ),
            "namespaces": ("TWO_NODE_E2E_LANE_", "TWO_NODE_E2E_SOURCE_", "TWO_NODE_E2E_EVIDENCE_"),
            "verification": metadata_verification,
        },
        "current-run-binding": {
            "consumers": final_lanes,
            "guard_symbols": (
                "CURRENT_EVIDENCE_RUN_ID_KEYS",
                "_current_run_blockers",
                "_recursive_current_run_blockers",
                "_explicit_bundle_run_ids",
                "_explicit_bundle_run_ids_from_value",
            ),
            "namespaces": (
                "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_",
                "TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH",
                "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
            ),
            "verification": producer_verification,
        },
        "producer-source-artifacts": {
            "consumers": (
                "docker_preflight",
                "docker_security",
                "readonly_db",
                "api",
                "browser",
                "logs",
                "cross_plane",
                "manual_ops",
                "slurm",
                "compute_summary",
                "display_summary",
            ),
            "guard_symbols": (
                "PRODUCER_EVIDENCE_KEYS",
                "SOURCE_SCOPED_PRODUCER_EVIDENCE_KEYS",
                "PRODUCER_AUTHORITATIVE_PROOF_CONTAINER_KEYS",
                "PRODUCER_NON_AUTHORITATIVE_PROOF_CONTAINER_KEYS",
                "_has_producer_backed_lane_evidence",
                "_source_lane_check_producer_blockers",
                "_source_scoped_producer_evidence_blockers",
                "_producer_source_artifact_blockers",
                "_producer_source_artifact_record_blockers",
            ),
            "namespaces": (
                "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_",
                "CHECK_PRODUCER_EVIDENCE_MISSING",
                "CHECK_PRODUCER_IDENTITY_",
            ),
            "verification": producer_verification,
        },
        "strict-identity": {
            "consumers": ("metadata", "readonly_db", "api", "browser", "logs", "cross_plane", "manual_ops"),
            "guard_symbols": (
                "two_node_e2e_metadata_lane.STRICT_IDENTITY_FIELDS",
                "two_node_e2e_metadata_lane.STRICT_LOG_IDENTITY_FIELDS",
                "LOG_URI_IDENTITY_FIELDS",
                "two_node_e2e_metadata_lane.resolve_strict_identities",
                "two_node_e2e_metadata_lane.strict_identity_metadata_issues",
                "_strict_identity_value_matches",
                "_record_identity",
            ),
            "namespaces": (
                "TWO_NODE_E2E_STRICT_IDENTITY_",
                "TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE",
                "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE",
            ),
            "verification": metadata_verification,
        },
        "approved-root-path-safety": {
            "consumers": final_lanes,
            "guard_symbols": (
                "APPROVED_EVIDENCE_ROOTS",
                "EvidenceWriter",
                "_safe_resolved_evidence_root",
                "_read_json",
                "_read_json_bytes",
                "_refuse_symlink_components",
                "_recorded_path_approval_blockers",
                "_producer_source_artifact_record_blockers",
            ),
            "namespaces": (
                "TWO_NODE_E2E_EVIDENCE_ROOT_UNAPPROVED",
                "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE",
                "TWO_NODE_E2E_RECORDED_PATH_OUTSIDE_APPROVED_ROOTS",
                "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_OUTSIDE_APPROVED_ROOT",
            ),
            "verification": safety_verification,
        },
        "redaction": {
            "consumers": final_lanes,
            "guard_symbols": (
                "LaneEvaluation.to_summary",
                "EvidenceWriter.write_json",
                "redact_payload",
                "redact_text",
                "_blocker",
                "_finding",
            ),
            "namespaces": (
                "TWO_NODE_E2E_EVIDENCE_REDACTION_DEPTH_EXCEEDED",
                "TWO_NODE_E2E_EVIDENCE_PAYLOAD_TOO_LARGE",
            ),
            "verification": safety_verification,
        },
        "log-uri-safety": {
            "consumers": ("logs", "browser"),
            "guard_symbols": (
                "LOG_URI_KEYS",
                "LOG_URI_REQUIRED_IDENTITY_FIELDS",
                "PUBLISHED_LOG_ROOT_KEYS",
                "PUBLISHED_LOG_S3_BUCKET_KEYS",
                "_published_log_uri_blockers",
                "_published_log_uri_identity_blockers",
                "_safe_log_relative_path_blockers",
                "_safe_log_absolute_path_blockers",
                "_safe_log_uri_summary",
                "_unsafe_log_uri_summary",
            ),
            "namespaces": (
                "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_",
                "TWO_NODE_E2E_LOGS_PRIVATE_LOG_URI",
                "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH",
            ),
            "verification": safety_verification,
        },
    }


def _inventory_row(inventory_text: str, contract_id: str) -> str:
    prefix = f"| `{contract_id}` |"
    for line in inventory_text.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"missing inventory row for {contract_id}")


def _simple_live_alias_cases() -> list[tuple[str, str]]:
    return [
        (lane_config.name, candidate)
        for lane_config in two_node_e2e_simple_live_lane.SIMPLE_LIVE_LANE_CONFIGS.values()
        for candidate in lane_config.document_candidates
    ]


def _simple_live_pass_alias_cases() -> list[tuple[str, str]]:
    return [
        (lane_config.name, pass_alias)
        for lane_config in two_node_e2e_simple_live_lane.SIMPLE_LIVE_LANE_CONFIGS.values()
        for pass_alias in lane_config.pass_aliases
    ]


def test_shared_two_node_evidence_contract_metadata_covers_producer_source_artifact_strict_identity() -> None:
    expected_contracts = _expected_shared_contracts()

    assert set(two_node_e2e_evidence.TWO_NODE_E2E_SHARED_CONTRACTS) == set(expected_contracts)
    for contract_id, expected in expected_contracts.items():
        metadata = two_node_e2e_evidence.TWO_NODE_E2E_SHARED_CONTRACTS[contract_id]
        assert metadata["owner"] == "services.production_closure.two_node_e2e_evidence"
        assert tuple(metadata["consumers"]) == expected["consumers"]
        assert tuple(metadata["guard_symbols"]) == expected["guard_symbols"]
        assert tuple(metadata["namespaces"]) == expected["namespaces"]
        assert metadata["verification"] == expected["verification"]
        for symbol in metadata["guard_symbols"]:
            assert _module_symbol_exists(two_node_e2e_evidence, str(symbol)), (contract_id, symbol)


def test_shared_two_node_evidence_contract_inventory_covers_metadata_source_scope() -> None:
    inventory_text = (
        REPO_ROOT / "docs" / "governance" / "TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md"
    ).read_text(encoding="utf-8")

    for contract_id, metadata in two_node_e2e_evidence.TWO_NODE_E2E_SHARED_CONTRACTS.items():
        row = _inventory_row(inventory_text, contract_id)
        assert str(metadata["owner"]) in inventory_text
        assert str(metadata["owner"]) in row
        assert str(metadata["verification"]) in row
        for consumer in metadata["consumers"]:
            assert f"`{consumer}`" in row
        for symbol in metadata["guard_symbols"]:
            assert f"`{symbol}`" in row
        for namespace in metadata["namespaces"]:
            assert f"`{namespace}`" in row


def test_metadata_lane_owner_module_covers_metadata_source_scope_contract() -> None:
    assert two_node_e2e_metadata_lane.METADATA_LANE_OWNER == (
        "services.production_closure.two_node_e2e_metadata_lane"
    )
    assert two_node_e2e_metadata_lane.METADATA_LANE_VERIFICATION == (
        'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "metadata or strict_identity or source_scope"'
    )
    assert two_node_e2e_metadata_lane.METADATA_DOCUMENT_CANDIDATES == (
        "run.json",
        "identity.json",
        "metadata.json",
        "cross-plane/run.json",
        "cross-plane/identity.json",
    )
    for symbol in two_node_e2e_metadata_lane.METADATA_LANE_GUARD_SYMBOLS:
        assert _module_symbol_exists(two_node_e2e_metadata_lane, symbol), symbol


def test_metadata_lane_owner_evaluates_scope_identity_and_downstream_seed() -> None:
    run_id = _run_id("metadata-owner")
    config = _seed_pass_bundle(run_id, sources=("GFS",), reduced_scope=True)
    metadata_doc = two_node_e2e_evidence._find_first_json(
        config.run_dir,
        two_node_e2e_metadata_lane.METADATA_DOCUMENT_CANDIDATES,
    )
    assert metadata_doc is not None

    result = two_node_e2e_metadata_lane.evaluate_metadata_lane(
        metadata_doc,
        metadata_doc.payload,
        evidence_run_id=run_id,
        configured_declared_sources=config.declared_sources,
        configured_reduced_scope=config.reduced_scope,
        helpers=two_node_e2e_evidence._metadata_lane_helpers(),
    )

    assert result.lane.status == STATUS_PASS
    assert result.scope.declared_sources == ("GFS",)
    assert result.scope.reduced_scope is True
    assert result.scope.reduced_scope_declared is True
    assert set(result.strict_identities) == {"GFS"}
    for field in two_node_e2e_metadata_lane.STRICT_LOG_IDENTITY_FIELDS:
        assert result.strict_identities["GFS"][field]

    summary = validate_two_node_e2e_evidence(config)
    assert summary["strict_identity"]["declared_sources"] == ["GFS"]
    assert summary["strict_identity"]["reduced_scope"] is True
    assert summary["strict_identity"]["sources"]["GFS"] == result.strict_identities["GFS"]
    assert summary["source_scope_results"]["GFS"]["identity"] == result.strict_identities["GFS"]


@pytest.mark.parametrize("candidate", two_node_e2e_metadata_lane.METADATA_DOCUMENT_CANDIDATES)
def test_metadata_lane_owner_discovers_each_metadata_alias(candidate: str) -> None:
    run_id = _run_id(f"metadata-alias-{candidate.replace('/', '-')}")
    config = _seed_pass_bundle(run_id)
    canonical = config.run_dir / "run.json"
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    canonical.unlink()
    _write(config.run_dir / candidate, payload)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["lane_summaries"]["metadata"]["status"] == STATUS_PASS
    assert summary["metadata"]["evidence_path"] == str((config.run_dir / candidate).resolve().relative_to(REPO_ROOT))
    assert summary["strict_identity"]["sources"]["GFS"]["job_id"]


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

    preflight_path = config.run_dir / "docker-preflight" / "summary.json"
    docker_preflight = summary["lane_summaries"]["docker_preflight"]
    assert summary["status"] == STATUS_PASS
    assert docker_preflight["status"] == STATUS_PASS
    assert docker_preflight["evidence_path"] == str(preflight_path.resolve().relative_to(REPO_ROOT))
    assert docker_preflight["evidence_sha256"] == _sha256_file(preflight_path)
    assert docker_preflight["summary_status"] == STATUS_PASS
    assert docker_preflight["blockers"] == []
    assert docker_preflight["findings"] == []
    redacted = docker_preflight["redacted_evidence"]
    assert redacted["schema_version"] == "nhms.two_node_docker.preflight.v1"
    assert set(redacted["commands"]) == {
        "docker_version",
        "docker_compose_version",
        "docker_info_docker_root",
        "docker_system_df",
        "df_h",
    }


def test_docker_preflight_owner_module_covers_resource_command_path_contract() -> None:
    assert two_node_e2e_docker_preflight.DOCKER_PREFLIGHT_LANE_OWNER == (
        "services.production_closure.two_node_e2e_docker_preflight"
    )
    assert two_node_e2e_docker_preflight.DOCKER_PREFLIGHT_LANE_VERIFICATION == (
        'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "docker_preflight"'
    )
    assert two_node_e2e_docker_preflight.DOCKER_PREFLIGHT_DOCUMENT_CANDIDATES == (
        "docker-preflight/summary.json",
        "docker-preflight/docker-preflight.json",
        "docker-preflight.json",
    )
    assert two_node_e2e_docker_preflight.DOCKER_PREFLIGHT_REQUIRED_COMMANDS == (
        "docker_version",
        "docker_compose_version",
        "docker_info_docker_root",
        "docker_system_df",
        "df_h",
    )
    assert two_node_e2e_docker_preflight.DOCKER_PREFLIGHT_REQUIRED_DISK_LABELS == (
        "evidence_root",
        "tmpdir",
        "docker_root",
    )
    assert two_node_e2e_docker_preflight.DOCKER_PREFLIGHT_BLOCKER_NAMESPACES == (
        "TWO_NODE_E2E_DOCKER_PREFLIGHT_",
        "TWO_NODE_E2E_DOCKER_ROOT_",
        "TWO_NODE_E2E_RECORDED_PATH_OUTSIDE_APPROVED_ROOTS",
        "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_",
        "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
    )
    for symbol in two_node_e2e_docker_preflight.DOCKER_PREFLIGHT_LANE_GUARD_SYMBOLS:
        assert _module_symbol_exists(two_node_e2e_docker_preflight, symbol), symbol


@pytest.mark.parametrize("candidate", two_node_e2e_docker_preflight.DOCKER_PREFLIGHT_DOCUMENT_CANDIDATES)
def test_docker_preflight_owner_discovers_each_preflight_alias(candidate: str) -> None:
    run_id = _run_id(f"preflight-alias-{candidate.replace('/', '-')}")
    config = _seed_pass_bundle(run_id)
    canonical = config.run_dir / "docker-preflight" / "summary.json"
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    canonical.unlink()
    _write(config.run_dir / candidate, payload)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["lane_summaries"]["docker_preflight"]["status"] == STATUS_PASS
    assert summary["lane_summaries"]["docker_preflight"]["evidence_path"] == str(
        (config.run_dir / candidate).resolve().relative_to(REPO_ROOT)
    )


def test_docker_security_owner_module_covers_child_artifact_display_contract() -> None:
    assert two_node_e2e_docker_security.DOCKER_SECURITY_LANE_OWNER == (
        "services.production_closure.two_node_e2e_docker_security"
    )
    assert two_node_e2e_docker_security.DOCKER_SECURITY_LANE_VERIFICATION == (
        'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "docker_security or docker_display"'
    )
    assert two_node_e2e_docker_security.DOCKER_SECURITY_DOCUMENT_CANDIDATES == (
        "docker-security/summary.json",
        "docker-security/display-isolation.json",
        "docker-security/docker-smoke.json",
        "docker-smoke/docker-smoke.json",
        "docker-smoke.json",
    )
    assert two_node_e2e_docker_security.DOCKER_SECURITY_CHILD_SCHEMAS == {
        "source_trust": "nhms.two_node_docker.source_trust.v1",
        "static": "nhms.two_node_docker.static_check.v1",
        "smoke": "nhms.two_node_docker.app_smoke.v1",
    }
    assert tuple(two_node_e2e_docker_security.DOCKER_REQUIRED_FALSE_PROOFS) == (
        "slurm_routes_enabled",
        "slurm_route_available",
        "slurm_cli_present",
        "slurm_config_present",
        "slurm_socket_present",
        "munge_path_present",
        "docker_socket_present",
        "privileged",
        "host_network",
        "host_pid",
        "host_ipc",
        "cap_add_present",
        "forbidden_hostconfig_hazard",
        "forbidden_mount_hazard",
        "forbidden_env_hazard",
        "broad_host_bind_present",
        "private_workspace_bind_present",
        "workspace_mount_present",
        "writable_published_artifact_mount",
        "display_write_capability_present",
    )
    assert tuple(two_node_e2e_docker_security.DOCKER_REQUIRED_TRUE_PROOFS) == (
        "published_artifacts_readonly",
        "root_filesystem_readonly",
        "cap_drop_all",
    )
    assert two_node_e2e_docker_security.DOCKER_SECURITY_BLOCKER_NAMESPACES == (
        "TWO_NODE_E2E_DOCKER_SECURITY_",
        "TWO_NODE_E2E_DOCKER_LIVE_CONTAINER_EVIDENCE_MISSING",
        "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_",
        "TWO_NODE_E2E_DOCKER_STATIC_",
        "TWO_NODE_E2E_DOCKER_SMOKE_",
        "TWO_NODE_E2E_DOCKER_DISPLAY_",
        "TWO_NODE_E2E_DISPLAY_",
        "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_",
        "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
    )
    for symbol in two_node_e2e_docker_security.DOCKER_SECURITY_LANE_GUARD_SYMBOLS:
        assert _module_symbol_exists(two_node_e2e_docker_security, symbol), symbol


@pytest.mark.parametrize("candidate", two_node_e2e_docker_security.DOCKER_SECURITY_DOCUMENT_CANDIDATES)
def test_docker_security_owner_discovers_each_security_alias(candidate: str) -> None:
    run_id = _run_id(f"security-alias-{candidate.replace('/', '-')}")
    config = _seed_pass_bundle(run_id)
    canonical = config.run_dir / "docker-security" / "summary.json"
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    canonical.unlink()
    candidate_index = two_node_e2e_docker_security.DOCKER_SECURITY_DOCUMENT_CANDIDATES.index(candidate)
    for earlier_candidate in two_node_e2e_docker_security.DOCKER_SECURITY_DOCUMENT_CANDIDATES[:candidate_index]:
        earlier_path = config.run_dir / earlier_candidate
        if earlier_path.exists():
            _move_conflicting_docker_security_child_artifact(payload, earlier_path)
            earlier_path.unlink()
    candidate_path = config.run_dir / candidate
    if candidate_path.exists():
        _move_conflicting_docker_security_child_artifact(payload, candidate_path)
        candidate_path.unlink()
    _write(candidate_path, payload)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["lane_summaries"]["docker_security"]["status"] == STATUS_PASS
    assert summary["lane_summaries"]["docker_security"]["evidence_path"] == str(
        candidate_path.resolve().relative_to(REPO_ROOT)
    )


def test_docker_security_root_alias_rejects_sibling_run_child_artifact() -> None:
    run_id = _run_id("security-root-alias-sibling-child")
    config = _seed_pass_bundle(run_id)
    canonical = config.run_dir / "docker-security" / "summary.json"
    payload = _read(canonical)
    canonical.unlink()
    for earlier_candidate in two_node_e2e_docker_security.DOCKER_SECURITY_DOCUMENT_CANDIDATES[:-1]:
        earlier_path = config.run_dir / earlier_candidate
        if earlier_path.exists():
            _move_conflicting_docker_security_child_artifact(payload, earlier_path)
            earlier_path.unlink()
    static_artifact = payload["source_artifacts"]["static"]
    sibling_static = config.evidence_root / f"{run_id}-sibling" / "docker-security" / "static-compose-env-check.json"
    _write(sibling_static, _read(Path(static_artifact["path"])))
    static_artifact["path"] = str(sibling_static)
    static_artifact["sha256"] = _sha256_file(sibling_static)
    _write(config.run_dir / "docker-smoke.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    docker_lane = summary["lane_summaries"]["docker_security"]
    assert docker_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_STALE_OR_UNSCOPED" in _codes(
        docker_lane["blockers"]
    )


def test_docker_security_emitted_blockers_are_covered_by_owner_namespaces() -> None:
    namespaces = two_node_e2e_docker_security.DOCKER_SECURITY_BLOCKER_NAMESPACES
    observed_codes: set[str] = set()

    live_run_id = _run_id("docker-security-live-namespace")
    live_config = _seed_pass_bundle(live_run_id)
    live_payload = _read(live_config.run_dir / "docker-security" / "summary.json")
    live_payload.pop("live_docker_evidence")
    _write(live_config.run_dir / "docker-security" / "summary.json", live_payload)
    observed_codes.update(_codes(validate_two_node_e2e_evidence(live_config)["lane_summaries"]["docker_security"]["blockers"]))

    current_run_id = _run_id("docker-security-current-namespace")
    current_config = _seed_pass_bundle(current_run_id)
    current_payload = _read(current_config.run_dir / "docker-security" / "summary.json")
    current_payload["evidence_run_id"] = "older-docker-security"
    _write(current_config.run_dir / "docker-security" / "summary.json", current_payload)
    observed_codes.update(
        _codes(validate_two_node_e2e_evidence(current_config)["lane_summaries"]["docker_security"]["blockers"])
    )

    stale_run_id = _run_id("docker-security-stale-namespace")
    stale_config = _seed_pass_bundle(stale_run_id)
    stale_payload = _read(stale_config.run_dir / "docker-security" / "summary.json")
    stale_payload["expected_evidence_run_id"] = "newer-docker-security"
    _write(stale_config.run_dir / "docker-security" / "summary.json", stale_payload)
    observed_codes.update(_codes(validate_two_node_e2e_evidence(stale_config)["lane_summaries"]["docker_security"]["blockers"]))

    assert {
        "TWO_NODE_E2E_DOCKER_LIVE_CONTAINER_EVIDENCE_MISSING",
        "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_MISMATCH",
        "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
    } <= observed_codes
    assert all(any(code.startswith(namespace) for namespace in namespaces) for code in observed_codes)


def test_readonly_db_owner_module_covers_live_source_route_permission_contract() -> None:
    assert two_node_e2e_readonly_db_lane.READONLY_DB_LANE_OWNER == (
        "services.production_closure.two_node_e2e_readonly_db_lane"
    )
    assert two_node_e2e_readonly_db_lane.READONLY_DB_LANE_VERIFICATION == (
        'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "readonly_db"'
    )
    assert two_node_e2e_readonly_db_lane.READONLY_DB_DOCUMENT_CANDIDATES == (
        "db/readonly-db-boundary/summary.json",
        "db/summary.json",
    )
    assert two_node_e2e_readonly_db_lane.READONLY_DB_LIVE_SCHEMA == READONLY_DB_LIVE_SCHEMA
    assert two_node_e2e_readonly_db_lane.READONLY_DB_REQUIRED_ROUTE_NAMES == frozenset(
        {
            "health",
            "runtime_config",
            "models",
            "stations",
            "latest_product",
            "pipeline_status",
            "pipeline_stages",
            "jobs",
            "job_logs",
        }
    )
    assert two_node_e2e_readonly_db_lane.READONLY_DB_STRICT_ROUTE_FIELDS == {
        "latest_product": two_node_e2e_metadata_lane.STRICT_IDENTITY_FIELDS,
        "pipeline_status": two_node_e2e_metadata_lane.STRICT_IDENTITY_FIELDS,
        "pipeline_stages": two_node_e2e_metadata_lane.STRICT_IDENTITY_FIELDS,
        "jobs": two_node_e2e_metadata_lane.STRICT_IDENTITY_FIELDS,
        "job_logs": two_node_e2e_metadata_lane.STRICT_LOG_IDENTITY_FIELDS,
    }
    assert two_node_e2e_readonly_db_lane.READONLY_DB_REQUIRED_MANUAL_ACTIONS == frozenset({"retry", "cancel"})
    assert two_node_e2e_readonly_db_lane.READONLY_DB_REQUIRED_PERMISSION_TARGETS == frozenset(
        {
            "hydro.hydro_run",
            "hydro.river_timeseries",
            "met.forecast_cycle",
            "met.forcing_station_timeseries",
            "ops.pipeline_job",
            "ops.pipeline_event",
            "reachable_roles",
            "audited_schema_sequences",
            "current_database",
            "hydro.*",
            "met.*",
            "ops.*",
        }
    )
    assert two_node_e2e_readonly_db_lane.READONLY_DB_TABLE_PERMISSION_TARGETS == frozenset(
        {
            "hydro.hydro_run",
            "hydro.river_timeseries",
            "met.forecast_cycle",
            "met.forcing_station_timeseries",
            "ops.pipeline_job",
            "ops.pipeline_event",
        }
    )
    assert two_node_e2e_readonly_db_lane.READONLY_DB_SCHEMA_PERMISSION_TARGETS == frozenset(
        {"hydro.*", "met.*", "ops.*"}
    )
    assert two_node_e2e_readonly_db_lane.READONLY_DB_TABLE_REQUIRED_OPERATIONS == frozenset(
        {"INSERT", "UPDATE", "DELETE"}
    )
    assert two_node_e2e_readonly_db_lane.READONLY_DB_SCHEMA_REQUIRED_OPERATIONS == frozenset(
        {"DDL_CREATE_TABLE"}
    )
    assert two_node_e2e_readonly_db_lane.READONLY_DB_DATABASE_REQUIRED_OPERATIONS == frozenset(
        {"DATABASE_CREATE"}
    )
    assert two_node_e2e_readonly_db_lane.READONLY_DB_SEQUENCE_REQUIRED_OPERATIONS == frozenset(
        {"AUDITED_SCHEMA_SEQUENCE_USAGE_UPDATE"}
    )
    assert two_node_e2e_readonly_db_lane.READONLY_DB_TABLE_MUTATING_FIELDS == (
        "table_privileges",
        "column_privileges",
        "sequence_privileges",
    )
    assert two_node_e2e_readonly_db_lane.READONLY_DB_MANUAL_WRITE_PROOF_ALIASES == {
        "write_dependency_constructed": (
            "write_dependency_constructed",
            "db_write_dependency_constructed",
            "control_write_dependency_constructed",
            "state_mutation_dependency_constructed",
        ),
        "write_executed": (
            "write_executed",
            "db_write_executed",
            "control_executed",
            "state_mutation_executed",
        ),
    }
    assert two_node_e2e_readonly_db_lane.READONLY_DB_SOURCE_ARTIFACT_FILENAMES == (
        "summary.json",
        "role.json",
        "route_smoke.json",
        "permission_probes.json",
    )
    assert two_node_e2e_readonly_db_lane.READONLY_DB_BLOCKER_NAMESPACES == (
        "TWO_NODE_E2E_READONLY_DB_",
        "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_",
        "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
        "TWO_NODE_E2E_STRICT_IDENTITY_",
        "TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE",
        "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE",
    )
    for symbol in two_node_e2e_readonly_db_lane.READONLY_DB_LANE_GUARD_SYMBOLS:
        assert _module_symbol_exists(two_node_e2e_readonly_db_lane, symbol), symbol


@pytest.mark.parametrize("candidate", two_node_e2e_readonly_db_lane.READONLY_DB_DOCUMENT_CANDIDATES)
def test_readonly_db_owner_discovers_each_summary_alias(candidate: str) -> None:
    run_id = _run_id(f"readonly-db-alias-{candidate.replace('/', '-')}")
    config = _seed_pass_bundle(run_id)
    canonical_dir = config.run_dir / "db" / "readonly-db-boundary"
    canonical_payloads = {
        filename: json.loads((canonical_dir / filename).read_text(encoding="utf-8"))
        for filename in ("summary.json", "role.json", "route_smoke.json", "permission_probes.json")
    }
    candidate_path = config.run_dir / candidate
    if candidate_path != canonical_dir / "summary.json":
        (canonical_dir / "summary.json").unlink()
        _write(candidate_path, canonical_payloads["summary.json"])
        for sibling in ("role.json", "route_smoke.json", "permission_probes.json"):
            _write(candidate_path.parent / sibling, canonical_payloads[sibling])

    summary = validate_two_node_e2e_evidence(config)

    assert summary["lane_summaries"]["readonly_db"]["status"] == STATUS_PASS
    assert summary["lane_summaries"]["readonly_db"]["evidence_path"] == str(
        candidate_path.resolve().relative_to(REPO_ROOT)
    )


def test_simple_lane_owner_module_covers_slurm_compute_summary_display_summary_contract() -> None:
    assert two_node_e2e_simple_live_lane.SIMPLE_LIVE_LANE_OWNER == (
        "services.production_closure.two_node_e2e_simple_live_lane"
    )
    assert two_node_e2e_simple_live_lane.SLURM_LANE_VERIFICATION == (
        'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or slurm"'
    )
    assert two_node_e2e_simple_live_lane.COMPUTE_SUMMARY_LANE_VERIFICATION == (
        'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or compute_summary"'
    )
    assert two_node_e2e_simple_live_lane.DISPLAY_SUMMARY_LANE_VERIFICATION == (
        'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or display_summary"'
    )
    assert two_node_e2e_simple_live_lane.SLURM_DOCUMENT_CANDIDATES == (
        "slurm/summary.json",
        "slurm/evidence.json",
    )
    assert two_node_e2e_simple_live_lane.COMPUTE_SUMMARY_DOCUMENT_CANDIDATES == (
        "22-compute/summary.json",
        "compute/summary.json",
        "compute-summary.json",
    )
    assert two_node_e2e_simple_live_lane.DISPLAY_SUMMARY_DOCUMENT_CANDIDATES == (
        "27-display/summary.json",
        "display/summary.json",
        "display-summary.json",
    )
    assert two_node_e2e_simple_live_lane.SLURM_LANE_CONFIG.live_flag == "live_slurm_evidence"
    assert two_node_e2e_simple_live_lane.SLURM_LANE_CONFIG.pass_aliases == (STATUS_PASS,)
    assert two_node_e2e_simple_live_lane.COMPUTE_SUMMARY_LANE_CONFIG.live_flag == "live_compute_evidence"
    assert two_node_e2e_simple_live_lane.COMPUTE_SUMMARY_LANE_CONFIG.pass_aliases == (
        STATUS_PASS,
        "ready",
        "submitted",
    )
    assert two_node_e2e_simple_live_lane.DISPLAY_SUMMARY_LANE_CONFIG.live_flag == "live_display_evidence"
    assert two_node_e2e_simple_live_lane.DISPLAY_SUMMARY_LANE_CONFIG.pass_aliases == (
        STATUS_PASS,
        "ready",
    )
    assert two_node_e2e_simple_live_lane.SIMPLE_LIVE_LANE_CONFIGS == {
        "slurm": two_node_e2e_simple_live_lane.SLURM_LANE_CONFIG,
        "compute_summary": two_node_e2e_simple_live_lane.COMPUTE_SUMMARY_LANE_CONFIG,
        "display_summary": two_node_e2e_simple_live_lane.DISPLAY_SUMMARY_LANE_CONFIG,
    }
    assert two_node_e2e_simple_live_lane.SIMPLE_LIVE_LANE_BLOCKER_NAMESPACES == (
        "TWO_NODE_E2E_SLURM_",
        "TWO_NODE_E2E_COMPUTE_SUMMARY_",
        "TWO_NODE_E2E_DISPLAY_SUMMARY_",
        "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_",
        "TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH",
        "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
        "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_",
    )
    for symbol in two_node_e2e_simple_live_lane.SIMPLE_LIVE_LANE_GUARD_SYMBOLS:
        assert _module_symbol_exists(two_node_e2e_simple_live_lane, symbol), symbol


@pytest.mark.parametrize(
    "lane_config",
    tuple(two_node_e2e_simple_live_lane.SIMPLE_LIVE_LANE_CONFIGS.values()),
    ids=lambda lane_config: lane_config.name,
)
def test_simple_lane_owner_evaluates_slurm_compute_summary_display_summary_contract(
    lane_config: two_node_e2e_simple_live_lane.SimpleLiveLaneConfig,
) -> None:
    run_id = _run_id(f"{lane_config.name}-owner")
    config = _seed_pass_bundle(run_id)
    doc = two_node_e2e_evidence._find_first_json(config.run_dir, lane_config.document_candidates)
    assert doc is not None

    lane = two_node_e2e_simple_live_lane.evaluate_simple_live_lane(
        lane_config,
        doc,
        evidence_run_id=run_id,
        run_dir=config.run_dir,
        helpers=two_node_e2e_evidence._simple_live_lane_helpers(),
    )

    assert lane.status == STATUS_PASS
    summary = validate_two_node_e2e_evidence(config)
    assert summary["lane_summaries"][lane_config.name]["status"] == lane.status
    assert summary["lane_summaries"][lane_config.name]["summary_status"] == lane.summary_status


@pytest.mark.parametrize(("lane_name", "candidate"), _simple_live_alias_cases())
def test_simple_lane_owner_discovers_slurm_compute_summary_display_summary_aliases(
    lane_name: str,
    candidate: str,
) -> None:
    run_id = _run_id(f"{lane_name}-alias-{candidate.replace('/', '-')}")
    config = _seed_pass_bundle(run_id)
    lane_config = two_node_e2e_simple_live_lane.SIMPLE_LIVE_LANE_CONFIGS[lane_name]
    canonical = config.run_dir / lane_config.document_candidates[0]
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    candidate_path = config.run_dir / candidate
    if candidate_path != canonical:
        canonical.unlink()
        _write(candidate_path, payload)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["lane_summaries"][lane_name]["status"] == STATUS_PASS
    assert summary["lane_summaries"][lane_name]["evidence_path"] == str(
        candidate_path.resolve().relative_to(REPO_ROOT)
    )


@pytest.mark.parametrize(("lane_name", "pass_alias"), _simple_live_pass_alias_cases())
def test_simple_lane_owner_preserves_each_pass_alias(lane_name: str, pass_alias: str) -> None:
    run_id = _run_id(f"{lane_name}-{pass_alias.lower()}-alias")
    config = _seed_pass_bundle(run_id)
    lane_config = two_node_e2e_simple_live_lane.SIMPLE_LIVE_LANE_CONFIGS[lane_name]
    payload = _read(config.run_dir / lane_config.document_candidates[0])
    payload["status"] = pass_alias
    _write(config.run_dir / lane_config.document_candidates[0], payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_name]
    assert lane["status"] == STATUS_PASS
    assert lane["summary_status"] == pass_alias


@pytest.mark.parametrize(
    "lane_config",
    tuple(two_node_e2e_simple_live_lane.SIMPLE_LIVE_LANE_CONFIGS.values()),
    ids=lambda lane_config: lane_config.name,
)
@pytest.mark.parametrize("producer_status", [STATUS_FAIL, STATUS_BLOCKED])
def test_simple_lane_owner_preserves_non_pass_statuses(
    lane_config: two_node_e2e_simple_live_lane.SimpleLiveLaneConfig,
    producer_status: str,
) -> None:
    run_id = _run_id(f"{lane_config.name}-{producer_status.lower()}")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_config.document_candidates[0])
    payload["status"] = producer_status
    payload.pop("commands", None)
    payload.pop(lane_config.live_flag, None)
    _write(config.run_dir / lane_config.document_candidates[0], payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_config.name]
    assert lane["status"] == producer_status
    assert lane["summary_status"] == producer_status
    assert f"TWO_NODE_E2E_{lane_config.name.upper()}_PRODUCER_EVIDENCE_MISSING" not in _codes(
        lane["blockers"]
    )


@pytest.mark.parametrize(
    "lane_config",
    tuple(two_node_e2e_simple_live_lane.SIMPLE_LIVE_LANE_CONFIGS.values()),
    ids=lambda lane_config: lane_config.name,
)
def test_simple_lane_owner_missing_lane_summary_shape(
    lane_config: two_node_e2e_simple_live_lane.SimpleLiveLaneConfig,
) -> None:
    run_id = _run_id(f"{lane_config.name}-missing-simple-lane")
    config = _seed_pass_bundle(run_id)
    for candidate in lane_config.document_candidates:
        candidate_path = config.run_dir / candidate
        if candidate_path.exists():
            candidate_path.unlink()

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_config.name]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert lane["evidence_path"] is None
    assert lane["evidence_sha256"] is None
    assert lane["summary_status"] is None
    assert f"TWO_NODE_E2E_{lane_config.name.upper()}_EVIDENCE_MISSING" in _codes(lane["blockers"])


@pytest.mark.parametrize(
    "lane_config",
    tuple(two_node_e2e_simple_live_lane.SIMPLE_LIVE_LANE_CONFIGS.values()),
    ids=lambda lane_config: lane_config.name,
)
def test_simple_lane_owner_mock_pass_becomes_fail(
    lane_config: two_node_e2e_simple_live_lane.SimpleLiveLaneConfig,
) -> None:
    run_id = _run_id(f"{lane_config.name}-mock-simple-lane")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_config.document_candidates[0])
    payload["execution_mode"] = "fixture"
    _write(config.run_dir / lane_config.document_candidates[0], payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_config.name]
    assert summary["status"] == STATUS_FAIL
    assert lane["status"] == STATUS_FAIL
    assert f"TWO_NODE_E2E_{lane_config.name.upper()}_MOCK_EVIDENCE" in _codes(lane["findings"])


@pytest.mark.parametrize(
    ("lane_name", "flat_alias"),
    [
        ("compute_summary", "compute-summary.json"),
        ("display_summary", "display-summary.json"),
    ],
)
def test_simple_lane_flat_alias_preserves_legacy_source_artifact_scope(
    lane_name: str,
    flat_alias: str,
) -> None:
    run_id = _run_id(f"{lane_name}-flat-artifact-scope")
    config = _seed_pass_bundle(run_id)
    lane_config = two_node_e2e_simple_live_lane.SIMPLE_LIVE_LANE_CONFIGS[lane_name]
    canonical = config.run_dir / lane_config.document_candidates[0]
    payload = _read(canonical)
    sibling_artifact = config.run_dir.parent / f"{lane_name}-producer-artifact.json"
    _write(sibling_artifact, {"status": STATUS_PASS, "evidence_run_id": run_id})
    payload["source_artifacts"] = [_artifact_summary(sibling_artifact)]
    canonical.unlink()
    _write(config.run_dir / flat_alias, payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_name]
    assert lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_STALE_OR_UNSCOPED" not in _codes(
        lane["blockers"]
    )


def test_api_lane_owner_module_covers_api_source_contract() -> None:
    assert two_node_e2e_api_lane.API_LANE_OWNER == "services.production_closure.two_node_e2e_api_lane"
    assert two_node_e2e_api_lane.API_LANE_VERIFICATION == (
        'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "api"'
    )
    assert two_node_e2e_api_lane.API_DOCUMENT_CANDIDATES == (
        "api/summary.json",
        "api/evidence.json",
    )
    assert two_node_e2e_api_lane.API_REQUIRED_CHECKS == (
        "latest_product",
        "series",
        "ops_status",
        "ops_stages",
        "jobs",
    )
    assert two_node_e2e_api_lane.API_LIVE_FLAG == "live_api_evidence"
    assert two_node_e2e_api_lane.API_LANE_BLOCKER_NAMESPACES == (
        "TWO_NODE_E2E_API_",
        "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_",
        "TWO_NODE_E2E_STRICT_IDENTITY_",
        "TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE",
        "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE",
        "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_",
        "TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH",
        "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
    )
    for symbol in two_node_e2e_api_lane.API_LANE_GUARD_SYMBOLS:
        assert _module_symbol_exists(two_node_e2e_api_lane, symbol), symbol


def test_api_lane_owner_direct_evaluator_matches_full_validator_pass() -> None:
    run_id = _run_id("api-owner-pass-parity")
    config = _seed_pass_bundle(run_id)
    metadata_doc = two_node_e2e_evidence._find_first_json(
        config.run_dir,
        two_node_e2e_metadata_lane.METADATA_DOCUMENT_CANDIDATES,
    )
    assert metadata_doc is not None
    metadata_result = two_node_e2e_metadata_lane.evaluate_metadata_lane(
        metadata_doc,
        metadata_doc.payload,
        evidence_run_id=run_id,
        configured_declared_sources=config.declared_sources,
        configured_reduced_scope=config.reduced_scope,
        helpers=two_node_e2e_evidence._metadata_lane_helpers(),
    )
    api_doc = two_node_e2e_evidence._find_first_json(
        config.run_dir,
        two_node_e2e_api_lane.API_DOCUMENT_CANDIDATES,
    )
    assert api_doc is not None

    api_lane = two_node_e2e_api_lane.evaluate_api_lane(
        api_doc,
        declared_sources=metadata_result.scope.declared_sources,
        strict_identities=metadata_result.strict_identities,
        evidence_run_id=run_id,
        helpers=two_node_e2e_evidence._api_lane_helpers(),
    )
    summary = validate_two_node_e2e_evidence(config)

    assert api_lane.status == STATUS_PASS
    assert summary["lane_summaries"]["api"] == api_lane.to_summary()
    assert summary["source_scope_results"]["GFS"]["lane_statuses"]["api"] == STATUS_PASS
    assert summary["source_scope_results"]["IFS"]["lane_statuses"]["api"] == STATUS_PASS


@pytest.mark.parametrize("candidate", two_node_e2e_api_lane.API_DOCUMENT_CANDIDATES)
def test_api_lane_owner_discovers_each_api_alias(candidate: str) -> None:
    run_id = _run_id(f"api-alias-{candidate.replace('/', '-')}")
    config = _seed_pass_bundle(run_id)
    canonical = config.run_dir / two_node_e2e_api_lane.API_DOCUMENT_CANDIDATES[0]
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    candidate_path = config.run_dir / candidate
    if candidate_path != canonical:
        canonical.unlink()
        _write(candidate_path, payload)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["lane_summaries"]["api"]["status"] == STATUS_PASS
    assert summary["lane_summaries"]["api"]["evidence_path"] == str(
        candidate_path.resolve().relative_to(REPO_ROOT)
    )


def test_api_lane_owner_missing_api_summary_shape_and_source_scope_contribution() -> None:
    run_id = _run_id("api-missing-owner")
    config = _seed_pass_bundle(run_id)
    for candidate in two_node_e2e_api_lane.API_DOCUMENT_CANDIDATES:
        candidate_path = config.run_dir / candidate
        if candidate_path.exists():
            candidate_path.unlink()

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert lane["evidence_path"] is None
    assert lane["evidence_sha256"] is None
    assert lane["summary_status"] is None
    assert "TWO_NODE_E2E_API_EVIDENCE_MISSING" in _codes(lane["blockers"])
    assert summary["source_scope_results"]["GFS"]["lane_statuses"]["api"] == STATUS_BLOCKED
    assert summary["source_scope_results"]["IFS"]["lane_statuses"]["api"] == STATUS_BLOCKED


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        ("missing", "TWO_NODE_E2E_API_CHECK_MISSING"),
        ("failed", "TWO_NODE_E2E_API_CHECK_FAILED"),
        ("blocked", "TWO_NODE_E2E_API_CHECK_BLOCKED"),
        ("partial", "TWO_NODE_E2E_API_CHECK_BLOCKED"),
    ],
)
def test_api_lane_owner_required_check_gaps_block_via_validator(
    mutator: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"api-required-check-{mutator}")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    checks = api_summary["sources"]["GFS"]["checks"]
    if mutator == "missing":
        checks.pop("latest_product")
    elif mutator == "failed":
        checks["latest_product"]["status"] = STATUS_FAIL
    elif mutator == "blocked":
        checks["latest_product"]["status"] = STATUS_BLOCKED
    else:
        checks["latest_product"]["status"] = STATUS_PARTIAL
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    source_scope = summary["source_scope_results"]["GFS"]
    expected_status = STATUS_FAIL if mutator == "failed" else STATUS_BLOCKED
    expected_bucket = "findings" if mutator == "failed" else "blockers"
    assert summary["status"] == expected_status
    assert api_lane["status"] == expected_status
    assert expected_code in _codes(api_lane[expected_bucket])
    assert expected_code in _codes(source_scope[expected_bucket])
    assert source_scope["lane_statuses"]["api"] == expected_status


@pytest.mark.parametrize(
    ("mutator", "expected_status", "expected_bucket", "expected_code"),
    [
        ("wrong_identity", STATUS_FAIL, "findings", "TWO_NODE_E2E_STRICT_IDENTITY_MISMATCH"),
        (
            "partial_identity",
            STATUS_BLOCKED,
            "blockers",
            "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE",
        ),
        ("historical_latest", STATUS_FAIL, "findings", "TWO_NODE_E2E_API_HISTORICAL_CHECK"),
    ],
)
def test_api_lane_owner_strict_identity_and_historical_latest_negative_cases(
    mutator: str,
    expected_status: str,
    expected_bucket: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"api-{mutator}")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    latest = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    if mutator == "wrong_identity":
        latest["identity"]["model_id"] = "wrong-model"
    elif mutator == "partial_identity":
        latest["identity"].pop("model_id")
    else:
        latest["historical_latest"] = True
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    source_scope = summary["source_scope_results"]["GFS"]
    assert summary["status"] == expected_status
    assert api_lane["status"] == expected_status
    assert expected_code in _codes(api_lane[expected_bucket])
    assert expected_code in _codes(source_scope[expected_bucket])


@pytest.mark.parametrize(
    ("source_status", "expected_status", "expected_bucket", "expected_code"),
    [
        (STATUS_FAIL, STATUS_FAIL, "findings", "TWO_NODE_E2E_API_SOURCE_FAILED"),
        (STATUS_BLOCKED, STATUS_BLOCKED, "blockers", "TWO_NODE_E2E_API_SOURCE_BLOCKED"),
        (STATUS_PARTIAL, STATUS_PARTIAL, "blockers", None),
    ],
)
def test_api_lane_owner_source_statuses_fold_into_validator_summary(
    source_status: str,
    expected_status: str,
    expected_bucket: str,
    expected_code: str | None,
) -> None:
    run_id = _run_id(f"api-source-{source_status.lower()}")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    api_summary["sources"]["GFS"]["status"] = source_status
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == expected_status
    assert api_lane["status"] == expected_status
    if expected_code is not None:
        assert expected_code in _codes(api_lane[expected_bucket])
        assert expected_code in _codes(summary["source_scope_results"]["GFS"][expected_bucket])
    else:
        assert api_lane["blockers"] == []
        assert api_lane["findings"] == []


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        ("lane", "TWO_NODE_E2E_API_MOCK_EVIDENCE"),
        ("check", "TWO_NODE_E2E_API_MOCK_CHECK"),
    ],
)
def test_api_lane_owner_mock_evidence_fails_via_validator(
    mutator: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"api-mock-{mutator}")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    if mutator == "lane":
        api_summary["mock_api_data"] = True
    else:
        api_summary["sources"]["GFS"]["checks"]["latest_product"]["mock_api_data"] = True
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_FAIL
    assert api_lane["status"] == STATUS_FAIL
    assert expected_code in _codes(api_lane["findings"])


def test_api_lane_owner_preserves_flat_alias_producer_artifact_scope() -> None:
    run_id = _run_id("api-flat-artifact-scope")
    config = _seed_pass_bundle(run_id)
    canonical = config.run_dir / two_node_e2e_api_lane.API_DOCUMENT_CANDIDATES[0]
    payload = _read(canonical)
    sibling_artifact = config.run_dir.parent / "api-producer-artifact.json"
    _write(sibling_artifact, {"status": STATUS_PASS, "evidence_run_id": run_id})
    payload["source_artifacts"] = [_artifact_summary(sibling_artifact)]
    flat_path = config.run_dir / "api-summary.json"
    _write(flat_path, payload)
    flat_doc = two_node_e2e_evidence._read_json(flat_path, containment_root=config.run_dir.parent)
    strict_identities = _read(config.run_dir / "run.json")["strict_identities"]

    api_lane = two_node_e2e_api_lane.evaluate_api_lane(
        flat_doc,
        declared_sources=config.declared_sources,
        strict_identities=strict_identities,
        evidence_run_id=run_id,
        helpers=two_node_e2e_evidence._api_lane_helpers(),
    )

    assert api_lane.status == STATUS_PASS
    assert "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_STALE_OR_UNSCOPED" not in _codes(
        api_lane.blockers
    )


def test_browser_lane_owner_module_covers_browser_source_contract() -> None:
    assert two_node_e2e_browser_lane.BROWSER_LANE_OWNER == (
        "services.production_closure.two_node_e2e_browser_lane"
    )
    assert two_node_e2e_browser_lane.BROWSER_LANE_VERIFICATION == (
        'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "browser"'
    )
    assert two_node_e2e_browser_lane.BROWSER_DOCUMENT_CANDIDATES == (
        "browser/summary.json",
        "browser/evidence.json",
    )
    assert two_node_e2e_browser_lane.BROWSER_BASE_REQUIRED_CHECKS == (
        "hydro_met",
        "ops",
        "ops_jobs",
        "ops_job_logs",
    )
    assert two_node_e2e_browser_lane.BROWSER_SOURCE_SWITCH_CHECK == "source_switch"
    assert two_node_e2e_browser_lane.BROWSER_JOB_ID_REQUIRED_CHECKS == (
        "ops_jobs",
        "ops_job_logs",
    )
    assert two_node_e2e_browser_lane.browser_required_checks(("GFS",)) == (
        "hydro_met",
        "ops",
        "ops_jobs",
        "ops_job_logs",
    )
    assert two_node_e2e_browser_lane.browser_required_checks(("GFS", "IFS")) == (
        "hydro_met",
        "ops",
        "ops_jobs",
        "ops_job_logs",
        "source_switch",
    )
    assert two_node_e2e_browser_lane.BROWSER_LIVE_FLAG == "live_browser_evidence"
    assert two_node_e2e_browser_lane.BROWSER_LANE_BLOCKER_NAMESPACES == (
        "TWO_NODE_E2E_BROWSER_",
        "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_",
        "TWO_NODE_E2E_STRICT_IDENTITY_",
        "TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE",
        "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE",
        "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_",
        "TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH",
        "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
    )
    for symbol in two_node_e2e_browser_lane.BROWSER_LANE_GUARD_SYMBOLS:
        assert _module_symbol_exists(two_node_e2e_browser_lane, symbol), symbol


def test_browser_lane_owner_direct_evaluator_matches_full_validator_pass() -> None:
    run_id = _run_id("browser-owner-pass-parity")
    config = _seed_pass_bundle(run_id)
    metadata_doc = two_node_e2e_evidence._find_first_json(
        config.run_dir,
        two_node_e2e_metadata_lane.METADATA_DOCUMENT_CANDIDATES,
    )
    assert metadata_doc is not None
    metadata_result = two_node_e2e_metadata_lane.evaluate_metadata_lane(
        metadata_doc,
        metadata_doc.payload,
        evidence_run_id=run_id,
        configured_declared_sources=config.declared_sources,
        configured_reduced_scope=config.reduced_scope,
        helpers=two_node_e2e_evidence._metadata_lane_helpers(),
    )
    browser_doc = two_node_e2e_evidence._find_first_json(
        config.run_dir,
        two_node_e2e_browser_lane.BROWSER_DOCUMENT_CANDIDATES,
    )
    assert browser_doc is not None

    browser_lane = two_node_e2e_browser_lane.evaluate_browser_lane(
        browser_doc,
        declared_sources=metadata_result.scope.declared_sources,
        strict_identities=metadata_result.strict_identities,
        evidence_run_id=run_id,
        helpers=two_node_e2e_evidence._browser_lane_helpers(),
    )
    summary = validate_two_node_e2e_evidence(config)

    assert browser_lane.status == STATUS_PASS
    assert summary["lane_summaries"]["browser"] == browser_lane.to_summary()
    assert summary["source_scope_results"]["GFS"]["lane_statuses"]["browser"] == STATUS_PASS
    assert summary["source_scope_results"]["IFS"]["lane_statuses"]["browser"] == STATUS_PASS


def test_browser_lane_owner_validator_uses_owner_evaluator(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _run_id("browser-owner-validator-call")
    config = _seed_pass_bundle(run_id)
    original = two_node_e2e_evidence.evaluate_browser_lane
    call: dict[str, Any] = {}

    def spy_evaluate_browser_lane(
        doc: two_node_e2e_evidence.EvidenceDocument | None,
        *,
        declared_sources: tuple[str, ...],
        strict_identities: Mapping[str, Mapping[str, Any]],
        evidence_run_id: str,
        helpers: two_node_e2e_browser_lane.BrowserLaneEvaluationHelpers[
            two_node_e2e_evidence.LaneEvaluation
        ],
    ) -> two_node_e2e_evidence.LaneEvaluation:
        call["doc_path"] = doc.path if doc is not None else None
        call["declared_sources"] = declared_sources
        call["strict_identity_sources"] = tuple(sorted(strict_identities))
        call["evidence_run_id"] = evidence_run_id
        call["helpers_type"] = type(helpers)
        return original(
            doc,
            declared_sources=declared_sources,
            strict_identities=strict_identities,
            evidence_run_id=evidence_run_id,
            helpers=helpers,
        )

    monkeypatch.setattr(two_node_e2e_evidence, "evaluate_browser_lane", spy_evaluate_browser_lane)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["lane_summaries"]["browser"]["status"] == STATUS_PASS
    assert call == {
        "doc_path": (config.run_dir / "browser" / "summary.json").resolve(strict=False),
        "declared_sources": ("GFS", "IFS"),
        "strict_identity_sources": ("GFS", "IFS"),
        "evidence_run_id": run_id,
        "helpers_type": two_node_e2e_browser_lane.BrowserLaneEvaluationHelpers,
    }


@pytest.mark.parametrize("candidate", two_node_e2e_browser_lane.BROWSER_DOCUMENT_CANDIDATES)
def test_browser_lane_owner_discovers_each_browser_alias(candidate: str) -> None:
    run_id = _run_id(f"browser-alias-{candidate.replace('/', '-')}")
    config = _seed_pass_bundle(run_id)
    canonical = config.run_dir / two_node_e2e_browser_lane.BROWSER_DOCUMENT_CANDIDATES[0]
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    candidate_path = config.run_dir / candidate
    if candidate_path != canonical:
        canonical.unlink()
        _write(candidate_path, payload)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["lane_summaries"]["browser"]["status"] == STATUS_PASS
    assert summary["lane_summaries"]["browser"]["evidence_path"] == str(
        candidate_path.resolve().relative_to(REPO_ROOT)
    )


def test_browser_lane_owner_missing_browser_summary_shape_and_source_scope_contribution() -> None:
    run_id = _run_id("browser-missing-owner")
    config = _seed_pass_bundle(run_id)
    for candidate in two_node_e2e_browser_lane.BROWSER_DOCUMENT_CANDIDATES:
        candidate_path = config.run_dir / candidate
        if candidate_path.exists():
            candidate_path.unlink()

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"]["browser"]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert lane["evidence_path"] is None
    assert lane["evidence_sha256"] is None
    assert lane["summary_status"] is None
    assert "TWO_NODE_E2E_BROWSER_EVIDENCE_MISSING" in _codes(lane["blockers"])
    assert summary["source_scope_results"]["GFS"]["lane_statuses"]["browser"] == STATUS_BLOCKED
    assert summary["source_scope_results"]["IFS"]["lane_statuses"]["browser"] == STATUS_BLOCKED


def test_browser_lane_owner_missing_declared_source_blocks_via_validator() -> None:
    run_id = _run_id("browser-missing-declared-source")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    browser_summary["sources"].pop("IFS")
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    ifs_scope = summary["source_scope_results"]["IFS"]
    assert summary["status"] == STATUS_BLOCKED
    assert browser_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_BROWSER_SOURCE_MISSING" in _codes(browser_lane["blockers"])
    assert "TWO_NODE_E2E_BROWSER_SOURCE_MISSING" in _codes(ifs_scope["blockers"])
    assert ifs_scope["lane_statuses"]["browser"] == STATUS_BLOCKED


def test_browser_lane_owner_missing_live_browser_flag_blocks_via_validator() -> None:
    run_id = _run_id("browser-missing-live-flag")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    browser_summary.pop(two_node_e2e_browser_lane.BROWSER_LIVE_FLAG)
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    assert summary["status"] == STATUS_BLOCKED
    assert browser_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_BROWSER_LIVE_EVIDENCE_MISSING" in _codes(browser_lane["blockers"])


def test_browser_lane_owner_allows_single_source_without_source_switch() -> None:
    run_id = _run_id("browser-single-source-no-switch")
    config = _seed_pass_bundle(run_id, sources=("GFS",), reduced_scope=True)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    assert browser_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_BROWSER_CHECK_MISSING" not in _codes(browser_lane["blockers"])
    assert summary["source_scope_results"]["GFS"]["lane_statuses"]["browser"] == STATUS_PASS


def test_browser_lane_owner_requires_source_switch_for_multi_source_scope() -> None:
    run_id = _run_id("browser-multi-source-no-switch")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    browser_summary["sources"]["GFS"]["checks"].pop("source_switch")
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    assert summary["status"] == STATUS_BLOCKED
    assert browser_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_BROWSER_CHECK_MISSING" in _codes(browser_lane["blockers"])
    assert any(
        blocker.get("source") == "GFS" and blocker.get("check") == "source_switch"
        for blocker in browser_lane["blockers"]
    )


@pytest.mark.parametrize("check", two_node_e2e_browser_lane.BROWSER_JOB_ID_REQUIRED_CHECKS)
def test_browser_lane_owner_job_like_check_requires_job_id_binding(check: str) -> None:
    run_id = _run_id(f"browser-{check}-no-job")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    browser_summary["sources"]["GFS"]["checks"][check]["identity"].pop("job_id")
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    source_scope = summary["source_scope_results"]["GFS"]
    assert summary["status"] == STATUS_BLOCKED
    assert browser_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE" in _codes(browser_lane["blockers"])
    assert "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE" in _codes(source_scope["blockers"])


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        ("missing", "TWO_NODE_E2E_BROWSER_CHECK_MISSING"),
        ("failed", "TWO_NODE_E2E_BROWSER_CHECK_FAILED"),
        ("blocked", "TWO_NODE_E2E_BROWSER_CHECK_BLOCKED"),
        ("partial", "TWO_NODE_E2E_BROWSER_CHECK_BLOCKED"),
    ],
)
def test_browser_lane_owner_required_check_gaps_block_via_validator(
    mutator: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"browser-required-check-{mutator}")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    checks = browser_summary["sources"]["GFS"]["checks"]
    if mutator == "missing":
        checks.pop("hydro_met")
    elif mutator == "failed":
        checks["hydro_met"]["status"] = STATUS_FAIL
    elif mutator == "blocked":
        checks["hydro_met"]["status"] = STATUS_BLOCKED
    else:
        checks["hydro_met"]["status"] = STATUS_PARTIAL
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    source_scope = summary["source_scope_results"]["GFS"]
    expected_status = STATUS_FAIL if mutator == "failed" else STATUS_BLOCKED
    expected_bucket = "findings" if mutator == "failed" else "blockers"
    assert summary["status"] == expected_status
    assert browser_lane["status"] == expected_status
    assert expected_code in _codes(browser_lane[expected_bucket])
    assert expected_code in _codes(source_scope[expected_bucket])
    assert source_scope["lane_statuses"]["browser"] == expected_status


@pytest.mark.parametrize(
    ("mutator", "expected_status", "expected_bucket", "expected_code"),
    [
        ("wrong_identity", STATUS_FAIL, "findings", "TWO_NODE_E2E_STRICT_IDENTITY_MISMATCH"),
        (
            "partial_identity",
            STATUS_BLOCKED,
            "blockers",
            "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE",
        ),
        ("historical_latest", STATUS_FAIL, "findings", "TWO_NODE_E2E_BROWSER_HISTORICAL_CHECK"),
    ],
)
def test_browser_lane_owner_strict_identity_and_historical_latest_negative_cases(
    mutator: str,
    expected_status: str,
    expected_bucket: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"browser-{mutator}")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    hydro_met = browser_summary["sources"]["GFS"]["checks"]["hydro_met"]
    if mutator == "wrong_identity":
        hydro_met["identity"]["model_id"] = "wrong-model"
    elif mutator == "partial_identity":
        hydro_met["identity"].pop("model_id")
    else:
        hydro_met["historical_latest"] = True
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    source_scope = summary["source_scope_results"]["GFS"]
    assert summary["status"] == expected_status
    assert browser_lane["status"] == expected_status
    assert expected_code in _codes(browser_lane[expected_bucket])
    assert expected_code in _codes(source_scope[expected_bucket])


@pytest.mark.parametrize(
    ("source_status", "expected_status", "expected_bucket", "expected_code"),
    [
        (STATUS_FAIL, STATUS_FAIL, "findings", "TWO_NODE_E2E_BROWSER_SOURCE_FAILED"),
        (STATUS_BLOCKED, STATUS_BLOCKED, "blockers", "TWO_NODE_E2E_BROWSER_SOURCE_BLOCKED"),
        (STATUS_PARTIAL, STATUS_PARTIAL, "blockers", None),
    ],
)
def test_browser_lane_owner_source_statuses_fold_into_validator_summary(
    source_status: str,
    expected_status: str,
    expected_bucket: str,
    expected_code: str | None,
) -> None:
    run_id = _run_id(f"browser-source-{source_status.lower()}")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    browser_summary["sources"]["GFS"]["status"] = source_status
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    assert summary["status"] == expected_status
    assert browser_lane["status"] == expected_status
    if expected_code is not None:
        assert expected_code in _codes(browser_lane[expected_bucket])
        assert expected_code in _codes(summary["source_scope_results"]["GFS"][expected_bucket])
    else:
        assert browser_lane["blockers"] == []
        assert browser_lane["findings"] == []


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        ("lane", "TWO_NODE_E2E_BROWSER_MOCK_EVIDENCE"),
        ("check", "TWO_NODE_E2E_BROWSER_MOCK_CHECK"),
    ],
)
def test_browser_lane_owner_mock_evidence_fails_via_validator(
    mutator: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"browser-mock-{mutator}")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    if mutator == "lane":
        browser_summary["mock_browser_data"] = True
    else:
        browser_summary["sources"]["GFS"]["checks"]["hydro_met"]["mock_browser_data"] = True
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    assert summary["status"] == STATUS_FAIL
    assert browser_lane["status"] == STATUS_FAIL
    assert expected_code in _codes(browser_lane["findings"])


def test_browser_lane_owner_top_level_historical_latest_fails_via_validator() -> None:
    run_id = _run_id("browser-top-historical-latest")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    browser_summary["historical_latest"] = True
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    assert summary["status"] == STATUS_FAIL
    assert browser_lane["status"] == STATUS_FAIL
    assert "TWO_NODE_E2E_BROWSER_HISTORICAL_LATEST" in _codes(browser_lane["findings"])


def test_browser_lane_owner_preserves_flat_alias_producer_artifact_scope() -> None:
    run_id = _run_id("browser-flat-artifact-scope")
    config = _seed_pass_bundle(run_id)
    canonical = config.run_dir / two_node_e2e_browser_lane.BROWSER_DOCUMENT_CANDIDATES[0]
    payload = _read(canonical)
    sibling_artifact = config.run_dir.parent / "browser-producer-artifact.json"
    _write(sibling_artifact, {"status": STATUS_PASS, "evidence_run_id": run_id})
    payload["source_artifacts"] = [_artifact_summary(sibling_artifact)]
    flat_path = config.run_dir / "browser-summary.json"
    _write(flat_path, payload)
    flat_doc = two_node_e2e_evidence._read_json(flat_path, containment_root=config.run_dir.parent)
    strict_identities = _read(config.run_dir / "run.json")["strict_identities"]

    browser_lane = two_node_e2e_browser_lane.evaluate_browser_lane(
        flat_doc,
        declared_sources=config.declared_sources,
        strict_identities=strict_identities,
        evidence_run_id=run_id,
        helpers=two_node_e2e_evidence._browser_lane_helpers(),
    )

    assert browser_lane.status == STATUS_PASS
    assert "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_STALE_OR_UNSCOPED" not in _codes(
        browser_lane.blockers
    )


def _logs_owner_inputs(
    config: TwoNodeE2EEvidenceConfig,
) -> tuple[
    two_node_e2e_metadata_lane.MetadataLaneEvaluation,
    two_node_e2e_evidence.EvidenceDocument,
    two_node_e2e_evidence.EvidenceDocument | None,
]:
    metadata_doc = two_node_e2e_evidence._find_first_json(
        config.run_dir,
        two_node_e2e_metadata_lane.METADATA_DOCUMENT_CANDIDATES,
    )
    assert metadata_doc is not None
    metadata_result = two_node_e2e_metadata_lane.evaluate_metadata_lane(
        metadata_doc,
        metadata_doc.payload,
        evidence_run_id=config.run_id,
        configured_declared_sources=config.declared_sources,
        configured_reduced_scope=config.reduced_scope,
        helpers=two_node_e2e_evidence._metadata_lane_helpers(),
    )
    logs_doc = two_node_e2e_evidence._find_first_json(
        config.run_dir,
        two_node_e2e_logs_lane.LOGS_DOCUMENT_CANDIDATES,
    )
    assert logs_doc is not None
    docker_security_doc = two_node_e2e_evidence._find_first_json(
        config.run_dir,
        two_node_e2e_docker_security.DOCKER_SECURITY_DOCUMENT_CANDIDATES,
    )
    return metadata_result, logs_doc, docker_security_doc


def test_logs_lane_owner_module_covers_logs_source_contract() -> None:
    assert two_node_e2e_logs_lane.LOGS_LANE_OWNER == (
        "services.production_closure.two_node_e2e_logs_lane"
    )
    assert two_node_e2e_logs_lane.LOGS_LANE_VERIFICATION == (
        'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "logs"'
    )
    assert two_node_e2e_logs_lane.LOGS_DOCUMENT_CANDIDATES == (
        "logs/summary.json",
        "logs/evidence.json",
    )
    assert two_node_e2e_logs_lane.LOGS_REQUIRED_CHECKS == ("job_logs",)
    assert two_node_e2e_logs_lane.LOGS_JOB_ID_REQUIRED_CHECKS == ("job_logs",)
    assert two_node_e2e_logs_lane.LOGS_LIVE_FLAG == "live_log_evidence"
    assert two_node_e2e_logs_lane.LOGS_LANE_BLOCKER_NAMESPACES == (
        "TWO_NODE_E2E_LOGS_",
        "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_",
        "TWO_NODE_E2E_STRICT_IDENTITY_",
        "TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE",
        "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE",
        "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_",
        "TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH",
        "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
    )
    for symbol in two_node_e2e_logs_lane.LOGS_LANE_GUARD_SYMBOLS:
        assert _module_symbol_exists(two_node_e2e_logs_lane, symbol), symbol


def test_logs_lane_owner_direct_evaluator_matches_full_validator_pass() -> None:
    run_id = _run_id("logs-owner-pass-parity")
    config = _seed_pass_bundle(run_id)
    metadata_result, logs_doc, docker_security_doc = _logs_owner_inputs(config)

    logs_lane = two_node_e2e_logs_lane.evaluate_logs_lane(
        logs_doc,
        declared_sources=metadata_result.scope.declared_sources,
        strict_identities=metadata_result.strict_identities,
        evidence_run_id=run_id,
        docker_security_doc=docker_security_doc,
        helpers=two_node_e2e_evidence._logs_lane_helpers(),
    )
    summary = validate_two_node_e2e_evidence(config)

    assert logs_lane.status == STATUS_PASS
    assert summary["lane_summaries"]["logs"] == logs_lane.to_summary()
    assert summary["source_scope_results"]["GFS"]["lane_statuses"]["logs"] == STATUS_PASS
    assert summary["source_scope_results"]["IFS"]["lane_statuses"]["logs"] == STATUS_PASS


def test_logs_lane_owner_validator_uses_owner_evaluator(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = _run_id("logs-owner-validator-call")
    config = _seed_pass_bundle(run_id)
    original = two_node_e2e_evidence.evaluate_logs_lane
    call: dict[str, Any] = {}

    def spy_evaluate_logs_lane(
        doc: two_node_e2e_evidence.EvidenceDocument | None,
        *,
        declared_sources: tuple[str, ...],
        strict_identities: Mapping[str, Mapping[str, Any]],
        evidence_run_id: str,
        docker_security_doc: two_node_e2e_evidence.EvidenceDocument | None,
        helpers: two_node_e2e_logs_lane.LogsLaneEvaluationHelpers[
            two_node_e2e_evidence.LaneEvaluation
        ],
    ) -> two_node_e2e_evidence.LaneEvaluation:
        call["doc_path"] = doc.path if doc is not None else None
        call["declared_sources"] = declared_sources
        call["strict_identity_sources"] = tuple(sorted(strict_identities))
        call["evidence_run_id"] = evidence_run_id
        call["docker_security_path"] = docker_security_doc.path if docker_security_doc is not None else None
        call["helpers_type"] = type(helpers)
        return original(
            doc,
            declared_sources=declared_sources,
            strict_identities=strict_identities,
            evidence_run_id=evidence_run_id,
            docker_security_doc=docker_security_doc,
            helpers=helpers,
        )

    monkeypatch.setattr(two_node_e2e_evidence, "evaluate_logs_lane", spy_evaluate_logs_lane)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["lane_summaries"]["logs"]["status"] == STATUS_PASS
    assert call == {
        "doc_path": (config.run_dir / "logs" / "summary.json").resolve(strict=False),
        "declared_sources": ("GFS", "IFS"),
        "strict_identity_sources": ("GFS", "IFS"),
        "evidence_run_id": run_id,
        "docker_security_path": (
            config.run_dir / "docker-security" / "summary.json"
        ).resolve(strict=False),
        "helpers_type": two_node_e2e_logs_lane.LogsLaneEvaluationHelpers,
    }


@pytest.mark.parametrize("candidate", two_node_e2e_logs_lane.LOGS_DOCUMENT_CANDIDATES)
def test_logs_lane_owner_discovers_each_logs_alias(candidate: str) -> None:
    run_id = _run_id(f"logs-alias-{candidate.replace('/', '-')}")
    config = _seed_pass_bundle(run_id)
    canonical = config.run_dir / two_node_e2e_logs_lane.LOGS_DOCUMENT_CANDIDATES[0]
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    candidate_path = config.run_dir / candidate
    if candidate_path != canonical:
        canonical.unlink()
        _write(candidate_path, payload)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["lane_summaries"]["logs"]["status"] == STATUS_PASS
    assert summary["lane_summaries"]["logs"]["evidence_path"] == str(
        candidate_path.resolve().relative_to(REPO_ROOT)
    )


def test_logs_lane_owner_missing_logs_summary_shape_and_source_scope_contribution() -> None:
    run_id = _run_id("logs-missing-owner")
    config = _seed_pass_bundle(run_id)
    for candidate in two_node_e2e_logs_lane.LOGS_DOCUMENT_CANDIDATES:
        candidate_path = config.run_dir / candidate
        if candidate_path.exists():
            candidate_path.unlink()

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"]["logs"]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert lane["evidence_path"] is None
    assert lane["evidence_sha256"] is None
    assert lane["summary_status"] is None
    assert "TWO_NODE_E2E_LOGS_EVIDENCE_MISSING" in _codes(lane["blockers"])
    assert summary["source_scope_results"]["GFS"]["lane_statuses"]["logs"] == STATUS_BLOCKED
    assert summary["source_scope_results"]["IFS"]["lane_statuses"]["logs"] == STATUS_BLOCKED


def test_logs_lane_owner_missing_declared_source_blocks_via_validator() -> None:
    run_id = _run_id("logs-missing-declared-source")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    logs_summary["sources"].pop("IFS")
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    ifs_scope = summary["source_scope_results"]["IFS"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_SOURCE_MISSING" in _codes(logs_lane["blockers"])
    assert "TWO_NODE_E2E_LOGS_SOURCE_MISSING" in _codes(ifs_scope["blockers"])
    assert ifs_scope["lane_statuses"]["logs"] == STATUS_BLOCKED


def test_logs_lane_owner_missing_live_log_flag_blocks_via_validator() -> None:
    run_id = _run_id("logs-missing-live-flag")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    logs_summary.pop(two_node_e2e_logs_lane.LOGS_LIVE_FLAG)
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_LIVE_EVIDENCE_MISSING" in _codes(logs_lane["blockers"])


def test_logs_lane_owner_job_logs_check_requires_job_id_binding() -> None:
    run_id = _run_id("logs-job-logs-no-job")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    logs_summary["sources"]["GFS"]["checks"]["job_logs"]["identity"].pop("job_id")
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    source_scope = summary["source_scope_results"]["GFS"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE" in _codes(logs_lane["blockers"])
    assert "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE" in _codes(source_scope["blockers"])


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        ("missing", "TWO_NODE_E2E_LOGS_CHECK_MISSING"),
        ("failed", "TWO_NODE_E2E_LOGS_CHECK_FAILED"),
        ("blocked", "TWO_NODE_E2E_LOGS_CHECK_BLOCKED"),
        ("partial", "TWO_NODE_E2E_LOGS_CHECK_BLOCKED"),
    ],
)
def test_logs_lane_owner_required_check_gaps_block_via_validator(
    mutator: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"logs-required-check-{mutator}")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    checks = logs_summary["sources"]["GFS"]["checks"]
    if mutator == "missing":
        checks.pop("job_logs")
    elif mutator == "failed":
        checks["job_logs"]["status"] = STATUS_FAIL
    elif mutator == "blocked":
        checks["job_logs"]["status"] = STATUS_BLOCKED
    else:
        checks["job_logs"]["status"] = STATUS_PARTIAL
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    source_scope = summary["source_scope_results"]["GFS"]
    expected_status = STATUS_FAIL if mutator == "failed" else STATUS_BLOCKED
    expected_bucket = "findings" if mutator == "failed" else "blockers"
    assert summary["status"] == expected_status
    assert logs_lane["status"] == expected_status
    assert expected_code in _codes(logs_lane[expected_bucket])
    assert expected_code in _codes(source_scope[expected_bucket])


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        ("lane_mock", "TWO_NODE_E2E_LOGS_MOCK_EVIDENCE"),
        ("check_mock", "TWO_NODE_E2E_LOGS_MOCK_CHECK"),
        ("lane_historical", "TWO_NODE_E2E_LOGS_HISTORICAL_LATEST"),
        ("check_historical", "TWO_NODE_E2E_LOGS_HISTORICAL_CHECK"),
    ],
)
def test_logs_lane_owner_mock_and_historical_evidence_fail_via_validator(
    mutator: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"logs-{mutator}")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    if mutator == "lane_mock":
        logs_summary["mock"] = True
    elif mutator == "check_mock":
        logs_summary["sources"]["GFS"]["checks"]["job_logs"]["mock"] = True
    elif mutator == "lane_historical":
        logs_summary["historical_latest"] = True
    else:
        logs_summary["sources"]["GFS"]["checks"]["job_logs"]["historical_latest"] = True
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    assert summary["status"] == STATUS_FAIL
    assert logs_lane["status"] == STATUS_FAIL
    assert expected_code in _codes(logs_lane["findings"])


def test_logs_lane_owner_direct_evaluator_preserves_log_uri_safety() -> None:
    run_id = _run_id("logs-owner-direct-uri-safety")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    check = logs_summary["sources"]["GFS"]["checks"]["job_logs"]
    log_uri = "published://logs/gfs/run/job.out?token=SECRET"
    check["log_uri"] = log_uri
    check["evidence"]["response"]["body"]["log_uri"] = log_uri
    _write(config.run_dir / "logs" / "summary.json", logs_summary)
    metadata_result, logs_doc, docker_security_doc = _logs_owner_inputs(config)

    logs_lane = two_node_e2e_logs_lane.evaluate_logs_lane(
        logs_doc,
        declared_sources=metadata_result.scope.declared_sources,
        strict_identities=metadata_result.strict_identities,
        evidence_run_id=run_id,
        docker_security_doc=docker_security_doc,
        helpers=two_node_e2e_evidence._logs_lane_helpers(),
    )

    assert logs_lane.status == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED" in _codes(logs_lane.blockers)
    assert "SECRET" not in json.dumps(logs_lane.to_summary()["blockers"])


def test_logs_lane_owner_direct_evaluator_accepts_typed_unavailable_proof() -> None:
    run_id = _run_id("logs-owner-direct-typed-unavailable")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    for record in logs_summary["sources"].values():
        check = record["checks"]["job_logs"]
        identity = copy.deepcopy(check["identity"])
        check.pop("log_uri", None)
        check.pop("published_log_read", None)
        check["evidence"]["response"] = {
            "status_code": 404,
            "error_code": "JOB_LOG_NOT_PUBLISHED",
            "method": "GET",
            "path": f"/api/v1/jobs/{identity['job_id']}/logs",
            **identity,
        }
    _write(config.run_dir / "logs" / "summary.json", logs_summary)
    metadata_result, logs_doc, docker_security_doc = _logs_owner_inputs(config)

    logs_lane = two_node_e2e_logs_lane.evaluate_logs_lane(
        logs_doc,
        declared_sources=metadata_result.scope.declared_sources,
        strict_identities=metadata_result.strict_identities,
        evidence_run_id=run_id,
        docker_security_doc=docker_security_doc,
        helpers=two_node_e2e_evidence._logs_lane_helpers(),
    )

    assert logs_lane.status == STATUS_PASS
    assert "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_EVIDENCE_MISSING" not in _codes(logs_lane.blockers)


def test_docker_preflight_missing_lane_blocks_with_missing_lane_code() -> None:
    run_id = _run_id("preflight-missing-lane")
    config = _seed_pass_bundle(run_id)
    (config.run_dir / "docker-preflight" / "summary.json").unlink()

    summary = validate_two_node_e2e_evidence(config)

    docker_preflight = summary["lane_summaries"]["docker_preflight"]
    assert summary["status"] == STATUS_BLOCKED
    assert docker_preflight["status"] == STATUS_BLOCKED
    assert docker_preflight["evidence_path"] is None
    assert docker_preflight["evidence_sha256"] is None
    assert docker_preflight["summary_status"] is None
    assert "redacted_evidence" not in docker_preflight
    assert "TWO_NODE_E2E_DOCKER_PREFLIGHT_MISSING" in _codes(docker_preflight["blockers"])


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

    blockers = summary["lane_summaries"]["docker_preflight"]["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert summary["lane_summaries"]["docker_preflight"]["status"] == STATUS_BLOCKED
    if missing_key == "disk":
        assert "TWO_NODE_E2E_DOCKER_PREFLIGHT_DISK_EVIDENCE_MISSING" in _codes(blockers)
    else:
        assert "TWO_NODE_E2E_DOCKER_PREFLIGHT_RESOURCE_EVIDENCE_MISSING" in _codes(blockers)


def test_docker_preflight_unrecognized_schema_blocks_with_schema_code() -> None:
    run_id = _run_id("preflight-schema")
    config = _seed_pass_bundle(run_id)
    preflight = _read(config.run_dir / "docker-preflight" / "summary.json")
    preflight["schema_version"] = "nhms.two_node_docker.preflight.v0"
    _write(config.run_dir / "docker-preflight" / "summary.json", preflight)

    summary = validate_two_node_e2e_evidence(config)

    blockers = summary["lane_summaries"]["docker_preflight"]["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_DOCKER_PREFLIGHT_SCHEMA_UNRECOGNIZED" in _codes(blockers)


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


def test_docker_preflight_failed_command_blocks_with_command_failure_code() -> None:
    run_id = _run_id("preflight-failed-command")
    config = _seed_pass_bundle(run_id)
    preflight = _read(config.run_dir / "docker-preflight" / "summary.json")
    preflight["commands"]["docker_system_df"]["returncode"] = 1
    _write(config.run_dir / "docker-preflight" / "summary.json", preflight)

    summary = validate_two_node_e2e_evidence(config)

    blockers = summary["lane_summaries"]["docker_preflight"]["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_DOCKER_PREFLIGHT_COMMAND_FAILED" in _codes(blockers)
    assert any(blocker.get("command") == "docker_system_df" for blocker in blockers)


def test_docker_preflight_missing_docker_root_dir_blocks_with_root_code() -> None:
    run_id = _run_id("preflight-missing-root-dir")
    config = _seed_pass_bundle(run_id)
    preflight = _read(config.run_dir / "docker-preflight" / "summary.json")
    preflight.pop("docker_root_dir")
    _write(config.run_dir / "docker-preflight" / "summary.json", preflight)

    summary = validate_two_node_e2e_evidence(config)

    blockers = summary["lane_summaries"]["docker_preflight"]["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_DOCKER_ROOT_MISSING" in _codes(blockers)


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


def test_docker_preflight_non_numeric_free_bytes_blocks_with_invalid_disk_code() -> None:
    run_id = _run_id("preflight-invalid-disk")
    config = _seed_pass_bundle(run_id)
    preflight = _read(config.run_dir / "docker-preflight" / "summary.json")
    preflight["disk"]["tmpdir"]["free_bytes"] = "unknown"
    _write(config.run_dir / "docker-preflight" / "summary.json", preflight)

    summary = validate_two_node_e2e_evidence(config)

    blockers = summary["lane_summaries"]["docker_preflight"]["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_DOCKER_PREFLIGHT_DISK_EVIDENCE_INVALID" in _codes(blockers)


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


def test_readonly_db_final_rejects_prefix_source_under_different_parent() -> None:
    run_id = _run_id("db-prefix-parent")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    source_artifacts = db_summary["validation_provenance"]["source_artifacts"]
    gfs_record = next(artifact for artifact in source_artifacts if artifact["sources"] == ["GFS"])
    old_source_dir = Path(gfs_record["source_dir"])
    alternate_root = REPO_ROOT / "artifacts" / "test-two-node-e2e-evidence-alt"
    new_source_dir = alternate_root / f"{run_id}-gfs" / "db" / "readonly-db-boundary"
    for filename in ("summary.json", "role.json", "route_smoke.json", "permission_probes.json"):
        payload = json.loads((old_source_dir / filename).read_text(encoding="utf-8"))
        _write(new_source_dir / filename, payload)
        gfs_record["artifacts"][filename] = _readonly_source_artifact(
            new_source_dir / filename,
            gfs_record["summary_run_id"],
        )
    gfs_record["source_dir"] = str(new_source_dir.resolve(strict=False))
    _write(lane / "summary.json", db_summary)

    summary = validate_two_node_e2e_evidence(config)

    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert summary["status"] == STATUS_BLOCKED
    assert readonly_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_PARENT_ROOT_MISMATCH" in _codes(
        readonly_lane["blockers"]
    )


def test_readonly_db_final_rejects_external_source_child_root_mismatch() -> None:
    run_id = _run_id("db-external-child-root")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    source_artifacts = db_summary["validation_provenance"]["source_artifacts"]
    gfs_record = next(artifact for artifact in source_artifacts if artifact["sources"] == ["GFS"])
    source_dir = Path(gfs_record["source_dir"])
    current_parent = config.run_dir.parent.resolve(strict=False)
    stale_parent = (REPO_ROOT / "artifacts" / "test-two-node-e2e-evidence-alt").resolve(strict=False)
    stale_parent.mkdir(parents=True, exist_ok=True)

    gfs_summary = _read(source_dir / "summary.json")
    gfs_summary["run_id"] = f"{run_id}-external-gfs"
    gfs_summary["validation_provenance"]["parent_evidence_run_id"] = run_id
    gfs_summary["validation_provenance"]["parent_evidence_root"] = str(stale_parent)
    _write(source_dir / "summary.json", gfs_summary)

    gfs_record["summary_run_id"] = gfs_summary["run_id"]
    gfs_record["parent_binding"] = "validation_provenance.parent_evidence_run_id"
    gfs_record["validation_provenance"]["parent_evidence_run_id"] = run_id
    gfs_record["validation_provenance"]["parent_evidence_root"] = str(current_parent)
    for filename in ("summary.json", "role.json", "route_smoke.json", "permission_probes.json"):
        gfs_record["artifacts"][filename] = _readonly_source_artifact(
            source_dir / filename,
            gfs_summary["run_id"],
        )
    _write(lane / "summary.json", db_summary)

    summary = validate_two_node_e2e_evidence(config)

    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert summary["status"] == STATUS_BLOCKED
    assert readonly_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_PARENT_ROOT_MISMATCH" in _codes(
        readonly_lane["blockers"]
    )


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


def test_readonly_db_manual_action_no_write_aliases_are_contractual() -> None:
    run_id = _run_id("db-manual-no-write-alias")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    action = db_summary["manual_action_probes"][0]
    action.pop("write_dependency_constructed", None)
    action.pop("write_executed", None)
    action["db_write_dependency_constructed"] = False
    action["db_write_executed"] = True
    _write(lane / "summary.json", db_summary)

    summary = validate_two_node_e2e_evidence(config)

    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert summary["status"] == STATUS_BLOCKED
    assert readonly_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_READONLY_DB_MANUAL_ACTION_WRITE_PROOF_FAILED" in _codes(readonly_lane["blockers"])


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


@pytest.mark.parametrize(
    ("display_cycle_time", "expected_status"),
    [
        (COMPACT_CYCLE_TIME, STATUS_PASS),
        (ISO_OFFSET_CYCLE_TIME, STATUS_PASS),
        (SHIFTED_CYCLE_TIME, STATUS_FAIL),
    ],
)
def test_readonly_db_route_display_identity_cycle_time_uses_timestamp_semantics(
    display_cycle_time: str,
    expected_status: str,
) -> None:
    run_id = _run_id(f"db-route-display-cycle-{expected_status.lower()}")
    config = _seed_pass_bundle(run_id)
    lane = config.run_dir / "db" / "readonly-db-boundary"
    db_summary = _read(lane / "summary.json")
    db_summary["display_identity"]["GFS"]["cycle_time"] = display_cycle_time
    _write(lane / "summary.json", db_summary)

    summary = validate_two_node_e2e_evidence(config)

    readonly_lane = summary["lane_summaries"]["readonly_db"]
    assert summary["status"] == expected_status
    assert readonly_lane["status"] == expected_status
    if expected_status == STATUS_FAIL:
        assert "TWO_NODE_E2E_READONLY_DB_ROUTE_DISPLAY_IDENTITY_MISMATCH" in _codes(readonly_lane["findings"])


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
    ("metadata_cycle_time", "expected_status"),
    [
        (COMPACT_CYCLE_TIME, STATUS_PASS),
        (ISO_OFFSET_CYCLE_TIME, STATUS_PASS),
        (SHIFTED_CYCLE_TIME, STATUS_FAIL),
    ],
)
def test_final_evidence_strict_identity_cycle_time_uses_timestamp_semantics(
    metadata_cycle_time: str,
    expected_status: str,
) -> None:
    run_id = _run_id(f"strict-cycle-{expected_status.lower()}")
    config = _seed_pass_bundle(run_id)
    metadata = _read(config.run_dir / "run.json")
    metadata["strict_identities"]["GFS"]["cycle_time"] = metadata_cycle_time
    _write(config.run_dir / "run.json", metadata)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == expected_status
    if expected_status == STATUS_PASS:
        assert summary["lane_summaries"]["readonly_db"]["status"] == STATUS_PASS
        assert summary["lane_summaries"]["api"]["status"] == STATUS_PASS
        assert summary["lane_summaries"]["browser"]["status"] == STATUS_PASS
        assert summary["lane_summaries"]["logs"]["status"] == STATUS_PASS
        assert summary["lane_summaries"]["cross_plane"]["status"] == STATUS_PASS
        assert summary["lane_summaries"]["manual_ops"]["status"] == STATUS_PASS
    else:
        mismatch_codes = set()
        for lane_name in ("api", "browser", "logs", "cross_plane", "manual_ops"):
            mismatch_codes.update(_codes(summary["lane_summaries"][lane_name]["findings"]))
        assert "TWO_NODE_E2E_STRICT_IDENTITY_MISMATCH" in mismatch_codes


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


def test_browser_source_lane_partial_with_strict_identity_blocker_yields_final_blocked() -> None:
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


@pytest.mark.parametrize(
    ("lane_dir", "lane_name", "path_to_check"),
    [
        ("api", "api", ("sources", "GFS", "checks", "latest_product")),
        ("browser", "browser", ("sources", "GFS", "checks", "ops_job_logs")),
        ("logs", "logs", ("sources", "GFS", "checks", "job_logs")),
        ("cross-plane", "cross_plane", ("sources", "GFS")),
        ("slurm", "slurm", ()),
    ],
)
def test_nested_stale_current_run_producer_evidence_blocks(
    lane_dir: str,
    lane_name: str,
    path_to_check: tuple[str, ...],
) -> None:
    run_id = _run_id(f"{lane_name}-nested-stale")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_dir / "summary.json")
    target = payload
    for key in path_to_check:
        target = target[key]
    target.setdefault("evidence", {})["bundle_run_id"] = "older-bundle"
    _write(config.run_dir / lane_dir / "summary.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_name]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH" in _codes(lane["blockers"])


def test_nested_source_artifact_with_stale_path_hash_blocks() -> None:
    old_run_id = _run_id("old-source-artifact")
    old_config = _seed_pass_bundle(old_run_id)
    old_artifact = old_config.run_dir / "api" / "old-producer.json"
    _write(old_artifact, {"status": STATUS_PASS, "evidence_run_id": old_run_id})

    run_id = _run_id("new-source-artifact")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    latest = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    latest["source_artifacts"] = [
        {
            "path": str(old_artifact.resolve(strict=False)),
            "sha256": _sha256_file(old_artifact),
        }
    ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_STALE_OR_UNSCOPED" in _codes(api_lane["blockers"])


@pytest.mark.parametrize(
    ("lane_dir", "lane_name", "live_flag"),
    [
        ("api", "api", "live_api_evidence"),
        ("browser", "browser", "live_browser_evidence"),
        ("logs", "logs", "live_log_evidence"),
    ],
)
def test_source_lane_required_checks_need_check_scoped_producer_evidence(
    lane_dir: str,
    lane_name: str,
    live_flag: str,
) -> None:
    run_id = _run_id(f"{lane_name}-check-scope")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_dir / "summary.json")
    payload["commands"] = {"unrelated_probe": {"returncode": 0}}
    payload[live_flag] = True
    for source in payload["sources"].values():
        for check in source["checks"].values():
            status = check["status"]
            identity = check["identity"]
            check.clear()
            check.update({"status": status, "identity": identity})
    _write(config.run_dir / lane_dir / "summary.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_name]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert f"TWO_NODE_E2E_{lane_name.upper()}_CHECK_PRODUCER_EVIDENCE_MISSING" in _codes(lane["blockers"])


@pytest.mark.parametrize(
    ("lane_dir", "lane_name", "check_name", "missing_fields"),
    [
        ("api", "api", "latest_product", ("run_id", "cycle_time", "model_id")),
        ("browser", "browser", "ops_job_logs", ("run_id", "cycle_time", "model_id", "job_id")),
    ],
)
def test_source_lane_producer_evidence_requires_complete_strict_identity(
    lane_dir: str,
    lane_name: str,
    check_name: str,
    missing_fields: tuple[str, ...],
) -> None:
    run_id = _run_id(f"{lane_name}-producer-unscoped")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_dir / "summary.json")
    check = payload["sources"]["GFS"]["checks"][check_name]
    check["evidence"] = {
        "evidence_run_id": run_id,
        "request": {
            "method": "GET",
            "path": f"/producer/gfs/{check_name}",
            "source": "GFS",
            "check": check_name,
        },
        "response": {
            "status_code": 200,
            "source": "GFS",
            "check": check_name,
        },
    }
    _write(config.run_dir / lane_dir / "summary.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_name]
    blockers = lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert f"TWO_NODE_E2E_{lane_name.upper()}_CHECK_PRODUCER_IDENTITY_UNSCOPED" in _codes(blockers)
    assert any(
        "unscoped for the required check identity" in str(blocker.get("message", ""))
        and set(missing_fields).issubset(set(blocker.get("missing_fields", []) or blocker.get("required_fields", [])))
        for blocker in blockers
    )


def test_api_latest_product_lineage_does_not_satisfy_producer_identity() -> None:
    run_id = _run_id("api-lineage-unscoped")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check["evidence"] = {
        "request": {
            "method": "GET",
            "path": "/mvp/qhh/latest-product",
        },
        "response": {
            "status_code": 200,
            "lineage": _producer_lineage(identity, "latest_product"),
        },
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_UNSCOPED" in _codes(api_lane["blockers"])


def test_browser_ops_job_logs_lineage_does_not_satisfy_producer_identity() -> None:
    run_id = _run_id("browser-lineage-unscoped")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    check = browser_summary["sources"]["GFS"]["checks"]["ops_job_logs"]
    identity = check["identity"]
    check["evidence"] = {
        "request": {
            "method": "GET",
            "path": f"/api/v1/jobs/{identity['job_id']}/logs",
        },
        "response": {
            "status_code": 200,
            "lineage": _producer_lineage(identity, "ops_job_logs", include_job_id=True),
        },
    }
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    assert summary["status"] == STATUS_BLOCKED
    assert browser_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_BROWSER_CHECK_PRODUCER_IDENTITY_UNSCOPED" in _codes(browser_lane["blockers"])


def test_api_latest_product_nested_identity_satisfies_producer_identity() -> None:
    run_id = _run_id("api-nested-identity-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = _producer_lineage(check["identity"], "latest_product")
    check["evidence"] = {
        "request": {
            "method": "GET",
            "path": "/mvp/qhh/latest-product",
        },
        "response": {
            "status_code": 200,
            "identity": identity,
        },
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["api"]["status"] == STATUS_PASS


def test_api_latest_product_authoritative_nested_request_response_satisfies_producer_identity() -> None:
    run_id = _run_id("api-authoritative-request-response-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check["evidence"] = {
        "transport": {
            "request": {
                "method": "GET",
                "path": "/mvp/qhh/latest-product",
                "source": identity["source"],
                "check": "latest_product",
                "run_id": identity["run_id"],
                "cycle_time": identity["cycle_time"],
                "model_id": identity["model_id"],
            },
            "response": {
                "status_code": 200,
                "source": identity["source"],
                "check": "latest_product",
                "run_id": identity["run_id"],
                "cycle_time": identity["cycle_time"],
                "model_id": identity["model_id"],
            },
        }
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["api"]["status"] == STATUS_PASS


def test_api_latest_product_explicit_list_item_satisfies_producer_identity() -> None:
    run_id = _run_id("api-list-item-proof-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    check["proofs"] = [
        _complete_explicit_producer_record(identity, "latest_product"),
    ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["api"]["status"] == STATUS_PASS


def test_source_scoped_producer_evidence_lineage_only_strict_fields_blocks() -> None:
    run_id = _run_id("source-lineage-unscoped")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    identity = latest["identity"]
    latest.pop("evidence", None)
    source_record["evidence"] = {
        "request": {
            "method": "GET",
            "path": "/mvp/qhh/latest-product",
            "source": "GFS",
            "check": "latest_product",
        },
        "response": {
            "status_code": 200,
            "source": "GFS",
            "check": "latest_product",
            "lineage": {
                "run_id": identity["run_id"],
                "cycle_time": identity["cycle_time"],
                "model_id": identity["model_id"],
            },
        },
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_UNSCOPED" in _codes(api_lane["blockers"])


def test_api_latest_product_hidden_text_identity_does_not_satisfy_producer_identity() -> None:
    run_id = _run_id("api-hidden-text-unscoped")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    query = _producer_identity_query(identity, "latest_product")
    request = check["evidence"]["request"]
    response = check["evidence"]["response"]
    request["path"] = f"/producer/gfs/latest_product?{query}"
    request["query"] = query
    request["body"] = {
        "status_code": 200,
        "source": identity["source"],
        "check": "latest_product",
        "run_id": identity["run_id"],
        "cycle_time": identity["cycle_time"],
        "model_id": identity["model_id"],
    }
    _remove_explicit_producer_identity_fields(
        (request, response),
        ("run_id", "cycle_time", "model_id"),
    )
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_UNSCOPED" in _codes(blockers)


def test_browser_ops_job_logs_hidden_text_identity_does_not_satisfy_producer_identity() -> None:
    run_id = _run_id("browser-hidden-log-unscoped")
    config = _seed_pass_bundle(run_id)
    browser_summary = _read(config.run_dir / "browser" / "summary.json")
    check = browser_summary["sources"]["GFS"]["checks"]["ops_job_logs"]
    identity = check["identity"]
    query = _producer_identity_query(identity, "ops_job_logs", include_job_id=True)
    request = check["evidence"]["request"]
    response = check["evidence"]["response"]
    request["path"] = f"/producer/gfs/ops_job_logs?{query}"
    request["query"] = query
    response["body"] = {
        "status_code": 200,
        "source": identity["source"],
        "check": "ops_job_logs",
        "run_id": identity["run_id"],
        "cycle_time": identity["cycle_time"],
        "model_id": identity["model_id"],
        "job_id": identity["job_id"],
        "log_uri": f"published://logs/gfs/{identity['run_id']}/{identity['job_id']}.out",
    }
    _remove_explicit_producer_identity_fields(
        (request, response),
        ("run_id", "cycle_time", "model_id", "job_id"),
    )
    _write(config.run_dir / "browser" / "summary.json", browser_summary)

    summary = validate_two_node_e2e_evidence(config)

    browser_lane = summary["lane_summaries"]["browser"]
    blockers = browser_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert browser_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_BROWSER_CHECK_PRODUCER_IDENTITY_UNSCOPED" in _codes(blockers)


def test_source_scoped_producer_evidence_requires_complete_required_check_identity() -> None:
    run_id = _run_id("source-artifact-unscoped")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    latest = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = latest["identity"]
    query = _producer_identity_query(identity, "latest_product")
    latest.pop("evidence", None)
    api_summary["sources"]["GFS"]["evidence"] = {
        "request": {
            "method": "GET",
            "path": f"/producer/gfs/latest_product?{query}",
            "query": query,
            "source": "GFS",
            "check": "latest_product",
            "body": {
                "status_code": 200,
                "source": identity["source"],
                "check": "latest_product",
                "run_id": identity["run_id"],
                "cycle_time": identity["cycle_time"],
                "model_id": identity["model_id"],
            },
        },
        "response": {"status_code": 200, "source": "GFS", "check": "latest_product"},
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_UNSCOPED" in _codes(blockers)
    assert any("required check identity" in str(blocker.get("message", "")) for blocker in blockers)


def test_complete_explicit_producer_record_still_reports_sibling_hidden_text_conflict() -> None:
    run_id = _run_id("api-sibling-hidden-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    sibling_query = (
        f"source=IFS&check=latest_product&run_id={identity['run_id']}"
        f"&cycle_time={identity['cycle_time']}&model_id={identity['model_id']}"
    )
    check["evidence"]["request"]["body"] = {
        "status_code": 200,
        "source": "IFS",
        "check": "latest_product",
        "run_id": identity["run_id"],
        "cycle_time": identity["cycle_time"],
        "model_id": identity["model_id"],
        "path": f"/producer/ifs/latest_product?{sibling_query}",
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(blocker.get("field") == "source" for blocker in blockers)


def test_api_check_proofs_hidden_only_body_sibling_conflict_blocks() -> None:
    run_id = _run_id("api-proofs-hidden-body-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check["proofs"] = [
        _complete_explicit_producer_record(identity, "latest_product"),
        {
            "body": {
                "status_code": 200,
                "source": "IFS",
                "check": "latest_product",
                "run_id": identity["run_id"],
                "cycle_time": identity["cycle_time"],
                "model_id": identity["model_id"],
            }
        },
    ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == "body"
        for blocker in blockers
    )


def test_api_check_proofs_hidden_only_text_sibling_conflict_blocks() -> None:
    run_id = _run_id("api-proofs-hidden-text-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check["proofs"] = [
        _complete_explicit_producer_record(identity, "latest_product"),
        {
            "text": _producer_identity_text(
                identity,
                "latest_product",
                source="IFS",
            )
        },
    ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == "text"
        for blocker in blockers
    )


def test_api_check_unstructured_evidence_root_hidden_conflict_blocks_with_complete_proof() -> None:
    run_id = _run_id("api-unstructured-root-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check["proofs"] = [_complete_explicit_producer_record(identity, "latest_product")]
    check["evidence"] = {
        "message": _producer_identity_text(identity, "latest_product", source="IFS"),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == "message"
        for blocker in blockers
    )


def test_api_check_scalar_evidence_root_hidden_conflict_blocks_with_complete_proof() -> None:
    run_id = _run_id("api-scalar-evidence-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check["proofs"] = [_complete_explicit_producer_record(identity, "latest_product")]
    check["evidence"] = _producer_identity_text(identity, "latest_product", source="IFS")
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == "evidence"
        for blocker in blockers
    )


@pytest.mark.parametrize("root_key", ["source_artifacts", "commands"])
@pytest.mark.parametrize("root_shape", ["scalar", "list"])
def test_api_check_scalar_or_list_producer_root_hidden_conflict_blocks_with_complete_proof(
    root_key: str,
    root_shape: str,
) -> None:
    run_id = _run_id(f"api-{root_key}-{root_shape}-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    conflict_text = _producer_identity_text(identity, "latest_product", source="IFS")
    check["proofs"] = [_complete_explicit_producer_record(identity, "latest_product")]
    check[root_key] = conflict_text if root_shape == "scalar" else ["collector ok", conflict_text]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == root_key
        for blocker in blockers
    )


def test_api_check_scalar_evidence_root_does_not_satisfy_producer_identity() -> None:
    run_id = _run_id("api-scalar-evidence-unscoped")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("proofs", None)
    check["evidence"] = _producer_identity_text(identity, "latest_product")
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_EVIDENCE_MISSING" in _codes(api_lane["blockers"])


def test_api_check_scalar_text_for_other_check_does_not_block_target_with_complete_proof() -> None:
    run_id = _run_id("api-scalar-other-check-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check["proofs"] = [_complete_explicit_producer_record(identity, "latest_product")]
    check["evidence"] = _producer_identity_text(identity, "series", source="IFS")
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


@pytest.mark.parametrize("root_key", ["proofs", "evidence", "commands", "source_artifacts"])
@pytest.mark.parametrize("text_key", ["message", "text", "details", "body"])
def test_api_latest_product_check_producer_root_nested_other_check_text_does_not_block_target(
    root_key: str,
    text_key: str,
) -> None:
    run_id = _run_id(f"api-check-root-{root_key}-{text_key}-other-check-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    series_identity = source_record["checks"]["series"]["identity"]
    sibling_run_id = f"{series_identity['run_id']}-sibling"
    target_proof = _complete_explicit_producer_record(identity, "latest_product")
    sibling_text = {
        text_key: _producer_identity_text(
            series_identity,
            "series",
            source="IFS",
            run_id=sibling_run_id,
        )
    }
    check["proofs"] = [target_proof]
    check[root_key] = [target_proof, sibling_text] if root_key == "proofs" else [sibling_text]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


@pytest.mark.parametrize("root_key", ["proofs", "evidence", "commands", "source_artifacts"])
@pytest.mark.parametrize("text_key", ["message", "text", "details", "body"])
def test_api_latest_product_check_producer_root_nested_target_text_conflict_blocks(
    root_key: str,
    text_key: str,
) -> None:
    run_id = _run_id(f"api-check-root-{root_key}-{text_key}-target-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    target_proof = _complete_explicit_producer_record(identity, "latest_product")
    sibling_conflict = {
        text_key: _producer_identity_text(
            identity,
            "latest_product",
            source="IFS",
        )
    }
    check["proofs"] = [target_proof]
    check[root_key] = [target_proof, sibling_conflict] if root_key == "proofs" else [sibling_conflict]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source"
        and blocker.get("observed") == "IFS"
        and blocker.get("evidence_key") == text_key
        for blocker in blockers
    )


@pytest.mark.parametrize("root_key", ["evidence", "source_artifacts"])
@pytest.mark.parametrize("wrapper_key", NON_AUTHORITATIVE_WRAPPER_KEYS)
@pytest.mark.parametrize("wrapper_shape", ["scalar", "list"])
def test_api_check_non_authoritative_wrapper_scalar_text_conflict_blocks_with_complete_proof(
    root_key: str,
    wrapper_key: str,
    wrapper_shape: str,
) -> None:
    run_id = _run_id(f"api-{root_key}-{wrapper_key}-{wrapper_shape}-text-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    conflict_text = _producer_identity_text(identity, "latest_product", source="IFS")
    check["proofs"] = [_complete_explicit_producer_record(identity, "latest_product")]
    check[root_key] = {
        wrapper_key: conflict_text if wrapper_shape == "scalar" else ["collector ok", conflict_text],
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == wrapper_key
        for blocker in blockers
    )


def test_api_check_non_authoritative_wrapper_scalar_other_check_does_not_block_target() -> None:
    run_id = _run_id("api-wrapper-scalar-other-check-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check["proofs"] = [_complete_explicit_producer_record(identity, "latest_product")]
    check["evidence"] = {
        "metadata": _producer_identity_text(identity, "series", source="IFS", run_id=f"{identity['run_id']}-sibling"),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


def test_api_check_top_level_identity_only_metadata_conflict_blocks_with_complete_proof() -> None:
    run_id = _run_id("api-top-metadata-identity-only-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check["proofs"] = [_complete_explicit_producer_record(identity, "latest_product")]
    check["metadata"] = _identity_only_producer_record(identity, "latest_product", source="IFS")
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source"
        and blocker.get("observed") == "IFS"
        and blocker.get("evidence_key") == "metadata"
        for blocker in blockers
    )


@pytest.mark.parametrize(
    ("root_key", "wrapper_key", "identity_key"),
    [
        ("evidence", "metadata", "identity"),
        ("proofs", "wrapper", "strict_identity"),
    ],
)
def test_api_check_producer_root_identity_only_wrapper_conflict_blocks_with_complete_proof(
    root_key: str,
    wrapper_key: str,
    identity_key: str,
) -> None:
    run_id = _run_id(f"api-root-{root_key}-{wrapper_key}-identity-only-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    explicit_proof_root = "proofs" if root_key == "evidence" else "evidence"
    check[explicit_proof_root] = [_complete_explicit_producer_record(identity, "latest_product")]
    check[root_key] = {
        wrapper_key: {
            identity_key: _identity_only_producer_record(identity, "latest_product", source="IFS"),
        },
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source"
        and blocker.get("observed") == "IFS"
        and blocker.get("evidence_key") == wrapper_key
        for blocker in blockers
    )


@pytest.mark.parametrize(
    ("surface", "source_key"),
    [
        ("arbitrary_container", "transport"),
        ("source_keyed_root", "proofs"),
    ],
)
def test_source_scoped_identity_only_metadata_conflict_blocks_with_complete_proof(
    surface: str,
    source_key: str,
) -> None:
    run_id = _run_id(f"source-{surface}-identity-only-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    identity = latest["identity"]
    latest.pop("evidence", None)
    proof = _complete_explicit_producer_record(identity, "latest_product")
    conflict = _identity_only_producer_record(identity, "latest_product", source="IFS")
    if surface == "arbitrary_container":
        source_record["proofs"] = {"latest_product": proof}
        source_record[source_key] = {
            "latest_product": {"status": "context"},
            "metadata": conflict,
        }
    else:
        source_record[source_key] = {
            "latest_product": proof,
            "metadata": conflict,
        }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(blocker.get("field") == "source" and blocker.get("observed") == "IFS" for blocker in blockers)


def test_api_check_source_artifacts_identity_only_metadata_conflict_blocks_without_artifact_proof() -> None:
    run_id = _run_id("api-source-artifacts-identity-only-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check["proofs"] = [_complete_explicit_producer_record(identity, "latest_product")]
    check["source_artifacts"] = {
        "metadata": _identity_only_producer_record(identity, "latest_product", source="IFS"),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(blocker.get("field") == "source" and blocker.get("observed") == "IFS" for blocker in blockers)


@pytest.mark.parametrize("surface", ["check_metadata", "source_keyed_root"])
def test_identity_only_metadata_naming_other_check_does_not_block_target(surface: str) -> None:
    run_id = _run_id(f"identity-only-other-check-{surface}")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    conflict = _identity_only_producer_record(
        identity,
        "hydro_met",
        source="IFS",
        run_id=f"{identity['run_id']}-sibling",
    )
    if surface == "check_metadata":
        check["proofs"] = [_complete_explicit_producer_record(identity, "latest_product")]
        check["metadata"] = conflict
    else:
        check.pop("evidence", None)
        source_record["proofs"] = {
            "latest_product": _complete_explicit_producer_record(identity, "latest_product"),
            "metadata": conflict,
        }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


def test_logs_check_identity_only_metadata_job_id_conflict_blocks_with_complete_proof() -> None:
    run_id = _run_id("logs-identity-only-job-conflict")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    check = logs_summary["sources"]["GFS"]["checks"]["job_logs"]
    identity = check["identity"]
    observed_job_id = f"{identity['job_id']}-sibling"
    check["proofs"] = [_complete_explicit_producer_record(identity, "job_logs")]
    check["metadata"] = _identity_only_producer_record(identity, "job_logs", job_id=observed_job_id)
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "job_id"
        and blocker.get("observed") == observed_job_id
        and blocker.get("evidence_key") == "metadata"
        for blocker in blockers
    )


@pytest.mark.parametrize("root_key", ["proofs", "evidence", "artifacts", "commands"])
@pytest.mark.parametrize("wrapper_key", ["metadata", "wrapper", "collector"])
def test_api_check_non_authoritative_wrapper_record_does_not_satisfy_producer_identity(
    root_key: str,
    wrapper_key: str,
) -> None:
    run_id = _run_id(f"api-{root_key}-{wrapper_key}-wrapper-unscoped")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    check[root_key] = {
        wrapper_key: _complete_explicit_producer_record(identity, "latest_product"),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_UNSCOPED" in _codes(api_lane["blockers"])


@pytest.mark.parametrize("root_key", ["proofs", "evidence", "artifacts", "commands"])
@pytest.mark.parametrize("wrapper_key", ["metadata", "wrapper", "collector"])
def test_api_check_non_authoritative_wrapper_record_conflict_still_blocks_with_explicit_proof(
    root_key: str,
    wrapper_key: str,
) -> None:
    run_id = _run_id(f"api-{root_key}-{wrapper_key}-wrapper-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    explicit_proof_root = "evidence" if root_key == "proofs" else "proofs"
    check[explicit_proof_root] = [_complete_explicit_producer_record(identity, "latest_product")]
    check[root_key] = {
        wrapper_key: {
            **_complete_explicit_producer_record(identity, "latest_product"),
            "source": "IFS",
        },
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(blocker.get("field") == "source" and blocker.get("observed") == "IFS" for blocker in blockers)


@pytest.mark.parametrize("wrapper_key", ["metadata", "wrapper", "collector", "context"])
@pytest.mark.parametrize("field", ["source", "run_id", "cycle_time", "model_id"])
def test_api_check_source_artifacts_non_authoritative_structured_conflict_blocks(
    wrapper_key: str,
    field: str,
) -> None:
    run_id = _run_id(f"api-source-artifacts-{wrapper_key}-{field}-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    observed = "IFS" if field == "source" else f"{identity[field]}-sibling"
    check.pop("evidence", None)
    check["proofs"] = [_structured_producer_metadata_record(identity, "latest_product")]
    check["source_artifacts"] = {
        wrapper_key: _structured_producer_metadata_record(identity, "latest_product", **{field: observed}),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == field and blocker.get("observed") == observed
        for blocker in blockers
    )


def test_logs_check_source_artifacts_non_authoritative_structured_job_id_conflict_blocks() -> None:
    run_id = _run_id("logs-source-artifacts-structured-job-conflict")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    check = logs_summary["sources"]["GFS"]["checks"]["job_logs"]
    identity = check["identity"]
    observed_job_id = f"{identity['job_id']}-sibling"
    check["proofs"] = [_structured_producer_metadata_record(identity, "job_logs")]
    check["source_artifacts"] = {
        "metadata": _structured_producer_metadata_record(
            identity,
            "job_logs",
            job_id=observed_job_id,
        ),
    }
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "job_id" and blocker.get("observed") == observed_job_id
        for blocker in blockers
    )


def test_api_check_source_artifacts_structured_metadata_other_check_does_not_block_target() -> None:
    run_id = _run_id("api-source-artifacts-structured-other-check-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    series = source_record["checks"]["series"]
    latest_identity = latest["identity"]
    series_identity = series["identity"]
    latest.pop("evidence", None)
    latest["proofs"] = [_structured_producer_metadata_record(latest_identity, "latest_product")]
    latest["source_artifacts"] = {
        "metadata": _structured_producer_metadata_record(
            series_identity,
            "series",
            source="IFS",
            run_id=f"{series_identity['run_id']}-sibling",
        ),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


@pytest.mark.parametrize("wrapper_key", ["metadata", "wrapper", "collector", "context"])
def test_api_check_source_artifacts_non_authoritative_structured_record_alone_is_unscoped(
    wrapper_key: str,
) -> None:
    run_id = _run_id(f"api-source-artifacts-{wrapper_key}-alone-unscoped")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    check["source_artifacts"] = {
        wrapper_key: _structured_producer_metadata_record(identity, "latest_product"),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_UNSCOPED" in _codes(api_lane["blockers"])


@pytest.mark.parametrize("wrapper_key", ["metadata", "wrapper", "collector"])
@pytest.mark.parametrize("field", ["source", "run_id", "cycle_time", "model_id"])
def test_api_check_top_level_non_authoritative_structured_conflict_blocks_with_complete_proof(
    wrapper_key: str,
    field: str,
) -> None:
    run_id = _run_id(f"api-check-top-{wrapper_key}-{field}-structured-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    observed = "IFS" if field == "source" else f"{identity[field]}-sibling"
    check["proofs"] = [_structured_producer_metadata_record(identity, "latest_product")]
    check[wrapper_key] = _structured_producer_metadata_record(
        identity,
        "latest_product",
        **{field: observed},
    )
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == field
        and blocker.get("observed") == observed
        and blocker.get("evidence_key") == wrapper_key
        for blocker in blockers
    )


@pytest.mark.parametrize("wrapper_key", ["metadata", "wrapper"])
@pytest.mark.parametrize("field", ["source", "run_id", "cycle_time", "model_id"])
def test_api_source_top_level_non_authoritative_structured_conflict_blocks_with_source_scoped_proof(
    wrapper_key: str,
    field: str,
) -> None:
    run_id = _run_id(f"api-source-top-{wrapper_key}-{field}-structured-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    observed = "IFS" if field == "source" else f"{identity[field]}-sibling"
    check.pop("evidence", None)
    source_record["proofs"] = {
        "latest_product": _structured_producer_metadata_record(identity, "latest_product"),
    }
    source_record[wrapper_key] = _structured_producer_metadata_record(
        identity,
        "latest_product",
        **{field: observed},
    )
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == field
        and blocker.get("observed") == observed
        and blocker.get("evidence_key") == wrapper_key
        for blocker in blockers
    )


@pytest.mark.parametrize("wrapper_shape", ["mapping", "list"])
def test_api_check_nested_non_authoritative_structured_conflict_blocks_with_complete_proof(
    wrapper_shape: str,
) -> None:
    run_id = _run_id(f"api-check-nested-{wrapper_shape}-structured-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check["proofs"] = [_structured_producer_metadata_record(identity, "latest_product")]
    conflict = _structured_producer_metadata_record(identity, "latest_product", source="IFS")
    if wrapper_shape == "mapping":
        wrapper_key = "metadata"
        check["transport"] = {wrapper_key: conflict}
    else:
        wrapper_key = "wrapper"
        check["transport"] = [{wrapper_key: conflict}]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source"
        and blocker.get("observed") == "IFS"
        and blocker.get("evidence_key") == wrapper_key
        for blocker in blockers
    )


def test_api_source_nested_non_authoritative_structured_conflict_blocks_with_source_scoped_proof() -> None:
    run_id = _run_id("api-source-nested-structured-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    source_record["proofs"] = {
        "latest_product": _structured_producer_metadata_record(identity, "latest_product"),
    }
    source_record["transport"] = {
        "metadata": _structured_producer_metadata_record(identity, "latest_product", source="IFS"),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source"
        and blocker.get("observed") == "IFS"
        and blocker.get("evidence_key") == "metadata"
        for blocker in blockers
    )


def test_api_source_arbitrary_container_check_sibling_structured_metadata_conflict_blocks() -> None:
    run_id = _run_id("api-source-arbitrary-transport-structured-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    source_record["proofs"] = {
        "latest_product": _structured_producer_metadata_record(identity, "latest_product"),
    }
    source_record["transport"] = {
        "latest_product": {"status": "context"},
        "metadata": _structured_producer_metadata_record(identity, "latest_product", source="IFS"),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source"
        and blocker.get("observed") == "IFS"
        and blocker.get("evidence_key") == "metadata"
        for blocker in blockers
    )


@pytest.mark.parametrize("evidence_key", ["metadata", "message"])
def test_api_source_arbitrary_container_check_sibling_scalar_text_conflict_blocks(
    evidence_key: str,
) -> None:
    run_id = _run_id(f"api-source-arbitrary-transport-{evidence_key}-text-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    source_record["proofs"] = {
        "latest_product": _structured_producer_metadata_record(identity, "latest_product"),
    }
    source_record["transport"] = {
        "latest_product": {"status": "context"},
        evidence_key: _producer_identity_text(identity, "latest_product", source="IFS"),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == evidence_key
        for blocker in blockers
    )


@pytest.mark.parametrize("evidence_key", ["metadata", "message"])
def test_api_source_arbitrary_container_check_sibling_other_check_text_does_not_block_target(
    evidence_key: str,
) -> None:
    run_id = _run_id(f"api-source-arbitrary-transport-{evidence_key}-series-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    latest_identity = latest["identity"]
    latest.pop("evidence", None)
    source_record["proofs"] = {
        "latest_product": _structured_producer_metadata_record(latest_identity, "latest_product"),
    }
    source_record["transport"] = {
        "latest_product": {"status": "context"},
        evidence_key: _producer_identity_text(
            latest_identity,
            "hydro_met",
            source="IFS",
            run_id=f"{latest_identity['run_id']}-sibling",
        ),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


def test_api_check_nested_non_authoritative_structured_other_check_does_not_block_target() -> None:
    run_id = _run_id("api-check-nested-structured-other-check-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    series = source_record["checks"]["series"]
    latest_identity = latest["identity"]
    series_identity = series["identity"]
    latest["proofs"] = [_structured_producer_metadata_record(latest_identity, "latest_product")]
    latest["transport"] = {
        "metadata": _structured_producer_metadata_record(
            series_identity,
            "series",
            source="IFS",
            run_id=f"{series_identity['run_id']}-sibling",
        ),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


def test_logs_check_nested_non_authoritative_structured_job_id_conflict_blocks_with_complete_proof() -> None:
    run_id = _run_id("logs-check-nested-structured-job-conflict")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    check = logs_summary["sources"]["GFS"]["checks"]["job_logs"]
    identity = check["identity"]
    observed_job_id = f"{identity['job_id']}-sibling"
    check["proofs"] = [_structured_producer_metadata_record(identity, "job_logs")]
    check["transport"] = {
        "metadata": _structured_producer_metadata_record(
            identity,
            "job_logs",
            job_id=observed_job_id,
        ),
    }
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "job_id"
        and blocker.get("observed") == observed_job_id
        and blocker.get("evidence_key") == "metadata"
        for blocker in blockers
    )


@pytest.mark.parametrize("surface", ["check", "source_record"])
@pytest.mark.parametrize("wrapper_key", ["metadata", "wrapper", "collector"])
def test_api_top_level_non_authoritative_structured_other_check_does_not_block_target(
    surface: str,
    wrapper_key: str,
) -> None:
    run_id = _run_id(f"api-{surface}-top-{wrapper_key}-series-structured-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    latest_identity = latest["identity"]
    latest.pop("evidence", None)
    if surface == "check":
        latest["proofs"] = [_structured_producer_metadata_record(latest_identity, "latest_product")]
        latest[wrapper_key] = _structured_producer_metadata_record(
            latest_identity,
            "hydro_met",
            source="IFS",
            run_id=f"{latest_identity['run_id']}-sibling",
        )
    else:
        source_record["proofs"] = {
            "latest_product": _structured_producer_metadata_record(latest_identity, "latest_product"),
        }
        source_record[wrapper_key] = _structured_producer_metadata_record(
            latest_identity,
            "hydro_met",
            source="IFS",
            run_id=f"{latest_identity['run_id']}-sibling",
        )
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


@pytest.mark.parametrize("surface", ["check", "source_record"])
@pytest.mark.parametrize("wrapper_key", ["metadata", "wrapper"])
def test_logs_top_level_non_authoritative_structured_job_id_conflict_blocks(
    surface: str,
    wrapper_key: str,
) -> None:
    run_id = _run_id(f"logs-{surface}-top-{wrapper_key}-job-structured-conflict")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    source_record = logs_summary["sources"]["GFS"]
    check = source_record["checks"]["job_logs"]
    identity = check["identity"]
    observed_job_id = f"{identity['job_id']}-sibling"
    if surface == "check":
        check["proofs"] = [_structured_producer_metadata_record(identity, "job_logs")]
        check[wrapper_key] = _structured_producer_metadata_record(
            identity,
            "job_logs",
            job_id=observed_job_id,
        )
    else:
        check.pop("evidence", None)
        source_record["proofs"] = {
            "job_logs": _structured_producer_metadata_record(identity, "job_logs"),
        }
        source_record[wrapper_key] = _structured_producer_metadata_record(
            identity,
            "job_logs",
            job_id=observed_job_id,
        )
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "job_id"
        and blocker.get("observed") == observed_job_id
        and blocker.get("evidence_key") == wrapper_key
        for blocker in blockers
    )


@pytest.mark.parametrize("surface", ["check", "source_record"])
def test_api_arbitrary_container_structured_conflict_blocks_with_complete_proof(surface: str) -> None:
    run_id = _run_id(f"api-{surface}-arbitrary-transport-context-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    identity = latest["identity"]
    conflict = _structured_producer_metadata_record(identity, "latest_product", source="IFS")
    if surface == "check":
        latest["proofs"] = [_structured_producer_metadata_record(identity, "latest_product")]
        latest["transport_context"] = conflict
    else:
        latest.pop("evidence", None)
        source_record["proofs"] = {
            "latest_product": _structured_producer_metadata_record(identity, "latest_product"),
        }
        source_record["transport_context"] = conflict
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source"
        and blocker.get("observed") == "IFS"
        and blocker.get("evidence_key") == "transport_context"
        for blocker in blockers
    )


@pytest.mark.parametrize("surface", ["check", "source_record"])
def test_api_arbitrary_container_structured_other_check_does_not_block_target(surface: str) -> None:
    run_id = _run_id(f"api-{surface}-arbitrary-transport-context-other-check-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    identity = latest["identity"]
    other_check_record = _structured_producer_metadata_record(
        identity,
        "hydro_met",
        source="IFS",
        run_id=f"{identity['run_id']}-sibling",
    )
    if surface == "check":
        latest["proofs"] = [_structured_producer_metadata_record(identity, "latest_product")]
        latest["transport_context"] = other_check_record
    else:
        latest.pop("evidence", None)
        source_record["proofs"] = {
            "latest_product": _structured_producer_metadata_record(identity, "latest_product"),
        }
        source_record["transport_context"] = other_check_record
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


@pytest.mark.parametrize("surface", ["check", "source_record"])
def test_api_arbitrary_container_structured_record_alone_is_unscoped(surface: str) -> None:
    run_id = _run_id(f"api-{surface}-arbitrary-transport-context-alone-unscoped")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    identity = latest["identity"]
    latest.pop("evidence", None)
    if surface == "check":
        latest["transport_context"] = _structured_producer_metadata_record(identity, "latest_product")
    else:
        source_record["transport_context"] = _structured_producer_metadata_record(identity, "latest_product")
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_UNSCOPED" in _codes(api_lane["blockers"])


@pytest.mark.parametrize(
    ("lane_dir", "lane_name", "check_name", "conflict_root", "expected_code"),
    [
        ("browser", "browser", "ops_job_logs", "artifacts", "TWO_NODE_E2E_BROWSER_CHECK_PRODUCER_IDENTITY_MISMATCH"),
        ("logs", "logs", "job_logs", "commands", "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH"),
    ],
)
def test_job_log_unstructured_producer_root_hidden_conflict_blocks_with_complete_proof(
    lane_dir: str,
    lane_name: str,
    check_name: str,
    conflict_root: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"{lane_name}-{conflict_root}-unstructured-conflict")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_dir / "summary.json")
    check = payload["sources"]["GFS"]["checks"][check_name]
    identity = check["identity"]
    sibling_run_id = f"{identity['run_id']}-sibling"
    sibling_job_id = f"{identity['job_id']}-sibling"
    check["proofs"] = [_complete_explicit_producer_record(identity, check_name)]
    check[conflict_root] = {
        "collector": {
            "message": _producer_identity_text(
                identity,
                check_name,
                run_id=sibling_run_id,
                job_id=sibling_job_id,
            )
        }
    }
    _write(config.run_dir / lane_dir / "summary.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_name]
    blockers = lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert expected_code in _codes(blockers)
    assert any(
        blocker.get("field") in {"run_id", "job_id"} and blocker.get("evidence_key") == "message"
        for blocker in blockers
    )


@pytest.mark.parametrize(
    ("lane_dir", "lane_name", "check_name", "other_check", "root_key", "text_key", "expected_code"),
    [
        (
            "logs",
            "logs",
            "job_logs",
            "ops_job_logs",
            "proofs",
            "message",
            "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH",
        ),
        (
            "browser",
            "browser",
            "ops_job_logs",
            "job_logs",
            "commands",
            "details",
            "TWO_NODE_E2E_BROWSER_CHECK_PRODUCER_IDENTITY_MISMATCH",
        ),
    ],
)
def test_job_log_check_producer_root_nested_other_check_text_does_not_block_target(
    lane_dir: str,
    lane_name: str,
    check_name: str,
    other_check: str,
    root_key: str,
    text_key: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"{lane_name}-{root_key}-{text_key}-other-check-pass")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_dir / "summary.json")
    source_record = payload["sources"]["GFS"]
    check = source_record["checks"][check_name]
    identity = check["identity"]
    sibling_run_id = f"{identity['run_id']}-sibling"
    sibling_job_id = f"{identity['job_id']}-sibling"
    target_proof = _complete_explicit_producer_record(identity, check_name)
    sibling_text = {
        text_key: _producer_identity_text(
            identity,
            other_check,
            source="IFS",
            run_id=sibling_run_id,
            job_id=sibling_job_id,
        )
    }
    check["proofs"] = [target_proof]
    check[root_key] = [target_proof, sibling_text] if root_key == "proofs" else [sibling_text]
    _write(config.run_dir / lane_dir / "summary.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_name]
    assert summary["status"] == STATUS_PASS
    assert lane["status"] == STATUS_PASS
    assert expected_code not in _codes(lane["blockers"])


@pytest.mark.parametrize("evidence_key", ["message", "text"])
def test_api_latest_product_check_top_level_hidden_text_conflict_blocks(evidence_key: str) -> None:
    run_id = _run_id(f"api-check-top-{evidence_key}-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check["evidence"] = {
        "producer": _complete_explicit_producer_record(identity, "latest_product"),
    }
    check[evidence_key] = _producer_identity_text(identity, "latest_product", source="IFS")
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == evidence_key
        for blocker in blockers
    )


@pytest.mark.parametrize("evidence_key", ["message", "text", "details", "body", "response_body"])
def test_api_latest_product_check_top_level_hidden_text_conflict_blocks_with_source_scoped_fallback(
    evidence_key: str,
) -> None:
    run_id = _run_id(f"api-check-top-source-fallback-{evidence_key}-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    source_record["proofs"] = {
        "latest_product": _complete_explicit_producer_record(identity, "latest_product"),
    }
    check[evidence_key] = _producer_identity_text(identity, "latest_product", source="IFS")
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("check") == "latest_product"
        and blocker.get("field") == "source"
        and blocker.get("observed") == "IFS"
        and blocker.get("evidence_key") == evidence_key
        for blocker in blockers
    )


def test_api_latest_product_check_top_level_other_check_text_passes_with_source_scoped_fallback() -> None:
    run_id = _run_id("api-check-top-other-check-source-fallback-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    source_record["proofs"] = {
        "latest_product": _complete_explicit_producer_record(identity, "latest_product"),
    }
    check["message"] = _producer_identity_text(
        identity,
        "series",
        source="IFS",
        run_id=f"{identity['run_id']}-sibling",
    )
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


def test_api_latest_product_source_top_level_message_conflict_blocks_with_check_evidence_intact() -> None:
    run_id = _run_id("api-source-top-message-check-proof-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    identity = source_record["checks"]["latest_product"]["identity"]
    source_record["message"] = _producer_identity_text(identity, "latest_product", source="IFS")
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("check") == "latest_product"
        and blocker.get("field") == "source"
        and blocker.get("observed") == "IFS"
        and blocker.get("evidence_key") == "message"
        for blocker in blockers
    )


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts", "source_artifacts"])
@pytest.mark.parametrize("conflict_shape", ["hidden_message", "identity_only"])
def test_api_latest_product_source_scoped_root_conflict_blocks_with_check_evidence_intact(
    source_evidence_key: str,
    conflict_shape: str,
) -> None:
    run_id = _run_id(f"api-source-{source_evidence_key}-{conflict_shape}-check-proof-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    identity = source_record["checks"]["latest_product"]["identity"]
    if conflict_shape == "hidden_message":
        source_record[source_evidence_key] = [
            {"message": _producer_identity_text(identity, "latest_product", source="IFS")}
        ]
    else:
        source_record[source_evidence_key] = [
            _identity_only_producer_record(identity, "latest_product", source="IFS")
        ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("check") == "latest_product"
        and blocker.get("field") == "source"
        and blocker.get("observed") == "IFS"
        for blocker in blockers
    )


def test_api_latest_product_source_proofs_wrapper_structured_conflict_blocks_with_check_evidence_intact() -> None:
    run_id = _run_id("api-source-proofs-wrapper-structured-check-proof-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    identity = source_record["checks"]["latest_product"]["identity"]
    source_record["proofs"] = [
        {
            "wrapper": _structured_producer_metadata_record(
                identity,
                "latest_product",
                source="IFS",
            )
        }
    ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("check") == "latest_product"
        and blocker.get("field") == "source"
        and blocker.get("observed") == "IFS"
        and blocker.get("evidence_key") == "wrapper"
        for blocker in blockers
    )


@pytest.mark.parametrize(
    ("lane_dir", "lane_name", "check_name", "expected_code"),
    [
        ("browser", "browser", "ops_job_logs", "TWO_NODE_E2E_BROWSER_CHECK_PRODUCER_IDENTITY_MISMATCH"),
        ("logs", "logs", "job_logs", "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH"),
    ],
)
def test_job_log_source_level_log_uri_conflict_blocks_with_check_evidence_intact(
    lane_dir: str,
    lane_name: str,
    check_name: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"{lane_name}-source-log-uri-check-proof-conflict")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_dir / "summary.json")
    source_record = payload["sources"]["GFS"]
    identity = source_record["checks"][check_name]["identity"]
    sibling_job_id = f"{identity['job_id']}-sibling"
    sibling_uri = f"published://logs/gfs/{identity['run_id']}/{sibling_job_id}.out"
    source_record["message"] = f"producer log check={check_name} log_uri={sibling_uri}"
    _write(config.run_dir / lane_dir / "summary.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_name]
    blockers = lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert expected_code in _codes(blockers)
    assert any(
        blocker.get("check") == check_name
        and blocker.get("field") == "job_id"
        and blocker.get("observed") == sibling_job_id
        and blocker.get("evidence_key") == "message"
        for blocker in blockers
    )


def test_api_source_top_level_other_check_only_conflict_passes_with_check_evidence_intact() -> None:
    run_id = _run_id("api-source-other-check-only-check-proof-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    identity = source_record["checks"]["latest_product"]["identity"]
    source_record["message"] = _producer_identity_text(
        identity,
        "hydro_met",
        source="IFS",
        run_id=f"{identity['run_id']}-sibling",
    )
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


@pytest.mark.parametrize(
    ("lane_dir", "lane_name", "check_name", "expected_code"),
    [
        ("browser", "browser", "ops_job_logs", "TWO_NODE_E2E_BROWSER_CHECK_PRODUCER_IDENTITY_MISMATCH"),
        ("logs", "logs", "job_logs", "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH"),
    ],
)
def test_job_log_proofs_hidden_only_response_body_sibling_conflict_blocks(
    lane_dir: str,
    lane_name: str,
    check_name: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"{lane_name}-proofs-hidden-response-conflict")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_dir / "summary.json")
    check = payload["sources"]["GFS"]["checks"][check_name]
    identity = check["identity"]
    sibling_run_id = f"{identity['run_id']}-sibling"
    sibling_job_id = f"{identity['job_id']}-sibling"
    check["proofs"] = [
        _complete_explicit_producer_record(identity, check_name),
        {
            "response_body": {
                "status_code": 200,
                "source": "IFS",
                "check": check_name,
                "run_id": sibling_run_id,
                "cycle_time": identity["cycle_time"],
                "model_id": identity["model_id"],
                "job_id": sibling_job_id,
                "log_uri": f"published://logs/ifs/{sibling_run_id}/{sibling_job_id}.out",
            }
        },
    ]
    _write(config.run_dir / lane_dir / "summary.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_name]
    blockers = lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert expected_code in _codes(blockers)
    assert any(blocker.get("field") in {"source", "run_id", "job_id"} for blocker in blockers)


@pytest.mark.parametrize(
    ("lane_dir", "lane_name", "check_name", "evidence_key", "expected_code"),
    [
        ("browser", "browser", "ops_job_logs", "text", "TWO_NODE_E2E_BROWSER_CHECK_PRODUCER_IDENTITY_MISMATCH"),
        ("browser", "browser", "ops_job_logs", "message", "TWO_NODE_E2E_BROWSER_CHECK_PRODUCER_IDENTITY_MISMATCH"),
        ("logs", "logs", "job_logs", "text", "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH"),
        ("logs", "logs", "job_logs", "message", "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH"),
    ],
)
def test_job_log_proofs_hidden_text_or_message_sibling_conflict_blocks(
    lane_dir: str,
    lane_name: str,
    check_name: str,
    evidence_key: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"{lane_name}-proofs-hidden-{evidence_key}-conflict")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_dir / "summary.json")
    check = payload["sources"]["GFS"]["checks"][check_name]
    identity = check["identity"]
    sibling_run_id = f"{identity['run_id']}-sibling"
    sibling_job_id = f"{identity['job_id']}-sibling"
    check["proofs"] = [
        _complete_explicit_producer_record(identity, check_name),
        {
            evidence_key: _producer_identity_text(
                identity,
                check_name,
                run_id=sibling_run_id,
                job_id=sibling_job_id,
            )
        },
    ]
    _write(config.run_dir / lane_dir / "summary.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_name]
    blockers = lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert expected_code in _codes(blockers)
    assert any(
        blocker.get("field") in {"run_id", "job_id"} and blocker.get("evidence_key") == evidence_key
        for blocker in blockers
    )


@pytest.mark.parametrize(
    ("lane_dir", "lane_name", "check_name", "evidence_key", "expected_code"),
    [
        ("browser", "browser", "ops_job_logs", "message", "TWO_NODE_E2E_BROWSER_CHECK_PRODUCER_IDENTITY_MISMATCH"),
        ("logs", "logs", "job_logs", "details", "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH"),
    ],
)
def test_job_log_check_top_level_hidden_text_conflict_blocks(
    lane_dir: str,
    lane_name: str,
    check_name: str,
    evidence_key: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"{lane_name}-check-top-{evidence_key}-conflict")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_dir / "summary.json")
    check = payload["sources"]["GFS"]["checks"][check_name]
    identity = check["identity"]
    sibling_run_id = f"{identity['run_id']}-sibling"
    sibling_job_id = f"{identity['job_id']}-sibling"
    check["evidence"] = {
        "producer": _complete_explicit_producer_record(identity, check_name),
    }
    check[evidence_key] = _producer_identity_text(
        identity,
        check_name,
        run_id=sibling_run_id,
        job_id=sibling_job_id,
    )
    _write(config.run_dir / lane_dir / "summary.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_name]
    blockers = lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert expected_code in _codes(blockers)
    assert any(
        blocker.get("field") in {"run_id", "job_id"} and blocker.get("evidence_key") == evidence_key
        for blocker in blockers
    )


def test_source_scoped_check_keyed_proofs_ignore_unrelated_sibling_checks() -> None:
    run_id = _run_id("source-keyed-proofs-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    series = source_record["checks"]["series"]
    latest_identity = latest["identity"]
    series_identity = series["identity"]
    latest.pop("evidence", None)
    source_record["proofs"] = {
        "latest_product": _complete_explicit_producer_record(latest_identity, "latest_product"),
        "series": _complete_explicit_producer_record(series_identity, "series"),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["api"]["status"] == STATUS_PASS


def test_source_scoped_check_keyed_target_proof_hidden_conflict_blocks() -> None:
    run_id = _run_id("source-keyed-target-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    series = source_record["checks"]["series"]
    latest_identity = latest["identity"]
    series_identity = series["identity"]
    latest.pop("evidence", None)
    source_record["proofs"] = {
        "latest_product": {
            "producer": _complete_explicit_producer_record(latest_identity, "latest_product"),
            "message": _producer_identity_text(latest_identity, "latest_product", source="IFS"),
        },
        "series": _complete_explicit_producer_record(series_identity, "series"),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == "message"
        for blocker in blockers
    )


@pytest.mark.parametrize(
    ("source_evidence_key", "metadata_key"),
    [
        ("proofs", "message"),
        ("evidence", "summary"),
        ("artifacts", "message"),
    ],
)
def test_source_scoped_check_keyed_root_non_check_hidden_target_conflict_blocks(
    source_evidence_key: str,
    metadata_key: str,
) -> None:
    run_id = _run_id(f"source-keyed-{source_evidence_key}-{metadata_key}-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    series = source_record["checks"]["series"]
    latest_identity = latest["identity"]
    series_identity = series["identity"]
    latest.pop("evidence", None)
    source_record[source_evidence_key] = {
        "latest_product": _complete_explicit_producer_record(latest_identity, "latest_product"),
        "series": _complete_explicit_producer_record(series_identity, "series"),
        metadata_key: _producer_identity_text(latest_identity, "latest_product", source="IFS"),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == metadata_key
        for blocker in blockers
    )


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts", "source_artifacts"])
@pytest.mark.parametrize("wrapper_key", NON_AUTHORITATIVE_WRAPPER_KEYS)
@pytest.mark.parametrize("wrapper_shape", ["scalar", "list"])
def test_source_scoped_check_keyed_root_non_check_wrapper_scalar_text_conflict_blocks(
    source_evidence_key: str,
    wrapper_key: str,
    wrapper_shape: str,
) -> None:
    run_id = _run_id(f"source-keyed-{source_evidence_key}-{wrapper_key}-{wrapper_shape}-text-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    identity = latest["identity"]
    latest.pop("evidence", None)
    conflict_text = _producer_identity_text(identity, "latest_product", source="IFS")
    proof_root = "evidence" if source_evidence_key == "proofs" else "proofs"
    source_record[proof_root] = {
        "latest_product": _complete_explicit_producer_record(identity, "latest_product"),
    }
    source_record[source_evidence_key] = {
        "series": _complete_explicit_producer_record(
            source_record["checks"]["series"]["identity"],
            "series",
        ),
        wrapper_key: conflict_text if wrapper_shape == "scalar" else ["collector ok", conflict_text],
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == wrapper_key
        for blocker in blockers
    )


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts", "source_artifacts"])
@pytest.mark.parametrize("metadata_key", ["metadata", "wrapper", "collector", "context"])
def test_source_scoped_check_keyed_root_non_check_structured_target_conflict_blocks(
    source_evidence_key: str,
    metadata_key: str,
) -> None:
    run_id = _run_id(f"source-keyed-{source_evidence_key}-{metadata_key}-structured-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    series = source_record["checks"]["series"]
    latest_identity = latest["identity"]
    series_identity = series["identity"]
    latest.pop("evidence", None)
    if source_evidence_key == "source_artifacts":
        source_record["proofs"] = {
            "latest_product": _structured_producer_metadata_record(latest_identity, "latest_product"),
        }
    source_record[source_evidence_key] = {
        "latest_product": _structured_producer_metadata_record(latest_identity, "latest_product"),
        "series": _structured_producer_metadata_record(series_identity, "series"),
        metadata_key: _structured_producer_metadata_record(
            latest_identity,
            "latest_product",
            source="IFS",
        ),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(blocker.get("field") == "source" and blocker.get("observed") == "IFS" for blocker in blockers)


def test_source_scoped_check_keyed_root_non_check_structured_run_id_conflict_blocks() -> None:
    run_id = _run_id("source-keyed-metadata-structured-run-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    identity = latest["identity"]
    observed_run_id = f"{identity['run_id']}-sibling"
    latest.pop("evidence", None)
    source_record["proofs"] = {
        "latest_product": _structured_producer_metadata_record(identity, "latest_product"),
        "metadata": _structured_producer_metadata_record(
            identity,
            "latest_product",
            run_id=observed_run_id,
        ),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "run_id" and blocker.get("observed") == observed_run_id
        for blocker in blockers
    )


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts"])
def test_source_scoped_check_keyed_root_non_check_hidden_other_check_does_not_block_target(
    source_evidence_key: str,
) -> None:
    run_id = _run_id(f"source-keyed-{source_evidence_key}-metadata-other-check")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    series = source_record["checks"]["series"]
    latest_identity = latest["identity"]
    latest.pop("evidence", None)
    source_record[source_evidence_key] = {
        "latest_product": _complete_explicit_producer_record(latest_identity, "latest_product"),
        "series": _complete_explicit_producer_record(series["identity"], "series"),
        "message": _producer_identity_text(
            latest_identity,
            "hydro_met",
            source="IFS",
            run_id=f"{latest_identity['run_id']}-sibling",
        ),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts", "source_artifacts"])
def test_source_scoped_check_keyed_root_non_check_structured_other_check_does_not_block_target(
    source_evidence_key: str,
) -> None:
    run_id = _run_id(f"source-keyed-{source_evidence_key}-structured-other-check")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    series = source_record["checks"]["series"]
    latest_identity = latest["identity"]
    latest.pop("evidence", None)
    if source_evidence_key == "source_artifacts":
        source_record["proofs"] = {
            "latest_product": _structured_producer_metadata_record(latest_identity, "latest_product"),
        }
    source_record[source_evidence_key] = {
        "latest_product": _structured_producer_metadata_record(latest_identity, "latest_product"),
        "series": _structured_producer_metadata_record(series["identity"], "series"),
        "metadata": _structured_producer_metadata_record(
            latest_identity,
            "hydro_met",
            source="IFS",
            run_id=f"{latest_identity['run_id']}-sibling",
        ),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts", "source_artifacts"])
@pytest.mark.parametrize("metadata_key", ["metadata", "wrapper", "collector", "context"])
def test_source_scoped_check_keyed_root_non_check_structured_record_alone_is_unscoped(
    source_evidence_key: str,
    metadata_key: str,
) -> None:
    run_id = _run_id(f"source-keyed-{source_evidence_key}-{metadata_key}-structured-alone")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    identity = latest["identity"]
    latest.pop("evidence", None)
    source_record[source_evidence_key] = {
        metadata_key: _structured_producer_metadata_record(identity, "latest_product"),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_UNSCOPED" in _codes(api_lane["blockers"])


def test_source_scoped_check_keyed_root_non_check_hidden_job_id_conflict_blocks() -> None:
    run_id = _run_id("source-keyed-job-metadata-conflict")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    source_record = logs_summary["sources"]["GFS"]
    job_logs = source_record["checks"]["job_logs"]
    identity = job_logs["identity"]
    job_logs.pop("evidence", None)
    source_record["proofs"] = {
        "job_logs": _complete_explicit_producer_record(identity, "job_logs"),
        "ops_job_logs": _complete_explicit_producer_record(identity, "ops_job_logs"),
        "summary": _producer_identity_text(
            identity,
            "job_logs",
            job_id=f"{identity['job_id']}-sibling",
        ),
    }
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "job_id" and blocker.get("evidence_key") == "summary"
        for blocker in blockers
    )


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts"])
@pytest.mark.parametrize("metadata_key", ["log_uri", "published_log_uri"])
@pytest.mark.parametrize("uri_scheme", ["published", "file", "s3"])
def test_logs_source_scoped_check_keyed_root_non_check_log_uri_identity_conflict_blocks(
    source_evidence_key: str,
    metadata_key: str,
    uri_scheme: str,
) -> None:
    run_id = _run_id(f"source-keyed-{source_evidence_key}-{metadata_key}-{uri_scheme}-conflict")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    source_record = logs_summary["sources"]["GFS"]
    job_logs = source_record["checks"]["job_logs"]
    identity = job_logs["identity"]
    sibling_run_id = f"{identity['run_id']}-sibling"
    sibling_job_id = f"{identity['job_id']}-sibling"
    job_logs.pop("evidence", None)
    source_record[source_evidence_key] = {
        "job_logs": _complete_explicit_producer_record(identity, "job_logs"),
        "ops_job_logs": _complete_explicit_producer_record(identity, "ops_job_logs"),
        metadata_key: _log_uri_for_scheme(
            uri_scheme,
            source=identity["source"],
            run_id=sibling_run_id,
            job_id=sibling_job_id,
        ),
    }
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") in {"run_id", "job_id"}
        and blocker.get("evidence_key") == metadata_key
        and blocker.get("observed") in {sibling_run_id, sibling_job_id}
        and blocker.get("expected") in {identity["run_id"], identity["job_id"]}
        for blocker in blockers
    )


@pytest.mark.parametrize("metadata_key", ["log_uri", "published_log_uri"])
def test_logs_source_scoped_check_keyed_root_matching_log_uri_metadata_does_not_block(
    metadata_key: str,
) -> None:
    run_id = _run_id(f"source-keyed-{metadata_key}-matching-log-uri-pass")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    source_record = logs_summary["sources"]["GFS"]
    job_logs = source_record["checks"]["job_logs"]
    identity = job_logs["identity"]
    job_logs.pop("evidence", None)
    source_record["proofs"] = {
        "job_logs": _complete_explicit_producer_record(identity, "job_logs"),
        "ops_job_logs": _complete_explicit_producer_record(identity, "ops_job_logs"),
        metadata_key: _log_uri_for_scheme(
            "published",
            source=identity["source"],
            run_id=identity["run_id"],
            job_id=identity["job_id"],
        ),
    }
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    assert summary["status"] == STATUS_PASS
    assert logs_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(logs_lane["blockers"])


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts", "source_artifacts"])
def test_logs_job_logs_check_keyed_sibling_hidden_target_text_conflict_blocks(
    source_evidence_key: str,
) -> None:
    run_id = _run_id(f"logs-sibling-key-text-{source_evidence_key}")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    source_record = logs_summary["sources"]["GFS"]
    job_logs = source_record["checks"]["job_logs"]
    identity = job_logs["identity"]
    sibling_job_id = f"{identity['job_id']}-sibling"
    job_logs.pop("evidence", None)
    target_proof = {"job_logs": _complete_explicit_producer_record(identity, "job_logs")}
    sibling_conflict = {
        "message": _producer_identity_text(
            identity,
            "job_logs",
            job_id=sibling_job_id,
        )
    }
    if source_evidence_key == "source_artifacts":
        source_record["proofs"] = target_proof
        source_record["source_artifacts"] = {"ops_job_logs": sibling_conflict}
    else:
        source_record[source_evidence_key] = {**target_proof, "ops_job_logs": sibling_conflict}
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "job_id"
        and blocker.get("evidence_key") == "message"
        and blocker.get("observed") == sibling_job_id
        for blocker in blockers
    )


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts", "source_artifacts"])
@pytest.mark.parametrize("identity_key", ["identity", "strict_identity"])
def test_logs_job_logs_check_keyed_sibling_nested_identity_target_conflict_blocks(
    source_evidence_key: str,
    identity_key: str,
) -> None:
    run_id = _run_id(f"logs-sibling-key-{identity_key}-{source_evidence_key}")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    source_record = logs_summary["sources"]["GFS"]
    job_logs = source_record["checks"]["job_logs"]
    identity = job_logs["identity"]
    sibling_job_id = f"{identity['job_id']}-sibling"
    job_logs.pop("evidence", None)
    target_proof = {"job_logs": _complete_explicit_producer_record(identity, "job_logs")}
    sibling_conflict = {
        identity_key: _identity_only_producer_record(
            identity,
            "job_logs",
            job_id=sibling_job_id,
        )
    }
    if source_evidence_key == "source_artifacts":
        source_record["proofs"] = target_proof
        source_record["source_artifacts"] = {"ops_job_logs": sibling_conflict}
    else:
        source_record[source_evidence_key] = {**target_proof, "ops_job_logs": sibling_conflict}
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "job_id" and blocker.get("observed") == sibling_job_id for blocker in blockers
    )


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts", "source_artifacts"])
def test_logs_job_logs_check_keyed_sibling_target_log_uri_conflict_blocks(
    source_evidence_key: str,
) -> None:
    run_id = _run_id(f"logs-sibling-key-log-uri-{source_evidence_key}")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    source_record = logs_summary["sources"]["GFS"]
    job_logs = source_record["checks"]["job_logs"]
    identity = job_logs["identity"]
    sibling_run_id = f"{identity['run_id']}-sibling"
    sibling_job_id = f"{identity['job_id']}-sibling"
    job_logs.pop("evidence", None)
    target_proof = {"job_logs": _complete_explicit_producer_record(identity, "job_logs")}
    sibling_conflict = {
        "check": "job_logs",
        "log_uri": _log_uri_for_scheme(
            "published",
            source=identity["source"],
            run_id=sibling_run_id,
            job_id=sibling_job_id,
        ),
    }
    if source_evidence_key == "source_artifacts":
        source_record["proofs"] = target_proof
        source_record["source_artifacts"] = {"ops_job_logs": sibling_conflict}
    else:
        source_record[source_evidence_key] = {**target_proof, "ops_job_logs": sibling_conflict}
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") in {"run_id", "job_id"}
        and blocker.get("evidence_key") == "log_uri"
        and blocker.get("observed") in {sibling_run_id, sibling_job_id}
        for blocker in blockers
    )


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts", "source_artifacts"])
def test_logs_job_logs_check_keyed_sibling_other_check_text_does_not_block_target(
    source_evidence_key: str,
) -> None:
    run_id = _run_id(f"logs-sibling-key-other-{source_evidence_key}")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    source_record = logs_summary["sources"]["GFS"]
    job_logs = source_record["checks"]["job_logs"]
    identity = job_logs["identity"]
    sibling_run_id = f"{identity['run_id']}-sibling"
    sibling_job_id = f"{identity['job_id']}-sibling"
    job_logs.pop("evidence", None)
    target_proof = {"job_logs": _complete_explicit_producer_record(identity, "job_logs")}
    sibling_other_check = {
        "message": _producer_identity_text(
            identity,
            "ops_job_logs",
            run_id=sibling_run_id,
            job_id=sibling_job_id,
        )
    }
    if source_evidence_key == "source_artifacts":
        source_record["proofs"] = target_proof
        source_record["source_artifacts"] = {"ops_job_logs": sibling_other_check}
    else:
        source_record[source_evidence_key] = {**target_proof, "ops_job_logs": sibling_other_check}
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    assert summary["status"] == STATUS_PASS
    assert logs_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(logs_lane["blockers"])


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts", "source_artifacts"])
@pytest.mark.parametrize("conflict_shape", ["text", "identity", "strict_identity"])
def test_api_latest_product_check_keyed_sibling_target_conflict_blocks(
    source_evidence_key: str,
    conflict_shape: str,
) -> None:
    run_id = _run_id(f"api-sibling-key-{conflict_shape}-{source_evidence_key}")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    identity = latest["identity"]
    latest.pop("evidence", None)
    target_proof = {"latest_product": _complete_explicit_producer_record(identity, "latest_product")}
    if conflict_shape == "text":
        sibling_conflict = {
            "message": _producer_identity_text(identity, "latest_product", source="IFS"),
        }
    else:
        sibling_conflict = {
            conflict_shape: _identity_only_producer_record(
                identity,
                "latest_product",
                source="IFS",
            )
        }
    if source_evidence_key == "source_artifacts":
        source_record["proofs"] = target_proof
        source_record["source_artifacts"] = {"series": sibling_conflict}
    else:
        source_record[source_evidence_key] = {**target_proof, "series": sibling_conflict}
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(blocker.get("field") == "source" and blocker.get("observed") == "IFS" for blocker in blockers)


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts", "source_artifacts"])
def test_api_latest_product_check_keyed_sibling_other_check_text_does_not_block_target(
    source_evidence_key: str,
) -> None:
    run_id = _run_id(f"api-sibling-key-series-{source_evidence_key}")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    identity = latest["identity"]
    latest.pop("evidence", None)
    target_proof = {"latest_product": _complete_explicit_producer_record(identity, "latest_product")}
    series_identity = source_record["checks"]["series"]["identity"]
    sibling_other_check = {
        "message": _producer_identity_text(
            series_identity,
            "series",
        )
    }
    if source_evidence_key == "source_artifacts":
        source_record["proofs"] = target_proof
        source_record["source_artifacts"] = {"series": sibling_other_check}
    else:
        source_record[source_evidence_key] = {**target_proof, "series": sibling_other_check}
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


def test_logs_job_logs_check_top_level_log_uri_conflict_blocks_with_source_scoped_fallback() -> None:
    run_id = _run_id("logs-check-top-log-uri-source-fallback-conflict")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    source_record = logs_summary["sources"]["GFS"]
    check = source_record["checks"]["job_logs"]
    identity = check["identity"]
    sibling_run_id = f"{identity['run_id']}-sibling"
    sibling_job_id = f"{identity['job_id']}-sibling"
    check.pop("evidence", None)
    source_record["proofs"] = {
        "job_logs": _complete_explicit_producer_record(identity, "job_logs"),
    }
    check["log_uri"] = _log_uri_for_scheme(
        "published",
        source=identity["source"],
        run_id=sibling_run_id,
        job_id=sibling_job_id,
    )
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("check") == "job_logs"
        and blocker.get("field") in {"run_id", "job_id"}
        and blocker.get("evidence_key") == "log_uri"
        and blocker.get("observed") in {sibling_run_id, sibling_job_id}
        for blocker in blockers
    )


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts", "source_artifacts"])
@pytest.mark.parametrize("wrapper_key", ["metadata", "wrapper"])
def test_logs_source_scoped_check_keyed_root_wrapper_scalar_log_uri_conflict_blocks(
    source_evidence_key: str,
    wrapper_key: str,
) -> None:
    run_id = _run_id(f"source-keyed-{source_evidence_key}-{wrapper_key}-log-uri-conflict")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    source_record = logs_summary["sources"]["GFS"]
    job_logs = source_record["checks"]["job_logs"]
    identity = job_logs["identity"]
    sibling_run_id = f"{identity['run_id']}-sibling"
    sibling_job_id = f"{identity['job_id']}-sibling"
    job_logs.pop("evidence", None)
    proof_root = "evidence" if source_evidence_key == "proofs" else "proofs"
    source_record[proof_root] = {
        "job_logs": _complete_explicit_producer_record(identity, "job_logs"),
    }
    source_record[source_evidence_key] = {
        "ops_job_logs": _complete_explicit_producer_record(identity, "ops_job_logs"),
        wrapper_key: _log_uri_for_scheme(
            "published",
            source=identity["source"],
            run_id=sibling_run_id,
            job_id=sibling_job_id,
        ),
    }
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") in {"run_id", "job_id"}
        and blocker.get("evidence_key") == wrapper_key
        and blocker.get("observed") in {sibling_run_id, sibling_job_id}
        for blocker in blockers
    )


@pytest.mark.parametrize("source_evidence_key", ["source_artifacts", "proofs", "evidence", "artifacts"])
@pytest.mark.parametrize("wrapper_key", ["metadata", "wrapper", "collector"])
def test_source_scoped_non_authoritative_wrapper_record_does_not_satisfy_producer_identity(
    source_evidence_key: str,
    wrapper_key: str,
) -> None:
    run_id = _run_id(f"source-{source_evidence_key}-{wrapper_key}-wrapper-unscoped")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    identity = latest["identity"]
    latest.pop("evidence", None)
    source_record[source_evidence_key] = {
        wrapper_key: _complete_explicit_producer_record(identity, "latest_product"),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_UNSCOPED" in _codes(api_lane["blockers"])


@pytest.mark.parametrize("source_evidence_key", ["source_artifacts", "proofs", "evidence", "artifacts"])
@pytest.mark.parametrize("wrapper_key", ["metadata", "wrapper", "collector"])
def test_source_scoped_non_authoritative_wrapper_record_conflict_still_blocks_with_explicit_proof(
    source_evidence_key: str,
    wrapper_key: str,
) -> None:
    run_id = _run_id(f"source-{source_evidence_key}-{wrapper_key}-wrapper-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    identity = latest["identity"]
    latest.pop("evidence", None)
    explicit_proof_root = "evidence" if source_evidence_key == "proofs" else "proofs"
    source_record[explicit_proof_root] = {
        "latest_product": _complete_explicit_producer_record(identity, "latest_product"),
    }
    source_record[source_evidence_key] = {
        wrapper_key: {
            **_complete_explicit_producer_record(identity, "latest_product"),
            "source": "IFS",
        },
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(blocker.get("field") == "source" and blocker.get("observed") == "IFS" for blocker in blockers)


def test_source_scoped_unstructured_evidence_root_hidden_conflict_blocks_with_complete_proof() -> None:
    run_id = _run_id("source-unstructured-root-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    latest = source_record["checks"]["latest_product"]
    identity = latest["identity"]
    latest.pop("evidence", None)
    source_record["proofs"] = {
        "latest_product": _complete_explicit_producer_record(identity, "latest_product"),
    }
    source_record["evidence"] = {
        "message": _producer_identity_text(identity, "latest_product", source="IFS"),
    }
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == "message"
        for blocker in blockers
    )


@pytest.mark.parametrize("root_shape", ["scalar", "list"])
def test_source_scoped_scalar_or_list_evidence_root_hidden_conflict_blocks_with_complete_proof(
    root_shape: str,
) -> None:
    run_id = _run_id(f"source-scalar-evidence-{root_shape}-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    conflict_text = _producer_identity_text(identity, "latest_product", source="IFS")
    check.pop("evidence", None)
    source_record["proofs"] = {
        "latest_product": _complete_explicit_producer_record(identity, "latest_product"),
    }
    source_record["evidence"] = conflict_text if root_shape == "scalar" else ["collector ok", conflict_text]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == "evidence"
        for blocker in blockers
    )


@pytest.mark.parametrize("source_evidence_key", ["evidence", "proofs"])
def test_source_scoped_producer_value_hidden_only_sibling_conflict_blocks(
    source_evidence_key: str,
) -> None:
    run_id = _run_id(f"source-{source_evidence_key}-hidden-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    source_record[source_evidence_key] = [
        _complete_explicit_producer_record(identity, "latest_product"),
        {
            "body": {
                "status_code": 200,
                "source": "IFS",
                "check": "latest_product",
                "run_id": identity["run_id"],
                "cycle_time": identity["cycle_time"],
                "model_id": identity["model_id"],
            }
        },
    ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == "body"
        for blocker in blockers
    )


@pytest.mark.parametrize("source_evidence_key", ["evidence", "proofs"])
def test_source_scoped_producer_value_hidden_only_text_sibling_conflict_blocks(
    source_evidence_key: str,
) -> None:
    run_id = _run_id(f"source-{source_evidence_key}-hidden-text-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    source_record[source_evidence_key] = [
        _complete_explicit_producer_record(identity, "latest_product"),
        {
            "text": _producer_identity_text(
                identity,
                "latest_product",
                source="IFS",
            )
        },
    ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == "text"
        for blocker in blockers
    )


@pytest.mark.parametrize("source_evidence_key", ["proofs", "evidence", "artifacts"])
def test_api_latest_product_source_scoped_list_identity_only_sibling_conflict_blocks(
    source_evidence_key: str,
) -> None:
    run_id = _run_id(f"source-{source_evidence_key}-identity-only-list-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    source_record[source_evidence_key] = [
        _complete_explicit_producer_record(identity, "latest_product"),
        _identity_only_producer_record(identity, "latest_product", source="IFS"),
    ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(blocker.get("field") == "source" and blocker.get("observed") == "IFS" for blocker in blockers)


@pytest.mark.parametrize("identity_key", ["identity", "strict_identity"])
def test_api_latest_product_source_scoped_list_nested_identity_only_sibling_conflict_blocks(
    identity_key: str,
) -> None:
    run_id = _run_id(f"source-list-{identity_key}-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    source_record["proofs"] = [
        _complete_explicit_producer_record(identity, "latest_product"),
        {identity_key: _identity_only_producer_record(identity, "latest_product", source="IFS")},
    ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(blocker.get("field") == "source" and blocker.get("observed") == "IFS" for blocker in blockers)


def test_logs_job_logs_source_scoped_list_identity_only_job_id_sibling_conflict_blocks() -> None:
    run_id = _run_id("logs-source-list-identity-job-conflict")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    source_record = logs_summary["sources"]["GFS"]
    check = source_record["checks"]["job_logs"]
    identity = check["identity"]
    sibling_job_id = f"{identity['job_id']}-sibling"
    check.pop("evidence", None)
    source_record["proofs"] = [
        _complete_explicit_producer_record(identity, "job_logs"),
        _identity_only_producer_record(identity, "job_logs", job_id=sibling_job_id),
    ]
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "job_id" and blocker.get("observed") == sibling_job_id
        for blocker in blockers
    )


def test_source_scoped_list_other_check_identity_only_sibling_does_not_block_target() -> None:
    run_id = _run_id("source-list-other-check-identity-only-pass")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    source_record["proofs"] = [
        _complete_explicit_producer_record(identity, "latest_product"),
        _identity_only_producer_record(
            identity,
            "hydro_met",
            source="IFS",
            run_id=f"{identity['run_id']}-sibling",
        ),
    ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


def test_source_scoped_list_other_check_hidden_target_text_conflict_blocks() -> None:
    run_id = _run_id("source-list-other-check-hidden-target")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    source_record["proofs"] = [
        _complete_explicit_producer_record(identity, "latest_product"),
        {
            "source": "GFS",
            "check": "series",
            "run_id": identity["run_id"],
            "cycle_time": identity["cycle_time"],
            "model_id": identity["model_id"],
            "message": _producer_identity_text(identity, "latest_product", source="IFS"),
        },
    ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == "message"
        for blocker in blockers
    )


def test_source_scoped_list_other_check_without_target_text_does_not_block_target() -> None:
    run_id = _run_id("source-list-other-check-ignored")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    source_record["proofs"] = [
        _complete_explicit_producer_record(identity, "latest_product"),
        {
            "source": "GFS",
            "check": "series",
            "run_id": identity["run_id"],
            "cycle_time": identity["cycle_time"],
            "model_id": identity["model_id"],
            "message": "producer response source=GFS check=series",
        },
    ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["api"]["status"] == STATUS_PASS


def test_source_scoped_list_hidden_other_check_without_target_text_does_not_block_target() -> None:
    run_id = _run_id("source-list-hidden-other-check")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    sibling_run_id = f"{identity['run_id']}-sibling"
    check.pop("evidence", None)
    source_record["proofs"] = [
        _complete_explicit_producer_record(identity, "latest_product"),
        {
            "message": _producer_identity_text(
                identity,
                "hydro_met",
                source="IFS",
                run_id=sibling_run_id,
            ),
        },
    ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    assert summary["status"] == STATUS_PASS
    assert api_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(api_lane["blockers"])


def test_source_scoped_job_id_list_other_check_hidden_target_text_conflict_blocks() -> None:
    run_id = _run_id("source-job-list-other-check-hidden-target")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    source_record = logs_summary["sources"]["GFS"]
    check = source_record["checks"]["job_logs"]
    identity = check["identity"]
    sibling_job_id = f"{identity['job_id']}-sibling"
    check.pop("evidence", None)
    source_record["proofs"] = [
        _complete_explicit_producer_record(identity, "job_logs"),
        {
            "source": "GFS",
            "check": "ops_job_logs",
            "run_id": identity["run_id"],
            "cycle_time": identity["cycle_time"],
            "model_id": identity["model_id"],
            "job_id": identity["job_id"],
            "message": _producer_identity_text(
                identity,
                "job_logs",
                job_id=sibling_job_id,
            ),
        },
    ]
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "job_id" and blocker.get("evidence_key") == "message"
        for blocker in blockers
    )


def test_source_scoped_job_id_list_hidden_other_check_without_target_text_does_not_block_target() -> None:
    run_id = _run_id("source-job-list-hidden-other-check")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    source_record = logs_summary["sources"]["GFS"]
    check = source_record["checks"]["job_logs"]
    identity = check["identity"]
    sibling_run_id = f"{identity['run_id']}-sibling"
    sibling_job_id = f"{identity['job_id']}-sibling"
    check.pop("evidence", None)
    source_record["proofs"] = [
        _complete_explicit_producer_record(identity, "job_logs"),
        {
            "message": _producer_identity_text(
                identity,
                "ops_job_logs",
                source="IFS",
                run_id=sibling_run_id,
                job_id=sibling_job_id,
            ),
        },
    ]
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    assert summary["status"] == STATUS_PASS
    assert logs_lane["status"] == STATUS_PASS
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" not in _codes(logs_lane["blockers"])


@pytest.mark.parametrize("evidence_key", ["summary", "message"])
def test_source_scoped_producer_top_level_hidden_text_conflict_blocks(evidence_key: str) -> None:
    run_id = _run_id(f"source-top-{evidence_key}-conflict")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    source_record = api_summary["sources"]["GFS"]
    check = source_record["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    source_record["proofs"] = {
        "latest_product": _complete_explicit_producer_record(identity, "latest_product"),
    }
    source_record[evidence_key] = _producer_identity_text(identity, "latest_product", source="IFS")
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == "source" and blocker.get("evidence_key") == evidence_key
        for blocker in blockers
    )


def test_hidden_text_identity_fields_do_not_become_proof_candidates() -> None:
    run_id = _run_id("api-hidden-text-candidate")
    config = _seed_pass_bundle(run_id)
    api_summary = _read(config.run_dir / "api" / "summary.json")
    check = api_summary["sources"]["GFS"]["checks"]["latest_product"]
    identity = check["identity"]
    check.pop("evidence", None)
    check["proofs"] = [
        {
            "text": {
                "status_code": 200,
                "source": identity["source"],
                "check": "latest_product",
                "run_id": identity["run_id"],
                "cycle_time": identity["cycle_time"],
                "model_id": identity["model_id"],
            }
        }
    ]
    _write(config.run_dir / "api" / "summary.json", api_summary)

    summary = validate_two_node_e2e_evidence(config)

    api_lane = summary["lane_summaries"]["api"]
    blockers = api_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert api_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_UNSCOPED" in _codes(blockers)


@pytest.mark.parametrize(
    ("lane_dir", "lane_name", "check_name", "mutation"),
    [
        ("api", "api", "latest_product", "sibling_source"),
        ("api", "api", "latest_product", "stale_run"),
        ("api", "api", "latest_product", "wrong_check"),
        ("browser", "browser", "ops_job_logs", "sibling_source"),
        ("browser", "browser", "ops_job_logs", "stale_run"),
        ("browser", "browser", "ops_job_logs", "wrong_check"),
        ("browser", "browser", "ops_job_logs", "wrong_job"),
        ("logs", "logs", "job_logs", "sibling_source"),
        ("logs", "logs", "job_logs", "stale_run"),
        ("logs", "logs", "job_logs", "wrong_check"),
        ("logs", "logs", "job_logs", "wrong_job"),
    ],
)
def test_source_lane_producer_evidence_must_semantically_bind_required_check(
    lane_dir: str,
    lane_name: str,
    check_name: str,
    mutation: str,
) -> None:
    run_id = _run_id(f"{lane_name}-producer-{mutation}")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_dir / "summary.json")
    check = payload["sources"]["GFS"]["checks"][check_name]
    evidence = check["evidence"]

    if mutation == "sibling_source":
        evidence["request"]["path"] = evidence["request"]["path"].replace("/gfs/", "/ifs/")
        evidence["request"]["source"] = "IFS"
        evidence["response"]["source"] = "IFS"
        evidence["response"].setdefault("body", {})["source"] = "IFS"
    elif mutation == "stale_run":
        evidence["request"]["path"] = f"{evidence['request']['path']}?run_id=older-run"
        evidence["response"].setdefault("body", {})["run_id"] = "older-run"
    elif mutation == "wrong_check":
        evidence["request"]["path"] = evidence["request"]["path"].replace(check_name, "ops_status")
        evidence["request"]["check"] = "ops_status"
        evidence["response"]["check"] = "ops_status"
    else:
        evidence["request"]["path"] = f"{evidence['request']['path']}?job_id=sibling-job"
        evidence["response"].setdefault("body", {})["job_id"] = "sibling-job"

    _write(config.run_dir / lane_dir / "summary.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_name]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert f"TWO_NODE_E2E_{lane_name.upper()}_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(lane["blockers"])


@pytest.mark.parametrize(
    ("lane_dir", "lane_name", "check_name", "hidden_check", "expected_code"),
    [
        ("api", "api", "jobs", "ops_status", "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH"),
        (
            "api",
            "api",
            "latest_product",
            "ops_status",
            "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH",
        ),
        (
            "logs",
            "logs",
            "job_logs",
            "ops_job_logs",
            "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH",
        ),
        (
            "api",
            "api",
            "latest_product",
            "not_latest_product",
            "TWO_NODE_E2E_API_CHECK_PRODUCER_IDENTITY_MISMATCH",
        ),
    ],
)
def test_source_lane_producer_text_check_identity_conflicts_even_when_explicit_fields_match(
    lane_dir: str,
    lane_name: str,
    check_name: str,
    hidden_check: str,
    expected_code: str,
) -> None:
    run_id = _run_id(f"{lane_name}-hidden-{hidden_check}")
    config = _seed_pass_bundle(run_id)
    payload = _read(config.run_dir / lane_dir / "summary.json")
    check = payload["sources"]["GFS"]["checks"][check_name]
    evidence = check["evidence"]
    identity = check["identity"]

    query = (
        f"source=GFS&check={hidden_check}&run_id={identity['run_id']}"
        f"&cycle_time={identity['cycle_time']}&model_id={identity['model_id']}"
    )
    if check_name == "job_logs":
        query = f"{query}&job_id={identity['job_id']}"
    evidence["request"]["path"] = f"/producer/gfs/{hidden_check}?{query}"
    evidence["request"]["query"] = query
    evidence["request"]["body"] = {"check": hidden_check, "source": "GFS"}
    evidence["request"]["check"] = check_name
    evidence["response"]["check"] = check_name
    evidence["response"]["body"] = {
        **dict(evidence["response"].get("body", {})),
        "check": hidden_check,
    }
    _write(config.run_dir / lane_dir / "summary.json", payload)

    summary = validate_two_node_e2e_evidence(config)

    lane = summary["lane_summaries"][lane_name]
    blockers = lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert lane["status"] == STATUS_BLOCKED
    assert expected_code in _codes(blockers)
    assert any(blocker.get("field") == "check" for blocker in blockers)


@pytest.mark.parametrize(
    "field",
    ["request_path", "log_uri"],
)
def test_logs_job_logs_producer_rejects_sibling_identity_in_path_or_uri(field: str) -> None:
    run_id = _run_id(f"logs-sibling-{field}")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    check = logs_summary["sources"]["GFS"]["checks"]["job_logs"]
    identity = check["identity"]
    sibling_run_id = f"{identity['run_id']}-sibling"
    sibling_job_id = f"{identity['job_id']}-sibling"
    sibling_uri = f"published://logs/gfs/{sibling_run_id}/{sibling_job_id}.out"
    if field == "request_path":
        check["evidence"]["request"]["path"] = (
            f"/api/v1/jobs/{sibling_job_id}/logs?source=GFS&run_id={sibling_run_id}"
            f"&cycle_time={identity['cycle_time']}&model_id={identity['model_id']}&job_id={sibling_job_id}"
        )
    else:
        check["log_uri"] = sibling_uri
        check["evidence"]["response"]["body"]["log_uri"] = sibling_uri
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any("conflicts with the required check identity" in str(blocker.get("message", "")) for blocker in blockers)


@pytest.mark.parametrize(
    ("field", "uri_scheme"),
    [
        ("log_uri", "published"),
        ("published_log_read.log_uri", "published"),
        ("log_uri", "file"),
        ("published_log_read.log_uri", "s3"),
    ],
)
def test_logs_job_logs_allowed_uri_fields_must_match_check_identity(field: str, uri_scheme: str) -> None:
    run_id = _run_id(f"logs-uri-identity-{field.replace('.', '-')}-{uri_scheme}")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    check = logs_summary["sources"]["GFS"]["checks"]["job_logs"]
    identity = check["identity"]
    correct_uri = f"published://logs/gfs/{identity['run_id']}/{identity['job_id']}.out"
    sibling_run_id = f"{identity['run_id']}-sibling"
    sibling_job_id = f"{identity['job_id']}-sibling"
    if uri_scheme == "published":
        sibling_uri = f"published://logs/gfs/{sibling_run_id}/{sibling_job_id}.out"
    elif uri_scheme == "file":
        root = Path("/mnt/nhms-published")
        check["published_artifact_root"] = str(root)
        sibling_uri = f"file://{root}/logs/gfs/{sibling_run_id}/{sibling_job_id}.out"
    else:
        check["published_artifact_s3_bucket"] = "nhms-prod"
        check["published_artifact_s3_prefix"] = "published"
        sibling_uri = f"s3://nhms-prod/published/logs/gfs/{sibling_run_id}/{sibling_job_id}.out"

    check["evidence"]["response"]["body"]["log_uri"] = correct_uri
    if field == "log_uri":
        check["log_uri"] = sibling_uri
    else:
        check["log_uri"] = correct_uri
        check["published_log_read"]["log_uri"] = sibling_uri
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") in {"run_id", "job_id"}
        and blocker.get("evidence_key") == "log_uri"
        and blocker.get("observed") in {sibling_run_id, sibling_job_id}
        and blocker.get("expected") in {identity["run_id"], identity["job_id"]}
        for blocker in blockers
    )


@pytest.mark.parametrize(
    ("uri_scheme", "stream"),
    [
        ("published", "out"),
        ("published", "err"),
        ("file", "out"),
        ("s3", "out"),
    ],
)
def test_logs_lane_accepts_canonical_log_uri_identity(uri_scheme: str, stream: str) -> None:
    run_id = _run_id(f"logs-canonical-{uri_scheme}-{stream}")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    for source, record in logs_summary["sources"].items():
        check = record["checks"]["job_logs"]
        identity = check["identity"]
        uri = _log_uri_for_scheme(
            uri_scheme,
            source=identity["source"],
            cycle_time=identity["cycle_time"],
            run_id=identity["run_id"],
            job_id=identity["job_id"],
            stream=stream,
        )
        if uri_scheme == "file":
            check["published_artifact_root"] = "/mnt/nhms-published"
        elif uri_scheme == "s3":
            check["published_artifact_s3_bucket"] = "nhms-prod"
            check["published_artifact_s3_prefix"] = "published"
        check["log_uri"] = uri
        check["published_log_read"]["log_uri"] = uri
        check["evidence"]["response"]["body"]["log_uri"] = uri
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["logs"]["status"] == STATUS_PASS


def test_logs_lane_accepts_real_published_log_uri_helper_contract() -> None:
    run_id = _run_id("logs-canonical-producer-helper")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    check = logs_summary["sources"]["GFS"]["checks"]["job_logs"]
    identity = check["identity"]
    uri = published_log_uri(
        source=identity["source"],
        cycle_time=datetime.fromisoformat(identity["cycle_time"].replace("Z", "+00:00")),
        run_id=identity["run_id"],
        job_id=identity["job_id"],
    )
    check["log_uri"] = uri
    check["published_log_read"]["log_uri"] = uri
    check["evidence"]["response"]["body"]["log_uri"] = uri
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["logs"]["status"] == STATUS_PASS


@pytest.mark.parametrize("field", ["source", "cycle_time", "run_id", "job_id"])
def test_logs_lane_canonical_log_uri_identity_mismatch_blocks(field: str) -> None:
    run_id = _run_id(f"logs-canonical-{field}-mismatch")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    check = logs_summary["sources"]["GFS"]["checks"]["job_logs"]
    identity = check["identity"]
    observed = "IFS" if field == "source" else f"{identity[field]}-sibling"
    uri_identity = {
        "source": identity["source"],
        "cycle_time": identity["cycle_time"],
        "run_id": identity["run_id"],
        "job_id": identity["job_id"],
        field: observed,
    }
    uri = _log_uri_for_scheme("published", **uri_identity)
    check["log_uri"] = uri
    check["published_log_read"]["log_uri"] = uri
    check["evidence"]["response"]["body"]["log_uri"] = uri
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") == field
        and blocker.get("observed") == (observed.lower() if field == "source" else observed)
        and blocker.get("evidence_key") == "log_uri"
        for blocker in blockers
    )


def test_logs_lane_canonical_log_uri_with_extra_segment_reports_missing_identity() -> None:
    run_id = _run_id("logs-canonical-extra-segment")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    check = logs_summary["sources"]["GFS"]["checks"]["job_logs"]
    identity = check["identity"]
    uri = (
        _log_uri_for_scheme(
            "published",
            source=identity["source"],
            cycle_time=identity["cycle_time"],
            run_id=identity["run_id"],
            job_id=identity["job_id"],
        )
        + "/tail"
    )
    check["log_uri"] = uri
    check["published_log_read"]["log_uri"] = uri
    check["evidence"]["response"]["body"]["log_uri"] = uri
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    blockers = logs_lane["blockers"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH" in _codes(blockers)
    assert any(
        blocker.get("field") in {"source", "run_id", "job_id"}
        and blocker.get("observed") is None
        and blocker.get("evidence_key") == "log_uri"
        for blocker in blockers
    )


@pytest.mark.parametrize(
    ("log_uri", "expected_code"),
    [
        ("/scratch/private/.nhms-runs/old/job.out", "TWO_NODE_E2E_LOGS_PRIVATE_LOG_URI"),
        ("file:///scratch/frd_muziyao/workspace/.nhms-runs/job.out", "TWO_NODE_E2E_LOGS_PRIVATE_LOG_URI"),
        ("https://logs.example/job.out", "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED"),
        ("published://../logs/job.out", "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSAFE"),
        ("published://logs/gfs/run/job.out?token=SECRET", "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED"),
        ("published://logs/gfs/run/job.out#SECRET", "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED"),
        ("published://user:pass@logs/gfs/run/job.out", "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED"),
        (
            "file:///mnt/nhms-published/logs/gfs/run/job.out?token=SECRET",
            "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED",
        ),
        (
            "s3://nhms-prod/published/logs/gfs/run/job.out?X-Amz-Signature=SECRET",
            "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED",
        ),
    ],
)
def test_logs_lane_rejects_private_or_unsupported_log_uri(log_uri: str, expected_code: str) -> None:
    run_id = _run_id("logs-private-uri")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    check = logs_summary["sources"]["GFS"]["checks"]["job_logs"]
    check["log_uri"] = log_uri
    check["evidence"]["response"]["body"]["log_uri"] = log_uri
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert expected_code in _codes(logs_lane["blockers"])
    assert "SECRET" not in json.dumps(logs_lane["blockers"])


def test_logs_lane_rejects_self_declared_private_file_publish_root() -> None:
    run_id = _run_id("logs-self-root")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    check = logs_summary["sources"]["GFS"]["checks"]["job_logs"]
    identity = check["identity"]
    private_root = Path("/scratch/private/workspace")
    log_uri = f"file://{private_root}/logs/gfs/{identity['run_id']}/{identity['job_id']}.out"
    check["published_artifact_root"] = str(private_root)
    check["log_uri"] = log_uri
    check["evidence"]["response"]["body"]["log_uri"] = log_uri
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    assert summary["status"] == STATUS_BLOCKED
    assert logs_lane["status"] == STATUS_BLOCKED
    assert "TWO_NODE_E2E_LOGS_PRIVATE_LOG_URI" in _codes(logs_lane["blockers"])


@pytest.mark.parametrize(
    "mutation",
    [
        "published_uri",
        "file_uri",
        "s3_uri",
        "typed_missing",
    ],
)
def test_logs_lane_accepts_published_log_uri_or_typed_unavailable(mutation: str) -> None:
    run_id = _run_id(f"logs-valid-{mutation}")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    for source, record in logs_summary["sources"].items():
        check = record["checks"]["job_logs"]
        identity = check["identity"]
        if mutation == "published_uri":
            uri = f"published://logs/{source.lower()}/{identity['run_id']}/{identity['job_id']}.out"
            check["log_uri"] = uri
            check["evidence"]["response"]["body"]["log_uri"] = uri
        elif mutation == "file_uri":
            root = Path("/mnt/nhms-published")
            uri = f"file://{root}/logs/{source.lower()}/{identity['run_id']}/{identity['job_id']}.out"
            check["published_artifact_root"] = str(root)
            check["log_uri"] = uri
            check["evidence"]["response"]["body"]["log_uri"] = uri
        elif mutation == "s3_uri":
            uri = f"s3://nhms-prod/published/logs/{source.lower()}/{identity['run_id']}/{identity['job_id']}.out"
            check["published_artifact_s3_bucket"] = "nhms-prod"
            check["published_artifact_s3_prefix"] = "published"
            check["log_uri"] = uri
            check["evidence"]["response"]["body"]["log_uri"] = uri
        else:
            check.pop("log_uri", None)
            check["evidence"]["response"] = {
                "status_code": 404,
                "error_code": "JOB_LOG_NOT_PUBLISHED",
                **copy.deepcopy(identity),
            }
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    assert summary["status"] == STATUS_PASS
    assert summary["lane_summaries"]["logs"]["status"] == STATUS_PASS


@pytest.mark.parametrize(
    ("readonly_proof", "expected_logs_status", "expected_final_status"),
    [
        (True, STATUS_PASS, STATUS_PASS),
        (False, STATUS_BLOCKED, STATUS_FAIL),
        (None, STATUS_BLOCKED, STATUS_BLOCKED),
    ],
)
def test_logs_lane_uses_docker_security_authoritative_published_log_root(
    readonly_proof: bool | None,
    expected_logs_status: str,
    expected_final_status: str,
) -> None:
    run_id = _run_id(f"logs-docker-root-{readonly_proof}")
    config = _seed_pass_bundle(run_id)
    published_root = Path("/var/nhms-published-test") / run_id
    docker_security = _read(config.run_dir / "docker-security" / "summary.json")
    docker_security["published_artifact_root"] = str(published_root)
    if readonly_proof is None:
        docker_security.pop("published_artifacts_readonly")
    else:
        docker_security["published_artifacts_readonly"] = readonly_proof
    _write(config.run_dir / "docker-security" / "summary.json", docker_security)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    for source, record in logs_summary["sources"].items():
        check = record["checks"]["job_logs"]
        identity = check["identity"]
        uri = f"file://{published_root}/logs/{source.lower()}/{identity['run_id']}/{identity['job_id']}.out"
        check.pop("published_artifact_root", None)
        check["log_uri"] = uri
        check["evidence"]["response"]["body"]["log_uri"] = uri
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    assert logs_lane["status"] == expected_logs_status
    assert summary["status"] == expected_final_status
    if expected_logs_status == STATUS_PASS:
        assert not logs_lane["blockers"]
    else:
        assert "TWO_NODE_E2E_LOGS_PRIVATE_LOG_URI" in _codes(logs_lane["blockers"])


@pytest.mark.parametrize(
    ("unavailable_cycle_time", "expected_status"),
    [
        (COMPACT_CYCLE_TIME, STATUS_PASS),
        (ISO_OFFSET_CYCLE_TIME, STATUS_PASS),
        (SHIFTED_CYCLE_TIME, STATUS_BLOCKED),
    ],
)
def test_logs_published_log_unavailable_binding_cycle_time_uses_timestamp_semantics(
    unavailable_cycle_time: str,
    expected_status: str,
) -> None:
    run_id = _run_id(f"logs-unavailable-cycle-{expected_status.lower()}")
    config = _seed_pass_bundle(run_id)
    logs_summary = _read(config.run_dir / "logs" / "summary.json")
    for record in logs_summary["sources"].values():
        check = record["checks"]["job_logs"]
        identity = copy.deepcopy(check["identity"])
        check.pop("log_uri", None)
        check.pop("published_log_read", None)
        unavailable = {
            "status_code": 404,
            "error_code": "JOB_LOG_NOT_PUBLISHED",
            "method": "GET",
            "path": f"/api/v1/jobs/{identity['job_id']}/logs",
            **identity,
        }
        if identity["source"] == "GFS":
            unavailable["cycle_time"] = unavailable_cycle_time
        check["evidence"]["response"] = unavailable
    _write(config.run_dir / "logs" / "summary.json", logs_summary)

    summary = validate_two_node_e2e_evidence(config)

    logs_lane = summary["lane_summaries"]["logs"]
    assert summary["status"] == expected_status
    assert logs_lane["status"] == expected_status
    if expected_status == STATUS_BLOCKED:
        assert "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_UNAVAILABLE_IDENTITY_MISMATCH" in _codes(logs_lane["blockers"])


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


@pytest.mark.parametrize(
    ("artifact_cycle_time", "expected_status"),
    [
        (COMPACT_CYCLE_TIME, STATUS_PASS),
        (ISO_OFFSET_CYCLE_TIME, STATUS_PASS),
        (SHIFTED_CYCLE_TIME, STATUS_BLOCKED),
    ],
)
def test_manual_ops_receipt_artifact_cycle_time_uses_timestamp_semantics(
    artifact_cycle_time: str,
    expected_status: str,
) -> None:
    run_id = _run_id(f"manual-artifact-cycle-{expected_status.lower()}")
    config = _seed_pass_bundle(run_id)
    manual_ops = _read(config.run_dir / "manual-ops" / "summary.json")
    receipt = next(item for item in manual_ops["control_receipts"] if item.get("source") == "GFS")
    artifact_path = Path(receipt["provenance"]["artifact_path"])
    artifact = _read(artifact_path)
    artifact["cycle_time"] = artifact_cycle_time
    _write(artifact_path, artifact)
    receipt["provenance"]["sha256"] = _sha256_file(artifact_path)
    _write(config.run_dir / "manual-ops" / "summary.json", manual_ops)

    summary = validate_two_node_e2e_evidence(config)

    manual_lane = summary["lane_summaries"]["manual_ops"]
    assert summary["status"] == expected_status
    assert manual_lane["status"] == expected_status
    if expected_status == STATUS_BLOCKED:
        assert "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_IDENTITY_MISMATCH" in _codes(manual_lane["blockers"])


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
            lane_name="api",
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
            lane_name="browser",
        ),
    )
    _write(
        config.run_dir / "logs" / "summary.json",
        _source_lane_payload(
            run_id,
            identities,
            required_checks=("job_logs",),
            live_flag="live_log_evidence",
            lane_name="logs",
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


def _producer_identity_query(identity: Mapping[str, str], check: str, *, include_job_id: bool = False) -> str:
    query = (
        f"source={identity['source']}&check={check}&run_id={identity['run_id']}"
        f"&cycle_time={identity['cycle_time']}&model_id={identity['model_id']}"
    )
    if include_job_id:
        query = f"{query}&job_id={identity['job_id']}"
    return query


def _producer_identity_text(
    identity: Mapping[str, str],
    check: str,
    *,
    source: str | None = None,
    run_id: str | None = None,
    job_id: str | None = None,
) -> str:
    parts = [
        "producer response",
        f"source={source or identity['source']}",
        f"check={check}",
        f"run_id={run_id or identity['run_id']}",
        f"cycle_time={identity['cycle_time']}",
        f"model_id={identity['model_id']}",
    ]
    if job_id is not None or "job_id" in identity:
        parts.append(f"job_id={job_id or identity['job_id']}")
    return " ".join(parts)


def _log_uri_for_scheme(
    uri_scheme: str,
    *,
    source: str,
    run_id: str,
    job_id: str,
    cycle_time: str | None = None,
    stream: str = "out",
) -> str:
    source_slug = source.lower()
    cycle_segment = f"{cycle_time}/" if cycle_time else ""
    if uri_scheme == "published":
        return f"published://logs/{source_slug}/{cycle_segment}{run_id}/{job_id}.{stream}"
    if uri_scheme == "file":
        return f"file:///mnt/nhms-published/logs/{source_slug}/{cycle_segment}{run_id}/{job_id}.{stream}"
    if uri_scheme == "s3":
        return f"s3://nhms-prod/published/logs/{source_slug}/{cycle_segment}{run_id}/{job_id}.{stream}"
    raise AssertionError(f"Unsupported log URI scheme: {uri_scheme}")


def _producer_lineage(identity: Mapping[str, str], check: str, *, include_job_id: bool = False) -> dict[str, str]:
    lineage = {
        "source": identity["source"],
        "check": check,
        "run_id": identity["run_id"],
        "cycle_time": identity["cycle_time"],
        "model_id": identity["model_id"],
    }
    if include_job_id:
        lineage["job_id"] = identity["job_id"]
    return lineage


def _complete_explicit_producer_record(identity: Mapping[str, str], check: str) -> dict[str, Any]:
    record = {
        "status_code": 200,
        "source": identity["source"],
        "check": check,
        "run_id": identity["run_id"],
        "cycle_time": identity["cycle_time"],
        "model_id": identity["model_id"],
        "path": f"/producer/{identity['source'].lower()}/{check}",
    }
    if check in {"job_logs", "ops_jobs", "ops_job_logs"}:
        record["job_id"] = identity["job_id"]
    return record


def _identity_only_producer_record(
    identity: Mapping[str, str],
    check: str,
    **overrides: str,
) -> dict[str, Any]:
    record = {
        "source": identity["source"],
        "check": check,
        "run_id": identity["run_id"],
        "cycle_time": identity["cycle_time"],
        "model_id": identity["model_id"],
    }
    if check in {"job_logs", "ops_jobs", "ops_job_logs"}:
        record["job_id"] = identity["job_id"]
    record.update(overrides)
    return record


def _structured_producer_metadata_record(
    identity: Mapping[str, str],
    check: str,
    **overrides: str,
) -> dict[str, Any]:
    record = {
        "status_code": 200,
        "source": identity["source"],
        "check": check,
        "run_id": identity["run_id"],
        "cycle_time": identity["cycle_time"],
        "model_id": identity["model_id"],
    }
    if check in {"job_logs", "ops_jobs", "ops_job_logs"}:
        record["job_id"] = identity["job_id"]
    record.update(overrides)
    return record


def _remove_explicit_producer_identity_fields(
    records: tuple[dict[str, Any], ...],
    fields: tuple[str, ...],
) -> None:
    for record in records:
        for field in fields:
            record.pop(field, None)
        identity = record.get("identity")
        if isinstance(identity, dict):
            for field in fields:
                identity.pop(field, None)


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


def _move_conflicting_docker_security_child_artifact(summary_payload: dict[str, Any], candidate_path: Path) -> None:
    source_artifacts = summary_payload.get("source_artifacts")
    assert isinstance(source_artifacts, dict)
    for artifact in source_artifacts.values():
        records = artifact if isinstance(artifact, list) else [artifact]
        for record in records:
            if not isinstance(record, dict):
                continue
            raw_path = record.get("path")
            if not isinstance(raw_path, str):
                continue
            artifact_path = Path(raw_path)
            if artifact_path.resolve() != candidate_path.resolve():
                continue
            child_payload = _read(candidate_path)
            child_path = candidate_path.with_name(f"{candidate_path.stem}-child{candidate_path.suffix}")
            _write(child_path, child_payload)
            record["path"] = str(child_path)
            record["sha256"] = _sha256_file(child_path)


def _source_lane_payload(
    run_id: str,
    identities: dict[str, dict[str, str]],
    *,
    required_checks: tuple[str, ...],
    live_flag: str,
    lane_name: str,
) -> dict[str, Any]:
    def _producer_request(source: str, check: str, identity: Mapping[str, str]) -> dict[str, Any]:
        query = (
            f"source={identity['source']}&check={check}&run_id={identity['run_id']}"
            f"&cycle_time={identity['cycle_time']}&model_id={identity['model_id']}"
        )
        if check in {"job_logs", "ops_jobs", "ops_job_logs"}:
            query = f"{query}&job_id={identity['job_id']}"
        return {
            "method": "GET",
            "path": f"/producer/{source.lower()}/{check}?{query}",
            "source": source,
            "check": check,
            **copy.deepcopy(identity),
        }

    def _producer_response(check: str, identity: Mapping[str, str]) -> dict[str, Any]:
        return {
            "status_code": 200,
            "source": identity["source"],
            "check": check,
            **copy.deepcopy(identity),
            **_log_response_fields(lane_name, check, identity),
        }

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
                            "evidence_run_id": run_id,
                            "request": _producer_request(source, check, identity),
                            "response": _producer_response(check, identity),
                        },
                        **_log_check_fields(lane_name, check, identity),
                    }
                    for check in required_checks
                },
            }
            for source, identity in identities.items()
        },
    }


def _log_check_fields(lane_name: str, check: str, identity: Mapping[str, str]) -> dict[str, Any]:
    if lane_name != "logs" or check != "job_logs":
        return {}
    log_uri = f"published://logs/{identity['source'].lower()}/{identity['run_id']}/{identity['job_id']}.out"
    return {
        "log_uri": log_uri,
        "published_log_read": {
            "status": STATUS_PASS,
            "status_code": 200,
            "log_uri": log_uri,
            "job_id": identity["job_id"],
        },
    }


def _log_response_fields(lane_name: str, check: str, identity: Mapping[str, str]) -> dict[str, Any]:
    if lane_name != "logs" or check != "job_logs":
        return {}
    log_uri = f"published://logs/{identity['source'].lower()}/{identity['run_id']}/{identity['job_id']}.out"
    return {
        "body": {
            "job_id": identity["job_id"],
            "log_uri": log_uri,
        }
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
