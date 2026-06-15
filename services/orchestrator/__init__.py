"""Pipeline orchestrator public exports."""

from __future__ import annotations

from typing import Any

from .production_contract import (
    PRODUCTION_CONTRACT_ID,
    PRODUCTION_CONTRACT_SCHEMA_VERSION,
    ProductionContractError,
    ProductionIdentity,
    production_contract_matrix,
    production_identity_from_payload,
    validate_display_artifact_evidence,
    validate_display_readable_uri,
    validate_same_production_identity,
)

_CHAIN_EXPORTS = {
    "ForecastOrchestrator",
    "OrchestratorConfig",
    "OrchestratorError",
    "PipelineAlreadyActiveError",
    "PipelineResult",
    "PsycopgOrchestratorRepository",
    "StageRunResult",
    "scenario_for_source",
}

__all__ = [
    "ForecastOrchestrator",
    "OrchestratorConfig",
    "OrchestratorError",
    "PipelineAlreadyActiveError",
    "PipelineResult",
    "PsycopgOrchestratorRepository",
    "StageRunResult",
    "PRODUCTION_CONTRACT_ID",
    "PRODUCTION_CONTRACT_SCHEMA_VERSION",
    "ProductionContractError",
    "ProductionIdentity",
    "production_contract_matrix",
    "production_identity_from_payload",
    "scenario_for_source",
    "validate_display_artifact_evidence",
    "validate_display_readable_uri",
    "validate_same_production_identity",
]


def __getattr__(name: str) -> Any:
    if name in _CHAIN_EXPORTS:
        from . import chain

        value = getattr(chain, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
