"""Deterministic tests for the M24 §1 gateway live-proof emitter.

These exercise the emitter's orchestration over an injected fake HTTP client;
no real Slurm, gateway process, or network is involved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from packages.common.request_auth import (
    SLURM_GATEWAY_SERVICE_TOKEN_ENV,
    read_configured_service_token,
)
from scripts import m24_gateway_proof as proof
from services.m24_live.receipt import validate_receipt
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES, SlurmGatewaySettings

# Distinctive, could-not-occur-by-accident value: it must also satisfy the
# shared reader's validity rules (ASCII, no whitespace, >= min length), which
# ``test_fake_token_is_usable_configuration`` asserts so the leak test cannot
# pass vacuously through the missing-token BLOCKED path.
FAKE_TOKEN = "M24-FAKE-BEARER-must-not-leak-3f9a1c7e"
OTHER_TOKEN = "M24-FAKE-BEARER-wrong-value-8b2d4a60"

_TOKEN_ENV = {SLURM_GATEWAY_SERVICE_TOKEN_ENV: FAKE_TOKEN}


class _Resp:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body


_HEALTHY_BINARIES = {
    name: {"resolved": True, "executable": True, "detail": None}
    for name in ("sbatch", "squeue", "sacct", "scancel")
}


def _auth_required_body() -> dict[str, Any]:
    return {
        "request_id": "req-auth",
        "error": {
            "code": "AUTH_REQUIRED",
            "message": "Authentication required for this operation.",
            "details": {},
        },
    }


class _FakeClient:
    """Scriptable fake gateway HTTP client.

    Short job: poll returns ``succeeded`` immediately.
    Long job: first poll ``running``; DELETE returns ``cancelled``.
    ``long_job_status`` overrides the long job's poll status (e.g. ``pending``
    for a job that never starts before the bounded wait runs out).

    Mutations (POST/DELETE) are authenticated exactly like the real gateway
    after #1888: a missing or mismatched bearer yields 401 ``AUTH_REQUIRED``.
    GET (health/poll) stays anonymous by contract and never inspects auth.
    """

    def __init__(
        self,
        *,
        healthy: bool = True,
        reachable: bool = True,
        expected_token: str = FAKE_TOKEN,
        long_job_status: str = "running",
    ) -> None:
        self.healthy = healthy
        self.long_job_status = long_job_status
        self.reachable = reachable
        self.expected_token = expected_token
        self._next_job = 1000
        # (method, url, headers) — headers recorded verbatim so the tests can
        # assert bearer presence on mutations and absence on reads.
        self.calls: list[tuple[str, str, dict[str, str] | None]] = []

    # -- helpers -----------------------------------------------------------
    def _authorized(self, headers: dict[str, str] | None) -> bool:
        provided = (headers or {}).get("Authorization")
        return provided == f"Bearer {self.expected_token}"

    # -- transport ---------------------------------------------------------
    def get(self, url: str) -> _Resp:
        self.calls.append(("GET", url, None))
        if not self.reachable:
            raise ConnectionError("connection refused")
        if url.endswith("/api/v1/slurm/health"):
            body = {
                "healthy": self.healthy,
                "binaries": _HEALTHY_BINARIES if self.healthy else {},
            }
            return _Resp(200 if self.healthy else 503, body)
        # job status poll: job_id encodes which job (>=2000 == long job)
        job_id = url.rsplit("/", 1)[-1]
        status = self.long_job_status if int(job_id) >= 2000 else "succeeded"
        return _Resp(
            200,
            {
                "job_id": job_id,
                "run_id": "rid",
                "status": status,
                "manifest": {"workspace_dir": "/scratch/ws", "run_id": "rid"},
                "resource_metrics": {"elapsed": "00:00:05"},
            },
        )

    def post(self, url: str, json: dict[str, Any], headers: dict[str, str] | None = None) -> _Resp:
        self.calls.append(("POST", url, dict(headers) if headers else headers))
        if not self.reachable:
            raise ConnectionError("connection refused")
        if not self._authorized(headers):
            return _Resp(401, _auth_required_body())
        sleep = int(json["slurm_env"]["SMOKE_SLEEP_SECONDS"])
        self._next_job = 2000 if sleep >= 600 else 1000
        return _Resp(201, {"job_id": str(self._next_job), "run_id": json["run_id"], "status": "submitted"})

    def delete(self, url: str, headers: dict[str, str] | None = None) -> _Resp:
        self.calls.append(("DELETE", url, dict(headers) if headers else headers))
        if not self.reachable:
            raise ConnectionError("connection refused")
        if not self._authorized(headers):
            return _Resp(401, _auth_required_body())
        job_id = url.rsplit("/", 1)[-1]
        return _Resp(200, {"job_id": job_id, "status": "cancelled", "manifest": {}})


def _noop_sleep(_seconds: float) -> None:
    return None


def _calls_by_method(client: _FakeClient, method: str) -> list[tuple[str, str, dict[str, str] | None]]:
    return [call for call in client.calls if call[0] == method]


def test_all_three_stages_pass_produces_valid_live_proof_receipt() -> None:
    client = _FakeClient(healthy=True)
    receipt = proof.build_gateway_receipt(
        "m24_smoke_run",
        gateway_url="http://gw:8081",
        client=client,
        sleep_func=_noop_sleep,
        env=_TOKEN_ENV,
    )

    validate_receipt(receipt)  # must satisfy the canonical contract
    assert receipt["section"] == "gateway"
    assert receipt["execution_mode"] == "live_proof"
    assert receipt["status"] == "PASS"
    assert receipt["live_proof_accepted"] is True
    assert receipt["dependency_blocker"] is None

    stage_names = [s["stage"] for s in receipt["stages"]]
    assert stage_names == ["health", "submit_poll_terminal", "submit_cancel"]
    assert all(s["status"] == "PASS" for s in receipt["stages"])

    # short-job terminal id propagates to the slurm block.
    assert receipt["slurm"]["job_id"] == "1000"
    assert receipt["slurm"]["log_uri"] == "/scratch/ws/rid/logs/1000.out"


def test_terminal_and_cancel_are_two_independent_stages() -> None:
    client = _FakeClient(healthy=True)
    receipt = proof.build_gateway_receipt(
        "m24_smoke_run",
        gateway_url="http://gw:8081",
        client=client,
        sleep_func=_noop_sleep,
        env=_TOKEN_ENV,
    )
    stages = {s["stage"]: s for s in receipt["stages"]}

    assert "submit_poll_terminal" in stages
    assert "submit_cancel" in stages
    # distinct jobs: short job terminal, long job cancelled-while-active.
    assert stages["submit_poll_terminal"]["counts"]["terminal_status"] == "succeeded"
    assert stages["submit_poll_terminal"]["counts"]["job_id"] == "1000"
    assert stages["submit_cancel"]["counts"]["job_id"] == "2000"
    assert stages["submit_cancel"]["counts"]["cancelled_status"] == "cancelled"
    assert stages["submit_cancel"]["counts"]["cancelled_while_active"] is True

    # two POSTs (two jobs) and one DELETE were issued.
    posts = _calls_by_method(client, "POST")
    deletes = _calls_by_method(client, "DELETE")
    assert len(posts) == 2
    assert len(deletes) == 1


def test_long_job_that_never_runs_is_cancelled_for_cleanup_and_blocks() -> None:
    # #2476: a cancel-before-start is not a cancel-while-active proof.
    client = _FakeClient(healthy=True, long_job_status="pending")
    sleeps: list[float] = []
    receipt = proof.build_gateway_receipt(
        "m24_smoke_run",
        gateway_url="http://gw:8081",
        client=client,
        sleep_func=sleeps.append,
        env=_TOKEN_ENV,
    )

    validate_receipt(receipt)  # BLOCKED receipts must still validate
    assert receipt["status"] == "BLOCKED"
    assert receipt["live_proof_accepted"] is False
    stages = {s["stage"]: s for s in receipt["stages"]}
    assert [s["stage"] for s in receipt["stages"]] == ["health", "submit_poll_terminal", "submit_cancel"]
    assert stages["health"]["status"] == "PASS"
    assert stages["submit_poll_terminal"]["status"] == "PASS"
    assert stages["submit_cancel"]["status"] == "BLOCKED"
    blocker = receipt["dependency_blocker"]
    assert isinstance(blocker, str)
    assert "'pending'" in blocker
    assert "2000" in blocker
    assert stages["submit_cancel"]["counts"]["error"] == blocker

    # the full bounded wait was spent on the long job, then it was still cancelled.
    long_job_url = "http://gw:8081/api/v1/slurm/jobs/2000"
    long_job_polls = [call for call in _calls_by_method(client, "GET") if call[1] == long_job_url]
    assert len(long_job_polls) == proof.CANCEL_WAIT_MAX_ATTEMPTS
    assert [call[1] for call in _calls_by_method(client, "DELETE")] == [long_job_url]
    assert sleeps == [proof.POLL_INTERVAL_SECONDS] * proof.CANCEL_WAIT_MAX_ATTEMPTS


def test_unreachable_gateway_blocks_without_fabricated_pass() -> None:
    client = _FakeClient(reachable=False)
    receipt = proof.build_gateway_receipt(
        "m24_smoke_run",
        gateway_url="http://gw:8081",
        client=client,
        sleep_func=_noop_sleep,
        env=_TOKEN_ENV,
    )

    validate_receipt(receipt)  # BLOCKED receipts must still validate
    assert receipt["status"] == "BLOCKED"
    assert receipt["live_proof_accepted"] is False
    assert isinstance(receipt["dependency_blocker"], str)
    assert receipt["dependency_blocker"].strip()
    # health blocked first; no later proof stages fabricated.
    assert receipt["stages"][0]["stage"] == "health"
    assert receipt["stages"][0]["status"] == "BLOCKED"
    assert len(receipt["stages"]) == 1


def test_unhealthy_binaries_block() -> None:
    client = _FakeClient(healthy=False)
    receipt = proof.build_gateway_receipt(
        "m24_smoke_run",
        gateway_url="http://gw:8081",
        client=client,
        sleep_func=_noop_sleep,
        env=_TOKEN_ENV,
    )
    assert receipt["status"] == "BLOCKED"
    assert receipt["live_proof_accepted"] is False
    assert "health" in receipt["dependency_blocker"] or "binaries" in receipt["dependency_blocker"]


def test_smoke_job_type_mapping_and_template_resolves_within_template_dir() -> None:
    # job_type mapping must exist for the emitter's submissions.
    assert DEFAULT_JOB_TYPE_TEMPLATES.get("smoke") == "smoke.sbatch"

    settings = SlurmGatewaySettings()
    assert settings.job_type_templates.get("smoke") == "smoke.sbatch"

    template_dir = Path(settings.template_dir).resolve()
    candidate = (template_dir / settings.job_type_templates["smoke"]).resolve()
    # no path traversal: resolved template stays inside the template dir.
    assert candidate.is_relative_to(template_dir)
    assert candidate.exists()


# --- #1897: authenticated mutations, anonymous reads, no secret in evidence ------


def test_fake_token_is_usable_configuration() -> None:
    # Guards the leak/bearer tests against passing vacuously: if the fake token
    # were rejected by the shared reader, every case below would silently fall
    # into the missing-token BLOCKED path.
    assert read_configured_service_token(dict(_TOKEN_ENV)) == FAKE_TOKEN


def test_every_mutation_carries_the_service_bearer() -> None:
    client = _FakeClient(healthy=True)
    receipt = proof.build_gateway_receipt(
        "m24_smoke_run",
        gateway_url="http://gw:8081",
        client=client,
        sleep_func=_noop_sleep,
        env=_TOKEN_ENV,
    )
    assert receipt["status"] == "PASS"

    mutations = _calls_by_method(client, "POST") + _calls_by_method(client, "DELETE")
    assert len(mutations) == 3
    for method, url, headers in mutations:
        assert headers is not None, f"{method} {url} carried no headers"
        assert headers.get("Authorization") == f"Bearer {FAKE_TOKEN}", f"{method} {url}"


def test_health_and_polls_stay_anonymous() -> None:
    client = _FakeClient(healthy=True)
    proof.build_gateway_receipt(
        "m24_smoke_run",
        gateway_url="http://gw:8081",
        client=client,
        sleep_func=_noop_sleep,
        env=_TOKEN_ENV,
    )

    gets = _calls_by_method(client, "GET")
    # health + at least one poll per submit stage.
    assert len(gets) >= 3
    assert any(url.endswith("/api/v1/slurm/health") for _method, url, _headers in gets)
    for _method, url, headers in gets:
        assert headers is None or "Authorization" not in headers, f"GET {url} carried a credential"


def test_missing_token_blocks_and_never_sends_an_anonymous_mutation() -> None:
    client = _FakeClient(healthy=True)
    receipt = proof.build_gateway_receipt(
        "m24_smoke_run",
        gateway_url="http://gw:8081",
        client=client,
        sleep_func=_noop_sleep,
        env={},  # deterministically "unset", independent of the ambient shell
    )

    validate_receipt(receipt)
    assert receipt["status"] == "BLOCKED"
    assert receipt["live_proof_accepted"] is False
    assert isinstance(receipt["dependency_blocker"], str)
    assert receipt["dependency_blocker"].strip()
    assert SLURM_GATEWAY_SERVICE_TOKEN_ENV in receipt["dependency_blocker"]
    # health still proves what it can; the mutation stage is the one blocked.
    assert receipt["stages"][0] == {
        "stage": "health",
        "status": "PASS",
        "counts": receipt["stages"][0]["counts"],
    }
    assert receipt["stages"][-1]["stage"] == "submit_poll_terminal"
    assert receipt["stages"][-1]["status"] == "BLOCKED"
    # no anonymous mutation may ever leave the emitter.
    assert _calls_by_method(client, "POST") == []
    assert _calls_by_method(client, "DELETE") == []


def test_auth_required_401_blocks_without_fabricated_pass() -> None:
    client = _FakeClient(healthy=True, expected_token=OTHER_TOKEN)
    receipt = proof.build_gateway_receipt(
        "m24_smoke_run",
        gateway_url="http://gw:8081",
        client=client,
        sleep_func=_noop_sleep,
        env=_TOKEN_ENV,
    )

    validate_receipt(receipt)
    assert receipt["status"] == "BLOCKED"
    assert receipt["live_proof_accepted"] is False
    assert "401" in receipt["dependency_blocker"]
    assert "AUTH_REQUIRED" in receipt["dependency_blocker"]
    assert FAKE_TOKEN not in json.dumps(receipt)


def test_receipt_never_serializes_the_token() -> None:
    for client in (
        _FakeClient(healthy=True),
        _FakeClient(healthy=True, expected_token=OTHER_TOKEN),
        _FakeClient(reachable=False),
    ):
        receipt = proof.build_gateway_receipt(
            "m24_smoke_run",
            gateway_url="http://gw:8081",
            client=client,
            sleep_func=_noop_sleep,
            env=_TOKEN_ENV,
        )
        serialized = json.dumps(receipt)
        assert FAKE_TOKEN not in serialized
        assert "Bearer" not in serialized
        assert FAKE_TOKEN not in receipt["command"]
        assert FAKE_TOKEN not in json.dumps(receipt["notes"])


def test_default_env_wiring_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SLURM_GATEWAY_SERVICE_TOKEN_ENV, FAKE_TOKEN)
    client = _FakeClient(healthy=True)
    receipt = proof.build_gateway_receipt(
        "m24_smoke_run",
        gateway_url="http://gw:8081",
        client=client,
        sleep_func=_noop_sleep,
    )
    assert receipt["status"] == "PASS"
    mutations = _calls_by_method(client, "POST") + _calls_by_method(client, "DELETE")
    assert mutations
    for _method, _url, headers in mutations:
        assert (headers or {}).get("Authorization") == f"Bearer {FAKE_TOKEN}"


def test_cli_help_exposes_no_token_flag_or_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(SLURM_GATEWAY_SERVICE_TOKEN_ENV, FAKE_TOKEN)
    with pytest.raises(SystemExit) as excinfo:
        proof.main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert FAKE_TOKEN not in out
    for forbidden in ("--token", "--service-token", "--bearer", "--authorization"):
        assert forbidden not in out


def test_httpx_adapter_forwards_mutation_headers_and_leaves_get_anonymous() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.headers.get("Authorization")))
        return httpx.Response(200, json={"ok": True})

    client = proof._HttpxClient()
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        client.post(
            "http://gw:8081/api/v1/slurm/jobs",
            json={"run_id": "rid"},
            headers={"Authorization": f"Bearer {FAKE_TOKEN}"},
        )
        client.delete(
            "http://gw:8081/api/v1/slurm/jobs/1",
            headers={"Authorization": f"Bearer {FAKE_TOKEN}"},
        )
        client.get("http://gw:8081/api/v1/slurm/health")
    finally:
        client.close()

    assert seen == [
        ("POST", f"Bearer {FAKE_TOKEN}"),
        ("DELETE", f"Bearer {FAKE_TOKEN}"),
        ("GET", None),
    ]
