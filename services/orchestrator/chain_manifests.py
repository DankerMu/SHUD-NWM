from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from packages.common.manifest_index import ManifestValidationError, serialize_manifest_index
from packages.common.object_store import LocalObjectStore
from packages.common.safe_fs import SafeFilesystemError
from packages.common.source_identity import normalize_source_id
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

ANALYSIS_SCENARIO_ID = "analysis_true_field"
DEFAULT_ERA5_REANALYSIS_LATENCY_MINUTES = 5 * 24 * 60
FORCING_CAUSALITY_CAUSAL = "causal"
FORCING_CAUSALITY_DELAYED_REANALYSIS = "delayed_reanalysis"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")

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
        "forcing": {
            "forcing_version_id": context.forcing_version_id,
            "forcing_uri": context.forcing_package_uri,
        },
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
    checkpoint_hours = forecast_state_checkpoint_hours(context.forecast_horizon_hours)
    if checkpoint_hours:
        manifest["runtime"]["state_checkpoint_hours"] = checkpoint_hours
        manifest["runtime"]["update_ic_step_minutes"] = min(checkpoint_hours) * 60
    return manifest


def build_analysis_run_manifest(context: AnalysisRunContext) -> dict[str, Any]:
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
        "forcing": {
            "forcing_version_id": context.forcing_version_id,
            "forcing_uri": context.forcing_package_uri,
        },
        "forcing_causality": dict(
            context.forcing_causality if context.forcing_causality is not None else _analysis_forcing_causality()
        ),
        "runtime": {
            "output_interval_minutes": 60,
            "init_mode": 3 if context.init_state_id else 1,
            "update_ic_step_minutes": (
                context.update_ic_step_minutes
                if context.update_ic_step_minutes is not None
                else _analysis_update_ic_step_minutes(context.start_time, context.end_time)
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
        "project_name": _project_name_for_basin(basin, fallback=model_id),
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
    forcing = {
        "forcing_version_id": forcing_version_id,
        "forcing_uri": forcing_uri,
        "forcing_package_uri": forcing_uri,
        "station_metadata": station_metadata,
        "station_count": station_metadata.get("station_count"),
        "station_ids": station_metadata.get("station_ids", []),
        "quality_flag": station_metadata.get("quality_flag"),
    }
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


def _default_forcing_uri(
    source_id: str,
    compact_cycle: str,
    basin_version_id: str,
    model_id: str,
    object_store: LocalObjectStore,
) -> str:
    return _directory_uri(object_store, f"forcing/{source_id.lower()}/{compact_cycle}/{basin_version_id}/{model_id}/")


def _directory_uri(object_store: LocalObjectStore, key: str) -> str:
    return object_store.uri_for_key(key).rstrip("/") + "/"


def _preserve_directory_uri(value: str | None, object_store: LocalObjectStore, fallback_key: str) -> str:
    if value is not None and _has_uri_scheme(value):
        return value.rstrip("/") + "/"
    return _directory_uri(object_store, fallback_key)


def _has_uri_scheme(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    match = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", candidate)
    return match is not None


def _model_package_manifest_uri(basin: Mapping[str, Any], model_package_uri: str) -> str:
    resource_profile = _nested_mapping(basin.get("resource_profile"))
    explicit = (
        basin.get("model_package_manifest_uri")
        or basin.get("manifest_uri")
        or resource_profile.get("manifest_uri")
    )
    if explicit not in (None, ""):
        return str(explicit)
    package_uri = model_package_uri.rstrip("/")
    if package_uri.endswith("/package"):
        return f"{package_uri.removesuffix('/package')}/manifest.json"
    return f"{package_uri}/manifest.json"


def _station_metadata_for_basin(basin: Mapping[str, Any]) -> dict[str, Any]:
    resource_profile = _nested_mapping(basin.get("resource_profile"))
    explicit = _nested_mapping(
        basin.get("forcing_station_metadata")
        or basin.get("station_metadata")
        or resource_profile.get("forcing_station_metadata")
    )
    if explicit:
        station_ids = [str(item) for item in explicit.get("station_ids") or []]
        station_count = _optional_int(explicit.get("station_count"))
        if station_count is None:
            station_count = len(station_ids)
        state = "ready" if station_count > 0 else "unavailable"
        return {
            "schema_version": "nhms.forcing_station_metadata.v1",
            "state": str(explicit.get("state") or state),
            "station_count": station_count,
            "station_ids": station_ids,
            "source": str(explicit.get("source") or "registry_package_metadata"),
            "shud_station": explicit.get("shud_station"),
            "quality_flag": str(
                explicit.get("quality_flag") or ("ok" if station_count > 0 else "station_forcing_unavailable")
            ),
        }
    station_count = _optional_int(basin.get("station_count"))
    raw_station_ids = basin.get("station_ids")
    station_ids = (
        [str(item) for item in raw_station_ids or []]
        if isinstance(raw_station_ids, Sequence) and not isinstance(raw_station_ids, str | bytes)
        else []
    )
    if station_count is None and station_ids:
        station_count = len(station_ids)
    if station_count is None:
        station_count = 0
    state = "ready" if station_count > 0 else "unavailable"
    return {
        "schema_version": "nhms.forcing_station_metadata.v1",
        "state": state,
        "station_count": station_count,
        "station_ids": station_ids,
        "source": "registry_package_metadata",
        "quality_flag": "ok" if station_count > 0 else "station_forcing_unavailable",
    }


def _output_river_contract(basin: Mapping[str, Any]) -> dict[str, Any]:
    explicit = _nested_mapping(basin.get("output_river") or basin.get("shud_output_river"))
    resource_profile = _nested_mapping(basin.get("resource_profile"))
    gis_segment_count = _optional_int(basin.get("segment_count"))
    profile_output_river = _nested_mapping(resource_profile.get("output_river"))
    output_segment_count = _first_optional_int(
        basin.get("output_segment_count"),
        basin.get("shud_output_segment_count"),
        basin.get("shud_output_river_count"),
        resource_profile.get("output_segment_count"),
        resource_profile.get("shud_output_segment_count"),
        resource_profile.get("shud_output_river_count"),
        profile_output_river.get("output_segment_count"),
        profile_output_river.get("segment_count"),
    )
    if explicit:
        state = str(explicit.get("state") or "ready")
        segment_ids = [str(item) for item in explicit.get("river_segment_ids") or explicit.get("segment_ids") or []]
        explicit_segment_count = _first_optional_int(
            explicit.get("output_segment_count"),
            explicit.get("segment_count"),
        )
        resolved_segment_count = _first_optional_int(
            explicit_segment_count,
            output_segment_count,
            len(segment_ids) if segment_ids else None,
            gis_segment_count,
        )
        if resolved_segment_count is None:
            state = "unavailable"
            resolved_segment_count = 0
        return {
            "state": state,
            "river_network_version_id": str(basin["river_network_version_id"]),
            "segment_count": resolved_segment_count,
            "output_segment_count": resolved_segment_count,
            "gis_segment_count": gis_segment_count,
            "river_segment_ids": segment_ids,
            "identity_source": str(explicit.get("identity_source") or "registry_package_metadata"),
            "quality_flag": str(
                explicit.get("quality_flag") or ("ok" if state == "ready" else "output_river_unavailable")
            ),
        }
    if output_segment_count is None and gis_segment_count is None:
        return {
            "state": "unavailable",
            "river_network_version_id": str(basin["river_network_version_id"]),
            "segment_count": 0,
            "output_segment_count": 0,
            "gis_segment_count": None,
            "river_segment_ids": [],
            "identity_source": "registry_package_metadata",
            "quality_flag": "output_river_unavailable",
        }
    resolved_segment_count = output_segment_count if output_segment_count is not None else gis_segment_count
    return {
        "state": "ready" if resolved_segment_count > 0 else "unavailable",
        "river_network_version_id": str(basin["river_network_version_id"]),
        "segment_count": resolved_segment_count,
        "output_segment_count": resolved_segment_count,
        "gis_segment_count": gis_segment_count,
        "river_segment_ids": [],
        "identity_source": (
            "resource_profile.output_segment_count" if output_segment_count is not None else "registry_package_metadata"
        ),
        "quality_flag": "ok" if resolved_segment_count > 0 else "output_river_unavailable",
    }


def _frequency_contract(basin: Mapping[str, Any]) -> dict[str, Any]:
    capabilities = _nested_mapping(basin.get("frequency_capabilities"))
    has_curves = _tri_state(
        basin.get("frequency_curves_available"),
        capabilities.get("curves_available"),
        capabilities.get("return_periods"),
    )
    has_thresholds = _tri_state(
        basin.get("warning_thresholds_available"),
        capabilities.get("warning_thresholds_available"),
        capabilities.get("warning_thresholds"),
    )
    unavailable: list[str] = []
    if has_curves is False:
        unavailable.append("frequency_curves")
    if has_thresholds is False:
        unavailable.append("warning_thresholds")
    state = "ready" if not unavailable else "unavailable"
    return {
        "state": state,
        "return_periods_enabled": bool(capabilities.get("return_periods", True)),
        "frequency_curves": "available" if has_curves is not False else "unavailable",
        "warning_thresholds": "available" if has_thresholds is not False else "unavailable",
        "quality_flag": "ok" if state == "ready" else "frequency_inputs_unavailable",
        "unavailable_products": unavailable,
    }


def _display_contract(basin: Mapping[str, Any], *, output_uri: str) -> dict[str, Any]:
    capabilities = _nested_mapping(basin.get("display_capabilities"))
    optional_weather = _tri_state(
        basin.get("optional_weather_available"),
        capabilities.get("optional_weather_available"),
        capabilities.get("weather_products"),
    )
    tiles_enabled = bool(capabilities.get("tiles", True))
    unavailable = []
    if optional_weather is False:
        unavailable.append("optional_weather_products")
    return {
        "state": "ready" if tiles_enabled else "unavailable",
        "tiles_enabled": tiles_enabled,
        "output_uri": output_uri,
        "optional_weather_products": "available" if optional_weather is not False else "unavailable",
        "quality_flag": "ok" if not unavailable and tiles_enabled else "display_inputs_unavailable",
        "unavailable_products": unavailable,
    }


def _assembly_quality_states(
    basin: Mapping[str, Any],
    *,
    station_metadata: Mapping[str, Any],
    output_river: Mapping[str, Any],
    frequency: Mapping[str, Any],
    display: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    states = {
        "station_forcing": {
            "state": station_metadata.get("state"),
            "quality_flag": station_metadata.get("quality_flag"),
        },
        "frequency": {
            "state": frequency.get("state"),
            "quality_flag": frequency.get("quality_flag"),
            "unavailable_products": list(frequency.get("unavailable_products") or []),
        },
        "display": {
            "state": display.get("state"),
            "quality_flag": display.get("quality_flag"),
            "unavailable_products": list(display.get("unavailable_products") or []),
        },
    }
    states["output_river"] = {
        "state": output_river.get("state"),
        "quality_flag": output_river.get("quality_flag"),
        "segment_count": output_river.get("segment_count"),
    }
    blockers: list[dict[str, Any]] = []
    if station_metadata.get("state") != "ready":
        blockers.append(
            {
                "code": "STATION_FORCING_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": station_metadata.get("quality_flag"),
                "residual_risk": "No forcing station metadata is available for this model package.",
            }
        )
    if output_river.get("state") != "ready":
        blockers.append(
            {
                "code": "OUTPUT_RIVER_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": output_river.get("quality_flag"),
                "residual_risk": (
                    "SHUD output-river segment metadata is unavailable; segment_count was not fabricated."
                ),
            }
        )
    for product in frequency.get("unavailable_products") or []:
        blockers.append(
            {
                "code": str(product).upper() + "_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": frequency.get("quality_flag"),
                "residual_risk": (
                    f"{product} is unavailable; downstream products must carry null values or quality flags."
                ),
            }
        )
    for product in display.get("unavailable_products") or []:
        blockers.append(
            {
                "code": str(product).upper() + "_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": display.get("quality_flag"),
                "residual_risk": f"{product} is unavailable; durable model outputs remain reusable.",
            }
        )
    for item in basin.get("residual_blockers") or ():
        if isinstance(item, Mapping):
            blockers.append(dict(item))
    return states, blockers


def _model_run_stage_evidence(stage: str, entry: Mapping[str, Any], *, cycle_id: str) -> dict[str, Any]:
    assembly = _assembly_from_entry(entry)
    identity = dict(assembly.get("identity") or {})
    return {
        "stage": stage,
        "production_stage": production_stage_for(stage),
        "cycle_id": cycle_id,
        "candidate_id": identity.get("candidate_id") or entry.get("candidate_id"),
        "run_id": identity.get("run_id") or entry.get("run_id"),
        "hydro_run_id": identity.get("hydro_run_id") or entry.get("hydro_run_id") or entry.get("run_id"),
        "model_id": identity.get("model_id") or entry.get("model_id"),
        "source": identity.get("source") or identity.get("source_id") or entry.get("source_id"),
        "source_id": identity.get("source_id") or entry.get("source_id"),
        "cycle_time": identity.get("cycle_time") or entry.get("cycle_time"),
        "scenario_id": identity.get("scenario_id") or entry.get("scenario_id"),
        "canonical_product_id": identity.get("canonical_product_id") or entry.get("canonical_product_id"),
        "forcing_version_id": identity.get("forcing_version_id") or entry.get("forcing_version_id"),
        "published_manifest_id": identity.get("published_manifest_id") or entry.get("published_manifest_id"),
        "model_package_uri": identity.get("model_package_uri") or entry.get("model_package_uri"),
        "basin_id": identity.get("basin_id") or entry.get("basin_id"),
        "basin_version_id": identity.get("basin_version_id") or entry.get("basin_version_id"),
        "river_network_version_id": identity.get("river_network_version_id") or entry.get("river_network_version_id"),
        "output_uri": _nested_mapping(assembly.get("outputs")).get("output_uri") or entry.get("output_uri"),
        "quality_states": dict(assembly.get("quality_states") or entry.get("quality_states") or {}),
        "residual_blockers": list(assembly.get("residual_blockers") or entry.get("residual_blockers") or []),
    }


def _frequency_quality_state(
    entry: Mapping[str, Any],
    *,
    cycle_id: str,
    model_run_stage_evidence: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    model_run_stage_evidence = model_run_stage_evidence or _model_run_stage_evidence
    evidence = model_run_stage_evidence("frequency", entry, cycle_id=cycle_id)
    frequency_state = _nested_mapping(evidence.get("quality_states")).get("frequency") or {}
    return {
        **evidence,
        "state": _nested_mapping(frequency_state).get("state", "ready"),
        "quality_flag": _nested_mapping(frequency_state).get("quality_flag", "ok"),
        "unavailable_products": list(_nested_mapping(frequency_state).get("unavailable_products") or []),
    }


def _publish_quality_state(
    entry: Mapping[str, Any],
    *,
    cycle_id: str,
    model_run_stage_evidence: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    model_run_stage_evidence = model_run_stage_evidence or _model_run_stage_evidence
    evidence = model_run_stage_evidence("publish", entry, cycle_id=cycle_id)
    display_state = _nested_mapping(evidence.get("quality_states")).get("display") or {}
    return {
        **evidence,
        "state": _nested_mapping(display_state).get("state", "ready"),
        "quality_flag": _nested_mapping(display_state).get("quality_flag", "ok"),
        "unavailable_products": list(_nested_mapping(display_state).get("unavailable_products") or []),
    }


def _cycle_residual_blockers(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for entry in entries:
        run_id = str(entry.get("run_id") or "")
        for blocker in entry.get("residual_blockers") or []:
            if isinstance(blocker, Mapping):
                blockers.append({"run_id": run_id, **dict(blocker)})
        assembly = _assembly_from_entry(entry)
        for blocker in assembly.get("residual_blockers") or []:
            if isinstance(blocker, Mapping):
                candidate = {"run_id": run_id, **dict(blocker)}
                if candidate not in blockers:
                    blockers.append(candidate)
    return blockers


def _assembly_from_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    assembly = entry.get("model_run_assembly")
    return dict(assembly) if isinstance(assembly, Mapping) else {}


def _assembly_payload_from_runtime_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "identity": dict(_nested_mapping(manifest.get("identity"))),
        "forcing": dict(_nested_mapping(manifest.get("forcing"))),
        "runtime": dict(_nested_mapping(manifest.get("runtime"))),
        "outputs": dict(_nested_mapping(manifest.get("outputs"))),
        "frequency": dict(_nested_mapping(manifest.get("frequency"))),
        "display": dict(_nested_mapping(manifest.get("display"))),
        "quality_states": dict(_nested_mapping(manifest.get("quality_states"))),
        "residual_blockers": list(manifest.get("residual_blockers") or []),
    }


def _nested_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _tri_state(*values: Any) -> bool | None:
    for value in values:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "available", "ready", "yes", "1"}:
                return True
            if normalized in {"false", "unavailable", "missing", "blocked", "no", "0"}:
                return False
    return None


def _safe_project_name(value: str) -> str:
    candidate = value.strip() or "shud"
    if _SAFE_ID_RE.fullmatch(candidate):
        return candidate
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate).strip("._-") or "shud"


def _project_name_for_basin(basin: Mapping[str, Any], *, fallback: str) -> str:
    resource_profile = _nested_mapping(basin.get("resource_profile"))
    runtime = _nested_mapping(basin.get("runtime"))
    for value in (
        basin.get("project_name"),
        basin.get("shud_input_name"),
        resource_profile.get("project_name"),
        resource_profile.get("shud_input_name"),
        runtime.get("project_name"),
        runtime.get("shud_input_name"),
        fallback,
    ):
        if value not in (None, ""):
            return _safe_project_name(str(value))
    return _safe_project_name(fallback)


def _cycle_payload_model_id(context: CycleOrchestrationContext) -> str:
    if context.active_basins:
        return str(context.active_basins[0].get("model_id") or "cycle")
    return "cycle"


def _basin_key(basin: Mapping[str, Any]) -> tuple[str, str]:
    return (str(basin.get("model_id") or ""), str(basin.get("basin_id") or basin.get("model_id") or ""))


def _basin_identifier(basin: Mapping[str, Any]) -> str:
    return str(basin.get("basin_id") or basin.get("model_id") or "")


def _nested_value(value: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = value
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _forecast_state_checkpoint_hours(forecast_horizon_hours: Any) -> list[int]:
    try:
        horizon = int(forecast_horizon_hours)
    except (TypeError, ValueError):
        return []
    return [hour for hour in (6, 12) if hour <= horizon]


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_optional_int(*values: Any) -> int | None:
    for value in values:
        coerced = _optional_int(value)
        if coerced is not None:
            return coerced
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
