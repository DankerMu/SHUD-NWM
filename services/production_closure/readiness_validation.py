from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from packages.common.redaction import redact_payload, redact_text
from services.production_closure import (
    readiness_dependency_summaries as _readiness_dependency_summaries,
)
from services.production_closure import (
    readiness_item_contracts as _readiness_item_contracts,
)
from services.production_closure import (
    readiness_scheduler_evidence as _readiness_scheduler_evidence,
)
from services.production_closure import (
    readiness_shared_artifacts as _readiness_shared_artifacts,
)

SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
STATUS_VALUES = _readiness_item_contracts.STATUS_VALUES
EXECUTION_MODE_VALUES = _readiness_item_contracts.EXECUTION_MODE_VALUES
EXECUTED_MODES = _readiness_item_contracts.EXECUTED_MODES
ALLOWED_STATUS_EXECUTION_MODES = _readiness_item_contracts.ALLOWED_STATUS_EXECUTION_MODES
ProductionReadinessValidationError = _readiness_item_contracts.ProductionReadinessValidationError
validate_readiness_item = _readiness_item_contracts.validate_readiness_item

DEPENDENCY_ROOT_ENV = _readiness_shared_artifacts.DEPENDENCY_ROOT_ENV
MAX_EVIDENCE_PAYLOAD_BYTES = _readiness_shared_artifacts.MAX_EVIDENCE_PAYLOAD_BYTES
MAX_JSON_DEPTH = _readiness_shared_artifacts.MAX_JSON_DEPTH
MAX_JSON_NODES = _readiness_shared_artifacts.MAX_JSON_NODES
MAX_STRING_LENGTH = _readiness_shared_artifacts.MAX_STRING_LENGTH
PATH_TOKEN_RE = _readiness_shared_artifacts.PATH_TOKEN_RE
PROOF_FILE_ENV = _readiness_shared_artifacts.PROOF_FILE_ENV
SCHEDULER_EVIDENCE_FILE_ENV = _readiness_shared_artifacts.SCHEDULER_EVIDENCE_FILE_ENV
SCHEDULER_EVIDENCE_ROOT_ENV = _readiness_shared_artifacts.SCHEDULER_EVIDENCE_ROOT_ENV
BoundedPayloadResult = _readiness_shared_artifacts.BoundedPayloadResult
EvidenceWriter = _readiness_shared_artifacts.EvidenceWriter
_bounded_payload = _readiness_shared_artifacts._bounded_payload
_bounded_redacted_payload = _readiness_shared_artifacts._bounded_redacted_payload
_environment_payload = _readiness_shared_artifacts._environment_payload
_load_proof = _readiness_shared_artifacts._load_proof
_path_for_evidence = _readiness_shared_artifacts._path_for_evidence
_preflight_payload = _readiness_shared_artifacts._preflight_payload
_receipt_artifact = _readiness_shared_artifacts._receipt_artifact
_receipt_details = _readiness_shared_artifacts._receipt_details
_receipt_validation_payload = _readiness_shared_artifacts._receipt_validation_payload
_redact_paths = _readiness_shared_artifacts._redact_paths
_redacted_preview = _readiness_shared_artifacts._redacted_preview
_refuse_symlink_components_to_deepest_existing = (
    _readiness_shared_artifacts._refuse_symlink_components_to_deepest_existing
)

MAX_RECEIPT_BYTES = _readiness_shared_artifacts.MAX_RECEIPT_BYTES
MAX_RECEIPT_PREVIEW_BYTES = _readiness_shared_artifacts.MAX_RECEIPT_PREVIEW_BYTES
LIVE_PROOF_SCHEMA = _readiness_shared_artifacts.LIVE_PROOF_SCHEMA
EXPECTED_TARGET_ENVIRONMENT = "production"

DEPENDENCY_SUMMARY_CONTRACTS = _readiness_dependency_summaries.DEPENDENCY_SUMMARY_CONTRACTS
_dependency_summary_blocked = _readiness_dependency_summaries._dependency_summary_blocked
_dependency_summary_artifact_ref = _readiness_dependency_summaries._dependency_summary_artifact_ref
_dependency_bindings = _readiness_dependency_summaries._dependency_bindings
_find_summary_path = _readiness_dependency_summaries._find_summary_path
PROOF_ENV = _readiness_shared_artifacts.PROOF_ENV
SCHEDULER_EVIDENCE_SCHEMA = _readiness_scheduler_evidence.SCHEDULER_EVIDENCE_SCHEMA
MAX_SCHEDULER_EVIDENCE_BYTES = _readiness_scheduler_evidence.MAX_SCHEDULER_EVIDENCE_BYTES
MAX_SCHEDULER_EVIDENCE_FILES = _readiness_scheduler_evidence.MAX_SCHEDULER_EVIDENCE_FILES
SCHEDULER_REVIEW_EXECUTION_MODES = _readiness_scheduler_evidence.SCHEDULER_REVIEW_EXECUTION_MODES
SCHEDULER_REVIEW_PASSED_STATUSES = _readiness_scheduler_evidence.SCHEDULER_REVIEW_PASSED_STATUSES
SCHEDULER_REVIEW_BLOCKED_STATUSES = _readiness_scheduler_evidence.SCHEDULER_REVIEW_BLOCKED_STATUSES
SCHEDULER_REQUIRED_COUNT_FIELDS = _readiness_scheduler_evidence.SCHEDULER_REQUIRED_COUNT_FIELDS
SCHEDULER_DRY_RUN_NO_MUTATION_FALSE_FIELDS = (
    _readiness_scheduler_evidence.SCHEDULER_DRY_RUN_NO_MUTATION_FALSE_FIELDS
)
SCHEDULER_LIVE_PRODUCER_EXECUTION_MODES = (
    _readiness_scheduler_evidence.SCHEDULER_LIVE_PRODUCER_EXECUTION_MODES
)
SCHEDULER_LIVE_WORK_STATUSES = _readiness_scheduler_evidence.SCHEDULER_LIVE_WORK_STATUSES
SCHEDULER_BINDING_ALIAS_GROUPS: Mapping[str, tuple[str, ...]] = {
    "producer_schema": ("producer_schema", "scheduler_schema"),
    "producer_run_id": ("producer_run_id", "scheduler_pass_id", "pass_id"),
    "producer_artifact_ref": (
        "producer_artifact_ref",
        "producer_artifact_path",
        "producer_artifact_uri",
        "scheduler_artifact_ref",
        "scheduler_artifact_path",
        "artifact_ref",
        "artifact_path",
        "artifact_uri",
    ),
    "producer_checksum_or_receipt_id": (
        "scheduler_checksum",
        "producer_checksum",
        "summary_checksum",
        "checksum",
        "digest",
        "producer_receipt_id",
        "receipt_id",
    ),
}
SCHEDULER_BINDING_ALIAS_ERROR_SUFFIXES = {
    "producer_schema": "producer_schema_alias_mismatch",
    "producer_run_id": "producer_run_id_alias_mismatch",
    "producer_artifact_ref": "producer_artifact_ref_alias_mismatch",
    "producer_checksum_or_receipt_id": "producer_checksum_or_receipt_id_alias_mismatch",
}
DEPENDENCY_BINDING_ALIAS_GROUPS: Mapping[str, tuple[str, ...]] = {
    "dependency": ("dependency_surface", "dependency_name", "dependency"),
    "producer_issue": ("producer_issue", "summary_issue"),
    "producer_schema": ("producer_schema", "summary_schema"),
    "producer_run_id": ("producer_run_id", "summary_run_id"),
    "producer_artifact_ref": (
        "producer_artifact_ref",
        "producer_artifact_path",
        "producer_artifact_uri",
        "summary_ref",
        "summary_path",
        "artifact_ref",
        "artifact_path",
        "artifact_uri",
    ),
    "producer_checksum_or_receipt_id": (
        "summary_checksum",
        "producer_checksum",
        "checksum",
        "digest",
        "producer_receipt_id",
        "receipt_id",
    ),
}
DEPENDENCY_BINDING_ALIAS_ERROR_SUFFIXES = {
    "dependency": "dependency_alias_mismatch",
    "producer_issue": "producer_issue_alias_mismatch",
    "producer_schema": "producer_schema_alias_mismatch",
    "producer_run_id": "producer_run_id_alias_mismatch",
    "producer_artifact_ref": "producer_artifact_ref_alias_mismatch",
    "producer_checksum_or_receipt_id": "producer_checksum_or_receipt_id_alias_mismatch",
}
PROOF_CONTRACTS = {
    "auth": {
        "proof_type": "auth",
        "surface": "live_backend_auth",
        "allowed_statuses": {"passed"},
    },
    "alert": {
        "proof_type": "alert",
        "surface": "live_alert_sink_delivery",
        "allowed_statuses": {"passed", "delivered"},
    },
    "rollback": {
        "proof_type": "rollback",
        "surface": "live_rollback_execution",
        "allowed_statuses": {"passed", "executed"},
    },
    "scheduler": {
        "proof_type": "scheduler_evidence",
        "surface": "live_scheduler_evidence_proof",
        "allowed_statuses": {"passed", "accepted", "ready", "submitted", "completed"},
    },
    "slurm": {
        "proof_type": "dependency",
        "surface": "live_slurm_dependency_proof",
        "dependency": "slurm",
        "allowed_statuses": {"passed", "accepted", "ready"},
    },
    "object_store": {
        "proof_type": "dependency",
        "surface": "live_object_store_dependency_proof",
        "dependency": "object_store",
        "allowed_statuses": {"passed", "accepted", "ready"},
    },
    "source": {
        "proof_type": "dependency",
        "surface": "live_source_weather_dependency_proof",
        "dependency": "source",
        "allowed_statuses": {"passed", "accepted", "ready"},
    },
    "e2e": {
        "proof_type": "dependency",
        "surface": "live_e2e_dependency_proof",
        "dependency": "e2e",
        "allowed_statuses": {"passed", "accepted", "ready"},
    },
    "mvt": {
        "proof_type": "dependency",
        "surface": "live_mvt_performance_proof",
        "dependency": "mvt",
        "allowed_statuses": {"passed", "accepted", "ready"},
    },
    "target_env": {
        "proof_type": "target_env",
        "surface": "target_environment_config_proof",
        "allowed_statuses": {"passed", "accepted", "ready"},
    },
}
REQUIRED_AUTH_ACTIONS = frozenset(
    {
        "pipeline.retry_run",
        "pipeline.cancel_run",
        "pipeline.rerun_cycle",
        "qc.override_result",
        "tiles.republish",
        "sources.update_config",
        "models.activate",
        "models.deactivate",
        "models.switch_version",
        "models.rollback_version",
        "models.supersede",
        "users.manage",
    }
)


@dataclass(frozen=True)
class ProductionReadinessConfig:
    evidence_root: Path
    run_id: str
    dependency_roots: Mapping[str, Path | None]
    scheduler_evidence_root: Path | None
    scheduler_evidence_file: Path | None
    proof_json: Mapping[str, str | None]
    proof_files: Mapping[str, Path | None]
    force: bool = False

    @property
    def lane_dir(self) -> Path:
        return self.evidence_root / self.run_id / "readiness"

    @classmethod
    def from_env(
        cls,
        *,
        evidence_root: Path,
        run_id: str | None = None,
        slurm_evidence_root: Path | None = None,
        object_store_evidence_root: Path | None = None,
        source_evidence_root: Path | None = None,
        e2e_evidence_root: Path | None = None,
        mvt_evidence_root: Path | None = None,
        scheduler_evidence_root: Path | None = None,
        scheduler_evidence_file: Path | None = None,
        auth_proof: str | None = None,
        auth_proof_file: Path | None = None,
        alert_proof: str | None = None,
        alert_proof_file: Path | None = None,
        rollback_proof: str | None = None,
        rollback_proof_file: Path | None = None,
        scheduler_proof: str | None = None,
        scheduler_proof_file: Path | None = None,
        slurm_proof: str | None = None,
        slurm_proof_file: Path | None = None,
        object_store_proof: str | None = None,
        object_store_proof_file: Path | None = None,
        source_proof: str | None = None,
        source_proof_file: Path | None = None,
        e2e_proof: str | None = None,
        e2e_proof_file: Path | None = None,
        mvt_proof: str | None = None,
        mvt_proof_file: Path | None = None,
        target_env_proof: str | None = None,
        target_env_proof_file: Path | None = None,
        force: bool = False,
    ) -> ProductionReadinessConfig:
        resolved_evidence_root = _safe_resolved_evidence_root(evidence_root)
        resolved_run_id = _safe_run_id(run_id or datetime.now(UTC).strftime("m19-%Y%m%dT%H%M%SZ"))
        explicit_roots = {
            "slurm": slurm_evidence_root,
            "object_store": object_store_evidence_root,
            "source": source_evidence_root,
            "e2e": e2e_evidence_root,
            "mvt": mvt_evidence_root,
        }
        explicit_proofs = {
            "auth": auth_proof,
            "alert": alert_proof,
            "rollback": rollback_proof,
            "scheduler": scheduler_proof,
            "slurm": slurm_proof,
            "object_store": object_store_proof,
            "source": source_proof,
            "e2e": e2e_proof,
            "mvt": mvt_proof,
            "target_env": target_env_proof,
        }
        explicit_files = {
            "auth": auth_proof_file,
            "alert": alert_proof_file,
            "rollback": rollback_proof_file,
            "scheduler": scheduler_proof_file,
            "slurm": slurm_proof_file,
            "object_store": object_store_proof_file,
            "source": source_proof_file,
            "e2e": e2e_proof_file,
            "mvt": mvt_proof_file,
            "target_env": target_env_proof_file,
        }
        return cls(
            evidence_root=resolved_evidence_root,
            run_id=resolved_run_id,
            dependency_roots={
                name: _path_from_env(DEPENDENCY_ROOT_ENV[name], explicit)
                for name, explicit in explicit_roots.items()
            },
            scheduler_evidence_root=_path_from_env(SCHEDULER_EVIDENCE_ROOT_ENV, scheduler_evidence_root),
            scheduler_evidence_file=_path_from_env(SCHEDULER_EVIDENCE_FILE_ENV, scheduler_evidence_file),
            proof_json={
                name: explicit if explicit is not None else os.getenv(PROOF_ENV[name])
                for name, explicit in explicit_proofs.items()
            },
            proof_files={
                name: _path_from_env(PROOF_FILE_ENV[name], explicit)
                for name, explicit in explicit_files.items()
            },
            force=force,
        )


def validate_readiness(config: ProductionReadinessConfig) -> dict[str, Any]:
    config = replace(config, evidence_root=_safe_resolved_evidence_root(config.evidence_root))
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=config.force)
    writer.prepare()

    receipts = {
        surface: _load_proof(surface, config.proof_json.get(surface), config.proof_files.get(surface), config=config)
        for surface in PROOF_ENV
    }
    preflight = _preflight_payload(config, receipts)
    writer.write_json(config.lane_dir / "preflight.json", preflight)
    writer.write_json(config.lane_dir / "live_proof_receipts.json", _receipt_artifact(config, receipts))

    items: list[dict[str, Any]] = []
    items.extend(_deterministic_items(config))
    dependency_summary_items = _dependency_summary_items(config)
    items.extend(dependency_summary_items)
    scheduler_evidence_items = _scheduler_evidence_items(config)
    items.extend(scheduler_evidence_items)
    items.extend(
        _live_proof_items(
            config,
            receipts,
            _dependency_bindings(dependency_summary_items),
            _scheduler_bindings(scheduler_evidence_items),
        )
    )
    items.extend(_exclusion_items(config))
    items = _validate_items(items)
    writer.write_json(
        config.lane_dir / "readiness_items.json",
        {"schema": "nhms.production_readiness.items.v1", "items": items},
    )

    release_blockers = _release_blockers(items)
    blocker_payload = {
        "schema": "nhms.production_readiness.release_blockers.v1",
        "issue": 181,
        "run_id": config.run_id,
        "generated_at": _now(),
        "final_production_readiness_claimed": _final_ready(items),
        "blockers": release_blockers,
        "exclusions": _summary_exclusions(items),
    }
    writer.write_json(config.lane_dir / "release_blockers.json", blocker_payload)

    environment = _environment_payload(config)
    writer.write_json(config.lane_dir / "environment.json", environment)

    summary = {
        "schema": "nhms.production_readiness.summary.v1",
        "issue": 181,
        "run_id": config.run_id,
        "status": "ready" if _final_ready(items) else "release_blocked",
        "evidence_dir": _path_for_evidence(config.lane_dir, config=config),
        "generated_at": _now(),
        "final_production_readiness_claimed": _final_ready(items),
        "deterministic_item_count": sum(1 for item in items if item["execution_mode"] != "live_proof"),
        "live_proof_item_count": sum(1 for item in items if item["execution_mode"] == "live_proof"),
        "required_live_proof_count": sum(1 for item in items if item["required_for_final"]),
        "accepted_live_proof_count": sum(
            1 for item in items if item["required_for_final"] and item["live_proof_accepted"]
        ),
        "release_blockers": release_blockers,
        "exclusions": _summary_exclusions(items),
        "artifact_refs": [
            "preflight.json",
            "live_proof_receipts.json",
            "readiness_items.json",
            "release_blockers.json",
            "environment.json",
            "summary.json",
        ],
        "interpretation": (
            "Deterministic readiness evidence is useful for review but is not live production proof. "
            "Final production readiness remains false until every required live proof item is accepted."
        ),
    }
    writer.write_json(config.lane_dir / "summary.json", summary)
    return summary


def _deterministic_items(config: ProductionReadinessConfig) -> list[dict[str, Any]]:
    return [
        _item(
            item_id="deterministic-auth-policy",
            surface="backend_auth_policy_matrix",
            status="passed",
            execution_mode="policy_simulated",
            required_for_final=False,
            live_proof_accepted=False,
            artifact_refs=["readiness_items.json"],
            residual_risk="Policy simulation does not prove target-environment IdP behavior.",
            removal_criteria="Provide accepted live backend auth proof covering allowed and denied protected actions.",
            dependencies=["packages.common.auth_policy.ACTION_MATRIX"],
        ),
        _item(
            item_id="deterministic-alert-dry-run",
            surface="alert_sink_dry_run",
            status="passed",
            execution_mode="dry_run_sink",
            required_for_final=False,
            live_proof_accepted=False,
            artifact_refs=["readiness_items.json"],
            residual_risk="Dry-run alert evidence does not prove delivery to the target sink.",
            removal_criteria="Provide accepted live alert sink delivery receipt.",
        ),
        _item(
            item_id="deterministic-rollback-simulated",
            surface="rollback_simulated_drill",
            status="passed",
            execution_mode="simulated_drill",
            required_for_final=False,
            live_proof_accepted=False,
            artifact_refs=["readiness_items.json"],
            residual_risk="Simulated rollback drills do not prove target-environment rollback execution.",
            removal_criteria="Provide accepted live rollback drill receipt.",
        ),
        _item(
            item_id="deterministic-report-generation",
            surface="readiness_report_generation",
            status="passed",
            execution_mode="deterministic",
            required_for_final=False,
            live_proof_accepted=False,
            artifact_refs=["summary.json", "release_blockers.json", "readiness_items.json"],
            residual_risk="Report generation does not imply live dependency readiness.",
            removal_criteria="Use this summary with accepted live proof receipts for final readiness review.",
            dependencies=[_path_for_evidence(config.lane_dir, config=config)],
        ),
        _item(
            item_id="deterministic-model-operations",
            surface="model_operations_drills",
            status="passed",
            execution_mode="deterministic",
            required_for_final=False,
            live_proof_accepted=False,
            artifact_refs=["readiness_items.json"],
            residual_risk="Deterministic model lifecycle drills do not prove production object-store mutation safety.",
            removal_criteria="Use accepted target-environment config and dependency proof before release.",
        ),
    ]


def _dependency_summary_items(config: ProductionReadinessConfig) -> list[dict[str, Any]]:
    return _readiness_dependency_summaries._dependency_summary_items(
        config,
        read_dependency_summary_item=_read_dependency_summary_item,
    )


def _read_dependency_summary_item(name: str, root: Path, *, config: ProductionReadinessConfig) -> dict[str, Any]:
    return _readiness_dependency_summaries._read_dependency_summary_item(
        name,
        root,
        config=config,
        find_summary_path=_find_summary_path,
        dependency_summary_blocked=_dependency_summary_blocked,
        dependency_summary_artifact_ref=_dependency_summary_artifact_ref,
    )


def _scheduler_evidence_items(config: ProductionReadinessConfig) -> list[dict[str, Any]]:
    return _readiness_scheduler_evidence._scheduler_evidence_items(
        config,
        read_scheduler_evidence_item=_read_scheduler_evidence_item,
        scheduler_evidence_blocked=_scheduler_evidence_blocked,
        find_scheduler_evidence_files=_find_scheduler_evidence_files,
    )


def _read_scheduler_evidence_item(path: Path, *, config: ProductionReadinessConfig) -> dict[str, Any]:
    return _readiness_scheduler_evidence._read_scheduler_evidence_item(
        path,
        config=config,
        safe_scheduler_evidence_file=_safe_scheduler_evidence_file,
        scheduler_evidence_blocked=_scheduler_evidence_blocked,
        scheduler_evidence_errors=_scheduler_evidence_errors,
        scheduler_readiness_status=_scheduler_readiness_status,
        scheduler_evidence_mode=_scheduler_evidence_mode,
        scheduler_evidence_artifact_ref=_scheduler_evidence_artifact_ref,
        scheduler_item_suffix=_scheduler_item_suffix,
    )


_scheduler_evidence_blocked = _readiness_scheduler_evidence._scheduler_evidence_blocked
_scheduler_bindings = _readiness_scheduler_evidence._scheduler_bindings
_safe_scheduler_evidence_file = _readiness_scheduler_evidence._safe_scheduler_evidence_file
_scheduler_evidence_errors = _readiness_scheduler_evidence._scheduler_evidence_errors
_scheduler_readiness_status = _readiness_scheduler_evidence._scheduler_readiness_status
_scheduler_evidence_mode = _readiness_scheduler_evidence._scheduler_evidence_mode
_scheduler_evidence_artifact_ref = _readiness_scheduler_evidence._scheduler_evidence_artifact_ref
_scheduler_item_suffix = _readiness_scheduler_evidence._scheduler_item_suffix


def _find_scheduler_evidence_files(root: Path) -> list[Path]:
    return _readiness_scheduler_evidence._find_scheduler_evidence_files(
        root,
        safe_scheduler_evidence_file=_safe_scheduler_evidence_file,
    )


def _live_proof_items(
    config: ProductionReadinessConfig,
    receipts: Mapping[str, Mapping[str, Any]],
    dependency_bindings: Mapping[str, Mapping[str, Any]],
    scheduler_binding: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    items = [
        _auth_live_item(config, receipts["auth"]),
        _surface_live_item(
            config,
            receipts["alert"],
            proof_key="alert",
            dependency_bindings=dependency_bindings,
            item_id="live-alert-sink",
            surface="live_alert_sink_delivery",
            missing_risk="Live alert sink delivery has not been proven.",
            removal=(
                "Provide an accepted alert sink receipt bound to this readiness run, target environment, sink, "
                "delivery result, live mode, and evidence artifacts."
            ),
        ),
        _surface_live_item(
            config,
            receipts["rollback"],
            proof_key="rollback",
            dependency_bindings=dependency_bindings,
            item_id="live-rollback-drill",
            surface="live_rollback_execution",
            missing_risk="Live rollback execution has not been proven.",
            removal=(
                "Provide an accepted rollback drill receipt bound to this readiness run, target environment, "
                "preconditions, command/drill metadata, execution result, live mode, and evidence artifacts."
            ),
        ),
        _surface_live_item(
            config,
            receipts["slurm"],
            proof_key="slurm",
            dependency_bindings=dependency_bindings,
            item_id="live-slurm-dependency",
            surface="live_slurm_dependency_proof",
            missing_risk="Live Slurm workload/accounting proof has not been accepted.",
            removal="Provide an accepted Slurm dependency proof receipt from the target environment.",
        ),
        _surface_live_item(
            config,
            receipts["object_store"],
            proof_key="object_store",
            dependency_bindings=dependency_bindings,
            item_id="live-object-store-dependency",
            surface="live_object_store_dependency_proof",
            missing_risk="Live object-store/API proof has not been accepted.",
            removal="Provide an accepted object-store dependency proof receipt from the target environment.",
        ),
        _surface_live_item(
            config,
            receipts["source"],
            proof_key="source",
            dependency_bindings=dependency_bindings,
            item_id="live-source-dependency",
            surface="live_source_weather_dependency_proof",
            missing_risk="Live weather/source credential and ingest proof has not been accepted.",
            removal="Provide an accepted source/weather dependency proof receipt from the target environment.",
        ),
        _surface_live_item(
            config,
            receipts["e2e"],
            proof_key="e2e",
            dependency_bindings=dependency_bindings,
            item_id="live-e2e-dependency",
            surface="live_e2e_dependency_proof",
            missing_risk="Live E2E target-environment proof has not been accepted.",
            removal="Provide an accepted E2E dependency proof receipt from the target environment.",
        ),
        _surface_live_item(
            config,
            receipts["mvt"],
            proof_key="mvt",
            dependency_bindings=dependency_bindings,
            item_id="live-mvt-performance",
            surface="live_mvt_performance_proof",
            missing_risk="Live MVT/performance proof has not been accepted.",
            removal="Provide accepted live PostGIS/national-data/browser or equivalent MVT performance proof.",
        ),
        _surface_live_item(
            config,
            receipts["target_env"],
            proof_key="target_env",
            dependency_bindings=dependency_bindings,
            item_id="live-target-environment-config",
            surface="target_environment_config_proof",
            missing_risk="Real target-environment configuration receipt has not been accepted.",
            removal="Provide an accepted target-environment configuration receipt.",
        ),
    ]
    scheduler_live_configured = (
        config.scheduler_evidence_root is not None
        or config.scheduler_evidence_file is not None
        or receipts["scheduler"]["status"] != "missing"
    )
    if scheduler_live_configured:
        items.insert(
            3,
            _surface_live_item(
                config,
                receipts["scheduler"],
                proof_key="scheduler",
                dependency_bindings=dependency_bindings,
                scheduler_binding=scheduler_binding,
                item_id="live-scheduler-evidence",
                surface="live_scheduler_evidence_proof",
                missing_risk="Live scheduler evidence receipt has not been accepted.",
                removal=(
                    "Provide an accepted live scheduler evidence receipt bound to this readiness run, target "
                    "environment, scheduler pass id, artifact reference, checksum or receipt id, schema, and live "
                    "execution mode."
                ),
            ),
        )
    return items


def _auth_live_item(config: ProductionReadinessConfig, receipt: Mapping[str, Any]) -> dict[str, Any]:
    base = {
        "item_id": "live-backend-auth",
        "surface": "live_backend_auth",
        "required_for_final": True,
        "artifact_refs": ["live_proof_receipts.json"],
        "residual_risk": "Live backend IdP proof is missing or incomplete.",
        "removal_criteria": (
            "Provide accepted live auth proof with provider metadata plus allowed and denied coverage for every "
            "canonical protected action."
        ),
    }
    if receipt["status"] != "parsed":
        return _required_live_blocker(config=config, receipt=receipt, **base)
    payload = _receipt_validation_payload(receipt)
    allowed = _string_set(payload.get("allowed_actions") or payload.get("allowed_coverage"))
    denied = _string_set(payload.get("denied_actions") or payload.get("denied_coverage"))
    missing_allowed = sorted(REQUIRED_AUTH_ACTIONS - allowed)
    missing_denied = sorted(REQUIRED_AUTH_ACTIONS - denied)
    errors = _common_live_receipt_errors(payload, proof_key="auth", config=config)
    if not _provider_metadata_is_meaningful(payload):
        errors.append("missing_provider_metadata")
    if not _role_mapping_is_meaningful(payload.get("role_mapping")) and not _role_mapping_is_meaningful(
        payload.get("role_mappings")
    ):
        errors.append("missing_role_mapping")
    if missing_allowed:
        errors.append("missing_allowed_actions")
    if missing_denied:
        errors.append("missing_denied_actions")
    accepted = not errors
    if accepted:
        return _item(
            item_id=base["item_id"],
            surface=base["surface"],
            required_for_final=base["required_for_final"],
            artifact_refs=base["artifact_refs"],
            status="passed",
            execution_mode="live_proof",
            live_proof_accepted=True,
            residual_risk="Accepted live auth proof is present for required protected action coverage.",
            removal_criteria="Keep the accepted live auth receipt attached to the release evidence bundle.",
            details=_receipt_details(receipt, config=config),
        )
    return _item(
        **base,
        status="release_blocked",
        execution_mode="live_proof",
        live_proof_accepted=False,
        details=_receipt_details(
            {
                **receipt,
                "acceptance_errors": {
                    "errors": errors,
                    "accepted": payload.get("accepted") is True,
                    "missing_allowed_actions": missing_allowed,
                    "missing_denied_actions": missing_denied,
                },
            },
            config=config,
        ),
    )


def _surface_live_item(
    config: ProductionReadinessConfig,
    receipt: Mapping[str, Any],
    *,
    proof_key: str,
    dependency_bindings: Mapping[str, Mapping[str, Any]],
    scheduler_binding: Sequence[Mapping[str, Any]] = (),
    item_id: str,
    surface: str,
    missing_risk: str,
    removal: str,
) -> dict[str, Any]:
    base = {
        "item_id": item_id,
        "surface": surface,
        "required_for_final": True,
        "artifact_refs": ["live_proof_receipts.json"],
        "residual_risk": missing_risk,
        "removal_criteria": removal,
    }
    if receipt["status"] != "parsed":
        return _required_live_blocker(config=config, receipt=receipt, **base)
    payload = _receipt_validation_payload(receipt)
    errors = _surface_live_receipt_errors(
        payload,
        proof_key=proof_key,
        config=config,
        dependency_bindings=dependency_bindings,
        scheduler_binding=scheduler_binding,
    )
    if not errors:
        return _item(
            item_id=base["item_id"],
            surface=base["surface"],
            required_for_final=base["required_for_final"],
            artifact_refs=base["artifact_refs"],
            status="passed",
            execution_mode="live_proof",
            live_proof_accepted=True,
            residual_risk=f"Accepted live proof is present for {surface}.",
            removal_criteria="Keep the accepted live proof receipt attached to the release evidence bundle.",
            details=_receipt_details(receipt, config=config),
        )
    return _item(
        **base,
        status="release_blocked",
        execution_mode="live_proof",
        live_proof_accepted=False,
        details=_receipt_details({**receipt, "acceptance_errors": {"errors": errors}}, config=config),
    )


def _required_live_blocker(
    *,
    config: ProductionReadinessConfig,
    receipt: Mapping[str, Any],
    item_id: str,
    surface: str,
    required_for_final: bool,
    artifact_refs: list[str],
    residual_risk: str,
    removal_criteria: str,
) -> dict[str, Any]:
    execution_mode = "live_proof" if receipt["status"] in {"invalid", "too_large"} else "not_executed"
    return _item(
        item_id=item_id,
        surface=surface,
        status="release_blocked",
        execution_mode=execution_mode,
        required_for_final=required_for_final,
        live_proof_accepted=False,
        artifact_refs=artifact_refs,
        residual_risk=residual_risk,
        removal_criteria=removal_criteria,
        details=_receipt_details(receipt, config=config),
    )


def _exclusion_items(config: ProductionReadinessConfig) -> list[dict[str, Any]]:
    del config
    return [
        _item(
            item_id="scope-exclusion-cldas",
            surface="cldas_restricted_source",
            status="not_executed",
            execution_mode="not_executed",
            required_for_final=False,
            live_proof_accepted=False,
            artifact_refs=["readiness_items.json", "summary.json"],
            residual_risk="CLDAS restricted data is outside the current M19 readiness scope.",
            removal_criteria=(
                "Enable CLDAS adapter, credentials, data-quality checks, and accepted live proof in a later scope."
            ),
            exclusions=[
                {
                    "id": "cldas-restricted",
                    "reason": "CLDAS is excluded by current product decision for M19.",
                    "status": "not_executed",
                    "removal_criteria": "Complete CLDAS authorization and production best-available integration.",
                }
            ],
        ),
        _item(
            item_id="scope-exclusion-national-data",
            surface="incomplete_real_national_data",
            status="not_executed",
            execution_mode="not_executed",
            required_for_final=False,
            live_proof_accepted=False,
            artifact_refs=["readiness_items.json", "summary.json"],
            residual_risk="Complete real national data coverage is outside the current deterministic M19 scope.",
            removal_criteria=(
                "Attach accepted target-environment national-data, live PostGIS, and performance evidence in a later "
                "scope."
            ),
            exclusions=[
                {
                    "id": "real-national-data-incomplete",
                    "reason": "Incomplete real national data is a scoped exclusion, not deterministic failure.",
                    "status": "not_executed",
                    "removal_criteria": "Complete national-data coverage and live MVT/performance proof.",
                }
            ],
        ),
    ]


def _validate_items(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    validated_items = []
    for index, item in enumerate(items):
        try:
            validate_readiness_item(item)
        except ProductionReadinessValidationError as error:
            validated_items.append(
                _item(
                    item_id=f"schema-validation-{index}",
                    surface="readiness_schema_validation",
                    status="failed",
                    execution_mode="deterministic",
                    required_for_final=False,
                    live_proof_accepted=False,
                    artifact_refs=["readiness_items.json"],
                    residual_risk=error.message,
                    removal_criteria="Fix the readiness producer item contract before release review.",
                )
            )
        else:
            validated_items.append(item)
    return validated_items


def _release_blockers(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    blockers = []
    for item in items:
        status = str(item["status"])
        if status not in {"failed", "blocked", "release_blocked"} and not (
            item["required_for_final"] and not item["live_proof_accepted"]
        ):
            continue
        blockers.append(
            {
                "blocker_id": f"m19-{item['item_id']}",
                "surface": item["surface"],
                "status": status,
                "execution_mode": item["execution_mode"],
                "owner": item["owner"],
                "action": item["action"],
                "residual_risk": item["residual_risk"],
                "removal_criteria": item["removal_criteria"],
                "artifact_refs": list(item["artifact_refs"]),
                "required_for_final": item["required_for_final"],
                "live_proof_accepted": item["live_proof_accepted"],
            }
        )
    return blockers


def _final_ready(items: Sequence[Mapping[str, Any]]) -> bool:
    for item in items:
        if item["status"] in {"failed", "blocked", "release_blocked"}:
            return False
        if item["required_for_final"] and (item["status"] != "passed" or item["live_proof_accepted"] is not True):
            return False
    return True


def _item(
    *,
    item_id: str,
    surface: str,
    status: str,
    execution_mode: str,
    required_for_final: bool,
    live_proof_accepted: bool,
    artifact_refs: Sequence[str],
    residual_risk: str,
    removal_criteria: str,
    exclusions: Sequence[Mapping[str, Any]] = (),
    dependencies: Sequence[str] = (),
    details: Mapping[str, Any] | None = None,
    owner: str = "release_owner",
    action: str | None = None,
) -> dict[str, Any]:
    item = {
        "item_id": item_id,
        "surface": surface,
        "status": status,
        "execution_mode": execution_mode,
        "required_for_final": required_for_final,
        "live_proof_accepted": live_proof_accepted,
        "artifact_refs": list(artifact_refs),
        "residual_risk": residual_risk,
        "removal_criteria": removal_criteria,
        "exclusions": [dict(exclusion) for exclusion in exclusions],
        "dependencies": list(dependencies),
        "owner": owner,
        "action": action or removal_criteria,
    }
    if details is not None:
        item["details"] = dict(details)
    validate_readiness_item(item)
    return item


def _summary_exclusions(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    exclusions = []
    for item in items:
        for exclusion in item.get("exclusions", []):
            exclusions.append(
                {
                    "surface": item["surface"],
                    "status": item["status"],
                    **dict(exclusion),
                }
            )
    return exclusions


def _surface_live_receipt_errors(
    payload: Mapping[str, Any],
    *,
    proof_key: str,
    config: ProductionReadinessConfig,
    dependency_bindings: Mapping[str, Mapping[str, Any]],
    scheduler_binding: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    errors = _common_live_receipt_errors(payload, proof_key=proof_key, config=config)
    if proof_key == "alert":
        if not _alert_sink_metadata_is_meaningful(payload):
            errors.append("missing_sink_metadata")
        if not _alert_delivery_metadata_is_meaningful(payload):
            errors.append("missing_delivery_metadata")
        if payload.get("delivered") is not True and str(payload.get("status", "")) != "delivered":
            errors.append("delivery_not_confirmed")
    elif proof_key == "rollback":
        if not _has_meaningful_value(payload.get("preconditions")):
            errors.append("missing_preconditions")
        if not _rollback_command_metadata_is_meaningful(payload):
            errors.append("missing_command_or_drill_metadata")
        if not _rollback_result_is_meaningful(payload):
            errors.append("rollback_not_executed")
    elif proof_key in DEPENDENCY_SUMMARY_CONTRACTS:
        errors.extend(_dependency_receipt_errors(payload, proof_key=proof_key, dependency_bindings=dependency_bindings))
    elif proof_key == "scheduler":
        errors.extend(_scheduler_receipt_errors(payload, scheduler_binding=scheduler_binding))
    elif proof_key == "target_env":
        if not _target_env_config_metadata_is_meaningful(payload):
            errors.append("missing_target_environment_config_metadata")
    return errors


def _common_live_receipt_errors(
    payload: Mapping[str, Any],
    *,
    proof_key: str,
    config: ProductionReadinessConfig,
) -> list[str]:
    contract = PROOF_CONTRACTS[proof_key]
    errors: list[str] = []
    if payload.get("accepted") is not True:
        errors.append("accepted_not_true")
    status = payload.get("status")
    if not isinstance(status, str) or not status.strip():
        errors.append("missing_status")
    elif status not in contract["allowed_statuses"]:
        errors.append("status_not_allowed")
    if payload.get("schema") != LIVE_PROOF_SCHEMA:
        errors.append("schema_mismatch")
    if payload.get("proof_type", payload.get("receipt_type")) != contract["proof_type"]:
        errors.append("proof_type_mismatch")
    if payload.get("surface") != contract["surface"]:
        errors.append("surface_mismatch")
    if payload.get("run_id") != config.run_id:
        errors.append("run_id_mismatch")
    target_environment = payload.get("target_environment")
    if not _non_empty_string(target_environment) and not _has_meaningful_value(target_environment):
        errors.append("missing_target_environment")
    elif _target_environment_name(target_environment) != EXPECTED_TARGET_ENVIRONMENT:
        errors.append("target_environment_mismatch")
    if not _is_live_proof_mode(payload):
        errors.append("execution_mode_not_live_proof")
    if not _has_artifact_or_evidence_refs(payload):
        errors.append("missing_artifact_or_evidence_refs")
    return errors


def _is_live_proof_mode(payload: Mapping[str, Any]) -> bool:
    values = {
        str(payload.get("execution_mode", "")),
        str(payload.get("proof_mode", "")),
        str(payload.get("mode", "")),
    }
    return bool(values & {"live_proof", "live_execution", "live"})


def _has_artifact_or_evidence_refs(payload: Mapping[str, Any]) -> bool:
    for key in ("artifact_refs", "evidence_refs", "artifacts", "evidence"):
        if _has_meaningful_ref(payload.get(key)):
            return True
    return False


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_meaningful_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, Mapping):
        return any(str(key).strip() and _has_meaningful_value(nested) for key, nested in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_has_meaningful_value(item) for item in value)
    return True


def _has_meaningful_ref(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        ref_keys = (
            "id",
            "ref",
            "path",
            "uri",
            "url",
            "checksum",
            "digest",
            "receipt_id",
            "artifact_ref",
            "artifact_path",
            "artifact_uri",
            "summary_path",
            "summary_ref",
            "summary_checksum",
        )
        return any(_has_meaningful_value(value.get(key)) for key in ref_keys) or any(
            _has_meaningful_ref(nested) for nested in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_has_meaningful_ref(item) for item in value)
    return False


def _target_environment_name(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("name", "environment", "id"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _string_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = set()
        for item in value:
            if isinstance(item, str) and item.strip():
                values.add(item.strip())
        return values
    return set()


def _provider_metadata_is_meaningful(payload: Mapping[str, Any]) -> bool:
    provider = _first_meaningful_mapping(payload, ("provider", "provider_metadata", "idp_metadata"))
    if provider is not None and _has_any_key_value(
        provider,
        (
            "issuer",
            "issuer_url",
            "provider_id",
            "provider",
            "provider_name",
            "idp",
            "idp_id",
            "tenant_id",
            "subject",
            "client_id",
        ),
    ):
        return True
    return _has_any_key_value(
        payload,
        ("issuer", "issuer_url", "provider_id", "provider_name", "idp_id", "tenant_id", "subject", "client_id"),
    )


def _role_mapping_is_meaningful(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    for role, mapped in value.items():
        if not _non_empty_string(role):
            continue
        if _string_set(mapped):
            return True
        if isinstance(mapped, Mapping) and any(
            _string_set(mapped.get(key)) for key in ("actions", "roles", "permissions", "allowed_actions")
        ):
            return True
    return False


def _alert_sink_metadata_is_meaningful(payload: Mapping[str, Any]) -> bool:
    sink = _first_meaningful_mapping(payload, ("sink_metadata", "sink"))
    if sink is not None and _has_any_key_value(sink, ("sink_id", "id", "name", "sink_name", "url", "uri", "channel")):
        return True
    return _has_any_key_value(payload, ("sink_id", "sink_name", "sink_url", "sink", "channel"))


def _alert_delivery_metadata_is_meaningful(payload: Mapping[str, Any]) -> bool:
    delivery = _first_meaningful_mapping(payload, ("delivery_metadata", "delivery_result", "delivery"))
    if delivery is None:
        return False
    has_id = _has_any_key_value(delivery, ("delivery_id", "message_id", "id", "receipt_id"))
    has_timestamp = _has_any_key_value(delivery, ("delivered_at", "timestamp", "time", "completed_at"))
    has_result = _has_any_key_value(delivery, ("result", "status", "delivery_status", "outcome"))
    return has_id and has_timestamp and has_result


def _rollback_command_metadata_is_meaningful(payload: Mapping[str, Any]) -> bool:
    command = _first_meaningful_mapping(payload, ("command_metadata", "drill_metadata", "command"))
    if command is not None and (
        _has_any_key_value(command, ("command", "command_id", "drill_id", "id", "runbook", "rollback_id"))
        or _non_empty_string(command.get("argv"))
    ):
        return True
    return _has_any_key_value(payload, ("command", "command_id", "drill_id", "rollback_id"))


def _rollback_result_is_meaningful(payload: Mapping[str, Any]) -> bool:
    if payload.get("executed") is True:
        return True
    result = _value_from(payload, ("execution_result", "result", "rollback_result", "outcome"))
    if _non_empty_string(result):
        return str(result).strip().lower() in {"passed", "executed", "success", "succeeded"}
    status = payload.get("status")
    return isinstance(status, str) and status.strip().lower() == "executed"


def _target_env_config_metadata_is_meaningful(payload: Mapping[str, Any]) -> bool:
    config_metadata = _first_meaningful_mapping(payload, ("config_metadata", "environment_metadata"))
    if config_metadata is None:
        return False
    has_metadata = _has_meaningful_value(config_metadata)
    has_identifier = _has_any_key_value(
        payload,
        ("config_receipt_id", "config_id", "environment_id", "environment_name", "target_config_id"),
    ) or _has_any_key_value(
        config_metadata,
        ("config_receipt_id", "config_id", "environment_id", "environment_name", "name", "id", "cluster"),
    )
    return has_metadata and has_identifier


def _dependency_receipt_errors(
    payload: Mapping[str, Any],
    *,
    proof_key: str,
    dependency_bindings: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    errors: list[str] = []
    expected_dependency = str(PROOF_CONTRACTS[proof_key]["dependency"])
    contract = DEPENDENCY_SUMMARY_CONTRACTS[proof_key]
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), Mapping) else {}
    top_level_binding = _dependency_producer_binding(payload)
    provenance_binding = _dependency_producer_binding(provenance)
    binding_values = {
        field: _coalesced_binding_value(top_level_binding, provenance_binding, field)
        for field in DEPENDENCY_BINDING_ALIAS_GROUPS
    }
    errors.extend(_dependency_binding_alias_errors(top_level_binding, source="top_level"))
    errors.extend(_dependency_binding_alias_errors(provenance_binding, source="provenance"))
    errors.extend(_dependency_binding_consistency_errors(top_level_binding, provenance_binding))

    dependency = binding_values["dependency"]
    if dependency != expected_dependency:
        errors.append("dependency_surface_mismatch")

    producer_issue = binding_values["producer_issue"]
    if not _issue_matches(producer_issue, contract["issue"]):
        errors.append("producer_issue_mismatch")

    producer_schema = binding_values["producer_schema"]
    if producer_schema != contract["schema"]:
        errors.append("producer_schema_mismatch")

    producer_run_id = binding_values["producer_run_id"]
    if not _non_empty_string(producer_run_id):
        errors.append("missing_producer_run_id")

    artifact_ref = binding_values["producer_artifact_ref"]
    if not _non_empty_string(artifact_ref):
        errors.append("missing_producer_artifact_ref")

    checksum_or_receipt = binding_values["producer_checksum_or_receipt_id"]
    if not _non_empty_string(checksum_or_receipt):
        errors.append("missing_producer_checksum_or_receipt_id")

    if not _has_meaningful_value(provenance):
        errors.append("missing_provenance")
    elif _contains_placeholder_value(provenance):
        errors.append("placeholder_provenance")

    binding = dependency_bindings.get(expected_dependency)
    if binding:
        if producer_run_id != binding.get("summary_run_id"):
            errors.append("producer_run_id_mismatch")
        if artifact_ref != binding.get("producer_artifact_ref"):
            errors.append("producer_artifact_ref_mismatch")
        if checksum_or_receipt != binding.get("summary_checksum"):
            errors.append("producer_checksum_mismatch")
        errors.extend(_dependency_binding_summary_errors(top_level_binding, binding, source="top_level"))
        errors.extend(_dependency_binding_summary_errors(provenance_binding, binding, source="provenance"))
    return errors


def _coalesced_binding_value(
    top_level_binding: Mapping[str, Any],
    provenance_binding: Mapping[str, Any],
    field: str,
) -> Any:
    top_value = _binding_canonical_value(top_level_binding, field)
    if _has_meaningful_value(top_value):
        return top_value
    return _binding_canonical_value(provenance_binding, field)


def _dependency_producer_binding(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        field: {
            key: _normalized_binding_value(payload.get(key), field=field)
            for key in aliases
            if _has_meaningful_value(payload.get(key))
        }
        for field, aliases in DEPENDENCY_BINDING_ALIAS_GROUPS.items()
    }


def _binding_values(receipt_binding: Mapping[str, Any], field: str) -> dict[str, Any]:
    values = receipt_binding.get(field)
    return values if isinstance(values, dict) else {}


def _binding_canonical_value(receipt_binding: Mapping[str, Any], field: str) -> Any:
    values = _binding_values(receipt_binding, field)
    for alias in DEPENDENCY_BINDING_ALIAS_GROUPS[field]:
        value = values.get(alias)
        if _has_meaningful_value(value):
            return value
    return None


def _normalized_binding_value(value: Any, *, field: str) -> Any:
    if value is None:
        return None
    if field == "producer_issue":
        if isinstance(value, str):
            return value.strip().lstrip("#")
        return str(value).strip()
    if isinstance(value, str):
        return value.strip()
    return value


def _dependency_binding_alias_errors(receipt_binding: Mapping[str, Any], *, source: str) -> list[str]:
    errors: list[str] = []
    for binding_field, suffix in DEPENDENCY_BINDING_ALIAS_ERROR_SUFFIXES.items():
        values = list(_binding_values(receipt_binding, binding_field).values())
        if values and any(value != values[0] for value in values[1:]):
            errors.append(f"{source}_{suffix}")
    return errors


def _dependency_binding_consistency_errors(
    top_level_binding: Mapping[str, Any],
    provenance_binding: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    for binding_field, error in (
        ("dependency", "provenance_dependency_mismatch"),
        ("producer_issue", "provenance_producer_issue_mismatch"),
        ("producer_schema", "provenance_producer_schema_mismatch"),
        ("producer_run_id", "provenance_producer_run_id_mismatch"),
        ("producer_artifact_ref", "provenance_producer_artifact_ref_mismatch"),
        ("producer_checksum_or_receipt_id", "provenance_producer_checksum_or_receipt_id_mismatch"),
    ):
        top_values = list(_binding_values(top_level_binding, binding_field).values())
        provenance_values = list(_binding_values(provenance_binding, binding_field).values())
        if top_values and provenance_values and any(
            top_value != provenance_value for top_value in top_values for provenance_value in provenance_values
        ):
            errors.append(error)
    return errors


def _dependency_binding_summary_errors(
    receipt_binding: Mapping[str, Any],
    summary_binding: Mapping[str, Any],
    *,
    source: str,
) -> list[str]:
    errors: list[str] = []
    for binding_field, summary_field, error_suffix in (
        ("producer_run_id", "summary_run_id", "producer_run_id_mismatch"),
        ("producer_artifact_ref", "producer_artifact_ref", "producer_artifact_ref_mismatch"),
        ("producer_checksum_or_receipt_id", "summary_checksum", "producer_checksum_mismatch"),
    ):
        summary_value = summary_binding.get(summary_field)
        values = list(_binding_values(receipt_binding, binding_field).values())
        if values and any(value != summary_value for value in values):
            errors.append(f"{source}_summary_{error_suffix}")
    return errors


def _scheduler_receipt_errors(
    payload: Mapping[str, Any],
    *,
    scheduler_binding: Sequence[Mapping[str, Any]],
) -> list[str]:
    errors: list[str] = []
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), Mapping) else {}
    top_level_binding = _scheduler_producer_binding(payload)
    provenance_binding = _scheduler_producer_binding(provenance)
    binding_values = {
        field: _coalesced_scheduler_binding_value(top_level_binding, provenance_binding, field)
        for field in SCHEDULER_BINDING_ALIAS_GROUPS
    }
    errors.extend(_scheduler_binding_alias_errors(top_level_binding, source="top_level"))
    errors.extend(_scheduler_binding_alias_errors(provenance_binding, source="provenance"))
    errors.extend(_scheduler_binding_consistency_errors(top_level_binding, provenance_binding))

    producer_schema = binding_values["producer_schema"]
    if producer_schema != SCHEDULER_EVIDENCE_SCHEMA:
        errors.append("producer_schema_mismatch")

    producer_run_id = binding_values["producer_run_id"]
    if not _non_empty_string(producer_run_id):
        errors.append("missing_producer_run_id")

    artifact_ref = binding_values["producer_artifact_ref"]
    if not _non_empty_string(artifact_ref):
        errors.append("missing_producer_artifact_ref")

    checksum_or_receipt = binding_values["producer_checksum_or_receipt_id"]
    if not _non_empty_string(checksum_or_receipt):
        errors.append("missing_producer_checksum_or_receipt_id")

    if not _has_meaningful_value(provenance):
        errors.append("missing_provenance")
    elif _contains_placeholder_value(provenance):
        errors.append("placeholder_provenance")

    producer_run_matches = [
        binding for binding in scheduler_binding if producer_run_id == binding.get("scheduler_pass_id")
    ]
    artifact_matches = [
        binding for binding in producer_run_matches if artifact_ref == binding.get("scheduler_artifact_ref")
    ]
    matches = [
        binding
        for binding in artifact_matches
        if checksum_or_receipt == binding.get("scheduler_checksum")
    ]
    if not scheduler_binding:
        errors.append("missing_scheduler_evidence_binding")
    elif not matches:
        errors.append("scheduler_evidence_binding_not_found")
        if not producer_run_matches:
            errors.append("producer_run_id_mismatch")
        elif not artifact_matches:
            errors.append("producer_artifact_ref_mismatch")
        else:
            errors.append("producer_checksum_mismatch")
    else:
        if len(matches) > 1:
            errors.append("ambiguous_scheduler_evidence_binding")
        binding = matches[0]
        if producer_schema != binding.get("scheduler_schema"):
            errors.append("producer_schema_mismatch")
        scheduler_mode = binding.get("scheduler_execution_mode")
        if scheduler_mode not in SCHEDULER_LIVE_PRODUCER_EXECUTION_MODES:
            errors.append("scheduler_execution_mode_not_live_eligible")
        scheduler_status = str(binding.get("scheduler_status") or "").strip().lower()
        if scheduler_status not in SCHEDULER_LIVE_WORK_STATUSES:
            errors.append("scheduler_status_not_live_eligible")
        errors.extend(_scheduler_binding_summary_errors(top_level_binding, binding, source="top_level"))
        errors.extend(_scheduler_binding_summary_errors(provenance_binding, binding, source="provenance"))
    return errors


def _coalesced_scheduler_binding_value(
    top_level_binding: Mapping[str, Any],
    provenance_binding: Mapping[str, Any],
    field: str,
) -> Any:
    top_value = _scheduler_binding_canonical_value(top_level_binding, field)
    if _has_meaningful_value(top_value):
        return top_value
    return _scheduler_binding_canonical_value(provenance_binding, field)


def _scheduler_producer_binding(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        field: {
            key: _normalized_binding_value(payload.get(key), field=field)
            for key in aliases
            if _has_meaningful_value(payload.get(key))
        }
        for field, aliases in SCHEDULER_BINDING_ALIAS_GROUPS.items()
    }


def _scheduler_binding_values(receipt_binding: Mapping[str, Any], field: str) -> dict[str, Any]:
    values = receipt_binding.get(field)
    return values if isinstance(values, dict) else {}


def _scheduler_binding_canonical_value(receipt_binding: Mapping[str, Any], field: str) -> Any:
    values = _scheduler_binding_values(receipt_binding, field)
    for alias in SCHEDULER_BINDING_ALIAS_GROUPS[field]:
        value = values.get(alias)
        if _has_meaningful_value(value):
            return value
    return None


def _scheduler_binding_alias_errors(receipt_binding: Mapping[str, Any], *, source: str) -> list[str]:
    errors: list[str] = []
    for binding_field, suffix in SCHEDULER_BINDING_ALIAS_ERROR_SUFFIXES.items():
        values = list(_scheduler_binding_values(receipt_binding, binding_field).values())
        if values and any(value != values[0] for value in values[1:]):
            errors.append(f"{source}_{suffix}")
    return errors


def _scheduler_binding_consistency_errors(
    top_level_binding: Mapping[str, Any],
    provenance_binding: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    for binding_field, error in (
        ("producer_schema", "provenance_producer_schema_mismatch"),
        ("producer_run_id", "provenance_producer_run_id_mismatch"),
        ("producer_artifact_ref", "provenance_producer_artifact_ref_mismatch"),
        ("producer_checksum_or_receipt_id", "provenance_producer_checksum_or_receipt_id_mismatch"),
    ):
        top_values = list(_scheduler_binding_values(top_level_binding, binding_field).values())
        provenance_values = list(_scheduler_binding_values(provenance_binding, binding_field).values())
        if top_values and provenance_values and any(
            top_value != provenance_value for top_value in top_values for provenance_value in provenance_values
        ):
            errors.append(error)
    return errors


def _scheduler_binding_summary_errors(
    receipt_binding: Mapping[str, Any],
    summary_binding: Mapping[str, Any],
    *,
    source: str,
) -> list[str]:
    errors: list[str] = []
    for binding_field, summary_field, error_suffix in (
        ("producer_schema", "scheduler_schema", "producer_schema_mismatch"),
        ("producer_run_id", "scheduler_pass_id", "producer_run_id_mismatch"),
        ("producer_artifact_ref", "scheduler_artifact_ref", "producer_artifact_ref_mismatch"),
        ("producer_checksum_or_receipt_id", "scheduler_checksum", "producer_checksum_mismatch"),
    ):
        summary_value = summary_binding.get(summary_field)
        values = list(_scheduler_binding_values(receipt_binding, binding_field).values())
        if values and any(value != summary_value for value in values):
            errors.append(f"{source}_summary_{error_suffix}")
    return errors


def _first_meaningful_mapping(payload: Mapping[str, Any], keys: Sequence[str]) -> Mapping[str, Any] | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping) and _has_meaningful_value(value):
            return value
    return None


def _has_any_key_value(mapping: Mapping[str, Any], keys: Sequence[str]) -> bool:
    return any(_has_meaningful_value(mapping.get(key)) for key in keys)


def _value_from(payload: Mapping[str, Any], keys: Sequence[str], *, fallback: Mapping[str, Any] | None = None) -> Any:
    for key in keys:
        if _has_meaningful_value(payload.get(key)):
            return payload.get(key)
    if fallback is not None:
        for key in keys:
            if _has_meaningful_value(fallback.get(key)):
                return fallback.get(key)
    return None


def _issue_matches(value: Any, expected: int) -> bool:
    if value == expected:
        return True
    if isinstance(value, str):
        return value.strip().lstrip("#") == str(expected)
    return False


def _contains_placeholder_value(value: Any) -> bool:
    placeholders = {"placeholder", "fabricated", "fake", "dummy", "todo", "tbd", "unknown", "null", "none"}
    if isinstance(value, str):
        stripped = value.strip().lower()
        return stripped in placeholders or stripped.startswith("placeholder-")
    if isinstance(value, Mapping):
        meaningful = [nested for nested in value.values() if _has_meaningful_value(nested)]
        return bool(meaningful) and all(_contains_placeholder_value(nested) for nested in meaningful)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        meaningful = [nested for nested in value if _has_meaningful_value(nested)]
        return bool(meaningful) and all(_contains_placeholder_value(nested) for nested in meaningful)
    return False


def redact_readiness_public_error(value: object) -> str:
    return redact_text(PATH_TOKEN_RE.sub("[redacted-path]", str(value)))


def _path_from_env(env_name: str, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit.expanduser()
    raw = os.getenv(env_name)
    return Path(raw).expanduser() if raw else None


def _safe_run_id(run_id: str) -> str:
    if SAFE_RUN_ID_RE.fullmatch(run_id):
        return run_id
    raise ProductionReadinessValidationError(
        "PRODUCTION_READINESS_RUN_ID_UNSAFE",
        "run_id may contain only alphanumeric characters, underscores, and hyphens.",
    )


def _safe_resolved_evidence_root(evidence_root: Path) -> Path:
    root = evidence_root.expanduser()
    _refuse_symlink_components_to_deepest_existing(root)
    return root.resolve(strict=False)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.command("validate-readiness")
    @_click_options
    def validate_readiness_command(**kwargs: Any) -> None:
        try:
            summary = validate_readiness(ProductionReadinessConfig.from_env(**kwargs))
            click.echo(json.dumps(redact_payload(summary), sort_keys=True))
        except ProductionReadinessValidationError as error:
            click.echo(f"{error.error_code}: {redact_readiness_public_error(error.message)}", err=True)
            raise SystemExit(1) from error
        except Exception as error:
            click.echo(
                f"PRODUCTION_READINESS_VALIDATION_FAILED: {redact_readiness_public_error(error)}",
                err=True,
            )
            raise SystemExit(1) from error

    try:
        validate_readiness_command.main(args=list(argv) if argv is not None else None, standalone_mode=False)
    except click.ClickException as error:
        error.show()
        raise SystemExit(error.exit_code) from error
    return 0


def _click_options(function: Any) -> Any:
    import click

    options = [
        click.option("--evidence-root", type=click.Path(path_type=Path), required=True),
        click.option("--run-id"),
        click.option("--slurm-evidence-root", type=click.Path(path_type=Path), default=None),
        click.option("--object-store-evidence-root", type=click.Path(path_type=Path), default=None),
        click.option("--source-evidence-root", type=click.Path(path_type=Path), default=None),
        click.option("--e2e-evidence-root", type=click.Path(path_type=Path), default=None),
        click.option("--mvt-evidence-root", type=click.Path(path_type=Path), default=None),
        click.option("--scheduler-evidence-root", type=click.Path(path_type=Path), default=None),
        click.option("--scheduler-evidence-file", type=click.Path(path_type=Path), default=None),
        click.option("--auth-proof", default=None),
        click.option("--auth-proof-file", type=click.Path(path_type=Path), default=None),
        click.option("--alert-proof", default=None),
        click.option("--alert-proof-file", type=click.Path(path_type=Path), default=None),
        click.option("--rollback-proof", default=None),
        click.option("--rollback-proof-file", type=click.Path(path_type=Path), default=None),
        click.option("--scheduler-proof", default=None),
        click.option("--scheduler-proof-file", type=click.Path(path_type=Path), default=None),
        click.option("--slurm-proof", default=None),
        click.option("--slurm-proof-file", type=click.Path(path_type=Path), default=None),
        click.option("--object-store-proof", default=None),
        click.option("--object-store-proof-file", type=click.Path(path_type=Path), default=None),
        click.option("--source-proof", default=None),
        click.option("--source-proof-file", type=click.Path(path_type=Path), default=None),
        click.option("--e2e-proof", default=None),
        click.option("--e2e-proof-file", type=click.Path(path_type=Path), default=None),
        click.option("--mvt-proof", default=None),
        click.option("--mvt-proof-file", type=click.Path(path_type=Path), default=None),
        click.option("--target-env-proof", default=None),
        click.option("--target-env-proof-file", type=click.Path(path_type=Path), default=None),
        click.option("--force", is_flag=True, default=False),
    ]
    for option in reversed(options):
        function = option(function)
    return function


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-production validate-readiness")
    _add_argparse_options(parser)
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                redact_payload(validate_readiness(ProductionReadinessConfig.from_env(**vars(args)))),
                sort_keys=True,
            )
        )
    except ProductionReadinessValidationError as error:
        print(f"{error.error_code}: {redact_readiness_public_error(error.message)}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"PRODUCTION_READINESS_VALIDATION_FAILED: {redact_readiness_public_error(error)}", file=sys.stderr)
        return 1
    return 0


def _add_argparse_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--slurm-evidence-root", type=Path, default=None)
    parser.add_argument("--object-store-evidence-root", type=Path, default=None)
    parser.add_argument("--source-evidence-root", type=Path, default=None)
    parser.add_argument("--e2e-evidence-root", type=Path, default=None)
    parser.add_argument("--mvt-evidence-root", type=Path, default=None)
    parser.add_argument("--scheduler-evidence-root", type=Path, default=None)
    parser.add_argument("--scheduler-evidence-file", type=Path, default=None)
    parser.add_argument("--auth-proof", default=None)
    parser.add_argument("--auth-proof-file", type=Path, default=None)
    parser.add_argument("--alert-proof", default=None)
    parser.add_argument("--alert-proof-file", type=Path, default=None)
    parser.add_argument("--rollback-proof", default=None)
    parser.add_argument("--rollback-proof-file", type=Path, default=None)
    parser.add_argument("--scheduler-proof", default=None)
    parser.add_argument("--scheduler-proof-file", type=Path, default=None)
    parser.add_argument("--slurm-proof", default=None)
    parser.add_argument("--slurm-proof-file", type=Path, default=None)
    parser.add_argument("--object-store-proof", default=None)
    parser.add_argument("--object-store-proof-file", type=Path, default=None)
    parser.add_argument("--source-proof", default=None)
    parser.add_argument("--source-proof-file", type=Path, default=None)
    parser.add_argument("--e2e-proof", default=None)
    parser.add_argument("--e2e-proof-file", type=Path, default=None)
    parser.add_argument("--mvt-proof", default=None)
    parser.add_argument("--mvt-proof-file", type=Path, default=None)
    parser.add_argument("--target-env-proof", default=None)
    parser.add_argument("--target-env-proof-file", type=Path, default=None)
    parser.add_argument("--force", action="store_true")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
