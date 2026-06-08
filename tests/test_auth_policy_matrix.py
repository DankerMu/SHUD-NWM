from __future__ import annotations

import pytest
from starlette.requests import Request

from apps.api import auth as api_auth
from apps.api.errors import ApiError
from packages.common.auth_policy import (
    ACTION_MATRIX,
    AUTH_REQUIRED,
    RBAC_FORBIDDEN,
    RELEASE_BLOCKED,
    ROLE_VOCABULARY,
    AuthContext,
    cli_policy_decision_from_evidence,
    evaluate_policy,
    redact_audit_payload,
)

_API_AUTH_ENV_VARS = (
    "ALLOW_DEV_ROLE_HEADER",
    "AUTH_BACKEND",
    "NHMS_AUTH_MODE",
    "NHMS_DEV_AUTH_TOKEN",
    "NHMS_INTERNAL_LIVE_PROOF_TOKEN",
    "NHMS_TRUSTED_LIVE_PROOF_MODE",
)


def _reset_api_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _API_AUTH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _api_request(
    headers: dict[str, str] | None = None,
    *,
    request_id: str = "req-auth-policy-wrapper",
) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/policy-wrapper-test",
            "raw_path": b"/policy-wrapper-test",
            "query_string": b"",
            "headers": [
                (key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in (headers or {}).items()
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )
    request.state.request_id = request_id
    return request


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


def test_api_wrapper_allows_and_records_redacted_audit_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_api_auth_env(monkeypatch)
    monkeypatch.setenv("ALLOW_DEV_ROLE_HEADER", "true")
    request = _api_request(
        {"X-User-ID": "api-operator", "X-User-Role": "operator"},
        request_id="req-api-wrapper-allow",
    )
    payload = {
        "model_package_uri": "s3://user:pass@bucket/model-package?token=secret",
        "manifest": {"uri": "s3://bucket/manifest.json?credential=secret"},
    }

    evaluated = api_auth.evaluate_request_action(
        request,
        "pipeline.retry_run",
        target_type="pipeline_run",
        target_id="run-allow",
    )
    decision = api_auth.require_action(
        request,
        "pipeline.retry_run",
        target_type="pipeline_run",
        target_id="run-allow",
        payload=payload,
    )

    assert evaluated.decision == "allow"
    assert decision.decision == "allow"
    assert decision.actor_id == "api-operator"
    assert request.state.auth_policy_decisions == [
        {
            "request_id": "req-api-wrapper-allow",
            "actor": "api-operator",
            "actor_id": "api-operator",
            "roles": ["operator"],
            "action": "pipeline.retry_run",
            "action_id": "pipeline.retry_run",
            "target": {"type": "pipeline_run", "id": "run-allow"},
            "decision": "allow",
            "reason": "Actor roles satisfy the canonical RBAC policy.",
            "reason_code": "RBAC_ALLOWED",
            "execution_mode": "backend_route_executed",
            "auth_mode": "dev_test",
            "live_backend_auth_executed": False,
            "provider_metadata": {},
            "role_mapping_result": {
                "raw_roles_present": True,
                "raw_roles_input_present": True,
                "raw_roles": ("operator",),
                "mapped_roles": ("operator",),
                "unmapped_roles": (),
                "mapping_status": "mapped",
            },
            "no_mutation_expected": False,
            "previous_state": {},
            "new_state": {},
            "lineage": {
                "model_package_uri": "[redacted]",
                "manifest": {"uri": "[redacted]"},
            },
        }
    ]


def test_api_wrapper_missing_auth_raises_401_and_records_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_api_auth_env(monkeypatch)
    request = _api_request(request_id="req-api-wrapper-missing-auth")

    with pytest.raises(ApiError) as exc_info:
        api_auth.require_action(
            request,
            "pipeline.retry_run",
            target_type="pipeline_run",
            target_id="run-missing-auth",
        )

    error = exc_info.value
    assert error.status_code == 401
    assert error.code == AUTH_REQUIRED
    assert error.details["policy_decision"]["reason_code"] == AUTH_REQUIRED
    assert error.details["audit_record"]["reason_code"] == AUTH_REQUIRED
    assert request.state.auth_policy_decisions[0]["decision"] == "deny"
    assert request.state.auth_policy_decisions[0]["reason_code"] == AUTH_REQUIRED
    assert request.state.auth_policy_decisions[0]["no_mutation_expected"] is True


def test_api_wrapper_known_role_without_permission_raises_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_api_auth_env(monkeypatch)
    monkeypatch.setenv("ALLOW_DEV_ROLE_HEADER", "true")
    request = _api_request(
        {"X-User-ID": "api-viewer", "X-User-Role": "viewer"},
        request_id="req-api-wrapper-rbac",
    )

    with pytest.raises(ApiError) as exc_info:
        api_auth.require_action(
            request,
            "pipeline.retry_run",
            target_type="pipeline_run",
            target_id="run-rbac-forbidden",
        )

    error = exc_info.value
    assert error.status_code == 403
    assert error.code == RBAC_FORBIDDEN
    assert error.details["policy_decision"]["reason_code"] == RBAC_FORBIDDEN
    assert request.state.auth_policy_decisions[0]["actor_id"] == "api-viewer"
    assert request.state.auth_policy_decisions[0]["roles"] == ["viewer"]
    assert request.state.auth_policy_decisions[0]["no_mutation_expected"] is True


def test_api_wrapper_live_backend_without_trusted_proof_raises_503(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_api_auth_env(monkeypatch)
    monkeypatch.setenv("AUTH_BACKEND", "oidc")
    request = _api_request(
        {"X-Live-User-ID": "live-operator", "X-Live-User-Roles": "operator"},
        request_id="req-api-wrapper-release-blocked",
    )

    with pytest.raises(ApiError) as exc_info:
        api_auth.require_action(
            request,
            "pipeline.retry_run",
            target_type="pipeline_run",
            target_id="run-live-blocked",
        )

    error = exc_info.value
    assert error.status_code == 503
    assert error.code == RELEASE_BLOCKED
    assert error.details["policy_decision"]["decision"] == "release_blocked"
    assert error.details["audit_record"]["execution_mode"] == "release_blocked"
    assert error.details["removal_criteria"] == "Configure and prove live backend identity-provider role mapping."
    assert request.state.auth_policy_decisions[0]["reason_code"] == RELEASE_BLOCKED


def test_api_wrapper_unknown_action_maps_policy_config_error_with_redacted_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_api_auth_env(monkeypatch)
    monkeypatch.setenv("ALLOW_DEV_ROLE_HEADER", "true")
    request = _api_request(
        {"X-User-ID": "api-operator", "X-User-Role": "operator"},
        request_id="req-api-wrapper-unknown-action",
    )

    with pytest.raises(ApiError) as exc_info:
        api_auth.require_action(
            request,
            "pipeline.typo",
            target_type="pipeline_run",
            target_id="/tmp/nhms-secret/run?token=secret",
            payload={
                "runtime_log": "DATABASE_URL=postgresql://nhms:secret@localhost/nhms failed in /tmp/local",
                "credential": {"token": "secret"},
            },
        )

    error = exc_info.value
    assert error.status_code == 403
    assert error.code == api_auth.POLICY_CONFIG_ERROR
    assert error.details["policy_decision"]["reason_code"] == "POLICY_ACTION_UNKNOWN"
    assert error.details["policy_decision"]["target_id"] == "[redacted]"
    assert error.details["audit_record"]["reason_code"] == "POLICY_ACTION_UNKNOWN"
    assert error.details["audit_record"]["target"]["id"] == "[redacted]"
    assert error.details["audit_record"]["lineage"] == {
        "runtime_log": "[redacted]",
        "credential": {"token": "[redacted]"},
    }
    assert request.state.auth_policy_decisions[0]["target"]["id"] == "[redacted]"


def test_cli_policy_unknown_explicit_role_denies_without_mutation() -> None:
    decision = cli_policy_decision_from_evidence(
        "models.activate",
        target_type="model_instance",
        target_id="model-unknown-role",
        actor_id="cli-actor",
        roles=("external_admin",),
        env={},
    )

    assert decision is not None
    assert decision.decision == "deny"
    assert decision.reason_code == RBAC_FORBIDDEN
    assert decision.no_mutation_expected is True
    assert decision.roles == ()
    assert decision.role_mapping_result is not None
    assert decision.role_mapping_result["unmapped_roles"] == ("external_admin",)


def test_cli_policy_unknown_env_role_denies_without_mutation() -> None:
    decision = cli_policy_decision_from_evidence(
        "models.activate",
        target_type="model_instance",
        target_id="model-env-unknown-role",
        env={
            "NHMS_CLI_AUTH_ACTOR_ID": "cli-env-actor",
            "NHMS_CLI_AUTH_ROLES": "external_admin",
        },
    )

    assert decision is not None
    assert decision.actor_id == "cli-env-actor"
    assert decision.decision == "deny"
    assert decision.reason_code == RBAC_FORBIDDEN
    assert decision.no_mutation_expected is True
    assert decision.roles == ()
    assert decision.role_mapping_result is not None
    assert decision.role_mapping_result["unmapped_roles"] == ("external_admin",)
