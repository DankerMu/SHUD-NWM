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
            "status": STATUS_PASS,
            "evidence_run_id": run_id,
            "docker_root_dir": "/var/lib/docker",
            "commands": {
                "docker_version": {"returncode": 0},
                "docker_compose_version": {"returncode": 0},
                "docker_info_docker_root": {"returncode": 0},
                "docker_system_df": {"returncode": 0},
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
            "published_artifacts_readonly": True,
            "docker_socket_present": False,
            "slurm_cli_present": False,
            "slurm_config_present": False,
            "munge_path_present": False,
            "writable_published_artifact_mount": False,
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
    permission_probes = [
        {
            "target": "hydro.hydro_run",
            "operations": [
                {
                    "operation": "INSERT",
                    "status": STATUS_PASS,
                    "privilege_allowed": False,
                    "execution_outcome": "permission_denied",
                }
            ],
        }
    ]
    route_smoke = [{"name": "latest_product", "status": STATUS_PASS, "identity": first_identity}]
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
            "manual_action_probes": [{"action": "retry", "status": STATUS_PASS}],
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
