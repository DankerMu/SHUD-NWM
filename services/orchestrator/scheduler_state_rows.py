from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from packages.common.source_identity import normalize_source_id
from services.orchestrator.production_contract import (
    PRODUCTION_EVIDENCE_CORRELATION_FIELDS,
    PRODUCTION_IDENTITY_FIELDS,
)
from services.orchestrator.scheduler_state_common import (
    _coerce_int,
    _coerce_optional_nonnegative_int,
    _evidence_safe,
    _first_nested_state_value,
    _format_utc,
    _looks_like_production_job_id,
    _stage_cycle_run_matches_candidate,
)
from services.orchestrator.scheduler_state_types import (
    ACTIVE_PIPELINE_STATUSES,
    CANDIDATE_STATE_TASK_RESULT_LIMIT,
    DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
    DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
    DOWNSTREAM_RESTART_STAGES,
    FAILED_PIPELINE_STATUSES,
    STATE_CANDIDATE_SCOPED_PROOF_FIELDS,
    STATE_M23_COMPARISON_FIELDS,
)
from workers.data_adapters.base import format_cycle_time


def _bounded_candidate_state(state: Mapping[str, Any]) -> dict[str, Any]:
    bounded = dict(state)
    events = _state_events(bounded)
    if events:
        bounded["pipeline_events"] = [_bounded_candidate_event(event) for event in events]
        bounded.pop("events", None)
    return bounded

def _bounded_candidate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    details = payload.get("details")
    if not isinstance(details, Mapping):
        return payload
    details_payload = dict(details)
    task_sample = _bounded_task_result_sample(details_payload)
    if task_sample is not None:
        task_rows, task_metadata = task_sample
        details_payload["task_results"] = task_rows
        details_payload.update(task_metadata)
    payload["details"] = details_payload
    return payload

def _event_identity_containers(index: int, event: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    containers: list[tuple[str, Mapping[str, Any]]] = [(f"pipeline_events[{index}]", event)]
    details = event.get("details")
    if not isinstance(details, Mapping):
        return containers
    identity = details.get("identity")
    if isinstance(identity, Mapping):
        containers.append((f"pipeline_events[{index}].details.identity", identity))
    containers.append((f"pipeline_events[{index}].details", details))
    for task_index, task in enumerate(_bounded_task_result_rows(details)):
        containers.append((f"pipeline_events[{index}].details.task_results[{task_index}]", task))
        task_identity = task.get("identity")
        if isinstance(task_identity, Mapping):
            containers.append(
                (f"pipeline_events[{index}].details.task_results[{task_index}].identity", task_identity)
            )
    for key in ("task_identity", "failed_task", "failed_task_identity"):
        value = details.get(key)
        if isinstance(value, Mapping):
            containers.append((f"pipeline_events[{index}].details.{key}", value))
    return containers

def _legacy_non_authoritative_state_row(expected: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    return bool(row) and not _state_row_has_authoritative_candidate_proof(expected, row)

def _state_row_has_authoritative_candidate_proof(
    expected: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    include_nested: bool = True,
) -> bool:
    row_values = _legacy_identity_values(row)
    expected_values = _legacy_identity_values(expected)
    if _state_values_have_authoritative_candidate_proof(row_values, expected_values):
        return True
    if not include_nested:
        return False
    for nested in _nested_state_identity_payloads(row):
        nested_values = _legacy_identity_values(nested)
        if _state_values_have_authoritative_candidate_proof(nested_values, expected_values):
            return True
    return False

def _state_values_have_authoritative_candidate_proof(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    if not row_values:
        return False
    if _state_values_are_scoped_to_other_candidate(row_values, expected_values):
        return False
    if _state_values_have_complete_m23_identity(row_values, expected_values):
        return True
    if _state_values_have_candidate_scoped_m23_proof(row_values, expected_values):
        return True
    return _legacy_values_prove_same_candidate(row_values, expected_values)

def _state_row_is_scoped_to_other_candidate(expected: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    expected_values = _legacy_identity_values(expected)
    if _state_values_are_scoped_to_other_candidate(_legacy_identity_values(row), expected_values):
        return True
    return any(
        _state_values_are_scoped_to_other_candidate(_legacy_identity_values(nested), expected_values)
        for nested in _nested_state_identity_payloads(row)
    )

def _state_values_are_scoped_to_other_candidate(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    for identity_field in STATE_CANDIDATE_SCOPED_PROOF_FIELDS:
        value = row_values.get(identity_field)
        expected = expected_values.get(identity_field)
        if identity_field == "run_id" and _stage_cycle_run_matches_candidate(value, expected_values):
            continue
        if identity_field == "run_id" and _shared_stage_cycle_run_matches_candidate(
            value,
            row_values,
            expected_values,
        ):
            continue
        if value not in (None, "") and expected not in (None, "") and value != expected:
            return True
    return False

def _state_values_have_complete_m23_identity(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    for identity_field in PRODUCTION_IDENTITY_FIELDS:
        value = row_values.get(identity_field)
        expected = expected_values.get(identity_field)
        if value in (None, "") or expected in (None, "") or value != expected:
            return False
    return True

def _state_values_have_candidate_scoped_m23_proof(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    return any(
        row_values.get(field) not in (None, "") and row_values.get(field) == expected_values.get(field)
        for field in STATE_CANDIDATE_SCOPED_PROOF_FIELDS
    )

def _legacy_values_prove_same_candidate(
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    if not row_values:
        return False
    for identity_field in ("model_id", "source", "cycle_time"):
        value = row_values.get(identity_field)
        expected_value = expected_values.get(identity_field)
        if value not in (None, "") and expected_value not in (None, "") and value != expected_value:
            return False
    run_id = row_values.get("run_id")
    expected_run_id = expected_values.get("run_id")
    if run_id not in (None, ""):
        if run_id == expected_run_id:
            return True
        if _shared_stage_cycle_run_matches_candidate(run_id, row_values, expected_values):
            return True
        if not _stage_cycle_run_matches_candidate(run_id, expected_values):
            return False
        return True
    source = row_values.get("source")
    cycle_time = row_values.get("cycle_time")
    model_id = row_values.get("model_id")
    if source in (None, "") or cycle_time in (None, ""):
        return False
    if source != expected_values.get("source") or cycle_time != expected_values.get("cycle_time"):
        return False
    return model_id in (None, "", expected_values.get("model_id"))


def _shared_stage_cycle_run_matches_candidate(
    run_id: str | None,
    row_values: Mapping[str, str],
    expected_values: Mapping[str, str],
) -> bool:
    """Accept a cohort run only when the row independently binds the model.

    Multi-basin Slurm arrays intentionally share one source/cycle/stage run id.
    A bare shared run id is not candidate proof; the row must also carry the
    exact candidate model id so sibling-model state cannot bleed across rows.
    """

    if run_id in (None, "") or row_values.get("model_id") != expected_values.get("model_id"):
        return False
    source = str(expected_values.get("source") or "").lower()
    cycle_time = str(expected_values.get("cycle_time") or "")
    if not source or not cycle_time:
        return False
    try:
        compact_cycle = format_cycle_time(cycle_time)
    except (TypeError, ValueError):
        return False
    prefix = f"cycle_{source}_{compact_cycle}_"
    stage = str(run_id).removeprefix(prefix) if str(run_id).startswith(prefix) else ""
    if "_cohort_" in stage:
        stage, separator, member_digest = stage.partition("_cohort_")
        if not separator or re.fullmatch(r"[0-9a-f]{12}", member_digest) is None:
            return False
    return stage == "full" or stage in DOWNSTREAM_RESTART_STAGES

def _state_row_has_m23_comparison_fields(values: Mapping[str, str]) -> bool:
    return any(
        field in values
        for field in (
            *STATE_M23_COMPARISON_FIELDS,
            *PRODUCTION_EVIDENCE_CORRELATION_FIELDS,
        )
    )

def _state_row_has_m23_comparison_evidence(row: Mapping[str, Any]) -> bool:
    if _state_row_has_m23_comparison_fields(_legacy_identity_values(row)):
        return True
    return any(
        _state_row_has_m23_comparison_fields(_legacy_identity_values(nested))
        for nested in _nested_state_identity_payloads(row)
    )

def _nested_state_identity_payloads(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    payloads: list[Mapping[str, Any]] = []
    for key in ("identity", "task_identity", "failed_task", "failed_task_identity"):
        value = row.get(key)
        if isinstance(value, Mapping):
            payloads.append(value)
    details = row.get("details")
    if isinstance(details, Mapping):
        payloads.append(details)
        for key in ("identity", "task_identity", "failed_task", "failed_task_identity"):
            value = details.get(key)
            if isinstance(value, Mapping):
                payloads.append(value)
        for task in _bounded_task_result_rows(details):
            payloads.append(task)
            identity = task.get("identity")
            if isinstance(identity, Mapping):
                payloads.append(identity)
    return payloads

def _bounded_task_result_rows(details: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    task_sample = _bounded_task_result_sample(details)
    if task_sample is None:
        return []
    return task_sample[0]

def _bounded_task_result_sample(
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
        if index >= CANDIDATE_STATE_TASK_RESULT_LIMIT:
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
        "task_results_limit": CANDIDATE_STATE_TASK_RESULT_LIMIT,
        "task_results_overflow": overflow,
    }
    if overflow:
        metadata["task_results_omitted"] = max(total - included, 0)
    return task_rows, metadata

def _legacy_compatible_state_row(expected: Mapping[str, Any], row: Mapping[str, Any]) -> Mapping[str, Any]:
    row_values = _legacy_identity_values(row)
    expected_values = _legacy_identity_values(expected)
    if not _stage_cycle_run_matches_candidate(
        row_values.get("run_id"), expected_values
    ) and not _shared_stage_cycle_run_matches_candidate(
        row_values.get("run_id"),
        row_values,
        expected_values,
    ):
        return row
    payload = dict(row)
    payload.pop("run_id", None)
    identity = payload.get("identity")
    if isinstance(identity, Mapping):
        identity_payload = dict(identity)
        identity_payload.pop("run_id", None)
        payload["identity"] = identity_payload
    return payload

def _legacy_identity_values(payload: Mapping[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    aliases: dict[str, tuple[tuple[str, ...], ...]] = {
        "run_id": (("run_id",), ("identity", "run_id")),
        "model_id": (("model_id",), ("identity", "model_id")),
        "basin_id": (("basin_id",), ("identity", "basin_id")),
        "source": (("source",), ("source_id",), ("identity", "source"), ("identity", "source_id")),
        "cycle_time": (
            ("cycle_time",),
            ("cycle_time_utc",),
            ("identity", "cycle_time"),
            ("identity", "cycle_time_utc"),
        ),
        "basin_version_id": (("basin_version_id",), ("identity", "basin_version_id")),
        "river_network_version_id": (("river_network_version_id",), ("identity", "river_network_version_id")),
        "canonical_product_id": (("canonical_product_id",), ("identity", "canonical_product_id")),
        "forcing_version_id": (("forcing_version_id",), ("identity", "forcing_version_id")),
        "hydro_run_id": (("hydro_run_id",), ("identity", "hydro_run_id")),
        "published_manifest_id": (("published_manifest_id",), ("identity", "published_manifest_id")),
        "pipeline_job_id": (("pipeline_job_id",), ("identity", "pipeline_job_id")),
        "pipeline_event_id": (("pipeline_event_id",), ("identity", "pipeline_event_id")),
    }
    for identity_field, field_aliases in aliases.items():
        value = _first_nested_state_value(payload, field_aliases)
        if value in (None, ""):
            continue
        if identity_field == "source":
            try:
                value = normalize_source_id(str(value))
            except ValueError:
                value = str(value).strip()
        elif identity_field == "cycle_time":
            try:
                value = _format_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
            except ValueError:
                try:
                    value = _format_utc(datetime.strptime(str(value), "%Y%m%d%H").replace(tzinfo=UTC))
                except ValueError:
                    value = str(value).strip()
        else:
            value = str(value).strip()
        if value:
            values[identity_field] = value
    job_id = payload.get("job_id") or payload.get("entity_id")
    if "pipeline_job_id" not in values and job_id not in (None, "") and _looks_like_production_job_id(job_id):
        values["stage_job_id"] = str(job_id).strip()
    event_id = payload.get("event_id")
    if "pipeline_event_id" not in values and event_id not in (None, ""):
        values["stage_event_id"] = str(event_id).strip()
    return values

def _candidate_state_has_identity_mismatch(evidence: Mapping[str, Any]) -> bool:
    validation = evidence.get("production_identity_validation")
    return isinstance(validation, Mapping) and validation.get("status") == "mismatch"

def _state_jobs(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = state.get("pipeline_jobs") or state.get("jobs")
    max_jobs = _state_job_limit(state)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [dict(item) for item in value if isinstance(item, Mapping)][:max_jobs]
    single = state.get("pipeline_job") or state.get("job")
    if isinstance(single, Mapping):
        return [dict(single)]
    fields = {
        "job_id",
        "pipeline_job_id",
        "run_id",
        "cycle_id",
        "job_type",
        "slurm_job_id",
        "array_task_id",
        "model_id",
        "status",
        "pipeline_status",
        "job_status",
        "stage",
        "exit_code",
        "retry_count",
        "error_code",
        "error_message",
        "log_uri",
    }
    if any(key in state for key in fields):
        return [dict(state)]
    return []

def _state_events(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = state.get("pipeline_events") or state.get("events")
    max_events = _state_event_limit(state)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [dict(item) for item in value if isinstance(item, Mapping)][:max_events]
    return []

def _state_job_limit(state: Mapping[str, Any]) -> int:
    return max(_coerce_int(state.get("job_limit"), default=DEFAULT_CANDIDATE_STATE_JOB_LIMIT), 1)

def _state_event_limit(state: Mapping[str, Any]) -> int:
    return max(_coerce_int(state.get("event_limit"), default=DEFAULT_CANDIDATE_STATE_EVENT_LIMIT), 1)

def _state_retry_attempt(state: Mapping[str, Any]) -> int:
    for key in ("retry_attempt", "attempt", "retry_count"):
        value = state.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=0)
    jobs = _state_jobs(state)
    if jobs:
        return max(_coerce_int(job.get("retry_count"), default=0) for job in jobs)
    return 0

def _state_retry_limit(state: Mapping[str, Any]) -> int | None:
    for key in ("retry_limit", "max_retries"):
        value = state.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=0)
    from services.orchestrator.scheduler_state_types import DEFAULT_RETRY_LIMIT

    return DEFAULT_RETRY_LIMIT

def _state_overflow_evidence(state: Mapping[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "job_limit": _state_job_limit(state),
        "event_limit": _state_event_limit(state),
    }
    overflow = False
    for count_key, limit_key, output_key in (
        ("pipeline_jobs_total", "job_limit", "pipeline_jobs"),
        ("pipeline_events_total", "event_limit", "pipeline_events"),
    ):
        count = state.get(count_key)
        if count in (None, ""):
            continue
        count_value = _coerce_int(count, default=0)
        limit_value = int(evidence[limit_key])
        evidence[f"{output_key}_total"] = count_value
        evidence[f"{output_key}_returned"] = min(count_value, limit_value)
        if count_value > limit_value:
            evidence[f"{output_key}_overflow"] = True
            overflow = True
    if state.get("state_truncated") is True:
        overflow = True
        evidence["state_truncated"] = True
    if not overflow:
        return {}
    evidence["bounded"] = True
    evidence["overflow"] = True
    evidence["reason"] = "candidate_state_row_limit_applied"
    return evidence

def _job_state_evidence(job: Mapping[str, Any]) -> dict[str, Any]:
    kept = {
        key: job.get(key)
        for key in (
            "job_id",
            "pipeline_job_id",
            "pipeline_event_id",
            "run_id",
            "cycle_id",
            "job_type",
            "slurm_job_id",
            "array_task_id",
            "model_id",
            "basin_id",
            "source",
            "source_id",
            "cycle_time",
            "basin_version_id",
            "river_network_version_id",
            "canonical_product_id",
            "forcing_version_id",
            "hydro_run_id",
            "published_manifest_id",
            "status",
            "stage",
            "exit_code",
            "retry_count",
            "error_code",
            "error_message",
            "log_uri",
            "repair_status",
            "superseded_by_job_id",
            "repaired_by_job_id",
            "repairs_job_id",
            "repairs_job_ids",
            "active_blocker",
        )
        if key in job and job.get(key) is not None
    }
    return _evidence_safe(kept)

def _optional_mapping_state(value: Any, *, defaults: Mapping[str, Any]) -> dict[str, Any] | None:
    payload = dict(value) if isinstance(value, Mapping) else {}
    for key, fallback in defaults.items():
        if fallback not in (None, ""):
            payload.setdefault(key, fallback)
    payload = {key: val for key, val in payload.items() if val not in (None, "")}
    return _evidence_safe(payload) if payload else None

def _state_status(state: Mapping[str, Any], *keys: str) -> str | None:
    explicit_key_seen = False
    for key in keys:
        explicit_key_seen = explicit_key_seen or key in state
        value = state.get(key)
        if value not in (None, ""):
            return str(value)
    if explicit_key_seen:
        return None
    for job in reversed(_state_jobs(state)):
        for key in keys:
            value = job.get(key)
            if value not in (None, ""):
                return str(value)
    return None

def _state_output_uri(state: Mapping[str, Any]) -> str | None:
    for container_key in ("hydro_run", "outputs", "runtime_outputs"):
        value = state.get(container_key)
        if isinstance(value, Mapping) and value.get("output_uri") not in (None, ""):
            return str(value["output_uri"])
    value = state.get("output_uri") or state.get("durable_output_uri")
    return str(value) if value not in (None, "") else None

def _state_active_jobs(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    explicit = state.get("active_slurm_jobs")
    if isinstance(explicit, Sequence) and not isinstance(explicit, str | bytes | bytearray):
        return [
            _evidence_safe(dict(job))
            for job in explicit
            if isinstance(job, Mapping) and _job_has_real_slurm_binding(job)
        ]
    active: list[dict[str, Any]] = []
    for job in _state_jobs(state):
        status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        if _job_has_real_slurm_binding(job) and status in ACTIVE_PIPELINE_STATUSES:
            active.append(_job_state_evidence(job))
    return active

def _job_has_real_slurm_binding(job: Mapping[str, Any]) -> bool:
    slurm_job_id = str(job.get("slurm_job_id") or "")
    return bool(slurm_job_id and slurm_job_id.lower() != "local") or job.get("array_task_id") not in (None, "")

def _job_is_unsubmitted_auto_retry_placeholder(job: Mapping[str, Any]) -> bool:
    status = str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
    if status not in {"pending", "submission_failed"}:
        return False
    if job.get("manual_retry_marker") is True:
        return False
    if job.get("slurm_job_id") not in (None, "") or job.get("array_task_id") not in (None, ""):
        return False
    retry_count = _coerce_int(job.get("retry_count"), default=0)
    if retry_count <= 0:
        return False
    job_id = str(job.get("job_id") or "")
    return "_retry_" in job_id

def _state_has_only_unsubmitted_auto_retry_placeholders(state: Mapping[str, Any]) -> bool:
    jobs = _state_jobs(state)
    active_jobs = [
        job
        for job in jobs
        if str(job.get("status") or job.get("pipeline_status") or job.get("job_status") or "")
        in ACTIVE_PIPELINE_STATUSES
    ]
    return bool(active_jobs) and all(_job_is_unsubmitted_auto_retry_placeholder(job) for job in active_jobs)

def _pipeline_job_is_repaired_stage_evidence(job: Mapping[str, Any]) -> bool:
    return job.get("repair_status") == "repaired" or job.get("active_blocker") is False

def _event_has_failure_signal(event: Mapping[str, Any]) -> bool:
    details = event.get("details")
    details_mapping = details if isinstance(details, Mapping) else {}
    status = str(
        event.get("status_to")
        or details_mapping.get("status_to")
        or details_mapping.get("status")
        or details_mapping.get("state")
        or ""
    )
    return status in FAILED_PIPELINE_STATUSES or details_mapping.get("error_code") not in (None, "")

def _is_source_cycle_download_stage(stage: str | None) -> bool:
    return stage in {"download", "download_source_cycle", "download_gfs"}

def _state_task_payload_failed(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("status") or payload.get("state") or "")
    return status not in {"", "succeeded", "complete", "published"}
