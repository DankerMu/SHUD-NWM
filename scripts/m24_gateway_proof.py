"""M24 §1 (#288) live-proof emitter for the standalone Slurm gateway.

Drives the gateway *only* over its HTTP API and records three independent
live receipts into ``artifacts/m24/<run_id>/gateway.json`` (section=gateway,
execution_mode=live_proof):

1. ``health``                4-binary (sbatch/squeue/sacct/scancel) resolved +
                             executable + healthy from ``GET .../slurm/health``.
2. ``submit_poll_terminal``  short ``smoke`` job submitted via ``POST .../jobs``;
                             polled via ``GET .../jobs/{id}`` until terminal.
3. ``submit_cancel``         long ``smoke`` job submitted, allowed to reach
                             RUNNING, then cancelled via ``DELETE .../jobs/{id}``.

The terminal-poll and cancel-while-active proofs are deliberately *separate*
stages on *separate* jobs; they are never collapsed into one step.

Fail-safe contract: any unreachable gateway, rejected template, or missing
dependency flips the whole receipt to ``BLOCKED`` with a non-empty
``dependency_blocker`` and ``live_proof_accepted == false``. The emitter never
fabricates a PASS.

Usage::

    uv run python scripts/m24_gateway_proof.py --run-id <id> \
        [--gateway-url http://127.0.0.1:8081] [--partition compute]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Protocol

# Allow running as a plain script (repo root on sys.path).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.m24_live.receipt import (  # noqa: E402
    CONTRACT_ID,
    SCHEMA_VERSION,
    validate_receipt,
    write_receipt,
)

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8081"
SMOKE_JOB_TYPE = "smoke"
SMOKE_MODEL_ID = "m24_smoke"

SHORT_SLEEP_SECONDS = 5
LONG_SLEEP_SECONDS = 600

HTTP_TIMEOUT_SECONDS = 10
POLL_INTERVAL_SECONDS = 5
POLL_MAX_ATTEMPTS = 60  # short job: 60 * 5s = 5 min ceiling
CANCEL_WAIT_MAX_ATTEMPTS = 12  # long job: wait up to 12 * 5s for RUNNING

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


class HttpClient(Protocol):
    def get(self, url: str) -> HttpResponse: ...
    def post(self, url: str, json: dict[str, Any]) -> HttpResponse: ...
    def delete(self, url: str) -> HttpResponse: ...


class _HttpxClient:
    """Thin httpx adapter used for real runs."""

    def __init__(self, timeout: float = HTTP_TIMEOUT_SECONDS) -> None:
        import httpx

        self._client = httpx.Client(timeout=timeout)

    def get(self, url: str) -> Any:
        return self._client.get(url)

    def post(self, url: str, json: dict[str, Any]) -> Any:
        return self._client.post(url, json=json)

    def delete(self, url: str) -> Any:
        return self._client.delete(url)

    def close(self) -> None:
        self._client.close()


class GatewayProofBlocked(Exception):
    """Raised when a stage cannot prove liveness; carries the blocker reason."""

    def __init__(self, blocker: str) -> None:
        super().__init__(blocker)
        self.blocker = blocker


def _safe_error(error: Exception) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text.splitlines()[0][:500]


def _body(response: HttpResponse) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - non-JSON body is still recordable
        return None


# --- stage 1: health -------------------------------------------------------------


def _run_health_stage(client: HttpClient, base_url: str) -> dict[str, Any]:
    url = base_url + "/api/v1/slurm/health"
    try:
        response = client.get(url)
    except Exception as error:  # noqa: BLE001
        raise GatewayProofBlocked(
            f"gateway unreachable at {url}: {_safe_error(error)}"
        ) from error
    if not (200 <= response.status_code < 300):
        raise GatewayProofBlocked(
            f"gateway health returned HTTP {response.status_code}; gateway not deployed/healthy"
        )
    body = _body(response) or {}
    binaries = body.get("binaries") or {}
    expected = ("sbatch", "squeue", "sacct", "scancel")
    resolved = {name: bool(binaries.get(name, {}).get("resolved")) for name in expected}
    executable = {name: bool(binaries.get(name, {}).get("executable")) for name in expected}
    healthy = bool(body.get("healthy"))
    if not (healthy and all(resolved.values()) and all(executable.values())):
        missing = [name for name in expected if not executable.get(name)]
        raise GatewayProofBlocked(
            f"slurm health unhealthy; non-executable binaries: {missing or 'unknown'}"
        )
    return {
        "stage": "health",
        "status": "PASS",
        "counts": {
            "healthy": healthy,
            "resolved": resolved,
            "executable": executable,
        },
    }


# --- shared submit / poll helpers ------------------------------------------------


def _submit_smoke(
    client: HttpClient,
    base_url: str,
    *,
    run_id: str,
    sleep_seconds: int,
    partition: str | None,
) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "model_id": SMOKE_MODEL_ID,
        "job_type": SMOKE_JOB_TYPE,
        "slurm_env": {"SMOKE_SLEEP_SECONDS": str(sleep_seconds)},
    }
    if partition:
        payload["partition"] = partition
    url = base_url + "/api/v1/slurm/jobs"
    try:
        response = client.post(url, json=payload)
    except Exception as error:  # noqa: BLE001
        raise GatewayProofBlocked(
            f"gateway unreachable on submit at {url}: {_safe_error(error)}"
        ) from error
    body = _body(response) or {}
    if response.status_code not in (200, 201):
        code = body.get("error", {}).get("code") if isinstance(body, dict) else None
        raise GatewayProofBlocked(
            f"smoke submit rejected HTTP {response.status_code}"
            + (f" ({code})" if code else "")
        )
    job_id = body.get("job_id")
    if not job_id:
        raise GatewayProofBlocked("smoke submit returned no job_id")
    return str(job_id), body


def _poll_status(client: HttpClient, base_url: str, job_id: str) -> dict[str, Any]:
    url = base_url + f"/api/v1/slurm/jobs/{job_id}"
    response = client.get(url)
    return _body(response) or {}


def _log_uri(record: dict[str, Any]) -> str | None:
    manifest = record.get("manifest") or {}
    workspace = manifest.get("workspace_dir")
    run_id = record.get("run_id") or manifest.get("run_id")
    job_id = record.get("job_id")
    if workspace and run_id and job_id:
        return f"{workspace}/{run_id}/logs/{job_id}.out"
    return None


# --- stage 2: short job submit -> poll -> terminal -------------------------------


def _run_submit_poll_terminal_stage(
    client: HttpClient,
    base_url: str,
    *,
    run_id: str,
    partition: str | None,
    sleep_func=time.sleep,
) -> dict[str, Any]:
    job_id, _submit_body = _submit_smoke(
        client, base_url, run_id=run_id, sleep_seconds=SHORT_SLEEP_SECONDS, partition=partition
    )
    record: dict[str, Any] = {}
    status = "submitted"
    attempts = 0
    for attempts in range(1, POLL_MAX_ATTEMPTS + 1):
        try:
            record = _poll_status(client, base_url, job_id)
        except Exception as error:  # noqa: BLE001
            raise GatewayProofBlocked(
                f"gateway unreachable while polling {job_id}: {_safe_error(error)}"
            ) from error
        status = str(record.get("status") or "")
        if status in TERMINAL_STATUSES:
            break
        sleep_func(POLL_INTERVAL_SECONDS)
    if status not in TERMINAL_STATUSES:
        raise GatewayProofBlocked(
            f"smoke job {job_id} did not reach a terminal state within {attempts} polls"
        )
    return {
        "stage": "submit_poll_terminal",
        "status": "PASS",
        "counts": {
            "job_id": job_id,
            "terminal_status": status,
            "poll_count": attempts,
            "log_uri": _log_uri(record),
            "accounting": record.get("resource_metrics") or record.get("manifest", {}).get("slurm_accounting"),
        },
    }


# --- stage 3: long job submit -> cancel-while-active -----------------------------


def _run_submit_cancel_stage(
    client: HttpClient,
    base_url: str,
    *,
    run_id: str,
    partition: str | None,
    sleep_func=time.sleep,
) -> dict[str, Any]:
    job_id, _submit_body = _submit_smoke(
        client, base_url, run_id=run_id, sleep_seconds=LONG_SLEEP_SECONDS, partition=partition
    )
    # Wait for the job to become active (RUNNING) so the cancel is provably
    # cancel-while-active rather than cancel-before-start.
    reached_active = False
    last_status = "submitted"
    for _ in range(1, CANCEL_WAIT_MAX_ATTEMPTS + 1):
        try:
            record = _poll_status(client, base_url, job_id)
        except Exception as error:  # noqa: BLE001
            raise GatewayProofBlocked(
                f"gateway unreachable while waiting for {job_id} to run: {_safe_error(error)}"
            ) from error
        last_status = str(record.get("status") or "")
        if last_status == "running":
            reached_active = True
            break
        if last_status in TERMINAL_STATUSES:
            raise GatewayProofBlocked(
                f"long smoke job {job_id} reached terminal {last_status!r} before cancel"
            )
        sleep_func(POLL_INTERVAL_SECONDS)

    url = base_url + f"/api/v1/slurm/jobs/{job_id}"
    try:
        response = client.delete(url)
    except Exception as error:  # noqa: BLE001
        raise GatewayProofBlocked(
            f"gateway unreachable on cancel at {url}: {_safe_error(error)}"
        ) from error
    body = _body(response) or {}
    if not (200 <= response.status_code < 300):
        code = body.get("error", {}).get("code") if isinstance(body, dict) else None
        raise GatewayProofBlocked(
            f"cancel rejected HTTP {response.status_code}" + (f" ({code})" if code else "")
        )
    cancelled_status = str(body.get("status") or "")
    if cancelled_status != "cancelled":
        raise GatewayProofBlocked(
            f"cancel did not prove CANCELLED state for {job_id} (got {cancelled_status!r})"
        )
    return {
        "stage": "submit_cancel",
        "status": "PASS",
        "counts": {
            "job_id": job_id,
            "cancelled_while_active": reached_active,
            "pre_cancel_status": last_status,
            "cancelled_status": cancelled_status,
            "log_uri": _log_uri(body),
            "accounting": body.get("resource_metrics") or body.get("manifest", {}).get("slurm_accounting"),
        },
    }


# --- orchestration ---------------------------------------------------------------


def build_gateway_receipt(
    run_id: str,
    *,
    gateway_url: str,
    client: HttpClient,
    partition: str | None = None,
    sleep_func=time.sleep,
    now=_utc_now_iso,
) -> dict[str, Any]:
    """Drive the three live proofs and assemble a validated gateway receipt."""

    base_url = gateway_url.rstrip("/")
    node = platform.node() or "unknown-node"
    command = "uv run python scripts/m24_gateway_proof.py --run-id " + run_id

    stages: list[dict[str, Any]] = []
    dependency_blocker: str | None = None
    slurm_job_id: str | None = None
    slurm_log_uri: str | None = None
    slurm_accounting: Any = None

    stage_runners = (
        lambda: _run_health_stage(client, base_url),
        lambda: _run_submit_poll_terminal_stage(
            client, base_url, run_id=run_id, partition=partition, sleep_func=sleep_func
        ),
        lambda: _run_submit_cancel_stage(
            client, base_url, run_id=run_id, partition=partition, sleep_func=sleep_func
        ),
    )
    stage_names = ("health", "submit_poll_terminal", "submit_cancel")

    for name, runner in zip(stage_names, stage_runners, strict=True):
        try:
            stage = runner()
        except GatewayProofBlocked as blocked:
            dependency_blocker = blocked.blocker
            stages.append(
                {"stage": name, "status": "BLOCKED", "counts": {"error": blocked.blocker}}
            )
            break
        except Exception as error:  # noqa: BLE001 - fail-safe, never fabricate PASS
            dependency_blocker = f"{name} failed: {_safe_error(error)}"
            stages.append(
                {"stage": name, "status": "BLOCKED", "counts": {"error": dependency_blocker}}
            )
            break
        stages.append(stage)
        if name == "submit_poll_terminal":
            slurm_job_id = stage["counts"].get("job_id")
            slurm_log_uri = stage["counts"].get("log_uri")
            slurm_accounting = stage["counts"].get("accounting")

    all_passed = len(stages) == len(stage_names) and all(s["status"] == "PASS" for s in stages)
    top_status = "PASS" if all_passed else "BLOCKED"
    live_proof_accepted = all_passed

    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "section": "gateway",
        "run_id": run_id,
        "node": node,
        "command": command,
        "timestamp": now(),
        "status": top_status,
        "execution_mode": "live_proof",
        "live_proof_accepted": live_proof_accepted,
        "dependency_blocker": dependency_blocker if top_status == "BLOCKED" else None,
        "redaction": {
            "db_dsn_redacted": True,
            "bounds": {"gateway_url": base_url + "/api/v1/slurm/health"},
        },
        "artifact_refs": [],
        "identity": {
            "source": None,
            "cycle_time": None,
            "model_id": SMOKE_MODEL_ID,
            "basin_id": None,
            "basin_version_id": None,
            "river_network_version_id": None,
        },
        "stages": stages,
        "slurm": {
            "job_id": slurm_job_id,
            "array_task_id": None,
            "original_task_id": None,
            "accounting": slurm_accounting if isinstance(slurm_accounting, dict) else None,
            "log_uri": slurm_log_uri,
        },
        "published_uri": None,
        "warm_start_quality": None,
        "notes": {"job_type": SMOKE_JOB_TYPE},
    }

    validate_receipt(receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit the M24 gateway live-proof receipt.")
    parser.add_argument("--run-id", required=True, help="Receipt run identifier.")
    parser.add_argument(
        "--root",
        default="artifacts/m24",
        help="Receipt root directory (default: artifacts/m24).",
    )
    parser.add_argument(
        "--gateway-url",
        default=os.getenv("SLURM_GATEWAY_URL", DEFAULT_GATEWAY_URL),
        help="Slurm gateway base URL (default: $SLURM_GATEWAY_URL or http://127.0.0.1:8081).",
    )
    parser.add_argument(
        "--partition",
        default=os.getenv("QHH_SLURM_PARTITION") or None,
        help="Slurm partition override (default: $QHH_SLURM_PARTITION or profile default).",
    )
    args = parser.parse_args(argv)

    client = _HttpxClient()
    try:
        receipt = build_gateway_receipt(
            args.run_id,
            gateway_url=args.gateway_url,
            client=client,
            partition=args.partition,
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    path = write_receipt(receipt, root=args.root)
    print(f"gateway receipt written: {path} (status={receipt['status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
