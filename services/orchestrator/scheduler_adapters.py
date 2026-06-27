from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Protocol

from services.orchestrator.scheduler_candidates import _canonical_readiness_unavailable_evidence
from services.orchestrator.scheduler_state import _format_utc
from services.orchestrator.scheduler_types import SchedulerCandidate
from workers.canonical_converter.converter import evaluate_canonical_readiness
from workers.data_adapters.base import CycleDiscovery, cycle_id_for


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
            frequency_capabilities={},
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
