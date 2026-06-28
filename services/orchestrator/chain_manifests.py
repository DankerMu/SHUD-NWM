from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from packages.common.manifest_index import ManifestValidationError, serialize_manifest_index
from packages.common.object_store import LocalObjectStore
from packages.common.safe_fs import SafeFilesystemError
from packages.common.source_identity import normalize_source_id
from services.orchestrator.chain_manifest_contracts import (
    _assembly_from_entry,
    _assembly_payload_from_runtime_manifest,
    _assembly_quality_states,
    _basin_identifier,
    _basin_key,
    _cycle_payload_model_id,
    _cycle_residual_blockers,
    _default_forcing_uri,
    _directory_uri,
    _display_contract,
    _ensure_utc,
    _first_optional_int,
    _forecast_state_checkpoint_hours,
    _format_time,
    _format_time_or_none,
    _frequency_contract,
    _frequency_quality_state,
    _has_uri_scheme,
    _model_package_manifest_uri,
    _model_run_stage_evidence,
    _nested_mapping,
    _nested_value,
    _optional_int,
    _output_river_contract,
    _parse_gateway_time,
    _preserve_directory_uri,
    _project_name_for_basin,
    _publish_quality_state,
    _runtime_forcing_metadata,
    _safe_project_name,
    _station_metadata_for_basin,
    _tri_state,
)
from services.orchestrator.chain_types import (
    AnalysisRunContext,
    CycleOrchestrationContext,
    ForecastRunContext,
    ModelRunAssembly,
    OrchestratorError,
    StageDefinition,
)
from services.orchestrator.production_contract import (
    PRODUCTION_CONTRACT_ID,
    PRODUCTION_CONTRACT_SCHEMA_VERSION,
    production_stage_for,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

__all__ = (
    "ANALYSIS_SCENARIO_ID",
    "DEFAULT_ERA5_REANALYSIS_LATENCY_MINUTES",
    "FORCING_CAUSALITY_CAUSAL",
    "FORCING_CAUSALITY_DELAYED_REANALYSIS",
    "ManifestValidationError",
    "PRODUCTION_CONTRACT_ID",
    "PRODUCTION_CONTRACT_SCHEMA_VERSION",
    "build_analysis_run_manifest",
    "build_cycle_stage_manifest",
    "build_forecast_run_manifest",
    "build_forecast_runtime_manifest",
    "build_model_run_assembly",
    "build_reindexed_manifest",
    "prepare_forecast_runtime_manifests",
    "production_stage_for",
    "reindexed_manifest_entries",
    "serialize_manifest_index",
    "validate_forecast_runtime_manifest",
    "write_cycle_manifest_index",
    "write_run_manifest",
    "_analysis_forcing_causality",
    "_analysis_update_ic_step_minutes",
    "_assembly_from_entry",
    "_assembly_payload_from_runtime_manifest",
    "_assembly_quality_states",
    "_basin_identifier",
    "_basin_key",
    "_cycle_payload_model_id",
    "_cycle_residual_blockers",
    "_default_forcing_uri",
    "_directory_uri",
    "_display_contract",
    "_ensure_segment_utc",
    "_ensure_utc",
    "_era5_reanalysis_latency_minutes",
    "_first_optional_int",
    "_forecast_state_checkpoint_hours",
    "_format_time",
    "_format_time_or_none",
    "_frequency_contract",
    "_frequency_quality_state",
    "_has_uri_scheme",
    "_model_package_manifest_uri",
    "_model_run_stage_evidence",
    "_nested_mapping",
    "_nested_value",
    "_optional_int",
    "_output_river_contract",
    "_parse_gateway_time",
    "_preserve_directory_uri",
    "_project_name_for_basin",
    "_publish_quality_state",
    "_runtime_forcing_metadata",
    "_safe_project_name",
    "_station_metadata_for_basin",
    "_tri_state",
)

ANALYSIS_SCENARIO_ID = "analysis_true_field"
DEFAULT_ERA5_REANALYSIS_LATENCY_MINUTES = 5 * 24 * 60
FORCING_CAUSALITY_CAUSAL = "causal"
FORCING_CAUSALITY_DELAYED_REANALYSIS = "delayed_reanalysis"


AssemblyBuilder = Callable[..., ModelRunAssembly]
ReindexBuilder = Callable[[Sequence[Mapping[str, Any]], Sequence[int]], list[dict[str, Any]]]
StageEvidenceBuilder = Callable[[str, Mapping[str, Any]], dict[str, Any]]
QualityStateBuilder = Callable[[Mapping[str, Any]], dict[str, Any]]
ResidualBlockerBuilder = Callable[[Sequence[Mapping[str, Any]]], list[dict[str, Any]]]
AssemblyPayloadBuilder = Callable[[Mapping[str, Any]], dict[str, Any]]
DefaultForcingUriBuilder = Callable[[str, str, str, str, LocalObjectStore], str]
DirectoryPreserver = Callable[[str | None, LocalObjectStore, str], str]


class ChainManifestOrchestrator(Protocol):
    config: Any
    object_store: LocalObjectStore
    repository: Any

    def _workspace_path(self, *parts: str) -> Path: ...

    def _safe_workspace_write_bytes(self, path: Path, content: bytes) -> Path: ...

    def _safe_workspace_read_bytes(self, path: Path) -> bytes: ...

    def _forecast_scenario_id(self, source_id: str) -> str: ...

    def _reindexed_manifest_entries(self, basins: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]: ...

    def _build_forecast_runtime_manifest(
        self,
        context: CycleOrchestrationContext,
        basin: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def _validate_forecast_runtime_manifest(
        self,
        manifest_path: Path,
        manifest: Mapping[str, Any],
        *,
        task_index: int,
    ) -> None: ...

    def _mark_staged_hydro_runs_failed(
        self,
        run_ids: Sequence[str],
        *,
        error_code: str,
        error_message: str,
    ) -> None: ...


def build_cycle_stage_manifest(
    orchestrator: ChainManifestOrchestrator,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    *,
    model_run_stage_evidence: Callable[..., dict[str, Any]] | None = None,
    frequency_quality_state: Callable[..., dict[str, Any]] | None = None,
    publish_quality_state: Callable[..., dict[str, Any]] | None = None,
    cycle_residual_blockers: Callable[[Sequence[Mapping[str, Any]]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    model_run_stage_evidence = model_run_stage_evidence or _model_run_stage_evidence
    if frequency_quality_state is None:
        def frequency_quality_state(entry: Mapping[str, Any], *, cycle_id: str) -> dict[str, Any]:
            return _frequency_quality_state(
                entry,
                cycle_id=cycle_id,
                model_run_stage_evidence=model_run_stage_evidence,
            )

    if publish_quality_state is None:
        def publish_quality_state(entry: Mapping[str, Any], *, cycle_id: str) -> dict[str, Any]:
            return _publish_quality_state(
                entry,
                cycle_id=cycle_id,
                model_run_stage_evidence=model_run_stage_evidence,
            )

    cycle_residual_blockers = cycle_residual_blockers or _cycle_residual_blockers
    manifest_index_entries = orchestrator._reindexed_manifest_entries(context.active_basins)
    manifest: dict[str, Any] = {
        "run_id": context.run_id,
        "model_id": _cycle_payload_model_id(context),
        "job_type": stage.job_type,
        "stage": stage.stage,
        "stage_name": stage.stage,
        "cycle_id": context.cycle_id,
        "source_id": context.source_id,
        "cycle_time": _format_time(context.cycle_time),
        "workspace_dir": str(Path(orchestrator.config.workspace_root)),
        "object_store_root": str(Path(orchestrator.config.object_store_root)),
        "object_store_prefix": orchestrator.config.object_store_prefix,
        "published_artifact_root": os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT", ""),
        "published_artifact_uri_prefix": os.getenv("NHMS_PUBLISHED_ARTIFACT_URI_PREFIX", "published://"),
        "scheduler_db_free_required": os.getenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", ""),
        "scheduler_allowed_roots": os.getenv("NHMS_SCHEDULER_ALLOWED_ROOTS", ""),
        "scheduler_state_index_backend": os.getenv("NHMS_SCHEDULER_STATE_INDEX_BACKEND", ""),
        "scheduler_state_index": os.getenv("NHMS_SCHEDULER_STATE_INDEX", ""),
        "total_basins": len(context.all_basins),
        "active_basins": len(context.active_basins),
        "manifest_index": manifest_index_entries,
        "model_runs": [
            model_run_stage_evidence(stage.stage, entry, cycle_id=context.cycle_id)
            for entry in manifest_index_entries
        ],
        "identity_contract": {
            "source_id": context.source_id,
            "cycle_id": context.cycle_id,
            "cycle_time": _format_time(context.cycle_time),
            "scenario_ids": sorted(
                {
                    str(entry.get("scenario_id") or orchestrator._forecast_scenario_id(context.source_id))
                    for entry in manifest_index_entries
                }
            ),
            "run_ids": [str(entry["run_id"]) for entry in manifest_index_entries],
            "model_ids": [str(entry["model_id"]) for entry in manifest_index_entries],
        },
    }
    if stage.stage == "frequency":
        manifest["quality_states"] = [
            frequency_quality_state(entry, cycle_id=context.cycle_id) for entry in manifest_index_entries
        ]
    if stage.stage == "publish":
        active_keys = {_basin_key(basin) for basin in context.active_basins}
        excluded = [basin for basin in context.all_basins if _basin_key(basin) not in active_keys]
        quality_states = [
            publish_quality_state(entry, cycle_id=context.cycle_id) for entry in manifest_index_entries
        ]
        manifest["metadata"] = {
            "total_basins": len(context.all_basins),
            "published_basins": len(context.active_basins),
            "excluded_basins": [_basin_identifier(basin) for basin in excluded],
            "quality_states": quality_states,
            "residual_blockers": cycle_residual_blockers(manifest_index_entries),
        }
        manifest["basins"] = list(context.active_basins)
        manifest["quality_states"] = quality_states
    return manifest


def write_cycle_manifest_index(
    orchestrator: ChainManifestOrchestrator,
    context: CycleOrchestrationContext,
    stage: StageDefinition,
    tasks: list[dict[str, Any]],
) -> Path:
    try:
        content = serialize_manifest_index(tasks)
    except ManifestValidationError as exc:
        raise OrchestratorError(
            "CYCLE_MANIFEST_INDEX_INVALID",
            f"Cycle manifest index for stage {stage.stage} exceeds the Slurm array manifest contract.",
            {"stage": stage.stage, **exc.details},
        ) from exc
    manifest_path = orchestrator._workspace_path("runs", context.run_id, "input", f"{stage.stage}_manifest_index.json")
    try:
        orchestrator._safe_workspace_write_bytes(manifest_path, content)
    except (OSError, SafeFilesystemError) as exc:
        raise OrchestratorError(
            "CYCLE_MANIFEST_INDEX_WRITE_FAILED",
            f"Failed to write cycle manifest index safely for stage {stage.stage}: {exc}",
            {"manifest_path": str(manifest_path), "stage": stage.stage},
        ) from exc
    return manifest_path


def prepare_forecast_runtime_manifests(
    orchestrator: ChainManifestOrchestrator,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    *,
    assembly_payload_from_runtime_manifest: AssemblyPayloadBuilder | None = None,
) -> None:
    if stage.stage != "forecast":
        return

    assembly_payload_from_runtime_manifest = (
        assembly_payload_from_runtime_manifest or _assembly_payload_from_runtime_manifest
    )
    staged: list[tuple[int, Mapping[str, Any], dict[str, Any], bytes, Path, str]] = []
    for index, basin in enumerate(context.active_basins):
        manifest = orchestrator._build_forecast_runtime_manifest(context, basin)
        content = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        manifest_uri = manifest["outputs"]["run_manifest_uri"]
        try:
            orchestrator.object_store.write_bytes_atomic(manifest_uri, content)
        except OSError as exc:
            raise OrchestratorError(
                "RUNTIME_MANIFEST_WRITE_FAILED",
                f"Failed to write runtime manifest to object store for task {index}: {exc}",
                {"task_id": index, "manifest_uri": manifest_uri},
            ) from exc

        manifest_path = orchestrator._workspace_path("runs", str(basin["run_id"]), "input", "manifest.json")
        try:
            orchestrator._safe_workspace_write_bytes(manifest_path, content)
        except (OSError, SafeFilesystemError) as exc:
            raise OrchestratorError(
                "RUNTIME_MANIFEST_WRITE_FAILED",
                f"Failed to write runtime manifest safely for task {index}: {exc}",
                {"task_id": index, "manifest_path": str(manifest_path)},
            ) from exc

        orchestrator._validate_forecast_runtime_manifest(manifest_path, manifest, task_index=index)
        staged.append((index, basin, manifest, content, manifest_path, manifest_uri))

    created_run_ids: list[str] = []
    try:
        for _index, basin, manifest, _content, manifest_path, _manifest_uri in staged:
            orchestrator.repository.create_hydro_run_from_basin(basin, manifest)
            created_run_ids.append(str(manifest["run_id"]))
            basin["manifest_path"] = str(manifest_path)
            basin["model_run_assembly"] = assembly_payload_from_runtime_manifest(manifest)
            basin["output_uri"] = manifest["outputs"]["output_uri"]
            basin["run_manifest_uri"] = manifest["outputs"]["run_manifest_uri"]
            basin["log_uri"] = manifest["outputs"]["log_uri"]
    except Exception as exc:
        orchestrator._mark_staged_hydro_runs_failed(
            created_run_ids,
            error_code=getattr(exc, "error_code", "RUNTIME_MANIFEST_STAGING_FAILED"),
            error_message=getattr(exc, "message", str(exc)),
        )
        raise


def build_forecast_runtime_manifest(
    orchestrator: ChainManifestOrchestrator,
    context: CycleOrchestrationContext,
    basin: Mapping[str, Any],
    *,
    assembly_builder: AssemblyBuilder | None = None,
    forecast_state_checkpoint_hours: Callable[[Any], list[int]] | None = None,
) -> dict[str, Any]:
    assembly_builder = assembly_builder or build_model_run_assembly
    forecast_state_checkpoint_hours = forecast_state_checkpoint_hours or _forecast_state_checkpoint_hours
    assembly = assembly_builder(
        basin,
        source_id=context.source_id,
        cycle_id=context.cycle_id,
        cycle_time=context.cycle_time,
        scenario_id=str(basin.get("scenario_id") or orchestrator._forecast_scenario_id(context.source_id)),
        workspace_root=Path(orchestrator.config.workspace_root),
        object_store=orchestrator.object_store,
        default_forecast_horizon_hours=orchestrator.config.forecast_horizon_hours,
    )
    run_id = str(basin["run_id"])
    manifest = {
        "run_id": run_id,
        "run_type": "forecast",
        "candidate_id": assembly.identity["candidate_id"],
        "scenario_id": assembly.identity["scenario_id"],
        "source_id": context.source_id,
        "cycle_time": _format_time(context.cycle_time),
        "start_time": assembly.identity["start_time"],
        "end_time": assembly.identity["end_time"],
        "forecast_horizon_hours": assembly.identity["forecast_horizon_hours"],
        "workspace_dir": str(Path(orchestrator.config.workspace_root)),
        "object_store_root": str(Path(orchestrator.config.object_store_root)),
        "object_store_prefix": orchestrator.config.object_store_prefix,
        "identity": dict(assembly.identity),
        "model": {
            "model_id": assembly.identity["model_id"],
            "basin_id": basin.get("basin_id"),
            "basin_version_id": assembly.identity["basin_version_id"],
            "river_network_version_id": assembly.identity["river_network_version_id"],
            "model_package_uri": assembly.identity["model_package_uri"],
            "model_package_manifest_uri": assembly.identity["model_package_manifest_uri"],
            "model_package_checksum": assembly.identity.get("model_package_checksum"),
            "segment_count": assembly.identity["segment_count"],
            "project_name": assembly.runtime.get("project_name"),
        },
        "forcing": dict(assembly.forcing),
        "initial_state": {
            "state_id": basin.get("init_state_id"),
            "ic_file_uri": basin.get("init_state_uri"),
            "valid_time": _format_time_or_none(_parse_gateway_time(basin.get("init_state_valid_time"))),
            "checksum": basin.get("init_state_checksum"),
            "quality": basin.get("init_state_quality") or "cold_start_no_state",
            "lineage": dict(basin.get("init_state_lineage") or {}),
        },
        "runtime": dict(assembly.runtime),
        "outputs": dict(assembly.outputs),
        "frequency": dict(assembly.frequency),
        "display": dict(assembly.display),
        "quality_states": dict(assembly.quality_states),
        "residual_blockers": [dict(item) for item in assembly.residual_blockers],
    }
    manifest["runtime"]["init_mode"] = 3 if basin.get("init_state_id") or basin.get("init_state_uri") else 1
    checkpoint_hours = forecast_state_checkpoint_hours(manifest["forecast_horizon_hours"])
    if checkpoint_hours:
        manifest["runtime"]["state_checkpoint_hours"] = checkpoint_hours
        manifest["runtime"]["update_ic_step_minutes"] = min(checkpoint_hours) * 60
    return manifest


def validate_forecast_runtime_manifest(
    orchestrator: ChainManifestOrchestrator,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    *,
    task_index: int,
) -> None:
    if not manifest_path.exists():
        raise OrchestratorError(
            "RUNTIME_MANIFEST_MISSING",
            f"Forecast runtime manifest was not written for task {task_index}.",
            {"manifest_path": str(manifest_path), "task_id": task_index},
        )
    try:
        persisted = json.loads(orchestrator._safe_workspace_read_bytes(manifest_path).decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise OrchestratorError(
            "RUNTIME_MANIFEST_INVALID_JSON",
            f"Forecast runtime manifest is not valid JSON for task {task_index}.",
            {"manifest_path": str(manifest_path), "task_id": task_index, "error": str(exc)},
        ) from exc
    except (OSError, SafeFilesystemError) as exc:
        raise OrchestratorError(
            "RUNTIME_MANIFEST_READ_FAILED",
            f"Forecast runtime manifest cannot be safely read for task {task_index}: {exc}",
            {"manifest_path": str(manifest_path), "task_id": task_index},
        ) from exc
    required_paths = (
        ("run_id",),
        ("run_type",),
        ("scenario_id",),
        ("source_id",),
        ("cycle_time",),
        ("start_time",),
        ("end_time",),
        ("model", "model_id"),
        ("model", "basin_version_id"),
        ("model", "river_network_version_id"),
        ("model", "model_package_uri"),
        ("forcing", "forcing_uri"),
        ("outputs", "run_manifest_uri"),
        ("outputs", "output_uri"),
        ("outputs", "log_uri"),
        ("identity",),
        ("workspace_dir",),
        ("object_store_root",),
    )
    missing = [".".join(path) for path in required_paths if _nested_value(persisted, path) in (None, "")]
    if missing:
        raise OrchestratorError(
            "RUNTIME_MANIFEST_INVALID",
            f"Forecast runtime manifest is missing required fields for task {task_index}: {', '.join(missing)}.",
            {"manifest_path": str(manifest_path), "task_id": task_index, "missing_fields": missing},
        )
    if persisted.get("run_id") != manifest.get("run_id"):
        raise OrchestratorError(
            "RUNTIME_MANIFEST_INVALID",
            f"Forecast runtime manifest run_id mismatch for task {task_index}.",
            {"manifest_path": str(manifest_path), "task_id": task_index},
        )


def reindexed_manifest_entries(
    orchestrator: ChainManifestOrchestrator,
    basins: Sequence[Mapping[str, Any]],
    *,
    reindex_builder: ReindexBuilder | None = None,
    assembly_builder: AssemblyBuilder | None = None,
) -> list[dict[str, Any]]:
    reindex_builder = reindex_builder or build_reindexed_manifest
    assembly_builder = assembly_builder or build_model_run_assembly
    entries = reindex_builder([dict(basin) for basin in basins], range(len(basins)))
    for entry in entries:
        if "model_run_assembly" not in entry:
            source_id = normalize_source_id(str(entry.get("source_id") or orchestrator.config.source_id))
            cycle_time = parse_cycle_time(entry["cycle_time"])
            assembly = assembly_builder(
                entry,
                source_id=source_id,
                cycle_id=str(entry.get("cycle_id") or cycle_id_for(source_id, cycle_time)),
                cycle_time=cycle_time,
                scenario_id=str(entry.get("scenario_id") or orchestrator._forecast_scenario_id(source_id)),
                workspace_root=Path(orchestrator.config.workspace_root),
                object_store=orchestrator.object_store,
                default_forecast_horizon_hours=orchestrator.config.forecast_horizon_hours,
            )
            entry["model_run_assembly"] = assembly.to_manifest_entry()
            entry["output_uri"] = assembly.outputs["output_uri"]
            entry["run_manifest_uri"] = assembly.outputs["run_manifest_uri"]
            entry["log_uri"] = assembly.outputs["log_uri"]
    return entries


def build_forecast_run_manifest(
    context: ForecastRunContext,
    *,
    forecast_state_checkpoint_hours: Callable[[Any], list[int]] | None = None,
) -> dict[str, Any]:
    forecast_state_checkpoint_hours = forecast_state_checkpoint_hours or _forecast_state_checkpoint_hours
    manifest = {
        "run_id": context.run_id,
        "run_type": "forecast",
        "scenario_id": context.scenario_id,
        "source_id": context.source_id,
        "cycle_time": _format_time(context.cycle_time),
        "start_time": _format_time(context.start_time),
        "end_time": _format_time(context.end_time),
        "forecast_horizon_hours": context.forecast_horizon_hours,
        "model": {
            "model_id": context.model_id,
            "basin_version_id": context.basin_version_id,
            "river_network_version_id": context.river_network_version_id,
            "model_package_uri": context.model_package_uri,
            "segment_count": context.segment_count,
            "output_segment_count": (
                context.output_segment_count if context.output_segment_count is not None else context.segment_count
            ),
        },
        "forcing": _runtime_forcing_metadata(
            {
                "forcing_version_id": context.forcing_version_id,
                "forcing_uri": context.forcing_package_uri,
                "forcing_package_uri": context.forcing_package_uri,
                "package_manifest_uri": context.forcing_package_manifest_uri,
                "package_manifest_checksum": context.forcing_package_manifest_checksum,
            }
        ),
        "initial_state": {
            "state_id": context.init_state_id,
            "ic_file_uri": context.init_state_uri,
            "valid_time": _format_time_or_none(context.init_state_valid_time),
            "checksum": context.init_state_checksum,
            "quality": context.init_state_quality,
        },
        "runtime": {
            "output_interval_minutes": 60,
            "init_mode": 3 if context.init_state_id else 1,
        },
        "outputs": {
            "run_manifest_uri": context.run_manifest_uri,
            "output_uri": context.output_uri,
            "log_uri": context.log_uri,
            "output_segment_count": (
                context.output_segment_count if context.output_segment_count is not None else context.segment_count
            ),
            "gis_segment_count": context.segment_count,
        },
    }
    if context.init_state_lineage:
        manifest["initial_state"]["lineage"] = dict(context.init_state_lineage)
    checkpoint_hours = forecast_state_checkpoint_hours(context.forecast_horizon_hours)
    if checkpoint_hours:
        manifest["runtime"]["state_checkpoint_hours"] = checkpoint_hours
        manifest["runtime"]["update_ic_step_minutes"] = min(checkpoint_hours) * 60
    return manifest


def build_analysis_run_manifest(
    context: AnalysisRunContext,
    *,
    analysis_forcing_causality: Callable[[], Mapping[str, Any]] | None = None,
    analysis_update_ic_step_minutes: Callable[[datetime, datetime], int] | None = None,
) -> dict[str, Any]:
    analysis_forcing_causality = analysis_forcing_causality or _analysis_forcing_causality
    analysis_update_ic_step_minutes = analysis_update_ic_step_minutes or _analysis_update_ic_step_minutes
    return {
        "run_id": context.run_id,
        "run_type": "analysis",
        "scenario_id": ANALYSIS_SCENARIO_ID,
        "source_id": context.source_id,
        "cycle_time": _format_time(context.cycle_time),
        "start_time": _format_time(context.start_time),
        "end_time": _format_time(context.end_time),
        "model": {
            "model_id": context.model_id,
            "basin_version_id": context.basin_version_id,
            "river_network_version_id": context.river_network_version_id,
            "model_package_uri": context.model_package_uri,
            "segment_count": context.segment_count,
            "output_segment_count": (
                context.output_segment_count if context.output_segment_count is not None else context.segment_count
            ),
        },
        "initial_state": {
            "state_id": context.init_state_id,
            "ic_file_uri": context.init_state_uri,
            "valid_time": _format_time_or_none(context.init_state_valid_time),
        },
        "forcing": _runtime_forcing_metadata(
            {
                "forcing_version_id": context.forcing_version_id,
                "forcing_uri": context.forcing_package_uri,
                "forcing_package_uri": context.forcing_package_uri,
                "package_manifest_uri": context.forcing_package_manifest_uri,
                "package_manifest_checksum": context.forcing_package_manifest_checksum,
            }
        ),
        "forcing_causality": dict(
            context.forcing_causality if context.forcing_causality is not None else analysis_forcing_causality()
        ),
        "runtime": {
            "output_interval_minutes": 60,
            "init_mode": 3 if context.init_state_id else 1,
            "update_ic_step_minutes": (
                context.update_ic_step_minutes
                if context.update_ic_step_minutes is not None
                else analysis_update_ic_step_minutes(context.start_time, context.end_time)
            ),
        },
        "outputs": {
            "run_manifest_uri": context.run_manifest_uri,
            "output_uri": context.output_uri,
            "log_uri": context.log_uri,
            "output_segment_count": (
                context.output_segment_count if context.output_segment_count is not None else context.segment_count
            ),
            "gis_segment_count": context.segment_count,
        },
    }


def write_run_manifest(
    orchestrator: ChainManifestOrchestrator,
    context: ForecastRunContext | AnalysisRunContext,
    manifest: dict[str, Any],
) -> None:
    content = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    orchestrator.object_store.write_bytes_atomic(context.run_manifest_uri, content)
    workspace_manifest = orchestrator._workspace_path("runs", context.run_id, "input", "manifest.json")
    try:
        orchestrator._safe_workspace_write_bytes(workspace_manifest, content)
    except (OSError, SafeFilesystemError) as exc:
        raise OrchestratorError(
            "RUNTIME_MANIFEST_WRITE_FAILED",
            f"Failed to write run manifest safely: {exc}",
            {"manifest_path": str(workspace_manifest), "run_id": context.run_id},
        ) from exc


def _analysis_update_ic_step_minutes(start_time: datetime, end_time: datetime) -> int:
    """Restart cadence (minutes) that writes a SHUD restart state exactly at ``end_time``."""

    duration_seconds = (_ensure_segment_utc(end_time) - _ensure_segment_utc(start_time)).total_seconds()
    if duration_seconds <= 0:
        raise OrchestratorError(
            "ANALYSIS_SEGMENT_INVALID_WINDOW",
            "Analysis segment end_time must be after start_time.",
            {"start_time": start_time.isoformat(), "end_time": end_time.isoformat()},
        )
    if duration_seconds % 60 != 0:
        raise OrchestratorError(
            "ANALYSIS_SEGMENT_NON_MINUTE_ALIGNED",
            "Analysis segment length must be a whole number of minutes for restart cadence.",
            {"duration_seconds": duration_seconds},
        )
    return int(duration_seconds // 60)


def _analysis_forcing_causality(latency_minutes: int | None = None) -> dict[str, Any]:
    resolved_latency = _era5_reanalysis_latency_minutes() if latency_minutes is None else int(latency_minutes)
    return {
        "mode": FORCING_CAUSALITY_DELAYED_REANALYSIS,
        "latency_minutes": resolved_latency,
        "no_future_leak": True,
    }


def _era5_reanalysis_latency_minutes() -> int:
    raw = os.getenv("ERA5_REANALYSIS_LATENCY_MINUTES", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_ERA5_REANALYSIS_LATENCY_MINUTES


def _ensure_segment_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def build_reindexed_manifest(
    entries: Sequence[Mapping[str, Any]],
    succeeded_task_ids: Sequence[int],
) -> list[dict[str, Any]]:
    by_task_id = {int(entry.get("task_id", index)): dict(entry) for index, entry in enumerate(entries)}
    reindexed: list[dict[str, Any]] = []
    for new_task_id, previous_task_id in enumerate(succeeded_task_ids):
        entry = dict(by_task_id[int(previous_task_id)])
        entry["task_id"] = new_task_id
        entry["original_task_id"] = int(entry.get("original_task_id", previous_task_id))
        reindexed.append(entry)
    return reindexed


def build_model_run_assembly(
    basin: Mapping[str, Any],
    *,
    source_id: str,
    cycle_id: str,
    cycle_time: datetime,
    scenario_id: str,
    workspace_root: Path,
    object_store: LocalObjectStore,
    default_forecast_horizon_hours: int,
    default_forcing_uri: DefaultForcingUriBuilder | None = None,
    preserve_directory_uri: DirectoryPreserver | None = None,
    station_metadata_for_basin: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    output_river_contract: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    frequency_contract: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    display_contract: Callable[..., dict[str, Any]] | None = None,
    assembly_quality_states: Callable[..., tuple[dict[str, Any], list[dict[str, Any]]]] | None = None,
    project_name_for_basin: Callable[..., str] | None = None,
    model_package_manifest_uri: Callable[[Mapping[str, Any], str], str] | None = None,
) -> ModelRunAssembly:
    del workspace_root
    default_forcing_uri = default_forcing_uri or _default_forcing_uri
    preserve_directory_uri = preserve_directory_uri or _preserve_directory_uri
    station_metadata_for_basin = station_metadata_for_basin or _station_metadata_for_basin
    output_river_contract = output_river_contract or _output_river_contract
    frequency_contract = frequency_contract or _frequency_contract
    display_contract = display_contract or _display_contract
    assembly_quality_states = assembly_quality_states or _assembly_quality_states
    project_name_for_basin = project_name_for_basin or _project_name_for_basin
    model_package_manifest_uri = model_package_manifest_uri or _model_package_manifest_uri
    source_id = normalize_source_id(source_id)
    cycle_time = _ensure_utc(cycle_time)
    compact_cycle = format_cycle_time(cycle_time)
    model_id = str(basin["model_id"])
    run_id = str(basin.get("run_id") or f"fcst_{source_id.lower()}_{compact_cycle}_{model_id}")
    forcing_version_id = str(
        basin.get("forcing_version_id") or f"forc_{source_id.lower()}_{compact_cycle}_{model_id}"
    )
    basin_version_id = str(basin["basin_version_id"])
    river_network_version_id = str(basin["river_network_version_id"])
    forecast_horizon_hours = int(
        basin.get("forecast_horizon_hours")
        or basin.get("max_lead_hours")
        or default_forecast_horizon_hours
    )
    start_time = cycle_time
    end_time = start_time + timedelta(hours=forecast_horizon_hours)
    model_package_uri = str(basin.get("model_package_uri") or f"models/{model_id}/")
    forcing_uri = str(
        basin.get("forcing_package_uri")
        or basin.get("forcing_uri")
        or default_forcing_uri(source_id, compact_cycle, basin_version_id, model_id, object_store)
    )
    output_uri = preserve_directory_uri(
        str(basin.get("output_uri")) if basin.get("output_uri") not in (None, "") else None,
        object_store,
        f"runs/{run_id}/output/",
    )
    run_manifest_uri = str(
        basin.get("run_manifest_uri") or object_store.uri_for_key(f"runs/{run_id}/input/manifest.json")
    )
    log_uri = preserve_directory_uri(
        str(basin.get("log_uri")) if basin.get("log_uri") not in (None, "") else None,
        object_store,
        f"runs/{run_id}/logs/",
    )
    candidate_id = str(basin.get("candidate_id") or f"{source_id}:{_format_time(cycle_time)}:{model_id}:{scenario_id}")
    station_metadata = station_metadata_for_basin(basin)
    output_river = output_river_contract(basin)
    frequency = frequency_contract(basin)
    display = display_contract(basin, output_uri=output_uri)
    quality_states, blockers = assembly_quality_states(
        basin,
        station_metadata=station_metadata,
        output_river=output_river,
        frequency=frequency,
        display=display,
    )
    runtime = {
        "command_style": str(
            basin.get("shud_command_style")
            or _nested_mapping(basin.get("runtime")).get("command_style")
            or "shud_project"
        ),
        "project_name": project_name_for_basin(basin, fallback=model_id),
        "output_interval_minutes": int(
            basin.get("output_interval_minutes")
            or _nested_mapping(basin.get("runtime")).get("output_interval_minutes")
            or 60
        ),
        "threads": int(
            basin.get("shud_threads")
            or _nested_mapping(basin.get("resource_profile")).get("shud_threads")
            or _nested_mapping(basin.get("runtime")).get("threads")
            or 1
        ),
        "mode": "native_shud_project",
        "output_river": output_river,
    }
    identity = {
        "schema_version": PRODUCTION_CONTRACT_SCHEMA_VERSION,
        "contract_id": PRODUCTION_CONTRACT_ID,
        "candidate_id": candidate_id,
        "run_id": run_id,
        "hydro_run_id": str(basin.get("hydro_run_id") or run_id),
        "published_manifest_id": str(basin.get("published_manifest_id") or f"manifest_{run_id}"),
        "canonical_product_id": str(
            basin.get("canonical_product_id") or f"canon_{source_id.lower()}_{compact_cycle}"
        ),
        "forcing_version_id": forcing_version_id,
        "source": source_id,
        "source_id": source_id,
        "cycle_id": cycle_id,
        "cycle_time": _format_time(cycle_time),
        "scenario_id": scenario_id,
        "model_id": model_id,
        "basin_id": basin.get("basin_id"),
        "basin_version_id": basin_version_id,
        "river_network_version_id": river_network_version_id,
        "model_package_uri": model_package_uri,
        "model_package_manifest_uri": model_package_manifest_uri(basin, model_package_uri),
        "model_package_checksum": basin.get("model_package_checksum") or basin.get("package_checksum"),
        "segment_count": int(output_river.get("segment_count") or 0),
        "forecast_horizon_hours": forecast_horizon_hours,
        "start_time": _format_time(start_time),
        "end_time": _format_time(end_time),
    }
    forcing = _runtime_forcing_metadata(
        {
            "forcing_version_id": forcing_version_id,
            "forcing_uri": forcing_uri,
            "forcing_package_uri": forcing_uri,
            "forcing_package_manifest_uri": basin.get("forcing_package_manifest_uri")
            or _nested_mapping(basin.get("resource_profile")).get("forcing_package_manifest_uri"),
            "forcing_manifest_checksum": basin.get("forcing_manifest_checksum")
            or _nested_mapping(basin.get("resource_profile")).get("forcing_manifest_checksum"),
            "station_metadata": station_metadata,
            "station_count": station_metadata.get("station_count"),
            "station_ids": station_metadata.get("station_ids", []),
            "quality_flag": station_metadata.get("quality_flag"),
        }
    )
    if station_metadata.get("shud_station"):
        forcing["shud_station"] = station_metadata["shud_station"]
    outputs = {
        "run_manifest_uri": run_manifest_uri,
        "output_uri": output_uri,
        "log_uri": log_uri,
        "reuse_policy": "deterministic_run_uri",
        "output_segment_count": int(output_river.get("segment_count") or 0),
        "gis_segment_count": _optional_int(basin.get("segment_count")),
    }
    return ModelRunAssembly(
        identity=identity,
        forcing=forcing,
        runtime=runtime,
        outputs=outputs,
        frequency=frequency,
        display=display,
        quality_states=quality_states,
        residual_blockers=tuple(blockers),
    )
