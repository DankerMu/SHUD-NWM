from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from packages.common.source_identity import normalize_source_id
from packages.common.state_lineage import WARM_START_SUCCESSOR_CHECKPOINT_MISSING
from services.orchestrator import chain as _chain
from services.orchestrator.chain_types import (
    CycleOrchestrationContext,
    ModelContext,
    OrchestratorError,
    StageDefinition,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time

__all__ = (
    "apply_cohort_warm_start",
    "cycle_download_success_missing_raw_manifest",
    "find_existing_stage_job",
    "job_matches_stage",
    "job_needs_submission",
    "normalize_cycle_basins",
    "query_pipeline_jobs_by_cycle",
    "query_pipeline_jobs_for_cycle_context",
    "validate_cycle_basin_identities",
)


def _apply_initial_state_selection_to_basin(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_apply_initial_state_selection_to_basin")(*args, **kwargs)


def _basin_has_prefilled_initial_state(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_basin_has_prefilled_initial_state")(*args, **kwargs)


def _basin_max_lead_hours(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_basin_max_lead_hours")(*args, **kwargs)


def _candidate_scoped_cycle_execution(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_candidate_scoped_cycle_execution")(*args, **kwargs)


def _cycle_pipeline_job_model_id(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_cycle_pipeline_job_model_id")(*args, **kwargs)


def _directory_uri(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_directory_uri")(*args, **kwargs)


def _format_time(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_format_time")(*args, **kwargs)


def _has_uri_scheme(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_has_uri_scheme")(*args, **kwargs)


def _stage_job_sort_key(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_stage_job_sort_key")(*args, **kwargs)


def _terminal_job_statuses() -> set[str]:
    return getattr(_chain, "TERMINAL_JOB_STATUSES")


def _terminal_pipeline_success_statuses() -> set[str]:
    return getattr(_chain, "TERMINAL_PIPELINE_SUCCESS_STATUSES")


def _validate_safe_id(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_validate_safe_id")(*args, **kwargs)


def normalize_cycle_basins(
    self: Any,
    basins: Sequence[Mapping[str, Any] | ModelContext],
    source_id: str,
    cycle_time: datetime,
) -> list[dict[str, Any]]:
    source_id = normalize_source_id(source_id)
    entries: list[dict[str, Any]] = []
    compact_cycle = format_cycle_time(cycle_time)
    for index, basin in enumerate(basins):
        if isinstance(basin, ModelContext):
            entry = {
                "model_id": basin.model_id,
                "basin_id": basin.basin_id,
                "basin_version_id": basin.basin_version_id,
                "river_network_version_id": basin.river_network_version_id,
                "segment_count": basin.segment_count,
                "output_segment_count": basin.output_segment_count,
                "model_package_uri": basin.model_package_uri,
                "model_package_checksum": basin.model_package_checksum,
            }
        else:
            entry = dict(basin)
        if entry.get("model_package_checksum") in (None, "") and entry.get("package_checksum") not in (None, ""):
            entry["model_package_checksum"] = entry["package_checksum"]
        provided_identity_fields = {
            field_name
            for field_name in (
                "candidate_id",
                "source_id",
                "cycle_id",
                "cycle_time",
                "scenario_id",
                "run_id",
                "forcing_version_id",
                "run_manifest_uri",
                "output_uri",
            )
            if entry.get(field_name) not in (None, "")
        }
        model_id = str(entry.get("model_id") or "")
        if not model_id:
            raise OrchestratorError("BASIN_MODEL_ID_MISSING", "Each basin entry requires model_id.")
        missing_production_metadata = [
            field_name
            for field_name in ("basin_version_id", "river_network_version_id", "model_package_uri")
            if entry.get(field_name) in (None, "")
        ]
        provided_run_id = str(entry.get("run_id") or "")
        production_candidate_scope = "candidate_id" in provided_identity_fields or provided_run_id.startswith(
            f"fcst_{source_id.lower()}_{compact_cycle}_"
        )
        if production_candidate_scope and missing_production_metadata:
            raise OrchestratorError(
                "PRODUCTION_CANDIDATE_METADATA_UNAVAILABLE",
                "Production candidate metadata is incomplete; registry/package identity fields are required.",
                {
                    "model_id": model_id,
                    "task_id": index,
                    "missing_fields": missing_production_metadata,
                },
            )
        scenario_id = str(entry.get("scenario_id") or self._forecast_scenario_id(source_id))
        entry.setdefault("basin_id", entry.get("model_id"))
        entry.setdefault("basin_version_id", f"{model_id}_basin")
        entry.setdefault("river_network_version_id", f"{model_id}_river")
        entry.setdefault("run_id", f"fcst_{source_id.lower()}_{compact_cycle}_{model_id}")
        entry.setdefault("forcing_version_id", f"forc_{source_id.lower()}_{compact_cycle}_{model_id}")
        entry.setdefault("workspace_dir", str(Path(self.config.workspace_root)))
        entry.setdefault("source_id", source_id)
        entry.setdefault("cycle_time", compact_cycle)
        entry.setdefault("cycle_id", cycle_id_for(source_id, cycle_time))
        entry.setdefault("scenario_id", scenario_id)
        entry.setdefault("candidate_id", f"{source_id}:{_format_time(cycle_time)}:{model_id}:{scenario_id}")
        entry.setdefault("model_package_uri", f"models/{model_id}/")
        entry.setdefault("output_uri", _directory_uri(self.object_store, f"runs/{entry['run_id']}/output/"))
        entry.setdefault(
            "run_manifest_uri",
            self.object_store.uri_for_key(f"runs/{entry['run_id']}/input/manifest.json"),
        )
        entry.setdefault("log_uri", _directory_uri(self.object_store, f"runs/{entry['run_id']}/logs/"))
        entry["_provided_identity_fields"] = sorted(provided_identity_fields)
        entry["task_id"] = index
        entry.setdefault("original_task_id", index)
        for field_name in (
            "model_id",
            "basin_id",
            "basin_version_id",
            "river_network_version_id",
            "run_id",
        ):
            field_value = entry.get(field_name)
            if field_value not in (None, ""):
                _validate_safe_id(f"basins[{index}].{field_name}", str(field_value))
        entries.append(entry)
    return entries


def apply_cohort_warm_start(
    self: Any,
    basins: Sequence[dict[str, Any]],
    source_id: str,
    cycle_time: datetime,
) -> None:
    """Select each basin's warm-start state so all three manifest faces agree."""

    if self.state_manager is None:
        if self.config.require_forecast_warm_start:
            raise OrchestratorError(
                WARM_START_SUCCESSOR_CHECKPOINT_MISSING,
                "Strict forecast warm-start requires a state manager.",
                {"source_id": source_id, "cycle_time": _format_time(cycle_time)},
            )
        return
    for basin in basins:
        if _basin_has_prefilled_initial_state(basin):
            if self.config.require_forecast_warm_start:
                selection = self._validate_prefilled_forecast_initial_state(
                    basin,
                    source_id=str(basin.get("source_id") or source_id),
                    cycle_time=cycle_time,
                    model_package_version=basin.get("model_package_uri"),
                    model_package_checksum=basin.get("model_package_checksum"),
                )
                _apply_initial_state_selection_to_basin(basin, selection)
            continue
        model_id = str(basin.get("model_id") or "")
        if not model_id:
            continue
        selection = self._select_forecast_initial_state(
            model_id=model_id,
            cycle_time=cycle_time,
            source_id=str(basin.get("source_id") or source_id),
            model_package_version=basin.get("model_package_uri"),
            model_package_checksum=basin.get("model_package_checksum"),
            max_lead_hours=_basin_max_lead_hours(basin),
        )
        _apply_initial_state_selection_to_basin(basin, selection)
        if selection.rejection_code is not None:
            basin["init_state_rejection_code"] = selection.rejection_code


def validate_cycle_basin_identities(
    self: Any,
    basins: Sequence[Mapping[str, Any]],
    source_id: str,
    cycle_time: datetime,
    cycle_id: str,
) -> None:
    seen: dict[str, dict[str, str]] = {
        "model_id": {},
        "candidate_id": {},
        "run_id": {},
        "forcing_version_id": {},
        "run_manifest_uri": {},
        "output_uri": {},
    }
    scenario_id_for_cycle = self._forecast_scenario_id(source_id)
    compact_cycle = format_cycle_time(cycle_time)
    canonical_cycle_time = _format_time(cycle_time)
    for index, basin in enumerate(basins):
        model_id = str(basin.get("model_id") or "")
        provided_identity_fields = set(basin.get("_provided_identity_fields") or [])
        strict_identity = bool(
            provided_identity_fields
            & {
                "candidate_id",
                "source_id",
                "cycle_id",
                "cycle_time",
                "scenario_id",
                "forcing_version_id",
                "run_manifest_uri",
            }
        )
        strict_identity = strict_identity or (
            "run_id" in provided_identity_fields
            and str(basin.get("run_id") or "").startswith(f"fcst_{source_id.lower()}_")
        )
        expected = {
            "source_id": source_id,
            "cycle_id": cycle_id,
            "cycle_time": compact_cycle,
            "scenario_id": scenario_id_for_cycle,
            "candidate_id": f"{source_id}:{canonical_cycle_time}:{model_id}:{scenario_id_for_cycle}",
            "run_id": f"fcst_{source_id.lower()}_{compact_cycle}_{model_id}",
            "forcing_version_id": f"forc_{source_id.lower()}_{compact_cycle}_{model_id}",
            "run_manifest_uri": self.object_store.uri_for_key(
                f"runs/fcst_{source_id.lower()}_{compact_cycle}_{model_id}/input/manifest.json"
            ),
            "output_uri": _directory_uri(
                self.object_store,
                f"runs/fcst_{source_id.lower()}_{compact_cycle}_{model_id}/output/",
            ),
        }
        output_uri = str(basin.get("output_uri") or expected["output_uri"])
        run_manifest_uri = str(basin.get("run_manifest_uri") or expected["run_manifest_uri"])
        values = {
            "model_id": model_id,
            "candidate_id": str(basin.get("candidate_id") or expected["candidate_id"]),
            "run_id": str(basin.get("run_id") or expected["run_id"]),
            "forcing_version_id": str(basin.get("forcing_version_id") or expected["forcing_version_id"]),
            "run_manifest_uri": run_manifest_uri,
            "output_uri": output_uri.rstrip("/") + "/" if _has_uri_scheme(output_uri) else output_uri.strip(),
        }
        for field_name, value in values.items():
            previous = seen[field_name].get(value)
            if previous is not None:
                raise OrchestratorError(
                    "DUPLICATE_CANDIDATE_IDENTITY",
                    f"Duplicate {field_name} in cycle basin list.",
                    {
                        "field": field_name,
                        "value": value,
                        "first_model_id": previous,
                        "model_id": model_id,
                        "task_id": index,
                    },
                )
            seen[field_name][value] = model_id
        for field_name, expected_value in expected.items():
            actual = basin.get(field_name)
            if actual in (None, ""):
                continue
            if not strict_identity and field_name not in {"source_id", "cycle_id", "cycle_time", "scenario_id"}:
                continue
            if not strict_identity and field_name in {"source_id", "cycle_id", "cycle_time", "scenario_id"}:
                if field_name not in provided_identity_fields:
                    continue
            if field_name == "cycle_time":
                try:
                    actual_value = format_cycle_time(actual)
                except (TypeError, ValueError) as exc:
                    raise OrchestratorError(
                        "CANDIDATE_IDENTITY_MISMATCH",
                        f"basins[{index}].cycle_time is not a valid cycle time.",
                        {"field": field_name, "actual": actual, "task_id": index},
                    ) from exc
            elif field_name == "output_uri":
                actual_text = str(actual).strip()
                if _has_uri_scheme(actual_text):
                    actual_value = actual_text.rstrip("/") + "/"
                elif actual_text.strip("/") == f"runs/{expected['run_id']}/output":
                    actual_value = str(expected_value)
                    if isinstance(basin, dict):
                        basin["output_uri"] = actual_value
                else:
                    actual_value = actual_text
                expected_value = str(expected_value)
            else:
                actual_value = str(actual)
                expected_value = str(expected_value)
            if actual_value != expected_value:
                raise OrchestratorError(
                    "CANDIDATE_IDENTITY_MISMATCH",
                    f"basins[{index}].{field_name} does not match the orchestration context.",
                    {
                        "field": field_name,
                        "actual": actual_value,
                        "expected": expected_value,
                        "task_id": index,
                        "model_id": model_id,
                    },
                )


def query_pipeline_jobs_by_cycle(self: Any, cycle_id: str) -> list[dict[str, Any]]:
    query = getattr(self.repository, "query_pipeline_jobs_by_cycle", None)
    if callable(query):
        return [dict(job) for job in query(cycle_id)]
    return []


def query_pipeline_jobs_for_cycle_context(self: Any, context: CycleOrchestrationContext) -> list[dict[str, Any]]:
    if _candidate_scoped_cycle_execution(context.all_basins):
        query = getattr(self.repository, "query_pipeline_jobs_by_run", None)
        if callable(query):
            return [dict(job) for job in query(context.run_id)]
        candidate_model_id = _cycle_pipeline_job_model_id(context)
        return [
            job
            for job in self._query_pipeline_jobs_by_cycle(context.cycle_id)
            if str(job.get("run_id") or "") == context.run_id
            or (candidate_model_id is not None and str(job.get("model_id") or "") == candidate_model_id)
        ]
    return self._query_pipeline_jobs_by_cycle(context.cycle_id)


def find_existing_stage_job(
    self: Any,
    jobs: Sequence[Mapping[str, Any]],
    stage: StageDefinition,
    *,
    context: CycleOrchestrationContext,
) -> dict[str, Any] | None:
    matches = [dict(job) for job in jobs if self._job_matches_stage(job, stage)]
    if not matches:
        return None
    active_matches = [job for job in matches if str(job.get("status")) not in _terminal_job_statuses()]
    return dict(max(active_matches or matches, key=lambda job: _stage_job_sort_key(job, stage)))


def cycle_download_success_missing_raw_manifest(
    self: Any,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    job: Mapping[str, Any],
) -> bool:
    if stage.stage != "download":
        return False
    if str(job.get("status") or "") not in _terminal_pipeline_success_statuses():
        return False
    manifest_uri = self.object_store.uri_for_key(
        f"raw/{context.source_id}/{format_cycle_time(context.cycle_time)}/manifest.json"
    )
    return not self.object_store.exists(manifest_uri)


def job_matches_stage(job: Mapping[str, Any], stage: StageDefinition) -> bool:
    return job.get("stage") == stage.stage or job.get("job_type") == stage.job_type


def job_needs_submission(job: Mapping[str, Any]) -> bool:
    return str(job.get("status")) == "pending" and not job.get("slurm_job_id")
