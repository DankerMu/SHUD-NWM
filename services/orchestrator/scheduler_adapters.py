from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from services.orchestrator.scheduler_candidates import _canonical_readiness_unavailable_evidence
from services.orchestrator.scheduler_state import _format_utc
from services.orchestrator.scheduler_types import SchedulerCandidate
from workers.canonical_converter.converter import evaluate_canonical_readiness
from workers.data_adapters.base import (
    CycleDiscovery,
    cycle_id_for,
    format_cycle_time,
    generate_segmented_forecast_hours,
    parse_cycle_date,
    parse_cycle_time,
    parse_resolution_segments,
)


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

    def get_model_internal(self, model_id: str) -> Mapping[str, Any]:
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

    def candidate_state(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        run_id: str,
        forcing_version_id: str,
        candidate_id: str,
    ) -> Mapping[str, Any] | None:
        raise NotImplementedError


class CanonicalReadinessProvider(Protocol):
    def canonical_readiness(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        forecast_hours: Sequence[int],
        policy_identity: Mapping[str, Any],
        source_object_identity: Mapping[str, Any],
        canonical_product_id: str,
        model_id: str,
        basin_id: str,
    ) -> Mapping[str, Any]:
        raise NotImplementedError


class ForcingProducerRunner(Protocol):
    def produce(
        self,
        *,
        source_id: str | None = None,
        cycle_time: str | datetime,
        model_id: str,
        max_lead_hours: int | None = None,
        basin_id: str | None = None,
        basin_version_id: str | None = None,
        river_network_version_id: str | None = None,
        canonical_product_id: str | None = None,
        canonical_identity: Mapping[str, Any] | None = None,
    ) -> Any:
        raise NotImplementedError


class ProductionOrchestratorFactory(Protocol):
    def __call__(self, source_id: str) -> Any:
        raise NotImplementedError


_CANONICAL_READINESS_PROVIDER_UNSET = object()


class _UnavailableCanonicalReadinessProvider:
    def __init__(self, *, reason: str, dependency: str, retryable: bool) -> None:
        self.reason = reason
        self.dependency = dependency
        self.retryable = retryable

    def canonical_readiness(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        forecast_hours: Sequence[int],
        policy_identity: Mapping[str, Any],
        source_object_identity: Mapping[str, Any],
        canonical_product_id: str,
        model_id: str,
        basin_id: str,
    ) -> Mapping[str, Any]:
        discovery = CycleDiscovery(
            cycle_id=cycle_id_for(source_id, cycle_time),
            source_id=source_id,
            cycle_time=cycle_time,
            cycle_hour=cycle_time.hour,
            available=True,
            status="discovered",
        )
        candidate = SchedulerCandidate(
            candidate_id=f"{source_id}:{_format_utc(cycle_time)}:{model_id}:canonical_readiness",
            source_id=source_id,
            cycle_id=cycle_id_for(source_id, cycle_time),
            cycle_time_utc=cycle_time,
            model_id=model_id,
            basin_id=basin_id,
            basin_version_id=None,
            river_network_version_id=None,
            segment_count=None,
            output_segment_count=None,
            model_package_uri=None,
            resource_profile={},
            display_capabilities={},
            horizon={},
            scenario_id="canonical_readiness",
            run_id="",
            forcing_version_id="",
            status="blocked",
        )
        return _canonical_readiness_unavailable_evidence(
            discovery,
            candidate,
            forecast_hours=forecast_hours,
            policy_identity=policy_identity,
            source_object_identity=source_object_identity,
            reason=self.reason,
            dependency=self.dependency,
            retryable=self.retryable,
        )


def _default_adapters() -> Mapping[str, CycleDiscoveryAdapter]:
    from workers.data_adapters.gfs_adapter import GFSAdapter, GFSAdapterConfig
    from workers.data_adapters.ifs_adapter import IFSAdapter, IFSAdapterConfig

    return {
        "gfs": GFSAdapter(config=GFSAdapterConfig(), repository=None),
        "IFS": IFSAdapter(config=IFSAdapterConfig(), repository=None),
    }


class NfsRawManifestAdapterConfig:
    """Network-free source-cycle config backed by node-27 raw manifests."""

    def __init__(self, source_id: str, *, allowed_cycle_hours_utc: Sequence[int]) -> None:
        self.source_id = str(source_id)
        self.cycle_hours_utc = tuple(
            hour for hour in (int(hour) for hour in allowed_cycle_hours_utc) if hour in {0, 12}
        )
        env_prefix = "IFS" if self.source_id.upper() == "IFS" else self.source_id.upper()
        self.forecast_start_hour = int(os.getenv(f"{env_prefix}_FORECAST_START_HOUR", "0"))
        self.forecast_step_hours = int(os.getenv(f"{env_prefix}_FORECAST_STEP_HOURS", "3"))
        self.forecast_resolution_segments = self._forecast_resolution_segments(env_prefix)
        self.forecast_end_hour = int(os.getenv(f"{env_prefix}_FORECAST_END_HOUR", "168"))

    def forecast_end_hour_for_cycle(self, cycle_hour: int) -> int:
        env_prefix = "IFS" if self.source_id.upper() == "IFS" else self.source_id.upper()
        if os.getenv(f"{env_prefix}_FORECAST_END_HOUR"):
            return self.forecast_end_hour
        if int(cycle_hour) % 24 not in {0, 12}:
            raise ValueError("DB-free NFS raw manifest discovery supports only 00Z/12Z source cycles.")
        return self.forecast_end_hour

    def forecast_hours_for_cycle(self, cycle_time: str | datetime) -> list[int]:
        parsed_cycle_time = parse_cycle_time(cycle_time)
        end_hour = self.forecast_end_hour_for_cycle(parsed_cycle_time.hour)
        if self.forecast_resolution_segments:
            return generate_segmented_forecast_hours(
                self.forecast_start_hour,
                end_hour,
                self.forecast_resolution_segments,
            )
        return list(range(self.forecast_start_hour, end_hour + 1, self.forecast_step_hours))

    def _forecast_resolution_segments(self, env_prefix: str) -> tuple[tuple[int, int], ...] | None:
        configured = parse_resolution_segments(os.getenv(f"{env_prefix}_FORECAST_RESOLUTION_SEGMENTS"))
        if configured is not None:
            return configured
        if self.source_id.upper() == "IFS":
            return ((144, 3), (360, 6))
        return None


class NfsRawManifestCycleDiscoveryAdapter:
    """Discover source cycles by reading node-27 raw handoff manifests only."""

    def __init__(self, source_id: str, *, allowed_cycle_hours_utc: Sequence[int]) -> None:
        self.source_id = str(source_id)
        self.config = NfsRawManifestAdapterConfig(
            self.source_id,
            allowed_cycle_hours_utc=allowed_cycle_hours_utc,
        )

    def discover_cycles(
        self,
        cycle_date: str | date | datetime,
        end_date: str | date | datetime | None = None,
    ) -> list[CycleDiscovery]:
        start = parse_cycle_date(cycle_date)
        end = parse_cycle_date(end_date) if end_date is not None else start
        discoveries: list[CycleDiscovery] = []
        current = start
        while current <= end:
            for cycle_hour in self.config.cycle_hours_utc:
                cycle_time = datetime(
                    current.year,
                    current.month,
                    current.day,
                    int(cycle_hour),
                    tzinfo=UTC,
                )
                discoveries.append(self._discovery_for_cycle(cycle_time))
            current += timedelta(days=1)
        return discoveries

    def source_policy_identity(
        self,
        cycle_time: str | datetime | Sequence[int] | None = None,
        forecast_hours: Sequence[int] | None = None,
    ) -> dict[str, Any]:
        parsed_cycle_time, hours = self._identity_inputs(cycle_time, forecast_hours)
        if parsed_cycle_time is not None:
            policy = self._manifest_policy_identity(parsed_cycle_time)
            if policy:
                return policy
        return {"source": self.source_id, "forecast_hours": list(hours)}

    def source_object_identity(
        self,
        cycle_time: str | datetime,
        forecast_hours: Sequence[int] | None = None,
    ) -> dict[str, Any]:
        parsed_cycle_time = parse_cycle_time(cycle_time)
        identity = self._manifest_source_object_identity(parsed_cycle_time)
        if identity:
            return identity
        del forecast_hours
        return {
            "source": self.source_id,
            "manifest_object_key": f"raw/{self.source_id}/{format_cycle_time(parsed_cycle_time)}/manifest.json",
        }

    def _discovery_for_cycle(self, cycle_time: datetime) -> CycleDiscovery:
        readiness = self._readiness(cycle_time)
        ready = readiness.get("status") == "ready"
        reason = None if ready else self._unavailable_reason(readiness)
        return CycleDiscovery(
            cycle_id=cycle_id_for(self.source_id, cycle_time),
            source_id=self.source_id,
            cycle_time=cycle_time,
            cycle_hour=cycle_time.hour,
            available=ready,
            status="discovered" if ready else str(readiness.get("status") or "unavailable"),
            reason=reason,
            retryable=not ready,
            probe_uri=None,
            evidence=self._public_readiness_evidence(readiness),
        )

    def _readiness(self, cycle_time: datetime) -> dict[str, Any]:
        from services.orchestrator import source_cycle_raw_manifest

        readiness = source_cycle_raw_manifest.nfs_raw_manifest_readiness_from_env(self.source_id, cycle_time)
        if isinstance(readiness, Mapping):
            return dict(readiness)
        return {
            "status": "missing",
            "required": False,
            "reason": "nfs_raw_manifest_disabled",
            "source": source_cycle_raw_manifest.NFS_RAW_MANIFEST_READY_SOURCE,
            "source_id": self.source_id,
            "cycle_id": cycle_id_for(self.source_id, cycle_time),
            "cycle_time": _format_utc(cycle_time),
        }

    def _manifest_policy_identity(self, cycle_time: datetime) -> dict[str, Any] | None:
        from services.orchestrator import source_cycle_raw_manifest

        readiness = self._readiness(cycle_time)
        identity = source_cycle_raw_manifest.source_policy_from_raw_manifest_readiness(readiness)
        return dict(identity) if isinstance(identity, Mapping) and identity else None

    def _manifest_source_object_identity(self, cycle_time: datetime) -> dict[str, Any] | None:
        from services.orchestrator import source_cycle_raw_manifest

        readiness = self._readiness(cycle_time)
        identity = source_cycle_raw_manifest.source_object_identity_from_raw_manifest_readiness(readiness)
        return dict(identity) if isinstance(identity, Mapping) and identity else None

    def _identity_inputs(
        self,
        cycle_time: str | datetime | Sequence[int] | None,
        forecast_hours: Sequence[int] | None,
    ) -> tuple[datetime | None, list[int]]:
        if isinstance(cycle_time, str | datetime):
            parsed_cycle_time = parse_cycle_time(cycle_time)
            hours = list(
                forecast_hours
                if forecast_hours is not None
                else self.config.forecast_hours_for_cycle(parsed_cycle_time)
            )
            return parsed_cycle_time, [int(hour) for hour in hours]
        if forecast_hours is not None:
            return None, [int(hour) for hour in forecast_hours]
        if isinstance(cycle_time, Sequence):
            return None, [int(hour) for hour in cycle_time]
        return None, []

    def _unavailable_reason(self, readiness: Mapping[str, Any]) -> str:
        reason = str(readiness.get("reason") or readiness.get("status") or "unavailable")
        return reason if reason.startswith("nfs_raw_manifest_") else f"nfs_raw_manifest_{reason}"

    def _public_readiness_evidence(self, readiness: Mapping[str, Any]) -> dict[str, Any]:
        from services.orchestrator.scheduler_file_providers import _public_raw_manifest_evidence

        return _public_raw_manifest_evidence(readiness)


def _db_free_default_adapters(config: Any) -> Mapping[str, CycleDiscoveryAdapter]:
    allowed_cycle_hours = tuple(int(hour) for hour in getattr(config, "allowed_cycle_hours_utc", (0, 12)))
    return {
        str(source_id): NfsRawManifestCycleDiscoveryAdapter(
            str(source_id),
            allowed_cycle_hours_utc=allowed_cycle_hours,
        )
        for source_id in getattr(config, "sources", ())
    }


class _MetStoreCanonicalReadinessProvider:
    def __init__(self, store: Any) -> None:
        self.store = store

    def canonical_readiness(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        forecast_hours: Sequence[int],
        policy_identity: Mapping[str, Any],
        source_object_identity: Mapping[str, Any],
        canonical_product_id: str,
        model_id: str,
        basin_id: str,
    ) -> Mapping[str, Any]:
        products = self.store.list_canonical_products(source_id=source_id, cycle_time=cycle_time)
        return evaluate_canonical_readiness(
            source_id=source_id,
            cycle_time=cycle_time,
            products=products,
            forecast_hours=forecast_hours,
            policy_identity=policy_identity,
            source_object_identity=source_object_identity,
            canonical_product_id=canonical_product_id,
            model_id=model_id,
            basin_id=basin_id,
        ).evidence


def _canonical_readiness_provider_from_env() -> CanonicalReadinessProvider:
    try:
        from packages.common.met_store import PsycopgMetStore

        return _MetStoreCanonicalReadinessProvider(PsycopgMetStore.from_env())
    except ImportError:
        return _UnavailableCanonicalReadinessProvider(
            reason="canonical_readiness_dependency_unavailable",
            dependency="canonical_readiness_provider",
            retryable=True,
        )
    except Exception:
        return _UnavailableCanonicalReadinessProvider(
            reason="canonical_readiness_provider_unavailable",
            dependency="canonical_readiness_provider",
            retryable=True,
        )


def _forcing_producer_from_env() -> ForcingProducerRunner:
    from workers.forcing_producer import ForcingProducer

    return ForcingProducer.from_env()


def _active_repository_from_env() -> ActiveCandidateRepository:
    from services.orchestrator.chain import PsycopgOrchestratorRepository

    return PsycopgOrchestratorRepository.from_env()


def _orchestrator_repository_from_env() -> Any:
    from services.orchestrator.chain import PsycopgOrchestratorRepository

    return PsycopgOrchestratorRepository.from_env()
