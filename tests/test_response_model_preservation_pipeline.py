"""#2348 oracle, pipeline half: ops routes keep the master JSON byte-for-byte.

Same oracle as ``tests/test_response_model_preservation.py`` (capture the
handler object, serialize it through the master ``dict[str, Any]`` field and
the route's model, compare type-strictly). Samples drive the real
``apps/api/routes/pipeline.py`` handlers over the sqlite-backed
``PipelineStore`` doubles of ``tests/test_monitoring_api.py`` and
``tests/test_retry_cancel_consistency.py``, so every payload is assembled by
the production projection code (`_job_payload`, `_stage_summaries`,
`_ok(identity=...)`, the cancel/retry result builders).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import tests.test_monitoring_api as monitoring
import tests.test_retry_cancel_consistency as cancels
from tests.api_contract_helpers import _RetryGateway
from tests.test_response_model_preservation import Sample, SampleEnv, ok, run_sample
from workers.data_adapters.base import cycle_id_for

OPERATOR = {"X-User-Role": "operator"}


def _strict_store(store: Any, log_root: Path) -> dict[str, str]:
    cycle_time = monitoring._cycle_time()
    cycle_id = cycle_id_for("GFS", cycle_time)
    monitoring._insert_cycle(store, cycle_time=cycle_time, source="GFS", current_state="forecast_running")
    monitoring._insert_hydro_run(
        store, "run_selected", status="succeeded", source_id="gfs", cycle_time=cycle_time, model_id="model_selected"
    )
    (log_root / "selected.log").write_text("selected log\nline 2\n", encoding="utf-8")
    common = {"run_id": "run_selected", "cycle_id": cycle_id, "model_id": "model_selected"}
    monitoring._create_job(
        store, job_id="job_download", stage="download", status="succeeded", log_uri="selected.log", **common
    )
    monitoring._create_job(store, job_id="job_forecast", stage="forecast", status="running", **common)
    monitoring._create_job(
        store,
        job_id="job_failed",
        stage="forcing",
        status="failed",
        error_code="NODE_FAILURE",
        error_message="node lost",
        **common,
    )
    return {
        "source": "GFS",
        "cycle_time": cycle_time.isoformat(),
        "run_id": "run_selected",
        "model_id": "model_selected",
    }


def _ops_reads(env: SampleEnv) -> None:
    env.monkeypatch.setenv("LOG_ROOT", str(env.tmp_path))
    with monitoring._store() as store:
        strict = _strict_store(store, env.tmp_path)
        monitoring._seed_monitoring_jobs(store, cycle_id=cycle_id_for("IFS", monitoring._cycle_time()))
        with monitoring._client(store) as client:
            non_strict = {"source": strict["source"], "cycle_time": strict["cycle_time"]}
            for params in (non_strict, strict):
                ok(client.get("/api/v1/pipeline/status", params=params))
                ok(client.get("/api/v1/pipeline/stages", params=params))
            ok(client.get("/api/v1/jobs", params={"limit": 50}))
            ok(client.get("/api/v1/jobs", params={**strict, "limit": 10, "sort_order": "asc"}))
            # Strict identity whose run filters match nothing: the empty early return.
            empty = ok(client.get("/api/v1/jobs", params={**strict, "run_type": "analysis"}))
            assert empty["data"]["items"] == []
            ok(client.get("/api/v1/jobs", params={"status": "cancelled"}))
            ok(client.get("/api/v1/jobs/job_download/logs"))
            ok(client.get("/api/v1/jobs/job_download/logs", params=strict))


def _metrics_and_queue(env: SampleEnv) -> None:
    with monitoring._store() as store:
        with monitoring._client(store) as client:
            assert ok(client.get("/api/v1/metrics/stage-duration", params={"days": 30}))["data"] == []
            assert ok(client.get("/api/v1/metrics/success-rate", params={"days": 30}))["data"] == []
        cycle_time = monitoring._cycle_time()
        monitoring._seed_monitoring_jobs(store, cycle_id=cycle_id_for("GFS", cycle_time))
        monitoring._create_job(store, job_id="job_ok", cycle_id=cycle_id_for("IFS", cycle_time), status="succeeded")
        gateway = monitoring._MockGateway(depth={"running": 2, "pending": 3, "idle": 1})
        with monitoring._client(store, gateway) as client:
            ok(client.get("/api/v1/metrics/stage-duration", params={"days": 30}))
            ok(client.get("/api/v1/metrics/success-rate", params={"days": 30}))
            ok(client.get("/api/v1/queue/depth"))
        with monitoring._client(store, _ListJobsGateway()) as client:
            assert ok(client.get("/api/v1/queue/depth"))["data"] == {"running": 1, "pending": 2, "idle": 0}


class _ListJobsGateway:
    """No ``queue_depth``: the route counts ``list_jobs`` records (`pipeline.py:917-929`)."""

    def list_jobs(self, *, limit: int, offset: int) -> list[Any]:
        del limit, offset
        return [SimpleNamespace(status=status) for status in ("running", "pending", "submitted", "failed")]


def _retry(env: SampleEnv) -> None:
    with monitoring._store() as store:
        monitoring._create_job(
            store,
            job_id="job_retry",
            run_id="run_retry",
            job_type=monitoring.GENERIC_RETRY_JOB_TYPE,
            stage="forecast",
            status="failed",
        )
        with monitoring._client(store, _RetryGateway(), allow_dev_role_header=True) as client:
            ok(client.post("/api/v1/runs/run_retry/retry", headers=OPERATOR))


def _cancel_store(store: Any, run_id: str, *, cycle_status: str = "forecast_running", jobs: dict[str, str]) -> None:
    cancels._insert_hydro_run(store, run_id, status="running")
    cancels._insert_forecast_cycle(store, f"cycle_{run_id}", status=cycle_status)
    for job_id, slurm_job_id in jobs.items():
        cancels._create_job(
            store, job_id=job_id, run_id=run_id, cycle_id=f"cycle_{run_id}", status="running", slurm_job_id=slurm_job_id
        )


def _cancel(env: SampleEnv) -> None:
    variants: list[tuple[str, dict[str, Any], dict[str, str], str]] = [
        ("run_clean", {}, {"job_clean": "slurm_clean"}, "forecast_running"),
        ("run_terminal_cycle", {}, {"job_terminal": "slurm_terminal"}, "published"),
        (
            "run_partial",
            {"failures": {"slurm_fail": cancels._scancel_path_bearing_error(502, "SLURM_ERROR", "run_partial")}},
            {"job_ok": "slurm_ok", "job_fail": "slurm_fail"},
            "forecast_running",
        ),
        (
            "run_blocked",
            {"failures": {"slurm_gone": cancels._scancel_path_bearing_error(404, "JOB_NOT_FOUND", "run_blocked")}},
            {"job_blocked": "slurm_gone"},
            "forecast_running",
        ),
        (
            "run_unproven",
            {"responses": {"slurm_unproven": {"job_id": "slurm_unproven", "status": "running"}}},
            {"job_unproven": "slurm_unproven"},
            "forecast_running",
        ),
    ]
    with cancels._store() as store:
        for run_id, gateway_kwargs, jobs, cycle_status in variants:
            _cancel_store(store, run_id, cycle_status=cycle_status, jobs=jobs)
            with cancels._client(store, gateway=cancels._MockGateway(**gateway_kwargs)) as client:
                ok(client.post(f"/api/v1/runs/{run_id}/cancel", headers=OPERATOR))
        # Nothing active left to cancel: the idempotent re-cancel.
        with cancels._client(store, gateway=cancels._MockGateway()) as client:
            ok(client.post("/api/v1/runs/run_clean/cancel", headers=OPERATOR))


PIPELINE_SAMPLES: dict[str, Sample] = {
    "ops_reads_non_strict_and_strict": Sample(
        _ops_reads,
        frozenset(
            {
                "GET /api/v1/pipeline/status",
                "GET /api/v1/pipeline/stages",
                "GET /api/v1/jobs",
                "GET /api/v1/jobs/{job_id}/logs",
            }
        ),
    ),
    "metrics_empty_and_seeded_queue_depth_both_sources": Sample(
        _metrics_and_queue,
        frozenset(
            {"GET /api/v1/metrics/stage-duration", "GET /api/v1/metrics/success-rate", "GET /api/v1/queue/depth"}
        ),
    ),
    "retry": Sample(_retry, frozenset({"POST /api/v1/runs/{run_id}/retry"})),
    "cancel_clean_terminal_partial_blocked_unproven_idempotent": Sample(
        _cancel, frozenset({"POST /api/v1/runs/{run_id}/cancel"})
    ),
}


@pytest.mark.parametrize("name", sorted(PIPELINE_SAMPLES))
def test_pipeline_response_model_preserves_the_master_json(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_sample(PIPELINE_SAMPLES[name], tmp_path, monkeypatch)
