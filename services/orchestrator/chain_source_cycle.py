from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlparse

from packages.common.source_identity import normalize_source_id
from workers.data_adapters.base import format_cycle_time

TERMINAL_PIPELINE_SUCCESS_STATUSES = {"succeeded", "complete", "published"}
FAILED_PIPELINE_STATUSES = {"failed", "submission_failed", "partially_failed", "permanently_failed"}
RAW_MANIFEST_READY_CYCLE_STATUSES = {"raw_complete", "canonical_ready", "forcing_ready", "complete", "published"}
MAX_CANDIDATE_STATE_TASK_RESULTS = 16


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_optional_nonnegative_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_gateway_time(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return _ensure_utc(value) if isinstance(value, datetime) else None
    if isinstance(value, str):
        return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    return None


def _format_time(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _source_cycle_download_repair_state(
    jobs: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    *,
    forecast_cycle: Mapping[str, Any] | None,
    source_id: str,
    cycle_time: datetime,
    cycle_id: str,
    cycle_run_id: str,
    jobs_truncated: bool = False,
    events_truncated: bool = False,
) -> dict[str, Any]:
    source_cycle_jobs = [
        dict(job) for job in jobs if _is_source_cycle_download_job(job, cycle_id=cycle_id, cycle_run_id=cycle_run_id)
    ]
    if not source_cycle_jobs:
        return {}

    failed_jobs = [job for job in source_cycle_jobs if str(job.get("status") or "") in FAILED_PIPELINE_STATUSES]
    if not failed_jobs:
        return {"retry_count_jobs": source_cycle_jobs}

    manifest_binding = _source_cycle_raw_manifest_binding(
        forecast_cycle,
        source_id=source_id,
        cycle_time=cycle_time,
        cycle_id=cycle_id,
    )
    repaired_by_failed_job_id: dict[str, dict[str, Any]] = {}
    if manifest_binding is not None:
        for failed_job in failed_jobs:
            repair = _linked_successful_source_cycle_retry(
                failed_job,
                source_cycle_jobs,
                events,
                cycle_id=cycle_id,
                cycle_run_id=cycle_run_id,
            )
            if repair is not None:
                repaired_by_failed_job_id[str(failed_job["job_id"])] = {
                    "failed_job": failed_job,
                    "retry_job": repair["retry_job"],
                    "event": repair["event"],
                    "manifest_binding": manifest_binding,
                }

    unrepaired_failed_jobs = [
        job for job in failed_jobs if str(job.get("job_id") or "") not in repaired_by_failed_job_id
    ]
    annotated_jobs = _annotated_source_cycle_repair_jobs(jobs, repaired_by_failed_job_id)
    payload: dict[str, Any] = {"retry_count_jobs": source_cycle_jobs}
    if annotated_jobs is not None:
        payload["annotated_jobs"] = annotated_jobs
    if unrepaired_failed_jobs:
        if manifest_binding is not None and (jobs_truncated or events_truncated):
            active_failure_job, inconclusive_failed_jobs = _source_cycle_truncated_failure_resolution(
                unrepaired_failed_jobs,
                source_cycle_jobs,
                cycle_id=cycle_id,
                cycle_run_id=cycle_run_id,
            )
            if inconclusive_failed_jobs:
                payload["repair_evidence_status"] = "inconclusive_truncated"
                payload["repair_evidence_truncated"] = True
                payload["repair_evidence_reason"] = "source_cycle_repair_window_truncated"
                payload["repair_evidence_unresolved_failed_job_ids"] = [
                    str(job.get("job_id"))
                    for job in sorted(inconclusive_failed_jobs, key=_pipeline_job_truth_sort_key)
                    if job.get("job_id") not in (None, "")
                ]
            if active_failure_job is not None:
                payload["active_failure_job"] = active_failure_job
            return payload
        payload["active_failure_job"] = max(unrepaired_failed_jobs, key=_pipeline_job_truth_sort_key)
        return payload
    latest_repair = max(
        repaired_by_failed_job_id.values(),
        key=lambda item: (
            _pipeline_job_truth_sort_key(item["retry_job"]),
            _source_cycle_original_failure_sort_key(item["failed_job"]),
            str(item["failed_job"].get("job_id") or ""),
        ),
    )
    payload["repaired_stage_evidence"] = _source_cycle_repaired_stage_evidence(
        latest_repair["failed_job"],
        latest_repair["retry_job"],
        latest_repair["event"],
        latest_repair["manifest_binding"],
        source_id=source_id,
        cycle_time=cycle_time,
        cycle_id=cycle_id,
    )
    return payload


def _source_cycle_truncated_failure_resolution(
    unrepaired_failed_jobs: Sequence[Mapping[str, Any]],
    source_cycle_jobs: Sequence[Mapping[str, Any]],
    *,
    cycle_id: str,
    cycle_run_id: str,
) -> tuple[dict[str, Any] | None, list[Mapping[str, Any]]]:
    repair_candidate_by_failed_job_id = {
        str(job.get("job_id"))
        for job in unrepaired_failed_jobs
        if job.get("job_id") not in (None, "")
        and _source_cycle_failed_job_has_later_repair_candidate(
            job,
            source_cycle_jobs,
            cycle_id=cycle_id,
            cycle_run_id=cycle_run_id,
        )
    }
    active_candidates = [
        job for job in unrepaired_failed_jobs if str(job.get("job_id") or "") not in repair_candidate_by_failed_job_id
    ]
    active_failure_job = dict(max(active_candidates, key=_pipeline_job_truth_sort_key)) if active_candidates else None
    active_failure_job_id = str(active_failure_job.get("job_id") or "") if active_failure_job else ""
    inconclusive_failed_jobs = [
        job
        for job in unrepaired_failed_jobs
        if str(job.get("job_id") or "") != active_failure_job_id
        and str(job.get("job_id") or "") in repair_candidate_by_failed_job_id
    ]
    return active_failure_job, inconclusive_failed_jobs


def _source_cycle_failed_job_has_later_repair_candidate(
    failed_job: Mapping[str, Any],
    source_cycle_jobs: Sequence[Mapping[str, Any]],
    *,
    cycle_id: str,
    cycle_run_id: str,
) -> bool:
    for retry_job in source_cycle_jobs:
        if str(retry_job.get("job_id") or "") == str(failed_job.get("job_id") or ""):
            continue
        if _source_cycle_retry_job_repairs_failure(
            retry_job,
            failed_job,
            cycle_id=cycle_id,
            cycle_run_id=cycle_run_id,
        ):
            return True
    return False


def _is_source_cycle_download_job(
    job: Mapping[str, Any],
    *,
    cycle_id: str,
    cycle_run_id: str,
) -> bool:
    if str(job.get("cycle_id") or "") != cycle_id:
        return False
    if str(job.get("run_id") or "") != cycle_run_id:
        return False
    if job.get("model_id") not in (None, ""):
        return False
    return _job_has_source_cycle_download_stage(job)


def _job_has_source_cycle_download_stage(job: Mapping[str, Any]) -> bool:
    stage = str(job.get("stage") or "")
    job_type = str(job.get("job_type") or "")
    return job_type == "download_source_cycle" or stage in {"download", "download_source_cycle", "download_gfs"}


def _source_cycle_raw_manifest_binding(
    forecast_cycle: Mapping[str, Any] | None,
    *,
    source_id: str,
    cycle_time: datetime,
    cycle_id: str,
) -> dict[str, Any] | None:
    if not isinstance(forecast_cycle, Mapping):
        return None
    status = str(forecast_cycle.get("status") or "")
    if status not in RAW_MANIFEST_READY_CYCLE_STATUSES and status != "failed_download":
        return None
    manifest_uri = forecast_cycle.get("manifest_uri")
    if manifest_uri in (None, ""):
        return None
    if not _raw_manifest_uri_matches_source_cycle(
        str(manifest_uri),
        source_id=source_id,
        cycle_time=cycle_time,
    ):
        return None
    return {
        "manifest_uri": str(manifest_uri),
        "forecast_cycle_status": status,
        "source_id": normalize_source_id(source_id),
        "cycle_id": cycle_id,
        "cycle_time": _format_time(cycle_time),
    }


def _raw_manifest_uri_matches_source_cycle(
    manifest_uri: str,
    *,
    source_id: str,
    cycle_time: datetime,
) -> bool:
    value = manifest_uri.strip()
    if not value:
        return False
    parsed = urlparse(value)
    if parsed.scheme:
        if parsed.scheme != "s3" or not parsed.netloc:
            return False
        if parsed.params or parsed.query or parsed.fragment:
            return False
        return _raw_manifest_key_matches_source_cycle(
            unquote(parsed.path).strip("/"),
            source_id=source_id,
            cycle_time=cycle_time,
            allow_prefix=True,
        )
    if parsed.netloc:
        return False
    return _raw_manifest_key_matches_source_cycle(
        unquote(value).strip("/"),
        source_id=source_id,
        cycle_time=cycle_time,
        allow_prefix=False,
    )


def _raw_manifest_key_matches_source_cycle(
    key: str,
    *,
    source_id: str,
    cycle_time: datetime,
    allow_prefix: bool,
) -> bool:
    if not key:
        return False
    parts = key.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if len(parts) < 4:
        return False
    if not allow_prefix and len(parts) != 4:
        return False
    raw, source, cycle, filename = parts[-4:]
    return (
        raw == "raw"
        and source.lower() == normalize_source_id(source_id).lower()
        and cycle == format_cycle_time(cycle_time)
        and filename == "manifest.json"
    )


def _linked_successful_source_cycle_retry(
    failed_job: Mapping[str, Any],
    source_cycle_jobs: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    *,
    cycle_id: str,
    cycle_run_id: str,
) -> dict[str, Any] | None:
    failed_job_id = str(failed_job.get("job_id") or "")
    if not failed_job_id:
        return None
    jobs_by_id = {str(job.get("job_id")): job for job in source_cycle_jobs if job.get("job_id") not in (None, "")}
    ancestor_ids_by_retry_job_id, events_by_retry_job_id = _source_cycle_retry_provenance(events)
    repairs: list[dict[str, Any]] = []
    for event in events:
        details = event.get("details")
        if event.get("event_type") not in {"retry", "manual_retry"} or not isinstance(details, Mapping):
            continue
        if details.get("trigger") != "manual" and details.get("manual_retry_marker") is not True:
            continue
        retry_job_id = (
            event.get("entity_id")
            or details.get("retry_job_id")
            or details.get("new_job_id")
            or details.get("job_id")
            or details.get("pipeline_job_id")
        )
        retry_job_id_text = str(retry_job_id or "")
        if not retry_job_id_text:
            continue
        retry_job = jobs_by_id.get(retry_job_id_text)
        if retry_job is None:
            continue
        if failed_job_id not in _bounded_retry_ancestor_ids(
            retry_job_id_text,
            ancestor_ids_by_retry_job_id,
            max_depth=max(len(events_by_retry_job_id), 1),
        ):
            continue
        if not _source_cycle_retry_job_repairs_failure(
            retry_job,
            failed_job,
            cycle_id=cycle_id,
            cycle_run_id=cycle_run_id,
        ):
            continue
        repair_event = events_by_retry_job_id.get(retry_job_id_text, event)
        repairs.append({"retry_job": dict(retry_job), "event": dict(repair_event)})
    if not repairs:
        return None
    return max(repairs, key=lambda item: _pipeline_job_truth_sort_key(item["retry_job"]))


def _source_cycle_retry_provenance(
    events: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, set[str]], dict[str, Mapping[str, Any]]]:
    ancestor_ids_by_retry_job_id: dict[str, set[str]] = {}
    events_by_retry_job_id: dict[str, Mapping[str, Any]] = {}
    for event in events:
        details = event.get("details")
        if event.get("event_type") not in {"retry", "manual_retry"} or not isinstance(details, Mapping):
            continue
        if details.get("trigger") != "manual" and details.get("manual_retry_marker") is not True:
            continue
        previous_job_id = details.get("previous_job_id") or details.get("failed_job_id")
        retry_job_id = (
            event.get("entity_id")
            or details.get("retry_job_id")
            or details.get("new_job_id")
            or details.get("job_id")
            or details.get("pipeline_job_id")
        )
        previous_job_id_text = str(previous_job_id or "")
        retry_job_id_text = str(retry_job_id or "")
        if not previous_job_id_text or not retry_job_id_text:
            continue
        ancestor_ids_by_retry_job_id.setdefault(retry_job_id_text, set()).add(previous_job_id_text)
        existing_event = events_by_retry_job_id.get(retry_job_id_text)
        if existing_event is None or _event_truth_sort_key(event) >= _event_truth_sort_key(existing_event):
            events_by_retry_job_id[retry_job_id_text] = event
    return ancestor_ids_by_retry_job_id, events_by_retry_job_id


def _bounded_retry_ancestor_ids(
    retry_job_id: str,
    ancestor_ids_by_retry_job_id: Mapping[str, set[str]],
    *,
    max_depth: int,
) -> set[str]:
    ancestors: set[str] = set()
    stack = list(ancestor_ids_by_retry_job_id.get(retry_job_id, set()))
    depth = 0
    while stack and depth < max_depth:
        depth += 1
        current_id = stack.pop()
        if current_id in ancestors:
            continue
        ancestors.add(current_id)
        stack.extend(ancestor_ids_by_retry_job_id.get(current_id, set()) - ancestors)
    return ancestors


def _source_cycle_retry_job_repairs_failure(
    retry_job: Mapping[str, Any],
    failed_job: Mapping[str, Any],
    *,
    cycle_id: str,
    cycle_run_id: str,
) -> bool:
    if str(retry_job.get("status") or "") not in TERMINAL_PIPELINE_SUCCESS_STATUSES:
        return False
    if not _is_source_cycle_download_job(retry_job, cycle_id=cycle_id, cycle_run_id=cycle_run_id):
        return False
    failed_time = _source_cycle_stage_terminal_time(failed_job)
    retry_time = _source_cycle_stage_terminal_time(retry_job)
    if retry_time is not None and failed_time is not None and retry_time < failed_time:
        return False
    failed_attempt = _coerce_int(failed_job.get("retry_count"), default=0)
    retry_attempt = _coerce_int(retry_job.get("retry_count"), default=failed_attempt)
    return retry_attempt >= failed_attempt


def _source_cycle_stage_terminal_time(job: Mapping[str, Any]) -> datetime | None:
    for key in ("finished_at", "submitted_at", "started_at", "created_at"):
        parsed = _parse_gateway_time(job.get(key))
        if parsed is not None:
            return parsed
    return None


def _annotated_source_cycle_repair_jobs(
    jobs: Sequence[Mapping[str, Any]],
    repaired_by_failed_job_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]] | None:
    if not repaired_by_failed_job_id:
        return None
    repaired_failed_ids_by_retry_job_id: dict[str, list[str]] = {}
    for repair in sorted(
        repaired_by_failed_job_id.values(),
        key=lambda item: _source_cycle_original_failure_sort_key(item["failed_job"]),
        reverse=True,
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


def _source_cycle_repaired_stage_evidence(
    failed_job: Mapping[str, Any],
    retry_job: Mapping[str, Any],
    event: Mapping[str, Any],
    manifest_binding: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    cycle_id: str,
) -> dict[str, Any]:
    return {
        "status": "repaired",
        "repair_status": "repaired",
        "stage": str(failed_job.get("stage") or retry_job.get("stage") or "download"),
        "job_type": str(failed_job.get("job_type") or retry_job.get("job_type") or "download_source_cycle"),
        "original_failed_job_id": failed_job.get("job_id"),
        "repairing_retry_job_id": retry_job.get("job_id"),
        "manual_retry_event_id": event.get("event_id"),
        "manual_retry_marker": True,
        "manifest_uri": manifest_binding.get("manifest_uri"),
        "forecast_cycle_status": manifest_binding.get("forecast_cycle_status"),
        "source_id": manifest_binding.get("source_id") or normalize_source_id(source_id),
        "cycle_id": manifest_binding.get("cycle_id") or cycle_id,
        "cycle_time": manifest_binding.get("cycle_time") or _format_time(cycle_time),
    }


def _pipeline_job_is_repaired_stage_evidence(job: Mapping[str, Any]) -> bool:
    return job.get("repair_status") == "repaired" or job.get("active_blocker") is False


def _job_belongs_to_candidate(job: Mapping[str, Any], *, run_id: str, model_id: str) -> bool:
    if str(job.get("run_id") or "") == run_id:
        return True
    return str(job.get("model_id") or "") == model_id


def _first_pipeline_truth_timestamp(job: Mapping[str, Any]) -> Any:
    for key in ("updated_at", "finished_at", "submitted_at", "started_at", "created_at"):
        value = job.get(key)
        if value not in (None, ""):
            return value
    return None


def _pipeline_job_truth_sort_key(job: Mapping[str, Any]) -> tuple[datetime, datetime, int, datetime, str]:
    return (
        _datetime_sort_key(_first_pipeline_truth_timestamp(job)),
        _source_cycle_stage_terminal_time(job) or datetime.min.replace(tzinfo=UTC),
        _coerce_int(job.get("retry_count"), default=0),
        _datetime_sort_key(job.get("created_at")),
        str(job.get("job_id") or ""),
    )


def _source_cycle_original_failure_sort_key(job: Mapping[str, Any]) -> tuple[int, datetime, datetime, datetime]:
    return (
        -_coerce_int(job.get("retry_count"), default=0),
        _inverse_datetime_sort_key(_source_cycle_stage_terminal_time(job) or datetime.min.replace(tzinfo=UTC)),
        _inverse_datetime_sort_key(_datetime_sort_key(job.get("created_at"))),
        _inverse_datetime_sort_key(_pipeline_job_truth_sort_key(job)[0]),
    )


def _inverse_datetime_sort_key(value: datetime) -> datetime:
    return datetime.max.replace(tzinfo=UTC) - (value - datetime.min.replace(tzinfo=UTC))


def _event_truth_sort_key(event: Mapping[str, Any]) -> tuple[datetime, int]:
    return (
        _datetime_sort_key(event.get("created_at")),
        _numeric_sort_key(event.get("event_id")),
    )


def _source_cycle_repair_evidence(source_cycle_download_state: Mapping[str, Any]) -> dict[str, Any] | None:
    status = source_cycle_download_state.get("repair_evidence_status")
    if status in (None, ""):
        return None
    payload = {
        "status": status,
        "truncated": bool(source_cycle_download_state.get("repair_evidence_truncated")),
        "reason": source_cycle_download_state.get("repair_evidence_reason"),
        "unresolved_failed_job_ids": list(
            source_cycle_download_state.get("repair_evidence_unresolved_failed_job_ids") or []
        ),
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [])}


def _datetime_sort_key(value: Any) -> datetime:
    parsed = _parse_gateway_time(value)
    if parsed is None:
        return datetime.min.replace(tzinfo=UTC)
    return parsed


def _numeric_sort_key(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _task_model_id(task: Mapping[str, Any]) -> str | None:
    value = task.get("model_id") or task.get("candidate_model_id")
    return str(value) if value not in (None, "") else None


def _task_candidate_id(task: Mapping[str, Any]) -> str | None:
    value = task.get("candidate_id")
    return str(value) if value not in (None, "") else None


def _task_identity_key(
    task: Mapping[str, Any],
    *,
    model_id: str,
    candidate_id: str | None = None,
) -> tuple[str, str, str] | None:
    task_candidate_id = _task_candidate_id(task)
    task_model_id = _task_model_id(task)
    if task_candidate_id is not None and candidate_id is not None and task_candidate_id != candidate_id:
        return None
    if task_candidate_id is not None and task_model_id is not None and task_model_id != model_id:
        return None
    if task_candidate_id is None and task_model_id != model_id:
        return None
    task_id = task.get("original_task_id", task.get("array_task_id", task.get("task_id")))
    if task_id in (None, ""):
        return None
    return (task_candidate_id or task_model_id or model_id, str(task_id), str(task.get("stage") or ""))


def _event_task_truth_sort_key(
    event: Mapping[str, Any],
    task: Mapping[str, Any],
    *,
    order: int,
    task_order: int,
) -> tuple[datetime, int, int, int]:
    timestamp = _parse_gateway_time(
        task.get("updated_at")
        or task.get("finished_at")
        or task.get("created_at")
        or event.get("created_at")
        or event.get("updated_at")
    ) or datetime.min.replace(tzinfo=UTC)
    return (
        timestamp,
        _numeric_sort_key(event.get("event_id")),
        order,
        task_order,
    )


def _candidate_failed_task_from_events(
    events: Sequence[Mapping[str, Any]],
    *,
    model_id: str,
    candidate_id: str | None = None,
    run_id: str | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any] | None:
    latest_by_identity: dict[
        tuple[str, str, str],
        tuple[tuple[datetime, int, int, int], Mapping[str, Any], Mapping[str, Any]],
    ] = {}
    for order, event in enumerate(events):
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        for task_order, task in enumerate(_bounded_candidate_state_task_results(details)):
            key = _task_identity_key(task, model_id=model_id, candidate_id=candidate_id)
            if key is None:
                continue
            truth_key = _event_task_truth_sort_key(event, task, order=order, task_order=task_order)
            previous = latest_by_identity.get(key)
            if previous is None or truth_key > previous[0]:
                latest_by_identity[key] = (truth_key, event, task)
    latest_failures: list[tuple[tuple[datetime, int, int, int], Mapping[str, Any], Mapping[str, Any]]] = []
    for truth_key, event, task in latest_by_identity.values():
        status = str(task.get("status") or task.get("state") or "")
        if status in {"", "succeeded"}:
            continue
        latest_failures.append((truth_key, event, task))
    if not latest_failures:
        return None
    _truth_key, event, task = max(latest_failures, key=lambda item: item[0])
    details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
    stage = task.get("stage") or details.get("stage")
    return {
        "job": {
            "job_id": event.get("entity_id"),
            "run_id": run_id,
            "cycle_id": cycle_id,
            "model_id": model_id,
            "status": event.get("status_to") or task.get("status") or task.get("state"),
            "stage": stage,
            "job_type": details.get("job_type"),
            "error_code": task.get("error_code") or details.get("error_code") or "NODE_FAILURE",
            "error_message": task.get("error_message") or details.get("error_message"),
            "retry_count": task.get("retry_count") or details.get("retry_count"),
            "created_at": event.get("created_at"),
        },
        "stage": stage,
        "array_task_id": task.get("array_task_id", task.get("task_id")),
        "original_task_id": task.get("original_task_id", task.get("array_task_id", task.get("task_id"))),
        "error_code": task.get("error_code") or details.get("error_code") or "NODE_FAILURE",
        "error_message": task.get("error_message") or details.get("error_message"),
    }


def _successful_sibling_task_count(events: Sequence[Mapping[str, Any]], *, model_id: str) -> int:
    count = 0
    for event in events:
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        for task in _bounded_candidate_state_task_results(details):
            if str(task.get("status") or task.get("state") or "") != "succeeded":
                continue
            task_model_id = _task_model_id(task)
            if task_model_id is None or task_model_id == model_id:
                continue
            count += 1
    return count


def _bounded_candidate_state_event(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    details = payload.get("details")
    if not isinstance(details, Mapping):
        return payload
    details_payload = dict(details)
    task_sample = _bounded_candidate_state_task_result_sample(details_payload)
    if task_sample is not None:
        task_rows, task_metadata = task_sample
        details_payload["task_results"] = task_rows
        details_payload.update(task_metadata)
    payload["details"] = details_payload
    return payload


def _bounded_candidate_state_task_results(details: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    task_sample = _bounded_candidate_state_task_result_sample(details)
    if task_sample is None:
        return []
    return task_sample[0]


def _bounded_candidate_state_task_result_sample(
    details: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], dict[str, Any]] | None:
    task_results = details.get("task_results")
    if not isinstance(task_results, Sequence) or isinstance(task_results, str | bytes | bytearray):
        return None
    task_rows: list[Mapping[str, Any]] = []
    observed_count = 0
    overflow = False
    for index, task in enumerate(task_results):
        observed_count = index + 1
        if index >= MAX_CANDIDATE_STATE_TASK_RESULTS:
            overflow = True
            break
        if isinstance(task, Mapping):
            task_rows.append(dict(task))
    reported_total = _coerce_optional_nonnegative_int(details.get("task_results_total"))
    total = max(reported_total, observed_count) if reported_total is not None else observed_count
    included = len(task_rows)
    overflow = overflow or total > included
    metadata: dict[str, Any] = {
        "task_results_total": total,
        "task_results_included": included,
        "task_results_limit": MAX_CANDIDATE_STATE_TASK_RESULTS,
        "task_results_overflow": overflow,
    }
    if overflow:
        metadata["task_results_omitted"] = max(total - included, 0)
    return task_rows, metadata
