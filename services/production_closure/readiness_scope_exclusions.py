from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

SCOPED_EXCLUSION_RECORDS: tuple[Mapping[str, Any], ...] = (
    {
        "item_id": "scope-exclusion-cldas",
        "surface": "cldas_restricted_source",
        "residual_risk": "CLDAS restricted data is outside the current M19 readiness scope.",
        "removal_criteria": (
            "Enable CLDAS adapter, credentials, data-quality checks, and accepted live proof in a later scope."
        ),
        "exclusion": {
            "id": "cldas-restricted",
            "reason": "CLDAS is excluded by current product decision for M19.",
            "status": "not_executed",
            "removal_criteria": "Complete CLDAS authorization and production best-available integration.",
        },
    },
    {
        "item_id": "scope-exclusion-national-data",
        "surface": "incomplete_real_national_data",
        "residual_risk": "Complete real national data coverage is outside the current deterministic M19 scope.",
        "removal_criteria": (
            "Attach accepted target-environment national-data, live PostGIS, and performance evidence in a later "
            "scope."
        ),
        "exclusion": {
            "id": "real-national-data-incomplete",
            "reason": "Incomplete real national data is a scoped exclusion, not deterministic failure.",
            "status": "not_executed",
            "removal_criteria": "Complete national-data coverage and live MVT/performance proof.",
        },
    },
)


def _exclusion_items(
    config: Any,
    *,
    item_factory: Callable[..., dict[str, Any]],
) -> list[dict[str, Any]]:
    del config
    return [
        item_factory(
            item_id=str(record["item_id"]),
            surface=str(record["surface"]),
            status="not_executed",
            execution_mode="not_executed",
            required_for_final=False,
            live_proof_accepted=False,
            artifact_refs=["readiness_items.json", "summary.json"],
            residual_risk=str(record["residual_risk"]),
            removal_criteria=str(record["removal_criteria"]),
            exclusions=[record["exclusion"]],
        )
        for record in SCOPED_EXCLUSION_RECORDS
    ]
