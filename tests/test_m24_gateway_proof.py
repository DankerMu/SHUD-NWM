"""Deterministic tests for the M24 §1 gateway live-proof emitter.

These exercise the emitter's orchestration over an injected fake HTTP client;
no real Slurm, gateway process, or network is involved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts import m24_gateway_proof as proof
from services.m24_live.receipt import validate_receipt
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES, SlurmGatewaySettings


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


class _FakeClient:
    """Scriptable fake gateway HTTP client.

    Short job: poll returns ``succeeded`` immediately.
    Long job: first poll ``running``; DELETE returns ``cancelled``.
    """

    def __init__(self, *, healthy: bool = True, reachable: bool = True) -> None:
        self.healthy = healthy
        self.reachable = reachable
        self._next_job = 1000
        self.calls: list[tuple[str, str]] = []

    # -- transport ---------------------------------------------------------
    def get(self, url: str) -> _Resp:
        self.calls.append(("GET", url))
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
        status = "running" if int(job_id) >= 2000 else "succeeded"
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

    def post(self, url: str, json: dict[str, Any]) -> _Resp:
        self.calls.append(("POST", url))
        sleep = int(json["slurm_env"]["SMOKE_SLEEP_SECONDS"])
        self._next_job = 2000 if sleep >= 600 else 1000
        return _Resp(201, {"job_id": str(self._next_job), "run_id": json["run_id"], "status": "submitted"})

    def delete(self, url: str) -> _Resp:
        self.calls.append(("DELETE", url))
        job_id = url.rsplit("/", 1)[-1]
        return _Resp(200, {"job_id": job_id, "status": "cancelled", "manifest": {}})


def _noop_sleep(_seconds: float) -> None:
    return None


def test_all_three_stages_pass_produces_valid_live_proof_receipt() -> None:
    client = _FakeClient(healthy=True)
    receipt = proof.build_gateway_receipt(
        "m24_smoke_run",
        gateway_url="http://gw:8081",
        client=client,
        sleep_func=_noop_sleep,
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
    posts = [c for c in client.calls if c[0] == "POST"]
    deletes = [c for c in client.calls if c[0] == "DELETE"]
    assert len(posts) == 2
    assert len(deletes) == 1


def test_unreachable_gateway_blocks_without_fabricated_pass() -> None:
    client = _FakeClient(reachable=False)
    receipt = proof.build_gateway_receipt(
        "m24_smoke_run",
        gateway_url="http://gw:8081",
        client=client,
        sleep_func=_noop_sleep,
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
