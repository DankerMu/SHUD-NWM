from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from packages.common.redaction import redact_payload, redact_text
from packages.common.safe_fs import (
    SafeFilesystemError,
    read_bytes_limited_no_follow,
)
from services.production_closure import (
    readiness_item_contracts as _readiness_item_contracts,
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
_path_for_evidence = _readiness_shared_artifacts._path_for_evidence
_preflight_payload = _readiness_shared_artifacts._preflight_payload
_redact_paths = _readiness_shared_artifacts._redact_paths
_refuse_symlink_components = _readiness_shared_artifacts._refuse_symlink_components
_refuse_symlink_components_to_deepest_existing = (
    _readiness_shared_artifacts._refuse_symlink_components_to_deepest_existing
)

MAX_RECEIPT_BYTES = 64 * 1024
MAX_RECEIPT_PREVIEW_BYTES = 2048
LIVE_PROOF_SCHEMA = "nhms.production_readiness.live_proof.v1"
EXPECTED_TARGET_ENVIRONMENT = "production"

DEPENDENCY_SUMMARY_CONTRACTS = {
    "slurm": {
        "issue": 147,
        "schema": "nhms.production_closure.slurm.v1",
        "allowed_statuses": {"ready", "submitted"},
    },
    "object_store": {
        "issue": 148,
        "schema": "nhms.production_closure.object_store.v1",
        "allowed_statuses": {"ready"},
    },
    "source": {
        "issue": 149,
        "schema": "nhms.production_closure.met.v1",
        "allowed_statuses": {"ready"},
    },
    "e2e": {
        "issue": 150,
        "schema": "nhms.production_closure.e2e.v1",
        "allowed_statuses": {"ready"},
    },
    "mvt": {
        "issue": 151,
        "schema": "nhms.production_closure.scale.v1",
        "allowed_statuses": {"ready"},
    },
}
PROOF_ENV = {
    "auth": "NHMS_PRODUCTION_READINESS_AUTH_PROOF",
    "alert": "NHMS_PRODUCTION_READINESS_ALERT_PROOF",
    "rollback": "NHMS_PRODUCTION_READINESS_ROLLBACK_PROOF",
    "scheduler": "NHMS_PRODUCTION_READINESS_SCHEDULER_PROOF",
    "slurm": "NHMS_PRODUCTION_READINESS_SLURM_PROOF",
    "object_store": "NHMS_PRODUCTION_READINESS_OBJECT_STORE_PROOF",
    "source": "NHMS_PRODUCTION_READINESS_SOURCE_PROOF",
    "e2e": "NHMS_PRODUCTION_READINESS_E2E_PROOF",
    "mvt": "NHMS_PRODUCTION_READINESS_MVT_PROOF",
    "target_env": "NHMS_PRODUCTION_READINESS_TARGET_ENV_PROOF",
}
SCHEDULER_EVIDENCE_SCHEMA = "nhms.production_scheduler.pass_evidence.v1"
MAX_SCHEDULER_EVIDENCE_BYTES = 256 * 1024
MAX_SCHEDULER_EVIDENCE_FILES = 16
SCHEDULER_REVIEW_EXECUTION_MODES = frozenset(
    {
        "deterministic",
        "deterministic_fixture",
        "dry_run",
        "planning_only",
        "production_like",
        "production_orchestration",
        "slurm_cancellation",
        "slurm_gateway_orchestration",
        "slurm_preflight",
        "slurm_status_sync",
        "simulated",
    }
)
SCHEDULER_LIVE_PRODUCER_EXECUTION_MODES = frozenset({"production_orchestration"})
SCHEDULER_REVIEW_PASSED_STATUSES = frozenset(
    {
        "planned",
        "ready",
        "passed",
        "submitted",
        "slurm_cancelled",
        "slurm_status_synced",
        "completed",
        "succeeded",
    }
)
SCHEDULER_REVIEW_BLOCKED_STATUSES = frozenset(
    {
        "blocked",
        "failed",
        "lock_contended",
        "permanently_failed",
        "preflight_blocked",
        "resource_limit_blocked",
        "slurm_cancellation_blocked",
        "slurm_partially_cancelled",
        "slurm_status_sync_failed",
        "submission_failed",
        "submitted_partial",
        "partial",
        "partially_failed",
    }
)
SCHEDULER_DRY_RUN_NO_MUTATION_FALSE_FIELDS = (
    "adapter_download_called",
    "slurm_submit_called",
    "slurm_status_sync_called",
    "slurm_cancellation_called",
    "shud_runtime_called",
    "hydro_result_table_writes",
    "met_result_table_writes",
    "pipeline_status_writes",
    "pipeline_event_writes",
)
SCHEDULER_PARTIAL_MODEL_RUN_STATUSES = frozenset(
    {
        "partial",
        "partially_failed",
        "submitted_partial",
    }
)
SCHEDULER_FAILED_MODEL_RUN_STATUSES = frozenset({"failed", "permanently_failed", "submission_failed"})
SCHEDULER_BLOCKED_MODEL_RUN_STATUSES = frozenset(
    {
        "blocked",
        "cancelled",
        "lock_contended",
        "preflight_blocked",
        "resource_limit_blocked",
        "unavailable",
    }
)
SCHEDULER_MODEL_RUN_STATUS_KEYS = frozenset({"status", "outcome", "result", "state"})
SCHEDULER_REQUIRED_COUNT_FIELDS = (
    "candidate_count",
    "blocked_candidate_count",
    "skipped_candidate_count",
    "submitted_count",
    "failed_count",
    "partial_count",
)
SCHEDULER_LIVE_WORK_STATUSES = frozenset({"submitted", "completed", "succeeded", "passed"})
SCHEDULER_LIVE_MODEL_RUN_STATUS_COMPATIBILITY: Mapping[str, frozenset[str]] = {
    "submitted": frozenset(
        {
            "accepted",
            "active",
            "complete",
            "completed",
            "passed",
            "published",
            "queued",
            "running",
            "submitted",
            "succeeded",
            "success",
        }
    ),
    "completed": frozenset({"complete", "completed", "passed", "published", "succeeded", "success"}),
    "succeeded": frozenset({"complete", "completed", "passed", "published", "succeeded", "success"}),
    "passed": frozenset({"complete", "completed", "passed", "published", "succeeded", "success"}),
}
SCHEDULER_LIVE_COMPATIBLE_MODEL_RUN_STATUSES = frozenset(
    status
    for statuses in SCHEDULER_LIVE_MODEL_RUN_STATUS_COMPATIBILITY.values()
    for status in statuses
)
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


@dataclass(frozen=True)
class _SchedulerCandidateIdentity:
    source_id: str
    cycle_identity: str
    model_id: str
    scenario_id: str


@dataclass(frozen=True)
class _SchedulerModelRunOutcome:
    status_values: frozenset[str]
    has_status_evidence: bool
    submitted: bool
    submitted_explicitly_false: bool
    failed: bool
    partial: bool
    blocked: bool
    producer_partial: bool


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
    items: list[dict[str, Any]] = []
    for name in DEPENDENCY_SUMMARY_CONTRACTS:
        root = config.dependency_roots.get(name)
        if root is None:
            items.append(
                _item(
                    item_id=f"deterministic-{name}-summary",
                    surface=f"{name}_production_like_evidence",
                    status="not_executed",
                    execution_mode="not_executed",
                    required_for_final=False,
                    live_proof_accepted=False,
                    artifact_refs=[],
                    residual_risk=(
                        "No existing production-closure summary was supplied to this readiness run; "
                        "deterministic fast CI remains self-contained."
                    ),
                    removal_criteria=(
                        f"Run or provide the {name} production-closure summary when deterministic dependency "
                        "lineage is needed for release review."
                    ),
                )
            )
            continue
        items.append(_read_dependency_summary_item(name, root, config=config))
    return items


def _read_dependency_summary_item(name: str, root: Path, *, config: ProductionReadinessConfig) -> dict[str, Any]:
    contract = DEPENDENCY_SUMMARY_CONTRACTS[name]
    try:
        summary_path = _find_summary_path(name, root)
        raw = read_bytes_limited_no_follow(summary_path, max_bytes=MAX_RECEIPT_BYTES)
        if len(raw) > MAX_RECEIPT_BYTES:
            return _dependency_summary_blocked(
                name,
                summary_path,
                config=config,
                reason="Dependency summary exceeds bounded readiness ingestion limit.",
            )
        summary = json.loads(raw.decode("utf-8"))
        if not isinstance(summary, Mapping):
            return _dependency_summary_blocked(
                name,
                summary_path,
                config=config,
                reason="Dependency summary JSON must be an object.",
            )
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        RecursionError,
        UnicodeDecodeError,
        SafeFilesystemError,
    ) as error:
        return _dependency_summary_blocked(
            name,
            root,
            config=config,
            reason=f"Dependency summary could not be read: {_redact_paths(str(error), config=config)}.",
        )
    status = str(summary.get("status", "unknown"))
    schema_ok = summary.get("schema") == contract["schema"]
    issue_ok = summary.get("issue") == contract["issue"]
    accepted_status = status in contract["allowed_statuses"]
    item_status = "passed" if schema_ok and issue_ok and accepted_status else "blocked"
    summary_checksum = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    producer_artifact_ref = _dependency_summary_artifact_ref(name, summary_path, root)
    return _item(
        item_id=f"deterministic-{name}-summary",
        surface=f"{name}_production_like_evidence",
        status=item_status,
        execution_mode="deterministic" if item_status == "passed" else "not_executed",
        required_for_final=False,
        live_proof_accepted=False,
        artifact_refs=[_path_for_evidence(summary_path, config=config)],
        residual_risk=(
            "Existing production-closure summary was consumed as deterministic review evidence; it is not live proof."
            if item_status == "passed"
            else "Existing production-closure summary is missing, malformed, or outside the expected contract."
        ),
        removal_criteria=(
            "Provide accepted live proof receipt for final readiness; keep deterministic producer evidence available "
            "for reviewer lineage."
            if item_status == "passed"
            else f"Provide a {contract['schema']} summary with accepted status for deterministic readiness review."
        ),
        dependencies=[
            f"issue=#{contract['issue']}",
            f"schema={contract['schema']}",
            f"summary_status={status}",
            f"producer_artifact_ref={producer_artifact_ref}",
            f"summary_checksum={summary_checksum}",
        ],
        details=_bounded_redacted_payload(
            {
                "dependency": name,
                "producer_issue": contract["issue"],
                "producer_schema": contract["schema"],
                "summary_schema": summary.get("schema"),
                "summary_issue": summary.get("issue"),
                "summary_run_id": summary.get("run_id"),
                "summary_status": status,
                "summary_execution_mode": summary.get("execution_mode"),
                "summary_final_production_readiness_claimed": summary.get("final_production_readiness_claimed"),
                "producer_artifact_ref": producer_artifact_ref,
                "summary_checksum": summary_checksum,
            },
            config=config,
        ),
    )


def _dependency_summary_blocked(
    name: str,
    path: Path,
    *,
    config: ProductionReadinessConfig,
    reason: str,
) -> dict[str, Any]:
    return _item(
        item_id=f"deterministic-{name}-summary",
        surface=f"{name}_production_like_evidence",
        status="blocked",
        execution_mode="not_executed",
        required_for_final=False,
        live_proof_accepted=False,
        artifact_refs=[_path_for_evidence(path, config=config)],
        residual_risk=reason,
        removal_criteria=f"Provide a readable bounded {name} production-closure summary.json artifact.",
    )


def _dependency_summary_artifact_ref(name: str, summary_path: Path, root: Path) -> str:
    try:
        relative = summary_path.resolve(strict=False).relative_to(root.expanduser().resolve(strict=False))
    except ValueError:
        relative = Path("summary.json")
    return f"{name}:{relative.as_posix()}"


def _dependency_bindings(items: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    bindings: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if item.get("status") != "passed":
            continue
        details = item.get("details")
        if not isinstance(details, Mapping):
            continue
        dependency = details.get("dependency")
        if isinstance(dependency, str) and dependency in DEPENDENCY_SUMMARY_CONTRACTS:
            bindings[dependency] = details
    return bindings


def _scheduler_evidence_items(config: ProductionReadinessConfig) -> list[dict[str, Any]]:
    configured = config.scheduler_evidence_root is not None or config.scheduler_evidence_file is not None
    if not configured:
        return []
    if config.scheduler_evidence_root is not None and config.scheduler_evidence_file is not None:
        return [
            _scheduler_evidence_blocked(
                config.scheduler_evidence_file,
                config=config,
                reason="Provide either scheduler_evidence_root or scheduler_evidence_file, not both.",
                error_code="PRODUCTION_READINESS_SCHEDULER_EVIDENCE_AMBIGUOUS",
            )
        ]
    if config.scheduler_evidence_file is not None:
        return [_read_scheduler_evidence_item(config.scheduler_evidence_file, config=config)]
    return _read_scheduler_evidence_root_items(config.scheduler_evidence_root, config=config)


def _read_scheduler_evidence_root_items(
    root: Path | None,
    *,
    config: ProductionReadinessConfig,
) -> list[dict[str, Any]]:
    if root is None:
        return []
    try:
        evidence_files = _find_scheduler_evidence_files(root)
    except (FileNotFoundError, OSError, SafeFilesystemError, ProductionReadinessValidationError) as error:
        return [
            _scheduler_evidence_blocked(
                root,
                config=config,
                reason=f"Scheduler evidence could not be discovered: {_redact_paths(str(error), config=config)}.",
                error_code="PRODUCTION_READINESS_SCHEDULER_EVIDENCE_DISCOVERY_FAILED",
            )
        ]
    if not evidence_files:
        return [
            _scheduler_evidence_blocked(
                root,
                config=config,
                reason="No scheduler evidence JSON file was found under the configured scheduler evidence root.",
                error_code="PRODUCTION_READINESS_SCHEDULER_EVIDENCE_MISSING",
            )
        ]
    if len(evidence_files) > MAX_SCHEDULER_EVIDENCE_FILES:
        return [
            _scheduler_evidence_blocked(
                root,
                config=config,
                reason=f"Scheduler evidence root contains more than {MAX_SCHEDULER_EVIDENCE_FILES} JSON artifacts.",
                error_code="PRODUCTION_READINESS_SCHEDULER_EVIDENCE_FILE_LIMIT",
            )
        ]
    return [_read_scheduler_evidence_item(path, config=config) for path in evidence_files]


def _read_scheduler_evidence_item(path: Path, *, config: ProductionReadinessConfig) -> dict[str, Any]:
    try:
        evidence_path = _safe_scheduler_evidence_file(path)
        raw = read_bytes_limited_no_follow(evidence_path, max_bytes=MAX_SCHEDULER_EVIDENCE_BYTES)
        if len(raw) > MAX_SCHEDULER_EVIDENCE_BYTES:
            return _scheduler_evidence_blocked(
                evidence_path,
                config=config,
                reason=f"Scheduler evidence exceeds {MAX_SCHEDULER_EVIDENCE_BYTES} bytes.",
                error_code="PRODUCTION_READINESS_SCHEDULER_EVIDENCE_TOO_LARGE",
                raw_preview=_redacted_preview(raw, config=config),
            )
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, Mapping):
            return _scheduler_evidence_blocked(
                evidence_path,
                config=config,
                reason="Scheduler evidence JSON must be an object.",
                error_code="PRODUCTION_READINESS_SCHEDULER_EVIDENCE_JSON_INVALID",
                raw_preview=_redacted_preview(raw, config=config),
            )
        raw_payload = parsed
        payload = _bounded_redacted_payload(parsed, config=config)
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        RecursionError,
        UnicodeDecodeError,
        SafeFilesystemError,
        ProductionReadinessValidationError,
    ) as error:
        return _scheduler_evidence_blocked(
            path,
            config=config,
            reason=f"Scheduler evidence could not be read: {_redact_paths(str(error), config=config)}.",
            error_code="PRODUCTION_READINESS_SCHEDULER_EVIDENCE_READ_FAILED",
        )

    errors = _scheduler_evidence_errors(raw_payload)
    status = str(raw_payload.get("status") or "unknown").strip()
    execution_mode = _scheduler_evidence_mode(raw_payload)
    item_status = _scheduler_readiness_status(raw_payload, errors=errors)
    summary_checksum = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    artifact_ref = _scheduler_evidence_artifact_ref(evidence_path, config=config)
    details = _bounded_redacted_payload(
        {
            "producer": "production_scheduler",
            "producer_schema": SCHEDULER_EVIDENCE_SCHEMA,
            "scheduler_schema": raw_payload.get("schema") or raw_payload.get("schema_version"),
            "scheduler_pass_id": raw_payload.get("pass_id"),
            "scheduler_status": status,
            "scheduler_execution_mode": execution_mode,
            "scheduler_artifact_ref": artifact_ref,
            "scheduler_checksum": summary_checksum,
            "candidate_count": _count_value(raw_payload, "candidate_count"),
            "blocked_candidate_count": _count_value(raw_payload, "blocked_candidate_count"),
            "submitted_count": _count_value(raw_payload, "submitted_count"),
            "skipped_candidate_count": _count_value(raw_payload, "skipped_candidate_count"),
            "partial_count": _count_value(raw_payload, "partial_count"),
            "failed_count": _count_value(raw_payload, "failed_count"),
            "no_mutation_proof": raw_payload.get("no_mutation_proof"),
            "execution_boundary": raw_payload.get("execution_boundary"),
            "acceptance_errors": errors,
            "payload": payload,
        },
        config=config,
    )
    return _item(
        item_id=f"deterministic-scheduler-evidence-{_scheduler_item_suffix(raw_payload, evidence_path)}",
        surface="scheduler_production_like_evidence",
        status=item_status,
        execution_mode="deterministic" if item_status == "passed" else "not_executed",
        required_for_final=False,
        live_proof_accepted=False,
        artifact_refs=[_path_for_evidence(evidence_path, config=config)],
        residual_risk=(
            "Scheduler evidence was consumed as deterministic/non-final review evidence; it is not live proof."
            if item_status == "passed"
            else "Scheduler evidence is malformed, stale, unsafe, or outside the expected review contract."
        ),
        removal_criteria=(
            "Provide an accepted live scheduler evidence receipt for live proof; keep scheduler evidence available "
            "for reviewer lineage."
            if item_status == "passed"
            else "Provide bounded scheduler pass evidence with matching schema, pass id, execution mode, and counts."
        ),
        dependencies=[
            f"schema={SCHEDULER_EVIDENCE_SCHEMA}",
            f"scheduler_pass_id={raw_payload.get('pass_id')}",
            f"scheduler_status={status}",
            f"scheduler_execution_mode={execution_mode}",
            f"producer_artifact_ref={artifact_ref}",
            f"scheduler_checksum={summary_checksum}",
        ],
        details=details,
    )


def _scheduler_evidence_blocked(
    path: Path,
    *,
    config: ProductionReadinessConfig,
    reason: str,
    error_code: str = "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_BLOCKED",
    raw_preview: str | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "producer": "production_scheduler",
        "producer_schema": SCHEDULER_EVIDENCE_SCHEMA,
        "error_code": error_code,
        "reason": reason,
    }
    if raw_preview is not None:
        details["raw_preview"] = raw_preview
    return _item(
        item_id="deterministic-scheduler-evidence-blocked",
        surface="scheduler_production_like_evidence",
        status="blocked",
        execution_mode="not_executed",
        required_for_final=False,
        live_proof_accepted=False,
        artifact_refs=[_path_for_evidence(path, config=config)],
        residual_risk=reason,
        removal_criteria="Provide a readable bounded production scheduler evidence JSON artifact.",
        details=_bounded_redacted_payload(details, config=config),
    )


def _scheduler_bindings(items: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    bindings: list[Mapping[str, Any]] = []
    for item in items:
        if item.get("status") != "passed":
            continue
        details = item.get("details")
        if isinstance(details, Mapping):
            bindings.append(details)
    return tuple(bindings)


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


def _load_proof(
    surface: str,
    proof_json: str | None,
    proof_file: Path | None,
    *,
    config: ProductionReadinessConfig,
) -> dict[str, Any]:
    if proof_json and proof_file:
        return {
            "surface": surface,
            "status": "invalid",
            "source": "ambiguous",
            "error_code": "PRODUCTION_READINESS_PROOF_AMBIGUOUS",
            "reason": "Provide either a JSON proof string or a proof file, not both.",
        }
    if not proof_json and proof_file is None:
        return {
            "surface": surface,
            "status": "missing",
            "source": "not_configured",
            "reason": "No live proof receipt configured.",
        }
    if proof_file is not None:
        try:
            raw = read_bytes_limited_no_follow(proof_file.expanduser(), max_bytes=MAX_RECEIPT_BYTES)
        except (OSError, SafeFilesystemError) as error:
            return {
                "surface": surface,
                "status": "invalid",
                "source": "file",
                "path": _path_for_evidence(proof_file, config=config),
                "error_code": "PRODUCTION_READINESS_PROOF_FILE_INVALID",
                "reason": redact_text(str(error)),
            }
        source = "file"
        source_ref = _path_for_evidence(proof_file, config=config)
    else:
        raw = str(proof_json).encode("utf-8", errors="replace")
        source = "json_string"
        source_ref = "inline_json"
    if len(raw) > MAX_RECEIPT_BYTES:
        return {
            "surface": surface,
            "status": "too_large",
            "source": source,
            "source_ref": source_ref,
            "error_code": "PRODUCTION_READINESS_PROOF_TOO_LARGE",
            "reason": f"Live proof payload exceeds {MAX_RECEIPT_BYTES} bytes.",
            "raw_preview": _redacted_preview(raw, config=config),
        }
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        return {
            "surface": surface,
            "status": "invalid",
            "source": source,
            "source_ref": source_ref,
            "error_code": "PRODUCTION_READINESS_PROOF_JSON_INVALID",
            "reason": redact_text(str(error)),
            "raw_preview": _redacted_preview(raw, config=config),
        }
    if not isinstance(parsed, Mapping):
        return {
            "surface": surface,
            "status": "invalid",
            "source": source,
            "source_ref": source_ref,
            "error_code": "PRODUCTION_READINESS_PROOF_JSON_INVALID",
            "reason": "Live proof payload must be a JSON object.",
            "raw_preview": _redacted_preview(raw, config=config),
        }
    try:
        bounded = _bounded_payload(parsed)
        raw_payload = bounded.payload
        payload = _bounded_redacted_payload(parsed, config=config)
    except RecursionError as error:
        return {
            "surface": surface,
            "status": "invalid",
            "source": source,
            "source_ref": source_ref,
            "error_code": "PRODUCTION_READINESS_PROOF_JSON_INVALID",
            "reason": redact_text(str(error)),
            "raw_preview": _redacted_preview(raw, config=config),
        }
    json_limit_errors = []
    if bounded.node_truncated:
        json_limit_errors.append("json_node_limit_exceeded")
    if bounded.depth_truncated:
        json_limit_errors.append("json_depth_limit_exceeded")
    if json_limit_errors:
        return {
            "surface": surface,
            "status": "invalid",
            "parse_status": "json_limit_exceeded",
            "source": source,
            "source_ref": source_ref,
            "error_code": "PRODUCTION_READINESS_PROOF_JSON_LIMIT_EXCEEDED",
            "reason": "Live proof JSON exceeded bounded traversal limits.",
            "json_limit_errors": json_limit_errors,
            "payload": payload,
        }
    return {
        "surface": surface,
        "status": "parsed",
        "source": source,
        "source_ref": source_ref,
        "raw_payload": raw_payload,
        "payload": payload,
    }


def _receipt_artifact(config: ProductionReadinessConfig, receipts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "nhms.production_readiness.live_proof_receipts.v1",
        "run_id": config.run_id,
        "receipts": {surface: _receipt_details(receipt, config=config) for surface, receipt in receipts.items()},
        "redaction": {
            "secrets_redacted": True,
            "local_paths_redacted": True,
            "payload_depth_bounded": True,
            "payload_size_bounded": True,
        },
    }


def _receipt_details(receipt: Mapping[str, Any], *, config: ProductionReadinessConfig) -> dict[str, Any]:
    return _bounded_redacted_payload(
        {key: value for key, value in receipt.items() if key not in {"payload", "raw_payload"}}
        | ({"payload": receipt.get("payload")} if "payload" in receipt else {}),
        config=config,
    )


def _receipt_validation_payload(receipt: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = receipt.get("raw_payload", receipt.get("payload"))
    return payload if isinstance(payload, Mapping) else {}


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


def _find_summary_path(name: str, root: Path) -> Path:
    root = root.expanduser()
    candidates = [root / "summary.json", root / name / "summary.json"]
    if name == "object_store":
        candidates.append(root / "object-store" / "summary.json")
    for candidate in candidates:
        if candidate.exists():
            _refuse_symlink_components(candidate)
            if candidate.is_symlink():
                raise SafeFilesystemError(f"Dependency summary must not be a symlink: {candidate}")
            try:
                file_stat = candidate.stat(follow_symlinks=False)
            except OSError as error:
                raise SafeFilesystemError(f"Failed to stat dependency summary: {candidate}", kind="io") from error
            if not stat.S_ISREG(file_stat.st_mode):
                raise SafeFilesystemError(f"Dependency summary must be a regular file: {candidate}")
            return candidate
    raise FileNotFoundError(f"No summary.json found under {root}")


def _find_scheduler_evidence_files(root: Path) -> list[Path]:
    root = root.expanduser()
    _refuse_symlink_components(root)
    try:
        root_stat = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise SafeFilesystemError(f"Failed to stat scheduler evidence root: {root}", kind="io") from error
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SafeFilesystemError(f"Scheduler evidence root must be a directory: {root}")
    candidates: list[Path] = []
    with os.scandir(root) as entries:
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            candidates.append(root / entry.name)
            if len(candidates) > MAX_SCHEDULER_EVIDENCE_FILES:
                return candidates
    return [_safe_scheduler_evidence_file(candidate) for candidate in sorted(candidates, key=lambda path: path.name)]


def _safe_scheduler_evidence_file(path: Path) -> Path:
    candidate = path.expanduser()
    _refuse_symlink_components(candidate)
    if candidate.is_symlink():
        raise SafeFilesystemError(f"Scheduler evidence must not be a symlink: {candidate}")
    try:
        file_stat = candidate.stat(follow_symlinks=False)
    except OSError as error:
        raise SafeFilesystemError(f"Failed to stat scheduler evidence: {candidate}", kind="io") from error
    if not stat.S_ISREG(file_stat.st_mode):
        raise SafeFilesystemError(f"Scheduler evidence must be a regular file: {candidate}")
    return candidate


def _scheduler_evidence_errors(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    schema = payload.get("schema") or payload.get("schema_version")
    if schema != SCHEDULER_EVIDENCE_SCHEMA:
        errors.append("schema_mismatch")
    if not _non_empty_string(payload.get("pass_id")):
        errors.append("missing_pass_id")
    execution_mode = _scheduler_evidence_mode(payload)
    if execution_mode not in SCHEDULER_REVIEW_EXECUTION_MODES:
        errors.append("execution_mode_not_review_evidence")
    status = str(payload.get("status") or "").strip()
    if not status:
        errors.append("missing_status")
    elif status not in SCHEDULER_REVIEW_PASSED_STATUSES and status not in SCHEDULER_REVIEW_BLOCKED_STATUSES:
        errors.append("status_not_allowed")
    counts = {count_field: _count_value(payload, count_field) for count_field in SCHEDULER_REQUIRED_COUNT_FIELDS}
    for count_field, value in counts.items():
        if not _count_value_present(payload, count_field) or value is None:
            errors.append(f"missing_{count_field}")
    if any(value is not None and value < 0 for value in counts.values()):
        errors.append("negative_counts")
    if _scheduler_evidence_is_stale(payload):
        errors.append("stale_scheduler_evidence")
    if _has_final_readiness_claim(payload):
        errors.append("scheduler_evidence_claimed_final_readiness")
    if execution_mode == "dry_run" and not _dry_run_no_mutation_proven(payload):
        errors.append("dry_run_no_mutation_proof_missing")
    if _has_unsafe_scheduler_identity(payload):
        errors.append("unsafe_scheduler_identity")
    errors.extend(_scheduler_identity_errors(payload))
    return errors


def _scheduler_readiness_status(payload: Mapping[str, Any], *, errors: Sequence[str]) -> str:
    if errors:
        return "blocked"
    status = str(payload.get("status") or "").strip()
    if status in SCHEDULER_REVIEW_BLOCKED_STATUSES or status.endswith(("_blocked", "_failed")):
        return "blocked"
    return "passed"


def _scheduler_evidence_mode(payload: Mapping[str, Any]) -> str:
    for key in ("execution_mode", "proof_mode", "mode"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _count_value_present(payload: Mapping[str, Any], field: str) -> bool:
    counts = payload.get("counts")
    if isinstance(counts, Mapping) and field in counts:
        return True
    return field in payload


def _count_value(payload: Mapping[str, Any], field: str) -> int | None:
    counts = payload.get("counts")
    value = counts.get(field) if isinstance(counts, Mapping) and field in counts else payload.get(field)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _scheduler_evidence_is_stale(payload: Mapping[str, Any]) -> bool:
    stale = payload.get("stale")
    if stale is True:
        return True
    freshness = payload.get("freshness") if isinstance(payload.get("freshness"), Mapping) else {}
    if freshness.get("stale") is True:
        return True
    if str(payload.get("status") or "").strip() in {"stale", "expired"}:
        return True
    return False


def _has_final_readiness_claim(payload: Mapping[str, Any]) -> bool:
    if payload.get("final_production_readiness_claimed") is True:
        return True
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), Mapping) else {}
    return readiness.get("production_ready") is True or readiness.get("final_production_readiness_claimed") is True


def _dry_run_no_mutation_proven(payload: Mapping[str, Any]) -> bool:
    proof = payload.get("no_mutation_proof")
    if not isinstance(proof, Mapping):
        return False
    return all(proof.get(key) is False for key in SCHEDULER_DRY_RUN_NO_MUTATION_FALSE_FIELDS)


def _has_unsafe_scheduler_identity(payload: Mapping[str, Any]) -> bool:
    identities = [payload.get("pass_id")]
    for collection_key in ("candidates", "blocked_candidates", "skipped_candidates", "model_run_evidence"):
        collection = payload.get(collection_key)
        if isinstance(collection, Sequence) and not isinstance(collection, (str, bytes, bytearray)):
            for item in collection:
                if isinstance(item, Mapping):
                    identities.extend(
                        item.get(key)
                        for key in (
                            "candidate_id",
                            "run_id",
                            "forcing_version_id",
                            "model_id",
                            "source_id",
                            "scenario_id",
                        )
                    )
    return any(isinstance(value, str) and _identity_value_looks_unsafe(value) for value in identities)


def _scheduler_identity_errors(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    candidate_count = _count_value(payload, "candidate_count")
    candidate_records = _scheduler_collection_identity_records(payload, "candidates")
    blocked_records = _scheduler_collection_identity_records(payload, "blocked_candidates")
    skipped_records = _scheduler_collection_identity_records(payload, "skipped_candidates")
    model_run_records = _scheduler_model_run_identity_records(payload)
    candidate_side_records = candidate_records + blocked_records + skipped_records
    identities = (
        [("candidates", record) for record in candidate_records]
        + [("blocked_candidates", record) for record in blocked_records]
        + [("skipped_candidates", record) for record in skipped_records]
        + [("model_run_evidence", record) for record in model_run_records]
    )
    if candidate_count is not None and candidate_count > 0 and not candidate_side_records:
        return ["missing_scheduler_candidate_identity"]

    selected_identity_by_candidate_id: dict[str, Mapping[str, Any]] = {}
    blocked_candidate_ids: set[str] = set()
    skipped_candidate_ids: set[str] = set()
    for collection_name, record in identities:
        record_errors = _scheduler_identity_record_errors(record, collection_name=collection_name)
        errors.extend(error for error in record_errors if error not in errors)
        candidate_id = _identity_string(record.get("candidate_id"))
        if not candidate_id:
            continue
        if collection_name == "candidates":
            if candidate_id in selected_identity_by_candidate_id:
                errors.append("duplicate_scheduler_candidate_identity")
            selected_identity_by_candidate_id.setdefault(candidate_id, record)
        elif collection_name == "blocked_candidates":
            blocked_candidate_ids.add(candidate_id)
        elif collection_name == "skipped_candidates":
            skipped_candidate_ids.add(candidate_id)

    for record in _scheduler_model_run_identity_records(payload):
        errors.extend(
            error
            for error in _scheduler_model_run_identity_errors(
                record,
                selected_identity_by_candidate_id=selected_identity_by_candidate_id,
                blocked_candidate_ids=blocked_candidate_ids,
                skipped_candidate_ids=skipped_candidate_ids,
            )
            if error not in errors
        )
    errors.extend(error for error in _scheduler_count_cardinality_errors(payload) if error not in errors)
    return errors


def _scheduler_collection_identity_records(payload: Mapping[str, Any], collection_name: str) -> list[Mapping[str, Any]]:
    return _mapping_sequence(payload.get(collection_name))


def _scheduler_model_run_identity_records(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for record in _mapping_sequence(payload.get("model_run_evidence")):
        records.append(record)
        nested = record.get("candidate_identity")
        if isinstance(nested, Mapping):
            records.append(nested)
    return records


def _scheduler_identity_record_errors(record: Mapping[str, Any], *, collection_name: str) -> list[str]:
    errors: list[str] = []
    if not _identity_string(record.get("candidate_id")):
        errors.append(f"{collection_name}_missing_candidate_id")
    if not _identity_string(record.get("source_id")):
        errors.append(f"{collection_name}_missing_source_id")
    if not _identity_string(record.get("cycle_time_utc")) and not _identity_string(record.get("cycle_id")):
        errors.append(f"{collection_name}_missing_cycle_identity")
    if not _identity_string(record.get("model_id")):
        errors.append(f"{collection_name}_missing_model_id")
    if not _identity_string(record.get("scenario_id")):
        errors.append(f"{collection_name}_missing_scenario_id")
    if collection_name in {"candidates", "model_run_evidence"}:
        if not _identity_string(record.get("run_id")):
            errors.append(f"{collection_name}_missing_run_id")
        if not _identity_string(record.get("forcing_version_id")):
            errors.append(f"{collection_name}_missing_forcing_version_id")
    parsed_identity = _parse_scheduler_candidate_identity(record.get("candidate_id"))
    if parsed_identity is not None:
        errors.extend(_scheduler_candidate_identity_mismatch_errors(record, parsed_identity, collection_name))
    elif _identity_string(record.get("candidate_id")):
        errors.append(f"{collection_name}_candidate_id_identity_mismatch")
    errors.extend(_scheduler_run_forcing_derivation_errors(record, parsed_identity, collection_name))
    return errors


def _scheduler_model_run_identity_errors(
    record: Mapping[str, Any],
    *,
    selected_identity_by_candidate_id: Mapping[str, Mapping[str, Any]],
    blocked_candidate_ids: set[str],
    skipped_candidate_ids: set[str],
) -> list[str]:
    errors = _scheduler_identity_record_errors(record, collection_name="model_run_evidence")
    candidate_id = _identity_string(record.get("candidate_id"))
    if not candidate_id:
        return errors
    candidate_record = selected_identity_by_candidate_id.get(candidate_id)
    if candidate_id in blocked_candidate_ids:
        errors.append("model_run_evidence_candidate_blocked")
    if candidate_id in skipped_candidate_ids:
        errors.append("model_run_evidence_candidate_skipped")
    if candidate_record is None:
        errors.append("model_run_evidence_candidate_not_selected")
    else:
        for field in (
            "source_id",
            "cycle_time_utc",
            "cycle_id",
            "model_id",
            "scenario_id",
            "run_id",
            "forcing_version_id",
        ):
            record_value = _identity_string(record.get(field))
            candidate_value = _identity_string(candidate_record.get(field))
            if record_value and candidate_value and record_value != candidate_value:
                errors.append(f"model_run_evidence_{field}_identity_mismatch")
    nested_run_id = _model_run_nested_value(record, "run_id")
    if nested_run_id and _identity_string(record.get("run_id")) != nested_run_id:
        errors.append("model_run_evidence_run_id_mismatch")
    nested_forcing_version_id = _model_run_nested_value(record, "forcing_version_id")
    if nested_forcing_version_id and _identity_string(record.get("forcing_version_id")) != nested_forcing_version_id:
        errors.append("model_run_evidence_forcing_version_id_mismatch")
    return errors


def _model_run_nested_value(record: Mapping[str, Any], field: str) -> str:
    values: list[str] = []
    if field == "forcing_version_id":
        nested = record.get("forcing")
        if isinstance(nested, Mapping):
            values.append(_identity_string(nested.get("forcing_version_id") or nested.get("id")))
        nested = record.get("forcing_version")
        if isinstance(nested, Mapping):
            values.append(_identity_string(nested.get("forcing_version_id") or nested.get("id")))
    elif field == "run_id":
        nested = record.get("hydro_run")
        if isinstance(nested, Mapping):
            values.append(_identity_string(nested.get("run_id") or nested.get("id")))
        nested = record.get("run")
        if isinstance(nested, Mapping):
            values.append(_identity_string(nested.get("run_id") or nested.get("id")))
    meaningful = [value for value in values if value]
    if not meaningful:
        return ""
    if any(value != meaningful[0] for value in meaningful[1:]):
        return ""
    return meaningful[0]


def _parse_scheduler_candidate_identity(value: Any) -> _SchedulerCandidateIdentity | None:
    candidate_id = _identity_string(value)
    if not candidate_id:
        return None
    parts = candidate_id.split(":")
    if len(parts) < 4 or not all(parts):
        return None
    cycle_identity = ":".join(parts[1:-2])
    if not cycle_identity:
        return None
    return _SchedulerCandidateIdentity(
        source_id=parts[0],
        cycle_identity=cycle_identity,
        model_id=parts[-2],
        scenario_id=parts[-1],
    )


def _scheduler_candidate_identity_mismatch_errors(
    record: Mapping[str, Any],
    parsed_identity: _SchedulerCandidateIdentity,
    collection_name: str,
) -> list[str]:
    errors: list[str] = []
    explicit_source = _identity_string(record.get("source_id"))
    if explicit_source and parsed_identity.source_id != explicit_source:
        errors.append(f"{collection_name}_candidate_id_identity_mismatch")
    explicit_model = _identity_string(record.get("model_id"))
    if explicit_model and parsed_identity.model_id != explicit_model:
        errors.append(f"{collection_name}_candidate_id_identity_mismatch")
    explicit_scenario = _identity_string(record.get("scenario_id"))
    if explicit_scenario and parsed_identity.scenario_id != explicit_scenario:
        errors.append(f"{collection_name}_scenario_id_identity_mismatch")
    explicit_cycle_time = _identity_string(record.get("cycle_time_utc"))
    explicit_cycle_id = _identity_string(record.get("cycle_id"))
    explicit_cycles: set[str] = set()
    explicit_cycles.update(_cycle_identity_aliases(explicit_cycle_time))
    explicit_cycles.update(_cycle_identity_aliases(explicit_cycle_id))
    parsed_cycles = _cycle_identity_aliases(parsed_identity.cycle_identity)
    if explicit_cycles and parsed_cycles.isdisjoint(explicit_cycles):
        errors.append(f"{collection_name}_candidate_id_identity_mismatch")
    return _dedupe_errors(errors)


def _scheduler_run_forcing_derivation_errors(
    record: Mapping[str, Any],
    parsed_identity: _SchedulerCandidateIdentity | None,
    collection_name: str,
) -> list[str]:
    expected = _scheduler_expected_run_forcing_ids(record, parsed_identity)
    if expected is None:
        return []
    errors: list[str] = []
    expected_run_id, expected_forcing_version_id = expected
    run_id = _identity_string(record.get("run_id"))
    if run_id and run_id != expected_run_id:
        errors.append(f"{collection_name}_run_id_derivation_mismatch")
    forcing_version_id = _identity_string(record.get("forcing_version_id"))
    if forcing_version_id and forcing_version_id != expected_forcing_version_id:
        errors.append(f"{collection_name}_forcing_version_id_derivation_mismatch")
    return errors


def _scheduler_expected_run_forcing_ids(
    record: Mapping[str, Any],
    parsed_identity: _SchedulerCandidateIdentity | None,
) -> tuple[str, str] | None:
    source_id = _scheduler_explicit_or_parsed_identity(record, "source_id", parsed_identity)
    model_id = _scheduler_explicit_or_parsed_identity(record, "model_id", parsed_identity)
    cycle_token = _scheduler_compact_cycle_token(record, parsed_identity)
    if not source_id or not model_id or not cycle_token:
        return None
    source_lower = source_id.lower()
    return (
        f"fcst_{source_lower}_{cycle_token}_{model_id}",
        f"forc_{source_lower}_{cycle_token}_{model_id}",
    )


def _scheduler_explicit_or_parsed_identity(
    record: Mapping[str, Any],
    field: str,
    parsed_identity: _SchedulerCandidateIdentity | None,
) -> str:
    explicit = _identity_string(record.get(field))
    if explicit:
        return explicit
    if parsed_identity is None:
        return ""
    if field == "source_id":
        return parsed_identity.source_id
    if field == "model_id":
        return parsed_identity.model_id
    if field == "scenario_id":
        return parsed_identity.scenario_id
    return ""


def _scheduler_compact_cycle_token(
    record: Mapping[str, Any],
    parsed_identity: _SchedulerCandidateIdentity | None,
) -> str:
    for value in (
        _identity_string(record.get("cycle_time_utc")),
        _identity_string(record.get("cycle_id")),
        parsed_identity.cycle_identity if parsed_identity is not None else "",
    ):
        token = _compact_cycle_token(value)
        if token:
            return token
    return ""


def _compact_cycle_token(value: str) -> str:
    if not value:
        return ""
    compact = _compact_cycle_id_suffix(value)
    if compact.isdigit() and len(compact) == 10:
        return compact
    parsed = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(parsed).astimezone(UTC).strftime("%Y%m%d%H")
    except ValueError:
        return ""


def _scheduler_count_cardinality_errors(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    candidates = _scheduler_collection_identity_records(payload, "candidates")
    blocked_candidates = _scheduler_collection_identity_records(payload, "blocked_candidates")
    skipped_candidates = _scheduler_collection_identity_records(payload, "skipped_candidates")
    model_run_rows = _mapping_sequence(payload.get("model_run_evidence"))
    model_run_outcomes = [_scheduler_model_run_outcome(record) for record in model_run_rows]
    has_submitted_or_attempted_work = _scheduler_has_submitted_or_attempted_model_run(model_run_rows)
    has_attempted_or_terminal_work = _scheduler_has_attempted_or_terminal_model_run(
        model_run_rows,
        model_run_outcomes,
    )

    candidate_count = _count_value(payload, "candidate_count")
    if candidate_count is not None:
        identity_count = len(candidates) + len(blocked_candidates) + len(skipped_candidates)
        if candidate_count != identity_count:
            errors.append("candidate_count_identity_cardinality_mismatch")
        if candidate_count == 0 and model_run_rows:
            errors.append("candidate_count_identity_cardinality_mismatch")

    count_expectations = (
        (
            "submitted_count",
            sum(1 for outcome in model_run_outcomes if outcome.submitted),
            "submitted_count_model_run_evidence_mismatch",
        ),
        ("blocked_candidate_count", len(blocked_candidates), "blocked_candidate_count_identity_cardinality_mismatch"),
        ("skipped_candidate_count", len(skipped_candidates), "skipped_candidate_count_identity_cardinality_mismatch"),
    )
    for count_field, actual, error in count_expectations:
        value = _count_value(payload, count_field)
        if value is not None and value != actual:
            errors.append(error)

    submitted_count = _count_value(payload, "submitted_count")
    model_run_capacity = submitted_count if submitted_count is not None else len(model_run_rows)
    status = str(payload.get("status") or "").strip().lower()
    count_expectations_by_field = {
        "failed_count": _scheduler_failed_count_model_run_rows(model_run_outcomes),
        "partial_count": _scheduler_partial_count_model_run_rows(
            model_run_outcomes,
            pass_status=status,
            submitted_count=submitted_count,
            has_submitted_or_attempted_work=has_submitted_or_attempted_work,
        ),
    }
    for count_field, error_prefix in (
        ("failed_count", "failed_count"),
        ("partial_count", "partial_count"),
    ):
        value = _count_value(payload, count_field)
        if value is None:
            continue
        count_capacity = _scheduler_count_model_run_capacity(
            count_field,
            pass_status=status,
            model_run_capacity=model_run_capacity,
            model_run_row_count=len(model_run_rows),
            has_submitted_or_attempted_work=has_submitted_or_attempted_work,
            has_attempted_or_terminal_work=has_attempted_or_terminal_work,
        )
        if value > count_capacity or value > len(model_run_rows):
            errors.append(f"{error_prefix}_exceeds_model_run_evidence")
            continue
        matching_rows = count_expectations_by_field[count_field]
        if value != matching_rows:
            errors.append(f"{error_prefix}_status_cardinality_mismatch")
    errors.extend(
        _scheduler_live_status_count_errors(
            payload,
            model_run_rows=model_run_rows,
            model_run_outcomes=model_run_outcomes,
        )
    )
    return errors


def _scheduler_failed_count_model_run_rows(model_run_outcomes: Sequence[_SchedulerModelRunOutcome]) -> int:
    return sum(1 for outcome in model_run_outcomes if outcome.failed)


def _scheduler_has_submitted_or_attempted_model_run(model_run_rows: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        record.get("submitted") is True or record.get("execution_attempted") is True for record in model_run_rows
    )


def _scheduler_has_attempted_or_terminal_model_run(
    model_run_rows: Sequence[Mapping[str, Any]],
    model_run_outcomes: Sequence[_SchedulerModelRunOutcome],
) -> bool:
    return any(
        record.get("execution_attempted") is True or outcome.failed or outcome.blocked
        for record, outcome in zip(model_run_rows, model_run_outcomes, strict=True)
    )


def _scheduler_count_model_run_capacity(
    count_field: str,
    *,
    pass_status: str,
    model_run_capacity: int,
    model_run_row_count: int,
    has_submitted_or_attempted_work: bool,
    has_attempted_or_terminal_work: bool,
) -> int:
    if (
        count_field in {"failed_count", "partial_count"}
        and _scheduler_pass_uses_model_run_count_capacity(pass_status)
        and has_attempted_or_terminal_work
    ):
        return model_run_row_count
    if count_field == "partial_count" and pass_status == "submitted_partial" and has_submitted_or_attempted_work:
        return model_run_row_count
    return model_run_capacity


def _scheduler_pass_uses_model_run_count_capacity(pass_status: str) -> bool:
    return pass_status in SCHEDULER_REVIEW_BLOCKED_STATUSES or pass_status.endswith(("_blocked", "_failed"))


def _scheduler_partial_count_model_run_rows(
    model_run_outcomes: Sequence[_SchedulerModelRunOutcome],
    *,
    pass_status: str,
    submitted_count: int | None,
    has_submitted_or_attempted_work: bool,
) -> int:
    if _scheduler_pass_uses_producer_partial_count(pass_status, submitted_count=submitted_count):
        if not has_submitted_or_attempted_work:
            return 0
        return sum(1 for outcome in model_run_outcomes if outcome.producer_partial)
    return sum(
        1
        for outcome in model_run_outcomes
        if outcome.partial
    )


def _scheduler_pass_uses_producer_partial_count(pass_status: str, *, submitted_count: int | None) -> bool:
    if pass_status == "submitted_partial":
        return True
    if submitted_count != 0:
        return False
    return pass_status in SCHEDULER_REVIEW_BLOCKED_STATUSES or pass_status.endswith(("_blocked", "_failed"))


def _scheduler_live_status_count_errors(
    payload: Mapping[str, Any],
    *,
    model_run_rows: Sequence[Mapping[str, Any]],
    model_run_outcomes: Sequence[_SchedulerModelRunOutcome],
) -> list[str]:
    execution_mode = _scheduler_evidence_mode(payload)
    status = str(payload.get("status") or "").strip().lower()
    if execution_mode not in SCHEDULER_LIVE_PRODUCER_EXECUTION_MODES or status not in SCHEDULER_LIVE_WORK_STATUSES:
        return []

    errors: list[str] = []
    submitted_count = _count_value(payload, "submitted_count")
    failed_count = _count_value(payload, "failed_count")
    partial_count = _count_value(payload, "partial_count")
    submitted_rows = sum(1 for outcome in model_run_outcomes if outcome.submitted)
    failed_rows = sum(1 for outcome in model_run_outcomes if outcome.failed)
    partial_rows = sum(1 for outcome in model_run_outcomes if outcome.partial)
    blocked_rows = sum(1 for outcome in model_run_outcomes if outcome.blocked)
    allowed_statuses = SCHEDULER_LIVE_MODEL_RUN_STATUS_COMPATIBILITY.get(status, frozenset())
    incompatible_rows = sum(
        1
        for outcome in model_run_outcomes
        if outcome.submitted_explicitly_false
        or not outcome.has_status_evidence
        or not outcome.status_values & allowed_statuses
        or outcome.failed
        or outcome.partial
        or outcome.blocked
    )

    if submitted_count is None or submitted_count <= 0 or not model_run_rows:
        errors.append("submitted_status_without_model_run_evidence")
    elif submitted_count != submitted_rows:
        errors.append("submitted_count_model_run_evidence_mismatch")

    if failed_count != 0:
        errors.append("live_status_failed_count_nonzero")
    if partial_count != 0:
        errors.append("live_status_partial_count_nonzero")
    if failed_rows or partial_rows or blocked_rows:
        errors.append("live_status_model_run_blocked_outcome")
    if incompatible_rows:
        errors.append("submitted_status_model_run_status_mismatch")
    return errors


def _scheduler_model_run_outcome(record: Mapping[str, Any]) -> _SchedulerModelRunOutcome:
    status_values = frozenset(_scheduler_model_run_status_values(record))
    submitted_explicitly_false = record.get("submitted") is False
    submitted = record.get("submitted") is True or bool(status_values & SCHEDULER_LIVE_COMPATIBLE_MODEL_RUN_STATUSES)
    if submitted_explicitly_false:
        submitted = False
    failed = any(_scheduler_model_run_failed_status(status) for status in status_values)
    partial = any(_scheduler_model_run_partial_status(status) for status in status_values)
    blocked = any(_scheduler_model_run_blocked_status(status) for status in status_values)
    producer_partial = any(_scheduler_model_run_producer_partial_status(status) for status in status_values)
    return _SchedulerModelRunOutcome(
        status_values=status_values,
        has_status_evidence=bool(status_values),
        submitted=submitted,
        submitted_explicitly_false=submitted_explicitly_false,
        failed=failed,
        partial=partial,
        blocked=blocked,
        producer_partial=producer_partial,
    )


def _scheduler_model_run_status_values(record: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("status", "outcome", "result", "state", "candidate_outcome"):
        value = record.get(key)
        values.update(_nested_scheduler_status_values(value, is_status_value=True))
    for key in ("stage_statuses", "stage_evidence", "task_results"):
        values.update(_nested_scheduler_status_values(record.get(key)))
    task_results_summary = record.get("task_results_summary")
    if isinstance(task_results_summary, Mapping):
        values.update(_scheduler_status_count_values(task_results_summary.get("status_counts")))
    return values


def _scheduler_status_count_values(value: Any) -> set[str]:
    if not isinstance(value, Mapping):
        return set()
    return {
        str(status).strip().lower()
        for status, count in value.items()
        if str(status).strip() and _positive_scheduler_status_count(count)
    }


def _positive_scheduler_status_count(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int | float):
        return value > 0
    if isinstance(value, str):
        try:
            return float(value.strip()) > 0
        except ValueError:
            return False
    return False


def _nested_scheduler_status_values(value: Any, *, is_status_value: bool = False) -> set[str]:
    values: set[str] = set()
    stack: list[tuple[Any, bool]] = [(value, is_status_value)]
    seen_containers: set[int] = set()
    while stack:
        current, current_is_status_value = stack.pop()
        if isinstance(current, str):
            if current_is_status_value and current.strip():
                values.add(current.strip().lower())
            continue
        if isinstance(current, Mapping):
            current_id = id(current)
            if current_id in seen_containers:
                continue
            seen_containers.add(current_id)
            for key, nested in current.items():
                if str(key) == "status_counts":
                    values.update(_scheduler_status_count_values(nested))
                else:
                    stack.append((nested, str(key) in SCHEDULER_MODEL_RUN_STATUS_KEYS))
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            current_id = id(current)
            if current_id in seen_containers:
                continue
            seen_containers.add(current_id)
            stack.extend((item, current_is_status_value) for item in current)
    return values


def _scheduler_model_run_failed_status(status: str) -> bool:
    return status in SCHEDULER_FAILED_MODEL_RUN_STATUSES or status.endswith("_failed")


def _scheduler_model_run_partial_status(status: str) -> bool:
    return status in SCHEDULER_PARTIAL_MODEL_RUN_STATUSES or status.endswith("_partial")


def _scheduler_model_run_blocked_status(status: str) -> bool:
    return status in SCHEDULER_BLOCKED_MODEL_RUN_STATUSES or status.endswith(
        ("_blocked", "_cancelled", "_unavailable")
    )


def _scheduler_model_run_producer_partial_status(status: str) -> bool:
    return (
        status in SCHEDULER_PARTIAL_MODEL_RUN_STATUSES
        or status in SCHEDULER_FAILED_MODEL_RUN_STATUSES
        or status in SCHEDULER_BLOCKED_MODEL_RUN_STATUSES
        or status.endswith(("_blocked", "_cancelled", "_failed", "_unavailable"))
    )


def _dedupe_errors(errors: Sequence[str]) -> list[str]:
    unique: list[str] = []
    for error in errors:
        if error not in unique:
            unique.append(error)
    return unique


def _cycle_identity_aliases(value: str) -> set[str]:
    if not value:
        return set()
    aliases = {value}
    aliases.add(_compact_cycle_id_suffix(value))
    parsed = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        aliases.add(datetime.fromisoformat(parsed).astimezone(UTC).strftime("%Y%m%d%H"))
    except ValueError:
        pass
    return aliases


def _compact_cycle_id_suffix(cycle_id: str) -> str:
    parts = cycle_id.rsplit("_", 1)
    return parts[-1] if len(parts) == 2 and parts[-1].isdigit() else cycle_id


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _identity_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _identity_value_looks_unsafe(value: str) -> bool:
    stripped = value.strip()
    return (
        not stripped
        or "\x00" in stripped
        or any(separator in stripped for separator in ("../", "..\\"))
        or bool(PATH_TOKEN_RE.search(stripped))
    )


def _scheduler_evidence_artifact_ref(path: Path, *, config: ProductionReadinessConfig) -> str:
    if config.scheduler_evidence_root is not None:
        try:
            relative = path.resolve(strict=False).relative_to(
                config.scheduler_evidence_root.expanduser().resolve(strict=False)
            )
        except ValueError:
            relative = Path(path.name)
    else:
        relative = Path(path.name)
    return f"scheduler:{relative.as_posix()}"


def _scheduler_item_suffix(payload: Mapping[str, Any], path: Path) -> str:
    pass_id = payload.get("pass_id")
    if isinstance(pass_id, str) and SAFE_RUN_ID_RE.fullmatch(pass_id):
        return pass_id
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    return digest


def _redacted_preview(raw: bytes, *, config: ProductionReadinessConfig) -> str:
    preview = raw[:MAX_RECEIPT_PREVIEW_BYTES].decode("utf-8", errors="replace")
    if len(raw) > MAX_RECEIPT_PREVIEW_BYTES:
        preview += "[truncated]"
    return str(_bounded_redacted_payload(preview, config=config))


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
