from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.production_closure import slurm_validation
from services.production_closure.readiness_validation import (
    ALLOWED_STATUS_EXECUTION_MODES,
    EXECUTION_MODE_VALUES,
    ProductionReadinessConfig,
    ProductionReadinessValidationError,
    validate_readiness,
    validate_readiness_item,
)

LIVE_SCHEMA = "nhms.production_readiness.live_proof.v1"
PROOF_SURFACES = {
    "auth": "live_backend_auth",
    "alert": "live_alert_sink_delivery",
    "rollback": "live_rollback_execution",
    "slurm": "live_slurm_dependency_proof",
    "object_store": "live_object_store_dependency_proof",
    "source": "live_source_weather_dependency_proof",
    "e2e": "live_e2e_dependency_proof",
    "mvt": "live_mvt_performance_proof",
    "target_env": "target_environment_config_proof",
}
DEPENDENCY_PROOFS = {"slurm", "object_store", "source", "e2e", "mvt"}


def _summary(root: Path, run_id: str = "m19") -> dict[str, object]:
    return json.loads((root / run_id / "readiness" / "summary.json").read_text(encoding="utf-8"))


def _items(root: Path, run_id: str = "m19") -> list[dict[str, object]]:
    payload = json.loads((root / run_id / "readiness" / "readiness_items.json").read_text(encoding="utf-8"))
    return list(payload["items"])


def _blockers(root: Path, run_id: str = "m19") -> list[dict[str, object]]:
    payload = json.loads((root / run_id / "readiness" / "release_blockers.json").read_text(encoding="utf-8"))
    return list(payload["blockers"])


def _base_item(status: str, execution_mode: str) -> dict[str, object]:
    return {
        "surface": "unit",
        "status": status,
        "execution_mode": execution_mode,
        "required_for_final": False,
        "live_proof_accepted": False,
        "artifact_refs": [],
        "residual_risk": "unit residual risk",
        "removal_criteria": "unit removal criteria",
        "exclusions": [],
    }


def _bound_proof(proof_key: str, *, run_id: str = "m19", **extra: object) -> str:
    payload: dict[str, object] = {
        "schema": LIVE_SCHEMA,
        "proof_type": "dependency" if proof_key in DEPENDENCY_PROOFS else proof_key,
        "surface": PROOF_SURFACES[proof_key],
        "run_id": run_id,
        "target_environment": "production",
        "execution_mode": "live_proof",
        "accepted": True,
        "status": "passed",
        "artifact_refs": [f"evidence/{run_id}/{proof_key}/receipt.json"],
    }
    if proof_key in DEPENDENCY_PROOFS:
        payload |= {
            "dependency_surface": proof_key,
            "provenance": {"producer_run_id": f"{proof_key}-live-run", "summary_checksum": "sha256:abc123"},
        }
    elif proof_key == "alert":
        payload |= {
            "sink_metadata": {"sink": "pagerduty-prod", "channel": "release-readiness"},
            "delivery_metadata": {"message_id": "alert-123", "delivered_at": "2026-05-21T00:00:00Z"},
            "delivered": True,
        }
    elif proof_key == "rollback":
        payload |= {
            "preconditions": {"backup_verified": True, "freeze_window": "approved"},
            "command_metadata": {"command": "nhms-production rollback --dry-run=false", "drill_id": "rb-123"},
            "executed": True,
        }
    elif proof_key == "target_env":
        payload |= {
            "config_metadata": {"cluster": "shudhpc", "object_store": "prod", "database": "prod-postgis"},
            "config_receipt_id": "target-env-123",
        }
    payload |= extra
    return json.dumps(payload)


def _auth_proof(*, allowed: list[str] | None = None, denied: list[str] | None = None, **extra: object) -> str:
    payload: dict[str, object] = {
        "schema": LIVE_SCHEMA,
        "proof_type": "auth",
        "surface": "live_backend_auth",
        "run_id": "m19",
        "target_environment": "production",
        "execution_mode": "live_proof",
        "artifact_refs": ["evidence/m19/auth/receipt.json"],
        "status": "passed",
        "accepted": True,
        "provider": {
            "issuer_url": "https://user:pass@idp.example.invalid/auth?token=secret",
            "client_secret": "super-secret",
        },
        "role_mapping": {"operator": ["pipeline.retry_run"], "model_admin": ["models.activate"]},
        "allowed_actions": allowed or [],
        "denied_actions": denied or [],
        **extra,
    }
    return json.dumps(payload)


def _all_live_proofs() -> dict[str, str]:
    actions = _all_auth_actions()
    return {
        "auth_proof": _auth_proof(allowed=actions, denied=actions),
        "alert_proof": _bound_proof("alert"),
        "rollback_proof": _bound_proof("rollback"),
        "slurm_proof": _bound_proof("slurm"),
        "object_store_proof": _bound_proof("object_store"),
        "source_proof": _bound_proof("source"),
        "e2e_proof": _bound_proof("e2e"),
        "mvt_proof": _bound_proof("mvt"),
        "target_env_proof": _bound_proof("target_env"),
    }


def _all_auth_actions() -> list[str]:
    return [
        "pipeline.retry_run",
        "pipeline.cancel_run",
        "pipeline.rerun_cycle",
        "qc.override_result",
        "tiles.republish",
        "sources.update_config",
        "models.activate",
        "models.deactivate",
        "models.switch_version",
        "models.rollback_version",
        "models.supersede",
        "users.manage",
    ]


def test_status_execution_mode_truth_table_accepts_allowed_and_rejects_forbidden() -> None:
    for status, modes in ALLOWED_STATUS_EXECUTION_MODES.items():
        for mode in modes:
            validate_readiness_item(_base_item(status, mode))

    for status in ALLOWED_STATUS_EXECUTION_MODES:
        forbidden = EXECUTION_MODE_VALUES - ALLOWED_STATUS_EXECUTION_MODES[status]
        for mode in forbidden:
            with pytest.raises(ProductionReadinessValidationError):
                validate_readiness_item(_base_item(status, mode))

    with pytest.raises(ProductionReadinessValidationError):
        validate_readiness_item(_base_item("not-a-status", "deterministic"))
    with pytest.raises(ProductionReadinessValidationError):
        validate_readiness_item(_base_item("passed", "not-a-mode"))
    missing = _base_item("passed", "deterministic")
    missing.pop("removal_criteria")
    with pytest.raises(ProductionReadinessValidationError):
        validate_readiness_item(missing)


def test_default_readiness_lane_is_deterministic_release_blocked_and_side_effect_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "token=secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret")
    root = tmp_path / "artifacts"

    exit_code = slurm_validation.main(["validate-readiness", "--evidence-root", str(root), "--run-id", "m19"])

    assert exit_code == 0
    stdout = capsys.readouterr().out
    assert "super-secret" not in stdout
    assert "token=secret" not in stdout
    rendered_summary = json.loads(stdout)
    assert rendered_summary["status"] == "release_blocked"
    assert rendered_summary["final_production_readiness_claimed"] is False
    assert rendered_summary["live_proof_item_count"] == 0

    items = _items(root)
    deterministic = [item for item in items if item["required_for_final"] is False and item["status"] == "passed"]
    assert deterministic
    assert all(item["execution_mode"] != "live_proof" for item in deterministic)
    required_live = [item for item in items if item["required_for_final"] is True]
    assert required_live
    assert all(item["status"] == "release_blocked" for item in required_live)
    assert all(item["execution_mode"] == "not_executed" for item in required_live)
    assert all(item["removal_criteria"] for item in required_live)
    assert all(item["owner"] for item in required_live)
    assert all(item["action"] for item in required_live)

    preflight = json.loads((root / "m19" / "readiness" / "preflight.json").read_text(encoding="utf-8"))
    policy = preflight["fast_ci_live_side_effect_policy"]
    assert policy == {
        "executes_live_idp": False,
        "executes_live_alert_sink": False,
        "executes_backend_mutation": False,
        "executes_live_rollback": False,
        "executes_live_slurm": False,
        "executes_live_object_store": False,
        "executes_live_weather_source": False,
        "executes_real_national_data": False,
    }

    artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").iterdir())
    assert "super-secret" not in artifact_text
    assert "token=secret" not in artifact_text


def test_exclusions_are_not_failed_and_do_not_satisfy_live_proof(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    validate_readiness(ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19"))

    exclusions = _summary(root)["exclusions"]
    exclusion_ids = {exclusion["id"] for exclusion in exclusions}
    assert {"cldas-restricted", "real-national-data-incomplete"} <= exclusion_ids
    exclusion_items = [item for item in _items(root) if item["exclusions"]]
    assert {item["status"] for item in exclusion_items} == {"not_executed"}
    assert all(item["execution_mode"] == "not_executed" for item in exclusion_items)
    assert all(item["live_proof_accepted"] is False for item in exclusion_items)


def test_incomplete_live_auth_receipt_is_redacted_and_remains_release_blocked(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            auth_proof=_auth_proof(allowed=["models.activate"], denied=[]),
        )
    )

    auth_item = next(item for item in _items(root) if item["surface"] == "live_backend_auth")
    assert auth_item["status"] == "release_blocked"
    assert auth_item["execution_mode"] == "live_proof"
    assert auth_item["live_proof_accepted"] is False
    assert "missing_allowed_actions" in auth_item["details"]["acceptance_errors"]
    assert _summary(root)["final_production_readiness_claimed"] is False

    evidence = (root / "m19" / "readiness" / "live_proof_receipts.json").read_text(encoding="utf-8")
    assert "super-secret" not in evidence
    assert "token=secret" not in evidence
    assert "user:pass@" not in evidence
    assert "https://idp.example.invalid/auth" in evidence


def test_malformed_and_oversized_live_proofs_are_bounded_release_blockers(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    oversized = "x" * (70 * 1024)
    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            auth_proof="{not-json-token=secret",
            alert_proof=oversized,
        )
    )

    items = _items(root)
    auth_item = next(item for item in items if item["surface"] == "live_backend_auth")
    alert_item = next(item for item in items if item["surface"] == "live_alert_sink_delivery")
    assert auth_item["status"] == "release_blocked"
    assert auth_item["execution_mode"] == "live_proof"
    assert alert_item["status"] == "release_blocked"
    assert alert_item["execution_mode"] == "live_proof"

    receipts = (root / "m19" / "readiness" / "live_proof_receipts.json").read_text(encoding="utf-8")
    assert "not-json-token=secret" not in receipts
    assert "[redacted]" in receipts
    assert len(receipts) < 20_000
    assert "Traceback" not in receipts


@pytest.mark.parametrize(
    ("proof_arg", "surface"),
    [
        ("alert_proof", "live_alert_sink_delivery"),
        ("rollback_proof", "live_rollback_execution"),
        ("slurm_proof", "live_slurm_dependency_proof"),
        ("object_store_proof", "live_object_store_dependency_proof"),
        ("source_proof", "live_source_weather_dependency_proof"),
        ("e2e_proof", "live_e2e_dependency_proof"),
        ("mvt_proof", "live_mvt_performance_proof"),
        ("target_env_proof", "target_environment_config_proof"),
    ],
)
@pytest.mark.parametrize("minimal", [{"accepted": True}, {"accepted": True, "status": "passed"}])
def test_minimal_accepted_live_receipts_remain_release_blocked(
    tmp_path: Path,
    proof_arg: str,
    surface: str,
    minimal: dict[str, object],
) -> None:
    root = tmp_path / "artifacts"
    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", **{proof_arg: json.dumps(minimal)})
    )

    item = next(item for item in _items(root) if item["surface"] == surface)
    assert item["status"] == "release_blocked"
    assert item["execution_mode"] == "live_proof"
    assert item["live_proof_accepted"] is False
    assert "missing_artifact_or_evidence_refs" in item["details"]["acceptance_errors"]["errors"]
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("proof_arg", "proof_key", "surface"),
    [
        ("alert_proof", "alert", "live_alert_sink_delivery"),
        ("slurm_proof", "slurm", "live_slurm_dependency_proof"),
        ("target_env_proof", "target_env", "target_environment_config_proof"),
    ],
)
def test_wrong_surface_target_schema_or_deterministic_mode_blocks_live_receipt(
    tmp_path: Path,
    proof_arg: str,
    proof_key: str,
    surface: str,
) -> None:
    root = tmp_path / "artifacts"
    receipt = _bound_proof(
        proof_key,
        schema="nhms.production_readiness.live_proof.v0",
        surface="live_sibling_surface",
        run_id="stale-run",
        target_environment="staging",
        execution_mode="deterministic",
    )

    validate_readiness(ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", **{proof_arg: receipt}))

    item = next(item for item in _items(root) if item["surface"] == surface)
    errors = item["details"]["acceptance_errors"]["errors"]
    assert item["status"] == "release_blocked"
    assert item["live_proof_accepted"] is False
    assert "schema_mismatch" in errors
    assert "surface_mismatch" in errors
    assert "run_id_mismatch" in errors
    assert "target_environment_mismatch" in errors
    assert "execution_mode_not_live_proof" in errors
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_embedded_local_paths_are_redacted_from_receipts_and_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "artifacts"
    unix_path = "/tmp/nhms/private/proof.json"
    windows_path = r"C:\Users\release\secret\proof.json"
    unc_path = r"\\prod-share\release\proof.json"
    file_uri = "file:///var/lib/nhms/receipt.json"
    exit_code = slurm_validation.main(
        [
            "validate-readiness",
            "--evidence-root",
            str(root),
            "--run-id",
            "m19",
            "--alert-proof",
            _bound_proof(
                "alert",
                accepted=False,
                status="failed",
                message=f"failed reading {unix_path} {windows_path} {unc_path} {file_uri}",
            ),
        ]
    )

    assert exit_code == 0
    combined = capsys.readouterr().out
    artifact_text = combined + "\n".join(
        path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").iterdir()
    )
    for raw_path in (unix_path, windows_path, unc_path, file_uri):
        assert raw_path not in artifact_text
    assert "[redacted-path]" in artifact_text


@pytest.mark.parametrize("proof_arg", ["auth_proof", "alert_proof"])
def test_deep_live_proof_json_is_stable_release_blocker(tmp_path: Path, proof_arg: str) -> None:
    root = tmp_path / "artifacts"
    deep_json = "[" * 20000 + "0" + "]" * 20000
    validate_readiness(ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", **{proof_arg: deep_json}))

    surface = "live_backend_auth" if proof_arg == "auth_proof" else "live_alert_sink_delivery"
    item = next(item for item in _items(root) if item["surface"] == surface)
    assert item["status"] == "release_blocked"
    assert item["execution_mode"] == "live_proof"
    assert item["live_proof_accepted"] is False
    receipts = (root / "m19" / "readiness" / "live_proof_receipts.json").read_text(encoding="utf-8")
    assert len(receipts) < 20_000
    assert "Traceback" not in receipts
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_all_live_receipts_accepted_claims_final_readiness(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            **_all_live_proofs(),
        )
    )

    summary = _summary(root)
    assert summary["final_production_readiness_claimed"] is True
    assert summary["status"] == "ready"
    assert summary["release_blockers"] == []
    assert summary["accepted_live_proof_count"] == summary["required_live_proof_count"] == 9


def test_any_required_live_blocker_keeps_final_readiness_false_and_lists_blocker(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    proofs = _all_live_proofs()
    proofs.pop("target_env_proof")
    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            **proofs,
        )
    )

    summary = _summary(root)
    assert summary["final_production_readiness_claimed"] is False
    blockers = _blockers(root)
    target_blocker = next(blocker for blocker in blockers if blocker["surface"] == "target_environment_config_proof")
    assert target_blocker["blocker_id"] == "m19-live-target-environment-config"
    assert target_blocker["residual_risk"]
    assert target_blocker["removal_criteria"]
    assert target_blocker["artifact_refs"] == ["live_proof_receipts.json"]


def test_consumes_existing_lane_summaries_without_changing_final_live_gate(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    lanes = {
        "slurm": ("nhms.production_closure.slurm.v1", 147, "ready"),
        "object-store": ("nhms.production_closure.object_store.v1", 148, "ready"),
        "source": ("nhms.production_closure.met.v1", 149, "ready"),
        "e2e": ("nhms.production_closure.e2e.v1", 150, "ready"),
        "mvt": ("nhms.production_closure.scale.v1", 151, "ready"),
    }
    for lane, (schema, issue, status) in lanes.items():
        lane_root = tmp_path / lane
        lane_root.mkdir()
        (lane_root / "summary.json").write_text(
            json.dumps(
                {
                    "schema": schema,
                    "issue": issue,
                    "run_id": f"{lane}-run",
                    "status": status,
                    "execution_mode": "deterministic_fixture",
                    "final_production_readiness_claimed": False,
                }
            ),
            encoding="utf-8",
        )

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            slurm_evidence_root=tmp_path / "slurm",
            object_store_evidence_root=tmp_path / "object-store",
            source_evidence_root=tmp_path / "source",
            e2e_evidence_root=tmp_path / "e2e",
            mvt_evidence_root=tmp_path / "mvt",
        )
    )

    summary = _summary(root)
    assert summary["final_production_readiness_claimed"] is False
    consumed = [item for item in _items(root) if item["surface"].endswith("_production_like_evidence")]
    assert len(consumed) == 5
    assert all(item["status"] == "passed" for item in consumed)
    assert all(item["execution_mode"] == "deterministic" for item in consumed)
    assert all(item["live_proof_accepted"] is False for item in consumed)


def test_deep_dependency_summary_json_is_stable_blocked_evidence(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    summary_root = tmp_path / "slurm"
    summary_root.mkdir()
    (summary_root / "summary.json").write_text("[" * 20000 + "0" + "]" * 20000, encoding="utf-8")

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", slurm_evidence_root=summary_root)
    )

    item = next(item for item in _items(root) if item["surface"] == "slurm_production_like_evidence")
    assert item["status"] == "blocked"
    assert item["execution_mode"] == "not_executed"
    assert item["live_proof_accepted"] is False
    artifacts = "\n".join(path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").iterdir())
    assert "Traceback" not in artifacts
    assert _summary(root)["final_production_readiness_claimed"] is False
