from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from packages.common.redaction import redact_payload, redact_text

SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:_-]{0,127}$")
SENSITIVE_PREFIX_ASSIGNMENT_RE = re.compile(
    r"(?:^|[;?#&/])[^=/?#;&]*(?:token|password|passwd|pwd|secret|credential|api[_-]?key|access[_-]?key|"
    r"session[_-]?key|signature|x-amz-signature)[^=/?#;&]*=",
    re.IGNORECASE,
)

DEFAULT_AUTH_MODE = "fallback_release_gated"
DEFAULT_REQUIRED_ROLES = ("operator", "model_admin", "source_admin", "tile_admin", "security_admin")
DEFAULT_ALERT_TARGET = "dry-run://ops-validation"
DEFAULT_DEPLOYMENT_CONFIG_SOURCE = "generated_deterministic_templates"
DEFAULT_ROLLBACK_SCOPE = "simulated_drills"
SERVICE_CONFIG_TEMPLATE = "docs/runbooks/production-service-config.md"
ROLLBACK_RUNBOOK = "docs/runbooks/rollback-drills.md"
MAX_EVIDENCE_PAYLOAD_BYTES = 768 * 1024
MAX_PERCENT_DECODE_ROUNDS = 4

SERVICE_CONFIGS = {
    "api": ("DATABASE_URL", "AUTH_BACKEND", "AUDIT_LOG_DESTINATION", "CORS_ALLOWED_ORIGINS"),
    "orchestrator": ("PIPELINE_DATABASE_URL", "OBJECT_STORE_PREFIX", "SLURM_GATEWAY_URL", "WORKSPACE_ROOT"),
    "slurm_gateway": ("SLURM_PARTITION", "SLURM_ACCOUNT", "SLURM_SHARED_LOG_ROOT", "SBATCH_TEMPLATE_ROOT"),
    "tile_publisher": ("TILE_OBJECT_PREFIX", "TILE_LAYER_REGISTRY", "TILE_ERROR_TOPIC"),
    "frontend": ("VITE_API_BASE_URL", "VITE_AUTH_MODE", "VITE_MAP_STYLE_URL"),
    "database": ("DATABASE_URL", "POSTGIS_ENABLED", "TIMESCALE_ENABLED", "MIGRATION_LOCK"),
    "object_store": ("OBJECT_STORE_ROOT", "OBJECT_STORE_PREFIX", "OBJECT_STORE_CREDENTIAL_SOURCE"),
    "source_adapters": ("GFS_CONFIG", "IFS_CONFIG", "ERA5_CONFIG", "CLDAS_RESTRICTED_REASON"),
    "workspace_roots": ("RUN_WORKSPACE_ROOT", "SHARED_LOG_ROOT", "ARTIFACT_RETENTION_POLICY"),
}
ACTION_MATRIX = {
    "model_activation": "model_admin",
    "rerun": "operator",
    "cancel": "operator",
    "qc_override": "model_admin",
    "source_config_change": "source_admin",
    "tile_republish": "tile_admin",
}
MONITORING_ALERTS = {
    "source_latency": {
        "metric": "nhms_source_cycle_latency_minutes",
        "severity": "warning",
        "observed": 95.0,
        "threshold": 60.0,
        "runbook": "docs/runbooks/source-latency.md",
        "action": "Check source availability, retry window, and best-available fallback lineage.",
    },
    "slurm_queue_backlog": {
        "metric": "nhms_slurm_queue_backlog_jobs",
        "severity": "critical",
        "observed": 120.0,
        "threshold": 80.0,
        "runbook": "docs/runbooks/slurm-backlog.md",
        "action": "Inspect partition health, array limits, fairshare, and cancel stale controlled failures.",
    },
    "failed_basin_retries": {
        "metric": "nhms_failed_basin_retry_count",
        "severity": "warning",
        "observed": 4.0,
        "threshold": 2.0,
        "runbook": "docs/runbooks/failed-basin-retry.md",
        "action": "Review basin stderr, retry class, and quarantine failed outputs before publication.",
    },
    "object_store_failure": {
        "metric": "nhms_object_store_write_failures",
        "severity": "critical",
        "observed": 1.0,
        "threshold": 0.0,
        "runbook": "docs/runbooks/object-store-failure.md",
        "action": "Stop imports, verify prefix permissions, and run cleanup rollback for partial manifests.",
    },
    "stale_analysis_state": {
        "metric": "nhms_analysis_state_age_minutes",
        "severity": "warning",
        "observed": 180.0,
        "threshold": 90.0,
        "runbook": "docs/runbooks/stale-analysis.md",
        "action": "Validate source cycle freshness and rerun analysis from the last accepted state snapshot.",
    },
    "tile_error": {
        "metric": "nhms_tile_publish_error_count",
        "severity": "critical",
        "observed": 3.0,
        "threshold": 0.0,
        "runbook": "docs/runbooks/tile-publish-error.md",
        "action": "Disable the bad layer version and republish from the last accepted tile artifact.",
    },
    "api_p95": {
        "metric": "nhms_api_p95_latency_ms",
        "severity": "warning",
        "observed": 325.0,
        "threshold": 250.0,
        "runbook": "docs/runbooks/api-latency.md",
        "action": "Inspect query plans, cache status, and recent object-store or DB latency changes.",
    },
}
ROLLBACK_DRILLS = {
    "bad_model_activation": {
        "command": "nhms-admin models deactivate --model-id <model> --restore-previous-version",
        "precondition": "New model activation produced failed runtime or QC evidence.",
        "recovery": "Previous active model version is restored and audit lineage links both versions.",
    },
    "failed_publish_import": {
        "command": "nhms-admin packages rollback --manifest <manifest> --quarantine-partial-objects",
        "precondition": "Package publish or registry import failed after partial object writes.",
        "recovery": "Partial objects are quarantined and registry activation remains unchanged.",
    },
    "failed_source_cycle": {
        "command": "nhms-admin sources mark-unavailable --cycle <cycle> --use-best-available",
        "precondition": "Source download or canonical conversion failed for an enabled source.",
        "recovery": "Best-available lineage points to the accepted fallback or explicit no-data state.",
    },
    "failed_slurm_array": {
        "command": "nhms-admin jobs retry-array --job-id <job> --failed-only",
        "precondition": "One or more Slurm array tasks failed while sibling outputs remain publishable.",
        "recovery": "Successful tasks stay immutable and failed tasks are retried or blocked with evidence.",
    },
    "bad_tile_release": {
        "command": "nhms-admin tiles rollback --layer <layer> --previous-version",
        "precondition": "Tile publication produced bad content type, stale data, or render errors.",
        "recovery": "Previous tile layer version is restored and the bad version is unpublished.",
    },
}
DEPENDENCY_CONTRACTS = {
    "slurm": {"issue": 147, "schema": "nhms.production_closure.slurm.v1", "allowed_statuses": {"ready", "submitted"}},
    "object_store": {
        "issue": 148,
        "schema": "nhms.production_closure.object_store.v1",
        "allowed_statuses": {"ready"},
    },
    "met": {"issue": 149, "schema": "nhms.production_closure.met.v1", "allowed_statuses": {"ready"}},
    "e2e": {"issue": 150, "schema": "nhms.production_closure.e2e.v1", "allowed_statuses": {"ready"}},
    "scale": {"issue": 151, "schema": "nhms.production_closure.scale.v1", "allowed_statuses": {"ready"}},
}
EXPLICIT_BLOCKED_DEPENDENCY_STATUSES = {"blocked", "failed", "failure", "error", "not_executed", "missing", "unknown"}
ACCEPTED_DEPENDENCY_RECEIPT_KEY = "accepted_dependency_evidence"
ACCEPTED_DEPENDENCY_RECEIPT_SCHEMA = "nhms.production_closure.ops.accepted_dependency_evidence.v1"
ACCEPTED_DEPENDENCY_EXECUTION_MODES = {"accepted_live_evidence", "live_executed", "consumed_live_evidence"}
LIVE_READY_DEPENDENCIES = frozenset(DEPENDENCY_CONTRACTS)
PRODUCER_LIVE_PROOF_CONTRACTS = {
    "slurm": {
        "execution_modes": {
            "accepted_live_evidence",
            "live_executed",
            "consumed_live_evidence",
            "live_slurm_submitted",
        },
        "required_true": ("live_slurm_executed",),
        "required_values": {"live_slurm_status": "executed"},
    },
    "object_store": {
        "execution_modes": {
            "accepted_live_evidence",
            "live_executed",
            "consumed_live_evidence",
            "live_registry_import_and_live_api",
        },
        "required_true": ("live_registry_import", "live_api"),
        "required_values": {"live_api_status": "executed"},
    },
    "met": {
        "execution_modes": {"accepted_live_evidence", "live_executed", "consumed_live_evidence", "live_source_ingest"},
        "required_true": ("live_met_executed",),
        "minimum_counts": {"live_source_count": 1},
    },
    "e2e": {
        "execution_modes": {"accepted_live_evidence", "live_executed", "consumed_live_evidence", "live_e2e_executed"},
        "required_true": (
            "live_db_executed",
            "live_api_executed",
            "live_slurm_executed",
            "live_frontend_executed",
        ),
    },
    "scale": {
        "execution_modes": {
            "accepted_live_evidence",
            "live_executed",
            "consumed_live_evidence",
            "live_scale_validation",
        },
        "required_true": ("live_db_executed", "live_api_executed", "live_frontend_executed"),
    },
}
ENCODED_SEPARATOR_RE = re.compile(r"%(?:2f|5c)", re.IGNORECASE)


class ProductionOpsValidationError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass
class EvidenceWriter:
    evidence_root: Path
    lane_dir: Path
    force: bool = False
    max_payload_bytes: int = MAX_EVIDENCE_PAYLOAD_BYTES
    _created_paths: set[Path] = field(default_factory=set)

    def prepare(self) -> None:
        _refuse_symlink_components(self.evidence_root)
        _refuse_symlink_components(self.lane_dir.parent)
        if self.lane_dir.exists() or self.lane_dir.is_symlink():
            _refuse_symlink_components(self.lane_dir)
            if not self.lane_dir.is_dir():
                raise ProductionOpsValidationError(
                    "PRODUCTION_OPS_EVIDENCE_PATH_UNSAFE",
                    f"Evidence lane path must be a directory: {self.lane_dir}.",
                )
            if any(self.lane_dir.iterdir()) and not self.force:
                raise ProductionOpsValidationError(
                    "PRODUCTION_OPS_EVIDENCE_EXISTS",
                    f"Evidence bundle already exists: {self.lane_dir}. Use --force to overwrite an existing run_id.",
                )
        resolved_lane = self.lane_dir.resolve(strict=False)
        try:
            resolved_lane.relative_to(self.evidence_root)
        except ValueError as error:
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_EVIDENCE_PATH_UNSAFE",
                "Evidence lane directory must stay under evidence root.",
            ) from error
        self.lane_dir.mkdir(parents=True, exist_ok=True)

    def write_json(self, path: Path, payload: Any) -> None:
        content = json.dumps(redact_payload(payload), indent=2, sort_keys=True).encode("utf-8") + b"\n"
        if len(content) > self.max_payload_bytes:
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_EVIDENCE_PAYLOAD_TOO_LARGE",
                f"Evidence payload exceeds configured limit of {self.max_payload_bytes} bytes.",
            )
        self._write_bytes(path, content)

    def _write_bytes(self, path: Path, content: bytes) -> None:
        safe_path = self._safe_file_path(path)
        if safe_path.exists() and safe_path not in self._created_paths and not self.force:
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_EVIDENCE_EXISTS",
                f"Evidence file already exists: {safe_path}. Use --force to overwrite an existing run_id bundle.",
            )
        temp_path = safe_path.with_name(f".{safe_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_bytes(content)
            os.replace(temp_path, safe_path)
            self._created_paths.add(safe_path)
        except OSError as error:
            temp_path.unlink(missing_ok=True)
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_EVIDENCE_WRITE_FAILED",
                f"Failed to write evidence file {safe_path}: {error}",
            ) from error

    def _safe_file_path(self, path: Path) -> Path:
        if path.is_symlink():
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_EVIDENCE_SYMLINK",
                f"Evidence file must not be a symlink: {path}",
            )
        _refuse_symlink_components(path.parent)
        resolved_parent = path.parent.resolve(strict=False)
        resolved_lane = self.lane_dir.resolve(strict=False)
        try:
            resolved_parent.relative_to(resolved_lane)
        except ValueError as error:
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_EVIDENCE_PATH_UNSAFE",
                "Evidence file path must stay under the current ops lane directory.",
            ) from error
        path.parent.mkdir(parents=True, exist_ok=True)
        return resolved_parent / path.name


@dataclass(frozen=True)
class ProductionOpsConfig:
    evidence_root: Path
    run_id: str
    auth_mode: str
    required_roles: tuple[str, ...]
    alert_target: str
    deployment_config_source: str
    rollback_scope: str
    dependency_roots: Mapping[str, Path | None]
    dependency_statuses: Mapping[str, str | None]
    force: bool = False

    @property
    def lane_dir(self) -> Path:
        return self.evidence_root / self.run_id / "ops"

    @classmethod
    def from_env(
        cls,
        *,
        evidence_root: Path,
        run_id: str | None,
        auth_mode: str | None = None,
        required_roles: str | None = None,
        alert_target: str | None = None,
        deployment_config_source: str | None = None,
        rollback_scope: str | None = None,
        slurm_evidence_root: Path | None = None,
        object_store_evidence_root: Path | None = None,
        met_evidence_root: Path | None = None,
        e2e_evidence_root: Path | None = None,
        scale_evidence_root: Path | None = None,
        dependency_statuses: str | None = None,
        force: bool = False,
    ) -> ProductionOpsConfig:
        resolved_evidence_root = _safe_resolved_evidence_root(evidence_root)
        resolved_run_id = _safe_run_id(run_id or datetime.now(UTC).strftime("m10-%Y%m%dT%H%M%SZ"))
        resolved_alert_target = alert_target or os.getenv("NHMS_PRODUCTION_OPS_ALERT_TARGET", DEFAULT_ALERT_TARGET)
        _validate_target_safe(resolved_alert_target, "alert_target", "PRODUCTION_OPS_ALERT_TARGET_UNSAFE")
        return cls(
            evidence_root=resolved_evidence_root,
            run_id=resolved_run_id,
            auth_mode=auth_mode or os.getenv("NHMS_PRODUCTION_OPS_AUTH_MODE", DEFAULT_AUTH_MODE),
            required_roles=_parse_csv_tuple(
                required_roles or os.getenv("NHMS_PRODUCTION_OPS_REQUIRED_ROLES"),
                DEFAULT_REQUIRED_ROLES,
            ),
            alert_target=resolved_alert_target,
            deployment_config_source=deployment_config_source
            or os.getenv("NHMS_PRODUCTION_OPS_DEPLOYMENT_CONFIG_SOURCE", DEFAULT_DEPLOYMENT_CONFIG_SOURCE),
            rollback_scope=rollback_scope or os.getenv("NHMS_PRODUCTION_OPS_ROLLBACK_SCOPE", DEFAULT_ROLLBACK_SCOPE),
            dependency_roots={
                "slurm": _dependency_root("NHMS_PRODUCTION_OPS_SLURM_EVIDENCE_ROOT", slurm_evidence_root),
                "object_store": _dependency_root(
                    "NHMS_PRODUCTION_OPS_OBJECT_STORE_EVIDENCE_ROOT",
                    object_store_evidence_root,
                ),
                "met": _dependency_root("NHMS_PRODUCTION_OPS_MET_EVIDENCE_ROOT", met_evidence_root),
                "e2e": _dependency_root("NHMS_PRODUCTION_OPS_E2E_EVIDENCE_ROOT", e2e_evidence_root),
                "scale": _dependency_root("NHMS_PRODUCTION_OPS_SCALE_EVIDENCE_ROOT", scale_evidence_root),
            },
            dependency_statuses=_parse_dependency_statuses(
                dependency_statuses or os.getenv("NHMS_PRODUCTION_OPS_DEPENDENCY_STATUSES")
            ),
            force=force,
        )


def validate_ops(config: ProductionOpsConfig) -> dict[str, Any]:
    config = replace(config, evidence_root=_safe_resolved_evidence_root(config.evidence_root))
    _validate_config(config)
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=config.force)
    writer.prepare()

    preflight = _preflight_payload(config)
    writer.write_json(config.lane_dir / "preflight.json", preflight)

    production_config = _production_config_evidence(config)
    writer.write_json(config.lane_dir / "config_validation.json", production_config)

    auth_rbac = _auth_rbac_evidence(config)
    writer.write_json(config.lane_dir / "auth_rbac.json", auth_rbac)
    release_blockers = _auth_release_blockers(config, auth_rbac)
    writer.write_json(config.lane_dir / "auth_release_blockers.json", release_blockers)

    audit = _audit_redaction_evidence(config, auth_rbac)
    writer.write_json(config.lane_dir / "audit_redaction.json", audit)

    monitoring = _monitoring_alert_evidence(config)
    writer.write_json(config.lane_dir / "monitoring_alerts.json", monitoring)

    rollback = _rollback_drill_evidence(config)
    writer.write_json(config.lane_dir / "rollback_drills.json", rollback)

    dependencies = _dependency_closure_evidence(config)
    writer.write_json(config.lane_dir / "dependency_closure.json", dependencies)

    environment = _environment_payload(config)
    writer.write_json(config.lane_dir / "environment.json", environment)

    blockers = _summary_blockers(production_config, auth_rbac, release_blockers, monitoring, rollback, dependencies)
    summary = _summary(
        config,
        status="ready" if not blockers else "release_blocked",
        blockers=blockers,
        production_config=production_config,
        auth_rbac=auth_rbac,
        monitoring=monitoring,
        rollback=rollback,
        dependencies=dependencies,
    )
    writer.write_json(config.lane_dir / "summary.json", summary)
    return summary


def _preflight_payload(config: ProductionOpsConfig) -> dict[str, Any]:
    return {
        "schema": "nhms.production_closure.ops.preflight.v1",
        "issue": 152,
        "run_id": config.run_id,
        "auth_mode": config.auth_mode,
        "required_roles": list(config.required_roles),
        "alert_target": _evidence_alert_target(config.alert_target),
        "deployment_config_source": config.deployment_config_source,
        "rollback_drill_scope": config.rollback_scope,
        "dependency_evidence": {
            name: {
                "root": str(config.dependency_roots.get(name)) if config.dependency_roots.get(name) else None,
                "explicit_status": config.dependency_statuses.get(name),
                "expected_issue": contract["issue"],
                "expected_schema": contract["schema"],
            }
            for name, contract in DEPENDENCY_CONTRACTS.items()
        },
        "evidence_root": str(config.evidence_root),
        "evidence_dir": str(config.lane_dir),
        "execution_policy": {
            "default_fast_path": "deterministic_fixture",
            "real_identity_provider_required": False,
            "external_material_required": False,
            "alert_sink_required": False,
            "object_store_required": False,
            "slurm_required": False,
            "postgis_api_frontend_required": False,
            "scheduler_required": False,
            "final_readiness_requires_live_controls_and_accepted_dependencies": True,
        },
    }


def _production_config_evidence(config: ProductionOpsConfig) -> dict[str, Any]:
    services = []
    blockers: list[dict[str, Any]] = []
    for service, required_settings in SERVICE_CONFIGS.items():
        settings = {}
        setting_source_metadata = []
        service_blockers = []
        for setting in required_settings:
            env_name = f"NHMS_PRODUCTION_OPS_{service.upper()}_{setting}"
            value = os.getenv(env_name)
            source = "environment"
            if value is None:
                value = _default_setting_value(config, service, setting)
                source = "generated_default"
            _validate_config_value_safe(value, service, setting)
            settings[setting] = value
            setting_source_metadata.append(
                {
                    "setting": setting,
                    "env_name": env_name,
                    "source": source,
                    "missing_required": source == "generated_default",
                    "generated_default": source == "generated_default",
                }
            )
            if source == "generated_default":
                service_blockers.append(
                    {
                        "error_code": "PRODUCTION_OPS_CONFIG_MISSING_SETTING",
                        "service": service,
                        "setting": setting,
                        "source": source,
                        "reason": "Required production setting was not supplied and was filled by a generated default.",
                    }
                )
            if _is_unsafe_setting(service, setting, value):
                service_blockers.append(
                    {
                        "error_code": "PRODUCTION_OPS_CONFIG_UNSAFE_SETTING",
                        "service": service,
                        "setting": setting,
                        "reason": (
                            "Setting uses deterministic or non-live fallback and cannot clear final release readiness."
                        ),
                    }
                )
        blockers.extend(service_blockers)
        services.append(
            {
                "service": service,
                "status": "blocked" if service_blockers else "ready",
                "template_source": config.deployment_config_source,
                "template_reference": SERVICE_CONFIG_TEMPLATE,
                "required_settings": list(required_settings),
                "settings": settings,
                "setting_source_metadata": setting_source_metadata,
                "blockers": service_blockers,
            }
        )
    return {
        "schema": "nhms.production_closure.ops.config_validation.v1",
        "run_id": config.run_id,
        "status": "blocked" if blockers else "ready",
        "services": services,
        "blockers": blockers,
    }


def _auth_rbac_evidence(config: ProductionOpsConfig) -> dict[str, Any]:
    live_backend_auth_executed = False
    model_activation = {
        "active_model_version": "previous-production-version",
        "qc_override_state": "not_overridden",
        "source_config_version": "previous-source-config",
        "tile_publication_version": "previous-tile-version",
        "pipeline_job_state": "unchanged",
    }
    decisions = []
    for action, required_role in ACTION_MATRIX.items():
        target = f"{action}:{config.run_id}"
        decisions.extend(
            [
                _action_decision(
                    config,
                    action,
                    required_role,
                    actor="ops-authorized",
                    role=required_role,
                    target=target,
                    decision="allowed",
                    reason="Policy simulation shows the configured role is sufficient.",
                    execution_mode="policy_simulated",
                    state=model_activation,
                ),
                _action_decision(
                    config,
                    action,
                    required_role,
                    actor="ops-viewer",
                    role="viewer",
                    target=target,
                    decision="denied",
                    reason="Actor role does not satisfy the required production role.",
                    execution_mode="policy_simulated",
                    error_code="PRODUCTION_OPS_RBAC_FORBIDDEN",
                    state=model_activation,
                ),
                _action_decision(
                    config,
                    action,
                    required_role,
                    actor="ops-release-gate",
                    role=required_role,
                    target=target,
                    decision="release_blocked",
                    reason="Full backend identity-provider enforcement was not executed in this deterministic lane.",
                    execution_mode="release_blocked",
                    error_code="PRODUCTION_OPS_BACKEND_AUTH_RELEASE_BLOCKED",
                    state=model_activation,
                ),
            ]
        )
    return {
        "schema": "nhms.production_closure.ops.auth_rbac.v1",
        "run_id": config.run_id,
        "status": "release_blocked",
        "auth_mode": config.auth_mode,
        "model_activation_boundary": {
            "backend_enforcement_available": False,
            "requested_auth_mode": config.auth_mode,
            "fallback_release_gate": True,
            "frontend_only_rbac_accepted_for_production": False,
        },
        "required_roles": list(config.required_roles),
        "action_decisions": decisions,
        "live_backend_auth_executed": live_backend_auth_executed,
        "execution_modes": sorted({item["execution_mode"] for item in decisions}),
        "state_mutation_assertions": {
            "denied_actions_mutated_state": False,
            "release_blocked_actions_mutated_state": False,
        },
        "blockers": [
            {
                "error_code": "PRODUCTION_OPS_BACKEND_AUTH_RELEASE_BLOCKED",
                "message": (
                    "Live backend auth/RBAC enforcement was not executed; final production readiness remains gated."
                ),
                "residual_risk": (
                    "Operator actions could rely on fallback or frontend-only gates if released without backend "
                    "enforcement."
                ),
                "removal_criteria": (
                    "Execute backend_route_executed evidence with a real identity provider and persisted audit "
                    "decisions."
                ),
            }
        ],
    }


def _action_decision(
    config: ProductionOpsConfig,
    action: str,
    required_role: str,
    *,
    actor: str,
    role: str,
    target: str,
    decision: str,
    reason: str,
    execution_mode: str,
    state: Mapping[str, Any],
    error_code: str | None = None,
) -> dict[str, Any]:
    mutated = decision == "allowed" and execution_mode == "backend_route_executed"
    return {
        "action": action,
        "actor": actor,
        "role": role,
        "target": target,
        "required_roles": [required_role],
        "decision": decision,
        "reason": reason,
        "error_code": error_code,
        "execution_mode": execution_mode,
        "live_backend_auth_executed": False,
        "previous_state": dict(state),
        "new_state": _new_state_for_action(state, action) if mutated else dict(state),
        "state_mutated": mutated,
        "lineage": {
            "run_id": config.run_id,
            "auth_mode": config.auth_mode,
            "audit_correlation_id": f"{config.run_id}-{action}-{decision}",
            "credential_hint": "token=deterministic-secret-for-redaction-test",
        },
    }


def _auth_release_blockers(config: ProductionOpsConfig, auth_rbac: Mapping[str, Any]) -> dict[str, Any]:
    blockers = []
    for action, required_role in ACTION_MATRIX.items():
        blockers.append(
            {
                "action": action,
                "required_roles": [required_role],
                "current_fallback": config.auth_mode,
                "residual_risk": (
                    "Production-impacting mutation is not proven against live backend identity and authorization."
                ),
                "removal_criteria": (
                    "Run backend_route_executed evidence for allowed and denied attempts with persisted audit rows "
                    "and no mutation for rejected attempts."
                ),
                "linked_decision_ids": [
                    item["lineage"]["audit_correlation_id"]
                    for item in auth_rbac["action_decisions"]
                    if item["action"] == action and item["decision"] == "release_blocked"
                ],
            }
        )
    return {
        "schema": "nhms.production_closure.ops.auth_release_blockers.v1",
        "run_id": config.run_id,
        "status": "release_blocked",
        "blockers": blockers,
    }


def _audit_redaction_evidence(config: ProductionOpsConfig, auth_rbac: Mapping[str, Any]) -> dict[str, Any]:
    audit_rows = []
    for decision in auth_rbac["action_decisions"]:
        audit_rows.append(
            {
                "actor": decision["actor"],
                "role": decision["role"],
                "target": decision["target"],
                "previous_state": decision["previous_state"],
                "new_state": decision["new_state"],
                "decision": decision["decision"],
                "reason": decision["reason"],
                "lineage": {
                    **decision["lineage"],
                    "api_payload": "password=deterministic-secret-for-redaction-test",
                    "alert_payload": (
                        f"{_evidence_alert_target(config.alert_target)}/"
                        "token=deterministic-secret-for-redaction-test"
                    ),
                    "frontend_output": "session_key=deterministic-secret-for-redaction-test",
                    "pr_evidence": "signed_url=https://example.test/object?X-Amz-Signature=deterministic-secret",
                },
            }
        )
    return {
        "schema": "nhms.production_closure.ops.audit_redaction.v1",
        "run_id": config.run_id,
        "status": "ready",
        "redaction_scope": [
            "config",
            "logs",
            "manifests",
            "audit_rows",
            "api_payloads",
            "alert_payloads",
            "pr_evidence",
            "frontend_output",
        ],
        "secret_shaped_values_redacted": True,
        "audit_rows": audit_rows,
    }


def _monitoring_alert_evidence(config: ProductionOpsConfig) -> dict[str, Any]:
    execution_mode = "dry_run_sink" if config.alert_target.startswith("dry-run://") else "not_executed"
    evidence_target = _evidence_alert_target(config.alert_target)
    alerts = []
    blockers = []
    for alert_name, fixture in MONITORING_ALERTS.items():
        _validate_finite_number(fixture["observed"], f"{alert_name}.observed")
        _validate_finite_number(fixture["threshold"], f"{alert_name}.threshold")
        alerts.append(
            {
                "alert": alert_name,
                "metric": fixture["metric"],
                "severity": fixture["severity"],
                "observed_value": fixture["observed"],
                "threshold": fixture["threshold"],
                "threshold_breached": fixture["observed"] > fixture["threshold"],
                "execution_mode": execution_mode,
                "live_alert_sink_delivered": False,
                "sink": evidence_target,
                "dry_run_target": evidence_target if execution_mode == "dry_run_sink" else None,
                "runbook_link": fixture["runbook"],
                "recommended_operator_action": fixture["action"],
            }
        )
    blockers.append(
        {
            "error_code": "PRODUCTION_OPS_LIVE_ALERT_SINK_RELEASE_BLOCKED",
            "message": "Alert payloads were recorded in dry-run/not-executed mode; live sink delivery is not proven.",
            "removal_criteria": "Deliver the alert matrix to the production alert sink and archive delivery receipts.",
        }
    )
    return {
        "schema": "nhms.production_closure.ops.monitoring_alerts.v1",
        "run_id": config.run_id,
        "status": "release_blocked",
        "alerts": alerts,
        "live_alert_sink_delivered": False,
        "blockers": blockers,
    }


def _rollback_drill_evidence(config: ProductionOpsConfig) -> dict[str, Any]:
    execution_mode = "simulated_drill"
    live_rollback_executed = False
    drills = []
    for drill, fixture in ROLLBACK_DRILLS.items():
        drills.append(
            {
                "drill": drill,
                "command": fixture["command"],
                "precondition": fixture["precondition"],
                "expected_evidence": [
                    "audit_redaction.json",
                    "dependency_closure.json",
                    "summary.json",
                ],
                "recovery_result": fixture["recovery"],
                "residual_risk": (
                    "Simulated drill only; live rollback execution evidence is required before final readiness."
                ),
                "dependency_artifact_references": _dependency_artifact_references(config, drill),
                "runbook_link": ROLLBACK_RUNBOOK,
                "requested_scope": config.rollback_scope,
                "execution_mode": execution_mode,
                "live_rollback_executed": live_rollback_executed,
            }
        )
    blockers = []
    blockers.append(
        {
            "error_code": "PRODUCTION_OPS_ROLLBACK_DRILL_RELEASE_BLOCKED",
            "message": "Rollback drills are simulated; live rollback execution evidence is not present.",
            "removal_criteria": (
                "Run each drill in a production-like environment and archive command output and recovery state."
            ),
        }
    )
    return {
        "schema": "nhms.production_closure.ops.rollback_drills.v1",
        "run_id": config.run_id,
        "status": "release_blocked",
        "requested_scope": config.rollback_scope,
        "drills": drills,
        "live_rollback_executed": live_rollback_executed,
        "blockers": blockers,
    }


def _dependency_closure_evidence(config: ProductionOpsConfig) -> dict[str, Any]:
    dependencies = []
    blockers = []
    for name in DEPENDENCY_CONTRACTS:
        dependency = _read_dependency(name, config.dependency_roots.get(name), config.dependency_statuses.get(name))
        dependencies.append(dependency)
        if dependency["status"] != "accepted":
            blockers.append(
                {
                    "error_code": "PRODUCTION_OPS_DEPENDENCY_NOT_ACCEPTED",
                    "dependency": name,
                    "status": dependency["status"],
                    "reason": dependency["reason"],
                }
            )
    return {
        "schema": "nhms.production_closure.ops.dependency_closure.v1",
        "run_id": config.run_id,
        "status": "accepted" if not blockers else "release_blocked",
        "dependencies": dependencies,
        "deterministic_fixture": any(item["status"] == "skipped" for item in dependencies),
        "final_production_readiness_claimed": False,
        "blockers": blockers,
    }


def _summary(
    config: ProductionOpsConfig,
    *,
    status: str,
    blockers: list[dict[str, Any]],
    production_config: Mapping[str, Any],
    auth_rbac: Mapping[str, Any],
    monitoring: Mapping[str, Any],
    rollback: Mapping[str, Any],
    dependencies: Mapping[str, Any],
) -> dict[str, Any]:
    final_ready = (
        status == "ready"
        and auth_rbac["live_backend_auth_executed"]
        and monitoring["live_alert_sink_delivered"]
        and rollback["live_rollback_executed"]
        and dependencies["status"] == "accepted"
        and production_config["status"] == "ready"
    )
    return {
        "schema": "nhms.production_closure.ops.v1",
        "issue": 152,
        "run_id": config.run_id,
        "status": "ready" if final_ready else "release_blocked",
        "evidence_dir": str(config.lane_dir),
        "auth_mode": config.auth_mode,
        "required_roles": list(config.required_roles),
        "final_production_readiness_claimed": final_ready,
        "live_backend_auth_executed": auth_rbac["live_backend_auth_executed"],
        "live_alert_sink_delivered": monitoring["live_alert_sink_delivered"],
        "live_rollback_executed": rollback["live_rollback_executed"],
        "dependency_status": dependencies["status"],
        "release_blockers": blockers,
        "files": [
            "preflight.json",
            "config_validation.json",
            "auth_rbac.json",
            "auth_release_blockers.json",
            "audit_redaction.json",
            "monitoring_alerts.json",
            "rollback_drills.json",
            "dependency_closure.json",
            "environment.json",
            "summary.json",
        ],
    }


def _summary_blockers(
    production_config: Mapping[str, Any],
    auth_rbac: Mapping[str, Any],
    release_blockers: Mapping[str, Any],
    monitoring: Mapping[str, Any],
    rollback: Mapping[str, Any],
    dependencies: Mapping[str, Any],
) -> list[dict[str, Any]]:
    blockers = []
    for evidence in (production_config, auth_rbac, release_blockers, monitoring, rollback, dependencies):
        blockers.extend(evidence.get("blockers", []))
    return blockers


def _environment_payload(config: ProductionOpsConfig) -> dict[str, Any]:
    env_keys = [
        "NHMS_RUN_PRODUCTION_CLOSURE",
        "NHMS_PRODUCTION_OPS_AUTH_MODE",
        "NHMS_PRODUCTION_OPS_REQUIRED_ROLES",
        "NHMS_PRODUCTION_OPS_ALERT_TARGET",
        "NHMS_PRODUCTION_OPS_DEPLOYMENT_CONFIG_SOURCE",
        "NHMS_PRODUCTION_OPS_ROLLBACK_SCOPE",
        "NHMS_PRODUCTION_OPS_DEPENDENCY_STATUSES",
        "DATABASE_URL",
        "AUTH_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
    ]
    return {
        "schema": "nhms.production_closure.ops.environment.v1",
        "run_id": config.run_id,
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": str(Path.cwd()),
        "env": {
            key: _environment_value_for_evidence(key, os.getenv(key, ""))
            for key in env_keys
            if key in os.environ
        },
        "redaction": {
            "secret_shaped_values_redacted": True,
            "stdout_redacted": True,
            "evidence_redacted": True,
        },
    }


def _read_dependency(name: str, root: Path | None, explicit_status: str | None) -> dict[str, Any]:
    contract = DEPENDENCY_CONTRACTS[name]
    if explicit_status:
        status = _dependency_status_from_explicit(explicit_status)
        return {
            "dependency": name,
            "issue": contract["issue"],
            "expected_schema": contract["schema"],
            "status": status,
            "execution_mode": "explicit_status",
            "summary_path": None,
            "summary_status": explicit_status,
            "deterministic_fixture": explicit_status == "skipped",
            "final_production_readiness_claimed": False,
            "reason": f"Explicit dependency status {explicit_status!r} supplied for ops readiness.",
        }
    if root is None:
        return {
            "dependency": name,
            "issue": contract["issue"],
            "expected_schema": contract["schema"],
            "status": "skipped",
            "execution_mode": "deterministic_fixture",
            "summary_path": None,
            "deterministic_fixture": True,
            "final_production_readiness_claimed": False,
            "reason": "No accepted dependency evidence root was supplied; deterministic fast-path summary used.",
        }
    try:
        summary_path = _dependency_summary_path(root, name)
    except ProductionOpsValidationError as error:
        return _invalid_dependency(name, root, "blocked", error.message, error_code=error.error_code)
    if summary_path is None:
        return _invalid_dependency(
            name,
            root,
            "not_executed",
            "Dependency evidence root has no summary.json.",
            error_code="PRODUCTION_OPS_DEPENDENCY_SUMMARY_MISSING",
        )
    try:
        summary, summary_sha256 = _read_dependency_summary_json(summary_path)
    except ProductionOpsValidationError as error:
        return _invalid_dependency(name, summary_path, "blocked", error.message, error_code=error.error_code)
    except (OSError, json.JSONDecodeError) as error:
        return _invalid_dependency(
            name,
            summary_path,
            "blocked",
            f"Dependency summary could not be read: {error}",
            error_code="PRODUCTION_OPS_DEPENDENCY_SUMMARY_INVALID",
        )
    if not isinstance(summary, Mapping):
        return _invalid_dependency(
            name,
            summary_path,
            "blocked",
            "Dependency summary JSON must be an object.",
            error_code="PRODUCTION_OPS_DEPENDENCY_SUMMARY_INVALID",
        )
    schema_matches = summary.get("schema") == contract["schema"]
    issue_matches = summary.get("issue") == contract["issue"]
    summary_status = str(summary.get("status", "unknown"))
    if not schema_matches or not issue_matches:
        return _invalid_dependency(
            name,
            summary_path,
            "blocked",
            (
                f"Dependency summary must be issue #{contract['issue']} with schema {contract['schema']}; "
                f"got issue={summary.get('issue')!r}, schema={summary.get('schema')!r}."
            ),
            summary=summary,
            error_code="PRODUCTION_OPS_DEPENDENCY_CONTRACT_MISMATCH",
        )
    if summary_status in EXPLICIT_BLOCKED_DEPENDENCY_STATUSES:
        status = summary_status if summary_status in {"blocked", "not_executed"} else "blocked"
        return _dependency_from_summary(
            name,
            contract,
            summary_path,
            summary,
            status=status,
            execution_mode="not_executed",
            deterministic_fixture=_summary_has_deterministic_evidence(summary),
            final_production_readiness_claimed=False,
            error_code="PRODUCTION_OPS_DEPENDENCY_NOT_ACCEPTED",
            reason=f"Dependency summary status {summary_status!r} is not accepted for final ops readiness.",
        )
    if summary_status not in contract["allowed_statuses"]:
        return _dependency_from_summary(
            name,
            contract,
            summary_path,
            summary,
            status="blocked",
            execution_mode="not_executed",
            deterministic_fixture=_summary_has_deterministic_evidence(summary),
            final_production_readiness_claimed=False,
            error_code="PRODUCTION_OPS_DEPENDENCY_NOT_ACCEPTED",
            reason=f"Dependency summary status {summary_status!r} is not accepted for final ops readiness.",
        )
    try:
        receipt_path = _dependency_acceptance_receipt_path(root, name, summary_path)
    except ProductionOpsValidationError as error:
        return _invalid_dependency(name, root, "blocked", error.message, error_code=error.error_code)
    if receipt_path is None:
        accepted = False
        reason = (
            "Dependency summary is missing external accepted_dependency_evidence.json receipt under the "
            "dependency evidence root."
        )
        receipt = None
    else:
        try:
            receipt = _read_dependency_receipt_json(receipt_path)
        except ProductionOpsValidationError as error:
            return _invalid_dependency(name, receipt_path, "blocked", error.message, error_code=error.error_code)
        except (OSError, json.JSONDecodeError) as error:
            return _invalid_dependency(
                name,
                receipt_path,
                "blocked",
                f"Dependency acceptance receipt could not be read: {error}",
                error_code="PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_INVALID",
            )
        if not isinstance(receipt, Mapping):
            return _invalid_dependency(
                name,
                receipt_path,
                "blocked",
                "Dependency acceptance receipt JSON must be an object.",
                error_code="PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_INVALID",
            )
        accepted, reason = _has_accepted_dependency_evidence(
            name,
            contract,
            summary,
            receipt,
            summary_path=summary_path,
            summary_sha256=summary_sha256,
            receipt_path=receipt_path,
        )
        receipt = {**receipt, "receipt_path": str(receipt_path)}
    if not accepted:
        deterministic_fixture = _summary_has_deterministic_evidence(summary)
        return _dependency_from_summary(
            name,
            contract,
            summary_path,
            summary,
            status="skipped" if deterministic_fixture else "blocked",
            execution_mode=_dependency_execution_mode(summary, deterministic_fixture),
            deterministic_fixture=deterministic_fixture,
            final_production_readiness_claimed=False,
            error_code="PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_MISSING",
            reason=reason,
        )
    return _dependency_from_summary(
        name,
        contract,
        summary_path,
        summary,
        status="accepted",
        execution_mode=str(receipt["execution_mode"]),
        deterministic_fixture=False,
        final_production_readiness_claimed=False,
        reason=(
            "Accepted production closure dependency summary consumed with external non-deterministic "
            "ops acceptance receipt."
        ),
        receipt=receipt,
    )


def _dependency_from_summary(
    name: str,
    contract: Mapping[str, Any],
    summary_path: Path,
    summary: Mapping[str, Any],
    *,
    status: str,
    execution_mode: str,
    deterministic_fixture: bool,
    final_production_readiness_claimed: bool,
    reason: str,
    error_code: str | None = None,
    receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "dependency": name,
        "issue": contract["issue"],
        "schema": summary.get("schema"),
        "status": status,
        "execution_mode": execution_mode,
        "summary_path": str(summary_path),
        "summary_status": str(summary.get("status", "unknown")),
        "run_id": summary.get("run_id"),
        "evidence_dir": summary.get("evidence_dir"),
        "deterministic_fixture": deterministic_fixture,
        "final_production_readiness_claimed": final_production_readiness_claimed,
        "summary_final_production_readiness_claimed": bool(summary.get("final_production_readiness_claimed", False)),
        "reason": reason,
    }
    if error_code is not None:
        payload["error_code"] = error_code
    if receipt is not None:
        payload["accepted_dependency_evidence"] = {
            "schema": receipt["schema"],
            "receipt_id": receipt["receipt_id"],
            "accepted_at": receipt["accepted_at"],
            "execution_mode": receipt["execution_mode"],
            "deterministic_fixture": receipt["deterministic_fixture"],
            "final_production_readiness_claimed": receipt["final_production_readiness_claimed"],
            "receipt_path": receipt["receipt_path"],
            "summary_sha256": receipt["summary_sha256"],
        }
    return payload


def _has_accepted_dependency_evidence(
    name: str,
    contract: Mapping[str, Any],
    summary: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    summary_path: Path,
    summary_sha256: str,
    receipt_path: Path,
) -> tuple[bool, str]:
    if _summary_has_deterministic_evidence(summary):
        return False, "Dependency summary is deterministic/non-live evidence and cannot be accepted by ops closure."
    if summary.get("final_production_readiness_claimed") is True:
        return False, "Dependency summary must not claim final production readiness."
    if name in LIVE_READY_DEPENDENCIES and not _summary_has_accepted_live_proof(name, summary):
        return (
            False,
            "Dependency summary is missing producer-level non-deterministic/live execution proof; "
            "sidecar receipt alone is not sufficient.",
        )
    required = (
        "schema",
        "accepted",
        "dependency",
        "issue",
        "summary_schema",
        "summary_run_id",
        "summary_path",
        "summary_sha256",
        "receipt_id",
        "accepted_at",
        "execution_mode",
        "deterministic_fixture",
        "final_production_readiness_claimed",
    )
    missing = [field for field in required if field not in receipt]
    if missing:
        return False, f"Dependency accepted-evidence receipt is missing fields: {', '.join(missing)}."
    if receipt.get("schema") != ACCEPTED_DEPENDENCY_RECEIPT_SCHEMA:
        return False, "Dependency accepted-evidence receipt schema is not supported."
    if receipt.get("accepted") is not True:
        return False, "Dependency accepted-evidence receipt must set accepted=true."
    if receipt.get("dependency") != name:
        return False, "Dependency accepted-evidence receipt dependency binding does not match."
    if receipt.get("issue") != contract["issue"]:
        return False, "Dependency accepted-evidence receipt issue binding does not match."
    if receipt.get("summary_schema") != contract["schema"]:
        return False, "Dependency accepted-evidence receipt schema binding does not match."
    if receipt.get("summary_run_id") != summary.get("run_id"):
        return False, "Dependency accepted-evidence receipt run_id binding does not match."
    if receipt.get("summary_path") != str(summary_path):
        return False, "Dependency accepted-evidence receipt summary_path binding does not match."
    if receipt.get("summary_sha256") != summary_sha256:
        return False, "Dependency accepted-evidence receipt checksum binding does not match."
    if receipt.get("final_production_readiness_claimed") is not False:
        return False, "Dependency accepted-evidence receipt must set final_production_readiness_claimed=false."
    if receipt.get("deterministic_fixture") is not False:
        return False, "Dependency accepted-evidence receipt must set deterministic_fixture=false."
    execution_mode = str(receipt.get("execution_mode", ""))
    if execution_mode not in ACCEPTED_DEPENDENCY_EXECUTION_MODES:
        return False, "Dependency accepted-evidence receipt must use a non-deterministic accepted execution mode."
    if _is_deterministic_execution_mode(execution_mode):
        return False, "Dependency accepted-evidence receipt execution mode must not be deterministic."
    if _is_deterministic_execution_mode(str(receipt.get("receipt_id", ""))):
        return False, "Dependency accepted-evidence receipt_id must not be deterministic fixture material."
    for receipt_field in ("receipt_id", "accepted_at"):
        if not str(receipt.get(receipt_field, "")).strip():
            return False, f"Dependency accepted-evidence receipt field {receipt_field} must be non-empty."
    return True, "Accepted dependency evidence receipt is present."


def _summary_has_deterministic_evidence(summary: Mapping[str, Any]) -> bool:
    if summary.get("deterministic_fixture") is True:
        return True
    if summary.get("live_registry_import") is False or summary.get("live_api") is False:
        return True
    if summary.get("final_production_readiness_claimed") is False and summary.get("deterministic_fixture") is not False:
        return True
    for key, value in _walk_summary_fields(summary):
        if key in {"execution_mode", "configured_execution_mode", "cached_fallback_policy", "dataset_source"}:
            if _is_deterministic_execution_mode(str(value)):
                return True
        if key.startswith("live_") and value is False:
            return True
        if key.startswith("live_") and key.endswith("_executed") and value is False:
            return True
        if key.startswith("live_") and key.endswith("_delivered") and value is False:
            return True
        if key.startswith("live_") and key.endswith("_status") and str(value) != "executed":
            return True
    return False


def _summary_has_accepted_live_proof(name: str, summary: Mapping[str, Any]) -> bool:
    if summary.get("deterministic_fixture") is not False:
        return False
    if summary.get("final_production_readiness_claimed") is not False:
        return False
    contract = PRODUCER_LIVE_PROOF_CONTRACTS.get(name)
    if contract is None:
        return False
    execution_mode = str(summary.get("execution_mode", ""))
    if execution_mode not in contract["execution_modes"]:
        return False
    if _is_deterministic_execution_mode(execution_mode):
        return False
    for required_field in contract.get("required_true", ()):
        if summary.get(required_field) is not True:
            return False
    for required_field, expected in contract.get("required_values", {}).items():
        if summary.get(required_field) != expected:
            return False
    for required_field, minimum in contract.get("minimum_counts", {}).items():
        value = summary.get(required_field)
        if not isinstance(value, int) or value < minimum:
            return False
    return True


def _walk_summary_fields(value: Any) -> tuple[tuple[str, Any], ...]:
    fields: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            fields.append((str(key), nested))
            fields.extend(_walk_summary_fields(nested))
    elif isinstance(value, list):
        for nested in value:
            fields.extend(_walk_summary_fields(nested))
    return tuple(fields)


def _is_deterministic_execution_mode(value: str) -> bool:
    normalized = value.strip().lower()
    return any(marker in normalized for marker in ("deterministic", "fixture", "simulated", "dry_run", "not_executed"))


def _dependency_execution_mode(summary: Mapping[str, Any], deterministic_fixture: bool) -> str:
    mode = summary.get("execution_mode")
    if isinstance(mode, str) and mode:
        return mode
    if deterministic_fixture:
        return "deterministic_fixture"
    return "not_executed"


def _invalid_dependency(
    name: str,
    path: Path,
    status: str,
    reason: str,
    *,
    summary: Mapping[str, Any] | None = None,
    error_code: str = "PRODUCTION_OPS_DEPENDENCY_NOT_ACCEPTED",
) -> dict[str, Any]:
    contract = DEPENDENCY_CONTRACTS[name]
    return {
        "dependency": name,
        "issue": contract["issue"],
        "expected_schema": contract["schema"],
        "status": status,
        "execution_mode": "not_executed",
        "summary_path": str(path),
        "summary_status": summary.get("status", "unknown") if summary else "unknown",
        "error_code": error_code,
        "deterministic_fixture": False,
        "final_production_readiness_claimed": False,
        "reason": reason,
    }


def _dependency_summary_path(root: Path, name: str) -> Path | None:
    resolved_root = _safe_dependency_root(root)
    candidates = [resolved_root / "summary.json", resolved_root / name / "summary.json"]
    if name == "object_store":
        candidates.append(resolved_root / "object-store" / "summary.json")
    for candidate in candidates:
        if candidate.is_symlink():
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK",
                f"Dependency summary must not be a symlink: {candidate}",
            )
        _refuse_dependency_symlink_components(candidate.parent)
        if candidate.exists():
            if not candidate.is_file():
                continue
            resolved_candidate = candidate.resolve(strict=True)
            try:
                resolved_candidate.relative_to(resolved_root)
            except ValueError as error:
                raise ProductionOpsValidationError(
                    "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_PATH_UNSAFE",
                    "Dependency summary file must stay under its supplied dependency root.",
                ) from error
            return resolved_candidate
    return None


def _dependency_acceptance_receipt_path(root: Path, name: str, summary_path: Path) -> Path | None:
    resolved_root = _safe_dependency_root(root)
    candidates = [
        summary_path.parent / "accepted_dependency_evidence.json",
        resolved_root / "accepted_dependency_evidence.json",
        resolved_root / name / "accepted_dependency_evidence.json",
    ]
    if name == "object_store":
        candidates.append(resolved_root / "object-store" / "accepted_dependency_evidence.json")
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_symlink():
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK",
                f"Dependency acceptance receipt must not be a symlink: {candidate}",
            )
        _refuse_dependency_symlink_components(candidate.parent)
        if not candidate.exists():
            continue
        if not candidate.is_file():
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_INVALID",
                f"Dependency acceptance receipt must be a file: {candidate}",
            )
        resolved_candidate = candidate.resolve(strict=True)
        try:
            resolved_candidate.relative_to(resolved_root)
        except ValueError as error:
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_PATH_UNSAFE",
                "Dependency acceptance receipt must stay under its supplied dependency root.",
            ) from error
        return resolved_candidate
    return None


def _dependency_status_from_explicit(value: str) -> str:
    normalized = value.strip().lower()
    allowed = {"skipped", "blocked", "not_executed"}
    if normalized not in allowed:
        raise ProductionOpsValidationError(
            "PRODUCTION_OPS_DEPENDENCY_STATUS_INVALID",
            "Dependency statuses must be skipped, blocked, or not_executed; accepted requires a validated summary.",
        )
    return normalized


def _dependency_artifact_references(config: ProductionOpsConfig, drill: str) -> list[dict[str, Any]]:
    references = []
    for name, root in config.dependency_roots.items():
        if root is not None:
            try:
                summary = _dependency_summary_path(root, name)
            except ProductionOpsValidationError as error:
                references.append(
                    {
                        "dependency": name,
                        "drill": drill,
                        "summary": str(root),
                        "error_code": error.error_code,
                        "status": "blocked",
                    }
                )
            else:
                references.append(
                    {
                        "dependency": name,
                        "drill": drill,
                        "summary": str(summary if summary is not None else root / "summary.json"),
                    }
                )
    if not references:
        references.append(
            {
                "dependency": "deterministic_fixture",
                "drill": drill,
                "summary": "No dependency root supplied; see dependency_closure.json deterministic_fixture records.",
            }
        )
    return references


def _safe_dependency_root(root: Path) -> Path:
    expanded = root.expanduser()
    _refuse_dependency_symlink_components(expanded)
    resolved_root = expanded.resolve(strict=False)
    if expanded.exists() and not expanded.is_dir():
        raise ProductionOpsValidationError(
            "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_PATH_UNSAFE",
            f"Dependency evidence root must be a directory: {expanded}",
        )
    return resolved_root


def _refuse_dependency_symlink_components(path: Path) -> None:
    try:
        _refuse_symlink_components(path)
    except ProductionOpsValidationError as error:
        raise ProductionOpsValidationError("PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK", error.message) from error


def _read_dependency_summary_json(summary_path: Path) -> tuple[Any, str]:
    return _read_bounded_json_with_digest(
        summary_path,
        too_large_code="PRODUCTION_OPS_DEPENDENCY_SUMMARY_TOO_LARGE",
        too_large_message="Dependency summary exceeds configured limit of {limit} bytes.",
    )


def _read_dependency_receipt_json(receipt_path: Path) -> Any:
    return _read_bounded_json(
        receipt_path,
        too_large_code="PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_TOO_LARGE",
        too_large_message="Dependency acceptance receipt exceeds configured limit of {limit} bytes.",
    )


def _read_bounded_json(path: Path, *, too_large_code: str, too_large_message: str) -> Any:
    parsed, _digest = _read_bounded_json_with_digest(
        path,
        too_large_code=too_large_code,
        too_large_message=too_large_message,
    )
    return parsed


def _read_bounded_json_with_digest(path: Path, *, too_large_code: str, too_large_message: str) -> tuple[Any, str]:
    with path.open("rb") as handle:
        content = handle.read(MAX_EVIDENCE_PAYLOAD_BYTES + 1)
    if len(content) > MAX_EVIDENCE_PAYLOAD_BYTES:
        raise ProductionOpsValidationError(
            too_large_code,
            too_large_message.format(limit=MAX_EVIDENCE_PAYLOAD_BYTES),
        )
    return json.loads(content.decode("utf-8")), hashlib.sha256(content).hexdigest()


def _default_setting_value(config: ProductionOpsConfig, service: str, setting: str) -> str:
    if "URL" in setting:
        return f"deterministic://{service}/{setting.lower()}"
    if "ROOT" in setting:
        return str(config.lane_dir / "workspace" / service / setting.lower())
    if "PREFIX" in setting:
        return f"s3://nhms-production-like/{config.run_id}/{service}"
    if setting.endswith("ENABLED"):
        return "true"
    if setting.endswith("REASON"):
        return "restricted source requires production credential approval"
    return f"deterministic-{service}-{setting.lower()}"


def _is_unsafe_setting(service: str, setting: str, value: str) -> bool:
    return value.startswith("deterministic://") or "deterministic-" in value or (
        service == "frontend" and setting == "VITE_AUTH_MODE"
    )


def _new_state_for_action(state: Mapping[str, Any], action: str) -> dict[str, Any]:
    updated = dict(state)
    if action == "model_activation":
        updated["active_model_version"] = "new-production-version"
    elif action == "rerun":
        updated["pipeline_job_state"] = "rerun_requested"
    elif action == "cancel":
        updated["pipeline_job_state"] = "cancel_requested"
    elif action == "qc_override":
        updated["qc_override_state"] = "override_requested"
    elif action == "source_config_change":
        updated["source_config_version"] = "new-source-config"
    elif action == "tile_republish":
        updated["tile_publication_version"] = "republish_requested"
    return updated


def _validate_config(config: ProductionOpsConfig) -> None:
    _validate_identifier(config.auth_mode, "auth_mode")
    _validate_identifier(config.deployment_config_source, "deployment_config_source")
    _validate_identifier(config.rollback_scope, "rollback_scope")
    for status in config.dependency_statuses.values():
        if status:
            _dependency_status_from_explicit(status)
    for role in config.required_roles:
        _validate_identifier(role, "required_role")
    missing_roles = sorted(set(ACTION_MATRIX.values()) - set(config.required_roles))
    if missing_roles:
        raise ProductionOpsValidationError(
            "PRODUCTION_OPS_REQUIRED_ROLES_INCOMPLETE",
            f"Required roles must include action roles: {', '.join(missing_roles)}.",
        )


def _evidence_alert_target(value: str) -> str:
    parsed = urlsplit(value)
    identity = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "redacted-alert-target"
    if parsed.scheme == "dry-run" and parsed.path in {"", "/"}:
        return identity
    return f"{identity}/[redacted-alert-path]"


def _environment_value_for_evidence(key: str, value: str) -> str:
    if key == "NHMS_PRODUCTION_OPS_ALERT_TARGET":
        return _evidence_alert_target(value)
    return value


def _validate_identifier(value: str, field_name: str) -> None:
    if not SAFE_IDENTIFIER_RE.fullmatch(value):
        raise ProductionOpsValidationError(
            "PRODUCTION_OPS_IDENTIFIER_UNSAFE",
            f"{field_name} must be a safe production ops identifier.",
        )


def _validate_config_value_safe(value: str, service: str, setting: str) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ProductionOpsValidationError(
            "PRODUCTION_OPS_CONFIG_VALUE_UNSAFE",
            f"{service}.{setting} must not contain invalid credential material.",
        ) from error
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProductionOpsValidationError(
            "PRODUCTION_OPS_CONFIG_VALUE_UNSAFE",
            f"{service}.{setting} must not contain userinfo credentials, query parameters, or fragments.",
        )
    for decoded in _canonical_decode_steps(value, "PRODUCTION_OPS_CONFIG_VALUE_UNSAFE"):
        if SENSITIVE_PREFIX_ASSIGNMENT_RE.search(decoded):
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_CONFIG_VALUE_UNSAFE",
                f"{service}.{setting} must not contain credential assignments.",
            )
    if _is_path_or_root_setting(setting):
        _guard_canonical_path_segments(
            value,
            error_code="PRODUCTION_OPS_CONFIG_VALUE_UNSAFE",
            field_name=f"{service}.{setting}",
        )


def _is_path_or_root_setting(setting: str) -> bool:
    return setting.endswith("ROOT") or setting.endswith("PREFIX") or "PATH" in setting


def _validate_target_safe(value: str, field_name: str, error_code: str) -> None:
    if not value:
        raise ProductionOpsValidationError(error_code, f"{field_name} must not be empty.")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ProductionOpsValidationError(error_code, f"{field_name} must not contain credential material.") from error
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProductionOpsValidationError(
            error_code,
            f"{field_name} must not contain userinfo credentials, query parameters, or fragments.",
        )
    _guard_canonical_path_segments(value, error_code=error_code, field_name=field_name)


def _guard_canonical_path_segments(value: str, *, error_code: str, field_name: str) -> None:
    for decoded in _canonical_decode_steps(value, error_code):
        if ENCODED_SEPARATOR_RE.search(decoded):
            raise ProductionOpsValidationError(error_code, f"{field_name} path must not contain encoded separators.")
        if SENSITIVE_PREFIX_ASSIGNMENT_RE.search(decoded):
            raise ProductionOpsValidationError(error_code, f"{field_name} must not contain credential assignments.")
        parsed = urlsplit(decoded)
        if parsed.username or parsed.password:
            raise ProductionOpsValidationError(error_code, f"{field_name} must not contain userinfo credentials.")
        _guard_url_authority(parsed.netloc, error_code=error_code, field_name=field_name)
        for segment in parsed.path.split("/"):
            if segment in {".", ".."} or "\\" in segment:
                raise ProductionOpsValidationError(error_code, f"{field_name} path must not contain traversal.")


def _guard_url_authority(netloc: str, *, error_code: str, field_name: str) -> None:
    if not netloc:
        return
    if "/" in netloc or "\\" in netloc:
        raise ProductionOpsValidationError(error_code, f"{field_name} URL authority must not contain separators.")
    host = netloc.rsplit("@", maxsplit=1)[-1].split(":", maxsplit=1)[0]
    if host in {".", ".."}:
        raise ProductionOpsValidationError(error_code, f"{field_name} URL authority must not contain traversal.")
    if any(segment in {".", ".."} for segment in host.split(".")):
        raise ProductionOpsValidationError(error_code, f"{field_name} URL authority must not contain dot segments.")


def _canonical_decode_steps(value: str, error_code: str) -> tuple[str, ...]:
    steps = [value]
    current = value
    for _ in range(MAX_PERCENT_DECODE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            break
        steps.append(decoded)
        current = decoded
    if unquote(current) != current:
        raise ProductionOpsValidationError(error_code, "Value contains over-encoded percent escapes.")
    return tuple(steps)


def _validate_finite_number(value: Any, field_name: str) -> None:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ProductionOpsValidationError(
            "PRODUCTION_OPS_METRIC_VALUE_INVALID",
            f"{field_name} must be numeric.",
        ) from error
    if not math.isfinite(parsed):
        raise ProductionOpsValidationError(
            "PRODUCTION_OPS_METRIC_VALUE_INVALID",
            f"{field_name} must be finite.",
        )


def _parse_csv_tuple(value: str | None, default: Sequence[str]) -> tuple[str, ...]:
    if value is None or not value.strip():
        return tuple(default)
    parsed = tuple(item.strip() for item in value.split(",") if item.strip())
    if not parsed:
        return tuple(default)
    return parsed


def _parse_dependency_statuses(value: str | None) -> Mapping[str, str | None]:
    if not value:
        return {}
    statuses: dict[str, str | None] = {}
    for item in value.split(","):
        if not item.strip():
            continue
        if "=" not in item:
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_DEPENDENCY_STATUS_INVALID",
                "Dependency statuses must use dependency=status entries.",
            )
        name, status = (part.strip() for part in item.split("=", maxsplit=1))
        if name not in DEPENDENCY_CONTRACTS:
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_DEPENDENCY_STATUS_INVALID",
                f"Unknown dependency status name: {name}.",
            )
        _dependency_status_from_explicit(status)
        statuses[name] = status
    return statuses


def _dependency_root(env_name: str, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit.expanduser()
    raw = os.getenv(env_name)
    return Path(raw).expanduser() if raw else None


def _safe_run_id(run_id: str) -> str:
    if SAFE_RUN_ID_RE.fullmatch(run_id):
        return run_id
    raise ProductionOpsValidationError(
        "PRODUCTION_OPS_RUN_ID_UNSAFE",
        "run_id may contain only alphanumeric characters, underscores, and hyphens.",
    )


def _safe_resolved_evidence_root(evidence_root: Path) -> Path:
    root = evidence_root.expanduser()
    _refuse_symlink_components_to_deepest_existing(root)
    return root.resolve(strict=False)


def _refuse_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part == path.anchor or part == "":
            continue
        current = current / part
        if current.is_symlink():
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_EVIDENCE_SYMLINK",
                f"Evidence path component must not be a symlink: {current}",
            )


def _refuse_symlink_components_to_deepest_existing(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part == path.anchor or part == "":
            continue
        current = current / part
        if current.is_symlink():
            raise ProductionOpsValidationError(
                "PRODUCTION_OPS_EVIDENCE_SYMLINK",
                f"Evidence path component must not be a symlink: {current}",
            )
        if not current.exists():
            break


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.command("validate-ops")
    @click.option("--evidence-root", type=click.Path(path_type=Path), required=True)
    @click.option("--run-id")
    @click.option("--auth-mode", default=None)
    @click.option("--required-roles", default=None)
    @click.option("--alert-target", default=None)
    @click.option("--deployment-config-source", default=None)
    @click.option("--rollback-scope", default=None)
    @click.option("--slurm-evidence-root", type=click.Path(path_type=Path), default=None)
    @click.option("--object-store-evidence-root", type=click.Path(path_type=Path), default=None)
    @click.option("--met-evidence-root", type=click.Path(path_type=Path), default=None)
    @click.option("--e2e-evidence-root", type=click.Path(path_type=Path), default=None)
    @click.option("--scale-evidence-root", type=click.Path(path_type=Path), default=None)
    @click.option("--dependency-statuses", default=None)
    @click.option("--force", is_flag=True, default=False)
    def validate_ops_command(
        evidence_root: Path,
        run_id: str | None,
        auth_mode: str | None,
        required_roles: str | None,
        alert_target: str | None,
        deployment_config_source: str | None,
        rollback_scope: str | None,
        slurm_evidence_root: Path | None,
        object_store_evidence_root: Path | None,
        met_evidence_root: Path | None,
        e2e_evidence_root: Path | None,
        scale_evidence_root: Path | None,
        dependency_statuses: str | None,
        force: bool,
    ) -> None:
        try:
            summary = validate_ops(
                ProductionOpsConfig.from_env(
                    evidence_root=evidence_root,
                    run_id=run_id,
                    auth_mode=auth_mode,
                    required_roles=required_roles,
                    alert_target=alert_target,
                    deployment_config_source=deployment_config_source,
                    rollback_scope=rollback_scope,
                    slurm_evidence_root=slurm_evidence_root,
                    object_store_evidence_root=object_store_evidence_root,
                    met_evidence_root=met_evidence_root,
                    e2e_evidence_root=e2e_evidence_root,
                    scale_evidence_root=scale_evidence_root,
                    dependency_statuses=dependency_statuses,
                    force=force,
                )
            )
            click.echo(json.dumps(redact_payload(summary), sort_keys=True))
        except ProductionOpsValidationError as error:
            click.echo(f"{error.error_code}: {redact_text(error.message)}", err=True)
            raise SystemExit(1) from error
        except Exception as error:
            click.echo(f"PRODUCTION_OPS_VALIDATION_FAILED: {redact_text(str(error))}", err=True)
            raise SystemExit(1) from error

    try:
        validate_ops_command.main(args=list(argv) if argv is not None else None, standalone_mode=False)
    except click.ClickException as error:
        error.show()
        raise SystemExit(error.exit_code) from error
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-production validate-ops")
    _add_argparse_options(parser)
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                redact_payload(
                    validate_ops(
                        ProductionOpsConfig.from_env(
                            evidence_root=args.evidence_root,
                            run_id=args.run_id,
                            auth_mode=args.auth_mode,
                            required_roles=args.required_roles,
                            alert_target=args.alert_target,
                            deployment_config_source=args.deployment_config_source,
                            rollback_scope=args.rollback_scope,
                            slurm_evidence_root=args.slurm_evidence_root,
                            object_store_evidence_root=args.object_store_evidence_root,
                            met_evidence_root=args.met_evidence_root,
                            e2e_evidence_root=args.e2e_evidence_root,
                            scale_evidence_root=args.scale_evidence_root,
                            dependency_statuses=args.dependency_statuses,
                            force=args.force,
                        )
                    )
                ),
                sort_keys=True,
            )
        )
    except ProductionOpsValidationError as error:
        print(f"{error.error_code}: {redact_text(error.message)}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"PRODUCTION_OPS_VALIDATION_FAILED: {redact_text(str(error))}", file=sys.stderr)
        return 1
    return 0


def _add_argparse_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--auth-mode", default=None)
    parser.add_argument("--required-roles", default=None)
    parser.add_argument("--alert-target", default=None)
    parser.add_argument("--deployment-config-source", default=None)
    parser.add_argument("--rollback-scope", default=None)
    parser.add_argument("--slurm-evidence-root", type=Path, default=None)
    parser.add_argument("--object-store-evidence-root", type=Path, default=None)
    parser.add_argument("--met-evidence-root", type=Path, default=None)
    parser.add_argument("--e2e-evidence-root", type=Path, default=None)
    parser.add_argument("--scale-evidence-root", type=Path, default=None)
    parser.add_argument("--dependency-statuses", default=None)
    parser.add_argument("--force", action="store_true")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
