"""Auth/RBAC evidence coverage for production-ops validation.

Owns the canonical-action evidence assertions for ``services.production_closure.
ops_validation``: every canonical business mutation plus the four Slurm gateway
actions, the live-proof redaction/coverage proofs, and the stable blocker-id
invariant. The general lane/summary/redaction/rollback evidence stays in
``tests/test_production_ops_validation.py``, which imports the helpers from here.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from services.production_closure import ops_validation as ops_validation_module
from services.production_closure.ops_validation import (
    ProductionOpsConfig,
    ProductionOpsValidationError,
    validate_ops,
)
from tests.test_production_ops_validation import _assert_stable_auth_blocker_ids, _read_json


def test_validate_ops_auth_rbac_audit_and_release_blockers_are_complete(tmp_path: Path) -> None:
    validate_ops(ProductionOpsConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="auth"))
    lane_dir = tmp_path / "artifacts" / "auth" / "ops"
    auth = _read_json(lane_dir / "auth_rbac.json")
    blockers = _read_json(lane_dir / "auth_release_blockers.json")
    audit = _read_json(lane_dir / "audit_redaction.json")

    expected_actions = {
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
        # #1684 Slurm gateway mutations: canonical in ACTION_MATRIX, so the
        # production-ops auth evidence covers them like every other mutation.
        "slurm.submit_job",
        "slurm.cancel_job",
        "slurm.reset_registry",
    }
    assert set(auth["canonical_action_ids"]) == expected_actions
    assert set(auth["canonical_roles"]) == {"viewer", "analyst", "operator", "model_admin", "sys_admin"}
    assert {item["action_id"] for item in auth["action_decisions"]} == expected_actions
    assert {item["decision"] for item in auth["action_decisions"]} == {
        "allow",
        "deny",
        "release_blocked",
    }
    assert set(auth["execution_modes"]) == {"policy_simulated", "release_blocked"}
    assert auth["auth_readiness_execution_mode"] == "release_blocked"
    assert auth["live_backend_auth_executed"] is False
    assert auth["state_mutation_assertions"] == {
        "denied_actions_mutated_state": False,
        "release_blocked_actions_mutated_state": False,
    }
    for decision in auth["action_decisions"]:
        if decision["decision"] in {"denied", "deny", "release_blocked"}:
            assert decision["previous_state"] == decision["new_state"]
            assert decision["state_mutated"] is False
            assert decision["no_mutation_expected"] is True
            assert decision["error_code"] in {
                "PRODUCTION_OPS_AUTH_REQUIRED",
                "PRODUCTION_OPS_RBAC_FORBIDDEN",
                "PRODUCTION_OPS_BACKEND_AUTH_RELEASE_BLOCKED",
            }

    assert blockers["status"] == "release_blocked"
    assert {item["action_id"] for item in blockers["blockers"]} == expected_actions
    assert all(item["residual_risk"] and item["removal_criteria"] for item in blockers["blockers"])
    _assert_stable_auth_blocker_ids(auth, blockers, _read_json(lane_dir / "summary.json"))

    assert audit["status"] == "ready"
    assert len(audit["audit_rows"]) == len(auth["action_decisions"])
    assert set(audit["redaction_scope"]) == {
        "config",
        "logs",
        "manifests",
        "audit_rows",
        "api_payloads",
        "alert_payloads",
        "pr_evidence",
        "frontend_output",
    }
    first_row = audit["audit_rows"][0]
    assert {
        "actor",
        "actor_id",
        "roles",
        "action",
        "action_id",
        "target",
        "previous_state",
        "new_state",
        "decision",
        "reason",
        "reason_code",
        "execution_mode",
        "lineage",
    } <= set(first_row)
    assert {row["decision"] for row in audit["audit_rows"]} == {"allow", "deny", "release_blocked"}
    represented_surfaces = {
        "config": "config",
        "logs": "log_output",
        "manifests": "manifest_payload",
        "audit_rows": "audit_correlation_id",
        "api_payloads": "api_payload",
        "alert_payloads": "alert_payload",
        "pr_evidence": "pr_evidence",
        "frontend_output": "frontend_output",
    }
    first_lineage = first_row["lineage"]
    for field in represented_surfaces.values():
        assert field in first_lineage
    audit_text = json.dumps(audit)
    for surface, field in represented_surfaces.items():
        assert surface in audit["redaction_scope"]
        if surface != "audit_rows":
            assert "[redacted]" in json.dumps(first_lineage[field])
    assert "deterministic-secret-for-redaction-test" not in audit_text
    assert "deterministic-secret" not in audit_text



def test_validate_ops_auth_slurm_actions_are_canonical_with_distinct_policy_targets(
    tmp_path: Path,
) -> None:
    """The three Slurm gateway actions are canonical and policy-targeted.

    They join the production-ops auth evidence like every canonical mutation
    (ACTION_MATRIX is the single source), and each carries its own policy
    target type/id derivation (``slurm_gateway``/``job-submit``,
    ``job-cancel``, ``registry-reset``) so a future renaming reddens here
    instead of silently aliasing two actions to one target.
    """
    validate_ops(ProductionOpsConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="slurm_actions"))
    lane_dir = tmp_path / "artifacts" / "slurm_actions" / "ops"
    auth = _read_json(lane_dir / "auth_rbac.json")

    slurm_decisions = [
        decision for decision in auth["action_decisions"] if decision["action_id"].startswith("slurm.")
    ]
    assert {decision["action_id"] for decision in slurm_decisions} == {
        "slurm.submit_job",
        "slurm.cancel_job",
        "slurm.reset_registry",
    }
    # Each slurm decision carries a distinct per-action target identity and the
    # same canonical-role evidence as the business actions.
    target_by_action = {decision["action_id"]: decision["target"] for decision in slurm_decisions}
    assert target_by_action["slurm.submit_job"] == "slurm.submit_job:slurm_actions"
    assert target_by_action["slurm.cancel_job"] == "slurm.cancel_job:slurm_actions"
    assert target_by_action["slurm.reset_registry"] == "slurm.reset_registry:slurm_actions"
    # Scheduler bearer-role evidence: each slurm action has an operator role in
    # the canonical matrix (reset is sys_admin-only).
    from packages.common.auth_policy import ACTION_MATRIX

    assert "operator" in ACTION_MATRIX["slurm.submit_job"]
    assert "operator" in ACTION_MATRIX["slurm.cancel_job"]
    assert ACTION_MATRIX["slurm.reset_registry"] == ("sys_admin",)
    # The release blockers enumerate the slurm actions too.
    blockers = _read_json(lane_dir / "auth_release_blockers.json")
    assert {item["action_id"] for item in blockers["blockers"]} >= set(ACTION_MATRIX)


def test_validate_ops_auth_live_proof_emits_redacted_live_evidence(tmp_path: Path) -> None:
    proof = {
        "execution_mode": "live_proof",
        "live_backend_auth_executed": True,
        "provider": "oidc-prod",
        "provider_metadata": {
            "issuer": "https://idp.example/realms/nhms",
            "token": "live-token-secret",
            "jwks_uri": "https://idp.example/.well-known/jwks.json?credential=secret",
        },
        "allowed_subject": {
            "actor_id": "live-admin",
            "raw_roles": ["sys_admin", "external-admin"],
            "mapped_roles": ["sys_admin"],
            "role_mapping_result": {
                "source_claim": "groups",
                "credential_hint": "token=live-token-secret",
            },
        },
        "denied_subject": {
            "actor_id": "live-viewer",
            "raw_roles": ["viewer", "external-viewer"],
            "mapped_roles": ["viewer"],
            "role_mapping_result": {
                "source_claim": "groups",
                "credential_hint": "token=viewer-token-secret",
            },
        },
    }

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="live_proof_auth",
            auth_mode="backend_route_executed",
            auth_live_proof=proof,
        )
    )

    lane_dir = tmp_path / "artifacts" / "live_proof_auth" / "ops"
    auth = _read_json(lane_dir / "auth_rbac.json")
    blockers = _read_json(lane_dir / "auth_release_blockers.json")
    rendered_auth = json.dumps(auth)
    assert summary["live_backend_auth_executed"] is True
    assert summary["auth_readiness_execution_mode"] == "live_proof"
    assert not any(
        blocker.get("error_code") == "PRODUCTION_OPS_AUTH_LIVE_PROOF_COVERAGE_MISSING"
        for blocker in summary["release_blockers"]
    )
    assert auth["status"] == "ready"
    assert auth["live_backend_auth_executed"] is True
    assert auth["auth_readiness_execution_mode"] == "live_proof"
    assert auth["execution_modes"] == ["live_proof"]
    assert auth["live_proof"]["provider_metadata"]["provider"] == "oidc-prod"
    assert auth["live_proof"]["provider_metadata"]["token"] == "[redacted]"
    assert auth["live_proof"]["provider_metadata"]["jwks_uri"] == "[redacted]"
    assert auth["live_proof"]["role_mapping_result"]["mapped_roles"] == ["sys_admin"]
    assert auth["live_proof"]["role_mapping_result"]["unmapped_roles"] == ["external-admin"]
    assert auth["blockers"] == []
    assert {decision["decision"] for decision in auth["action_decisions"]} == {"allow", "deny"}
    for action in auth["canonical_action_ids"]:
        action_decisions = [decision for decision in auth["action_decisions"] if decision["action_id"] == action]
        assert any(
            decision["decision"] == "allow"
            and decision["auth_live_proof_subject"] == "allowed_subject"
            and decision["actor_id"] == "live-admin"
            and decision["roles"] == ["sys_admin"]
            and decision["live_backend_auth_executed"] is True
            and decision["provider_metadata"]["provider"] == "oidc-prod"
            and decision["role_mapping_result"]["mapping_status"] == "mapped"
            for decision in action_decisions
        )
        assert any(
            decision["decision"] == "deny"
            and decision["auth_live_proof_subject"] == "denied_subject"
            and decision["actor_id"] == "live-viewer"
            and decision["roles"] == ["viewer"]
            and decision["role_mapping_result"]["raw_roles"] == ["viewer", "external-viewer"]
            and "sys_admin" not in decision["role_mapping_result"]["raw_roles"]
            and decision["previous_state"] == decision["new_state"]
            and decision["state_mutated"] is False
            for decision in action_decisions
        )
    assert blockers["status"] == "ready"
    assert blockers["blockers"] == []
    assert "live-token-secret" not in rendered_auth
    assert "viewer-token-secret" not in rendered_auth
    assert "credential=secret" not in rendered_auth


def test_validate_ops_auth_live_proof_swapped_subject_labels_remains_blocked(tmp_path: Path) -> None:
    proof = {
        "execution_mode": "live_proof",
        "live_backend_auth_executed": True,
        "provider": "oidc-prod",
        "allowed_subject": {
            "actor_id": "live-viewer",
            "raw_roles": ["viewer"],
            "mapped_roles": ["viewer"],
        },
        "denied_subject": {
            "actor_id": "live-admin",
            "raw_roles": ["sys_admin"],
            "mapped_roles": ["sys_admin"],
        },
    }

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="swapped_live_proof_auth",
            auth_mode="backend_route_executed",
            auth_live_proof=proof,
        )
    )

    lane_dir = tmp_path / "artifacts" / "swapped_live_proof_auth" / "ops"
    auth = _read_json(lane_dir / "auth_rbac.json")
    blockers = _read_json(lane_dir / "auth_release_blockers.json")

    assert summary["status"] == "release_blocked"
    assert auth["status"] == "release_blocked"
    assert auth["auth_readiness_execution_mode"] == "release_blocked"
    assert summary["auth_readiness_execution_mode"] == "release_blocked"
    assert blockers["status"] == "release_blocked"
    assert {decision["auth_live_proof_subject"] for decision in auth["action_decisions"]} == {
        "allowed_subject",
        "denied_subject",
    }
    coverage_blockers = [
        blocker
        for blocker in blockers["blockers"]
        if blocker.get("error_code") == "PRODUCTION_OPS_AUTH_LIVE_PROOF_COVERAGE_MISSING"
    ]
    assert coverage_blockers
    assert all(
        set(blocker["missing_coverage"]) == {"allowed_live_proof", "denied_no_mutation_live_proof"}
        for blocker in coverage_blockers
    )
    assert all(
        blocker.get("error_code") != "PRODUCTION_OPS_AUTH_LIVE_PROOF_SUBJECT_MISSING_OR_INVALID"
        for blocker in blockers["blockers"]
    )
    assert any(
        blocker.get("error_code") == "PRODUCTION_OPS_AUTH_LIVE_PROOF_COVERAGE_MISSING"
        and set(blocker["missing_coverage"]) == {"allowed_live_proof", "denied_no_mutation_live_proof"}
        for blocker in summary["release_blockers"]
    )


def test_validate_ops_auth_live_proof_partial_action_coverage_remains_blocked(tmp_path: Path) -> None:
    proof = {
        "execution_mode": "live_proof",
        "live_backend_auth_executed": True,
        "provider": "oidc-prod",
        "allowed_subject": {
            "actor_id": "live-operator",
            "raw_roles": ["operator"],
            "mapped_roles": ["operator"],
        },
        "denied_subject": {
            "actor_id": "live-viewer",
            "raw_roles": ["viewer"],
            "mapped_roles": ["viewer"],
        },
    }

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="partial_live_proof_auth",
            auth_mode="backend_route_executed",
            auth_live_proof=proof,
        )
    )

    lane_dir = tmp_path / "artifacts" / "partial_live_proof_auth" / "ops"
    auth = _read_json(lane_dir / "auth_rbac.json")
    blockers = _read_json(lane_dir / "auth_release_blockers.json")

    assert summary["status"] == "release_blocked"
    assert auth["status"] == "release_blocked"
    assert blockers["status"] == "release_blocked"
    missing_allowed_actions = {
        blocker["action_id"]
        for blocker in blockers["blockers"]
        if "allowed_live_proof" in blocker["missing_coverage"]
    }
    assert {
        "sources.update_config",
        "models.activate",
        "models.deactivate",
        "models.switch_version",
        "models.rollback_version",
        "models.supersede",
        "users.manage",
    }.issubset(missing_allowed_actions)
    assert all(
        "denied_no_mutation_live_proof" not in blocker["missing_coverage"]
        for blocker in blockers["blockers"]
    )
    assert missing_allowed_actions.issubset(
        {
            blocker["action_id"]
            for blocker in summary["release_blockers"]
            if blocker.get("error_code") == "PRODUCTION_OPS_AUTH_LIVE_PROOF_COVERAGE_MISSING"
        }
    )
    summary_coverage_keys = [
        (
            blocker["error_code"],
            blocker["action_id"],
            tuple(blocker["missing_coverage"]),
        )
        for blocker in summary["release_blockers"]
        if blocker.get("error_code") == "PRODUCTION_OPS_AUTH_LIVE_PROOF_COVERAGE_MISSING"
    ]
    assert all(count == 1 for count in Counter(summary_coverage_keys).values())
    summary_coverage_ids = [
        blocker["blocker_id"]
        for blocker in summary["release_blockers"]
        if blocker.get("error_code") == "PRODUCTION_OPS_AUTH_LIVE_PROOF_COVERAGE_MISSING"
    ]
    assert all(blocker_id.startswith("m17-auth:live-proof-coverage:") for blocker_id in summary_coverage_ids)
    assert all(count == 1 for count in Counter(summary_coverage_ids).values())
    _assert_stable_auth_blocker_ids(auth, blockers, summary)


def test_validate_ops_auth_live_proof_requires_explicit_allowed_subject(tmp_path: Path) -> None:
    proof = {
        "execution_mode": "live_proof",
        "live_backend_auth_executed": True,
        "provider": "oidc-prod",
        "actor_id": "legacy-live-admin",
        "raw_roles": ["sys_admin"],
        "mapped_roles": ["sys_admin"],
        "denied_subject": {
            "actor_id": "live-viewer",
            "raw_roles": ["viewer"],
            "mapped_roles": ["viewer"],
        },
    }

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="missing_allowed_subject_live_proof_auth",
            auth_mode="backend_route_executed",
            auth_live_proof=proof,
        )
    )

    lane_dir = tmp_path / "artifacts" / "missing_allowed_subject_live_proof_auth" / "ops"
    auth = _read_json(lane_dir / "auth_rbac.json")
    blockers = _read_json(lane_dir / "auth_release_blockers.json")

    assert auth["status"] == "release_blocked"
    assert blockers["status"] == "release_blocked"
    assert "allowed" not in auth["live_proof"]["subjects"]
    assert auth["live_proof"]["subjects"]["denied"]["actor_id"] == "live-viewer"

    allowed_subject_blocker = next(
        blocker
        for blocker in summary["release_blockers"]
        if blocker.get("error_code") == "PRODUCTION_OPS_AUTH_LIVE_PROOF_SUBJECT_MISSING_OR_INVALID"
        and blocker.get("subject") == "allowed_subject"
    )
    assert "Missing or invalid explicit allowed_subject" in allowed_subject_blocker["message"]
    assert any(
        blocker.get("error_code") == "PRODUCTION_OPS_AUTH_LIVE_PROOF_SUBJECT_MISSING_OR_INVALID"
        and blocker.get("subject") == "allowed_subject"
        for blocker in blockers["blockers"]
    )


def test_validate_ops_auth_live_proof_rejects_incomplete_explicit_subject_objects(tmp_path: Path) -> None:
    proof = {
        "execution_mode": "live_proof",
        "live_backend_auth_executed": True,
        "provider": "oidc-prod",
        "actor_id": "legacy-live-admin",
        "allowed_subject": {
            "raw_roles": ["sys_admin"],
            "mapped_roles": ["sys_admin"],
        },
        "denied_subject": {
            "actor_id": "live-viewer",
            "mapped_roles": ["viewer"],
        },
    }

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="incomplete_subject_live_proof_auth",
            auth_mode="backend_route_executed",
            auth_live_proof=proof,
        )
    )

    lane_dir = tmp_path / "artifacts" / "incomplete_subject_live_proof_auth" / "ops"
    auth = _read_json(lane_dir / "auth_rbac.json")
    blockers = _read_json(lane_dir / "auth_release_blockers.json")

    assert summary["status"] == "release_blocked"
    assert auth["status"] == "release_blocked"
    assert blockers["status"] == "release_blocked"
    assert auth["live_proof"]["subjects"] == {}
    for subject in ("allowed_subject", "denied_subject"):
        assert any(
            blocker.get("error_code") == "PRODUCTION_OPS_AUTH_LIVE_PROOF_SUBJECT_MISSING_OR_INVALID"
            and blocker.get("subject") == subject
            for blocker in auth["blockers"]
        )
        assert any(
            blocker.get("error_code") == "PRODUCTION_OPS_AUTH_LIVE_PROOF_SUBJECT_MISSING_OR_INVALID"
            and blocker.get("subject") == subject
            for blocker in blockers["blockers"]
        )
        assert any(
            blocker.get("error_code") == "PRODUCTION_OPS_AUTH_LIVE_PROOF_SUBJECT_MISSING_OR_INVALID"
            and blocker.get("subject") == subject
            for blocker in summary["release_blockers"]
        )


def test_validate_ops_auth_live_proof_rejects_unbounded_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ops_validation_module, "MAX_AUTH_LIVE_PROOF_BYTES", 128)
    oversized_proof = json.dumps(
        {
            "execution_mode": "live_proof",
            "live_backend_auth_executed": True,
            "allowed_subject": {"actor_id": "a", "raw_roles": ["sys_admin"], "mapped_roles": ["sys_admin"]},
            "denied_subject": {"actor_id": "d", "raw_roles": ["viewer"], "mapped_roles": ["viewer"]},
            "provider_metadata": {"token": "x" * 256},
        }
    )
    with pytest.raises(ProductionOpsValidationError) as size_exc:
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="oversized_live_proof_auth",
            auth_live_proof=oversized_proof,
        )
    assert size_exc.value.error_code == "PRODUCTION_OPS_AUTH_LIVE_PROOF_TOO_LARGE"

    with pytest.raises(ProductionOpsValidationError) as mapping_size_exc:
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="oversized_mapping_live_proof_auth",
            auth_live_proof={
                "execution_mode": "live_proof",
                "live_backend_auth_executed": True,
                "allowed_subject": {"actor_id": "a", "raw_roles": ["sys_admin"], "mapped_roles": ["sys_admin"]},
                "denied_subject": {"actor_id": "d", "raw_roles": ["viewer"], "mapped_roles": ["viewer"]},
                "provider_metadata": {"token": "x" * 256},
            },
        )
    assert mapping_size_exc.value.error_code == "PRODUCTION_OPS_AUTH_LIVE_PROOF_TOO_LARGE"

    monkeypatch.setattr(ops_validation_module, "MAX_AUTH_LIVE_PROOF_DEPTH", 3)
    with pytest.raises(ProductionOpsValidationError) as depth_exc:
        validate_ops(
            ProductionOpsConfig.from_env(
                evidence_root=tmp_path / "artifacts",
                run_id="deep_live_proof_auth",
                auth_mode="backend_route_executed",
                auth_live_proof={
                    "execution_mode": "live_proof",
                    "live_backend_auth_executed": True,
                    "allowed_subject": {
                        "actor_id": "a",
                        "raw_roles": ["sys_admin"],
                        "mapped_roles": ["sys_admin"],
                        "provider_metadata": {"nested": {"too": "deep"}},
                    },
                    "denied_subject": {
                        "actor_id": "d",
                        "raw_roles": ["viewer"],
                        "mapped_roles": ["viewer"],
                    },
                },
            )
        )
    assert depth_exc.value.error_code == "PRODUCTION_OPS_AUTH_LIVE_PROOF_INVALID"


def test_validate_ops_auth_live_proof_inconsistent_same_actor_roles_remains_blocked(tmp_path: Path) -> None:
    proof = {
        "execution_mode": "live_proof",
        "live_backend_auth_executed": True,
        "provider": "oidc-prod",
        "allowed_subject": {
            "actor_id": "live-user",
            "raw_roles": ["sys_admin"],
            "mapped_roles": ["sys_admin"],
        },
        "denied_subject": {
            "actor_id": "live-user",
            "raw_roles": ["viewer"],
            "mapped_roles": ["viewer"],
        },
    }

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="inconsistent_live_proof_auth",
            auth_mode="backend_route_executed",
            auth_live_proof=proof,
        )
    )

    lane_dir = tmp_path / "artifacts" / "inconsistent_live_proof_auth" / "ops"
    auth = _read_json(lane_dir / "auth_rbac.json")
    blockers = _read_json(lane_dir / "auth_release_blockers.json")

    assert auth["status"] == "release_blocked"
    assert blockers["status"] == "release_blocked"
    assert not any(
        blocker.get("error_code") == "PRODUCTION_OPS_AUTH_LIVE_PROOF_COVERAGE_MISSING"
        for blocker in blockers["blockers"]
    )

    auth_blocker = next(
        blocker
        for blocker in blockers["blockers"]
        if blocker.get("error_code") == "PRODUCTION_OPS_AUTH_LIVE_PROOF_SUBJECT_IDENTITY_INCONSISTENT"
    )
    assert auth_blocker["actor_id"] == "live-user"
    assert auth_blocker["allowed_role_evidence"]["mapped_roles"] == ["sys_admin"]
    assert auth_blocker["denied_role_evidence"]["mapped_roles"] == ["viewer"]
    assert "Inconsistent live-proof subject identity/role mapping" in auth_blocker["message"]
    assert any(
        blocker.get("error_code") == "PRODUCTION_OPS_AUTH_LIVE_PROOF_SUBJECT_IDENTITY_INCONSISTENT"
        for blocker in summary["release_blockers"]
    )
