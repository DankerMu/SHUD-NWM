"""A state_save_qc submit whose outcome is unknown must not become a permanent failure (#2584).

Production shape (2026-09-23, ``basins_hlj`` IFS 12Z): the gateway answered the
``state_save_qc`` array submit with HTTP 502 ``SLURM_PARSE_ERROR`` while Slurm had in fact
accepted the job.  ``SLURM_PARSE_ERROR`` is on no transient list, so the cohort master row was
declined for automatic retry and marked ``permanently_failed``.  The requirement: once the
submit crossed the gateway call boundary and the failure is not a proven rejection, the row is
recorded as the transient ``STATE_SAVE_SUBMIT_AMBIGUOUS`` (the gateway code kept as
``origin_error_code``), so the ordinary in-stage retry picks it up.  Proven rejections,
submits that never reached the gateway, and every other stage keep the gateway code.

Seam: the real stage-execution entry (``_submit_and_wait_cycle_stage``) against the real
``HttpSlurmGatewayClient`` -- only ``httpx`` (the network) is replaced -- and a real file
journal repository; the retry decision is read through ``FileJournalRetryService``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from packages.common.object_store import LocalObjectStore
from services.orchestrator.chain import (
    M3_STAGES,
    CycleOrchestrationContext,
    ForecastOrchestrator,
    HttpSlurmGatewayClient,
    OrchestratorConfig,
    OrchestratorError,
)
from services.orchestrator.chain_config import SubmitDisposition
from services.orchestrator.file_orchestration_journal import (
    FileJournalRetryService,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.retry import (
    NON_TRANSIENT_ERROR_CODES,
    TRANSIENT_ERROR_CODES,
    classify_failure,
    failure_classifier,
)
from services.orchestrator.scheduler_state_types import TRANSIENT_RETRY_REASON_CODES

_CONVERT = M3_STAGES[0]
_STATE_SAVE_QC = M3_STAGES[4]
_CYCLE = "2026092212"
_MODEL_ID = "dg_8a34ed2ba8f8dd22f2716405569628a9"
# The single-model cohort master that carried the production row.
_COHORT_RUN_ID = f"cycle_ifs_{_CYCLE}_convert_{_MODEL_ID}"


def _gateway_answers(monkeypatch: pytest.MonkeyPatch, status_code: int, payload: dict[str, Any]) -> list[str]:
    """Replace only the network: every gateway request gets this one HTTP answer."""

    calls: list[str] = []

    class _HttpClient:
        def __enter__(self) -> _HttpClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def request(self, method: str, path: str, **_kwargs: Any) -> httpx.Response:
            calls.append(f"{method} {path}")
            return httpx.Response(status_code, json=payload)

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: _HttpClient())
    return calls


def _hlj_cohort_context(orchestrator: Any) -> CycleOrchestrationContext:
    cycle_time = _dt("2026-09-22T12:00:00Z")
    basins = orchestrator._normalize_cycle_basins(
        [
            {
                "model_id": _MODEL_ID,
                "basin_id": "basins_hlj",
                "basin_version_id": "basins_hlj_vbasins",
                "river_network_version_id": "basins_hlj_rivnet_vbasins",
                "run_id": f"fcst_ifs_{_CYCLE}_{_MODEL_ID}",
                "model_package_uri": "s3://nhms/models/basins_hlj/package/",
                "model_package_checksum": "sha256:hlj",
            }
        ],
        "IFS",
        cycle_time,
    )
    return CycleOrchestrationContext(
        source_id="IFS",
        cycle_time=cycle_time,
        cycle_id=f"ifs_{_CYCLE}",
        run_id=_COHORT_RUN_ID,
        all_basins=basins,
        active_basins=list(basins),
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _hlj_orchestrator(tmp_path: Path) -> tuple[Any, FileOrchestrationJournalRepository]:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.ensure_forecast_cycle(source_id="IFS", cycle_time=_dt("2026-09-22T12:00:00Z"))
    object_root = tmp_path / "object-store"
    orchestrator = ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            poll_interval_seconds=0,
            job_timeout_seconds=120.0,
        ),
        repository=repository,
        slurm_client=HttpSlurmGatewayClient("http://gateway.test"),
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )
    return orchestrator, repository


def _submit_stage(tmp_path: Path, stage: Any) -> tuple[Any, FileOrchestrationJournalRepository]:
    orchestrator, repository = _hlj_orchestrator(tmp_path)
    result, aggregation = orchestrator._submit_and_wait_cycle_stage(stage, _hlj_cohort_context(orchestrator))
    assert aggregation is None
    return result, repository


def _submission_failure_events(repository: FileOrchestrationJournalRepository, job_id: str) -> list[dict[str, Any]]:
    rows = repository._cycle_rows(source_id="IFS", cycle_time=_dt("2026-09-22T12:00:00Z"), model_id=None)
    events = [
        event
        for event in rows.pipeline_events
        if event.get("entity_id") == job_id and event.get("event_type") == "submission"
    ]
    if not events:
        rows = repository._cycle_rows(
            source_id="IFS", cycle_time=_dt("2026-09-22T12:00:00Z"), model_id=_MODEL_ID
        )
        events = [
            event
            for event in rows.pipeline_events
            if event.get("entity_id") == job_id and event.get("event_type") == "submission"
        ]
    return events


def test_state_save_qc_gateway_parse_error_is_recorded_as_a_transient_ambiguous_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _gateway_answers(monkeypatch, 502, {"detail": {"error": {"code": "SLURM_PARSE_ERROR"}}})

    result, repository = _submit_stage(tmp_path, _STATE_SAVE_QC)

    # The submit really reached the gateway: this is the post-boundary case.
    assert calls and calls[0].startswith("POST ")
    assert result.status == "submission_failed"
    assert result.error_code == "STATE_SAVE_SUBMIT_AMBIGUOUS"
    assert result.error_message == "Slurm Gateway returned HTTP 502."
    row = repository.get_pipeline_job(result.pipeline_job_id)
    assert row is not None
    assert row["stage"] == "state_save_qc"
    assert row["status"] == "submission_failed"
    assert row["error_code"] == "STATE_SAVE_SUBMIT_AMBIGUOUS"
    assert row["error_message"] == "Slurm Gateway returned HTTP 502."
    events = _submission_failure_events(repository, result.pipeline_job_id)
    assert len(events) == 1
    assert events[0]["status_to"] == "submission_failed"
    assert events[0]["details"]["origin_error_code"] == "SLURM_PARSE_ERROR"
    # The forecast-cycle status carries the same recorded code as the row.
    cycle = repository._cycle_rows(
        source_id="IFS", cycle_time=_dt("2026-09-22T12:00:00Z"), model_id=None
    ).forecast_cycle
    assert cycle["error_code"] == "STATE_SAVE_SUBMIT_AMBIGUOUS"

    retry = FileJournalRetryService(repository)
    assert retry.should_auto_retry(row) is True
    policy = retry.retry_policy_for_job(row)
    assert policy["classifier"] == "transient_slurm_runtime"
    assert policy["permanent"] is False


def test_state_save_qc_proven_rejection_keeps_the_gateway_code_and_is_not_auto_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway_answers(monkeypatch, 422, {"error": {"code": "VALIDATION_ERROR"}})

    result, repository = _submit_stage(tmp_path, _STATE_SAVE_QC)

    assert result.status == "submission_failed"
    assert result.error_code == "VALIDATION_ERROR"
    row = repository.get_pipeline_job(result.pipeline_job_id)
    assert row["error_code"] == "VALIDATION_ERROR"
    events = _submission_failure_events(repository, result.pipeline_job_id)
    assert len(events) == 1
    assert "origin_error_code" not in events[0]["details"]
    assert FileJournalRetryService(repository).should_auto_retry(row) is False


def test_convert_gateway_parse_error_keeps_the_gateway_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway_answers(monkeypatch, 502, {"detail": {"error": {"code": "SLURM_PARSE_ERROR"}}})

    result, repository = _submit_stage(tmp_path, _CONVERT)

    assert result.status == "submission_failed"
    assert result.error_code == "SLURM_PARSE_ERROR"
    row = repository.get_pipeline_job(result.pipeline_job_id)
    assert row["stage"] == "convert"
    assert row["error_code"] == "SLURM_PARSE_ERROR"
    events = _submission_failure_events(repository, result.pipeline_job_id)
    assert len(events) == 1
    assert "origin_error_code" not in events[0]["details"]
    assert FileJournalRetryService(repository).should_auto_retry(row) is False


def test_state_save_qc_failure_before_the_gateway_boundary_keeps_its_own_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _gateway_answers(monkeypatch, 502, {"detail": {"error": {"code": "SLURM_PARSE_ERROR"}}})

    def _index_write_fails(*_args: Any, **_kwargs: Any) -> Path:
        # Carries an AMBIGUOUS disposition on purpose: only the boundary flag, not the
        # disposition, may decide that a pre-gateway failure keeps its code.
        error = OrchestratorError("CYCLE_MANIFEST_INDEX_WRITE_FAILED", "index write failed")
        error.submit_disposition = SubmitDisposition.AMBIGUOUS
        raise error

    orchestrator, repository = _hlj_orchestrator(tmp_path)
    monkeypatch.setattr(orchestrator, "_write_cycle_manifest_index", _index_write_fails)
    context = _hlj_cohort_context(orchestrator)

    result, _aggregation = orchestrator._submit_and_wait_cycle_stage(_STATE_SAVE_QC, context)

    assert calls == []
    assert result.status == "submission_failed"
    assert result.error_code == "CYCLE_MANIFEST_INDEX_WRITE_FAILED"
    row = repository.get_pipeline_job(result.pipeline_job_id)
    assert row["error_code"] == "CYCLE_MANIFEST_INDEX_WRITE_FAILED"
    events = _submission_failure_events(repository, result.pipeline_job_id)
    assert len(events) == 1
    assert "origin_error_code" not in events[0]["details"]


def test_state_save_submit_ambiguous_is_registered_transient_on_both_surfaces() -> None:
    assert "STATE_SAVE_SUBMIT_AMBIGUOUS" in TRANSIENT_ERROR_CODES
    assert "STATE_SAVE_SUBMIT_AMBIGUOUS" in TRANSIENT_RETRY_REASON_CODES
    assert "STATE_SAVE_SUBMIT_AMBIGUOUS" not in NON_TRANSIENT_ERROR_CODES
    assert failure_classifier("STATE_SAVE_SUBMIT_AMBIGUOUS") == "transient_slurm_runtime"
    failure = classify_failure("STATE_SAVE_SUBMIT_AMBIGUOUS", attempt=1, retry_limit=3)
    assert failure["retryable"] is True
    assert failure["permanent"] is False
    # Budget still bounds it: an exhausted retry limit is permanent, as for every transient code.
    assert classify_failure("STATE_SAVE_SUBMIT_AMBIGUOUS", attempt=3, retry_limit=3)["permanent"] is True
