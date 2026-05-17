from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

from packages.common.redaction import redact_payload, redact_text

SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:_-]{0,127}$")
SENSITIVE_PREFIX_ASSIGNMENT_RE = re.compile(
    r"(?:^|[;?#&/])[^=/?#;&]*(?:token|password|passwd|pwd|secret|credential|api[_-]?key|access[_-]?key|"
    r"session[_-]?key|signature|x-amz-signature)[^=/?#;&]*=",
    re.IGNORECASE,
)
SENSITIVE_PREFIX_SEPARATOR_RE = re.compile(r"[/;?#&]")
DEFAULT_SOURCE_CYCLE = "2026-05-07T00:00:00Z"
DEFAULT_MODEL_SET = ("basins_qhh_shud_fixture",)
DEFAULT_OBJECT_PREFIX = "s3://nhms-production-like/e2e"
DEFAULT_DB_TARGET = "local-deterministic-fixture"
DEFAULT_SLURM_PARTITION = "deterministic-fixture"
DEFAULT_SLURM_ACCOUNT = "deterministic-fixture"
DEFAULT_FRONTEND_API_BASE = "deterministic-evidence-fixture"
VALID_QC_FIXTURES = {
    "valid",
    "missing_rivqdown",
    "malformed_columns",
    "non_finite",
    "missing_required_output",
    "count_mismatch",
    "time_axis_mismatch",
}
STAGE_NAMES = ("download", "canonical", "forcing", "slurm", "parse", "frequency", "tile", "api", "frontend")
DOWNSTREAM_STAGE_NAMES = {"parse", "frequency", "tile", "api", "frontend"}
DERIVED_SEGMENT_IDS = ("seg_a", "seg_b")
EXPECTED_TIMESTEP_HOURS = (0, 3)
MAX_EVIDENCE_PAYLOAD_BYTES = 768 * 1024
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
    "met": {
        "issue": 149,
        "schema": "nhms.production_closure.met.v1",
        "allowed_statuses": {"ready"},
    },
}
EXPLICIT_BLOCKED_DEPENDENCY_STATUSES = {
    "blocked",
    "failed",
    "failure",
    "error",
    "not_executed",
    "missing",
    "unknown",
}


class ProductionE2EValidationError(RuntimeError):
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
            if any(self.lane_dir.iterdir()) and not self.force:
                raise ProductionE2EValidationError(
                    "PRODUCTION_E2E_EVIDENCE_EXISTS",
                    f"Evidence bundle already exists: {self.lane_dir}. Use --force to overwrite an existing run_id.",
                )
        resolved_lane = self.lane_dir.resolve(strict=False)
        try:
            resolved_lane.relative_to(self.evidence_root)
        except ValueError as error:
            raise ProductionE2EValidationError(
                "PRODUCTION_E2E_EVIDENCE_PATH_UNSAFE",
                "Evidence lane directory must stay under evidence root.",
            ) from error
        self.lane_dir.mkdir(parents=True, exist_ok=True)

    def write_json(self, path: Path, payload: Any) -> None:
        content = json.dumps(redact_payload(payload), indent=2, sort_keys=True).encode("utf-8") + b"\n"
        if len(content) > self.max_payload_bytes:
            raise ProductionE2EValidationError(
                "PRODUCTION_E2E_EVIDENCE_PAYLOAD_TOO_LARGE",
                f"Evidence payload exceeds configured limit of {self.max_payload_bytes} bytes.",
            )
        self._write_bytes(path, content)

    def write_text(self, path: Path, value: str) -> None:
        content = redact_text(value).encode("utf-8")
        self._write_bytes(path, content)

    def _write_bytes(self, path: Path, content: bytes) -> None:
        safe_path = self._safe_file_path(path)
        if safe_path.exists() and safe_path not in self._created_paths and not self.force:
            raise ProductionE2EValidationError(
                "PRODUCTION_E2E_EVIDENCE_EXISTS",
                f"Evidence file already exists: {safe_path}. Use --force to overwrite an existing run_id bundle.",
            )
        temp_path = safe_path.with_name(f".{safe_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_bytes(content)
            os.replace(temp_path, safe_path)
            self._created_paths.add(safe_path)
        except OSError as error:
            temp_path.unlink(missing_ok=True)
            raise ProductionE2EValidationError(
                "PRODUCTION_E2E_EVIDENCE_WRITE_FAILED",
                f"Failed to write evidence file {safe_path}: {error}",
            ) from error

    def _safe_file_path(self, path: Path) -> Path:
        if path.is_symlink():
            raise ProductionE2EValidationError(
                "PRODUCTION_E2E_EVIDENCE_SYMLINK",
                f"Evidence file must not be a symlink: {path}",
            )
        _refuse_symlink_components(path.parent)
        resolved_parent = path.parent.resolve(strict=False)
        try:
            resolved_parent.relative_to(self.evidence_root)
        except ValueError as error:
            raise ProductionE2EValidationError(
                "PRODUCTION_E2E_EVIDENCE_PATH_UNSAFE",
                "Evidence file path must stay under evidence root.",
            ) from error
        path.parent.mkdir(parents=True, exist_ok=True)
        return resolved_parent / path.name


@dataclass(frozen=True)
class ProductionE2EConfig:
    evidence_root: Path
    run_id: str
    source_cycle: datetime
    model_set: tuple[str, ...]
    db_target: str
    object_prefix: str
    configured_object_prefix: str
    slurm_partition: str
    slurm_account: str
    frontend_api_base: str
    dependency_roots: Mapping[str, Path | None]
    shud_qc_fixture: str = "valid"
    force: bool = False

    @property
    def lane_dir(self) -> Path:
        return self.evidence_root / self.run_id / "e2e"

    @classmethod
    def from_env(
        cls,
        *,
        evidence_root: Path,
        run_id: str | None,
        source_cycle: str | None = None,
        model_set: str | None = None,
        db_target: str | None = None,
        object_prefix: str | None = None,
        slurm_partition: str | None = None,
        slurm_account: str | None = None,
        frontend_api_base: str | None = None,
        slurm_evidence_root: Path | None = None,
        object_store_evidence_root: Path | None = None,
        met_evidence_root: Path | None = None,
        shud_qc_fixture: str | None = None,
        force: bool = False,
    ) -> ProductionE2EConfig:
        resolved_evidence_root = _safe_resolved_evidence_root(evidence_root)
        resolved_run_id = _safe_run_id(run_id or datetime.now(UTC).strftime("m10-%Y%m%dT%H%M%SZ"))
        configured_prefix = (
            object_prefix
            or os.getenv("NHMS_PRODUCTION_E2E_OBJECT_PREFIX")
            or os.getenv("NHMS_PRODUCTION_OBJECT_STORE_PREFIX")
            or DEFAULT_OBJECT_PREFIX
        )
        _validate_object_prefix_safe(configured_prefix)
        return cls(
            evidence_root=resolved_evidence_root,
            run_id=resolved_run_id,
            source_cycle=_parse_cycle_time(
                source_cycle or os.getenv("NHMS_PRODUCTION_E2E_SOURCE_CYCLE", DEFAULT_SOURCE_CYCLE)
            ),
            model_set=_parse_model_set(model_set or os.getenv("NHMS_PRODUCTION_E2E_MODEL_SET")),
            db_target=db_target or os.getenv("NHMS_PRODUCTION_E2E_DB_TARGET", DEFAULT_DB_TARGET),
            object_prefix=_run_scoped_prefix(configured_prefix, resolved_run_id),
            configured_object_prefix=configured_prefix,
            slurm_partition=slurm_partition
            or os.getenv("NHMS_PRODUCTION_E2E_SLURM_PARTITION")
            or os.getenv("NHMS_PRODUCTION_SLURM_PARTITION", DEFAULT_SLURM_PARTITION),
            slurm_account=slurm_account
            or os.getenv("NHMS_PRODUCTION_E2E_SLURM_ACCOUNT")
            or os.getenv("NHMS_PRODUCTION_SLURM_ACCOUNT", DEFAULT_SLURM_ACCOUNT),
            frontend_api_base=frontend_api_base
            or os.getenv("NHMS_PRODUCTION_E2E_FRONTEND_API_BASE", DEFAULT_FRONTEND_API_BASE),
            dependency_roots={
                "slurm": _dependency_root("NHMS_PRODUCTION_E2E_SLURM_EVIDENCE_ROOT", slurm_evidence_root),
                "object_store": _dependency_root(
                    "NHMS_PRODUCTION_E2E_OBJECT_STORE_EVIDENCE_ROOT",
                    object_store_evidence_root,
                ),
                "met": _dependency_root("NHMS_PRODUCTION_E2E_MET_EVIDENCE_ROOT", met_evidence_root),
            },
            shud_qc_fixture=shud_qc_fixture
            or os.getenv("NHMS_PRODUCTION_E2E_SHUD_QC_FIXTURE", "valid"),
            force=force,
        )


def validate_e2e(config: ProductionE2EConfig) -> dict[str, Any]:
    config = replace(config, evidence_root=_safe_resolved_evidence_root(config.evidence_root))
    _validate_config(config)
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=config.force)
    writer.prepare()

    preflight = _preflight_payload(config)
    writer.write_json(config.lane_dir / "preflight.json", preflight)

    dependency_status = _dependency_status_payload(config)
    writer.write_json(config.lane_dir / "dependency_status.json", dependency_status)

    derived_ids = _derived_identifiers(config)
    qc = _write_shud_output_qc(config, writer, derived_ids)
    writer.write_json(config.lane_dir / "shud_output_qc.json", qc)

    stage_artifacts = _write_stage_artifacts(config, writer, derived_ids, dependency_status, qc)
    stage_manifest = _stage_manifest(config, derived_ids, dependency_status, qc, stage_artifacts)
    writer.write_json(config.lane_dir / "stage_manifest.json", stage_manifest)

    api_evidence = _api_contract_evidence(config, derived_ids, dependency_status, stage_manifest, qc)
    writer.write_json(config.lane_dir / "api_contract_evidence.json", api_evidence)

    frontend_evidence = _frontend_smoke_evidence(config, derived_ids, dependency_status, stage_manifest, qc)
    writer.write_json(config.lane_dir / "frontend_smoke_evidence.json", frontend_evidence)

    environment = _environment_payload(config)
    writer.write_json(config.lane_dir / "environment.json", environment)

    blockers = _summary_blockers(dependency_status, qc)
    status = "ready" if not blockers else "blocked"
    summary = _summary(
        config,
        status=status,
        blockers=blockers,
        derived_ids=derived_ids,
        stage_manifest=stage_manifest,
        qc=qc,
        api_evidence=api_evidence,
        frontend_evidence=frontend_evidence,
    )
    writer.write_json(config.lane_dir / "summary.json", summary)
    return summary


def _preflight_payload(config: ProductionE2EConfig) -> dict[str, Any]:
    return {
        "schema": "nhms.production_closure.e2e.preflight.v1",
        "issue": 150,
        "run_id": config.run_id,
        "source_cycle": _format_time(config.source_cycle),
        "model_set": list(config.model_set),
        "db_target": config.db_target,
        "object_prefix": config.object_prefix,
        "configured_object_prefix": config.configured_object_prefix,
        "slurm": {
            "partition": config.slurm_partition,
            "account": config.slurm_account,
        },
        "frontend_api_base": config.frontend_api_base,
        "dependency_evidence_roots": {
            name: str(path) if path else None for name, path in config.dependency_roots.items()
        },
        "evidence_root": str(config.evidence_root),
        "evidence_dir": str(config.lane_dir),
        "shud_qc_fixture": config.shud_qc_fixture,
        "execution_policy": {
            "default_fast_path": "deterministic_self_contained",
            "external_network_required": False,
            "real_object_store_required": False,
            "copied_volume_data_required": False,
            "postgis_required": False,
            "real_slurm_required": False,
            "live_shud_solver_required": False,
            "running_frontend_required": False,
        },
    }


def _dependency_status_payload(config: ProductionE2EConfig) -> dict[str, Any]:
    dependencies = []
    blockers: list[dict[str, Any]] = []
    for name in ("met", "object_store", "slurm"):
        root = config.dependency_roots.get(name)
        dependency = _read_dependency(name, root)
        dependencies.append(dependency)
        if dependency["status"] == "blocked":
            blockers.append(
                {
                    "error_code": "PRODUCTION_E2E_DEPENDENCY_BLOCKED",
                    "dependency": name,
                    "message": dependency.get("reason", f"{name} dependency evidence is blocked."),
                }
            )
        elif dependency["status"] == "missing":
            blockers.append(
                {
                    "error_code": "PRODUCTION_E2E_DEPENDENCY_EVIDENCE_MISSING",
                    "dependency": name,
                    "message": dependency.get(
                        "reason",
                        f"{name} dependency evidence root does not contain a valid summary.json.",
                    ),
                }
            )
    return {
        "schema": "nhms.production_closure.e2e.dependency_status.v1",
        "run_id": config.run_id,
        "dependencies": dependencies,
        "blockers": blockers,
        "deterministic_equivalents_used": [item["dependency"] for item in dependencies if item["status"] == "skipped"],
    }


def _read_dependency(name: str, root: Path | None) -> dict[str, Any]:
    contract = DEPENDENCY_SUMMARY_CONTRACTS[name]
    if root is None:
        return {
            "dependency": name,
            "status": "skipped",
            "execution_mode": "deterministic_equivalent",
            "live_success_claimed": False,
            "summary_path": None,
            "expected_issue": contract["issue"],
            "expected_schema": contract["schema"],
            "reason": "No accepted dependency evidence root was supplied; using bounded deterministic equivalent.",
        }
    summary_path = _dependency_summary_path(root, name)
    if summary_path is None:
        return {
            "dependency": name,
            "status": "missing",
            "execution_mode": "not_executed",
            "live_success_claimed": False,
            "summary_path": str(root),
            "expected_issue": contract["issue"],
            "expected_schema": contract["schema"],
            "reason": "Dependency evidence root was supplied but no summary.json was found.",
        }
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {
            "dependency": name,
            "status": "blocked",
            "execution_mode": "not_executed",
            "live_success_claimed": False,
            "summary_path": str(summary_path),
            "expected_issue": contract["issue"],
            "expected_schema": contract["schema"],
            "reason": f"Dependency summary could not be read: {error}",
        }
    if not isinstance(summary, Mapping):
        return _invalid_dependency_summary(name, summary_path, "Dependency summary JSON must be an object.")

    summary_status = str(summary.get("status", "unknown"))
    summary_schema = summary.get("schema")
    summary_issue = summary.get("issue")
    schema_matches = summary_schema == contract["schema"]
    issue_matches = summary_issue == contract["issue"]
    if not schema_matches or not issue_matches:
        return _invalid_dependency_summary(
            name,
            summary_path,
            (
                f"Dependency summary must be issue #{contract['issue']} with schema {contract['schema']}; "
                f"got issue={summary_issue!r}, schema={summary_schema!r}."
            ),
            summary=summary,
        )
    if summary_status in contract["allowed_statuses"]:
        return {
            "dependency": name,
            "status": "consumed",
            "execution_mode": "consumed_evidence",
            "live_success_claimed": False,
            "summary_path": str(summary_path),
            "summary_status": summary_status,
            "schema": summary_schema,
            "issue": summary_issue,
            "run_id": summary.get("run_id"),
            "evidence_dir": summary.get("evidence_dir"),
            "reason": "Accepted production closure dependency summary consumed.",
        }
    dependency_status = "blocked" if summary_status in EXPLICIT_BLOCKED_DEPENDENCY_STATUSES else "blocked"
    return {
        "dependency": name,
        "status": dependency_status,
        "execution_mode": "not_executed",
        "live_success_claimed": False,
        "summary_path": str(summary_path),
        "summary_status": summary_status,
        "schema": summary_schema,
        "issue": summary_issue,
        "run_id": summary.get("run_id"),
        "evidence_dir": summary.get("evidence_dir"),
        "reason": (
            f"Dependency summary status {summary_status!r} is not an allowed success status "
            f"for #{contract['issue']} {name} evidence."
        ),
    }


def _invalid_dependency_summary(
    name: str,
    summary_path: Path,
    reason: str,
    *,
    summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    contract = DEPENDENCY_SUMMARY_CONTRACTS[name]
    return {
        "dependency": name,
        "status": "blocked",
        "execution_mode": "not_executed",
        "live_success_claimed": False,
        "summary_path": str(summary_path),
        "summary_status": summary.get("status", "unknown") if summary is not None else "unknown",
        "schema": summary.get("schema") if summary is not None else None,
        "issue": summary.get("issue") if summary is not None else None,
        "expected_schema": contract["schema"],
        "expected_issue": contract["issue"],
        "reason": reason,
    }


def _dependency_summary_path(root: Path, name: str) -> Path | None:
    candidates = [root / "summary.json", root / name / "summary.json"]
    if name == "object_store":
        candidates.append(root / "object-store" / "summary.json")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _derived_identifiers(config: ProductionE2EConfig) -> dict[str, Any]:
    model_id = config.model_set[0]
    source_id = "GFS"
    return {
        "model_id": model_id,
        "model_set": list(config.model_set),
        "basin_version_id": f"{model_id}_basin_v1",
        "river_network_version_id": f"{model_id}_rivnet_v1",
        "segment_id": DERIVED_SEGMENT_IDS[0],
        "segment_ids": list(DERIVED_SEGMENT_IDS),
        "source": source_id,
        "cycle_time": _format_time(config.source_cycle),
        "run_id": config.run_id,
        "job_id": f"{config.run_id}-array-0",
        "layer_id": f"{config.run_id}-q-down-rp",
        "tilejson_url": f"{config.object_prefix}/tiles/{config.run_id}/tilejson.json",
        "publication_time": _format_time(config.source_cycle + timedelta(hours=3)),
    }


def _write_shud_output_qc(
    config: ProductionE2EConfig,
    writer: EvidenceWriter,
    derived_ids: Mapping[str, Any],
) -> dict[str, Any]:
    raw_dir = _safe_raw_shud_dir(config)
    rivqdown_path = raw_dir / f"{config.run_id}.rivqdown"
    log_path = raw_dir / "shud.log"
    if config.force:
        _remove_current_qc_outputs(config, rivqdown_path, log_path)
    _write_qc_fixture(config, writer, rivqdown_path, log_path)
    return _qc_result(config, rivqdown_path, log_path, derived_ids)


def _safe_raw_shud_dir(config: ProductionE2EConfig) -> Path:
    raw_dir = config.lane_dir / "raw" / "shud"
    _validate_evidence_path_contained(config, raw_dir, path_kind="SHUD raw output directory")
    raw_dir.mkdir(parents=True, exist_ok=True)
    _validate_evidence_path_contained(config, raw_dir, path_kind="SHUD raw output directory")
    return raw_dir


def _validate_evidence_path_contained(config: ProductionE2EConfig, path: Path, *, path_kind: str) -> None:
    _refuse_symlink_components(path)
    resolved_path = path.resolve(strict=False)
    resolved_lane = config.lane_dir.resolve(strict=False)
    for root, message in (
        (config.evidence_root, "Evidence path must stay under evidence root."),
        (resolved_lane, "Evidence path must stay under evidence lane."),
    ):
        try:
            resolved_path.relative_to(root)
        except ValueError as error:
            raise ProductionE2EValidationError(
                "PRODUCTION_E2E_EVIDENCE_PATH_UNSAFE",
                message,
            ) from error
    if path.is_symlink():
        raise ProductionE2EValidationError(
            "PRODUCTION_E2E_EVIDENCE_SYMLINK",
            f"{path_kind} must not be a symlink: {path}",
        )


def _remove_current_qc_outputs(config: ProductionE2EConfig, *paths: Path) -> None:
    for path in paths:
        _validate_evidence_path_contained(config, path, path_kind="SHUD raw output")
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise ProductionE2EValidationError(
                    "PRODUCTION_E2E_EVIDENCE_SYMLINK",
                    f"SHUD raw output must not be a symlink: {path}",
                )
            path.unlink()


def _write_qc_fixture(
    config: ProductionE2EConfig,
    writer: EvidenceWriter,
    rivqdown_path: Path,
    log_path: Path,
) -> None:
    if config.shud_qc_fixture != "missing_rivqdown":
        first_time = _format_time(config.source_cycle)
        second_time = _format_time(config.source_cycle + timedelta(hours=3))
        mismatched_time = _format_time(config.source_cycle + timedelta(hours=6))
        rows = {
            "valid": f"time,seg_a,seg_b\n{first_time},86400,172800\n{second_time},129600,216000\n",
            "missing_required_output": (
                f"time,seg_a,seg_b\n{first_time},86400,172800\n{second_time},129600,216000\n"
            ),
            "malformed_columns": f"time,seg_a\n{first_time},86400\n{second_time},129600\n",
            "non_finite": f"time,seg_a,seg_b\n{first_time},NaN,172800\n{second_time},129600,216000\n",
            "count_mismatch": f"time,seg_a,seg_b\n{first_time},86400,172800\n",
            "time_axis_mismatch": (
                f"time,seg_a,seg_b\n{first_time},86400,172800\n{mismatched_time},129600,216000\n"
            ),
        }[config.shud_qc_fixture]
        writer.write_text(rivqdown_path, rows)
    if config.shud_qc_fixture != "missing_required_output":
        writer.write_text(
            log_path,
            f"run_id={config.run_id} source_cycle={_format_time(config.source_cycle)} status=completed\n",
        )


def _qc_result(
    config: ProductionE2EConfig,
    rivqdown_path: Path,
    log_path: Path,
    derived_ids: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_evidence_path_contained(config, rivqdown_path, path_kind="SHUD raw output")
    _validate_evidence_path_contained(config, log_path, path_kind="SHUD raw output")
    retained_paths = {
        "raw_output_dir": str(rivqdown_path.parent),
        "rivqdown": str(rivqdown_path) if rivqdown_path.exists() else None,
        "log": str(log_path) if log_path.exists() else None,
    }
    if not rivqdown_path.is_file():
        return _qc_blocked(
            config,
            "SHUD_RIVQDOWN_MISSING",
            "Required .rivqdown output is missing.",
            retained_paths,
        )
    if not log_path.is_file():
        return _qc_blocked(
            config,
            "SHUD_REQUIRED_OUTPUT_MISSING",
            "Required SHUD runtime log output is missing.",
            retained_paths,
        )

    lines = [
        line.strip()
        for line in rivqdown_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) < 2:
        return _qc_blocked(
            config,
            "SHUD_RIVQDOWN_MALFORMED_COLUMNS",
            ".rivqdown contains no data rows.",
            retained_paths,
        )
    header = _split_qc_row(lines[0])
    if header != ["time", *DERIVED_SEGMENT_IDS]:
        return _qc_blocked(
            config,
            "SHUD_RIVQDOWN_MALFORMED_COLUMNS",
            f".rivqdown header must be time plus {len(DERIVED_SEGMENT_IDS)} segment columns.",
            retained_paths,
        )
    data_rows = [_split_qc_row(line) for line in lines[1:]]
    for index, row in enumerate(data_rows, start=2):
        if len(row) != len(header):
            return _qc_blocked(
                config,
                "SHUD_RIVQDOWN_MALFORMED_COLUMNS",
                f".rivqdown row {index} has {len(row)} columns; expected {len(header)}.",
                retained_paths,
            )
        for value in row[1:]:
            try:
                parsed = float(value)
            except ValueError:
                return _qc_blocked(
                    config,
                    "SHUD_RIVQDOWN_MALFORMED_COLUMNS",
                    f".rivqdown row {index} contains a non-numeric value.",
                    retained_paths,
                )
            if not math.isfinite(parsed):
                return _qc_blocked(
                    config,
                    "SHUD_RIVQDOWN_NON_FINITE",
                    f".rivqdown row {index} contains NaN or Inf.",
                    retained_paths,
                )
    if len(data_rows) != len(EXPECTED_TIMESTEP_HOURS):
        return _qc_blocked(
            config,
            "SHUD_RIVQDOWN_COUNT_MISMATCH",
            f".rivqdown has {len(data_rows)} data rows; expected {len(EXPECTED_TIMESTEP_HOURS)}.",
            retained_paths,
        )
    expected_times = {
        _format_time(config.source_cycle + timedelta(hours=hours)) for hours in EXPECTED_TIMESTEP_HOURS
    }
    observed_times = {row[0] for row in data_rows}
    if observed_times != expected_times:
        return _qc_blocked(
            config,
            "SHUD_RIVQDOWN_TIME_AXIS_MISMATCH",
            f".rivqdown times {sorted(observed_times)} do not match expected {sorted(expected_times)}.",
            retained_paths,
        )
    return {
        "schema": "nhms.production_closure.e2e.shud_output_qc.v1",
        "run_id": config.run_id,
        "status": "pass",
        "qc_passed": True,
        "error_code": None,
        "message": "SHUD .rivqdown and required outputs passed deterministic closure QC.",
        "retained_paths": retained_paths,
        "expected": {
            "segment_count": len(DERIVED_SEGMENT_IDS),
            "row_count": len(EXPECTED_TIMESTEP_HOURS),
            "times": sorted(expected_times),
        },
        "observed": {
            "segment_ids": list(derived_ids["segment_ids"]),
            "row_count": len(data_rows),
            "times": sorted(observed_times),
        },
        "downstream_publication_blocked": False,
        "downstream_blocked_stages": [],
    }


def _qc_blocked(
    config: ProductionE2EConfig,
    error_code: str,
    message: str,
    retained_paths: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": "nhms.production_closure.e2e.shud_output_qc.v1",
        "run_id": config.run_id,
        "status": "blocked",
        "qc_passed": False,
        "error_code": error_code,
        "message": message,
        "retained_paths": dict(retained_paths),
        "downstream_publication_blocked": True,
        "downstream_blocked_stages": sorted(DOWNSTREAM_STAGE_NAMES),
        "stable_error_metadata": {
            "error_code": error_code,
            "category": "shud_output_qc",
            "blocks_publication": True,
        },
        "next_step": "Inspect retained SHUD raw output and logs, regenerate valid .rivqdown, then rerun with --force.",
    }


def _stage_manifest(
    config: ProductionE2EConfig,
    derived_ids: Mapping[str, Any],
    dependency_status: Mapping[str, Any],
    qc: Mapping[str, Any],
    stage_artifacts: Mapping[str, list[str]],
) -> dict[str, Any]:
    qc_blocker = _qc_stage_blocker(qc)
    dependency_blocker = _dependency_stage_blocker(dependency_status)
    stages = []
    for stage_name in STAGE_NAMES:
        blocked_by_qc = stage_name in DOWNSTREAM_STAGE_NAMES and qc.get("status") != "pass"
        blocked_by_dependency = dependency_blocker is not None
        stage_blockers = []
        if blocked_by_dependency:
            stage_blockers.append(dependency_blocker)
        if blocked_by_qc and qc_blocker:
            stage_blockers.append(qc_blocker)
        stage_status = "blocked" if blocked_by_qc or blocked_by_dependency else "ready"
        stages.append(
            {
                "stage": stage_name,
                "status": stage_status,
                "execution_mode": _stage_execution_mode(stage_name),
                "live_success_claimed": False,
                "inputs": _stage_inputs(config, stage_name, derived_ids),
                "outputs": [] if stage_status == "blocked" else stage_artifacts[stage_name],
                "blockers": stage_blockers,
            }
        )
    return {
        "schema": "nhms.production_closure.e2e.stage_manifest.v1",
        "run_id": config.run_id,
        "source_cycle": _format_time(config.source_cycle),
        "derived_identifiers": dict(derived_ids),
        "dependency_status": dependency_status["dependencies"],
        "stages": stages,
        "stage_statuses": {stage["stage"]: stage["status"] for stage in stages},
        "blockers": [blocker for blocker in (dependency_blocker, qc_blocker) if blocker],
    }


def _api_contract_evidence(
    config: ProductionE2EConfig,
    derived_ids: Mapping[str, Any],
    dependency_status: Mapping[str, Any],
    stage_manifest: Mapping[str, Any],
    qc: Mapping[str, Any],
) -> dict[str, Any]:
    dependency_blocker = _dependency_stage_blocker(dependency_status)
    if dependency_blocker:
        return {
            "schema": "nhms.production_closure.e2e.api_contract.v1",
            "run_id": config.run_id,
            "status": "blocked",
            "execution_mode": "not_executed",
            "live_api_executed": False,
            "blockers": [dependency_blocker],
            "reason": "API publication checks are blocked by missing or invalid dependency evidence.",
            "contract_queries": [],
        }
    if qc.get("status") != "pass":
        return {
            "schema": "nhms.production_closure.e2e.api_contract.v1",
            "run_id": config.run_id,
            "status": "blocked",
            "execution_mode": "not_executed",
            "live_api_executed": False,
            "blockers": [_qc_stage_blocker(qc)],
            "reason": "API publication checks are blocked by SHUD output QC.",
            "contract_queries": [],
        }
    return {
        "schema": "nhms.production_closure.e2e.api_contract.v1",
        "run_id": config.run_id,
        "status": "ready",
        "execution_mode": "deterministic_contract_evidence",
        "api_base": config.frontend_api_base,
        "live_api_executed": False,
        "db_target": config.db_target,
        "derived_identifiers": dict(derived_ids),
        "stage_statuses": stage_manifest["stage_statuses"],
        "contract_queries": _api_contract_queries(derived_ids),
        "run_id_specific_api_filters_added": False,
        "notes": "Fast path records existing-contract queries from derived IDs without contacting a live API.",
    }


def _frontend_smoke_evidence(
    config: ProductionE2EConfig,
    derived_ids: Mapping[str, Any],
    dependency_status: Mapping[str, Any],
    stage_manifest: Mapping[str, Any],
    qc: Mapping[str, Any],
) -> dict[str, Any]:
    dependency_blocker = _dependency_stage_blocker(dependency_status)
    if dependency_blocker:
        return {
            "schema": "nhms.production_closure.e2e.frontend_smoke.v1",
            "run_id": config.run_id,
            "status": "blocked",
            "execution_mode": "not_executed",
            "live_frontend_executed": False,
            "blockers": [dependency_blocker],
            "reason": "Frontend smoke is blocked by missing or invalid dependency evidence.",
        }
    if qc.get("status") != "pass":
        return {
            "schema": "nhms.production_closure.e2e.frontend_smoke.v1",
            "run_id": config.run_id,
            "status": "blocked",
            "execution_mode": "not_executed",
            "live_frontend_executed": False,
            "blockers": [_qc_stage_blocker(qc)],
            "reason": "Frontend smoke is blocked by SHUD output QC.",
        }
    return {
        "schema": "nhms.production_closure.e2e.frontend_smoke.v1",
        "run_id": config.run_id,
        "status": "ready",
        "execution_mode": "deterministic_evidence_backed_fixture",
        "frontend_api_base": config.frontend_api_base,
        "live_frontend_executed": False,
        "running_frontend_required": False,
        "mock_api_routes_used": False,
        "mock_only_placeholder_accepted": False,
        "staging_frontend_readiness_claimed": False,
        "lineage": {
            "source": derived_ids["source"],
            "cycle_time": derived_ids["cycle_time"],
            "model_id": derived_ids["model_id"],
            "run_id": config.run_id,
            "qc_status": qc["status"],
            "publication_time": derived_ids["publication_time"],
        },
        "surfaces": {
            "map": {"status": "deterministic_evidence", "layer_id": derived_ids["layer_id"]},
            "forecast_curve": {"status": "deterministic_evidence", "segment_id": derived_ids["segment_id"]},
            "monitoring": {"status": "deterministic_evidence", "job_id": derived_ids["job_id"]},
            "alerts": {"status": "deterministic_evidence", "segment_id": derived_ids["segment_id"]},
        },
        "stage_statuses": stage_manifest["stage_statuses"],
    }


def _summary(
    config: ProductionE2EConfig,
    *,
    status: str,
    blockers: list[dict[str, Any]],
    derived_ids: Mapping[str, Any],
    stage_manifest: Mapping[str, Any],
    qc: Mapping[str, Any],
    api_evidence: Mapping[str, Any],
    frontend_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": "nhms.production_closure.e2e.v1",
        "issue": 150,
        "run_id": config.run_id,
        "status": status,
        "evidence_dir": str(config.lane_dir),
        "source_cycle": _format_time(config.source_cycle),
        "model_set": list(config.model_set),
        "db_target": config.db_target,
        "object_prefix": config.object_prefix,
        "slurm_partition": config.slurm_partition,
        "slurm_account": config.slurm_account,
        "frontend_api_base": config.frontend_api_base,
        "derived_identifiers": dict(derived_ids),
        "stage_statuses": stage_manifest["stage_statuses"],
        "blockers": blockers,
        "object_uris": _object_uris(config, derived_ids),
        "logs": qc.get("retained_paths", {}),
        "qc_result": {
            "status": qc.get("status"),
            "error_code": qc.get("error_code"),
            "downstream_publication_blocked": qc.get("downstream_publication_blocked"),
        },
        "tile_artifacts": _ready_stage_outputs(stage_manifest, "tile"),
        "api_status": api_evidence.get("status"),
        "frontend_status": frontend_evidence.get("status"),
        "files": [
            "preflight.json",
            "dependency_status.json",
            "stage_manifest.json",
            "api_contract_evidence.json",
            "frontend_smoke_evidence.json",
            "shud_output_qc.json",
            "environment.json",
            "summary.json",
        ],
    }


def _summary_blockers(dependency_status: Mapping[str, Any], qc: Mapping[str, Any]) -> list[dict[str, Any]]:
    blockers = list(dependency_status.get("blockers", []))
    qc_blocker = _qc_stage_blocker(qc)
    if qc_blocker:
        blockers.append(qc_blocker)
    return blockers


def _dependency_stage_blocker(dependency_status: Mapping[str, Any]) -> dict[str, Any] | None:
    blockers = list(dependency_status.get("blockers", []))
    if not blockers:
        return None
    dependencies = [
        {
            "dependency": item.get("dependency"),
            "status": item.get("status"),
            "summary_status": item.get("summary_status"),
            "summary_path": item.get("summary_path"),
            "reason": item.get("reason"),
        }
        for item in dependency_status.get("dependencies", [])
        if item.get("status") in {"blocked", "missing"}
    ]
    return {
        "error_code": "PRODUCTION_E2E_DEPENDENCY_CHAIN_BLOCKED",
        "stage": "dependency_evidence",
        "message": "Supplied #147/#148/#149 dependency evidence is missing, blocked, invalid, or not ready.",
        "blocks": list(STAGE_NAMES),
        "dependencies": dependencies,
    }


def _qc_stage_blocker(qc: Mapping[str, Any]) -> dict[str, Any] | None:
    error_code = qc.get("error_code")
    if not error_code:
        return None
    return {
        "error_code": error_code,
        "stage": "shud_output_qc",
        "message": qc.get("message", "SHUD output QC failed."),
        "blocks": sorted(DOWNSTREAM_STAGE_NAMES),
    }


def _stage_execution_mode(stage_name: str) -> str:
    if stage_name in {"api", "frontend"}:
        return "deterministic_contract_evidence"
    if stage_name == "slurm":
        return "deterministic_slurm_evidence"
    return "deterministic_fixture"


def _write_stage_artifacts(
    config: ProductionE2EConfig,
    writer: EvidenceWriter,
    derived_ids: Mapping[str, Any],
    dependency_status: Mapping[str, Any],
    qc: Mapping[str, Any],
) -> dict[str, list[str]]:
    artifacts_dir = config.lane_dir / "stage_artifacts"
    if config.force:
        _remove_current_stage_artifacts(config, artifacts_dir)
    outputs: dict[str, list[str]] = {
        "download": [str(artifacts_dir / "download" / "raw_cycle_manifest.json")],
        "canonical": [str(artifacts_dir / "canonical" / "canonical_manifest.json")],
        "forcing": [str(artifacts_dir / "forcing" / "forcing_manifest.json")],
        "slurm": [str(artifacts_dir / "slurm" / "slurm_manifest.json"), str(config.lane_dir / "raw" / "shud")],
        "parse": [str(artifacts_dir / "parse" / "parsed_timeseries_manifest.json")],
        "frequency": [str(artifacts_dir / "frequency" / "return_period_manifest.json")],
        "tile": [
            str(artifacts_dir / "tile" / "tilejson.json"),
            str(artifacts_dir / "tile" / "0" / "0" / "0.pbf"),
        ],
        "api": [str(artifacts_dir / "api" / "api_contract_queries.json")],
        "frontend": [str(artifacts_dir / "frontend" / "frontend_lineage.json")],
    }
    base_payload = {
        "run_id": config.run_id,
        "source_cycle": _format_time(config.source_cycle),
        "model_id": derived_ids["model_id"],
        "source": derived_ids["source"],
        "cycle_time": derived_ids["cycle_time"],
        "execution_mode": "deterministic_fixture",
        "live_db_executed": False,
        "live_api_executed": False,
        "live_slurm_executed": False,
        "live_frontend_executed": False,
    }
    dependency_blocker = _dependency_stage_blocker(dependency_status)
    if dependency_blocker:
        for stage_name, stage_outputs in outputs.items():
            for output in stage_outputs:
                if output.endswith(".json"):
                    writer.write_json(
                        Path(output),
                        _blocked_stage_artifact_payload(config, stage_name, derived_ids, dependency_blocker),
                    )
        return outputs
    qc_blocker = _qc_stage_blocker(qc)
    writer.write_json(
        Path(outputs["download"][0]),
        {
            **base_payload,
            "schema": "nhms.production_closure.e2e.stage.download.v1",
            "status": "ready",
            "raw_cycle_uri": f"{config.object_prefix}/raw/{derived_ids['source']}/{derived_ids['cycle_time']}/",
            "files": [{"name": "gfs_surface.grib2", "bytes": 128, "sha256": "deterministic-download-fixture"}],
        },
    )
    writer.write_json(
        Path(outputs["canonical"][0]),
        {
            **base_payload,
            "schema": "nhms.production_closure.e2e.stage.canonical.v1",
            "status": "ready",
            "input_manifest": outputs["download"][0],
            "canonical_uri": f"{config.object_prefix}/canonical/{derived_ids['source']}/{derived_ids['cycle_time']}/",
            "products": [{"variable": "precipitation_rate", "unit": "mm/h"}],
        },
    )
    writer.write_json(
        Path(outputs["forcing"][0]),
        {
            **base_payload,
            "schema": "nhms.production_closure.e2e.stage.forcing.v1",
            "status": "ready",
            "input_manifest": outputs["canonical"][0],
            "forcing_uri": f"{config.object_prefix}/forcing/{config.run_id}/forcing.json",
            "segment_ids": list(derived_ids["segment_ids"]),
        },
    )
    writer.write_json(
        Path(outputs["slurm"][0]),
        {
            **base_payload,
            "schema": "nhms.production_closure.e2e.stage.slurm.v1",
            "status": "ready",
            "execution_mode": "deterministic_slurm_evidence",
            "live_slurm_executed": False,
            "job_id": derived_ids["job_id"],
            "raw_output_dir": str(config.lane_dir / "raw" / "shud"),
            "qc_status": qc.get("status"),
        },
    )
    if qc.get("status") != "pass":
        for stage_name in sorted(DOWNSTREAM_STAGE_NAMES):
            for output in outputs[stage_name]:
                if output.endswith(".json"):
                    writer.write_json(
                        Path(output),
                        _blocked_stage_artifact_payload(config, stage_name, derived_ids, qc_blocker),
                    )
        return outputs
    if qc.get("status") == "pass":
        writer.write_json(
            Path(outputs["parse"][0]),
            {
                **base_payload,
                "schema": "nhms.production_closure.e2e.stage.parse.v1",
                "status": "ready",
                "input_rivqdown": qc.get("retained_paths", {}).get("rivqdown"),
                "records": len(DERIVED_SEGMENT_IDS) * len(EXPECTED_TIMESTEP_HOURS),
                "segment_ids": list(derived_ids["segment_ids"]),
            },
        )
        writer.write_json(
            Path(outputs["frequency"][0]),
            {
                **base_payload,
                "schema": "nhms.production_closure.e2e.stage.frequency.v1",
                "status": "ready",
                "input_manifest": outputs["parse"][0],
                "return_periods": [2, 5, 10],
                "segment_id": derived_ids["segment_id"],
            },
        )
        writer.write_json(
            Path(outputs["tile"][0]),
            {
                "tilejson": "3.0.0",
                "name": f"{config.run_id}-flood-return-period",
                "tiles": [f"{config.object_prefix}/tiles/{config.run_id}/{{z}}/{{x}}/{{y}}.pbf"],
                "metadata": {
                    **base_payload,
                    "schema": "nhms.production_closure.e2e.stage.tile.v1",
                    "status": "ready",
                    "layer_id": derived_ids["layer_id"],
                    "run_id": config.run_id,
                    "duration": "PT3H",
                    "valid_time": derived_ids["publication_time"],
                },
            },
        )
        writer.write_text(Path(outputs["tile"][1]), "deterministic mvt fixture\n")
        writer.write_json(
            Path(outputs["api"][0]),
            {
                **base_payload,
                "schema": "nhms.production_closure.e2e.stage.api.v1",
                "status": "ready",
                "execution_mode": "deterministic_contract_evidence",
                "contract_queries": _api_contract_queries(derived_ids),
            },
        )
        writer.write_json(
            Path(outputs["frontend"][0]),
            {
                **base_payload,
                "schema": "nhms.production_closure.e2e.stage.frontend.v1",
                "status": "ready",
                "execution_mode": "deterministic_evidence_backed_fixture",
                "staging_frontend_readiness_claimed": False,
                "lineage": {
                    "source": derived_ids["source"],
                    "cycle_time": derived_ids["cycle_time"],
                    "model_id": derived_ids["model_id"],
                    "run_id": config.run_id,
                    "publication_time": derived_ids["publication_time"],
                },
            },
        )
    return outputs


def _remove_current_stage_artifacts(config: ProductionE2EConfig, artifacts_dir: Path) -> None:
    _validate_evidence_path_contained(config, artifacts_dir, path_kind="stage_artifacts directory")
    if not artifacts_dir.exists() and not artifacts_dir.is_symlink():
        return
    if artifacts_dir.is_symlink():
        raise ProductionE2EValidationError(
            "PRODUCTION_E2E_EVIDENCE_SYMLINK",
            f"stage_artifacts directory must not be a symlink: {artifacts_dir}",
        )
    for path in sorted(artifacts_dir.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        _validate_evidence_path_contained(config, path, path_kind="stage artifact")
        if path.is_symlink():
            raise ProductionE2EValidationError(
                "PRODUCTION_E2E_EVIDENCE_SYMLINK",
                f"stage artifact must not be a symlink: {path}",
            )
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    artifacts_dir.rmdir()


def _blocked_stage_artifact_payload(
    config: ProductionE2EConfig,
    stage_name: str,
    derived_ids: Mapping[str, Any],
    blocker: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema": f"nhms.production_closure.e2e.stage.{stage_name}.v1",
        "run_id": config.run_id,
        "source_cycle": _format_time(config.source_cycle),
        "model_id": derived_ids["model_id"],
        "source": derived_ids["source"],
        "cycle_time": derived_ids["cycle_time"],
        "status": "blocked",
        "execution_mode": "not_executed",
        "live_db_executed": False,
        "live_api_executed": False,
        "live_slurm_executed": False,
        "live_frontend_executed": False,
        "blockers": [dict(blocker)] if blocker else [],
        "reason": _blocked_stage_artifact_reason(blocker),
    }


def _blocked_stage_artifact_reason(blocker: Mapping[str, Any] | None) -> str:
    if blocker and blocker.get("stage") == "shud_output_qc":
        return "Stage artifact was not executed because SHUD output QC blocked downstream publication."
    return "Stage artifact was not executed because dependency evidence is missing, blocked, invalid, or not ready."


def _api_contract_queries(derived_ids: Mapping[str, Any]) -> list[dict[str, Any]]:
    run_id = derived_ids["run_id"]
    segment_id = derived_ids["segment_id"]
    return [
        {
            "contract": "model_detail",
            "method": "GET",
            "path": f"/api/v1/models/{derived_ids['model_id']}",
            "status": "deterministic_evidence",
        },
        {
            "contract": "forecast_series",
            "method": "GET",
            "path": (
                f"/api/v1/basin-versions/{derived_ids['basin_version_id']}/river-segments/"
                f"{segment_id}/forecast-series"
            ),
            "query": {
                "issue_time": derived_ids["cycle_time"],
                "variables": "q_down",
                "scenarios": derived_ids["source"],
                "include_analysis": "false",
            },
            "status": "deterministic_evidence",
        },
        {
            "contract": "flood_alerts_summary",
            "method": "GET",
            "path": "/api/v1/flood-alerts/summary",
            "query": {"run_id": run_id},
            "status": "deterministic_evidence",
        },
        {
            "contract": "flood_alerts_ranking",
            "method": "GET",
            "path": "/api/v1/flood-alerts/ranking",
            "query": {"run_id": run_id, "limit": 10, "offset": 0, "valid_time": derived_ids["publication_time"]},
            "status": "deterministic_evidence",
        },
        {
            "contract": "flood_alerts_timeline",
            "method": "GET",
            "path": "/api/v1/flood-alerts/timeline",
            "query": {"run_id": run_id, "segment_id": segment_id},
            "status": "deterministic_evidence",
        },
        {
            "contract": "jobs",
            "method": "GET",
            "path": "/api/v1/jobs",
            "query": {
                "model_id": derived_ids["model_id"],
                "source": derived_ids["source"],
                "cycle_time": derived_ids["cycle_time"],
            },
            "status": "deterministic_evidence",
        },
        {
            "contract": "job_logs",
            "method": "GET",
            "path": f"/api/v1/jobs/{derived_ids['job_id']}/logs",
            "status": "deterministic_evidence",
        },
        {
            "contract": "tile_metadata",
            "method": "GET",
            "path": "/api/v1/tiles/flood-return-period",
            "query": {
                "run_id": run_id,
                "duration": "PT3H",
                "valid_time": derived_ids["publication_time"],
            },
            "status": "deterministic_evidence",
        },
    ]


def _stage_inputs(config: ProductionE2EConfig, stage_name: str, derived_ids: Mapping[str, Any]) -> list[str]:
    inputs = {
        "download": [f"source:{derived_ids['source']} cycle:{derived_ids['cycle_time']}"],
        "canonical": [f"{config.object_prefix}/raw/{derived_ids['source']}/{derived_ids['cycle_time']}"],
        "forcing": [f"{config.object_prefix}/canonical/{derived_ids['source']}/{derived_ids['cycle_time']}"],
        "slurm": [f"{config.object_prefix}/forcing/{config.run_id}/forcing.json"],
        "parse": [str(config.lane_dir / "raw" / "shud")],
        "frequency": [f"db:{config.db_target}:river_timeseries:{config.run_id}"],
        "tile": [f"db:{config.db_target}:flood_frequency:{config.run_id}"],
        "api": [f"db:{config.db_target}", f"layer:{derived_ids['layer_id']}"],
        "frontend": [config.frontend_api_base, f"run:{config.run_id}"],
    }
    return inputs[stage_name]


def _ready_stage_outputs(stage_manifest: Mapping[str, Any], stage_name: str) -> list[str]:
    for stage in stage_manifest.get("stages", []):
        if stage.get("stage") == stage_name and stage.get("status") == "ready":
            outputs = stage.get("outputs", [])
            return list(outputs) if isinstance(outputs, list) else []
    return []


def _object_uris(config: ProductionE2EConfig, derived_ids: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "raw": f"{config.object_prefix}/raw/{derived_ids['source']}/{derived_ids['cycle_time']}/",
        "canonical": f"{config.object_prefix}/canonical/{derived_ids['source']}/{derived_ids['cycle_time']}/",
        "forcing": f"{config.object_prefix}/forcing/{config.run_id}/forcing.json",
        "shud_output": f"{config.object_prefix}/runs/{config.run_id}/output/{config.run_id}.rivqdown",
        "tile_artifacts": [
            f"{config.object_prefix}/tiles/{config.run_id}/tilejson.json",
            f"{config.object_prefix}/tiles/{config.run_id}/0/0/0.pbf",
        ],
    }


def _environment_payload(config: ProductionE2EConfig) -> dict[str, Any]:
    env_keys = [
        "NHMS_RUN_PRODUCTION_CLOSURE",
        "NHMS_PRODUCTION_E2E_SOURCE_CYCLE",
        "NHMS_PRODUCTION_E2E_MODEL_SET",
        "NHMS_PRODUCTION_E2E_DB_TARGET",
        "NHMS_PRODUCTION_E2E_OBJECT_PREFIX",
        "NHMS_PRODUCTION_E2E_SLURM_PARTITION",
        "NHMS_PRODUCTION_E2E_SLURM_ACCOUNT",
        "NHMS_PRODUCTION_E2E_FRONTEND_API_BASE",
        "NHMS_PRODUCTION_E2E_SLURM_EVIDENCE_ROOT",
        "NHMS_PRODUCTION_E2E_OBJECT_STORE_EVIDENCE_ROOT",
        "NHMS_PRODUCTION_E2E_MET_EVIDENCE_ROOT",
        "NHMS_PRODUCTION_E2E_SHUD_QC_FIXTURE",
        "DATABASE_URL",
        "AWS_SECRET_ACCESS_KEY",
        "CDSAPI_KEY",
        "IFS_API_KEY",
    ]
    return {
        "schema": "nhms.production_closure.e2e.environment.v1",
        "run_id": config.run_id,
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": str(Path.cwd()),
        "env": {key: os.getenv(key, "") for key in env_keys if key in os.environ},
        "redaction": {
            "secret_shaped_values_redacted": True,
            "stdout_redacted": True,
            "evidence_redacted": True,
        },
    }


def _validate_config(config: ProductionE2EConfig) -> None:
    if config.shud_qc_fixture not in VALID_QC_FIXTURES:
        raise ProductionE2EValidationError(
            "PRODUCTION_E2E_QC_FIXTURE_INVALID",
            f"--shud-qc-fixture must be one of {', '.join(sorted(VALID_QC_FIXTURES))}.",
        )
    if not config.model_set:
        raise ProductionE2EValidationError("PRODUCTION_E2E_MODEL_SET_INVALID", "At least one model must be selected.")
    if len(config.model_set) > 1:
        raise ProductionE2EValidationError(
            "PRODUCTION_E2E_MODEL_SET_UNSUPPORTED",
            "Staging E2E closure currently supports one selected model until per-model evidence is implemented.",
        )
    for model_id in config.model_set:
        _validate_identifier(model_id, "model_id")
    for value, field_name in (
        (config.db_target, "db_target"),
        (config.slurm_partition, "slurm_partition"),
        (config.slurm_account, "slurm_account"),
    ):
        if value and not SAFE_IDENTIFIER_RE.fullmatch(value):
            raise ProductionE2EValidationError(
                "PRODUCTION_E2E_IDENTIFIER_UNSAFE",
                f"{field_name} must be a safe staging identifier.",
            )
    _validate_frontend_api_base(config.frontend_api_base)


def _validate_identifier(value: str, field_name: str) -> None:
    if not SAFE_IDENTIFIER_RE.fullmatch(value):
        raise ProductionE2EValidationError(
            "PRODUCTION_E2E_IDENTIFIER_UNSAFE",
            f"{field_name} must be a safe staging identifier.",
        )


def _validate_frontend_api_base(value: str) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ProductionE2EValidationError(
            "PRODUCTION_E2E_FRONTEND_API_BASE_UNSAFE",
            "Frontend API base must not contain credential material.",
        ) from error
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProductionE2EValidationError(
            "PRODUCTION_E2E_FRONTEND_API_BASE_UNSAFE",
            "Frontend API base must not contain userinfo credentials, query parameters, or fragments.",
        )


def _validate_object_prefix_safe(prefix: str) -> None:
    if not prefix:
        raise ProductionE2EValidationError(
            "PRODUCTION_E2E_OBJECT_PREFIX_INVALID",
            "E2E object prefix must not be empty.",
        )
    try:
        parsed = urlsplit(prefix)
    except ValueError as error:
        raise ProductionE2EValidationError(
            "PRODUCTION_E2E_OBJECT_PREFIX_UNSAFE",
            "E2E object prefix must not contain credential material.",
        ) from error
    if not parsed.scheme or not parsed.netloc:
        raise ProductionE2EValidationError(
            "PRODUCTION_E2E_OBJECT_PREFIX_INVALID",
            "E2E object prefix must be an object URI prefix such as s3://bucket/prefix.",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProductionE2EValidationError(
            "PRODUCTION_E2E_OBJECT_PREFIX_UNSAFE",
            "E2E object prefix must not contain userinfo credentials, query parameters, or fragments.",
        )
    for raw_segment in parsed.path.split("/"):
        segment = unquote(raw_segment)
        if "/" in segment or "\\" in segment or segment in {".", ".."}:
            raise ProductionE2EValidationError(
                "PRODUCTION_E2E_OBJECT_PREFIX_UNSAFE",
                "E2E object prefix path segments must not contain '.', '..', or decoded path separators.",
            )
        decoded_parts = SENSITIVE_PREFIX_SEPARATOR_RE.split(segment)
        if any(SENSITIVE_PREFIX_ASSIGNMENT_RE.search(part) for part in decoded_parts):
            raise ProductionE2EValidationError(
                "PRODUCTION_E2E_OBJECT_PREFIX_UNSAFE",
                "E2E object prefix path segments must not contain credential assignments.",
            )


def _parse_model_set(value: str | None) -> tuple[str, ...]:
    if not value:
        return DEFAULT_MODEL_SET
    models = tuple(part.strip() for part in value.split(",") if part.strip())
    if not models:
        raise ProductionE2EValidationError(
            "PRODUCTION_E2E_MODEL_SET_INVALID",
            "Model set must contain at least one model identifier.",
        )
    return tuple(dict.fromkeys(models))


def _parse_cycle_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProductionE2EValidationError(
            "PRODUCTION_E2E_SOURCE_CYCLE_INVALID",
            "Source cycle must be an ISO-8601 timestamp.",
        ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _split_qc_row(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\s,]+", value.strip()) if part.strip()]


def _safe_run_id(run_id: str) -> str:
    if SAFE_RUN_ID_RE.fullmatch(run_id):
        return run_id
    raise ProductionE2EValidationError(
        "PRODUCTION_E2E_RUN_ID_UNSAFE",
        "run_id may contain only alphanumeric characters, underscores, and hyphens.",
    )


def _safe_resolved_evidence_root(evidence_root: Path) -> Path:
    root = evidence_root.expanduser()
    if root.exists() or root.is_symlink():
        _refuse_symlink_components(root)
    parent = root.parent
    if parent.exists() or parent.is_symlink():
        _refuse_symlink_components(parent)
    return root.resolve(strict=False)


def _refuse_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part == path.anchor or part == "":
            continue
        current = current / part
        if current.is_symlink():
            raise ProductionE2EValidationError(
                "PRODUCTION_E2E_EVIDENCE_SYMLINK",
                f"Evidence path component must not be a symlink: {current}",
            )


def _dependency_root(env_name: str, value: Path | None) -> Path | None:
    if value is not None:
        return value.expanduser()
    raw = os.getenv(env_name)
    if not raw:
        return None
    return Path(raw).expanduser()


def _run_scoped_prefix(prefix: str, run_id: str) -> str:
    parsed = urlsplit(prefix.rstrip("/"))
    path = parsed.path.rstrip("/")
    run_segment = f"/runs/{run_id}/e2e"
    scoped_path = path if path.endswith(run_segment) else f"{path}{run_segment}" if path else run_segment
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, scoped_path, "", ""))


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("validate-e2e")
    @click.option("--evidence-root", type=click.Path(path_type=Path), required=True)
    @click.option("--run-id")
    @click.option("--source-cycle", default=None)
    @click.option("--model-set", default=None)
    @click.option("--db-target", default=None)
    @click.option("--object-prefix", default=None)
    @click.option("--slurm-partition", default=None)
    @click.option("--slurm-account", default=None)
    @click.option("--frontend-api-base", default=None)
    @click.option("--slurm-evidence-root", type=click.Path(path_type=Path), default=None)
    @click.option("--object-store-evidence-root", type=click.Path(path_type=Path), default=None)
    @click.option("--met-evidence-root", type=click.Path(path_type=Path), default=None)
    @click.option("--shud-qc-fixture", default=None)
    @click.option("--force", is_flag=True, default=False)
    def validate_e2e_command(
        evidence_root: Path,
        run_id: str | None,
        source_cycle: str | None,
        model_set: str | None,
        db_target: str | None,
        object_prefix: str | None,
        slurm_partition: str | None,
        slurm_account: str | None,
        frontend_api_base: str | None,
        slurm_evidence_root: Path | None,
        object_store_evidence_root: Path | None,
        met_evidence_root: Path | None,
        shud_qc_fixture: str | None,
        force: bool,
    ) -> None:
        try:
            summary = validate_e2e(
                ProductionE2EConfig.from_env(
                    evidence_root=evidence_root,
                    run_id=run_id,
                    source_cycle=source_cycle,
                    model_set=model_set,
                    db_target=db_target,
                    object_prefix=object_prefix,
                    slurm_partition=slurm_partition,
                    slurm_account=slurm_account,
                    frontend_api_base=frontend_api_base,
                    slurm_evidence_root=slurm_evidence_root,
                    object_store_evidence_root=object_store_evidence_root,
                    met_evidence_root=met_evidence_root,
                    shud_qc_fixture=shud_qc_fixture,
                    force=force,
                )
            )
            click.echo(json.dumps(redact_payload(summary), sort_keys=True))
        except ProductionE2EValidationError as error:
            click.echo(f"{error.error_code}: {redact_text(error.message)}", err=True)
            raise SystemExit(1) from error
        except Exception as error:
            click.echo(f"PRODUCTION_E2E_VALIDATION_FAILED: {redact_text(str(error))}", err=True)
            raise SystemExit(1) from error

    try:
        cli.main(args=list(argv) if argv is not None else None, standalone_mode=False)
    except click.ClickException as error:
        error.show()
        raise SystemExit(error.exit_code) from error
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-production")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate-e2e")
    validate_parser.add_argument("--evidence-root", type=Path, required=True)
    validate_parser.add_argument("--run-id")
    validate_parser.add_argument("--source-cycle", default=None)
    validate_parser.add_argument("--model-set", default=None)
    validate_parser.add_argument("--db-target", default=None)
    validate_parser.add_argument("--object-prefix", default=None)
    validate_parser.add_argument("--slurm-partition", default=None)
    validate_parser.add_argument("--slurm-account", default=None)
    validate_parser.add_argument("--frontend-api-base", default=None)
    validate_parser.add_argument("--slurm-evidence-root", type=Path, default=None)
    validate_parser.add_argument("--object-store-evidence-root", type=Path, default=None)
    validate_parser.add_argument("--met-evidence-root", type=Path, default=None)
    validate_parser.add_argument("--shud-qc-fixture", default=None)
    validate_parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "validate-e2e":
        try:
            print(
                json.dumps(
                    redact_payload(
                        validate_e2e(
                            ProductionE2EConfig.from_env(
                                evidence_root=args.evidence_root,
                                run_id=args.run_id,
                                source_cycle=args.source_cycle,
                                model_set=args.model_set,
                                db_target=args.db_target,
                                object_prefix=args.object_prefix,
                                slurm_partition=args.slurm_partition,
                                slurm_account=args.slurm_account,
                                frontend_api_base=args.frontend_api_base,
                                slurm_evidence_root=args.slurm_evidence_root,
                                object_store_evidence_root=args.object_store_evidence_root,
                                met_evidence_root=args.met_evidence_root,
                                shud_qc_fixture=args.shud_qc_fixture,
                                force=args.force,
                            )
                        )
                    ),
                    sort_keys=True,
                )
            )
        except ProductionE2EValidationError as error:
            print(f"{error.error_code}: {redact_text(error.message)}", file=sys.stderr)
            return 1
        except Exception as error:
            print(f"PRODUCTION_E2E_VALIDATION_FAILED: {redact_text(str(error))}", file=sys.stderr)
            return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
