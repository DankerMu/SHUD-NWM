from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from services.orchestrator import chain_source_cycle, source_cycle_raw_manifest
from workers.data_adapters.base import cycle_id_for, format_cycle_time

DEFAULT_CANDIDATE_STATE_EVENT_LIMIT = 100
DEFAULT_CANDIDATE_STATE_JOB_LIMIT = 100
FAILED_PIPELINE_STATUSES = {"failed", "submission_failed", "partially_failed", "permanently_failed"}
TERMINAL_PIPELINE_SUCCESS_STATUSES = {"succeeded", "complete", "published"}
TERMINAL_PIPELINE_COMPLETION_STAGES = {"parse", "state_save_qc", "publish"}
_FORECAST_STAGE_ORDER = ("convert", "forcing", "forecast", "parse", "state_save_qc")
_COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE = "forecast_state_save_qc"
_COMPUTE_STATE_SAVE_QC_ALLOWED_STAGES = {"download", "convert", "forcing", "forecast", "state_save_qc"}
_COMPUTE_STATE_SAVE_QC_LEGACY_DOWNSTREAM_STAGES = {"parse", "publish"}
_STAGE_ALIASES = {
    "download": "download",
    "download_gfs": "download",
    "download_source_cycle": "download",
    "convert": "convert",
    "convert_canonical": "convert",
    "canonical": "convert",
    "forcing": "forcing",
    "produce_forcing": "forcing",
    "produce_forcing_array": "forcing",
    "forecast": "forecast",
    "run_shud_forecast": "forecast",
    "run_shud_forecast_array": "forecast",
    "parse": "parse",
    "parse_output": "parse",
    "parse_output_array": "parse",
    "state_save_qc": "state_save_qc",
    "save_state_snapshot": "state_save_qc",
    "save_state_snapshot_array": "state_save_qc",
    "publish": "publish",
    "publish_tiles": "publish",
}
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
_coerce_int = chain_source_cycle._coerce_int


def _stage_after(stage: str | None) -> str | None:
    if _compute_state_save_qc_terminal_enabled() and stage == "forecast":
        return "state_save_qc"
    if stage not in _FORECAST_STAGE_ORDER:
        return None
    index = _FORECAST_STAGE_ORDER.index(stage)
    if index + 1 >= len(_FORECAST_STAGE_ORDER):
        return None
    return _FORECAST_STAGE_ORDER[index + 1]


def _compute_state_save_qc_terminal_enabled() -> bool:
    return os.getenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", "").strip() == _COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE


def _normalized_record_stage(record: Mapping[str, Any]) -> str | None:
    candidates: list[Any] = [record.get("stage"), record.get("job_type")]
    details = record.get("details")
    if isinstance(details, Mapping):
        candidates.extend([details.get("stage"), details.get("job_type")])
    for raw in candidates:
        if raw in (None, ""):
            continue
        stage = _STAGE_ALIASES.get(str(raw), str(raw))
        if stage:
            return stage
    return None


def _record_allowed_for_compute_state_terminal(record: Mapping[str, Any]) -> bool:
    if not _compute_state_save_qc_terminal_enabled():
        return True
    stage = _normalized_record_stage(record)
    if stage in _COMPUTE_STATE_SAVE_QC_ALLOWED_STAGES:
        return True
    if stage in _COMPUTE_STATE_SAVE_QC_LEGACY_DOWNSTREAM_STAGES:
        return False
    return True


def _manual_retry_previous_job_ids(events: list[dict[str, Any]]) -> dict[str, tuple[str, dict[str, Any]]]:
    previous: dict[str, tuple[str, dict[str, Any]]] = {}
    for event in events:
        if event.get("event_type") not in {"retry", "manual_retry"}:
            continue
        entity_id = str(event.get("entity_id") or "")
        if not entity_id:
            continue
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        previous_job_id = details.get("previous_job_id") or details.get("failed_job_id")
        if previous_job_id in (None, ""):
            continue
        previous[entity_id] = (str(previous_job_id), event)
    return previous


def _manual_retry_event_for_job(job_id: str, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = [
        event
        for event in events
        if str(event.get("entity_id") or "") == job_id and event.get("event_type") in {"retry", "manual_retry"}
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda event: (
            _datetime_sort_key(event.get("created_at")),
            _numeric_sort_key(event.get("event_id")),
        ),
    )


def _job_stage_key(job: Mapping[str, Any]) -> tuple[str, str]:
    return str(job.get("stage") or ""), str(job.get("job_type") or "")


def _jobs_share_stage(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_stage, left_type = _job_stage_key(left)
    right_stage, right_type = _job_stage_key(right)
    return bool((left_stage and left_stage == right_stage) or (left_type and left_type == right_type))


def _linked_manual_retry_chain(
    retry_job: Mapping[str, Any],
    *,
    jobs_by_id: Mapping[str, Mapping[str, Any]],
    previous_by_job_id: Mapping[str, tuple[str, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    chain: list[dict[str, Any]] = []
    event: dict[str, Any] | None = None
    seen = {str(retry_job.get("job_id") or "")}
    current = retry_job
    for _ in range(16):
        previous_job_id = current.get("previous_job_id")
        current_event = None
        current_job_id = str(current.get("job_id") or "")
        if previous_job_id in (None, "") and current_job_id in previous_by_job_id:
            previous_job_id, current_event = previous_by_job_id[current_job_id]
        if previous_job_id in (None, ""):
            break
        previous_job_id = str(previous_job_id)
        if previous_job_id in seen:
            break
        previous = jobs_by_id.get(previous_job_id)
        if previous is None or not _jobs_share_stage(retry_job, previous):
            break
        seen.add(previous_job_id)
        chain.append(dict(previous))
        if current_event is not None:
            event = current_event
        current = previous
    return chain, event


def _annotated_manual_stage_repair_jobs(
    jobs: list[dict[str, Any]],
    repaired_by_failed_job_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]] | None:
    if not repaired_by_failed_job_id:
        return None
    repaired_failed_ids_by_retry_job_id: dict[str, list[str]] = {}
    for repair in sorted(
        repaired_by_failed_job_id.values(),
        key=lambda item: _pipeline_job_truth_sort_key(item["failed_job"]),
    ):
        retry_job_id = str(repair["retry_job"].get("job_id") or "")
        failed_job_id = str(repair["failed_job"].get("job_id") or "")
        if retry_job_id and failed_job_id:
            repaired_failed_ids_by_retry_job_id.setdefault(retry_job_id, []).append(failed_job_id)
    annotated: list[dict[str, Any]] = []
    changed = False
    for job in jobs:
        payload = dict(job)
        job_id = str(payload.get("job_id") or "")
        repair = repaired_by_failed_job_id.get(job_id)
        if repair is not None:
            retry_job_id = str(repair["retry_job"].get("job_id") or "")
            payload["repair_status"] = "repaired"
            payload["superseded_by_job_id"] = retry_job_id
            payload["repaired_by_job_id"] = retry_job_id
            payload["active_blocker"] = False
            changed = True
        elif job_id in repaired_failed_ids_by_retry_job_id:
            payload["repair_status"] = "repair_succeeded"
            payload["repairs_job_id"] = repaired_failed_ids_by_retry_job_id[job_id][0]
            payload["repairs_job_ids"] = repaired_failed_ids_by_retry_job_id[job_id]
            changed = True
        annotated.append(payload)
    return annotated if changed else None


def _manual_stage_repaired_evidence(
    failed_job: Mapping[str, Any],
    retry_job: Mapping[str, Any],
    event: Mapping[str, Any] | None,
    *,
    source_id: str,
    cycle_time: datetime,
    cycle_id: str,
) -> dict[str, Any]:
    stage = str(retry_job.get("stage") or failed_job.get("stage") or "")
    restart_stage = _stage_after(stage)
    payload = {
        "status": "repaired",
        "repair_status": "repaired",
        "stage": stage,
        "job_type": str(retry_job.get("job_type") or failed_job.get("job_type") or ""),
        "original_failed_job_id": failed_job.get("job_id"),
        "repairing_retry_job_id": retry_job.get("job_id"),
        "manual_retry_event_id": event.get("event_id") if isinstance(event, Mapping) else None,
        "manual_retry_marker": True,
        "source_id": source_id,
        "cycle_id": cycle_id,
        "cycle_time": cycle_time.isoformat().replace("+00:00", "Z"),
    }
    if restart_stage is not None:
        payload["restart_stage"] = restart_stage
        payload["restart_from_stage"] = restart_stage
    return payload


def _completed_stage_success_evidence(
    job: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    cycle_id: str,
) -> dict[str, Any] | None:
    if str(job.get("status") or "") not in TERMINAL_PIPELINE_SUCCESS_STATUSES:
        return None
    stage = _normalized_record_stage(job)
    restart_stage = _stage_after(stage)
    if stage is None or restart_stage is None:
        return None
    return {
        "status": "succeeded",
        "stage": stage,
        "job_type": str(job.get("job_type") or ""),
        "job_id": job.get("job_id"),
        "slurm_job_id": job.get("slurm_job_id"),
        "source_id": source_id,
        "cycle_id": cycle_id,
        "cycle_time": cycle_time.isoformat().replace("+00:00", "Z"),
        "restart_stage": restart_stage,
        "restart_from_stage": restart_stage,
    }


def _best_completed_stage_success_evidence(
    jobs: list[dict[str, Any]],
    *,
    source_id: str,
    cycle_time: datetime,
    cycle_id: str,
) -> dict[str, Any] | None:
    completed: list[tuple[int, tuple[Any, ...], dict[str, Any]]] = []
    stage_order = {stage: index for index, stage in enumerate(_FORECAST_STAGE_ORDER)}
    for job in jobs:
        evidence = _completed_stage_success_evidence(
            job,
            source_id=source_id,
            cycle_time=cycle_time,
            cycle_id=cycle_id,
        )
        if evidence is None:
            continue
        stage = str(evidence.get("stage") or "")
        completed.append((stage_order.get(stage, -1), _pipeline_job_truth_sort_key(job), evidence))
    if not completed:
        return None
    return max(completed, key=lambda item: (item[0], item[1]))[2]


def _has_terminal_completion_stage_success(jobs: list[dict[str, Any]]) -> bool:
    for job in jobs:
        if str(job.get("status") or "") not in TERMINAL_PIPELINE_SUCCESS_STATUSES:
            continue
        stage = _normalized_record_stage(job)
        if stage not in TERMINAL_PIPELINE_COMPLETION_STAGES:
            continue
        if _stage_after(stage) is None:
            return True
    return False


def _run_manifest_initial_state_for_run(run_id: str) -> dict[str, Any] | None:
    if not run_id or "/" in run_id or "\\" in run_id:
        return None
    root_value = os.getenv("OBJECT_STORE_ROOT") or os.getenv("NHMS_OBJECT_STORE_ROOT")
    if root_value in (None, ""):
        return None
    root = Path(str(root_value)).expanduser()
    try:
        root_resolved = root.resolve()
        manifest_path = (root_resolved / "runs" / run_id / "input" / "manifest.json").resolve()
        manifest_path.relative_to(root_resolved)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    initial_state = payload.get("initial_state")
    if not isinstance(initial_state, Mapping):
        return None
    return dict(initial_state)


def _candidate_manual_stage_repair_state(
    jobs: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    source_id: str,
    cycle_time: datetime,
    cycle_id: str,
    run_id: str,
    model_id: str,
) -> dict[str, Any]:
    candidate_jobs = [job for job in jobs if _job_belongs_to_candidate(job, run_id=run_id, model_id=model_id)]
    if not candidate_jobs:
        return {}
    jobs_by_id = {str(job.get("job_id") or ""): job for job in candidate_jobs if job.get("job_id") not in (None, "")}
    previous_by_job_id = _manual_retry_previous_job_ids(events)
    repaired_by_failed_job_id: dict[str, dict[str, Any]] = {}
    repair_events: dict[str, dict[str, Any] | None] = {}
    successful_retry_jobs = [
        job
        for job in candidate_jobs
        if job.get("manual_retry_marker") is True
        and str(job.get("status") or "") in TERMINAL_PIPELINE_SUCCESS_STATUSES
        and str(job.get("stage") or "") in _FORECAST_STAGE_ORDER
    ]
    for retry_job in sorted(successful_retry_jobs, key=_pipeline_job_truth_sort_key, reverse=True):
        chain, chain_event = _linked_manual_retry_chain(
            retry_job,
            jobs_by_id=jobs_by_id,
            previous_by_job_id=previous_by_job_id,
        )
        failed_jobs = [
            job
            for job in chain
            if str(job.get("status") or "") in FAILED_PIPELINE_STATUSES
            or (
                str(job.get("status") or "") == "pending"
                and job.get("slurm_job_id") in (None, "")
                and _coerce_int(job.get("retry_count"), default=0) > 0
            )
        ]
        if not failed_jobs:
            continue
        linked_failed_job_ids = {str(job.get("job_id") or "") for job in failed_jobs}
        retry_truth = _pipeline_job_truth_sort_key(retry_job)
        for job in candidate_jobs:
            job_id = str(job.get("job_id") or "")
            if not job_id or job_id in linked_failed_job_ids or job_id == str(retry_job.get("job_id") or ""):
                continue
            if not _jobs_share_stage(retry_job, job):
                continue
            if _pipeline_job_truth_sort_key(job) > retry_truth:
                continue
            status = str(job.get("status") or "")
            if status in FAILED_PIPELINE_STATUSES or (
                status == "pending"
                and job.get("slurm_job_id") in (None, "")
                and _coerce_int(job.get("retry_count"), default=0) > 0
            ):
                failed_jobs.append(job)
        event = _manual_retry_event_for_job(str(retry_job.get("job_id") or ""), events) or chain_event
        for failed_job in failed_jobs:
            failed_job_id = str(failed_job.get("job_id") or "")
            if failed_job_id:
                repaired_by_failed_job_id[failed_job_id] = {"failed_job": failed_job, "retry_job": retry_job}
                repair_events[failed_job_id] = event
        break
    if not repaired_by_failed_job_id:
        return {}
    annotated_jobs = _annotated_manual_stage_repair_jobs(jobs, repaired_by_failed_job_id)
    latest_repair = max(
        repaired_by_failed_job_id.values(),
        key=lambda item: (
            _pipeline_job_truth_sort_key(item["retry_job"]),
            _pipeline_job_truth_sort_key(item["failed_job"]),
        ),
    )
    failed_job_id = str(latest_repair["failed_job"].get("job_id") or "")
    return {
        "annotated_jobs": annotated_jobs,
        "repaired_stage_evidence": _manual_stage_repaired_evidence(
            latest_repair["failed_job"],
            latest_repair["retry_job"],
            repair_events.get(failed_job_id),
            source_id=source_id,
            cycle_time=cycle_time,
            cycle_id=cycle_id,
        ),
    }


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
    return candidate_state_from_rows(
        source_id=source_id,
        cycle_time=cycle_time,
        model_id=model_id,
        run_id=run_id,
        forcing_version_id=forcing_version_id,
        candidate_id=candidate_id,
        hydro_run=hydro_run,
        pipeline_jobs=jobs,
        pipeline_events=events,
        forcing_version=forcing_version,
        forecast_cycle=forecast_cycle,
        retry_limit=retry_limit,
        job_limit=job_limit,
        event_limit=event_limit,
    )


def candidate_state_from_rows(
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    run_id: str,
    forcing_version_id: str,
    candidate_id: str,
    hydro_run: Mapping[str, Any] | None,
    pipeline_jobs: list[dict[str, Any]],
    pipeline_events: list[dict[str, Any]],
    forcing_version: Mapping[str, Any] | None,
    forecast_cycle: Mapping[str, Any] | None,
    retry_limit: int | None = None,
    job_limit: int = DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
    event_limit: int = DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
) -> dict[str, Any] | None:
    cycle_id = cycle_id_for(source_id, cycle_time)
    cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
    job_limit = max(int(job_limit), 1)
    event_limit = max(int(event_limit), 1)
    indexed_jobs = [
        (index, dict(job))
        for index, job in enumerate(pipeline_jobs)
        if _record_allowed_for_compute_state_terminal(job)
    ]
    indexed_events = [
        (index, dict(event))
        for index, event in enumerate(pipeline_events)
        if _record_allowed_for_compute_state_terminal(event)
    ]
    jobs_total = len(indexed_jobs)
    jobs_truncated = jobs_total > job_limit
    jobs = [
        job
        for _, job in sorted(
            indexed_jobs,
            key=lambda indexed_job: (
                _pipeline_job_truth_sort_key(indexed_job[1]),
                _datetime_sort_key(indexed_job[1].get("created_at")),
            ),
            reverse=True,
        )
    ]
    jobs = sorted(
        jobs[:job_limit],
        key=lambda job: (
            _pipeline_job_truth_sort_key(job),
            _datetime_sort_key(job.get("created_at")),
        ),
    )
    events_total = len(indexed_events)
    events_truncated = events_total > event_limit
    events = [
        event
        for _, event in sorted(
            indexed_events,
            key=lambda indexed_event: (
                _datetime_sort_key(indexed_event[1].get("created_at")),
                _numeric_sort_key(indexed_event[1].get("event_id")),
            ),
            reverse=True,
        )
    ]
    events = sorted(
        events[:event_limit],
        key=lambda event: (
            _datetime_sort_key(event.get("created_at")),
            _numeric_sort_key(event.get("event_id")),
        ),
    )
    events = [_bounded_candidate_state_event(event) for event in events]
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
    manual_stage_repair_state = _candidate_manual_stage_repair_state(
        jobs,
        events,
        source_id=source_id,
        cycle_time=cycle_time,
        cycle_id=cycle_id,
        run_id=run_id,
        model_id=model_id,
    )
    if manual_stage_repair_state.get("annotated_jobs"):
        jobs = list(manual_stage_repair_state["annotated_jobs"])
    run_manifest_initial_state = _run_manifest_initial_state_for_run(run_id)
    if isinstance(hydro_run, Mapping) and isinstance(run_manifest_initial_state, Mapping):
        manifest_state_id = run_manifest_initial_state.get("state_id")
        if manifest_state_id not in (None, "") and hydro_run.get("init_state_id") in (None, ""):
            hydro_run = {
                **dict(hydro_run),
                "init_state_id": str(manifest_state_id),
                "initial_state_id": str(manifest_state_id),
                "initial_state_quality": run_manifest_initial_state.get("quality"),
            }
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
    if not isinstance(repaired_stage_evidence, Mapping):
        repaired_stage_evidence = manual_stage_repair_state.get("repaired_stage_evidence")
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
    if isinstance(run_manifest_initial_state, Mapping):
        state["run_manifest_initial_state"] = dict(run_manifest_initial_state)
    if isinstance(repaired_stage_evidence, Mapping):
        state["repaired_stage_evidence"] = dict(repaired_stage_evidence)
        restart_stage = repaired_stage_evidence.get("restart_stage")
        if restart_stage not in (None, ""):
            state["completed_stage_evidence"] = dict(repaired_stage_evidence)
            state["restart_stage"] = str(restart_stage)
            state["restart_from_stage"] = str(restart_stage)
            state["pipeline_status"] = None
            state["stage"] = None
            state["failed_stage"] = None
            state["error_code"] = None
            state["error_message"] = None
    elif not _has_terminal_completion_stage_success(candidate_jobs) and (
        completed_stage_evidence := _best_completed_stage_success_evidence(
            jobs,
            source_id=source_id,
            cycle_time=cycle_time,
            cycle_id=cycle_id,
        )
    ):
        state["completed_stage_evidence"] = completed_stage_evidence
        state["restart_stage"] = str(completed_stage_evidence["restart_stage"])
        state["restart_from_stage"] = str(completed_stage_evidence["restart_from_stage"])
    source_cycle_repair_evidence = _source_cycle_repair_evidence(source_cycle_download_state)
    if source_cycle_repair_evidence:
        state["source_cycle_repair_evidence"] = source_cycle_repair_evidence
    return state
