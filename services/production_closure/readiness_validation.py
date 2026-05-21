from __future__ import annotations

import argparse
import json
import os
import platform
import re
import stat
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from packages.common.redaction import redact_payload, redact_text
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_limited_no_follow,
)

SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
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

MAX_EVIDENCE_PAYLOAD_BYTES = 768 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
MAX_RECEIPT_PREVIEW_BYTES = 2048
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 1200
MAX_STRING_LENGTH = 2048
LIVE_PROOF_SCHEMA = "nhms.production_readiness.live_proof.v1"
EXPECTED_TARGET_ENVIRONMENT = "production"
PATH_TOKEN_RE = re.compile(
    r"\bfile://(?:localhost)?/[^\s\"'<>),;]+"
    r"|\\\\[^\s\"'<>),;]+\\[^\s\"'<>),;]+"
    r"|\b[A-Za-z]:\\[^\s\"'<>),;]+"
    r"|(?<![:/\w])/(?:[^\s\"'<>),;]+)"
)

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
DEPENDENCY_ROOT_ENV = {
    "slurm": "NHMS_PRODUCTION_READINESS_SLURM_EVIDENCE_ROOT",
    "object_store": "NHMS_PRODUCTION_READINESS_OBJECT_STORE_EVIDENCE_ROOT",
    "source": "NHMS_PRODUCTION_READINESS_SOURCE_EVIDENCE_ROOT",
    "e2e": "NHMS_PRODUCTION_READINESS_E2E_EVIDENCE_ROOT",
    "mvt": "NHMS_PRODUCTION_READINESS_MVT_EVIDENCE_ROOT",
}
PROOF_ENV = {
    "auth": "NHMS_PRODUCTION_READINESS_AUTH_PROOF",
    "alert": "NHMS_PRODUCTION_READINESS_ALERT_PROOF",
    "rollback": "NHMS_PRODUCTION_READINESS_ROLLBACK_PROOF",
    "slurm": "NHMS_PRODUCTION_READINESS_SLURM_PROOF",
    "object_store": "NHMS_PRODUCTION_READINESS_OBJECT_STORE_PROOF",
    "source": "NHMS_PRODUCTION_READINESS_SOURCE_PROOF",
    "e2e": "NHMS_PRODUCTION_READINESS_E2E_PROOF",
    "mvt": "NHMS_PRODUCTION_READINESS_MVT_PROOF",
    "target_env": "NHMS_PRODUCTION_READINESS_TARGET_ENV_PROOF",
}
PROOF_FILE_ENV = {
    "auth": "NHMS_PRODUCTION_READINESS_AUTH_PROOF_FILE",
    "alert": "NHMS_PRODUCTION_READINESS_ALERT_PROOF_FILE",
    "rollback": "NHMS_PRODUCTION_READINESS_ROLLBACK_PROOF_FILE",
    "slurm": "NHMS_PRODUCTION_READINESS_SLURM_PROOF_FILE",
    "object_store": "NHMS_PRODUCTION_READINESS_OBJECT_STORE_PROOF_FILE",
    "source": "NHMS_PRODUCTION_READINESS_SOURCE_PROOF_FILE",
    "e2e": "NHMS_PRODUCTION_READINESS_E2E_PROOF_FILE",
    "mvt": "NHMS_PRODUCTION_READINESS_MVT_PROOF_FILE",
    "target_env": "NHMS_PRODUCTION_READINESS_TARGET_ENV_PROOF_FILE",
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


class ProductionReadinessValidationError(RuntimeError):
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
        _refuse_symlink_components_to_deepest_existing(self.evidence_root)
        _refuse_symlink_components_to_deepest_existing(self.lane_dir.parent)
        if self.lane_dir.exists() or self.lane_dir.is_symlink():
            _refuse_symlink_components(self.lane_dir)
            if not self.lane_dir.is_dir():
                raise ProductionReadinessValidationError(
                    "PRODUCTION_READINESS_EVIDENCE_PATH_UNSAFE",
                    f"Evidence lane path must be a directory: {self.lane_dir}.",
                )
            if any(self.lane_dir.iterdir()) and not self.force:
                raise ProductionReadinessValidationError(
                    "PRODUCTION_READINESS_EVIDENCE_EXISTS",
                    f"Evidence bundle already exists: {self.lane_dir}. Use --force to overwrite an existing run_id.",
                )
        resolved_lane = self.lane_dir.resolve(strict=False)
        try:
            resolved_lane.relative_to(self.evidence_root)
        except ValueError as error:
            raise ProductionReadinessValidationError(
                "PRODUCTION_READINESS_EVIDENCE_PATH_UNSAFE",
                "Evidence lane directory must stay under evidence root.",
            ) from error
        try:
            ensure_directory_no_follow(self.evidence_root)
            ensure_directory_no_follow(self.lane_dir, containment_root=self.evidence_root)
        except SafeFilesystemError as error:
            error_code = (
                "PRODUCTION_READINESS_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "PRODUCTION_READINESS_EVIDENCE_PATH_UNSAFE"
            )
            raise ProductionReadinessValidationError(
                error_code,
                f"Failed to prepare evidence lane {self.lane_dir}: {error}",
            ) from error

    def write_json(self, path: Path, payload: Any) -> None:
        content = json.dumps(redact_payload(payload), indent=2, sort_keys=True).encode("utf-8") + b"\n"
        if len(content) > self.max_payload_bytes:
            raise ProductionReadinessValidationError(
                "PRODUCTION_READINESS_EVIDENCE_PAYLOAD_TOO_LARGE",
                f"Evidence payload exceeds configured limit of {self.max_payload_bytes} bytes.",
            )
        self._write_bytes(path, content)

    def _write_bytes(self, path: Path, content: bytes) -> None:
        safe_path = self._safe_file_path(path)
        if safe_path.exists() and safe_path not in self._created_paths and not self.force:
            raise ProductionReadinessValidationError(
                "PRODUCTION_READINESS_EVIDENCE_EXISTS",
                f"Evidence file already exists: {safe_path}. Use --force to overwrite an existing run_id bundle.",
            )
        try:
            atomic_write_bytes_no_follow(safe_path, content, containment_root=self.lane_dir)
            self._created_paths.add(safe_path)
        except SafeFilesystemError as error:
            error_code = (
                "PRODUCTION_READINESS_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "PRODUCTION_READINESS_EVIDENCE_PATH_UNSAFE"
            )
            raise ProductionReadinessValidationError(
                error_code,
                f"Failed to write evidence file {safe_path}: {error}",
            ) from error

    def _safe_file_path(self, path: Path) -> Path:
        if path.is_symlink():
            raise ProductionReadinessValidationError(
                "PRODUCTION_READINESS_EVIDENCE_SYMLINK",
                f"Evidence file must not be a symlink: {path}",
            )
        _refuse_symlink_components(path.parent)
        resolved_parent = path.parent.resolve(strict=False)
        resolved_lane = self.lane_dir.resolve(strict=False)
        try:
            resolved_parent.relative_to(resolved_lane)
        except ValueError as error:
            raise ProductionReadinessValidationError(
                "PRODUCTION_READINESS_EVIDENCE_PATH_UNSAFE",
                "Evidence file path must stay under the current readiness lane directory.",
            ) from error
        try:
            ensure_directory_no_follow(path.parent, containment_root=self.lane_dir)
        except SafeFilesystemError as error:
            error_code = (
                "PRODUCTION_READINESS_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "PRODUCTION_READINESS_EVIDENCE_PATH_UNSAFE"
            )
            raise ProductionReadinessValidationError(
                error_code,
                f"Failed to prepare evidence file parent {path.parent}: {error}",
            ) from error
        return resolved_parent / path.name


@dataclass(frozen=True)
class ProductionReadinessConfig:
    evidence_root: Path
    run_id: str
    dependency_roots: Mapping[str, Path | None]
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
        auth_proof: str | None = None,
        auth_proof_file: Path | None = None,
        alert_proof: str | None = None,
        alert_proof_file: Path | None = None,
        rollback_proof: str | None = None,
        rollback_proof_file: Path | None = None,
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
    items.extend(_dependency_summary_items(config))
    items.extend(_live_proof_items(config, receipts))
    items.extend(_exclusion_items(config))
    validation = _validate_items(items)
    items.extend(validation)
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
        "surface",
        "required_for_final",
        "live_proof_accepted",
        "artifact_refs",
        "residual_risk",
        "removal_criteria",
        "exclusions",
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
            dependencies=["apps.api.auth.ACTION_MATRIX"],
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
            reason=f"Dependency summary could not be read: {redact_text(str(error))}.",
        )
    status = str(summary.get("status", "unknown"))
    schema_ok = summary.get("schema") == contract["schema"]
    issue_ok = summary.get("issue") == contract["issue"]
    accepted_status = status in contract["allowed_statuses"]
    item_status = "passed" if schema_ok and issue_ok and accepted_status else "blocked"
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
        ],
        details=_bounded_redacted_payload(
            {
                "summary_schema": summary.get("schema"),
                "summary_issue": summary.get("issue"),
                "summary_run_id": summary.get("run_id"),
                "summary_status": status,
                "summary_execution_mode": summary.get("execution_mode"),
                "summary_final_production_readiness_claimed": summary.get("final_production_readiness_claimed"),
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


def _live_proof_items(
    config: ProductionReadinessConfig,
    receipts: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        _auth_live_item(config, receipts["auth"]),
        _surface_live_item(
            config,
            receipts["alert"],
            proof_key="alert",
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
            item_id="live-slurm-dependency",
            surface="live_slurm_dependency_proof",
            missing_risk="Live Slurm workload/accounting proof has not been accepted.",
            removal="Provide an accepted Slurm dependency proof receipt from the target environment.",
        ),
        _surface_live_item(
            config,
            receipts["object_store"],
            proof_key="object_store",
            item_id="live-object-store-dependency",
            surface="live_object_store_dependency_proof",
            missing_risk="Live object-store/API proof has not been accepted.",
            removal="Provide an accepted object-store dependency proof receipt from the target environment.",
        ),
        _surface_live_item(
            config,
            receipts["source"],
            proof_key="source",
            item_id="live-source-dependency",
            surface="live_source_weather_dependency_proof",
            missing_risk="Live weather/source credential and ingest proof has not been accepted.",
            removal="Provide an accepted source/weather dependency proof receipt from the target environment.",
        ),
        _surface_live_item(
            config,
            receipts["e2e"],
            proof_key="e2e",
            item_id="live-e2e-dependency",
            surface="live_e2e_dependency_proof",
            missing_risk="Live E2E target-environment proof has not been accepted.",
            removal="Provide an accepted E2E dependency proof receipt from the target environment.",
        ),
        _surface_live_item(
            config,
            receipts["mvt"],
            proof_key="mvt",
            item_id="live-mvt-performance",
            surface="live_mvt_performance_proof",
            missing_risk="Live MVT/performance proof has not been accepted.",
            removal="Provide accepted live PostGIS/national-data/browser or equivalent MVT performance proof.",
        ),
        _surface_live_item(
            config,
            receipts["target_env"],
            proof_key="target_env",
            item_id="live-target-environment-config",
            surface="target_environment_config_proof",
            missing_risk="Real target-environment configuration receipt has not been accepted.",
            removal="Provide an accepted target-environment configuration receipt.",
        ),
    ]


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
    payload = receipt["payload"]
    allowed = _string_set(payload.get("allowed_actions") or payload.get("allowed_coverage"))
    denied = _string_set(payload.get("denied_actions") or payload.get("denied_coverage"))
    missing_allowed = sorted(REQUIRED_AUTH_ACTIONS - allowed)
    missing_denied = sorted(REQUIRED_AUTH_ACTIONS - denied)
    errors = _common_live_receipt_errors(payload, proof_key="auth", config=config)
    if not _non_empty_mapping(payload.get("provider")) and not _non_empty_mapping(payload.get("provider_metadata")):
        errors.append("missing_provider_metadata")
    if not _non_empty_mapping(payload.get("role_mapping")) and not _non_empty_mapping(payload.get("role_mappings")):
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
    payload = receipt["payload"]
    errors = _surface_live_receipt_errors(payload, proof_key=proof_key, config=config)
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


def _validate_items(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    validation_failures = []
    for index, item in enumerate(items):
        try:
            validate_readiness_item(item)
        except ProductionReadinessValidationError as error:
            validation_failures.append(
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
    return validation_failures


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
    return {
        "surface": surface,
        "status": "parsed",
        "source": source,
        "source_ref": source_ref,
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
        {key: value for key, value in receipt.items() if key != "payload"}
        | ({"payload": receipt.get("payload")} if "payload" in receipt else {}),
        config=config,
    )


def _preflight_payload(
    config: ProductionReadinessConfig,
    receipts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": "nhms.production_readiness.preflight.v1",
        "issue": 181,
        "run_id": config.run_id,
        "evidence_root": _path_for_evidence(config.evidence_root, config=config),
        "evidence_dir": _path_for_evidence(config.lane_dir, config=config),
        "dependency_roots": {
            name: _path_for_evidence(root, config=config) if root is not None else None
            for name, root in config.dependency_roots.items()
        },
        "live_proof_configured": {
            surface: receipt["status"] not in {"missing"} for surface, receipt in receipts.items()
        },
        "fast_ci_live_side_effect_policy": {
            "executes_live_idp": False,
            "executes_live_alert_sink": False,
            "executes_backend_mutation": False,
            "executes_live_rollback": False,
            "executes_live_slurm": False,
            "executes_live_object_store": False,
            "executes_live_weather_source": False,
            "executes_real_national_data": False,
        },
    }


def _environment_payload(config: ProductionReadinessConfig) -> dict[str, Any]:
    env_keys = [
        "NHMS_RUN_PRODUCTION_CLOSURE",
        *DEPENDENCY_ROOT_ENV.values(),
        *PROOF_FILE_ENV.values(),
        "AUTH_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URL",
    ]
    return {
        "schema": "nhms.production_readiness.environment.v1",
        "run_id": config.run_id,
        "captured_at": _now(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": _path_for_evidence(Path.cwd(), config=config),
        "env": {
            key: _redact_paths(os.getenv(key, ""), config=config)
            for key in env_keys
            if key in os.environ
        },
    }


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
) -> list[str]:
    errors = _common_live_receipt_errors(payload, proof_key=proof_key, config=config)
    if proof_key == "alert":
        if not (_non_empty_mapping(payload.get("sink_metadata")) or _non_empty_string(payload.get("sink"))):
            errors.append("missing_sink_metadata")
        if not (
            payload.get("delivered") is True
            or _non_empty_mapping(payload.get("delivery_metadata"))
            or _non_empty_mapping(payload.get("delivery_result"))
        ):
            errors.append("missing_delivery_metadata")
        if payload.get("delivered") is not True and str(payload.get("status", "")) != "delivered":
            errors.append("delivery_not_confirmed")
    elif proof_key == "rollback":
        if not _non_empty_mapping(payload.get("preconditions")):
            errors.append("missing_preconditions")
        if not (
            _non_empty_mapping(payload.get("command_metadata"))
            or _non_empty_mapping(payload.get("drill_metadata"))
            or _non_empty_string(payload.get("command"))
        ):
            errors.append("missing_command_or_drill_metadata")
        if payload.get("executed") is not True and str(payload.get("status", "")) != "executed":
            errors.append("rollback_not_executed")
    elif proof_key in DEPENDENCY_SUMMARY_CONTRACTS:
        expected_dependency = str(PROOF_CONTRACTS[proof_key]["dependency"])
        dependency = payload.get("dependency_surface", payload.get("dependency"))
        if dependency != expected_dependency:
            errors.append("dependency_surface_mismatch")
        if not _non_empty_mapping(payload.get("provenance")) and not _non_empty_string(payload.get("producer_run_id")):
            errors.append("missing_provenance")
    elif proof_key == "target_env":
        if not (
            _non_empty_mapping(payload.get("config_metadata"))
            or _non_empty_mapping(payload.get("environment_metadata"))
            or _non_empty_string(payload.get("config_receipt_id"))
        ):
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
    if not isinstance(status, str) or not status:
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
    if not _non_empty_string(target_environment) and not _non_empty_mapping(target_environment):
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
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            if any(str(item).strip() for item in value):
                return True
        if _non_empty_mapping(value):
            return True
    return False


def _non_empty_mapping(value: Any) -> bool:
    return isinstance(value, Mapping) and bool(value)


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


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
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


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


def _bounded_redacted_payload(value: Any, *, config: ProductionReadinessConfig) -> Any:
    nodes = 0

    def walk(current: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES:
            return "[truncated:max-nodes]"
        if depth > MAX_JSON_DEPTH:
            return "[truncated:max-depth]"
        if isinstance(current, Mapping):
            return {str(key): walk(nested, depth + 1) for key, nested in current.items()}
        if isinstance(current, list):
            return [walk(item, depth + 1) for item in current[:MAX_JSON_NODES]]
        if isinstance(current, tuple):
            return [walk(item, depth + 1) for item in current[:MAX_JSON_NODES]]
        if isinstance(current, str):
            redacted = _redact_paths(current[:MAX_STRING_LENGTH], config=config)
            if len(current) > MAX_STRING_LENGTH:
                return f"{redacted}[truncated]"
            return redacted
        return current

    return redact_payload(walk(value, 0))


def _redacted_preview(raw: bytes, *, config: ProductionReadinessConfig) -> str:
    preview = raw[:MAX_RECEIPT_PREVIEW_BYTES].decode("utf-8", errors="replace")
    if len(raw) > MAX_RECEIPT_PREVIEW_BYTES:
        preview += "[truncated]"
    return str(_bounded_redacted_payload(preview, config=config))


def _path_for_evidence(path: Path | None, *, config: ProductionReadinessConfig) -> str | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve(strict=False)
    bases = ((config.lane_dir, "readiness"), (config.evidence_root, "evidence-root"), (Path.cwd(), "workspace"))
    for base, prefix in bases:
        try:
            relative = resolved.relative_to(base.expanduser().resolve(strict=False))
        except ValueError:
            continue
        return prefix if str(relative) == "." else f"{prefix}/{relative.as_posix()}"
    if resolved.is_absolute():
        return "[redacted-path]"
    return redact_text(str(path))


def _redact_paths(value: str, *, config: ProductionReadinessConfig) -> str:
    if str(config.evidence_root) in value:
        value = value.replace(str(config.evidence_root), "evidence-root")
    cwd = str(Path.cwd())
    if cwd in value:
        value = value.replace(cwd, "workspace")
    value = PATH_TOKEN_RE.sub("[redacted-path]", value)
    return redact_text(value)


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


def _refuse_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part == path.anchor or part == "":
            continue
        current = current / part
        if current.is_symlink():
            raise ProductionReadinessValidationError(
                "PRODUCTION_READINESS_EVIDENCE_SYMLINK",
                f"Evidence path component must not be a symlink: {current}",
            )


def _refuse_symlink_components_to_deepest_existing(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part == path.anchor or part == "":
            continue
        current = current / part
        if current.is_symlink():
            raise ProductionReadinessValidationError(
                "PRODUCTION_READINESS_EVIDENCE_SYMLINK",
                f"Evidence path component must not be a symlink: {current}",
            )
        if not current.exists():
            break


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
            click.echo(f"{error.error_code}: {redact_text(error.message)}", err=True)
            raise SystemExit(1) from error
        except Exception as error:
            click.echo(f"PRODUCTION_READINESS_VALIDATION_FAILED: {redact_text(str(error))}", err=True)
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
        click.option("--auth-proof", default=None),
        click.option("--auth-proof-file", type=click.Path(path_type=Path), default=None),
        click.option("--alert-proof", default=None),
        click.option("--alert-proof-file", type=click.Path(path_type=Path), default=None),
        click.option("--rollback-proof", default=None),
        click.option("--rollback-proof-file", type=click.Path(path_type=Path), default=None),
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
        print(f"{error.error_code}: {redact_text(error.message)}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"PRODUCTION_READINESS_VALIDATION_FAILED: {redact_text(str(error))}", file=sys.stderr)
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
    parser.add_argument("--auth-proof", default=None)
    parser.add_argument("--auth-proof-file", type=Path, default=None)
    parser.add_argument("--alert-proof", default=None)
    parser.add_argument("--alert-proof-file", type=Path, default=None)
    parser.add_argument("--rollback-proof", default=None)
    parser.add_argument("--rollback-proof-file", type=Path, default=None)
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
