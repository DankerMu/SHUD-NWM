from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from packages.common.model_registry import PsycopgModelRegistryStore
from packages.common.source_identity import normalize_source_id
from services.orchestrator.chain import scenario_for_source
from workers.data_adapters.base import CycleDiscovery, cycle_id_for, format_cycle_time

DEFAULT_PRODUCTION_SOURCES = ("gfs", "IFS")
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_CYCLE_LAG_HOURS = 0
DEFAULT_MAX_CYCLES_PER_SOURCE = 1
DEFAULT_LOCK_TTL_SECONDS = 3600


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
    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class ProductionSchedulerConfig:
    workspace_root: Path | str = field(default_factory=lambda: os.getenv("WORKSPACE_ROOT", ".nhms-workspace"))
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

    def __post_init__(self) -> None:
        workspace_root = Path(self.workspace_root).expanduser().resolve()
        object.__setattr__(self, "workspace_root", workspace_root)
        object.__setattr__(self, "sources", tuple(normalize_source_id(source) for source in self.sources))
        object.__setattr__(self, "lookback_hours", max(int(self.lookback_hours), 0))
        object.__setattr__(self, "cycle_lag_hours", max(int(self.cycle_lag_hours), 0))
        object.__setattr__(self, "max_cycles_per_source", max(int(self.max_cycles_per_source), 1))
        object.__setattr__(self, "model_ids", tuple(str(model_id) for model_id in self.model_ids if model_id))
        object.__setattr__(self, "basin_ids", tuple(str(basin_id) for basin_id in self.basin_ids if basin_id))
        object.__setattr__(self, "interval_seconds", max(float(self.interval_seconds), 1.0))
        object.__setattr__(self, "lock_ttl_seconds", max(int(self.lock_ttl_seconds), 1))
        if self.lock_path is None:
            object.__setattr__(self, "lock_path", workspace_root / "scheduler" / "production-scheduler.lock")
        else:
            object.__setattr__(self, "lock_path", Path(self.lock_path).expanduser().resolve())
        if self.evidence_dir is None:
            object.__setattr__(self, "evidence_dir", workspace_root / "scheduler" / "evidence")
        else:
            object.__setattr__(self, "evidence_dir", Path(self.evidence_dir).expanduser().resolve())
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
            "scenario_id": self.scenario_id,
            "run_id": self.run_id,
            "forcing_version_id": self.forcing_version_id,
            "status": self.status,
            "reason": self.reason,
        }


class ProductionScheduler:
    def __init__(
        self,
        config: ProductionSchedulerConfig | None = None,
        *,
        registry: ModelRegistryReader | None = None,
        adapters: Mapping[str, CycleDiscoveryAdapter] | None = None,
        active_repository: ActiveCandidateRepository | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config or ProductionSchedulerConfig()
        self.registry = registry or PsycopgModelRegistryStore.from_env()
        self.adapters = dict(adapters or _default_adapters())
        self.active_repository = active_repository
        self.sleep = sleep or _sleep

    @classmethod
    def from_env(cls, config: ProductionSchedulerConfig | None = None) -> ProductionScheduler:
        return cls(config=config or ProductionSchedulerConfig())

    def run_once(self) -> SchedulerPassResult:
        started_at = _now(self.config)
        pass_id = f"scheduler_{format_cycle_time(started_at)}_{uuid4().hex[:12]}"
        lock = FileSchedulerLease(Path(self.config.lock_path), ttl_seconds=self.config.lock_ttl_seconds)
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
            return SchedulerPassResult(
                pass_id=pass_id,
                status="lock_contended",
                evidence=evidence,
                artifact_path=artifact_path,
            )

        try:
            models, model_evidence = self._discover_models()
            cycles, source_cycle_evidence = self._discover_cycles(started_at)
            candidates, blocked_candidates, skipped_candidates = self._build_candidates(models=models, cycles=cycles)
            finished_at = _now(self.config)
            evidence = self._base_evidence(pass_id, started_at)
            evidence.update(
                {
                    "status": "planned",
                    "finished_at": _format_utc(finished_at),
                    "lock": lock_result,
                    "model_discovery": model_evidence,
                    "source_cycles": source_cycle_evidence,
                    "candidates": [candidate.to_dict() for candidate in candidates],
                    "blocked_candidates": [candidate.to_dict() for candidate in blocked_candidates],
                    "skipped_candidates": skipped_candidates,
                    "counts": {
                        "candidate_count": len(candidates),
                        "blocked_candidate_count": len(blocked_candidates),
                        "skipped_candidate_count": len(skipped_candidates),
                        "selected_model_count": len(models),
                        "source_cycle_count": len(cycles),
                        "submitted_count": 0,
                        "failed_count": 0,
                        "partial_count": 0,
                    },
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
            return SchedulerPassResult(
                pass_id=pass_id,
                status="planned",
                evidence=evidence,
                artifact_path=artifact_path,
            )
        finally:
            lock.release(pass_id=pass_id)

    def run_continuous(self, *, max_passes: int | None = None) -> list[SchedulerPassResult]:
        results: list[SchedulerPassResult] = []
        completed = 0
        while max_passes is None or completed < max_passes:
            results.append(self.run_once())
            completed += 1
            if max_passes is not None and completed >= max_passes:
                break
            self.sleep(self.config.interval_seconds)
        return results

    def _base_evidence(self, pass_id: str, started_at: datetime) -> dict[str, Any]:
        end_time = started_at - timedelta(hours=self.config.cycle_lag_hours)
        start_time = end_time - timedelta(hours=self.config.lookback_hours)
        return {
            "pass_id": pass_id,
            "started_at": _format_utc(started_at),
            "execution_mode": "dry_run" if self.config.dry_run else "planning",
            "dry_run": self.config.dry_run,
            "sources": list(self.config.sources),
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

    def _discover_cycles(self, started_at: datetime) -> tuple[list[CycleDiscovery], list[dict[str, Any]]]:
        end_time = started_at - timedelta(hours=self.config.cycle_lag_hours)
        start_time = end_time - timedelta(hours=self.config.lookback_hours)
        source_cycles: list[CycleDiscovery] = []
        evidence: list[dict[str, Any]] = []

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

            discoveries = self._discover_source_window(adapter, start_time=start_time, end_time=end_time)
            discoveries = [
                discovery
                for discovery in discoveries
                if discovery.source_id == source_id and start_time <= _ensure_utc(discovery.cycle_time) <= end_time
            ]
            discoveries.sort(key=lambda discovery: discovery.cycle_time, reverse=True)
            selected_for_source = discoveries[: self.config.max_cycles_per_source]
            source_cycles.extend(selected_for_source)
            for discovery in selected_for_source:
                evidence.append(_source_cycle_evidence(discovery))

        source_order = {source_id: index for index, source_id in enumerate(self.config.sources)}
        source_cycles.sort(key=lambda item: (source_order.get(item.source_id, 999), item.cycle_time, item.cycle_hour))
        return source_cycles, evidence

    def _discover_source_window(
        self,
        adapter: CycleDiscoveryAdapter,
        *,
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
            discoveries.extend(daily)
            current_date += timedelta(days=1)
        return discoveries

    def _build_candidates(
        self,
        *,
        models: Sequence[RegisteredSchedulerModel],
        cycles: Sequence[CycleDiscovery],
    ) -> tuple[list[SchedulerCandidate], list[SchedulerCandidate], list[dict[str, Any]]]:
        candidates: list[SchedulerCandidate] = []
        blocked: list[SchedulerCandidate] = []
        skipped: list[dict[str, Any]] = []
        for discovery in cycles:
            for model in models:
                candidate = _candidate_for(discovery=discovery, model=model)
                if not discovery.available:
                    blocked.append(_blocked_candidate(candidate, "source_cycle_unavailable"))
                    continue
                if self.active_repository is not None and self.active_repository.has_active_pipeline(
                    source_id=discovery.source_id,
                    cycle_time=discovery.cycle_time,
                    model_id=model.model_id,
                ):
                    skipped.append({**candidate.to_dict(), "reason": "active_duplicate_pipeline"})
                    continue
                candidates.append(candidate)
        return candidates, blocked, skipped

    def _write_evidence(self, pass_id: str, evidence: Mapping[str, Any]) -> Path | None:
        evidence_dir = Path(self.config.evidence_dir)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = evidence_dir / f"{pass_id}.json"
        payload = dict(evidence)
        payload["artifact_path"] = str(artifact_path)
        artifact_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        evidence.setdefault("artifact_path", str(artifact_path)) if isinstance(evidence, dict) else None
        return artifact_path


class FileSchedulerLease:
    def __init__(self, lock_path: Path, *, ttl_seconds: int) -> None:
        self.lock_path = lock_path
        self.ttl_seconds = ttl_seconds
        self.acquired = False

    def acquire(self, *, pass_id: str, started_at: datetime) -> dict[str, Any]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pass_id": pass_id,
            "pid": os.getpid(),
            "started_at": _format_utc(started_at),
            "lock_path": str(self.lock_path),
        }
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(self.lock_path, flags, 0o644)
        except FileExistsError:
            if self._is_stale(started_at):
                self.lock_path.unlink(missing_ok=True)
                return self.acquire(pass_id=pass_id, started_at=started_at)
            return {
                "acquired": False,
                "contention": True,
                "lock_path": str(self.lock_path),
                "existing_lock": self._read_existing_lock(),
            }
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        self.acquired = True
        return {"acquired": True, "contention": False, "lock_path": str(self.lock_path), "lease": payload}

    def release(self, *, pass_id: str) -> None:
        if not self.acquired:
            return
        existing = self._read_existing_lock()
        if existing.get("pass_id") == pass_id:
            self.lock_path.unlink(missing_ok=True)
        self.acquired = False

    def _is_stale(self, now: datetime) -> bool:
        try:
            mtime = datetime.fromtimestamp(self.lock_path.stat().st_mtime, tz=UTC)
        except FileNotFoundError:
            return False
        return (now - mtime).total_seconds() > self.ttl_seconds

    def _read_existing_lock(self) -> dict[str, Any]:
        try:
            value = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"raw": None}
        return dict(value) if isinstance(value, Mapping) else {"raw": value}


def _fetch_active_model_details(registry: ModelRegistryReader) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    offset = 0
    limit = 500
    while True:
        page = registry.list_models(basin_version_id=None, active=True, limit=limit, offset=offset)
        items = list(page.get("items") or [])
        for item in items:
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


def _source_cycle_evidence(discovery: CycleDiscovery) -> dict[str, Any]:
    available = bool(discovery.available)
    return {
        "source_id": discovery.source_id,
        "cycle_id": discovery.cycle_id,
        "cycle_time_utc": _format_utc(discovery.cycle_time),
        "cycle_hour": discovery.cycle_hour,
        "available": available,
        "status": discovery.status or ("discovered" if available else "unavailable"),
        "reason": None if available else "source_cycle_unavailable",
        "db_cycle_status_written": None if not available else "discovered",
    }


def _candidate_for(*, discovery: CycleDiscovery, model: RegisteredSchedulerModel) -> SchedulerCandidate:
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
        scenario_id=candidate.scenario_id,
        run_id=candidate.run_id,
        forcing_version_id=candidate.forcing_version_id,
        status="blocked",
        reason=reason,
    )


def _default_adapters() -> Mapping[str, CycleDiscoveryAdapter]:
    from workers.data_adapters.gfs_adapter import GFSAdapter, GFSAdapterConfig
    from workers.data_adapters.ifs_adapter import IFSAdapter, IFSAdapterConfig

    return {
        "gfs": GFSAdapter(config=GFSAdapterConfig(), repository=None),
        "IFS": IFSAdapter(config=IFSAdapterConfig(), repository=None),
    }


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


def _empty_model_discovery() -> dict[str, Any]:
    return {
        "active_model_count": 0,
        "runnable_model_count": 0,
        "selected_model_count": 0,
        "excluded_model_count": 0,
        "models": [],
        "exclusions": [],
    }


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
