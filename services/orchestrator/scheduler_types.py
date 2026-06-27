from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from services.orchestrator import scheduler as _scheduler
from services.orchestrator.production_contract import production_identity_contract_evidence


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
    output_segment_count: int | None
    model_package_uri: str
    shud_code_version: str
    resource_profile: Mapping[str, Any]
    resource_profile_summary: Mapping[str, Any]
    display_capabilities: Mapping[str, Any]
    frequency_capabilities: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "model_id": self.model_id,
            "basin_id": self.basin_id,
            "basin_version_id": self.basin_version_id,
            "river_network_version_id": self.river_network_version_id,
            "segment_count": self.segment_count,
            "output_segment_count": self.output_segment_count,
            "model_package_uri": _scheduler._redact_secret_manifest_for_evidence(
                self.model_package_uri,
                "model_package_uri",
            ),
            "shud_code_version": self.shud_code_version,
            "resource_profile": _scheduler._resource_profile_evidence(self.resource_profile_summary),
            "display_capabilities": dict(self.display_capabilities),
            "frequency_capabilities": dict(self.frequency_capabilities),
        }
        project_identity = _resource_profile_project_identity(self.resource_profile)
        if project_identity is not None:
            payload.update(project_identity)
        return payload


def _resource_profile_project_identity(resource_profile: Mapping[str, Any]) -> dict[str, str] | None:
    project_name = resource_profile.get("project_name")
    shud_input_name = resource_profile.get("shud_input_name")
    project = str(project_name) if project_name not in (None, "") else None
    shud_input = str(shud_input_name) if shud_input_name not in (None, "") else None
    if project is None and shud_input is None:
        return None
    return {"project_name": project or shud_input or "", "shud_input_name": shud_input or project or ""}


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
    output_segment_count: int | None
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
    state_evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        contract_identity = _scheduler._candidate_production_identity(self)
        payload = {
            "production_identity_contract": production_identity_contract_evidence(contract_identity),
            "candidate_id": self.candidate_id,
            "source_id": self.source_id,
            "source": self.source_id,
            "cycle_id": self.cycle_id,
            "cycle_time_utc": _scheduler._format_utc(self.cycle_time_utc),
            "cycle_time": _scheduler._format_utc(self.cycle_time_utc),
            "model_id": self.model_id,
            "basin_id": self.basin_id,
            "basin_version_id": self.basin_version_id,
            "river_network_version_id": self.river_network_version_id,
            "segment_count": self.segment_count,
            "output_segment_count": self.output_segment_count,
            "model_package_uri": _scheduler._redact_secret_manifest_for_evidence(
                self.model_package_uri,
                "model_package_uri",
            ),
            "resource_profile": _scheduler._resource_profile_evidence(self.resource_profile),
            "display_capabilities": dict(self.display_capabilities),
            "frequency_capabilities": dict(self.frequency_capabilities),
            "horizon": dict(self.horizon),
            "scenario_id": self.scenario_id,
            "run_id": self.run_id,
            "canonical_product_id": contract_identity["canonical_product_id"],
            "forcing_version_id": self.forcing_version_id,
            "hydro_run_id": contract_identity["hydro_run_id"],
            "published_manifest_id": contract_identity["published_manifest_id"],
            "status": self.status,
            "reason": self.reason,
        }
        if contract_identity.get("pipeline_job_id") not in (None, ""):
            payload["pipeline_job_id"] = contract_identity["pipeline_job_id"]
        if self.state_evidence:
            payload["state_evidence"] = _scheduler._evidence_safe(self.state_evidence)
        return payload
