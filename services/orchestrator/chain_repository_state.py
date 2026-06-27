from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from services.orchestrator import chain_source_cycle, source_cycle_raw_manifest
from workers.data_adapters.base import cycle_id_for, format_cycle_time

DEFAULT_CANDIDATE_STATE_EVENT_LIMIT = 100
DEFAULT_CANDIDATE_STATE_JOB_LIMIT = 100
FAILED_PIPELINE_STATUSES = {"failed", "submission_failed", "partially_failed", "permanently_failed"}
TERMINAL_PIPELINE_SUCCESS_STATUSES = {"succeeded", "complete", "published"}
_bounded_candidate_state_event = chain_source_cycle._bounded_candidate_state_event
_candidate_failed_task_from_events = chain_source_cycle._candidate_failed_task_from_events
_datetime_sort_key = chain_source_cycle._datetime_sort_key
_first_pipeline_truth_timestamp = chain_source_cycle._first_pipeline_truth_timestamp
_job_belongs_to_candidate = chain_source_cycle._job_belongs_to_candidate
_numeric_sort_key = chain_source_cycle._numeric_sort_key
_pipeline_job_is_repaired_stage_evidence = chain_source_cycle._pipeline_job_is_repaired_stage_evidence
_pipeline_job_truth_sort_key = chain_source_cycle._pipeline_job_truth_sort_key
_source_cycle_download_repair_state = chain_source_cycle._source_cycle_download_repair_state
_source_cycle_repair_evidence = chain_source_cycle._source_cycle_repair_evidence
_successful_sibling_task_count = chain_source_cycle._successful_sibling_task_count


def _forecast_cycle_has_ready_raw_manifest(
    forecast_cycle: Mapping[str, Any] | None,
    *,
    source_id: str,
    cycle_time: datetime,
    cycle_id: str,
) -> bool:
    if not isinstance(forecast_cycle, Mapping):
        return False
    status = str(forecast_cycle.get("status") or "")
    if status not in chain_source_cycle.RAW_MANIFEST_READY_CYCLE_STATUSES:
        return False
    return (
        chain_source_cycle._source_cycle_raw_manifest_binding(
            forecast_cycle,
            source_id=source_id,
            cycle_time=cycle_time,
            cycle_id=cycle_id,
        )
        is not None
    )


def candidate_state(
    self,
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    run_id: str,
    forcing_version_id: str,
    candidate_id: str,
    retry_limit: int | None = None,
    job_limit: int = DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
    event_limit: int = DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
) -> dict[str, Any] | None:
    cycle_id = cycle_id_for(source_id, cycle_time)
    cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
    job_limit = max(int(job_limit), 1)
    event_limit = max(int(event_limit), 1)
    hydro_run = self._fetch_optional(
        """
        SELECT
            run_id,
            scenario_id,
            model_id,
            basin_version_id,
            forcing_version_id,
            source_id,
            cycle_time,
            status,
            slurm_job_id,
            output_uri,
            log_uri,
            error_code,
            error_message,
            updated_at
        FROM hydro.hydro_run
        WHERE run_id = %s
           OR (
                source_id = %s
            AND cycle_time = %s
            AND model_id = %s
           )
        ORDER BY CASE WHEN run_id = %s THEN 0 ELSE 1 END, updated_at DESC
        LIMIT 1
        """,
        (run_id, source_id, cycle_time, model_id, run_id),
    )
    jobs = self._fetch_all(
        """
        SELECT
            job_id,
            run_id,
            cycle_id,
            job_type,
            slurm_job_id,
            array_task_id,
            model_id,
            status,
            stage,
            submitted_at,
            started_at,
            finished_at,
            exit_code,
            retry_count,
            manual_retry_marker,
            error_code,
            error_message,
            log_uri,
            created_at,
            updated_at
        FROM ops.pipeline_job
        WHERE (
                run_id = %s
             OR (cycle_id = %s AND model_id = %s)
             OR (cycle_id = %s AND run_id = %s)
             OR (cycle_id = %s AND model_id IS NULL AND run_id = %s)
              )
        ORDER BY
            COALESCE(updated_at, finished_at, submitted_at, started_at, created_at) DESC NULLS LAST,
            COALESCE(finished_at, submitted_at, started_at, created_at) DESC NULLS LAST,
            retry_count DESC NULLS LAST,
            created_at DESC,
            job_id DESC
        LIMIT %s
        """,
        (
            run_id,
            cycle_id,
            model_id,
            cycle_id,
            run_id,
            cycle_id,
            cycle_run_id,
            job_limit + 1,
        ),
    )
    jobs_total = len(jobs)
    jobs_truncated = jobs_total > job_limit
    jobs = sorted(
        jobs[:job_limit],
        key=lambda job: (
            _pipeline_job_truth_sort_key(job),
            _datetime_sort_key(job.get("created_at")),
        ),
    )
    events: list[dict[str, Any]] = []
    events_total = 0
    events_truncated = False
    events = self._fetch_all(
        """
        SELECT
            pe.event_id,
            pe.entity_type,
            pe.entity_id,
            pe.event_type,
            pe.status_from,
            pe.status_to,
            pe.message,
            pe.details,
            pe.created_at
        FROM ops.pipeline_event pe
        WHERE pe.entity_type = 'pipeline_job'
          AND pe.entity_id IN (
            SELECT pj.job_id
            FROM ops.pipeline_job pj
            WHERE (
                    pj.run_id = %s
                 OR (pj.cycle_id = %s AND pj.model_id = %s)
                 OR (pj.cycle_id = %s AND pj.run_id = %s)
                 OR (pj.cycle_id = %s AND pj.model_id IS NULL AND pj.run_id = %s)
                  )
          )
        ORDER BY pe.created_at DESC, pe.event_id DESC
        LIMIT %s
        """,
        (
            run_id,
            cycle_id,
            model_id,
            cycle_id,
            run_id,
            cycle_id,
            cycle_run_id,
            event_limit + 1,
        ),
    )
    events_total = len(events)
    events_truncated = events_total > event_limit
    events = sorted(
        events[:event_limit],
        key=lambda event: (
            _datetime_sort_key(event.get("created_at")),
            _numeric_sort_key(event.get("event_id")),
        ),
    )
    events = [_bounded_candidate_state_event(event) for event in events]
    forcing_version = self._fetch_optional(
        """
        SELECT
            forcing_version_id,
            model_id,
            source_id,
            cycle_time,
            start_time,
            end_time,
            station_count,
            forcing_package_uri,
            checksum,
            lineage_json,
            created_at
        FROM met.forcing_version
        WHERE forcing_version_id = %s
           OR (source_id = %s AND cycle_time = %s AND model_id = %s)
        ORDER BY CASE WHEN forcing_version_id = %s THEN 0 ELSE 1 END, created_at DESC
        LIMIT 1
        """,
        (forcing_version_id, source_id, cycle_time, model_id, forcing_version_id),
    )
    forecast_cycle = self._fetch_optional(
        """
        SELECT
            cycle_id,
            source_id,
            cycle_time,
            issue_time,
            status,
            manifest_uri,
            retry_count,
            error_code,
            error_message,
            created_at
        FROM met.forecast_cycle
        WHERE cycle_id = %s OR (source_id = %s AND cycle_time = %s)
        ORDER BY CASE WHEN cycle_id = %s THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (cycle_id, source_id, cycle_time, cycle_id),
    )
    nfs_raw_manifest = source_cycle_raw_manifest.nfs_raw_manifest_readiness_from_env(source_id, cycle_time)
    if isinstance(nfs_raw_manifest, Mapping) and nfs_raw_manifest.get("status") == "ready":
        if not _forecast_cycle_has_ready_raw_manifest(
            forecast_cycle,
            source_id=source_id,
            cycle_time=cycle_time,
            cycle_id=cycle_id,
        ):
            forecast_cycle = source_cycle_raw_manifest.forecast_cycle_from_raw_manifest_readiness(
                nfs_raw_manifest,
                source_id=source_id,
                cycle_time=cycle_time,
            )
    if (
        hydro_run is None
        and not jobs
        and forcing_version is None
        and forecast_cycle is None
        and nfs_raw_manifest is None
    ):
        return None
    source_cycle_download_state = _source_cycle_download_repair_state(
        jobs,
        events,
        forecast_cycle=forecast_cycle,
        source_id=source_id,
        cycle_time=cycle_time,
        cycle_id=cycle_id,
        cycle_run_id=cycle_run_id,
        jobs_truncated=jobs_truncated,
        events_truncated=events_truncated,
    )
    if source_cycle_download_state.get("annotated_jobs"):
        jobs = list(source_cycle_download_state["annotated_jobs"])
    candidate_jobs = [job for job in jobs if _job_belongs_to_candidate(job, run_id=run_id, model_id=model_id)]
    failed_task = _candidate_failed_task_from_events(
        events,
        model_id=model_id,
        candidate_id=candidate_id,
        run_id=run_id,
        cycle_id=cycle_id,
    )
    relevant_jobs = candidate_jobs or ([failed_task["job"]] if failed_task and failed_task.get("job") else [])
    latest_job = (relevant_jobs or jobs)[-1] if (relevant_jobs or jobs) else {}
    latest_shared_cycle_aggregate = bool(
        not candidate_jobs and latest_job.get("run_id") == cycle_run_id and latest_job.get("model_id") in (None, "")
    )
    latest_status = str(latest_job.get("status") or "")
    latest_job_repaired = _pipeline_job_is_repaired_stage_evidence(latest_job)
    latest_failed_job = latest_job if latest_status in FAILED_PIPELINE_STATUSES and not latest_job_repaired else {}
    latest_shared_cycle_success = bool(
        latest_shared_cycle_aggregate and latest_status in TERMINAL_PIPELINE_SUCCESS_STATUSES
    )
    latest_shared_cycle_failure = bool(
        latest_shared_cycle_aggregate
        and latest_status in FAILED_PIPELINE_STATUSES
        and not latest_job_repaired
        and failed_task is None
    )
    exposed_latest_job = {} if latest_shared_cycle_success or latest_shared_cycle_failure else latest_job
    pipeline_status = latest_job.get("status")
    if failed_task is not None and (
        not latest_job or latest_status in {"", "partially_failed"} or latest_shared_cycle_success
    ):
        latest_failed_job = failed_task["job"] if failed_task.get("job") else latest_failed_job
        pipeline_status = latest_failed_job.get("status")
        exposed_latest_job = latest_failed_job
    elif latest_shared_cycle_success or latest_shared_cycle_failure:
        pipeline_status = None
        latest_failed_job = {}
    active_source_cycle_failure = source_cycle_download_state.get("active_failure_job")
    repaired_stage_evidence = source_cycle_download_state.get("repaired_stage_evidence")
    if failed_task is None and not candidate_jobs and isinstance(active_source_cycle_failure, Mapping):
        latest_failed_job = dict(active_source_cycle_failure)
        exposed_latest_job = latest_failed_job
        pipeline_status = latest_failed_job.get("status")
        latest_shared_cycle_failure = False
    elif failed_task is None and not candidate_jobs and isinstance(repaired_stage_evidence, Mapping):
        latest_failed_job = {}
        exposed_latest_job = {}
        pipeline_status = None
        latest_shared_cycle_failure = False
        latest_shared_cycle_success = False
    successful_siblings = _successful_sibling_task_count(events, model_id=model_id)
    retry_count_jobs = relevant_jobs
    if not retry_count_jobs:
        retry_count_jobs = list(source_cycle_download_state.get("retry_count_jobs") or [])
    state = {
        "candidate_id": candidate_id,
        "run_id": run_id,
        "forcing_version_id": forcing_version_id,
        "retry_limit": retry_limit,
        "job_limit": job_limit,
        "event_limit": event_limit,
        "pipeline_jobs_total": jobs_total,
        "pipeline_events_total": events_total,
        "state_truncated": jobs_truncated or events_truncated,
        "hydro_run": hydro_run,
        "hydro_status": hydro_run.get("status") if hydro_run else None,
        "output_uri": hydro_run.get("output_uri") if hydro_run else None,
        "forcing_version": forcing_version,
        "forecast_cycle": forecast_cycle,
        "nfs_raw_manifest": dict(nfs_raw_manifest) if isinstance(nfs_raw_manifest, Mapping) else None,
        "pipeline_jobs": jobs,
        "pipeline_events": events,
        "pipeline_status": pipeline_status,
        "stage": (
            (failed_task or {}).get("stage") or latest_failed_job.get("stage") or exposed_latest_job.get("stage")
        ),
        "failed_stage": (failed_task or {}).get("stage") or latest_failed_job.get("stage"),
        "array_task_id": (failed_task or {}).get("array_task_id"),
        "original_task_id": (failed_task or {}).get("original_task_id"),
        "hydro_truth_timestamp": hydro_run.get("updated_at") if hydro_run else None,
        "pipeline_truth_timestamp": _first_pipeline_truth_timestamp(latest_failed_job or exposed_latest_job or {}),
        "error_code": (failed_task or {}).get("error_code")
        or latest_failed_job.get("error_code")
        or exposed_latest_job.get("error_code"),
        "error_message": (failed_task or {}).get("error_message")
        or latest_failed_job.get("error_message")
        or exposed_latest_job.get("error_message"),
        "retry_count": max((int(job.get("retry_count") or 0) for job in retry_count_jobs), default=0),
        "successful_sibling_outputs_reused": successful_siblings > 0,
        "successful_sibling_task_count": successful_siblings,
        "shared_cycle_aggregate": latest_shared_cycle_aggregate,
        "shared_cycle_ambiguous_failure": latest_shared_cycle_failure,
    }
    if isinstance(repaired_stage_evidence, Mapping):
        state["repaired_stage_evidence"] = dict(repaired_stage_evidence)
    source_cycle_repair_evidence = _source_cycle_repair_evidence(source_cycle_download_state)
    if source_cycle_repair_evidence:
        state["source_cycle_repair_evidence"] = source_cycle_repair_evidence
    return state
