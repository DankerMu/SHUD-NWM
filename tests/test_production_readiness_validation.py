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


def _proof(**extra: object) -> str:
    payload = {"accepted": True, "status": "passed", **extra}
    return json.dumps(payload)


def _auth_proof(*, allowed: list[str] | None = None, denied: list[str] | None = None, **extra: object) -> str:
    payload = {
        "accepted": True,
        "provider": {
            "issuer_url": "https://user:pass@idp.example.invalid/auth?token=secret",
            "client_secret": "super-secret",
        },
        "allowed_actions": allowed or [],
        "denied_actions": denied or [],
        **extra,
    }
    return json.dumps(payload)


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


def test_all_live_receipts_accepted_claims_final_readiness(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    actions = _all_auth_actions()
    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            auth_proof=_auth_proof(allowed=actions, denied=actions),
            alert_proof=_proof(delivered=True),
            rollback_proof=_proof(executed=True),
            slurm_proof=_proof(),
            object_store_proof=_proof(),
            source_proof=_proof(),
            e2e_proof=_proof(),
            mvt_proof=_proof(),
            target_env_proof=_proof(),
        )
    )

    summary = _summary(root)
    assert summary["final_production_readiness_claimed"] is True
    assert summary["status"] == "ready"
    assert summary["release_blockers"] == []
    assert summary["accepted_live_proof_count"] == summary["required_live_proof_count"] == 9


def test_any_required_live_blocker_keeps_final_readiness_false_and_lists_blocker(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    actions = _all_auth_actions()
    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            auth_proof=_auth_proof(allowed=actions, denied=actions),
            alert_proof=_proof(delivered=True),
            rollback_proof=_proof(executed=True),
            slurm_proof=_proof(),
            object_store_proof=_proof(),
            source_proof=_proof(),
            e2e_proof=_proof(),
            mvt_proof=_proof(),
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
