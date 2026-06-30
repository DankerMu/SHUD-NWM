from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from packages.common.source_identity import normalize_source_id
from services.orchestrator.chain_runtime_utils import _format_time
from services.orchestrator.chain_stages import STAGES, terminal_stage_names
from services.orchestrator.chain_types import OrchestratorError
from workers.data_adapters.base import format_cycle_time, parse_cycle_time


def scenario_for_source(source_id: str) -> str:
    try:
        normalized_source_id = normalize_source_id(source_id)
    except ValueError:
        normalized_source_id = source_id
    if normalized_source_id == "gfs":
        return "forecast_gfs_deterministic"
    if normalized_source_id == "IFS":
        return "forecast_ifs_deterministic"
    return f"forecast_{source_id.lower()}_deterministic"


class PipelineAlreadyActiveError(OrchestratorError):
    def __init__(self, source_id: str, cycle_time: datetime, model_id: str) -> None:
        super().__init__(
            "PIPELINE_ALREADY_ACTIVE",
            f"An active pipeline already exists for {source_id} {format_cycle_time(cycle_time)} {model_id}.",
            {"source_id": source_id, "cycle_time": _format_time(cycle_time), "model_id": model_id},
        )


class AnalysisPipelineAlreadyActiveError(OrchestratorError):
    def __init__(self, model_id: str, start_time: datetime, end_time: datetime) -> None:
        super().__init__(
            "ANALYSIS_PIPELINE_ALREADY_ACTIVE",
            f"An active analysis pipeline already overlaps {model_id} "
            f"{_format_time(start_time)}..{_format_time(end_time)}.",
            {"model_id": model_id, "start_time": _format_time(start_time), "end_time": _format_time(end_time)},
        )


class SlurmClientError(OrchestratorError):
    pass


class SlurmAccountingEvidenceGap(OrchestratorError):
    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__("SLURM_ACCOUNTING_EVIDENCE_GAP", message, details)


@dataclass(frozen=True)
class OrchestratorConfig:
    workspace_root: Path | str
    object_store_root: Path | str
    object_store_prefix: str = ""
    slurm_gateway_url: str = "http://localhost:8000"
    templates_dir: Path | str | None = None
    poll_interval_seconds: float = 30.0
    job_timeout_seconds: float = 3600.0
    source_id: str = "gfs"
    forecast_horizon_hours: int = 168
    scenario_id: str | None = None
    scenario_id_explicit: bool = field(init=False, default=False, repr=False, compare=False)
    era5_area: str = "55,70,15,140"
    state_soft_stale_threshold_days: int = 7
    state_hard_stale_threshold_days: int = 30
    require_forecast_warm_start: bool = False
    forecast_warm_start_required_from: datetime | None = None
    terminal_stage: str | None = None
    slurm_job_type_templates: Mapping[str, str] = field(default_factory=dict)
    slurm_env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_root", Path(self.workspace_root).expanduser().resolve())
        object.__setattr__(self, "object_store_root", Path(self.object_store_root).expanduser().resolve())
        object.__setattr__(self, "source_id", normalize_source_id(self.source_id))
        if self.forecast_warm_start_required_from is not None:
            object.__setattr__(
                self,
                "forecast_warm_start_required_from",
                parse_cycle_time(self.forecast_warm_start_required_from),
            )
        object.__setattr__(self, "poll_interval_seconds", max(float(self.poll_interval_seconds), 1.0))
        object.__setattr__(self, "scenario_id_explicit", self.scenario_id is not None)
        if self.scenario_id is None:
            object.__setattr__(self, "scenario_id", scenario_for_source(self.source_id))
        object.__setattr__(self, "terminal_stage", _normalize_terminal_stage(self.terminal_stage))
        if self.templates_dir is None:
            repo_root = Path(__file__).resolve().parents[2]
            object.__setattr__(self, "templates_dir", repo_root / "infra" / "sbatch")
        else:
            object.__setattr__(self, "templates_dir", Path(self.templates_dir).expanduser().resolve())
        object.__setattr__(
            self,
            "slurm_job_type_templates",
            {str(key): str(value) for key, value in dict(self.slurm_job_type_templates).items()},
        )
        object.__setattr__(self, "slurm_env", {str(key): str(value) for key, value in dict(self.slurm_env).items()})

    @classmethod
    def from_env(cls) -> OrchestratorConfig:
        workspace_root = os.getenv("WORKSPACE_ROOT", ".")
        return cls(
            workspace_root=workspace_root,
            object_store_root=os.getenv("OBJECT_STORE_ROOT", workspace_root),
            object_store_prefix=os.getenv("OBJECT_STORE_PREFIX", ""),
            slurm_gateway_url=os.getenv("SLURM_GATEWAY_URL", "http://localhost:8000"),
            poll_interval_seconds=float(os.getenv("ORCHESTRATOR_POLL_INTERVAL_SECONDS", "30")),
            job_timeout_seconds=float(os.getenv("ORCHESTRATOR_JOB_TIMEOUT_SECONDS", "3600")),
            source_id=os.getenv("FORECAST_SOURCE_ID", "gfs"),
            forecast_horizon_hours=int(os.getenv("FORECAST_HORIZON_HOURS", "168")),
            era5_area=os.getenv("ERA5_AREA", "55,70,15,140"),
            state_soft_stale_threshold_days=int(os.getenv("STATE_SOFT_STALE_THRESHOLD_DAYS", "7")),
            state_hard_stale_threshold_days=int(os.getenv("STATE_HARD_STALE_THRESHOLD_DAYS", "30")),
            require_forecast_warm_start=_env_flag("NHMS_REQUIRE_FORECAST_WARM_START", default=False),
            forecast_warm_start_required_from=_env_cycle_time("NHMS_FORECAST_WARM_START_REQUIRED_FROM"),
            terminal_stage=os.getenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE") or None,
        )

    def strict_forecast_warm_start_required_for(self, cycle_time: datetime) -> bool:
        if not self.require_forecast_warm_start:
            return False
        if self.forecast_warm_start_required_from is None:
            return True
        return parse_cycle_time(cycle_time) >= self.forecast_warm_start_required_from

    def forecast_warm_start_bootstrap_cycle(self, cycle_time: datetime) -> bool:
        return (
            self.require_forecast_warm_start
            and self.forecast_warm_start_required_from is not None
            and parse_cycle_time(cycle_time) < self.forecast_warm_start_required_from
        )


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value.")


def _env_cycle_time(name: str) -> datetime | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return parse_cycle_time(value)


def _normalize_terminal_stage(value: str | None) -> str | None:
    if value is None:
        return None
    terminal_stage = value.strip()
    if not terminal_stage:
        return None
    known_stages = set(terminal_stage_names(STAGES))
    if terminal_stage not in known_stages:
        known = ", ".join(terminal_stage_names(STAGES))
        raise ValueError(f"NHMS_ORCHESTRATOR_TERMINAL_STAGE must be one of: {known}.")
    return terminal_stage
