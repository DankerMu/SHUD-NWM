from __future__ import annotations

import json
import os
import re
import stat
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from errno import EEXIST, EISDIR, ELOOP, ENOTDIR
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from packages.common.model_registry import PsycopgModelRegistryStore
from packages.common.redaction import redact_payload
from packages.common.slurm_env import is_sensitive_slurm_env_key, secret_bearing_url_reason
from packages.common.source_identity import normalize_source_id
from services.orchestrator.chain import ForecastOrchestrator, OrchestratorConfig, PipelineResult, scenario_for_source
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES
from workers.data_adapters.base import CycleDiscovery, cycle_id_for, format_cycle_time

DEFAULT_PRODUCTION_SOURCES = ("gfs", "IFS")
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_CYCLE_LAG_HOURS = 0
DEFAULT_MAX_CYCLES_PER_SOURCE = 1
DEFAULT_LOCK_TTL_SECONDS = 3600
MAX_LOOKBACK_HOURS = 168
MAX_SOURCES = 4
MAX_CYCLES_PER_SOURCE = 16
MAX_DISCOVERED_MODELS = 1000
MAX_DISCOVERED_CYCLES = 10000
MAX_CANDIDATES = 10000
MAX_REGISTRY_PAGES = 20
MAX_EVIDENCE_BYTES = 5_000_000
MAX_LOCK_PAYLOAD_BYTES = 16_384
MAX_CONTINUOUS_JSON_PASSES = 100
MAX_SLURM_ENV_VALUE_LENGTH = 1024
LOCK_OWNER = "production_scheduler"
LOCK_SCHEMA_VERSION = 1
SLURM_ARRAY_STAGE_NAMES = {"forcing", "forecast", "parse", "frequency"}
SAFE_SLURM_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SAFE_SLURM_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:=,@+\-]*$")
SHELL_META_RE = re.compile(r"[;|&$`<>\n\r]")
LOCALHOST_NAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class SchedulerResourceLimitError(ValueError):
    def __init__(self, reason: str, details: Mapping[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details)


class UnsafeSchedulerLockError(OSError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SchedulerEvidenceWriteError(OSError):
    def __init__(self, reason: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details or {})


class ModelRegistryReader(Protocol):
    def list_models(
        self,
        *,
        basin_version_id: str | None,
        active: bool | None,
        limit: int,
        offset: int,
    ) -> Mapping[str, Any]:
        raise NotImplementedError

    def get_model(self, model_id: str) -> Mapping[str, Any]:
        raise NotImplementedError


class CycleDiscoveryAdapter(Protocol):
    def discover_cycles(
        self,
        cycle_date: str | date | datetime,
        end_date: str | date | datetime | None = None,
    ) -> list[CycleDiscovery]:
        raise NotImplementedError


class ActiveCandidateRepository(Protocol):
    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        raise NotImplementedError

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        raise NotImplementedError

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        raise NotImplementedError

    def active_slurm_jobs(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
    ) -> Sequence[Mapping[str, Any]]:
        raise NotImplementedError


class ProductionOrchestratorFactory(Protocol):
    def __call__(self, source_id: str) -> ForecastOrchestrator:
        raise NotImplementedError


@dataclass(frozen=True)
class ProductionSchedulerConfig:
    workspace_root: Path | str = field(default_factory=lambda: os.getenv("WORKSPACE_ROOT", ".nhms-workspace"))
    object_store_root: Path | str | None = field(default_factory=lambda: os.getenv("OBJECT_STORE_ROOT"))
    log_root: Path | str | None = field(
        default_factory=lambda: os.getenv("SLURM_SHARED_LOG_ROOT") or os.getenv("LOG_ROOT")
    )
    runtime_root: Path | str | None = field(
        default_factory=lambda: os.getenv("NHMS_RUNTIME_ROOT")
        or os.getenv("RUN_WORKSPACE_ROOT")
        or os.getenv("SHUD_RUNTIME_ROOT")
    )
    database_url: str | None = field(
        default_factory=lambda: os.getenv("DATABASE_URL")
    )
    slurm_execution_enabled: bool = field(
        default_factory=lambda: _env_flag("NHMS_PRODUCTION_SLURM_ENABLED")
        or _env_flag("SLURM_EXECUTION_ENABLED")
    )
    allowed_storage_roots: tuple[Path | str, ...] = ()
    slurm_job_type_templates: Mapping[str, str] | None = None
    slurm_env: Mapping[str, str] = field(default_factory=dict)
    cancel_active_slurm: bool = False
    sources: tuple[str, ...] = DEFAULT_PRODUCTION_SOURCES
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS
    cycle_lag_hours: int = DEFAULT_CYCLE_LAG_HOURS
    max_cycles_per_source: int = DEFAULT_MAX_CYCLES_PER_SOURCE
    model_ids: tuple[str, ...] = ()
    basin_ids: tuple[str, ...] = ()
    dry_run: bool = True
    continuous: bool = False
    interval_seconds: float = 300.0
    lock_path: Path | str | None = None
    evidence_dir: Path | str | None = None
    lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS
    now: datetime | None = None
    source_exclusions: tuple[dict[str, Any], ...] = field(init=False, default=())

    def __post_init__(self) -> None:
        workspace_root = Path(self.workspace_root).expanduser().resolve()
        object.__setattr__(self, "workspace_root", workspace_root)
        object.__setattr__(self, "object_store_root", _optional_config_path(self.object_store_root))
        object.__setattr__(self, "log_root", _optional_config_path(self.log_root))
        object.__setattr__(self, "runtime_root", _optional_config_path(self.runtime_root))
        object.__setattr__(self, "database_url", str(self.database_url).strip() if self.database_url else None)
        allowed_roots = tuple(_optional_config_path(root) for root in self.allowed_storage_roots if root)
        object.__setattr__(self, "allowed_storage_roots", allowed_roots)
        templates = dict(self.slurm_job_type_templates or DEFAULT_JOB_TYPE_TEMPLATES)
        object.__setattr__(self, "slurm_job_type_templates", templates)
        object.__setattr__(self, "slurm_env", {str(key): str(value) for key, value in dict(self.slurm_env).items()})
        if len(self.sources) > MAX_SOURCES:
            raise ValueError(f"production scheduler source count exceeds limit {MAX_SOURCES}")
        sources, source_exclusions = _normalize_sources(self.sources)
        if len(sources) > MAX_SOURCES:
            raise ValueError(f"production scheduler source count exceeds limit {MAX_SOURCES}")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "source_exclusions", tuple(source_exclusions))
        lookback_hours = max(int(self.lookback_hours), 0)
        if lookback_hours > MAX_LOOKBACK_HOURS:
            raise ValueError(f"production scheduler lookback_hours exceeds limit {MAX_LOOKBACK_HOURS}")
        object.__setattr__(self, "lookback_hours", lookback_hours)
        object.__setattr__(self, "cycle_lag_hours", max(int(self.cycle_lag_hours), 0))
        max_cycles_per_source = max(int(self.max_cycles_per_source), 1)
        if max_cycles_per_source > MAX_CYCLES_PER_SOURCE:
            raise ValueError(f"production scheduler max_cycles_per_source exceeds limit {MAX_CYCLES_PER_SOURCE}")
        object.__setattr__(self, "max_cycles_per_source", max_cycles_per_source)
        object.__setattr__(self, "model_ids", tuple(str(model_id) for model_id in self.model_ids if model_id))
        object.__setattr__(self, "basin_ids", tuple(str(basin_id) for basin_id in self.basin_ids if basin_id))
        object.__setattr__(self, "interval_seconds", max(float(self.interval_seconds), 1.0))
        object.__setattr__(self, "lock_ttl_seconds", max(int(self.lock_ttl_seconds), 1))
        if self.lock_path is None:
            lock_path = _confined_path(
                workspace_root / "scheduler" / "production-scheduler.lock",
                workspace_root,
                "lock_path",
            )
            object.__setattr__(self, "lock_path", lock_path)
        else:
            lock_path = _confined_path(self.lock_path, workspace_root, "lock_path")
            _require_under_workspace(lock_path, workspace_root, "lock_path")
            object.__setattr__(self, "lock_path", lock_path)
        if self.evidence_dir is None:
            evidence_dir = _confined_path(
                workspace_root / "scheduler" / "evidence",
                workspace_root,
                "evidence_dir",
            )
            _require_safe_directory_final_component(evidence_dir, workspace_root, "evidence_dir")
            object.__setattr__(self, "evidence_dir", evidence_dir)
        else:
            evidence_dir = _confined_path(self.evidence_dir, workspace_root, "evidence_dir")
            _require_under_workspace(evidence_dir, workspace_root, "evidence_dir")
            _require_safe_directory_final_component(evidence_dir, workspace_root, "evidence_dir")
            object.__setattr__(self, "evidence_dir", evidence_dir)
        if self.now is not None:
            object.__setattr__(self, "now", _ensure_utc(self.now))


@dataclass(frozen=True)
class SchedulerPassResult:
    pass_id: str
    status: str
    evidence: dict[str, Any]
    artifact_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.evidence)
        if self.artifact_path is not None:
            payload.setdefault("artifact_path", str(self.artifact_path))
        return payload


@dataclass(frozen=True)
class RegisteredSchedulerModel:
    model_id: str
    basin_id: str
    basin_version_id: str
    river_network_version_id: str
    segment_count: int | None
    model_package_uri: str
    shud_code_version: str
    resource_profile: Mapping[str, Any]
    resource_profile_summary: Mapping[str, Any]
    display_capabilities: Mapping[str, Any]
    frequency_capabilities: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "basin_id": self.basin_id,
            "basin_version_id": self.basin_version_id,
            "river_network_version_id": self.river_network_version_id,
            "segment_count": self.segment_count,
            "model_package_uri": self.model_package_uri,
            "shud_code_version": self.shud_code_version,
            "resource_profile": dict(self.resource_profile_summary),
            "display_capabilities": dict(self.display_capabilities),
            "frequency_capabilities": dict(self.frequency_capabilities),
        }


@dataclass(frozen=True)
class SchedulerCandidate:
    candidate_id: str
    source_id: str
    cycle_id: str
    cycle_time_utc: datetime
    model_id: str
    basin_id: str
    basin_version_id: str
    river_network_version_id: str
    segment_count: int | None
    model_package_uri: str
    resource_profile: Mapping[str, Any]
    display_capabilities: Mapping[str, Any]
    frequency_capabilities: Mapping[str, Any]
    horizon: Mapping[str, Any]
    scenario_id: str
    run_id: str
    forcing_version_id: str
    status: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_id": self.source_id,
            "cycle_id": self.cycle_id,
            "cycle_time_utc": _format_utc(self.cycle_time_utc),
            "model_id": self.model_id,
            "basin_id": self.basin_id,
            "basin_version_id": self.basin_version_id,
            "river_network_version_id": self.river_network_version_id,
            "segment_count": self.segment_count,
            "model_package_uri": self.model_package_uri,
            "resource_profile": dict(self.resource_profile),
            "display_capabilities": dict(self.display_capabilities),
            "frequency_capabilities": dict(self.frequency_capabilities),
            "horizon": dict(self.horizon),
            "scenario_id": self.scenario_id,
            "run_id": self.run_id,
            "forcing_version_id": self.forcing_version_id,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SchedulerSourceCycle:
    discovery: CycleDiscovery
    horizon: Mapping[str, Any]


class ProductionScheduler:
    def __init__(
        self,
        config: ProductionSchedulerConfig | None = None,
        *,
        registry: ModelRegistryReader | None = None,
        adapters: Mapping[str, CycleDiscoveryAdapter] | None = None,
        active_repository: ActiveCandidateRepository | None = None,
        orchestrator_factory: ProductionOrchestratorFactory | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config or ProductionSchedulerConfig()
        self.registry = registry or PsycopgModelRegistryStore.from_env()
        self.adapters = dict(adapters or _default_adapters())
        self.active_repository = active_repository
        self.orchestrator_factory = orchestrator_factory
        self.sleep = sleep or _sleep

    @classmethod
    def from_env(cls, config: ProductionSchedulerConfig | None = None) -> ProductionScheduler:
        return cls(
            config=config or ProductionSchedulerConfig(),
            active_repository=_active_repository_from_env(),
        )

    def run_once(self) -> SchedulerPassResult:
        started_at = _now(self.config)
        pass_id = f"scheduler_{format_cycle_time(started_at)}_{uuid4().hex[:12]}"
        lock = FileSchedulerLease(
            Path(self.config.lock_path),
            ttl_seconds=self.config.lock_ttl_seconds,
            workspace_root=Path(self.config.workspace_root),
        )
        lock_result = lock.acquire(pass_id=pass_id, started_at=started_at)
        if not lock_result["acquired"]:
            evidence = self._base_evidence(pass_id, started_at)
            evidence.update(
                {
                    "status": "lock_contended",
                    "finished_at": _format_utc(_now(self.config)),
                    "lock": lock_result,
                    "counts": _empty_counts(),
                    "candidates": [],
                    "blocked_candidates": [],
                    "model_discovery": _empty_model_discovery(),
                    "source_cycles": [],
                }
            )
            artifact_path = self._write_evidence(pass_id, evidence)
            status = _evidence_status(evidence, "lock_contended")
            return SchedulerPassResult(
                pass_id=pass_id,
                status=status,
                evidence=evidence,
                artifact_path=artifact_path,
            )

        try:
            models, model_evidence = self._discover_models()
            cycles, source_cycle_evidence = self._discover_cycles(started_at)
            candidates, blocked_candidates, skipped_candidates, candidate_duplicate_exclusions = self._build_candidates(
                models=models,
                cycles=cycles,
            )
            cancellation_evidence: list[dict[str, Any]] = []
            if (
                self.config.cancel_active_slurm
                and not self.config.dry_run
                and any(candidate.get("reason") == "cancel_requested_active_slurm" for candidate in skipped_candidates)
            ):
                cancellation_evidence = self._cancel_requested_active_slurm(skipped_candidates)
            execution_evidence: list[dict[str, Any]] = []
            submitted_count = 0
            failed_count = 0
            partial_count = 0
            execution_boundary = "planning_only"
            pass_status = "planned"
            no_mutation_proof = _no_mutation_proof()
            slurm_preflight_evidence: dict[str, Any] | None = None
            if not self.config.dry_run and candidates:
                slurm_preflight = _slurm_preflight(self.config)
                if slurm_preflight["status"] != "not_required":
                    slurm_preflight_evidence = redact_payload(slurm_preflight)
                if slurm_preflight["status"] == "blocked":
                    execution_evidence = [
                        _candidate_slurm_preflight_blocked_evidence(candidate, slurm_preflight)
                        for candidate in candidates
                    ]
                    execution_boundary = "slurm_preflight_blocked"
                    pass_status = "preflight_blocked"
                elif self.orchestrator_factory is None and not self.config.slurm_execution_enabled:
                    execution_evidence = [
                        _candidate_preflight_blocked_evidence(candidate, config=self.config) for candidate in candidates
                    ]
                    execution_boundary = "preflight_blocked"
                    pass_status = "preflight_blocked"
                    no_mutation_proof = _no_mutation_proof()
                else:
                    execution_evidence = self._execute_candidates(candidates)
                    submitted_count = sum(1 for item in execution_evidence if item.get("submitted") is True)
                    execution_boundary = (
                        "slurm_gateway_orchestration"
                        if self.config.slurm_execution_enabled
                        else "production_orchestration"
                    )
                pass_status = _scheduler_pass_status_from_execution(execution_evidence)
                no_mutation_proof = {
                    "adapter_download_called": False,
                    "slurm_submit_called": submitted_count > 0,
                    "shud_runtime_called": False,
                    "hydro_result_table_writes": submitted_count > 0,
                    "met_result_table_writes": submitted_count > 0,
                }
                failed_count = sum(1 for item in execution_evidence if item.get("status") == "failed")
                partial_count = _scheduler_partial_count_from_execution(execution_evidence)
            finished_at = _now(self.config)
            evidence = self._base_evidence(pass_id, started_at)
            evidence["operator_filters"].update(model_evidence["operator_filters"])
            duplicate_exclusions = [
                *self.config.source_exclusions,
                *[item for item in source_cycle_evidence if item.get("status") == "excluded"],
                *candidate_duplicate_exclusions,
            ]
            evidence.update(
                {
                    "status": pass_status,
                    "finished_at": _format_utc(finished_at),
                    "lock": lock_result,
                    "model_discovery": model_evidence,
                    "source_cycles": source_cycle_evidence,
                    "candidates": [candidate.to_dict() for candidate in candidates],
                    "blocked_candidates": [candidate.to_dict() for candidate in blocked_candidates],
                    "skipped_candidates": skipped_candidates,
                    "duplicate_exclusions": duplicate_exclusions,
                    "counts": {
                        "candidate_count": len(candidates),
                        "blocked_candidate_count": len(blocked_candidates),
                        "skipped_candidate_count": len(skipped_candidates),
                        "selected_model_count": len(models),
                        "source_cycle_count": len(cycles),
                        "submitted_count": submitted_count,
                        "failed_count": failed_count,
                        "partial_count": partial_count,
                    },
                    "model_run_evidence": execution_evidence,
                    "slurm_cancellation_evidence": cancellation_evidence,
                    "no_mutation_proof": no_mutation_proof,
                    "execution_boundary": execution_boundary,
                }
            )
            if slurm_preflight_evidence is not None:
                evidence["slurm_preflight"] = slurm_preflight_evidence
            artifact_path = self._write_evidence(pass_id, evidence)
            status = _evidence_status(evidence, pass_status)
            return SchedulerPassResult(
                pass_id=pass_id,
                status=status,
                evidence=evidence,
                artifact_path=artifact_path,
            )
        except SchedulerResourceLimitError as error:
            finished_at = _now(self.config)
            evidence = self._base_evidence(pass_id, started_at)
            evidence.update(
                {
                    "status": "resource_limit_blocked",
                    "finished_at": _format_utc(finished_at),
                    "lock": lock_result,
                    "limit": {"reason": error.reason, **error.details},
                    "counts": _empty_counts(),
                    "candidates": [],
                    "blocked_candidates": [],
                    "skipped_candidates": [],
                    "duplicate_exclusions": list(self.config.source_exclusions),
                    "model_discovery": _empty_model_discovery(),
                    "source_cycles": [],
                    "no_mutation_proof": {
                        "adapter_download_called": False,
                        "slurm_submit_called": False,
                        "shud_runtime_called": False,
                        "hydro_result_table_writes": False,
                        "met_result_table_writes": False,
                    },
                    "execution_boundary": "planning_only",
                }
            )
            artifact_path = self._write_evidence(pass_id, evidence)
            status = _evidence_status(evidence, "resource_limit_blocked")
            return SchedulerPassResult(
                pass_id=pass_id,
                status=status,
                evidence=evidence,
                artifact_path=artifact_path,
            )
        finally:
            lock.release(pass_id=pass_id)

    def run_continuous(self, *, max_passes: int | None = None) -> list[SchedulerPassResult]:
        if max_passes is not None:
            max_passes = int(max_passes)
            if max_passes > MAX_CONTINUOUS_JSON_PASSES:
                raise ValueError(
                    "production scheduler max_passes exceeds finite JSON output limit "
                    f"{MAX_CONTINUOUS_JSON_PASSES}"
                )
        results: list[SchedulerPassResult] = []
        completed = 0
        while max_passes is None or completed < max_passes:
            result = self.run_once()
            if max_passes is None:
                results[:] = [result]
            else:
                results.append(result)
            completed += 1
            if max_passes is not None and completed >= max_passes:
                break
            self.sleep(self.config.interval_seconds)
        return results

    def _execute_candidates(self, candidates: Sequence[SchedulerCandidate]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, datetime], list[SchedulerCandidate]] = {}
        for candidate in candidates:
            grouped.setdefault((candidate.source_id, candidate.cycle_time_utc), []).append(candidate)

        evidence: list[dict[str, Any]] = []
        for (source_id, cycle_time), cycle_candidates in sorted(
            grouped.items(),
            key=lambda item: (item[0][0], item[0][1], [candidate.model_id for candidate in item[1]]),
        ):
            cycle_id = cycle_id_for(source_id, cycle_time)
            orchestrator = self._orchestrator_for(source_id)
            basins: list[dict[str, Any]] = []
            submitted_candidates: list[SchedulerCandidate] = []
            candidate_output_uris: dict[str, str] = {}
            for candidate in cycle_candidates:
                output_uri = _candidate_output_uri(candidate, getattr(orchestrator, "object_store", None))
                if output_uri is None:
                    evidence.append(
                        {
                            **_candidate_identity_evidence(candidate),
                            "status": "blocked",
                            "submitted": False,
                            "mutation_occurred": False,
                            "cycle_id": cycle_id,
                            "error_code": "OUTPUT_URI_UNAVAILABLE",
                            "error_message": (
                                "Production orchestration requires an absolute deterministic output_uri "
                                "before runtime handoff."
                            ),
                            "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
                            "qhh_script_invoked": False,
                        }
                    )
                    continue
                candidate_output_uris[candidate.candidate_id] = output_uri
                submitted_candidates.append(candidate)
                basin_manifest = _candidate_basin_manifest(candidate, output_uri=output_uri)
                if self.config.slurm_execution_enabled and self.config.slurm_env:
                    basin_manifest["slurm_env"] = dict(self.config.slurm_env)
                basins.append(basin_manifest)
            if not basins:
                continue
            try:
                result = orchestrator.orchestrate_cycle(source_id, cycle_time, basins)
            except Exception as error:
                for candidate in submitted_candidates:
                    output_uri = candidate_output_uris.get(candidate.candidate_id)
                    evidence.append(
                        {
                            **_candidate_identity_evidence(candidate, output_uri=output_uri),
                            "status": "blocked",
                            "submitted": False,
                            "mutation_occurred": False,
                            "cycle_id": cycle_id,
                            "error_code": getattr(error, "error_code", "PRODUCTION_ORCHESTRATION_FAILED"),
                            "error_message": getattr(error, "message", str(error)),
                            "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
                            "qhh_script_invoked": False,
                        }
                    )
                continue
            evidence.extend(
                _candidate_execution_evidence(
                    result,
                    submitted_candidates,
                    output_uris=candidate_output_uris,
                )
            )
        return evidence

    def _cancel_requested_active_slurm(self, skipped_candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for candidate in skipped_candidates:
            if candidate.get("reason") != "cancel_requested_active_slurm":
                continue
            source_id = str(candidate.get("source_id") or "")
            cycle_time_text = candidate.get("cycle_time_utc")
            if not source_id or not cycle_time_text:
                continue
            grouped.setdefault((source_id, str(cycle_time_text)), candidate)

        evidence: list[dict[str, Any]] = []
        for (source_id, cycle_time_text), skipped in sorted(grouped.items()):
            cycle_time = _ensure_utc(datetime.fromisoformat(cycle_time_text.replace("Z", "+00:00")))
            cycle_id = cycle_id_for(source_id, cycle_time)
            orchestrator = self._orchestrator_for(source_id)
            cancel = getattr(orchestrator, "cancel_active_cycle_jobs", None)
            if not callable(cancel):
                evidence.append(
                    {
                        "source_id": source_id,
                        "cycle_id": cycle_id,
                        "cycle_time_utc": cycle_time_text,
                        "status": "blocked",
                        "error_code": "SLURM_CANCEL_UNSUPPORTED",
                        "replacement_submitted": False,
                    }
                )
                continue
            try:
                cancelled = [dict(item) for item in cancel(cycle_id, reason="scheduler_cancel_requested")]
            except Exception as error:
                evidence.append(
                    {
                        "source_id": source_id,
                        "cycle_id": cycle_id,
                        "cycle_time_utc": cycle_time_text,
                        "status": "failed",
                        "error_code": getattr(error, "error_code", "SLURM_CANCEL_FAILED"),
                        "error_message": getattr(error, "message", str(error)),
                        "replacement_submitted": False,
                        "active_slurm_jobs": skipped.get("active_slurm_jobs", []),
                    }
                )
                continue
            cancellation_status = _scheduler_cancellation_status(cancelled)
            cancellation_item: dict[str, Any] = {
                "source_id": source_id,
                "cycle_id": cycle_id,
                "cycle_time_utc": cycle_time_text,
                "status": cancellation_status,
                "cancelled_jobs": cancelled,
                "replacement_submitted": False,
                "active_slurm_jobs": skipped.get("active_slurm_jobs", []),
            }
            if cancellation_status != "cancelled":
                cancellation_item["error_code"] = "SLURM_CANCELLATION_GAP"
                cancellation_item["cancellation_proven"] = False
            evidence.append(
                cancellation_item
            )
        return evidence

    def _orchestrator_for(self, source_id: str) -> ForecastOrchestrator:
        if self.orchestrator_factory is not None:
            return self.orchestrator_factory(source_id)
        config = OrchestratorConfig.from_env()
        if self.config.slurm_execution_enabled:
            config = OrchestratorConfig(
                workspace_root=self.config.workspace_root,
                object_store_root=self.config.object_store_root or config.object_store_root,
                object_store_prefix=os.getenv("OBJECT_STORE_PREFIX", config.object_store_prefix),
                slurm_gateway_url=config.slurm_gateway_url,
                templates_dir=config.templates_dir,
                poll_interval_seconds=config.poll_interval_seconds,
                job_timeout_seconds=config.job_timeout_seconds,
                source_id=config.source_id,
                forecast_horizon_hours=config.forecast_horizon_hours,
                scenario_id=config.scenario_id if config.scenario_id_explicit else None,
                era5_area=config.era5_area,
                state_soft_stale_threshold_days=config.state_soft_stale_threshold_days,
                state_hard_stale_threshold_days=config.state_hard_stale_threshold_days,
                slurm_job_type_templates=dict(self.config.slurm_job_type_templates or {}),
                slurm_env=dict(self.config.slurm_env),
            )
        if config.source_id != source_id:
            config = OrchestratorConfig(
                workspace_root=config.workspace_root,
                object_store_root=config.object_store_root,
                object_store_prefix=config.object_store_prefix,
                slurm_gateway_url=config.slurm_gateway_url,
                templates_dir=config.templates_dir,
                poll_interval_seconds=config.poll_interval_seconds,
                job_timeout_seconds=config.job_timeout_seconds,
                source_id=source_id,
                forecast_horizon_hours=config.forecast_horizon_hours,
                scenario_id=scenario_for_source(source_id),
                era5_area=config.era5_area,
                state_soft_stale_threshold_days=config.state_soft_stale_threshold_days,
                state_hard_stale_threshold_days=config.state_hard_stale_threshold_days,
                slurm_job_type_templates=config.slurm_job_type_templates,
                slurm_env=config.slurm_env,
            )
        return ForecastOrchestrator(
            config=config,
            repository=_orchestrator_repository_from_env(),
            state_manager=None,
        )

    def _base_evidence(self, pass_id: str, started_at: datetime) -> dict[str, Any]:
        end_time = started_at - timedelta(hours=self.config.cycle_lag_hours)
        start_time = end_time - timedelta(hours=self.config.lookback_hours)
        return {
            "pass_id": pass_id,
            "started_at": _format_utc(started_at),
            "execution_mode": "dry_run" if self.config.dry_run else "production_orchestration",
            "dry_run": self.config.dry_run,
            "sources": list(self.config.sources),
            "duplicate_exclusions": list(self.config.source_exclusions),
            "cycle_window": {
                "start_time_utc": _format_utc(start_time),
                "end_time_utc": _format_utc(end_time),
                "lookback_hours": self.config.lookback_hours,
                "cycle_lag_hours": self.config.cycle_lag_hours,
                "max_cycles_per_source": self.config.max_cycles_per_source,
            },
            "operator_filters": {
                "model_ids": list(self.config.model_ids),
                "basin_ids": list(self.config.basin_ids),
                "expression": _filter_expression(self.config.model_ids, self.config.basin_ids),
                "excluded_runnable_count": 0,
            },
            "readiness": {
                "deterministic_fixture": True,
                "live_receipts": [],
                "production_ready": False,
            },
        }

    def _discover_models(self) -> tuple[list[RegisteredSchedulerModel], dict[str, Any]]:
        rows = _fetch_active_model_details(self.registry)
        exclusions: list[dict[str, Any]] = []
        runnable: list[RegisteredSchedulerModel] = []

        model_counts = Counter(str(row.get("model_id") or "") for row in rows)
        duplicate_model_ids = {model_id for model_id, count in model_counts.items() if model_id and count > 1}

        for row in rows:
            model_id = str(row.get("model_id") or "")
            if model_id in duplicate_model_ids:
                exclusions.append(_model_exclusion(row, "duplicate_active_model_identity"))
                continue
            model = _coerce_registered_model(row)
            if isinstance(model, RegisteredSchedulerModel):
                runnable.append(model)
            else:
                exclusions.append(model)

        runnable.sort(key=lambda item: item.model_id)
        selected: list[RegisteredSchedulerModel] = []
        filter_excluded = 0
        for model in runnable:
            if not _matches_filters(model, model_ids=self.config.model_ids, basin_ids=self.config.basin_ids):
                filter_excluded += 1
                exclusions.append(
                    {
                        "model_id": model.model_id,
                        "basin_id": model.basin_id,
                        "basin_version_id": model.basin_version_id,
                        "reason": "operator_filter_excluded",
                    }
                )
                continue
            selected.append(model)

        evidence = {
            "active_model_count": len(rows),
            "runnable_model_count": len(runnable),
            "selected_model_count": len(selected),
            "excluded_model_count": len(exclusions),
            "models": [model.to_dict() for model in selected],
            "exclusions": exclusions,
        }
        evidence["operator_filters"] = {
            "expression": _filter_expression(self.config.model_ids, self.config.basin_ids),
            "excluded_runnable_count": filter_excluded,
        }
        return selected, evidence

    def _discover_cycles(self, started_at: datetime) -> tuple[list[SchedulerSourceCycle], list[dict[str, Any]]]:
        end_time = started_at - timedelta(hours=self.config.cycle_lag_hours)
        start_time = end_time - timedelta(hours=self.config.lookback_hours)
        source_cycles: list[SchedulerSourceCycle] = []
        evidence: list[dict[str, Any]] = []
        seen_cycles: set[tuple[str, str]] = set()
        source_order = {source_id: index for index, source_id in enumerate(self.config.sources)}

        for source_id in self.config.sources:
            adapter = self.adapters.get(source_id)
            if adapter is None:
                source_evidence = {
                    "source_id": source_id,
                    "available": False,
                    "status": "blocked",
                    "reason": "source_adapter_unavailable",
                    "cycle_id": None,
                    "cycle_time_utc": None,
                }
                evidence.append(source_evidence)
                continue

            discoveries = self._discover_source_window(
                adapter,
                source_id=source_id,
                start_time=start_time,
                end_time=end_time,
            )
            discoveries = [
                discovery
                for discovery in discoveries
                if discovery.source_id == source_id and start_time <= _ensure_utc(discovery.cycle_time) <= end_time
            ]
            discoveries.sort(key=lambda discovery: discovery.cycle_time, reverse=True)
            selected_for_source: list[CycleDiscovery] = []
            for discovery in discoveries:
                cycle_key = (source_id, cycle_id_for(source_id, discovery.cycle_time))
                if cycle_key in seen_cycles:
                    evidence.append(_duplicate_cycle_evidence(discovery, reason="duplicate_source_cycle"))
                    continue
                seen_cycles.add(cycle_key)
                if len(selected_for_source) < self.config.max_cycles_per_source:
                    selected_for_source.append(discovery)
            for discovery in selected_for_source:
                horizon = _source_horizon_metadata(discovery, adapter)
                source_cycles.append(SchedulerSourceCycle(discovery=discovery, horizon=horizon))
                evidence.append(_source_cycle_evidence(discovery, horizon=horizon))

        source_cycles.sort(
            key=lambda item: (
                source_order.get(item.discovery.source_id, 999),
                item.discovery.cycle_time,
                item.discovery.cycle_hour,
            )
        )
        return source_cycles, evidence

    def _discover_source_window(
        self,
        adapter: CycleDiscoveryAdapter,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[CycleDiscovery]:
        discoveries: list[CycleDiscovery] = []
        current_date = start_time.date()
        while current_date <= end_time.date():
            try:
                daily = adapter.discover_cycles(current_date)
            except TypeError:
                daily = adapter.discover_cycles(current_date, None)
            if len(discoveries) + len(daily) > MAX_DISCOVERED_CYCLES:
                raise SchedulerResourceLimitError(
                    "cycle_discovery_limit_exceeded",
                    {
                        "max_discovered_cycles": MAX_DISCOVERED_CYCLES,
                        "discovered_cycle_count": len(discoveries) + len(daily),
                        "source_id": source_id,
                        "cycle_date": current_date.isoformat(),
                    },
                )
            discoveries.extend(daily)
            current_date += timedelta(days=1)
        return discoveries

    def _build_candidates(
        self,
        *,
        models: Sequence[RegisteredSchedulerModel],
        cycles: Sequence[SchedulerSourceCycle],
    ) -> tuple[list[SchedulerCandidate], list[SchedulerCandidate], list[dict[str, Any]], list[dict[str, Any]]]:
        candidates: list[SchedulerCandidate] = []
        blocked: list[SchedulerCandidate] = []
        skipped: list[dict[str, Any]] = []
        duplicate_exclusions: list[dict[str, Any]] = []
        seen_candidate_ids: set[str] = set()
        active_orchestration_provider = (
            getattr(self.active_repository, "has_active_orchestration", None)
            if self.active_repository is not None
            else None
        )
        completed_provider = (
            getattr(self.active_repository, "has_completed_pipeline", None)
            if self.active_repository is not None
            else None
        )
        active_slurm_jobs_provider = (
            getattr(self.active_repository, "active_slurm_jobs", None)
            if self.active_repository is not None
            else None
        )
        for cycle in cycles:
            discovery = cycle.discovery
            has_active_orchestration: bool | None = None
            for model in models:
                if len(candidates) + len(blocked) + len(skipped) >= MAX_CANDIDATES:
                    raise SchedulerResourceLimitError(
                        "candidate_limit_exceeded",
                        {
                            "max_candidates": MAX_CANDIDATES,
                            "source_cycle_count": len(cycles),
                            "selected_model_count": len(models),
                        },
                    )
                candidate = _candidate_for(discovery=discovery, model=model, horizon=cycle.horizon)
                if candidate.candidate_id in seen_candidate_ids:
                    exclusion = {
                        **candidate.to_dict(),
                        "status": "excluded",
                        "reason": "duplicate_candidate_identity",
                    }
                    skipped.append(exclusion)
                    duplicate_exclusions.append({"type": "candidate", **exclusion})
                    continue
                seen_candidate_ids.add(candidate.candidate_id)
                if not discovery.available:
                    blocked.append(_blocked_candidate(candidate, "source_cycle_unavailable"))
                    continue
                if has_active_orchestration is None:
                    has_active_orchestration = bool(
                        callable(active_orchestration_provider)
                        and active_orchestration_provider(
                            source_id=discovery.source_id,
                            cycle_time=discovery.cycle_time,
                        )
                    )
                active_slurm_jobs = (
                    list(
                        active_slurm_jobs_provider(
                            source_id=discovery.source_id,
                            cycle_time=discovery.cycle_time,
                            model_id=model.model_id,
                        )
                    )
                    if callable(active_slurm_jobs_provider)
                    else []
                )
                if has_active_orchestration and not (self.config.cancel_active_slurm and active_slurm_jobs):
                    skipped.append({**candidate.to_dict(), "reason": "active_duplicate_pipeline"})
                    continue
                if active_slurm_jobs:
                    if self.config.cancel_active_slurm:
                        skipped.append(
                            {
                                **candidate.to_dict(),
                                "reason": "cancel_requested_active_slurm",
                                "active_slurm_jobs": [dict(job) for job in active_slurm_jobs],
                                "replacement_submitted": False,
                            }
                        )
                    else:
                        skipped.append(
                            {
                                **candidate.to_dict(),
                                "reason": "active_slurm_job",
                                "active_slurm_jobs": [dict(job) for job in active_slurm_jobs],
                            }
                        )
                    continue
                if self.active_repository is not None and self.active_repository.has_active_pipeline(
                    source_id=discovery.source_id,
                    cycle_time=discovery.cycle_time,
                    model_id=model.model_id,
                ):
                    skipped.append({**candidate.to_dict(), "reason": "active_duplicate_pipeline"})
                    continue
                if callable(completed_provider) and completed_provider(
                    source_id=discovery.source_id,
                    cycle_time=discovery.cycle_time,
                    model_id=model.model_id,
                ):
                    skipped.append({**candidate.to_dict(), "reason": "completed_duplicate_pipeline"})
                    continue
                candidates.append(candidate)
        return candidates, blocked, skipped, duplicate_exclusions

    def _write_evidence(self, pass_id: str, evidence: Mapping[str, Any]) -> Path | None:
        evidence_dir = Path(self.config.evidence_dir)
        workspace_root = Path(self.config.workspace_root)
        _require_safe_directory_final_component(evidence_dir, workspace_root, "evidence_dir")
        artifact_path = evidence_dir / f"{pass_id}.json"
        _require_under_workspace(artifact_path.parent.resolve(), workspace_root, "evidence_dir")
        payload = dict(evidence)
        payload["artifact_path"] = str(artifact_path)
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        bounded_payload: dict[str, Any] | None = None
        if len(serialized.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            bounded_payload = _bounded_evidence_payload(payload, reason="evidence_size_limit_exceeded")
            serialized = json.dumps(bounded_payload, indent=2, sort_keys=True)
        evidence_dir_fd = _open_evidence_directory(evidence_dir, workspace_root)
        try:
            _write_new_regular_file(
                f"{pass_id}.json",
                serialized,
                dir_fd=evidence_dir_fd,
                artifact_path=artifact_path,
            )
        finally:
            os.close(evidence_dir_fd)
        if isinstance(evidence, dict):
            if bounded_payload is not None:
                evidence.clear()
                evidence.update(bounded_payload)
            evidence.setdefault("artifact_path", str(artifact_path))
        return artifact_path


class FileSchedulerLease:
    def __init__(self, lock_path: Path, *, ttl_seconds: int, workspace_root: Path | None = None) -> None:
        self.lock_path = lock_path
        self.ttl_seconds = ttl_seconds
        self.workspace_root = workspace_root
        self.acquired = False
        self.lease_token: str | None = None

    def acquire(self, *, pass_id: str, started_at: datetime) -> dict[str, Any]:
        token = uuid4().hex
        payload = {
            "owner": LOCK_OWNER,
            "schema_version": LOCK_SCHEMA_VERSION,
            "pass_id": pass_id,
            "lease_token": token,
            "pid": os.getpid(),
            "started_at": _format_utc(started_at),
            "lock_path": str(self.lock_path),
        }
        try:
            with self._guarded() as parent_fd:
                return self._acquire_locked(
                    pass_id=pass_id,
                    started_at=started_at,
                    payload=payload,
                    parent_fd=parent_fd,
                )
        except UnsafeSchedulerLockError as error:
            return {
                "acquired": False,
                "contention": True,
                "lock_path": str(self.lock_path),
                "reason": error.reason,
                "existing_lock": {"raw": None},
            }

    def _acquire_locked(
        self,
        *,
        pass_id: str,
        started_at: datetime,
        payload: Mapping[str, Any],
        parent_fd: int,
    ) -> dict[str, Any]:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock_path.name, flags, 0o644, dir_fd=parent_fd)
        except FileExistsError:
            state = self._existing_lock_state(started_at, parent_fd=parent_fd)
            if state["unsafe"]:
                return {
                    "acquired": False,
                    "contention": True,
                    "lock_path": str(self.lock_path),
                    "reason": state["reason"],
                    "existing_lock": state["existing_lock"],
                }
            if state["stale"]:
                _unlink_lock_file(self.lock_path.name, parent_fd=parent_fd)
                return self._acquire_locked(
                    pass_id=pass_id,
                    started_at=started_at,
                    payload=payload,
                    parent_fd=parent_fd,
                )
            return {
                "acquired": False,
                "contention": True,
                "lock_path": str(self.lock_path),
                "existing_lock": state["existing_lock"],
            }
        except OSError as error:
            if error.errno in {ELOOP, ENOTDIR}:
                raise UnsafeSchedulerLockError("unsafe_lock_symlink") from error
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        self.acquired = True
        self.lease_token = str(payload.get("lease_token"))
        return {"acquired": True, "contention": False, "lock_path": str(self.lock_path), "lease": dict(payload)}

    def release(self, *, pass_id: str) -> None:
        if not self.acquired:
            return
        try:
            with self._guarded() as parent_fd:
                existing = self._read_existing_lock(parent_fd=parent_fd)
                if existing.get("pass_id") == pass_id and existing.get("lease_token") == self.lease_token:
                    _unlink_lock_file(self.lock_path.name, parent_fd=parent_fd)
        except UnsafeSchedulerLockError:
            pass
        self.acquired = False
        self.lease_token = None

    @contextmanager
    def _guarded(self) -> Any:
        import fcntl

        parent_fd = _open_lock_parent_directory(self.lock_path.parent, self.workspace_root)
        try:
            guard_fd = _open_regular_guard_file(f"{self.lock_path.name}.guard", dir_fd=parent_fd)
        except Exception:
            os.close(parent_fd)
            raise
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX)
            yield parent_fd
        finally:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
            os.close(guard_fd)
            os.close(parent_fd)

    def _existing_lock_state(self, now: datetime | None = None, *, parent_fd: int) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        try:
            lock_stat = os.stat(self.lock_path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return {"unsafe": False, "stale": False, "reason": None, "existing_lock": {}}
        if stat.S_ISLNK(lock_stat.st_mode):
            return {
                "unsafe": True,
                "stale": False,
                "reason": "unsafe_lock_symlink",
                "existing_lock": {"raw": None},
            }
        if not stat.S_ISREG(lock_stat.st_mode):
            return {
                "unsafe": True,
                "stale": False,
                "reason": "unsafe_lock_not_regular_file",
                "existing_lock": {"raw": None},
            }
        if lock_stat.st_size > MAX_LOCK_PAYLOAD_BYTES:
            return {
                "unsafe": True,
                "stale": False,
                "reason": "unsafe_lock_too_large",
                "existing_lock": {
                    "raw": None,
                    "size_bytes": lock_stat.st_size,
                    "max_bytes": MAX_LOCK_PAYLOAD_BYTES,
                },
            }
        existing = self._read_existing_lock(parent_fd=parent_fd)
        scheduler_owned = (
            existing.get("owner") == LOCK_OWNER
            and existing.get("schema_version") == LOCK_SCHEMA_VERSION
            and existing.get("lease_token") not in (None, "")
            and existing.get("pass_id") not in (None, "")
        )
        mtime = datetime.fromtimestamp(lock_stat.st_mtime, tz=UTC)
        stale = (now - mtime).total_seconds() > self.ttl_seconds
        if stale and not scheduler_owned:
            return {
                "unsafe": True,
                "stale": True,
                "reason": "unsafe_lock_not_scheduler_owned",
                "existing_lock": existing,
            }
        return {"unsafe": False, "stale": stale, "reason": None, "existing_lock": existing}

    def _read_existing_lock(self, *, parent_fd: int) -> dict[str, Any]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock_path.name, flags, dir_fd=parent_fd)
        except OSError:
            return {"raw": None}
        try:
            lock_stat = os.fstat(fd)
            if not stat.S_ISREG(lock_stat.st_mode):
                return {"raw": None}
            if lock_stat.st_size > MAX_LOCK_PAYLOAD_BYTES:
                raise UnsafeSchedulerLockError("unsafe_lock_too_large")
            raw = os.read(fd, MAX_LOCK_PAYLOAD_BYTES + 1)
            if len(raw) > MAX_LOCK_PAYLOAD_BYTES:
                raise UnsafeSchedulerLockError("unsafe_lock_too_large")
            value = json.loads(raw.decode("utf-8"))
        except UnsafeSchedulerLockError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"raw": None}
        finally:
            os.close(fd)
        return dict(value) if isinstance(value, Mapping) else {"raw": value}


def _fetch_active_model_details(registry: ModelRegistryReader) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    offset = 0
    limit = 500
    pages = 0
    while True:
        pages += 1
        if pages > MAX_REGISTRY_PAGES:
            raise SchedulerResourceLimitError(
                "registry_page_limit_exceeded",
                {"max_registry_pages": MAX_REGISTRY_PAGES, "model_count": len(rows)},
            )
        page = registry.list_models(basin_version_id=None, active=True, limit=limit, offset=offset)
        items = list(page.get("items") or [])
        for item in items:
            if len(rows) >= MAX_DISCOVERED_MODELS:
                raise SchedulerResourceLimitError(
                    "model_limit_exceeded",
                    {"max_discovered_models": MAX_DISCOVERED_MODELS, "model_count": len(rows)},
                )
            model_id = str(item.get("model_id") or "")
            rows.append(registry.get_model(model_id) if model_id else item)
        total = int(page.get("total") or len(rows))
        offset += len(items)
        if len(items) == 0 or offset >= total:
            break
    return rows


def _coerce_registered_model(row: Mapping[str, Any]) -> RegisteredSchedulerModel | dict[str, Any]:
    resource_profile = row.get("resource_profile")
    if not isinstance(resource_profile, Mapping):
        resource_profile = {}
    lifecycle_state = str(row.get("lifecycle_state") or ("active" if row.get("active_flag") else "inactive"))
    required = {
        "model_id": row.get("model_id"),
        "basin_id": row.get("basin_id") or resource_profile.get("basin_id"),
        "basin_version_id": row.get("basin_version_id"),
        "river_network_version_id": row.get("river_network_version_id"),
        "model_package_uri": row.get("model_package_uri"),
        "shud_code_version": row.get("shud_code_version"),
    }
    if row.get("active_flag") is False or lifecycle_state != "active":
        return _model_exclusion(row, "inactive_model")
    if resource_profile.get("runnable") is False:
        return _model_exclusion(row, "not_runnable")
    if not required["shud_code_version"]:
        return _model_exclusion(row, "not_shud_model")
    missing = sorted(key for key, value in required.items() if value in (None, ""))
    if missing:
        return {**_model_exclusion(row, "incomplete_model_metadata"), "missing_fields": missing}

    segment_count = row.get("segment_count")
    return RegisteredSchedulerModel(
        model_id=str(required["model_id"]),
        basin_id=str(required["basin_id"]),
        basin_version_id=str(required["basin_version_id"]),
        river_network_version_id=str(required["river_network_version_id"]),
        segment_count=int(segment_count) if segment_count not in (None, "") else None,
        model_package_uri=str(required["model_package_uri"]),
        shud_code_version=str(required["shud_code_version"]),
        resource_profile=dict(resource_profile),
        resource_profile_summary=_resource_profile_summary(resource_profile),
        display_capabilities=_mapping_value(resource_profile.get("display_capabilities")),
        frequency_capabilities=_mapping_value(resource_profile.get("frequency_capabilities")),
    )


def _resource_profile_summary(resource_profile: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "resource_profile_id",
        "cpu",
        "memory_gb",
        "walltime",
        "max_concurrent",
        "shud_threads",
        "station_count",
        "station_ids",
        "forcing_station_metadata",
        "manifest_uri",
        "output_uri",
        "display_capabilities",
        "frequency_capabilities",
    )
    return {key: resource_profile[key] for key in keys if key in resource_profile}


def _mapping_value(value: Any) -> Mapping[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _model_exclusion(row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "model_id": row.get("model_id"),
        "basin_id": row.get("basin_id"),
        "basin_version_id": row.get("basin_version_id"),
        "reason": reason,
    }


def _matches_filters(
    model: RegisteredSchedulerModel,
    *,
    model_ids: Sequence[str],
    basin_ids: Sequence[str],
) -> bool:
    if model_ids and model.model_id not in set(model_ids):
        return False
    return not (basin_ids and model.basin_id not in set(basin_ids) and model.basin_version_id not in set(basin_ids))


def _filter_expression(model_ids: Sequence[str], basin_ids: Sequence[str]) -> str | None:
    parts: list[str] = []
    if model_ids:
        parts.append("model_id in [" + ",".join(model_ids) + "]")
    if basin_ids:
        parts.append("basin_id in [" + ",".join(basin_ids) + "]")
    return " and ".join(parts) if parts else None


def _source_cycle_evidence(discovery: CycleDiscovery, *, horizon: Mapping[str, Any]) -> dict[str, Any]:
    available = bool(discovery.available)
    return {
        "source_id": discovery.source_id,
        "cycle_id": discovery.cycle_id,
        "cycle_time_utc": _format_utc(discovery.cycle_time),
        "cycle_hour": discovery.cycle_hour,
        "horizon": dict(horizon),
        "available": available,
        "status": discovery.status or ("discovered" if available else "unavailable"),
        "reason": None if available else "source_cycle_unavailable",
        "db_cycle_status_written": None,
        "cycle_status_candidate": "discovered" if available else "unavailable",
    }


def _duplicate_cycle_evidence(discovery: CycleDiscovery, *, reason: str) -> dict[str, Any]:
    return {
        "type": "source_cycle",
        "source_id": discovery.source_id,
        "cycle_id": cycle_id_for(discovery.source_id, discovery.cycle_time),
        "cycle_time_utc": _format_utc(discovery.cycle_time),
        "cycle_hour": discovery.cycle_hour,
        "available": discovery.available,
        "status": "excluded",
        "reason": reason,
    }


def _candidate_for(
    *,
    discovery: CycleDiscovery,
    model: RegisteredSchedulerModel,
    horizon: Mapping[str, Any],
) -> SchedulerCandidate:
    source_id = normalize_source_id(discovery.source_id)
    cycle_time = _ensure_utc(discovery.cycle_time)
    compact_cycle = format_cycle_time(cycle_time)
    scenario_id = scenario_for_source(source_id)
    candidate_id = f"{source_id}:{_format_utc(cycle_time)}:{model.model_id}:{scenario_id}"
    return SchedulerCandidate(
        candidate_id=candidate_id,
        source_id=source_id,
        cycle_id=cycle_id_for(source_id, cycle_time),
        cycle_time_utc=cycle_time,
        model_id=model.model_id,
        basin_id=model.basin_id,
        basin_version_id=model.basin_version_id,
        river_network_version_id=model.river_network_version_id,
        segment_count=model.segment_count,
        model_package_uri=model.model_package_uri,
        resource_profile=model.resource_profile_summary,
        display_capabilities=model.display_capabilities,
        frequency_capabilities=model.frequency_capabilities,
        horizon=horizon,
        scenario_id=scenario_id,
        run_id=f"fcst_{source_id.lower()}_{compact_cycle}_{model.model_id}",
        forcing_version_id=f"forc_{source_id.lower()}_{compact_cycle}_{model.model_id}",
        status="selected",
    )


def _blocked_candidate(candidate: SchedulerCandidate, reason: str) -> SchedulerCandidate:
    return SchedulerCandidate(
        candidate_id=candidate.candidate_id,
        source_id=candidate.source_id,
        cycle_id=candidate.cycle_id,
        cycle_time_utc=candidate.cycle_time_utc,
        model_id=candidate.model_id,
        basin_id=candidate.basin_id,
        basin_version_id=candidate.basin_version_id,
        river_network_version_id=candidate.river_network_version_id,
        segment_count=candidate.segment_count,
        model_package_uri=candidate.model_package_uri,
        resource_profile=candidate.resource_profile,
        display_capabilities=candidate.display_capabilities,
        frequency_capabilities=candidate.frequency_capabilities,
        horizon=candidate.horizon,
        scenario_id=candidate.scenario_id,
        run_id=candidate.run_id,
        forcing_version_id=candidate.forcing_version_id,
        status="blocked",
        reason=reason,
    )


def _candidate_basin_manifest(candidate: SchedulerCandidate, *, output_uri: str) -> dict[str, Any]:
    resource_profile = dict(candidate.resource_profile)
    manifest = {
        "candidate_id": candidate.candidate_id,
        "source_id": candidate.source_id,
        "cycle_id": candidate.cycle_id,
        "cycle_time": format_cycle_time(candidate.cycle_time_utc),
        "model_id": candidate.model_id,
        "basin_id": candidate.basin_id,
        "basin_version_id": candidate.basin_version_id,
        "river_network_version_id": candidate.river_network_version_id,
        "segment_count": candidate.segment_count,
        "model_package_uri": candidate.model_package_uri,
        "model_package_manifest_uri": _model_package_manifest_uri(candidate),
        "resource_profile": dict(candidate.resource_profile),
        "display_capabilities": dict(candidate.display_capabilities),
        "frequency_capabilities": dict(candidate.frequency_capabilities),
        "scenario_id": candidate.scenario_id,
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "forecast_horizon_hours": candidate.horizon.get("forecast_horizon_hours")
        or candidate.horizon.get("max_lead_hours"),
        "max_lead_hours": candidate.horizon.get("max_lead_hours"),
        "station_count": _candidate_station_count(candidate),
        "station_ids": _candidate_station_ids(candidate),
        "frequency_curves_available": _nested_bool(
            candidate.frequency_capabilities,
            "curves_available",
            fallback=_nested_bool(candidate.frequency_capabilities, "return_periods"),
        ),
        "warning_thresholds_available": _nested_bool(candidate.frequency_capabilities, "warning_thresholds_available"),
        "optional_weather_available": _nested_bool(candidate.display_capabilities, "optional_weather_available"),
        "output_key": _candidate_output_key(candidate),
        "output_uri": output_uri,
    }
    forcing_metadata = resource_profile.get("forcing_station_metadata")
    if isinstance(forcing_metadata, Mapping):
        manifest["forcing_station_metadata"] = dict(forcing_metadata)
    slurm_env = resource_profile.get("slurm_env")
    if isinstance(slurm_env, Mapping):
        manifest["slurm_env"] = {str(key): str(value) for key, value in slurm_env.items()}
    return manifest


def _candidate_execution_attempted(outcome: Mapping[str, Any] | None, submitted: bool) -> bool:
    if submitted and outcome is None:
        return True
    if not outcome:
        return False
    return any(
        outcome.get(field) not in (None, "")
        for field in ("slurm_job_id", "exit_code", "log_uri", "accounting", "task_id", "original_task_id")
    )


def _candidate_identity_evidence(candidate: SchedulerCandidate, *, output_uri: str | None = None) -> dict[str, Any]:
    evidence = {
        "candidate_id": candidate.candidate_id,
        "source_id": candidate.source_id,
        "cycle_id": candidate.cycle_id,
        "cycle_time_utc": _format_utc(candidate.cycle_time_utc),
        "model_id": candidate.model_id,
        "scenario_id": candidate.scenario_id,
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "model_package_uri": candidate.model_package_uri,
        "model_package_manifest_uri": _model_package_manifest_uri(candidate),
        "basin_version_id": candidate.basin_version_id,
        "river_network_version_id": candidate.river_network_version_id,
        "segment_count": candidate.segment_count,
        "output_key": _candidate_output_key(candidate),
    }
    resolved_output_uri = output_uri or _candidate_output_uri(candidate)
    if resolved_output_uri is not None:
        evidence["output_uri"] = resolved_output_uri
    return evidence


def _candidate_preflight_blocked_evidence(
    candidate: SchedulerCandidate,
    *,
    config: ProductionSchedulerConfig | None = None,
) -> dict[str, Any]:
    if config is not None and config.slurm_execution_enabled:
        preflight = _slurm_preflight(config)
        return _candidate_slurm_preflight_blocked_evidence(candidate, preflight)
    return {
        **_candidate_identity_evidence(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "unsupported_without_safe_preflight",
        "error_code": "PRODUCTION_PREFLIGHT_UNSUPPORTED",
        "error_message": (
            "Default non-dry-run production scheduling is blocked until the Slurm/database preflight "
            "from issue #194 is available or a deterministic orchestrator_factory is injected."
        ),
        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
        "qhh_script_invoked": False,
        "residual_blockers": [
            {
                "code": "PRODUCTION_PREFLIGHT_UNSUPPORTED",
                "state": "blocked",
                "quality_flag": "preflight_required",
                "residual_risk": "No scheduler mutation was attempted.",
            }
        ],
    }


def _candidate_slurm_preflight_blocked_evidence(
    candidate: SchedulerCandidate,
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    blockers = list(preflight.get("blockers") or [])
    primary = blockers[0] if blockers else {
        "code": "SLURM_PREFLIGHT_BLOCKED",
        "message": "Slurm preflight blocked submission.",
    }
    return {
        **_candidate_identity_evidence(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "slurm_preflight",
        "slurm_preflight": redact_payload(preflight),
        "error_code": str(primary.get("code") or "SLURM_PREFLIGHT_BLOCKED"),
        "error_message": str(primary.get("message") or "Slurm preflight blocked submission."),
        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
        "qhh_script_invoked": False,
        "residual_blockers": [
            {
                "code": str(blocker.get("code") or "SLURM_PREFLIGHT_BLOCKED"),
                "field": blocker.get("field"),
                "state": "blocked",
                "quality_flag": "slurm_preflight_blocked",
                "residual_risk": blocker.get("message"),
            }
            for blocker in blockers
        ],
    }


def _candidate_execution_evidence(
    result: PipelineResult,
    candidates: Sequence[SchedulerCandidate],
    *,
    output_uris: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    stage_names = [stage.stage for stage in result.stages]
    stage_statuses = [
        {
            "stage": stage.stage,
            "job_type": stage.job_type,
            "status": stage.status,
            "slurm_job_id": stage.slurm_job_id,
            "error_code": stage.error_code,
        }
        for stage in result.stages
    ]
    submitted = any(stage.slurm_job_id for stage in result.stages)
    outcomes_by_candidate = {
        str(outcome.get("candidate_id")): dict(outcome)
        for outcome in getattr(result, "candidate_outcomes", ()) or ()
        if outcome.get("candidate_id")
    }
    return [
        _candidate_execution_evidence_item(
            result,
            candidate,
            output_uri=(output_uris or {}).get(candidate.candidate_id),
            outcome=outcomes_by_candidate.get(candidate.candidate_id),
            submitted=submitted,
            stage_names=stage_names,
            stage_statuses=stage_statuses,
        )
        for candidate in candidates
    ]


def _candidate_execution_evidence_item(
    result: PipelineResult,
    candidate: SchedulerCandidate,
    *,
    output_uri: str | None,
    outcome: Mapping[str, Any] | None,
    submitted: bool,
    stage_names: Sequence[str],
    stage_statuses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if outcome is None:
        status = result.status
        candidate_submitted = submitted
        mutation_occurred = submitted
        candidate_outcome: dict[str, Any] | None = None
        execution_attempted = submitted
    else:
        outcome_status = str(outcome.get("status") or "")
        status = _candidate_status_from_outcome(result.status, outcome_status)
        execution_attempted = _candidate_execution_attempted(outcome, submitted)
        candidate_submitted = submitted and (outcome_status == "active" or execution_attempted)
        mutation_occurred = candidate_submitted
        candidate_outcome = dict(outcome)
    item = {
        **_candidate_identity_evidence(
            candidate,
            output_uri=output_uri,
        ),
        "status": status,
        "submitted": candidate_submitted,
        "execution_attempted": execution_attempted,
        "final_candidate_success": (
            status == result.status and not _is_non_submitted_terminal_or_unavailable_status(status)
        ),
        "mutation_occurred": mutation_occurred,
        "pipeline_run_id": result.run_id,
        "standard_chain_shape": stage_names,
        "stage_statuses": stage_statuses,
        "qhh_script_invoked": False,
    }
    if candidate_outcome is not None:
        item["candidate_outcome"] = candidate_outcome
        if _is_partial_candidate_evidence(item):
            item["error_code"] = str(candidate_outcome.get("reason") or f"CANDIDATE_{status}").upper()
            item["error_message"] = (
                f"Candidate {candidate.candidate_id} was {status} in the partial multi-basin cycle."
            )
    return item


def _candidate_status_from_outcome(result_status: str, outcome_status: str) -> str:
    if outcome_status == "active":
        return result_status
    if _is_non_submitted_terminal_or_unavailable_status(outcome_status):
        return outcome_status
    return "unavailable"


def _model_package_manifest_uri(candidate: SchedulerCandidate) -> str:
    resource_profile = dict(candidate.resource_profile)
    explicit = resource_profile.get("manifest_uri")
    if explicit not in (None, ""):
        return str(explicit)
    package_uri = candidate.model_package_uri.rstrip("/")
    if package_uri.endswith("/package"):
        return f"{package_uri.removesuffix('/package')}/manifest.json"
    return f"{package_uri}/manifest.json"


def _candidate_output_key(candidate: SchedulerCandidate) -> str:
    return f"runs/{candidate.run_id}/output/"


def _candidate_output_uri(candidate: SchedulerCandidate, object_store: Any | None = None) -> str | None:
    explicit = candidate.resource_profile.get("output_uri")
    if explicit not in (None, "") and _has_uri_scheme(str(explicit)):
        return str(explicit).rstrip("/") + "/"
    if object_store is not None:
        uri_for_key = getattr(object_store, "uri_for_key", None)
        if callable(uri_for_key):
            return str(uri_for_key(_candidate_output_key(candidate))).rstrip("/") + "/"
    return None


def _has_uri_scheme(value: str) -> bool:
    return re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value.strip()) is not None


def _candidate_station_count(candidate: SchedulerCandidate) -> int | None:
    value = candidate.resource_profile.get("station_count")
    if value in (None, ""):
        forcing = candidate.resource_profile.get("forcing_station_metadata")
        if isinstance(forcing, Mapping):
            value = forcing.get("station_count")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _candidate_station_ids(candidate: SchedulerCandidate) -> list[str]:
    value = candidate.resource_profile.get("station_ids")
    if value in (None, ""):
        forcing = candidate.resource_profile.get("forcing_station_metadata")
        if isinstance(forcing, Mapping):
            value = forcing.get("station_ids")
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [str(item) for item in value]
    return []


def _slurm_preflight(config: ProductionSchedulerConfig) -> dict[str, Any]:
    if not config.slurm_execution_enabled:
        return {
            "status": "not_required",
            "enabled": False,
            "blockers": [],
            "checks": {},
        }

    blockers: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}

    database_url = config.database_url
    db_blocker = _database_url_blocker(database_url)
    checks["database"] = {
        "configured": bool(database_url),
        "host": _database_host(database_url),
        "compute_node_reachable": db_blocker is None,
    }
    if db_blocker is not None:
        blockers.append(db_blocker)

    roots = {
        "workspace_root": config.workspace_root,
        "object_store_root": config.object_store_root,
        "log_root": config.log_root,
        "runtime_root": config.runtime_root,
    }
    allowed_roots = _preflight_allowed_roots(config)
    root_checks: dict[str, Any] = {}
    for field_name, value in roots.items():
        root_check, blocker = _storage_root_check(field_name, value, allowed_roots)
        root_checks[field_name] = root_check
        if blocker is not None:
            blockers.append(blocker)
    checks["storage_roots"] = root_checks
    checks["allowed_roots"] = [str(root) for root in allowed_roots]

    template_check, template_blockers = _slurm_template_allowlist_check(config)
    checks["templates"] = template_check
    blockers.extend(template_blockers)

    env_check, env_blockers = _slurm_env_check(config.slurm_env)
    checks["environment"] = env_check
    blockers.extend(env_blockers)

    return {
        "status": "blocked" if blockers else "ready",
        "enabled": True,
        "blockers": blockers,
        "checks": checks,
    }


def _database_url_blocker(database_url: str | None) -> dict[str, Any] | None:
    if not database_url:
        return {
            "code": "SLURM_PREFLIGHT_DATABASE_URL_MISSING",
            "field": "DATABASE_URL",
            "message": "Slurm execution requires a compute-node reachable DATABASE_URL before submission.",
        }
    host = _database_host(database_url)
    if host is None or host.lower() in LOCALHOST_NAMES:
        return {
            "code": "SLURM_PREFLIGHT_DATABASE_URL_LOCALHOST",
            "field": "DATABASE_URL",
            "message": "Slurm execution rejects localhost-only DATABASE_URL values.",
            "host": host,
        }
    return None


def _database_host(database_url: str | None) -> str | None:
    if not database_url:
        return None
    parsed = urlparse(database_url)
    if parsed.scheme == "sqlite":
        return "localhost"
    return parsed.hostname


def _preflight_allowed_roots(config: ProductionSchedulerConfig) -> tuple[Path, ...]:
    roots = list(config.allowed_storage_roots) or [Path(config.workspace_root)]
    resolved: list[Path] = []
    for root in roots:
        candidate = root.expanduser().resolve()
        if candidate not in resolved:
            resolved.append(candidate)
    return tuple(resolved)


def _storage_root_check(
    field_name: str,
    value: Path | str | None,
    allowed_roots: Sequence[Path],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if value in (None, ""):
        return (
            {
                "configured": False,
                "path": None,
                "contained": False,
                "compute_node_visible": False,
            },
            {
                "code": f"SLURM_PREFLIGHT_{field_name.upper()}_MISSING",
                "field": field_name,
                "message": f"Slurm execution requires configured {field_name}.",
            },
        )
    path = Path(value).expanduser()
    resolved = path.resolve()
    visible = path.exists() and path.is_dir()
    contained = _path_is_under_any(resolved, allowed_roots)
    check = {
        "configured": True,
        "path": str(resolved),
        "contained": contained,
        "compute_node_visible": visible,
    }
    if not contained:
        return (
            check,
            {
                "code": f"SLURM_PREFLIGHT_{field_name.upper()}_OUT_OF_ROOT",
                "field": field_name,
                "path": str(resolved),
                "message": f"Slurm {field_name} must stay under configured project or production roots.",
            },
        )
    if not visible:
        return (
            check,
            {
                "code": f"SLURM_PREFLIGHT_{field_name.upper()}_NOT_VISIBLE",
                "field": field_name,
                "path": str(resolved),
                "message": f"Slurm {field_name} must exist as a compute-node visible directory.",
            },
        )
    return check, None


def _path_is_under_any(path: Path, allowed_roots: Sequence[Path]) -> bool:
    for root in allowed_roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _slurm_template_allowlist_check(config: ProductionSchedulerConfig) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    templates = dict(config.slurm_job_type_templates or {})
    blockers: list[dict[str, Any]] = []
    expected_by_stage = {stage.stage: stage.job_type for stage in ForecastOrchestrator.stages}
    allowed_names = set(DEFAULT_JOB_TYPE_TEMPLATES.values())
    checks: dict[str, Any] = {}
    for stage_name, job_type in expected_by_stage.items():
        template_name = templates.get(job_type)
        check = {
            "job_type": job_type,
            "template_name": template_name,
            "allowlisted": template_name in allowed_names,
            "array_capable": stage_name in SLURM_ARRAY_STAGE_NAMES,
        }
        checks[stage_name] = check
        if template_name not in allowed_names:
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_TEMPLATE_NOT_ALLOWLISTED",
                    "field": f"slurm_job_type_templates.{job_type}",
                    "stage": stage_name,
                    "job_type": job_type,
                    "template_name": template_name,
                    "message": f"Slurm stage {stage_name} must use an allowlisted sbatch template.",
                }
            )
        expected_template = DEFAULT_JOB_TYPE_TEMPLATES.get(job_type)
        if template_name in allowed_names and template_name != expected_template:
            check["expected_template_name"] = expected_template
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_TEMPLATE_MISMATCH",
                    "field": f"slurm_job_type_templates.{job_type}",
                    "stage": stage_name,
                    "job_type": job_type,
                    "template_name": template_name,
                    "expected_template_name": expected_template,
                    "message": f"Slurm stage {stage_name} must use the template assigned to its job type.",
                }
            )
        if stage_name in SLURM_ARRAY_STAGE_NAMES and not str(template_name or "").endswith("_array.sbatch"):
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ARRAY_TEMPLATE_REQUIRED",
                    "field": f"slurm_job_type_templates.{job_type}",
                    "stage": stage_name,
                    "job_type": job_type,
                    "template_name": template_name,
                    "message": f"Slurm stage {stage_name} requires an array-capable template.",
                }
            )
    return {"stage_templates": checks}, blockers


def _slurm_env_check(env: Mapping[str, str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    sanitized: dict[str, str] = {}
    for key, value in env.items():
        key_text = str(key)
        value_text = str(value)
        if not SAFE_SLURM_ENV_KEY_RE.fullmatch(key_text):
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_KEY_UNSAFE",
                    "field": f"slurm_env.{key_text}",
                    "message": "Slurm exported environment keys must be uppercase shell identifiers.",
                }
            )
            continue
        if is_sensitive_slurm_env_key(key_text):
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_SECRET_REJECTED",
                    "field": f"slurm_env.{key_text}",
                    "message": "Slurm scheduler evidence and exports reject secret-shaped environment keys.",
                }
            )
            sanitized[key_text] = "[redacted]"
            continue
        if len(value_text) > MAX_SLURM_ENV_VALUE_LENGTH:
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_VALUE_TOO_LONG",
                    "field": f"slurm_env.{key_text}",
                    "max_length": MAX_SLURM_ENV_VALUE_LENGTH,
                    "message": "Slurm exported environment values must be bounded.",
                }
            )
            sanitized[key_text] = value_text[:64] + "...[truncated]"
            continue
        secret_url_reason = secret_bearing_url_reason(value_text)
        if secret_url_reason is not None:
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_SECRET_REJECTED",
                    "field": f"slurm_env.{key_text}",
                    "reason": secret_url_reason,
                    "message": (
                        "Slurm exported environment values must not contain URL credentials "
                        "or secret query parameters."
                    ),
                }
            )
            sanitized[key_text] = "[redacted]"
            continue
        if SHELL_META_RE.search(value_text) or not SAFE_SLURM_ENV_VALUE_RE.fullmatch(value_text):
            blockers.append(
                {
                    "code": "SLURM_PREFLIGHT_ENV_VALUE_UNSAFE",
                    "field": f"slurm_env.{key_text}",
                    "message": "Slurm exported environment values must be shell-safe.",
                }
            )
            sanitized[key_text] = "[unsafe]"
            continue
        sanitized[key_text] = value_text
    return {"count": len(env), "sanitized": sanitized}, blockers


def _scheduler_cancellation_status(cancelled_jobs: Sequence[Mapping[str, Any]]) -> str:
    if not cancelled_jobs:
        return "blocked"
    cancelled_count = 0
    for job in cancelled_jobs:
        status = str(job.get("status") or "").lower()
        if job.get("error_code") or job.get("cancellation_proven") is False or status != "cancelled":
            continue
        cancelled_count += 1
    if cancelled_count == len(cancelled_jobs):
        return "cancelled"
    if cancelled_count:
        return "partially_cancelled"
    return "blocked"


def _nested_bool(mapping: Mapping[str, Any], key: str, *, fallback: bool | None = None) -> bool | None:
    value = mapping.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "available", "ready", "yes", "1"}:
            return True
        if normalized in {"false", "unavailable", "missing", "blocked", "no", "0"}:
            return False
    return fallback


def _default_adapters() -> Mapping[str, CycleDiscoveryAdapter]:
    from workers.data_adapters.gfs_adapter import GFSAdapter, GFSAdapterConfig
    from workers.data_adapters.ifs_adapter import IFSAdapter, IFSAdapterConfig

    return {
        "gfs": GFSAdapter(config=GFSAdapterConfig(), repository=None),
        "IFS": IFSAdapter(config=IFSAdapterConfig(), repository=None),
    }


def _active_repository_from_env() -> ActiveCandidateRepository:
    from services.orchestrator.chain import PsycopgOrchestratorRepository

    return PsycopgOrchestratorRepository.from_env()


def _orchestrator_repository_from_env() -> Any:
    from services.orchestrator.chain import PsycopgOrchestratorRepository

    return PsycopgOrchestratorRepository.from_env()


def _empty_counts() -> dict[str, int]:
    return {
        "candidate_count": 0,
        "blocked_candidate_count": 0,
        "skipped_candidate_count": 0,
        "selected_model_count": 0,
        "source_cycle_count": 0,
        "submitted_count": 0,
        "failed_count": 0,
        "partial_count": 0,
    }


def _no_mutation_proof() -> dict[str, bool]:
    return {
        "adapter_download_called": False,
        "slurm_submit_called": False,
        "shud_runtime_called": False,
        "hydro_result_table_writes": False,
        "met_result_table_writes": False,
    }


def _scheduler_pass_status_from_execution(execution_evidence: Sequence[Mapping[str, Any]]) -> str:
    if not execution_evidence:
        return "planned"
    if all(str(item.get("status")) == "preflight_blocked" for item in execution_evidence):
        return "preflight_blocked"
    if any(item.get("submitted") is True for item in execution_evidence):
        if _scheduler_partial_count_from_execution(execution_evidence) > 0:
            return "submitted_partial"
        return "submitted"
    if any(str(item.get("status")) in {"blocked", "failed"} for item in execution_evidence):
        return "preflight_blocked"
    return str(execution_evidence[-1].get("status") or "planned")


def _scheduler_partial_count_from_execution(execution_evidence: Sequence[Mapping[str, Any]]) -> int:
    if not any(item.get("submitted") is True or item.get("execution_attempted") is True for item in execution_evidence):
        return 0
    return sum(1 for item in execution_evidence if _is_partial_candidate_evidence(item))


def _is_partial_candidate_evidence(item: Mapping[str, Any]) -> bool:
    status = str(item.get("status") or "")
    if item.get("submitted") is True:
        return _is_non_submitted_terminal_or_unavailable_status(status)
    return _is_non_submitted_terminal_or_unavailable_status(status) or status.endswith("_partial")


def _is_non_submitted_terminal_or_unavailable_status(status: str) -> bool:
    normalized = status.strip().lower()
    return normalized in {
        "blocked",
        "cancelled",
        "failed",
        "partially_failed",
        "permanently_failed",
        "preflight_blocked",
        "submission_failed",
        "unavailable",
    } or normalized.endswith(("_blocked", "_cancelled", "_failed", "_unavailable"))


def _empty_model_discovery() -> dict[str, Any]:
    return {
        "active_model_count": 0,
        "runnable_model_count": 0,
        "selected_model_count": 0,
        "excluded_model_count": 0,
        "models": [],
        "exclusions": [],
        "operator_filters": {"expression": None, "excluded_runnable_count": 0},
    }


def _normalize_sources(sources: Sequence[str]) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    normalized: list[str] = []
    exclusions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_source in sources:
        source_id = normalize_source_id(raw_source)
        if source_id in seen:
            exclusions.append(
                {
                    "type": "source",
                    "source_id": source_id,
                    "status": "excluded",
                    "reason": "duplicate_source",
                }
            )
            continue
        seen.add(source_id)
        normalized.append(source_id)
    return tuple(normalized), exclusions


def _confined_path(value: Path | str, workspace_root: Path, field_name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workspace_root / path
    resolved_parent = path.parent.resolve()
    candidate = resolved_parent / path.name
    _require_under_workspace(resolved_parent, workspace_root, field_name)
    return candidate


def _optional_config_path(value: Path | str | None) -> Path | None:
    if value in (None, ""):
        return None
    return Path(value).expanduser().resolve()


def _env_flag(name: str) -> bool:
    value = os.getenv(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _require_under_workspace(path: Path, workspace_root: Path, field_name: str) -> None:
    try:
        path.relative_to(workspace_root)
    except ValueError as error:
        raise ValueError(f"production scheduler {field_name} must be under workspace_root") from error


def _require_safe_directory_final_component(path: Path, workspace_root: Path, field_name: str) -> None:
    _require_under_workspace(path.parent.resolve(), workspace_root, field_name)
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(path_stat.st_mode):
        resolved = path.resolve(strict=False)
        _require_under_workspace(resolved, workspace_root, field_name)
        if resolved.exists() and not resolved.is_dir():
            raise ValueError(f"production scheduler {field_name} must be a directory")
        return
    if not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError(f"production scheduler {field_name} must be a directory")


def _open_lock_parent_directory(lock_parent: Path, workspace_root: Path | None) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    if workspace_root is None:
        lock_parent.mkdir(parents=True, exist_ok=True)
        try:
            return os.open(lock_parent, directory_flags)
        except OSError as error:
            if error.errno in {ELOOP, ENOTDIR}:
                raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
            raise

    workspace_root = workspace_root.resolve()
    _ensure_workspace_directory(workspace_root)
    try:
        relative_parent = lock_parent.relative_to(workspace_root)
    except ValueError as error:
        raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error

    try:
        root_fd = os.open(workspace_root, directory_flags)
    except OSError as error:
        if error.errno in {ELOOP, ENOTDIR}:
            raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
        raise

    parent_fd = root_fd
    try:
        for component in relative_parent.parts:
            if component in ("", "."):
                continue
            if component == "..":
                raise UnsafeSchedulerLockError("unsafe_lock_parent_directory")
            try:
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o755, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except OSError as error:
                if error.errno in {ELOOP, ENOTDIR}:
                    raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
                raise
            os.close(parent_fd)
            parent_fd = child_fd
    except Exception:
        os.close(parent_fd)
        raise
    return parent_fd


def _ensure_workspace_directory(workspace_root: Path) -> None:
    try:
        workspace_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        if error.errno in {ELOOP, ENOTDIR}:
            raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
        raise
    try:
        root_stat = workspace_root.lstat()
    except FileNotFoundError as error:
        raise UnsafeSchedulerLockError("unsafe_lock_parent_directory") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise UnsafeSchedulerLockError("unsafe_lock_parent_directory")


def _open_evidence_directory(evidence_dir: Path, workspace_root: Path) -> int:
    try:
        return _open_lock_parent_directory(evidence_dir, workspace_root)
    except UnsafeSchedulerLockError as error:
        raise SchedulerEvidenceWriteError("unsafe_evidence_directory") from error


def _write_new_regular_file(
    artifact_name: str,
    serialized: str,
    *,
    dir_fd: int,
    artifact_path: Path,
) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(artifact_name, flags, 0o644, dir_fd=dir_fd)
    except FileExistsError as error:
        try:
            artifact_stat = os.stat(artifact_name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            artifact_stat = None
        reason = (
            "evidence_artifact_exists"
            if artifact_stat is not None and stat.S_ISREG(artifact_stat.st_mode)
            else "unsafe_evidence_artifact"
        )
        raise SchedulerEvidenceWriteError(
            reason,
            {"artifact_path": str(artifact_path)},
        ) from error
    except OSError as error:
        if error.errno in {EEXIST, EISDIR, ELOOP, ENOTDIR}:
            raise SchedulerEvidenceWriteError(
                "unsafe_evidence_artifact",
                {"artifact_path": str(artifact_path)},
            ) from error
        raise
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise
    with handle:
        handle.write(serialized)


def _open_regular_guard_file(guard_name: str, *, dir_fd: int) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(guard_name, os.O_CREAT | os.O_RDWR | nofollow, 0o644, dir_fd=dir_fd)
    except OSError as error:
        if error.errno in {EEXIST, EISDIR, ELOOP, ENOTDIR}:
            raise UnsafeSchedulerLockError("unsafe_lock_guard_not_regular_file") from error
        raise
    try:
        guard_stat = os.fstat(fd)
        if not stat.S_ISREG(guard_stat.st_mode):
            raise UnsafeSchedulerLockError("unsafe_lock_guard_not_regular_file")
    except Exception:
        os.close(fd)
        raise
    return fd


def _unlink_lock_file(lock_name: str, *, parent_fd: int) -> None:
    try:
        os.unlink(lock_name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _source_horizon_metadata(discovery: CycleDiscovery, adapter: CycleDiscoveryAdapter) -> dict[str, Any]:
    source_id = normalize_source_id(discovery.source_id)
    cycle_time = _ensure_utc(discovery.cycle_time)
    config = getattr(adapter, "config", None)
    max_lead_hours: int | None = None
    if config is not None and hasattr(config, "forecast_end_hour_for_cycle"):
        max_lead_hours = int(config.forecast_end_hour_for_cycle(cycle_time.hour))
    elif config is not None and hasattr(config, "forecast_end_hour"):
        max_lead_hours = int(getattr(config, "forecast_end_hour"))
    elif source_id == "IFS":
        max_lead_hours = 144 if cycle_time.hour in {6, 18} else 168
    elif source_id == "gfs":
        max_lead_hours = 168
    return {
        "max_lead_hours": max_lead_hours,
        "forecast_horizon_hours": max_lead_hours,
        "policy": "source_cycle",
    }


def _bounded_evidence_payload(payload: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
    return {
        "pass_id": payload.get("pass_id"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "status": "resource_limit_blocked",
        "limit": {"reason": reason, "max_evidence_bytes": MAX_EVIDENCE_BYTES},
        "counts": payload.get("counts", _empty_counts()),
        "candidates": [],
        "blocked_candidates": [],
        "skipped_candidates": [],
        "duplicate_exclusions": payload.get("duplicate_exclusions", []),
        "source_cycles": [],
        "model_discovery": _empty_model_discovery(),
        "artifact_path": payload.get("artifact_path"),
        "execution_boundary": payload.get("execution_boundary", "planning_only"),
        "no_mutation_proof": payload.get(
            "no_mutation_proof",
            {
                "adapter_download_called": False,
                "slurm_submit_called": False,
                "shud_runtime_called": False,
                "hydro_result_table_writes": False,
                "met_result_table_writes": False,
            },
        ),
    }


def _evidence_status(evidence: Mapping[str, Any], fallback: str) -> str:
    status = evidence.get("status")
    return str(status) if status not in (None, "") else fallback


def _now(config: ProductionSchedulerConfig) -> datetime:
    return config.now or datetime.now(UTC)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
