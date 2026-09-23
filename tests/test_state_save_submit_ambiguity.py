"""A state_save_qc submit whose outcome is unknown must not become a permanent failure (#2584).

Production shape (2026-09-23, ``basins_hlj`` IFS 12Z): the gateway answered the
``state_save_qc`` array submit with HTTP 502 ``SLURM_PARSE_ERROR`` while Slurm had in fact
accepted the job.  ``SLURM_PARSE_ERROR`` is on no transient list, so the cohort master row was
declined for automatic retry and marked ``permanently_failed``.  The requirement: once the
submit crossed the gateway call boundary and the failure is not a proven rejection, the row is
recorded as the transient ``STATE_SAVE_SUBMIT_AMBIGUOUS`` (the gateway code kept as
``origin_error_code``), so the ordinary in-stage retry picks it up.  Proven rejections,
submits that never reached the gateway, and every other stage keep the gateway code.

Seams: the real stage-execution entry (``_submit_and_wait_cycle_stage``) and the real cycle
stage loop (``orchestrate_cycle``) against the real ``HttpSlurmGatewayClient`` -- only ``httpx``
(the network) and the Slurm runtime behind it are replaced -- and a real file journal
repository; the retry decision is read through ``FileJournalRetryService`` and the scheduler's
candidate-state decision.
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
from services.orchestrator.chain_stage_execution import STATE_SAVE_SUBMIT_AMBIGUOUS, is_state_save_qc_stage
from services.orchestrator.chain_stages import COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE, StageDefinition
from services.orchestrator.file_orchestration_journal import (
    FileJournalRetryService,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.retry import (
    NON_TRANSIENT_ERROR_CODES,
    TRANSIENT_ERROR_CODES,
    RetryConfig,
    classify_failure,
    failure_classifier,
)
from services.orchestrator.scheduler_state_types import TRANSIENT_RETRY_REASON_CODES
from tests.test_orchestration_chain import FakeCycleSlurmClient, _basins, _orchestrator

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


def test_only_the_state_save_qc_stage_carries_the_ambiguous_code() -> None:
    # The recorded code is the literal the transient tables register (independent oracle).
    assert STATE_SAVE_SUBMIT_AMBIGUOUS == "STATE_SAVE_SUBMIT_AMBIGUOUS"
    assert is_state_save_qc_stage(_STATE_SAVE_QC) is True
    # A stage named only by its job type still normalizes to state_save_qc.
    unnamed = StageDefinition("", "save_state_snapshot_array", "x.sbatch", "complete", "failed")
    assert is_state_save_qc_stage(unnamed) is True
    assert [stage.stage for stage in M3_STAGES if is_state_save_qc_stage(stage)] == ["state_save_qc"]


# --- the real cycle stage loop: in-stage resubmit and its exhaustion ----------------------
#
# ``orchestrate_cycle`` drives forecast then state_save_qc (production terminal stage
# ``forecast_state_save_qc``).  Every submit goes through the real ``HttpSlurmGatewayClient``;
# the network answers the first ``ambiguous_posts`` state_save_qc POSTs with HTTP 502
# ``SLURM_PARSE_ERROR`` and every other POST with 201 carrying the job the Slurm runtime
# double accepted.  Status/array polls go straight to that runtime double.  The cohort run id
# has the production single-model shape ``cycle_<source>_<cycle>_convert_<model>``.

_LOOP_CYCLE = "2026050100"
_LOOP_CYCLE_ID = f"gfs_{_LOOP_CYCLE}"
_LOOP_CYCLE_TIME = _dt("2026-05-01T00:00:00Z")
_LOOP_MODEL = "model_0"
_LOOP_COHORT_RUN_ID = f"cycle_gfs_{_LOOP_CYCLE}_convert_{_LOOP_MODEL}"
_LOOP_STATE_SAVE_JOB = f"job_{_LOOP_COHORT_RUN_ID}_state_save_qc"
_PARSE_ERROR_502 = {"detail": {"error": {"code": "SLURM_PARSE_ERROR"}}}


class _GatewayOverRuntime(HttpSlurmGatewayClient):
    """The real gateway client; only reads bypass HTTP to the Slurm runtime double."""

    def __init__(self, runtime: FakeCycleSlurmClient) -> None:
        super().__init__("http://gateway.test")
        self.runtime = runtime

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        return self.runtime.get_job_status(job_id)

    def get_array_task_results(self, job_id: str) -> list[dict[str, Any]]:
        return self.runtime.get_array_task_results(job_id)

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        return self.runtime.fetch_logs(job_id)


def _scripted_gateway(
    monkeypatch: pytest.MonkeyPatch, runtime: FakeCycleSlurmClient, *, ambiguous_posts: int
) -> list[tuple[str, str, str]]:
    posts: list[tuple[str, str, str]] = []

    class _HttpClient:
        def __enter__(self) -> _HttpClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def request(self, method: str, path: str, *, json: Any = None, headers: Any = None) -> httpx.Response:
            del headers
            payload = dict(json or {})
            stage_name = str(payload.get("stage_name") or "")
            posts.append((method, path, stage_name))
            state_save_posts = sum(1 for post in posts if post[2] == "state_save_qc")
            if stage_name == "state_save_qc" and state_save_posts <= ambiguous_posts:
                return httpx.Response(502, json=_PARSE_ERROR_502)
            accepted = runtime.submit_job_array(
                str(payload["job_type"]),
                cycle_id=str(payload["cycle_id"]),
                stage_name=stage_name,
                tasks=[dict(task) for task in payload["tasks"]],
                manifest=dict(payload["manifest"]),
            )
            return httpx.Response(201, json=accepted)

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: _HttpClient())
    return posts


def _loop_basins() -> list[dict[str, Any]]:
    basin = _basins(1)[0]
    basin.update(
        {
            "run_id": f"fcst_gfs_{_LOOP_CYCLE}_{_LOOP_MODEL}",
            "candidate_id": f"gfs:2026-05-01T00:00:00Z:{_LOOP_MODEL}:forecast_gfs_deterministic",
            "orchestration_run_id": _LOOP_COHORT_RUN_ID,
            "restart_stage": "forecast",
            "state_evidence": {"restart_stage": "forecast"},
            "model_package_uri": f"s3://nhms/models/{_LOOP_MODEL}.tar",
            "model_package_checksum": f"sha256:{_LOOP_MODEL}",
            "init_state_id": f"state_gfs_{_LOOP_MODEL}_2026050100_gfs_2026043012_f012",
            "init_state_uri": f"s3://nhms/states/gfs/{_LOOP_MODEL}/2026050100/state.cfg.ic",
            "init_state_checksum": f"sha256:state-{_LOOP_MODEL}",
            "init_state_valid_time": "2026-05-01T00:00:00Z",
        }
    )
    return [basin]


def _run_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ambiguous_posts: int,
    retry_service_cls: type[FileJournalRetryService] = FileJournalRetryService,
) -> tuple[Any, FileOrchestrationJournalRepository, list[tuple[str, str, str]]]:
    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE)
    runtime = FakeCycleSlurmClient()
    posts = _scripted_gateway(monkeypatch, runtime, ambiguous_posts=ambiguous_posts)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    retry_service = retry_service_cls(repository, RetryConfig(max_retries=1, backoff_schedule=[0]))
    orchestrator = _orchestrator(
        tmp_path,
        repository,
        _GatewayOverRuntime(runtime),
        terminal_stage=COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE,
        retry_service=retry_service,
    )
    result = orchestrator.orchestrate_cycle("gfs", _LOOP_CYCLE, _loop_basins())
    return result, repository, posts


def _state_save_rows(repository: FileOrchestrationJournalRepository) -> dict[str, dict[str, Any]]:
    return {
        row["job_id"]: row
        for row in repository.query_pipeline_jobs_by_cycle(_LOOP_CYCLE_ID)
        if row["stage"] == "state_save_qc"
    }


def _state_save_events(repository: FileOrchestrationJournalRepository) -> list[dict[str, Any]]:
    rows = repository._cycle_rows(source_id="gfs", cycle_time=_LOOP_CYCLE_TIME, model_id=None)
    return [
        event
        for event in rows.pipeline_events
        if str(event.get("entity_id") or "").startswith(_LOOP_STATE_SAVE_JOB)
    ]


def _loop_candidate_decision(repository: FileOrchestrationJournalRepository) -> Any:
    """The scheduler's next-pass decision for the cohort's model, read from the journal."""

    from services.orchestrator import scheduler as scheduler_module

    candidate = scheduler_module.SchedulerCandidate(
        candidate_id=f"gfs:2026-05-01T00:00:00Z:{_LOOP_MODEL}:forecast_gfs_deterministic",
        source_id="gfs",
        cycle_id=_LOOP_CYCLE_ID,
        cycle_time_utc=_LOOP_CYCLE_TIME,
        model_id=_LOOP_MODEL,
        basin_id="basin_0",
        basin_version_id="basin_v0",
        river_network_version_id="river_v0",
        segment_count=3,
        output_segment_count=3,
        model_package_uri=f"s3://nhms/models/{_LOOP_MODEL}.tar",
        resource_profile={},
        display_capabilities={},
        horizon={},
        scenario_id="forecast_gfs_deterministic",
        run_id=f"fcst_gfs_{_LOOP_CYCLE}_{_LOOP_MODEL}",
        forcing_version_id=f"forc_gfs_{_LOOP_CYCLE}_{_LOOP_MODEL}",
        status="selected",
    )
    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=_LOOP_CYCLE_TIME,
        model_id=_LOOP_MODEL,
        run_id=candidate.run_id,
        forcing_version_id=candidate.forcing_version_id,
        candidate_id=candidate.candidate_id,
        retry_limit=1,
    )
    assert state is not None
    return scheduler_module._candidate_state_decision(candidate, state)


def test_ambiguous_state_save_qc_submit_is_resubmitted_in_stage_and_the_cycle_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, repository, posts = _run_cycle(tmp_path, monkeypatch, ambiguous_posts=1)

    # A second state_save_qc POST really went out, after backoff, in the same pass.
    assert posts == [
        ("POST", "/api/v1/slurm/job-arrays", "forecast"),
        ("POST", "/api/v1/slurm/job-arrays", "state_save_qc"),
        ("POST", "/api/v1/slurm/job-arrays", "state_save_qc"),
    ]
    rows = _state_save_rows(repository)
    assert rows[_LOOP_STATE_SAVE_JOB]["status"] == "submission_failed"
    assert rows[_LOOP_STATE_SAVE_JOB]["error_code"] == "STATE_SAVE_SUBMIT_AMBIGUOUS"
    retry_row = rows[f"{_LOOP_STATE_SAVE_JOB}_retry_1"]
    assert retry_row["status"] == "succeeded"
    assert retry_row["error_code"] is None
    retry_events = [event for event in _state_save_events(repository) if event.get("event_type") == "retry"]
    assert len(retry_events) == 1
    assert retry_events[0]["entity_id"] == f"{_LOOP_STATE_SAVE_JOB}_retry_1"
    assert retry_events[0]["details"]["previous_error"] == "STATE_SAVE_SUBMIT_AMBIGUOUS"
    assert retry_events[0]["details"]["previous_job_id"] == _LOOP_STATE_SAVE_JOB
    assert retry_events[0]["details"]["trigger"] == "auto"
    # The stage and the cycle complete; nothing is left for an operator.
    assert [(stage.stage, stage.status) for stage in result.stages] == [
        ("forecast", "succeeded"),
        ("state_save_qc", "succeeded"),
    ]
    assert result.stages[-1].pipeline_job_id == f"{_LOOP_STATE_SAVE_JOB}_retry_1"
    assert result.status == "succeeded"
    assert all(row["status"] != "permanently_failed" for row in rows.values())


def test_exhausted_ambiguous_state_save_qc_retries_block_as_permanent_failure_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Runbook claim (scheduler-dbfree-typed-reasons.md, node22-control-plane-manual-recovery.md):
    # once the in-stage retries are spent, the row is permanently_failed with the recorded
    # STATE_SAVE_SUBMIT_AMBIGUOUS code, and the scheduler blocks the candidate as
    # permanent_failure / permanent_failure_guard -- not retry_limit_exhausted.
    result, repository, posts = _run_cycle(tmp_path, monkeypatch, ambiguous_posts=2)

    assert [post[2] for post in posts] == ["forecast", "state_save_qc", "state_save_qc"]
    assert result.status == "failed"
    rows = _state_save_rows(repository)
    exhausted = rows[f"{_LOOP_STATE_SAVE_JOB}_retry_1"]
    assert exhausted["status"] == "permanently_failed"
    assert exhausted["error_code"] == "STATE_SAVE_SUBMIT_AMBIGUOUS"
    assert exhausted["retry_count"] == 1

    decision = _loop_candidate_decision(repository)

    assert decision is not None
    assert (decision.action, decision.reason) == ("blocked", "permanent_failure_guard")
    assert decision.evidence["decision"] == "permanent_failure"
    failure = decision.evidence["failure"]
    assert failure["reason_code"] == "STATE_SAVE_SUBMIT_AMBIGUOUS"
    assert failure["stage"] == "state_save_qc"
    assert failure["limit_exhausted"] is True
    assert failure["permanent"] is True
    assert decision.evidence["retry_policy"]["automatic_retry_allowed"] is False


class _PassDiesBeforeThePermanentMark(FileJournalRetryService):
    def mark_permanently_failed(self, job: Any) -> Any:
        raise OrchestratorError("PASS_INTERRUPTED", "the pass died before the permanent mark landed")


def test_exhausted_ambiguous_row_left_unmarked_blocks_as_retry_limit_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other runbook arm: retry_limit_exhausted appears only while the row is still
    # submission_failed, i.e. the pass ended before the permanent mark landed.
    with pytest.raises(OrchestratorError, match="pass died before the permanent mark"):
        _run_cycle(tmp_path, monkeypatch, ambiguous_posts=2, retry_service_cls=_PassDiesBeforeThePermanentMark)

    # A fresh reader of the same journal root: the next pass's view.
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    exhausted = _state_save_rows(repository)[f"{_LOOP_STATE_SAVE_JOB}_retry_1"]
    assert exhausted["status"] == "submission_failed"
    assert exhausted["error_code"] == "STATE_SAVE_SUBMIT_AMBIGUOUS"

    decision = _loop_candidate_decision(repository)

    assert decision is not None
    assert (decision.action, decision.reason) == ("blocked", "retry_limit_exhausted")
    assert decision.evidence["decision"] == "permanent_failure"
    assert decision.evidence["failure"]["reason_code"] == "STATE_SAVE_SUBMIT_AMBIGUOUS"
