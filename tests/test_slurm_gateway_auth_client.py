"""Client-side and shared-validator auth contract for the Slurm gateway.

Covers issue #1684's client half: the one-token contract shared by the
producer HTTP client, the shared validator, and the scheduler preflight; the
whitespace/no-trim rule; and the proven pre-acceptance rejection disposition
for POST responses carrying stable auth/policy denial codes.

Helpers/constants are imported from ``tests/test_slurm_gateway_auth.py``, which
is the same-name suite for the gateway auth surface and stays the single home
for the shared fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_slurm_gateway_auth import (
    SERVICE_TOKEN,
    _client_for,
    _standalone_app,
)


def test_short_or_whitespace_token_is_not_configured(monkeypatch) -> None:
    from packages.common.request_auth import read_configured_service_token

    assert read_configured_service_token({"SLURM_GATEWAY_SERVICE_TOKEN": "short"}) is None
    assert read_configured_service_token({"SLURM_GATEWAY_SERVICE_TOKEN": "token with spaces 1234567890"}) is None
    assert read_configured_service_token({"SLURM_GATEWAY_SERVICE_TOKEN": SERVICE_TOKEN}) == SERVICE_TOKEN


def test_any_whitespace_token_byte_is_rejected_without_trimming() -> None:
    """Whitespace-bearing tokens are never accepted, including leading/trailing.

    ``read_configured_service_token`` must NOT auto-trim: a value with any
    ``isspace()`` character (leading space, trailing newline, trailing tab,
    internal tab) is not a usable credential. A legitimate token is returned
    byte-for-byte unchanged.
    """
    from packages.common.request_auth import read_configured_service_token

    leading = f" {SERVICE_TOKEN}"
    trailing_newline = f"{SERVICE_TOKEN}\n"
    trailing_tab = f"{SERVICE_TOKEN}\t"
    internal_tab = SERVICE_TOKEN.replace("token", "token\t", 1)
    for value in (leading, trailing_newline, trailing_tab, internal_tab):
        assert read_configured_service_token({"SLURM_GATEWAY_SERVICE_TOKEN": value}) is None, repr(value)
    # The raw value must never be trimmed away: exactly the original token.
    assert read_configured_service_token({"SLURM_GATEWAY_SERVICE_TOKEN": SERVICE_TOKEN}) == SERVICE_TOKEN


def test_non_ascii_configured_token_is_unusable_configuration() -> None:
    """A non-ASCII configured token is unusable/missing configuration.

    The service token contract is an ASCII opaque bearer token (it must be
    representable in an HTTP Authorization header). Any non-ASCII configured
    value is rejected by the shared reader as not configured — never accepted
    by preflight yet unrepresentable in the actual header path. It must return
    ``None``, never raise.
    """
    from packages.common.request_auth import read_configured_service_token

    assert read_configured_service_token({"SLURM_GATEWAY_SERVICE_TOKEN": "tókén-0123456789abcdef"}) is None
    assert read_configured_service_token({"SLURM_GATEWAY_SERVICE_TOKEN": "token-1234567890-eñe"}) is None


def test_non_ascii_provided_header_is_ordinary_mismatch() -> None:
    """A raw non-ASCII Authorization header is an ordinary mismatch, not a 500.

    Starlette latin-1-decodes raw header bytes, so the route can receive a
    non-ASCII ``str``. Comparing it must not raise ``TypeError`` (the pre-fix
    ``hmac.compare_digest`` on ``str`` did); it is simply not the configured
    credential.
    """
    from packages.common.request_auth import service_bearer_matches

    class _Request:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers

    env = {"SLURM_GATEWAY_SERVICE_TOKEN": SERVICE_TOKEN}
    assert service_bearer_matches(_Request({"Authorization": "Bearer tókén-wrong-value-abcdef"}), env) is False
    assert service_bearer_matches(_Request({"Authorization": "Bearer óther-wrong-value-abcdef"}), env) is False
    assert service_bearer_matches(_Request({"Authorization": ""}), env) is False


def test_shared_token_behavior_rejects_leading_trailing_whitespace(monkeypatch) -> None:
    """Client and shared validator reject the same whitespace-bearing values."""
    from packages.common.request_auth import read_configured_service_token
    from services.orchestrator import chain_slurm_client

    leading = f" {SERVICE_TOKEN}"
    trailing_newline = f"{SERVICE_TOKEN}\n"
    for value in (leading, trailing_newline):
        assert read_configured_service_token({"SLURM_GATEWAY_SERVICE_TOKEN": value}) is None, repr(value)
        monkeypatch.setenv("SLURM_GATEWAY_SERVICE_TOKEN", value)
        assert chain_slurm_client._configured_service_token() is None, repr(value)
    monkeypatch.delenv("SLURM_GATEWAY_SERVICE_TOKEN", raising=False)


def test_route_rejects_whitespace_bearing_configured_token(monkeypatch) -> None:
    """Route auth inherits the no-trim contract: a whitespace-bearing env value
    is not a configured credential, so even the bare token value as bearer does
    not authenticate — fail closed with 401, no gateway side effect.
    """
    leading = f" {SERVICE_TOKEN}"
    app = _standalone_app()
    # Treat the whitespace value as the configured credential: the route must
    # NOT trim-and-accept it.
    client = _client_for(monkeypatch, app, service_token=leading)
    response = client.post(
        "/api/v1/slurm/jobs",
        json={"run_id": "run_001", "model_id": "model_001"},
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_one_token_contract_shared_across_producer_validator_preflight(monkeypatch) -> None:
    """F2: one token contract. Producer, validator, and preflight share one source."""

    from packages.common import request_auth as shared
    from services.orchestrator import chain_slurm_client

    # Consumers delegate token validation to the shared owner; the env name and
    # min-length live in exactly one module (the preflight module has no
    # duplicate copies either — checked by scanning its source for the env name
    # and min-length constants only as directive references).
    assert chain_slurm_client._configured_service_token is shared.read_configured_service_token
    preflight_source = (
        Path(__file__).resolve().parents[1] / "services" / "orchestrator" / "scheduler_gateway.py"
    ).read_text(encoding="utf-8")
    assert "SLURM_GATEWAY_SERVICE_TOKEN_ENV =" not in preflight_source
    assert "SERVICE_TOKEN_MIN_LENGTH =" not in preflight_source
    assert "read_configured_service_token" in preflight_source

    assert shared.SLURM_GATEWAY_SERVICE_TOKEN_ENV == "SLURM_GATEWAY_SERVICE_TOKEN"
    assert shared.SERVICE_TOKEN_MIN_LENGTH == 16

    # Behavior identity: producer and validator agree on the same token values.
    env_ok = {"SLURM_GATEWAY_SERVICE_TOKEN": "shared-token-value-0123456789"}
    env_bad = {"SLURM_GATEWAY_SERVICE_TOKEN": "short"}
    assert shared.read_configured_service_token(env_ok)
    assert shared.read_configured_service_token(env_bad) is None
    monkeypatch.setenv("SLURM_GATEWAY_SERVICE_TOKEN", env_ok["SLURM_GATEWAY_SERVICE_TOKEN"])
    assert chain_slurm_client._configured_service_token() == env_ok["SLURM_GATEWAY_SERVICE_TOKEN"]
    monkeypatch.delenv("SLURM_GATEWAY_SERVICE_TOKEN", raising=False)
    assert chain_slurm_client._configured_service_token() is None


# ---------------------------------------------------------------------------
# F4 - auth/policy denials are proven pre-acceptance rejections for POST
# ---------------------------------------------------------------------------


def _disposition_for_response(monkeypatch, status_code: int, payload: dict) -> str:
    import httpx

    from services.orchestrator.chain_slurm_client import HttpSlurmGatewayClient

    class _FakeHttpResponse:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _FakeHttpClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: D401
            del args, kwargs

        def __enter__(self) -> _FakeHttpClient:
            return self

        def __exit__(self, *args) -> None:
            del args

        def request(self, method: str, path: str, *, json=None, headers=None) -> _FakeHttpResponse:
            return _FakeHttpResponse(status_code, payload)

    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    client = HttpSlurmGatewayClient("http://gateway.test")
    try:
        client.submit_job({"job_type": "x"})
    except Exception as error:
        return str(getattr(error, "submit_disposition", None))
    raise AssertionError("expected error")


def _nested_error_payload(code: str) -> dict:
    return {"error": {"code": code, "message": "denied"}}


@pytest.mark.parametrize("status_code", [401, 403, 503])
@pytest.mark.parametrize(
    ("code", "payload"),
    [
        ("AUTH_REQUIRED", None),
        ("RBAC_FORBIDDEN", None),
        ("RELEASE_BLOCKED", None),
        ("POLICY_CONFIG_ERROR", None),
    ],
)
def test_policy_denial_codes_are_proven_rejected_for_post(monkeypatch, status_code, code, payload) -> None:
    from services.orchestrator.chain_config import SubmitDisposition

    body = payload if payload is not None else _nested_error_payload(code)
    disposition = _disposition_for_response(monkeypatch, status_code, body)
    assert disposition == SubmitDisposition.REJECTED.value, (status_code, code)


def test_unknown_error_remains_ambiguous_for_post(monkeypatch) -> None:
    from services.orchestrator.chain_config import SubmitDisposition

    for status_code in (500, 502, 503):
        disposition = _disposition_for_response(
            monkeypatch,
            status_code,
            {"error": {"code": "SLURM_COMMAND_ERROR"}},
        )
        assert disposition == SubmitDisposition.AMBIGUOUS.value, (status_code, disposition)


def test_network_error_remains_ambiguous_for_post(monkeypatch) -> None:
    import httpx

    from services.orchestrator.chain_config import SubmitDisposition
    from services.orchestrator.chain_slurm_client import HttpSlurmGatewayClient

    class _FakeHttpClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: D401
            del args, kwargs

        def __enter__(self) -> _FakeHttpClient:
            return self

        def __exit__(self, *args) -> None:
            del args

        def request(self, method: str, path: str, *, json=None, headers=None) -> httpx.Response:
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    client = HttpSlurmGatewayClient("http://gateway.test")
    try:
        client.submit_job({"job_type": "x"})
    except Exception as error:
        assert str(getattr(error, "submit_disposition", None)) == SubmitDisposition.AMBIGUOUS.value
    else:
        raise AssertionError("expected error")


def test_http_client_injects_bearer_only_on_mutation_calls(monkeypatch) -> None:
    import httpx

    from services.orchestrator.chain_slurm_client import HttpSlurmGatewayClient

    seen: list[tuple[str, str]] = []

    class _FakeHttpResponse:
        status_code = 200

        def json(self) -> dict:
            return {}

    class _FakeHttpClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: D401
            del args, kwargs

        def __enter__(self) -> _FakeHttpClient:
            return self

        def __exit__(self, *args) -> None:
            del args

        def request(self, method: str, path: str, *, json=None, headers=None) -> _FakeHttpResponse:
            seen.append((method, str(headers), path))
            return _FakeHttpResponse()

    monkeypatch.setenv("SLURM_GATEWAY_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    client = HttpSlurmGatewayClient("http://gateway.test")

    client.get_job_status("mock_1")
    client.fetch_logs("mock_1")
    client.cancel_job("mock_1")
    try:
        client.get_array_task_results("mock_1")
    except Exception:
        # response shape validation is out of scope here; headers were recorded.
        pass
    try:
        client.submit_job({"job_id": "mock_1", "status": "submitted"})
    except Exception:
        # response shape validation is out of scope here; headers were recorded.
        pass
    try:
        client.submit_job_array({"job_id": "mock_1", "status": "submitted"})
    except Exception:
        # response shape validation is out of scope here; headers were recorded.
        pass

    methods = [entry[0] for entry in seen]
    assert methods == ["GET", "GET", "DELETE", "GET", "POST", "POST"]
    # Health/read calls carry no Authorization header.
    for index, (method, headers_text, _path) in enumerate(seen):
        if method == "GET":
            assert "Bearer" not in headers_text or headers_text == "None"
    # Mutation calls carry the bearer.
    for entry in seen:
        if entry[0] in {"DELETE", "POST"}:
            assert SERVICE_TOKEN in entry[1], entry
