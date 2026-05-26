from __future__ import annotations

import pytest

from apps.api.auth import ACTION_MATRIX, ROLE_VOCABULARY, AuthContext, evaluate_policy, redact_audit_payload


@pytest.mark.parametrize("role", ROLE_VOCABULARY)
@pytest.mark.parametrize("action_id,allowed_roles", ACTION_MATRIX.items())
def test_action_matrix_role_decisions(role: str, action_id: str, allowed_roles: tuple[str, ...]) -> None:
    decision = evaluate_policy(
        AuthContext(
            actor_id=f"actor-{role}",
            roles=(role,),  # type: ignore[arg-type]
            auth_mode="dev_test",
            live_backend_auth_executed=False,
        ),
        action_id,
        target_type="matrix_fixture",
        target_id=f"{action_id}:{role}",
    )

    if role in allowed_roles:
        assert decision.decision == "allow"
        assert decision.no_mutation_expected is False
        assert decision.matched_roles == (role,)
    else:
        assert decision.decision == "deny"
        assert decision.reason_code == "RBAC_FORBIDDEN"
        assert decision.no_mutation_expected is True


@pytest.mark.parametrize("action_id", ACTION_MATRIX)
def test_sys_admin_full_admin_actions(action_id: str) -> None:
    decision = evaluate_policy(
        AuthContext(
            actor_id="sys-admin",
            roles=("sys_admin",),
            auth_mode="dev_test",
            live_backend_auth_executed=False,
        ),
        action_id,
        target_type="matrix_fixture",
        target_id=action_id,
    )

    assert decision.decision == "allow"


def test_missing_context_is_auth_required_no_mutation() -> None:
    decision = evaluate_policy(None, "pipeline.retry_run", target_type="pipeline_run", target_id="run")

    assert decision.decision == "deny"
    assert decision.reason_code == "AUTH_REQUIRED"
    assert decision.no_mutation_expected is True


def test_unknown_action_fails_closed_with_stable_config_error() -> None:
    decision = evaluate_policy(
        AuthContext(
            actor_id="operator",
            roles=("operator",),
            auth_mode="dev_test",
            live_backend_auth_executed=False,
        ),
        "pipeline.typo",
        target_type="pipeline_run",
        target_id="run",
    )

    assert decision.decision == "deny"
    assert decision.reason_code == "POLICY_ACTION_UNKNOWN"
    assert decision.no_mutation_expected is True
    assert decision.required_roles == ()


def test_audit_redaction_preserves_numeric_log_id_evidence_only() -> None:
    redacted = redact_audit_payload(
        {
            "log_id": 12,
            "prior_audit_log_id": 34,
            "rollback_history": {
                "prior_audit_log_id": 56,
                "log_id": "log text with /tmp/local token=secret",
            },
            "runtime_log": "DATABASE_URL=postgresql://nhms:secret@localhost/nhms failed in /tmp/local",
            "log_path": "/tmp/local/worker.log",
            "model_package_uri": "s3://user:pass@nhms/model/package?token=secret",
            "package_checksum": "a" * 64,
            "manifest": {"uri": "s3://bucket/manifest.json?credential=secret"},
            "credential": {"token": "secret"},
            "Authorization": "Bearer audit-secret",
            "provider_metadata": {
                "authorization": "Basic provider-secret",
                "text": '{"Authorization": "Bearer json-audit-secret"}',
            },
        }
    )

    assert redacted["log_id"] == 12
    assert redacted["prior_audit_log_id"] == 34
    assert redacted["rollback_history"]["prior_audit_log_id"] == 56
    assert redacted["rollback_history"]["log_id"] == "[redacted]"
    assert redacted["runtime_log"] == "[redacted]"
    assert redacted["log_path"] == "[redacted]"
    assert redacted["model_package_uri"] == "[redacted]"
    assert redacted["package_checksum"] == "[redacted]"
    assert redacted["manifest"] == {"uri": "[redacted]"}
    assert redacted["credential"] == {"token": "[redacted]"}
    assert redacted["Authorization"] == "[redacted]"
    assert redacted["provider_metadata"]["authorization"] == "[redacted]"
    assert redacted["provider_metadata"]["text"] == '{"Authorization": "Bearer [redacted]"}'
