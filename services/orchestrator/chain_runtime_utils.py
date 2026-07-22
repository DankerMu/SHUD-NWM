from __future__ import annotations

import json
import os
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from packages.common.redaction import redact_payload
from packages.common.source_identity import normalize_source_id
from services.orchestrator.chain_stages import STAGES
from services.orchestrator.chain_types import (
    CycleOrchestrationContext,
    ForcingContext,
    InitialStateSelection,
    OrchestratorError,
    PipelineResult,
    StageDefinition,
    StageRunResult,
)
from workers.canonical_converter.converter import expected_converter_version
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

TERMINAL_JOB_STATUSES = {
    "succeeded",
    "partially_failed",
    "failed",
    "cancelled",
    "submission_failed",
    "reservation_lost",
    "permanently_failed",
}
TERMINAL_PIPELINE_SUCCESS_STATUSES = {"succeeded", "complete", "published"}
CANONICAL_PRECIP_VARIABLE = "prcp_rate_or_amount"
CANONICAL_PRECIP_UNIT = "mm/day"


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_optional_nonnegative_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _cycle_payload_model_id(context: CycleOrchestrationContext) -> str:
    if context.active_basins:
        return str(context.active_basins[0].get("model_id") or "cycle")
    return "cycle"


def _cycle_pipeline_job_model_id(context: CycleOrchestrationContext) -> str | None:
    if len(context.all_basins) != 1:
        return None
    return str(context.all_basins[0].get("model_id") or "") or None


def _cycle_orchestration_run_id(
    source_id: str,
    cycle_time: datetime,
    basins: Sequence[Mapping[str, Any]],
) -> str:
    run_ids = {
        str(basin.get("orchestration_run_id"))
        for basin in basins
        if basin.get("orchestration_run_id") not in (None, "")
    }
    if len(run_ids) == 1:
        return run_ids.pop()
    return f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"


def _active_orchestration_conflicts(
    repository: Any,
    *,
    source_id: str,
    cycle_time: datetime,
    cycle_id: str,
    run_id: str,
    basins: Sequence[Mapping[str, Any]],
) -> bool:
    model_ids = _cycle_basin_model_ids(basins)
    active_pipeline_provider = getattr(repository, "has_active_pipeline", None)
    if _candidate_scoped_cycle_execution(basins):
        replacement_retry = _replacement_retry_scoped_cycle_execution(basins)
        for job in repository.query_pipeline_jobs_by_run(run_id):
            if _is_active_pipeline_job(job):
                if replacement_retry and _is_unsubmitted_retry_placeholder(job):
                    continue
                return True
        if replacement_retry:
            return False
        if model_ids and callable(active_pipeline_provider):
            return any(
                bool(active_pipeline_provider(source_id=source_id, cycle_time=cycle_time, model_id=model_id))
                for model_id in model_ids
            )
        return False
    if model_ids and callable(active_pipeline_provider):
        return any(
            bool(active_pipeline_provider(source_id=source_id, cycle_time=cycle_time, model_id=model_id))
            for model_id in model_ids
        )
    if repository.has_active_orchestration(source_id=source_id, cycle_time=cycle_time):
        return True
    return any(_is_active_pipeline_job(job) for job in repository.query_pipeline_jobs_by_cycle(cycle_id))


def _in_memory_active_cycle_conflicts(
    cycle_id: str,
    active_cycles: set[str],
    basins: Sequence[Mapping[str, Any]],
) -> bool:
    return cycle_id in active_cycles and not _candidate_scoped_cycle_execution(basins)


def _candidate_scoped_cycle_execution(basins: Sequence[Mapping[str, Any]]) -> bool:
    if not basins:
        return False
    orchestration_run_ids = {
        str(basin.get("orchestration_run_id"))
        for basin in basins
        if basin.get("orchestration_run_id") not in (None, "")
    }
    if len(orchestration_run_ids) == 1 and all(
        basin.get("orchestration_run_id") not in (None, "") for basin in basins
    ):
        return True
    if len(basins) != 1:
        return False
    basin = basins[0]
    return (
        _restart_stage_from_basins(basins) is not None
        or basin.get("orchestration_run_id") not in (None, "")
        or _manual_retry_scoped_cycle_execution(basins)
    )


def _manual_retry_scoped_cycle_execution(basins: Sequence[Mapping[str, Any]]) -> bool:
    if len(basins) != 1:
        return False
    basin = basins[0]
    if basin.get("manual_retry_attempt") not in (None, ""):
        return True
    state_evidence = basin.get("state_evidence")
    if not isinstance(state_evidence, Mapping):
        return False
    manual_retry = state_evidence.get("manual_retry")
    return bool(
        isinstance(manual_retry, Mapping)
        and (manual_retry.get("marker") or manual_retry.get("requested") or manual_retry.get("allowed"))
    )


def _replacement_retry_scoped_cycle_execution(basins: Sequence[Mapping[str, Any]]) -> bool:
    if _manual_retry_scoped_cycle_execution(basins):
        return True
    force_replacement_decisions = {
        "retry_missing_forecast_output",
        "retry_strict_warm_start_terminal_init_state_mismatch",
        "retry_strict_warm_start_terminal_run_manifest_missing",
        "retry_strict_warm_start_retry_run_manifest_mismatch",
        "retry_terminal_run_manifest_missing",
    }
    if basins and all(
        isinstance(basin.get("state_evidence"), Mapping)
        and basin["state_evidence"].get("decision") in force_replacement_decisions
        and _canonical_restart_stage(
            basin["state_evidence"].get("restart_stage")
            or basin["state_evidence"].get("restart_from_stage")
        )
        is not None
        for basin in basins
    ):
        # A strict-warm repair is intentionally a replacement submission.  A
        # stopped pass may have left candidate hydro rows in ``created`` with
        # no Slurm binding; those placeholders must not block a multi-basin
        # replacement cohort.  The run-scoped active-job check above still
        # rejects a real in-flight submission for this exact cohort.
        return True
    completed_stage_restarts = []
    for basin in basins:
        state_evidence = basin.get("state_evidence")
        if not isinstance(state_evidence, Mapping) or state_evidence.get("decision") != "retry_after_completed_stage":
            break
        completed_stage = state_evidence.get("completed_stage_evidence")
        restart_stage = _canonical_restart_stage(
            state_evidence.get("restart_stage")
            or state_evidence.get("restart_from_stage")
            or (completed_stage.get("restart_stage") if isinstance(completed_stage, Mapping) else None)
            or (completed_stage.get("restart_from_stage") if isinstance(completed_stage, Mapping) else None)
        )
        completed_stage_restarts.append(
            isinstance(completed_stage, Mapping)
            and str(completed_stage.get("status") or "") in TERMINAL_PIPELINE_SUCCESS_STATUSES
            and restart_stage is not None
        )
    if basins and len(completed_stage_restarts) == len(basins) and all(completed_stage_restarts):
        return True
    if len(basins) != 1:
        return False
    state_evidence = basins[0].get("state_evidence")
    if not isinstance(state_evidence, Mapping):
        return False
    if state_evidence.get("decision") == "retry_after_model_package_refresh":
        return True
    retry_policy = state_evidence.get("retry_policy")
    return bool(
        isinstance(retry_policy, Mapping)
        and retry_policy.get("override_reason") == "model_package_refresh"
        and retry_policy.get("automatic_retry_allowed") is True
    )


def _cycle_basin_model_ids(basins: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    model_ids: list[str] = []
    seen: set[str] = set()
    for basin in basins:
        model_id = str(basin.get("model_id") or "")
        if not model_id or model_id in seen:
            continue
        model_ids.append(model_id)
        seen.add(model_id)
    return tuple(model_ids)


def _is_active_pipeline_job(job: Mapping[str, Any]) -> bool:
    if _is_unsubmitted_retry_placeholder(job):
        return False
    if _compute_terminal_legacy_downstream_job(job):
        return False
    return str(job.get("status") or "") not in TERMINAL_JOB_STATUSES


def _compute_terminal_legacy_downstream_job(job: Mapping[str, Any]) -> bool:
    if os.getenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", "").strip() != "forecast_state_save_qc":
        return False
    candidates = [job.get("stage"), job.get("job_type")]
    aliases = {
        "parse_output": "parse",
        "parse_output_array": "parse",
        "publish_tiles": "publish",
    }
    for raw in candidates:
        if raw in (None, ""):
            continue
        stage = aliases.get(str(raw), str(raw))
        if stage in {"parse", "publish"}:
            return True
    return False


def _is_unsubmitted_retry_placeholder(job: Mapping[str, Any]) -> bool:
    status = str(job.get("status") or "")
    if status not in {"pending", "queued", "submitted"}:
        return False
    if job.get("slurm_job_id") not in (None, ""):
        return False
    if job.get("array_task_id") not in (None, ""):
        return False
    if job.get("submitted_at") not in (None, ""):
        return False
    try:
        retry_count = int(job.get("retry_count") or 0)
    except (TypeError, ValueError):
        retry_count = 0
    if retry_count <= 0:
        return False
    return job.get("candidate_id") in (None, "") and job.get("idempotency_key") in (None, "")


def _restart_stage_from_basins(basins: Sequence[Mapping[str, Any]]) -> str | None:
    restart_stages: list[str] = []
    for basin in basins:
        restart_stage = _canonical_restart_stage(basin.get("restart_stage"))
        if restart_stage is not None:
            restart_stages.append(restart_stage)
            continue
        state_evidence = basin.get("state_evidence")
        if isinstance(state_evidence, Mapping):
            restart_stage = _canonical_restart_stage(
                state_evidence.get("restart_stage") or state_evidence.get("restart_from_stage")
            )
            if restart_stage is not None:
                restart_stages.append(restart_stage)
    if not restart_stages:
        return None
    stage_order = {stage.stage: index for index, stage in enumerate(STAGES)}
    return min(restart_stages, key=lambda stage: stage_order.get(stage, len(stage_order)))


def _retry_attempt_from_basins(basins: Sequence[Mapping[str, Any]]) -> int | None:
    attempts: list[int] = []
    for basin in basins:
        for value in (
            basin.get("retry_attempt"),
            basin.get("manual_retry_attempt"),
        ):
            attempt = _coerce_positive_int(value)
            if attempt is not None:
                attempts.append(attempt)
        state_evidence = basin.get("state_evidence")
        if isinstance(state_evidence, Mapping):
            manual_retry = state_evidence.get("manual_retry")
            if isinstance(manual_retry, Mapping):
                attempt = _coerce_positive_int(
                    manual_retry.get("new_attempt") or manual_retry.get("attempt") or manual_retry.get("retry_count")
                )
                if attempt is not None:
                    attempts.append(attempt)
    if not attempts:
        return None
    return max(attempts)


def _coerce_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _stage_result_finished_at(result: StageRunResult) -> datetime | None:
    if result.finished_at is not None:
        return result.finished_at
    for key in ("finished_at", "updated_at"):
        value = result.accounting.get(key)
        parsed = _parse_gateway_time(value)
        if parsed is not None:
            return parsed
    return datetime.now(UTC)


def _pipeline_job_terminal_time(job: Mapping[str, Any]) -> datetime | None:
    for key in ("finished_at", "updated_at", "submitted_at", "started_at", "created_at"):
        parsed = _parse_gateway_time(job.get(key))
        if parsed is not None:
            return parsed
    return None


def _canonical_restart_stage(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value)
    aliases = {
        "parse_output": "parse",
        "publish_tiles": "publish",
    }
    normalized = aliases.get(normalized, normalized)
    allowed = {stage.stage for stage in STAGES}
    return normalized if normalized in allowed else None


def _restart_stage_index(restart_stage: str | None, stages: Sequence[StageDefinition]) -> int:
    if restart_stage is None:
        return 0
    for index, stage in enumerate(stages):
        if stage.stage == restart_stage:
            return index
    return 0


def _pipeline_job_id(run_id: str, stage: str) -> str:
    return f"job_{run_id}_{stage}"


def _pipeline_retry_job_id(base_job_id: str, attempt: int) -> str:
    return f"{base_job_id}_retry_{max(int(attempt), 1)}"


def _stage_job_sort_key(job: Mapping[str, Any], stage: StageDefinition) -> tuple[int, int, datetime, datetime]:
    job_id = str(job.get("job_id") or "")
    base_job_id = str(job_id).removesuffix("_retry_0")
    persisted_attempt = _coerce_int(job.get("retry_count"), default=-1)
    attempt = persisted_attempt if persisted_attempt >= 0 else 0
    marker = "_retry_"
    if persisted_attempt < 0 and marker in job_id:
        try:
            attempt = int(job_id.rsplit(marker, maxsplit=1)[1])
        except ValueError:
            attempt = 0
        base_job_id = job_id.rsplit(marker, maxsplit=1)[0]
    elif marker in job_id:
        base_job_id = job_id.rsplit(marker, maxsplit=1)[0]
    expected_suffix = f"_{stage.stage}"
    stage_match = (
        1
        if job.get("stage") == stage.stage
        or job.get("job_type") == stage.job_type
        or base_job_id.endswith(expected_suffix)
        else 0
    )
    terminal_time = _pipeline_job_terminal_time(job) or datetime.min.replace(tzinfo=UTC)
    created_time = _parse_gateway_time(job.get("created_at")) or datetime.min.replace(tzinfo=UTC)
    return stage_match, attempt, terminal_time, created_time


def _cycle_stage_idempotency_key(
    context: CycleOrchestrationContext,
    stage: StageDefinition,
    *,
    pipeline_job_id: str | None = None,
) -> str:
    """Stable idempotency key for a cohort's cycle-level stage submission.

    ``run_id`` deterministically encodes source/cycle/basin-cohort, so
    ``run_id:stage`` is the equivalent of the per-candidate
    ``source:cycle:basin:stage`` key and is constant across passes.
    """

    base_job_id = _pipeline_job_id(context.run_id, stage.stage)
    if pipeline_job_id and pipeline_job_id != base_job_id:
        suffix = str(pipeline_job_id).removeprefix(f"{base_job_id}_")
        if suffix:
            return f"{context.run_id}:{stage.stage}:{suffix}"
    return f"{context.run_id}:{stage.stage}"


def _published_artifact_root_configured() -> bool:
    return bool(os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT", "").strip())


def _absolute_configured_path(path: Path) -> Path:
    expanded = Path(path).expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _log_stream_for_stage(stage: str) -> str:
    return "err" if stage in {"submission_failed", "error"} else "out"


def _source_id_from_cycle_id(value: object) -> str | None:
    if value in (None, ""):
        return None
    source = str(value).split("_", maxsplit=1)[0]
    try:
        return normalize_source_id(source)
    except ValueError:
        return source or None


def _cycle_time_from_cycle_id(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    compact_cycle = str(value).rsplit("_", maxsplit=1)[-1]
    try:
        return parse_cycle_time(compact_cycle)
    except ValueError:
        return None


def _stage_status_message(stage: str, status: str, job: dict[str, Any]) -> str:
    if status == "failed":
        error_code = job.get("error_code") or "UNKNOWN"
        error_message = redact_payload(job.get("error_message") or "No error message provided.")
        return f"{stage} failed: {error_code} {error_message}"
    return f"{stage} status changed to {status}"


def _resolve_forecast_horizon_hours(
    *,
    source_id: str,
    cycle_time: datetime,
    configured_horizon_hours: int,
    forcing: ForcingContext,
    max_lead_hours: int | None,
) -> int:
    source_max_lead_hours = max_lead_hours or forcing.max_lead_hours
    normalized_source_id = normalize_source_id(source_id)
    if source_max_lead_hours is None and normalized_source_id == "IFS" and forcing.end_time is not None:
        source_max_lead_hours = _elapsed_hours(cycle_time, forcing.end_time)
    if source_max_lead_hours is None and normalized_source_id == "IFS":
        source_max_lead_hours = _ifs_max_lead_hours_for_cycle(cycle_time)
    if source_max_lead_hours is None:
        return int(configured_horizon_hours)
    return min(int(configured_horizon_hours), int(source_max_lead_hours))


def _ifs_max_lead_hours_for_cycle(cycle_time: datetime) -> int | None:
    hour = _ensure_utc(cycle_time).hour
    if hour in {6, 18}:
        return 144
    if hour in {0, 12}:
        return 168
    return None


def _elapsed_hours(start_time: datetime, end_time: datetime) -> int | None:
    elapsed_seconds = (_ensure_utc(end_time) - _ensure_utc(start_time)).total_seconds()
    if elapsed_seconds <= 0:
        return None
    return int(round(elapsed_seconds / 3600.0))


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _first_optional_int(*values: Any) -> int | None:
    for value in values:
        coerced = _optional_int(value)
        if coerced is not None:
            return coerced
    return None


def _max_lead_hours_from_lineage(value: Any) -> int | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, Mapping):
        return None
    return _optional_int(value.get("max_lead_hours"))


def _basin_max_lead_hours(basin: Mapping[str, Any]) -> int | None:
    """Configured ``max_lead`` policy (hours) for a cohort basin's warm-start chaining."""
    for key in ("max_lead_hours", "warm_start_max_lead_hours"):
        value = _optional_int(basin.get(key))
        if value is not None:
            return value
    horizon = basin.get("horizon")
    if isinstance(horizon, Mapping):
        return _optional_int(horizon.get("max_lead_hours"))
    return None


def _basin_has_prefilled_initial_state(basin: Mapping[str, Any]) -> bool:
    return any(
        basin.get(key) not in (None, "")
        for key in (
            "init_state_id",
            "init_state_uri",
            "init_state_checksum",
            "init_state_valid_time",
            "init_state_lineage",
        )
    )


def _apply_initial_state_selection_to_basin(basin: dict[str, Any], selection: InitialStateSelection) -> None:
    basin["init_state_id"] = selection.state_id
    basin["init_state_uri"] = selection.state_uri
    basin["init_state_checksum"] = selection.checksum
    basin["init_state_valid_time"] = _format_time(selection.valid_time) if selection.valid_time is not None else None
    basin["init_state_quality"] = selection.quality
    basin["init_state_lineage"] = _initial_state_lineage(selection)


def _initial_state_lineage(selection: InitialStateSelection) -> dict[str, Any]:
    lineage = {
        "source_id": selection.source_id,
        "cycle_id": selection.cycle_id,
        "lead_hours": selection.lead_hours,
        "model_package_version": selection.model_package_version,
        "model_package_checksum": selection.model_package_checksum,
    }
    return lineage if any(value is not None for value in lineage.values()) else {}


def _auto_trigger_forecast_hours(
    *,
    source_id: str,
    cycle_time: datetime,
    configured_horizon_hours: int,
    max_lead_hours: int | None,
) -> list[int]:
    source_max_lead_hours = max_lead_hours
    normalized_source_id = normalize_source_id(source_id)
    if source_max_lead_hours is None and normalized_source_id == "IFS":
        source_max_lead_hours = _ifs_max_lead_hours_for_cycle(cycle_time)
    if source_max_lead_hours is None:
        source_max_lead_hours = int(configured_horizon_hours)
    horizon = min(int(configured_horizon_hours), int(source_max_lead_hours))
    return list(range(0, horizon + 1, 3))


def _auto_trigger_source_policy_identity(
    *,
    source_id: str,
    cycle_time: datetime,
    forecast_hours: Sequence[int],
    workspace_root: Path | str,
    object_store_root: Path | str,
    object_store_prefix: str,
) -> dict[str, Any]:
    adapter = _auto_trigger_source_identity_adapter(
        source_id=source_id,
        workspace_root=workspace_root,
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
    )
    if adapter is not None and hasattr(adapter, "source_policy_identity"):
        try:
            return dict(adapter.source_policy_identity(cycle_time, list(forecast_hours)))
        except TypeError:
            return dict(adapter.source_policy_identity(list(forecast_hours)))
    return {
        "source": source_id,
        "cycle_hour": _ensure_utc(cycle_time).hour,
        "forecast_hours": list(forecast_hours),
    }


def _auto_trigger_source_object_identity(
    *,
    source_id: str,
    cycle_time: datetime,
    forecast_hours: Sequence[int],
    workspace_root: Path | str,
    object_store_root: Path | str,
    object_store_prefix: str,
) -> dict[str, Any]:
    adapter = _auto_trigger_source_identity_adapter(
        source_id=source_id,
        workspace_root=workspace_root,
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
    )
    if adapter is not None and hasattr(adapter, "source_object_identity"):
        return dict(adapter.source_object_identity(cycle_time, list(forecast_hours)))
    return {
        "source": source_id,
        "cycle_time": _format_time(cycle_time),
        "cycle_id": cycle_id_for(source_id, cycle_time),
        "forecast_hour_count": len(forecast_hours),
    }


def _auto_trigger_source_identity_adapter(
    *,
    source_id: str,
    workspace_root: Path | str,
    object_store_root: Path | str,
    object_store_prefix: str,
) -> Any | None:
    normalized_source_id = normalize_source_id(source_id)
    if normalized_source_id == "gfs":
        from workers.data_adapters.gfs_adapter import GFSAdapter, GFSAdapterConfig

        return GFSAdapter(
            config=GFSAdapterConfig(
                workspace_root=workspace_root,
                object_store_root=object_store_root,
                object_store_prefix=object_store_prefix,
            ),
            repository=None,
        )
    if normalized_source_id == "IFS":
        from workers.data_adapters.ifs_adapter import IFSAdapter, IFSAdapterConfig

        return IFSAdapter(
            config=IFSAdapterConfig(
                workspace_root=workspace_root,
                object_store_root=object_store_root,
                object_store_prefix=object_store_prefix,
            ),
            repository=None,
        )
    return None


def _stale_converter_versions_in_cycle(
    cycle: Mapping[str, Any],
    *,
    source_id: str,
) -> set[str | None]:
    """Return stale canonical markers in ``cycle`` (version mismatch or bad unit).

    Two orthogonal stale criteria are applied:

    1. Version: each canonical product carries ``converter_version`` inside
       ``lineage_json`` (with a top-level fallback). A value that is explicitly
       recorded and differs from the source's current expected version is stale.
       A missing version is NOT flagged (mirrors fixture/seed safety).

    2. Unit (precip): a precipitation product (``prcp_rate_or_amount``) whose
       ``unit`` is explicitly recorded and is not the post-#269 canonical
       ``mm/day`` is stale, marked as ``"unit:<observed>"``. A missing/empty
       unit is NOT flagged (same fixture/seed safety philosophy).

    WHY the unit criterion: pre-#269 precip rows were written with ``unit="mm"``
    and frequently lack a converter_version, so they slip past criterion 1.
    Without this orthogonal check they would pass readiness and then hit the
    producer's mm/day unit gate, terminating in ``failed_forcing`` with no
    self-heal path (a "break userspace" regression). Flagging the unit triggers
    the same demote -> re-conversion loop, restoring migration self-heal.

    Returns an empty set when every product is current (or none exist).
    """
    products = cycle.get("canonical_products")
    if isinstance(products, str):
        try:
            products = json.loads(products)
        except json.JSONDecodeError:
            products = []
    if not isinstance(products, Sequence) or isinstance(products, (bytes, bytearray, str)):
        return set()
    expected = expected_converter_version(source_id)
    stale: set[str | None] = set()
    for row in products:
        if not isinstance(row, Mapping):
            continue
        lineage = row.get("lineage_json")
        if isinstance(lineage, str):
            try:
                lineage = json.loads(lineage)
            except json.JSONDecodeError:
                lineage = {}
        version: str | None = None
        if isinstance(lineage, Mapping):
            raw = lineage.get("converter_version", row.get("converter_version"))
            version = str(raw) if raw is not None else None
        else:
            raw = row.get("converter_version")
            version = str(raw) if raw is not None else None
        # Criterion 1 (version): only an explicitly-recorded, different version is
        # treated as stale. A missing version (None) is left untouched so that
        # incomplete fixtures/seeds and post-#269 rows lacking a version are not
        # aggressively demoted.
        if version is not None and version != expected:
            stale.add(version)
        # Criterion 2 (precip unit): a precip product whose unit is explicitly
        # recorded and is not the canonical mm/day is stale. Missing/empty unit is
        # left untouched (same fixture/seed safety as the version criterion).
        if row.get("variable") == CANONICAL_PRECIP_VARIABLE:
            raw_unit = row.get("unit")
            if raw_unit is not None:
                normalized_unit = str(raw_unit).strip().lower()
                if normalized_unit and normalized_unit != CANONICAL_PRECIP_UNIT:
                    stale.add(f"unit:{normalized_unit}")
    return stale


def _canonical_products_from_ready_cycle(
    cycle: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
) -> list[dict[str, Any]]:
    products = cycle.get("canonical_products")
    if isinstance(products, str):
        try:
            products = json.loads(products)
        except json.JSONDecodeError:
            products = []
    if not isinstance(products, Sequence) or isinstance(products, (bytes, bytearray, str)):
        products = []
    return [
        _canonical_product_row_from_ready_cycle(row, source_id=source_id, cycle_time=cycle_time) for row in products
    ]


def _canonical_product_row_from_ready_cycle(
    row: Any,
    *,
    source_id: str,
    cycle_time: datetime,
) -> dict[str, Any]:
    product = dict(row) if isinstance(row, Mapping) else {}
    product.setdefault("source_id", source_id)
    product.setdefault("cycle_time", cycle_time)
    lineage = product.get("lineage_json")
    if isinstance(lineage, str):
        try:
            product["lineage_json"] = json.loads(lineage)
        except json.JSONDecodeError:
            product["lineage_json"] = {}
    elif not isinstance(lineage, Mapping):
        product["lineage_json"] = {}
    return product


def _auto_trigger_canonical_readiness_unavailable_evidence(
    *,
    source_id: str,
    cycle_time: datetime,
    forecast_hours: Sequence[int],
    reason: str,
    error: Exception,
) -> dict[str, Any]:
    return {
        "source": source_id,
        "source_id": source_id,
        "cycle_time": _format_time(cycle_time),
        "status": "canonical_incomplete",
        "ready": False,
        "reason": reason,
        "canonical_product_id": f"canon_{source_id.lower()}_{format_cycle_time(cycle_time)}",
        "accepted_horizon": _accepted_horizon_from_hours(forecast_hours),
        "expected_leads": list(forecast_hours),
        "policy_identity_matched": False,
        "source_object_identity_matched": False,
        "dependency": {
            "name": "canonical_readiness_provider",
            "status": "unavailable",
            "retryable": True,
        },
        "failure": {
            "error_type": type(error).__name__,
            "message": str(redact_payload(str(error))),
        },
    }


def _accepted_horizon_from_hours(forecast_hours: Sequence[int]) -> dict[str, Any]:
    hours = sorted(int(hour) for hour in forecast_hours)
    return {
        "first_lead_hour": min(hours) if hours else None,
        "last_lead_hour": max(hours) if hours else None,
        "lead_count": len(hours),
    }


def _skipped_ready_forecast_result(
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    reason: str,
    canonical_readiness: Mapping[str, Any],
) -> PipelineResult:
    cycle_id = cycle_id_for(source_id, cycle_time)
    run_id = f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
    return PipelineResult(
        run_id=run_id,
        cycle_id=cycle_id,
        status="skipped",
        stages=(),
        candidate_outcomes=(
            {
                "candidate_id": run_id,
                "run_id": run_id,
                "cycle_id": cycle_id,
                "source_id": source_id,
                "cycle_time": _format_time(cycle_time),
                "model_id": model_id,
                "status": "skipped",
                "reason": reason,
                "state_evidence": {"canonical_readiness": dict(canonical_readiness)},
            },
        ),
    )


def _coerce_array_task_id(value: Any) -> int | None:
    """Best-effort int coercion for a gateway-reported array task id.

    ``ops.pipeline_job.array_task_id`` is an integer column; a master array job
    has no single task id and yields ``None``. Non-integer junk is dropped rather
    than raised so receipt persistence never breaks on an odd gateway payload.
    """

    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_gateway_time(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return _ensure_utc(value) if isinstance(value, datetime) else None
    if isinstance(value, str):
        return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    return None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _format_time_or_none(value: datetime | None) -> str | None:
    return _format_time(value) if value is not None else None


def parse_date_range(value: str | tuple[datetime, datetime]) -> tuple[datetime, datetime]:
    if isinstance(value, tuple):
        start_time, end_time = value
        return _validated_date_range(_ensure_utc(start_time), _ensure_utc(end_time))

    candidate = value.strip()
    separators = ("..", "/", ",")
    for separator in separators:
        if separator in candidate:
            left, right = candidate.split(separator, maxsplit=1)
            return _validated_date_range(_parse_date_range_endpoint(left), _parse_date_range_endpoint(right))
    raise OrchestratorError(
        "INVALID_DATE_RANGE",
        "date_range must use START/END, START..END, or START,END.",
        {"date_range": value},
    )


def _parse_date_range_endpoint(value: str) -> datetime:
    candidate = value.strip()
    if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-":
        return datetime.fromisoformat(candidate).replace(tzinfo=UTC)
    return parse_cycle_time(candidate)


def _validated_date_range(start_time: datetime, end_time: datetime) -> tuple[datetime, datetime]:
    start = _ensure_utc(start_time)
    end = _ensure_utc(end_time)
    if end <= start:
        raise OrchestratorError(
            "INVALID_DATE_RANGE",
            "date_range end must be after start.",
            {"start_time": _format_time(start), "end_time": _format_time(end)},
        )
    return start, end


def _analysis_error_code(stage: StageDefinition, terminal: dict[str, Any]) -> str:
    raw_code = terminal.get("error_code")
    timeout_codes = {"TIMEOUT", "SLURM_TIMEOUT", "SLURM_JOB_TIMEOUT"}
    if stage.stage == "analysis_run" and str(raw_code or "").upper() in timeout_codes:
        return "SLURM_TIMEOUT"
    return str(raw_code or f"{stage.stage.upper()}_{terminal['status'].upper()}")


def _template_export_lines(context: Mapping[str, Any]) -> list[str]:
    export_fields = {
        "WORKSPACE_ROOT": context.get("workspace_dir", ""),
        "OBJECT_STORE_ROOT": context.get("object_store_root", context.get("workspace_dir", "")),
        "OBJECT_STORE_PREFIX": context.get("object_store_prefix", ""),
        "NHMS_PUBLISHED_ARTIFACT_ROOT": context.get("published_artifact_root", ""),
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX": context.get("published_artifact_uri_prefix", "published://"),
        "NHMS_RUN_ID": context.get("run_id", ""),
        "NHMS_MODEL_ID": context.get("model_id", ""),
        "NHMS_SOURCE_ID": context.get("source_id", "GFS"),
        "NHMS_CYCLE_ID": context.get("cycle_id", ""),
        "NHMS_CYCLE_TIME": context.get("cycle_time", ""),
        "NHMS_START_TIME": context.get("start_time", ""),
        "NHMS_END_TIME": context.get("end_time", ""),
        "NHMS_BASIN_VERSION_ID": context.get("basin_version_id", ""),
        "NHMS_RIVER_NETWORK_VERSION_ID": context.get("river_network_version_id", ""),
        "NHMS_FORCING_VERSION_ID": context.get("forcing_version_id", ""),
        "NHMS_FORCING_PACKAGE_URI": context.get("forcing_package_uri", ""),
        "NHMS_JOB_TYPE": context.get("job_type", ""),
        "NHMS_RUN_MANIFEST_URI": context.get("run_manifest_uri", ""),
        "NHMS_MANIFEST_INDEX": context.get("manifest_index_path", ""),
        "NHMS_SCHEDULER_REGISTRY_MANIFEST": _first_nonempty(
            os.getenv("NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST"),
            context.get("scheduler_registry_manifest"),
        ),
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX": _first_nonempty(
            os.getenv("NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX"),
            context.get("scheduler_canonical_readiness_index"),
        ),
        "NHMS_SCHEDULER_STATE_INDEX": _first_nonempty(
            os.getenv("NHMS_SLURM_SCHEDULER_STATE_INDEX"),
            context.get("scheduler_state_index"),
        ),
        "NHMS_MAX_CONCURRENT": context.get("max_concurrent", ""),
        "SHUD_THREADS": context.get("shud_threads", ""),
        "OMP_NUM_THREADS": context.get("shud_threads", ""),
    }
    lines = [f"export {key}={shlex.quote(str(value or ''))}" for key, value in export_fields.items()]
    lines.extend(_python_runtime_export_lines())
    grib_env_root = os.getenv("NHMS_GRIB_ENV_ROOT")
    if grib_env_root:
        # Compute nodes (cn01-24) lack cdo/libeccodes; inject the shared conda
        # env's PATH/LD_LIBRARY_PATH so GRIB clip/read works on the node.
        # Quote only the root segment; keep $PATH / ${LD_LIBRARY_PATH:-}
        # outside the quotes so the shell expands them at runtime.
        quoted_root = shlex.quote(grib_env_root)
        lines.append(f"export PATH={quoted_root}/bin:$PATH")
        lines.append(f"export LD_LIBRARY_PATH={quoted_root}/lib:${{LD_LIBRARY_PATH:-}}")
    return lines


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _python_runtime_export_lines() -> list[str]:
    explicit_bin = os.getenv("NHMS_PYTHON_VENV_BIN")
    candidates: list[Path] = []
    if explicit_bin:
        candidates.append(Path(explicit_bin))
    virtual_env = os.getenv("VIRTUAL_ENV")
    if virtual_env:
        candidates.append(Path(virtual_env) / "bin")
    candidates.append(Path.cwd() / ".venv" / "bin")

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        quoted = shlex.quote(str(resolved))
        return [f"export PATH={quoted}:$PATH"]
    return []


def _response_json_or_text(response: httpx.Response) -> dict[str, Any] | str:
    try:
        return response.json()
    except ValueError:
        return response.text


def _error_code_from_response(details: dict[str, Any] | str) -> str:
    if isinstance(details, dict):
        error = details.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return error["code"]
    return "SLURM_GATEWAY_ERROR"
