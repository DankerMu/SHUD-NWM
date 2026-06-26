from __future__ import annotations

from typing import Any, Mapping

STATUS_VALUES = frozenset({"passed", "failed", "blocked", "not_executed", "release_blocked"})
EXECUTION_MODE_VALUES = frozenset(
    {
        "deterministic",
        "policy_simulated",
        "backend_route_executed",
        "dry_run_sink",
        "simulated_drill",
        "live_proof",
        "not_executed",
    }
)
EXECUTED_MODES = EXECUTION_MODE_VALUES - {"not_executed"}
ALLOWED_STATUS_EXECUTION_MODES: Mapping[str, frozenset[str]] = {
    "passed": frozenset(
        {
            "deterministic",
            "policy_simulated",
            "backend_route_executed",
            "dry_run_sink",
            "simulated_drill",
            "live_proof",
        }
    ),
    "failed": frozenset(EXECUTED_MODES),
    "blocked": frozenset({"not_executed"}),
    "not_executed": frozenset({"not_executed"}),
    "release_blocked": frozenset(
        {"not_executed", "policy_simulated", "dry_run_sink", "simulated_drill", "live_proof"}
    ),
}


class ProductionReadinessValidationError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def validate_readiness_item(item: Mapping[str, Any]) -> None:
    status = str(item.get("status", ""))
    execution_mode = str(item.get("execution_mode", ""))
    if status not in STATUS_VALUES:
        raise ProductionReadinessValidationError(
            "PRODUCTION_READINESS_STATUS_INVALID",
            f"Readiness status is not supported: {status!r}.",
        )
    if execution_mode not in EXECUTION_MODE_VALUES:
        raise ProductionReadinessValidationError(
            "PRODUCTION_READINESS_EXECUTION_MODE_INVALID",
            f"Readiness execution_mode is not supported: {execution_mode!r}.",
        )
    if execution_mode not in ALLOWED_STATUS_EXECUTION_MODES[status]:
        raise ProductionReadinessValidationError(
            "PRODUCTION_READINESS_STATUS_MODE_INVALID",
            f"Readiness status/execution_mode pair is not allowed: {status}/{execution_mode}.",
        )
    required_fields = (
        "item_id",
        "surface",
        "required_for_final",
        "live_proof_accepted",
        "artifact_refs",
        "residual_risk",
        "removal_criteria",
        "exclusions",
        "owner",
        "action",
    )
    missing = [field for field in required_fields if field not in item]
    if missing:
        raise ProductionReadinessValidationError(
            "PRODUCTION_READINESS_ITEM_FIELD_MISSING",
            f"Readiness item is missing required fields: {', '.join(missing)}.",
        )
    if status == "release_blocked" and item.get("required_for_final") is True:
        if not str(item.get("residual_risk", "")).strip() or not str(item.get("removal_criteria", "")).strip():
            raise ProductionReadinessValidationError(
                "PRODUCTION_READINESS_BLOCKER_CONTEXT_MISSING",
                "Release-blocked readiness items require residual_risk and removal_criteria.",
            )


__all__ = [
    "ALLOWED_STATUS_EXECUTION_MODES",
    "EXECUTED_MODES",
    "EXECUTION_MODE_VALUES",
    "ProductionReadinessValidationError",
    "STATUS_VALUES",
    "validate_readiness_item",
]
