from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit

from packages.common.redaction import redact_payload, redact_text
from services.orchestrator.retry import compute_backoff_seconds, is_transient_error
from services.production_closure.e2e_validation import (
    ProductionE2EConfig,
    ProductionE2EValidationError,
    validate_e2e,
)
from services.production_closure.met_validation import (
    ProductionMetConfig,
    ProductionMetValidationError,
    validate_met,
)
from services.production_closure.object_store_validation import (
    ProductionObjectStoreConfig,
    ProductionObjectStoreValidationError,
    validate_object_store,
)
from services.production_closure.scale_validation import (
    ProductionScaleConfig,
    ProductionScaleValidationError,
    validate_scale,
)
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES, SlurmGatewaySettings
from services.slurm_gateway.real_backend import RealSlurmGateway, map_slurm_error_code

SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SAFE_SLURM_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SLURM_WALLTIME_RE = re.compile(r"^(?:(?P<days>\d{1,3})-)?(?P<hours>\d{1,3}):(?P<minutes>[0-5]\d):(?P<seconds>[0-5]\d)$")
SENSITIVE_URI_ASSIGNMENT_RE = re.compile(
    r"(?:^|[/?&;\s])[^/?&;\s=]*(?:token|password|passwd|pwd|secret|credential|api[_-]?key|"
    r"access[_-]?key|session[_-]?key|signature)[^/?&;\s=]*=[^/?&;\s]*",
    re.IGNORECASE,
)
TERMINAL_SLURM_STATES = {
    "BOOT_FAIL",
    "CANCELLED",
    "COMPLETED",
    "DEADLINE",
    "FAILED",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "REVOKED",
    "SPECIAL_EXIT",
    "TIMEOUT",
}
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
DEFAULT_POLL_TIMEOUT_SECONDS = 900.0
MAX_POLL_INTERVAL_SECONDS = 300.0
MAX_POLL_TIMEOUT_SECONDS = 86400.0
MIN_POLL_INTERVAL_SECONDS = 1.0
CONTROLLED_FAILURE_LOG_MARKER = "NHMS_PRODUCTION_SLURM_CONTROLLED_FAILURE_EXPECTED"
CONTROLLED_FAILURE_LOG_SIGNATURES = ("NON_FINITE_FLOW",)
RESOURCE_LIMITS = {
    "nodes": 128,
    "ntasks": 4096,
    "cpus_per_task": 256,
    "memory_gb": 4096,
    "shud_threads": 256,
    "max_concurrent": 10000,
}


class ProductionValidationError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass
class EvidenceWriter:
    evidence_root: Path
    lane_dir: Path
    force: bool = False
    _created_paths: set[Path] = field(default_factory=set)

    def prepare(self) -> None:
        _refuse_symlink_components(self.evidence_root)
        _refuse_symlink_components(self.lane_dir.parent)
        if self.lane_dir.exists() or self.lane_dir.is_symlink():
            _refuse_symlink_components(self.lane_dir)
        resolved_lane = self.lane_dir.resolve()
        try:
            resolved_lane.relative_to(self.evidence_root)
        except ValueError as error:
            raise ProductionValidationError(
                "PRODUCTION_SLURM_EVIDENCE_PATH_UNSAFE",
                "Evidence lane directory must stay under evidence root.",
            ) from error
        self.lane_dir.mkdir(parents=True, exist_ok=True)

    def write_json(self, path: Path, payload: Any) -> None:
        self.write_text(
            path,
            json.dumps(redact_payload(payload), indent=2, sort_keys=True) + "\n",
            already_redacted=True,
        )

    def write_runtime_manifest_json(self, path: Path, payload: Any) -> None:
        self._write_bytes(
            path,
            (json.dumps(redact_payload(payload), indent=2, sort_keys=True) + "\n").encode("utf-8"),
            allow_outside_evidence=True,
            file_label="Runtime manifest",
            exists_error_code="PRODUCTION_SLURM_RUNTIME_MANIFEST_EXISTS",
            write_error_code="PRODUCTION_SLURM_RUNTIME_MANIFEST_WRITE_FAILED",
        )

    def write_text(self, path: Path, value: str, *, already_redacted: bool = False) -> None:
        content = value if already_redacted else redact_text(value)
        self._write_bytes(path, content.encode("utf-8"))

    def _write_bytes(
        self,
        path: Path,
        content: bytes,
        *,
        allow_outside_evidence: bool = False,
        file_label: str = "Evidence file",
        exists_error_code: str = "PRODUCTION_SLURM_EVIDENCE_EXISTS",
        write_error_code: str = "PRODUCTION_SLURM_EVIDENCE_WRITE_FAILED",
    ) -> None:
        safe_path = self._safe_file_path(path, allow_outside_evidence=allow_outside_evidence)
        if safe_path.exists() and safe_path not in self._created_paths and not self.force:
            raise ProductionValidationError(
                exists_error_code,
                f"{file_label} already exists: {safe_path}. Use --force to overwrite an existing run_id bundle.",
            )
        temp_path = safe_path.with_name(f".{safe_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_bytes(content)
            os.replace(temp_path, safe_path)
            self._created_paths.add(safe_path)
        except OSError as error:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise ProductionValidationError(
                write_error_code,
                f"Failed to write {file_label.lower()} {safe_path}: {error}",
            ) from error

    def _safe_file_path(self, path: Path, *, allow_outside_evidence: bool = False) -> Path:
        if path.is_symlink():
            raise ProductionValidationError(
                "PRODUCTION_SLURM_EVIDENCE_SYMLINK",
                f"Evidence file must not be a symlink: {path}",
            )
        _refuse_symlink_components(path.parent)
        resolved_parent = path.parent.resolve()
        if not allow_outside_evidence:
            try:
                resolved_parent.relative_to(self.evidence_root)
            except ValueError as error:
                raise ProductionValidationError(
                    "PRODUCTION_SLURM_EVIDENCE_PATH_UNSAFE",
                    "Evidence file path must stay under evidence root.",
                ) from error
        path.parent.mkdir(parents=True, exist_ok=True)
        return resolved_parent / path.name


@dataclass(frozen=True)
class ProductionSlurmConfig:
    evidence_root: Path
    run_id: str
    cluster: str
    account: str
    partition: str
    workspace_root: Path
    object_store_root: str
    object_store_prefix: str
    model_id: str
    model_package_uri: str
    solver_binary: str
    solver_module: str
    walltime: str
    nodes: int
    ntasks: int
    memory_gb: int
    cpus_per_task: int
    shud_threads: int
    max_concurrent: int
    submit: bool
    fake_slurm: bool
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS
    force: bool = False

    @property
    def lane_dir(self) -> Path:
        return self.evidence_root / self.run_id / "slurm"

    @classmethod
    def from_env(
        cls,
        *,
        evidence_root: Path,
        run_id: str | None,
        submit: bool,
        fake_slurm: bool,
        poll_interval_seconds: float | None = None,
        poll_timeout_seconds: float | None = None,
        force: bool = False,
    ) -> ProductionSlurmConfig:
        resolved_run_id = _safe_run_id(run_id or datetime.now(UTC).strftime("m10-%Y%m%dT%H%M%SZ"))
        workspace_root = Path(os.getenv("NHMS_PRODUCTION_SLURM_WORKSPACE_ROOT", "workspace")).expanduser()
        resolved_evidence_root = _safe_resolved_evidence_root(evidence_root)
        shud_threads_env_name = (
            "SHUD_THREADS" if os.getenv("SHUD_THREADS") is not None else "NHMS_PRODUCTION_SLURM_SHUD_THREADS"
        )
        config = cls(
            evidence_root=resolved_evidence_root,
            run_id=resolved_run_id,
            cluster=os.getenv("NHMS_PRODUCTION_SLURM_CLUSTER", ""),
            account=os.getenv("NHMS_PRODUCTION_SLURM_ACCOUNT", ""),
            partition=os.getenv("NHMS_PRODUCTION_SLURM_PARTITION", ""),
            workspace_root=workspace_root,
            object_store_root=_object_store_env_value(
                "NHMS_PRODUCTION_OBJECT_STORE_ROOT",
                "OBJECT_STORE_ROOT",
                str(workspace_root),
            ),
            object_store_prefix=_object_store_env_value(
                "NHMS_PRODUCTION_OBJECT_STORE_PREFIX",
                "OBJECT_STORE_PREFIX",
                "",
            ),
            model_id=os.getenv("NHMS_PRODUCTION_SLURM_MODEL_ID", "basins_qhh_shud"),
            model_package_uri=os.getenv("NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI", ""),
            solver_binary=os.getenv("SHUD_EXECUTABLE", os.getenv("NHMS_PRODUCTION_SLURM_SOLVER_BINARY", "shud_omp")),
            solver_module=os.getenv("NHMS_PRODUCTION_SLURM_SOLVER_MODULE", ""),
            walltime=os.getenv("NHMS_PRODUCTION_SLURM_WALLTIME", "00:30:00"),
            nodes=_positive_int_env("NHMS_PRODUCTION_SLURM_NODES", 1, maximum=RESOURCE_LIMITS["nodes"]),
            ntasks=_positive_int_env("NHMS_PRODUCTION_SLURM_NTASKS", 1, maximum=RESOURCE_LIMITS["ntasks"]),
            memory_gb=_positive_int_env("NHMS_PRODUCTION_SLURM_MEMORY_GB", 8, maximum=RESOURCE_LIMITS["memory_gb"]),
            cpus_per_task=_positive_int_env(
                "NHMS_PRODUCTION_SLURM_CPUS_PER_TASK",
                2,
                maximum=RESOURCE_LIMITS["cpus_per_task"],
            ),
            shud_threads=_positive_int_env(
                shud_threads_env_name,
                2,
                maximum=RESOURCE_LIMITS["shud_threads"],
            ),
            max_concurrent=_positive_int_env(
                "NHMS_PRODUCTION_SLURM_MAX_CONCURRENT",
                2,
                maximum=RESOURCE_LIMITS["max_concurrent"],
            ),
            submit=submit,
            fake_slurm=fake_slurm,
            poll_interval_seconds=_poll_float_option(
                poll_interval_seconds,
                "NHMS_PRODUCTION_SLURM_POLL_INTERVAL_SECONDS",
                DEFAULT_POLL_INTERVAL_SECONDS,
                minimum=MIN_POLL_INTERVAL_SECONDS,
                maximum=MAX_POLL_INTERVAL_SECONDS,
            ),
            poll_timeout_seconds=_poll_float_option(
                poll_timeout_seconds,
                "NHMS_PRODUCTION_SLURM_POLL_TIMEOUT_SECONDS",
                DEFAULT_POLL_TIMEOUT_SECONDS,
                minimum=0.0,
                maximum=MAX_POLL_TIMEOUT_SECONDS,
            ),
            force=force,
        )
        _validate_config(config)
        return config


def validate_slurm(config: ProductionSlurmConfig) -> dict[str, Any]:
    config = replace(config, evidence_root=_safe_resolved_evidence_root(config.evidence_root))
    _validate_config(config)
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=config.force)
    writer.prepare()
    preflight = _preflight_payload(config)
    writer.write_json(config.lane_dir / "preflight.json", preflight)

    blockers = _preflight_blockers(config)
    use_shared_workspace_inputs = _uses_shared_workspace_inputs(config) and not blockers
    manifest_index, manifest_tasks = _write_manifest_index(
        config,
        writer,
        use_shared_workspace_inputs=use_shared_workspace_inputs,
    )
    rendered_script = _render_production_template(config, manifest_index, writer)
    writer.write_text(config.lane_dir / "rendered_run_shud_forecast_array.sbatch", rendered_script)

    accounting = _fake_accounting(config) if config.fake_slurm else _real_accounting(config, blockers)
    if accounting.get("shared_runtime_inputs_cleaned") is True:
        manifest_index, manifest_tasks = _write_manifest_index(
            config,
            writer,
            use_shared_workspace_inputs=False,
        )
    writer.write_json(config.lane_dir / "slurm_accounting.json", accounting)

    partial_success = _partial_success_evidence(config, accounting)
    writer.write_json(config.lane_dir / "array_partial_success.json", partial_success)

    retry_cancel = _retry_cancel_evidence(config, partial_success, accounting)
    writer.write_json(config.lane_dir / "retry_cancel.json", retry_cancel)

    qc = _qc_blocking_evidence(config, partial_success, accounting)
    writer.write_json(config.lane_dir / "qc_blocking.json", qc)

    metadata = _environment_metadata(config)
    writer.write_json(config.lane_dir / "environment.json", metadata)

    accounting_blockers = accounting.get("blockers") if isinstance(accounting.get("blockers"), list) else []
    all_blockers = [*blockers, *accounting_blockers]
    status = "ready"
    if all_blockers:
        status = "blocked"
    elif config.submit:
        status = "submitted"
    summary = {
        "schema": "nhms.production_closure.slurm.v1",
        "issue": 147,
        "run_id": config.run_id,
        "status": status,
        "evidence_dir": str(config.lane_dir),
        "blockers": all_blockers,
        "files": [
            "preflight.json",
            "rendered_run_shud_forecast_array.sbatch",
            "manifest_index.json",
            "slurm_accounting.json",
            "array_partial_success.json",
            "retry_cancel.json",
            "qc_blocking.json",
            "environment.json",
        ],
        "manifest_index_path": str(manifest_index),
        "runtime_manifest_paths": [
            task.get("manifest_path")
            for task in manifest_tasks
        ],
    }
    writer.write_json(config.lane_dir / "summary.json", summary)
    return summary


def _preflight_payload(config: ProductionSlurmConfig) -> dict[str, Any]:
    return redact_payload(
        {
            "schema": "nhms.production_closure.slurm.preflight.v1",
            "run_id": config.run_id,
            "cluster": config.cluster,
            "account": config.account,
            "partition": config.partition,
            "shared_workspace_root": str(config.workspace_root),
            "solver_binary": config.solver_binary,
            "solver_module": config.solver_module,
            "selected_model": config.model_id,
            "model_package_uri": config.model_package_uri,
            "walltime": config.walltime,
            "resources": {
                "nodes": config.nodes,
                "ntasks": config.ntasks,
                "cpus_per_task": config.cpus_per_task,
                "shud_threads": config.shud_threads,
                "omp_num_threads": config.shud_threads,
                "memory_gb": config.memory_gb,
                "max_concurrent": config.max_concurrent,
            },
            "object_store": {
                "root": config.object_store_root,
                "prefix": config.object_store_prefix,
            },
            "evidence_root": str(config.evidence_root),
            "submit": config.submit,
            "fake_slurm": config.fake_slurm,
        }
    )


def _safe_run_id(run_id: str) -> str:
    if SAFE_RUN_ID_RE.fullmatch(run_id):
        return run_id
    raise ProductionValidationError(
        "PRODUCTION_SLURM_RUN_ID_UNSAFE",
        "run_id may contain only alphanumeric characters, underscores, and hyphens.",
    )


def _validate_config(config: ProductionSlurmConfig) -> None:
    if config.submit and config.fake_slurm:
        raise ProductionValidationError(
            "PRODUCTION_SLURM_SUBMIT_FAKE_CONFLICT",
            "--submit and --fake-slurm are mutually exclusive.",
        )
    _validate_slurm_identifier(config.partition, "NHMS_PRODUCTION_SLURM_PARTITION")
    _validate_slurm_identifier(config.account, "NHMS_PRODUCTION_SLURM_ACCOUNT", allow_empty=True)
    _validate_walltime(config.walltime)
    for field_name, maximum in RESOURCE_LIMITS.items():
        value = getattr(config, field_name)
        if not isinstance(value, int) or value < 1 or value > maximum:
            raise ProductionValidationError(
                "PRODUCTION_SLURM_RESOURCE_INVALID",
                f"{field_name} must be an integer between 1 and {maximum}.",
            )


def _validate_slurm_identifier(value: str, env_name: str, *, allow_empty: bool = False) -> None:
    if value == "" and allow_empty:
        return
    if value == "":
        return
    if SAFE_SLURM_IDENTIFIER_RE.fullmatch(value):
        return
    raise ProductionValidationError(
        "PRODUCTION_SLURM_RESOURCE_INVALID",
        f"{env_name} must be a safe Slurm identifier.",
    )


def _validate_walltime(value: str) -> None:
    match = SLURM_WALLTIME_RE.fullmatch(value)
    if not match:
        raise ProductionValidationError(
            "PRODUCTION_SLURM_RESOURCE_INVALID",
            "NHMS_PRODUCTION_SLURM_WALLTIME must use [days-]HH:MM:SS with minute/second fields below 60.",
        )
    days = int(match.group("days") or 0)
    hours = int(match.group("hours"))
    if days == 0 and hours > 999:
        raise ProductionValidationError(
            "PRODUCTION_SLURM_RESOURCE_INVALID",
            "NHMS_PRODUCTION_SLURM_WALLTIME hours must be bounded.",
        )


def _object_store_env_value(production_name: str, generic_name: str, default: str) -> str:
    production_value = os.getenv(production_name)
    if production_value is not None:
        return production_value
    return os.getenv(generic_name, default)


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
            raise ProductionValidationError(
                "PRODUCTION_SLURM_EVIDENCE_SYMLINK",
                f"Evidence path component must not be a symlink: {current}",
            )


def _positive_int_env(env_name: str, default: int, *, maximum: int) -> int:
    raw_value = os.getenv(env_name)
    if raw_value is None or raw_value == "":
        return default
    try:
        resolved = int(raw_value, 10)
    except ValueError as error:
        raise ProductionValidationError(
            "PRODUCTION_SLURM_RESOURCE_INVALID",
            f"{env_name} must be an integer between 1 and {maximum}.",
        ) from error
    if str(resolved) != raw_value.strip() or resolved < 1 or resolved > maximum:
        raise ProductionValidationError(
            "PRODUCTION_SLURM_RESOURCE_INVALID",
            f"{env_name} must be an integer between 1 and {maximum}.",
        )
    return resolved


def _poll_float_option(
    value: float | None,
    env_name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw_value = os.getenv(env_name) if value is None else value
    if raw_value is None or raw_value == "":
        return default
    try:
        resolved = float(raw_value)
    except (TypeError, ValueError) as error:
        raise ProductionValidationError(
            "PRODUCTION_SLURM_POLL_OPTION_INVALID",
            f"{env_name} must be a finite number between {minimum:g} and {maximum:g}.",
        ) from error
    if not math.isfinite(resolved) or resolved < minimum:
        raise ProductionValidationError(
            "PRODUCTION_SLURM_POLL_OPTION_INVALID",
            f"{env_name} must be a finite number between {minimum:g} and {maximum:g}.",
        )
    if resolved > maximum:
        raise ProductionValidationError(
            "PRODUCTION_SLURM_POLL_OPTION_INVALID",
            f"{env_name} must be less than or equal to {maximum:g}.",
        )
    return resolved


def _preflight_blockers(config: ProductionSlurmConfig) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    required = {
        "NHMS_PRODUCTION_SLURM_CLUSTER": config.cluster,
        "NHMS_PRODUCTION_SLURM_ACCOUNT": config.account,
        "NHMS_PRODUCTION_SLURM_PARTITION": config.partition,
        "NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI": config.model_package_uri,
    }
    for name, value in required.items():
        if not value:
            blockers.append({"error_code": "PRODUCTION_SLURM_PREFLIGHT_MISSING", "field": name})
    if config.submit and not config.fake_slurm:
        for command in ("sinfo", "squeue", "sacct", "scontrol", "sbatch", "scancel"):
            if shutil.which(command) is None:
                blockers.append({"error_code": "SLURM_CLI_MISSING", "field": command})
    return blockers


def _write_manifest_index(
    config: ProductionSlurmConfig,
    writer: EvidenceWriter,
    *,
    use_shared_workspace_inputs: bool,
) -> tuple[Path, list[dict[str, Any]]]:
    manifest_index = _manifest_index_path(config, use_shared_workspace_inputs=use_shared_workspace_inputs)
    tasks = _manifest_index_tasks(config, use_shared_workspace_inputs=use_shared_workspace_inputs)
    for task in tasks:
        _write_runtime_manifest(config, task, writer)
    if use_shared_workspace_inputs:
        writer.write_runtime_manifest_json(manifest_index, redact_payload(tasks))
        writer.write_json(config.lane_dir / "manifest_index.json", redact_payload(tasks))
    else:
        writer.write_json(manifest_index, redact_payload(tasks))
    return manifest_index, tasks


def _manifest_index_tasks(
    config: ProductionSlurmConfig,
    *,
    use_shared_workspace_inputs: bool,
) -> list[dict[str, Any]]:
    return [
        _task_manifest(
            config,
            task_id=0,
            run_id=f"{config.run_id}_success",
            model_id=config.model_id,
            use_shared_workspace_inputs=use_shared_workspace_inputs,
        ),
        _task_manifest(
            config,
            task_id=1,
            run_id=f"{config.run_id}_controlled_fail",
            model_id=f"{config.model_id}_fail",
            use_shared_workspace_inputs=use_shared_workspace_inputs,
        ),
    ]


def _manifest_index_path(config: ProductionSlurmConfig, *, use_shared_workspace_inputs: bool) -> Path:
    if use_shared_workspace_inputs:
        return config.workspace_root / "runs" / config.run_id / "input" / "manifest_index.json"
    return config.lane_dir / "manifest_index.json"


def _uses_shared_workspace_inputs(config: ProductionSlurmConfig) -> bool:
    return config.submit and not config.fake_slurm


def _task_manifest(
    config: ProductionSlurmConfig,
    *,
    task_id: int,
    run_id: str,
    model_id: str,
    use_shared_workspace_inputs: bool,
) -> dict[str, Any]:
    input_root = config.workspace_root if use_shared_workspace_inputs else config.lane_dir
    output_uri = _object_uri(config, f"runs/{run_id}/output/")
    log_uri = _object_uri(config, f"runs/{run_id}/logs/")
    return {
        "task_id": task_id,
        "run_id": run_id,
        "model_id": model_id,
        "basin_version_id": "basin_v1",
        "river_network_version_id": "rnv_v1",
        "source_id": "GFS",
        "cycle_time": "2026-05-16T00:00:00Z",
        "workspace_dir": str(config.workspace_root),
        "manifest_path": str(input_root / "runs" / run_id / "input" / "manifest.json"),
        "model_package_uri": config.model_package_uri,
        "forcing_uri": _object_uri(config, f"forcing/gfs/2026051600/basin_v1/{model_id}/"),
        "output_uri": output_uri,
        "log_uri": log_uri,
        "expected_outcome": "succeeded" if task_id == 0 else "controlled_failure",
    }


def _write_runtime_manifest(config: ProductionSlurmConfig, task: dict[str, Any], writer: EvidenceWriter) -> Path:
    run_id = str(task["run_id"])
    manifest_path = Path(str(task["manifest_path"]))
    runtime_manifest = {
        "run_id": run_id,
        "run_type": "forecast",
        "scenario_id": "production_closure_validation",
        "source_id": task["source_id"],
        "cycle_time": task["cycle_time"],
        "start_time": "2026-05-16T00:00:00Z",
        "end_time": "2026-05-16T06:00:00Z",
        "forecast_horizon_hours": 6,
        "workspace_dir": str(config.workspace_root),
        "object_store_root": config.object_store_root,
        "object_store_prefix": config.object_store_prefix,
        "model": {
            "model_id": task["model_id"],
            "basin_id": "basin_v1",
            "basin_version_id": task["basin_version_id"],
            "river_network_version_id": task["river_network_version_id"],
            "model_package_uri": task["model_package_uri"],
            "project_name": "production_closure",
            "segment_count": 1,
        },
        "forcing": {
            "forcing_version_id": f"forc_{run_id}",
            "forcing_uri": task["forcing_uri"],
        },
        "initial_state": {
            "state_id": None,
            "ic_file_uri": None,
            "valid_time": None,
            "checksum": None,
            "quality": "cold_start_no_state",
        },
        "runtime": {
            "output_interval_minutes": 60,
            "init_mode": 1,
            "external_shud_execution": "opt_in_required",
            "validation_expected_outcome": task["expected_outcome"],
        },
        "outputs": {
            "run_manifest_uri": _object_uri(config, f"runs/{run_id}/input/manifest.json"),
            "output_uri": task["output_uri"],
            "log_uri": task["log_uri"],
        },
    }
    writer.write_runtime_manifest_json(manifest_path, runtime_manifest)
    return manifest_path


def _object_uri(config: ProductionSlurmConfig, key: str) -> str:
    prefix = _sanitized_object_store_prefix(config.object_store_prefix)
    if prefix:
        return f"{prefix.rstrip('/')}/{key.lstrip('/')}"
    return key.lstrip("/")


def _sanitized_object_store_prefix(prefix: str) -> str:
    raw_prefix = SENSITIVE_URI_ASSIGNMENT_RE.sub("", prefix.strip())
    try:
        parsed = urlsplit(raw_prefix)
    except ValueError:
        return raw_prefix.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0].strip("/")

    sanitized_path = parsed.path.rstrip("/")
    if parsed.scheme and parsed.netloc:
        hostname = parsed.hostname or ""
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, sanitized_path, "", ""))
    if parsed.netloc:
        hostname = parsed.hostname or ""
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit(("", netloc, sanitized_path, "", "")).rstrip("/")
    return sanitized_path.strip("/")


def _render_production_template(config: ProductionSlurmConfig, manifest_index: Path, writer: EvidenceWriter) -> str:
    profile_path = config.lane_dir / "resource_profiles.yaml"
    writer.write_text(
        profile_path,
        "\n".join(
            [
                "resource_profiles:",
                "  default:",
                f"    partition: {json.dumps(config.partition or 'compute')}",
                f"    nodes: {config.nodes}",
                f"    ntasks: {config.ntasks}",
                f"    cpus_per_task: {config.cpus_per_task}",
                f"    memory_gb: {config.memory_gb}",
                f"    walltime: {json.dumps(config.walltime)}",
                f"    max_concurrent: {config.max_concurrent}",
                f"    shud_threads: {config.shud_threads}",
                "  overrides: {}",
                "",
            ]
        ),
    )
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(profile_path),
            workspace_dir=str(config.workspace_root),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        )
    )
    manifest = {
        "run_id": config.run_id,
        "model_id": config.model_id,
        "job_type": "run_shud_forecast_array",
        "stage_name": "run_shud_forecast_array",
        "cycle_id": f"{config.run_id}_cycle",
        "account": config.account,
        "manifest_index_path": str(manifest_index),
        "workspace_dir": str(config.workspace_root),
        "object_store_root": config.object_store_root,
        "object_store_prefix": config.object_store_prefix,
        "model_package_uri": config.model_package_uri,
        "controlled_failure_log_marker": CONTROLLED_FAILURE_LOG_MARKER,
    }
    return gateway.render_template("run_shud_forecast_array", manifest, str(manifest_index))


def _real_accounting(config: ProductionSlurmConfig, blockers: list[dict[str, str]]) -> dict[str, Any]:
    if blockers or not config.submit:
        return {
            "mode": "blocked" if blockers else "not_submitted",
            "blockers": blockers,
            "commands": _inspection_commands(config),
            "records": [],
        }
    _prepare_shared_log_dir(config)
    script_path = config.lane_dir / "rendered_run_shud_forecast_array.sbatch"
    array_spec = f"0-1%{max(1, min(config.max_concurrent, 2))}"
    submit_command = ["sbatch", "--parsable", f"--array={array_spec}"]
    if config.account:
        submit_command.append(f"--account={config.account}")
    submit_command.append(str(script_path))
    submit = _run_command(submit_command)
    if submit["returncode"] != 0:
        cleanup = _cleanup_shared_runtime_inputs(config)
        cleanup_blockers = _shared_runtime_cleanup_blockers(cleanup)
        return {
            "mode": "blocked",
            "job_id": None,
            "array_spec": array_spec,
            "blockers": [
                {
                    "error_code": "SBATCH_SUBMISSION_FAILED",
                    "field": "sbatch",
                    "returncode": str(submit["returncode"]),
                },
                *cleanup_blockers,
            ],
            "commands": _inspection_commands(config),
            "submit": submit,
            "inspection": {
                "sinfo": _run_command(["sinfo", "-o", "%P|%a|%l|%D|%t|%N"]),
                "squeue": _run_command(["squeue", "-u", os.getenv("USER", ""), "-o", "%i|%P|%j|%u|%T|%M|%D|%R"]),
                "scontrol": _slurm_config_inspection(),
            },
            "raw_sacct": "",
            "sacct": None,
            "poll": {
                "attempts": 0,
                "elapsed_seconds": 0.0,
                "interval_seconds": config.poll_interval_seconds,
                "timeout_seconds": config.poll_timeout_seconds,
            },
            "shared_runtime_inputs_cleaned": not cleanup_blockers,
            "shared_runtime_input_cleanup": cleanup,
            "records": [],
        }
    job_id = _parse_sbatch_parsable(submit["stdout"])
    poll = _poll_sacct_for_expected_array(config, job_id, expected_task_ids={0, 1})
    sacct = poll["last_sacct"]
    records = poll["records"]
    task_blockers = [*poll["blockers"], *_submitted_log_blockers(config, job_id, records)]
    mode = "blocked" if task_blockers else "submitted"
    return {
        "mode": mode,
        "job_id": job_id,
        "array_spec": array_spec,
        "blockers": task_blockers,
        "commands": _inspection_commands(config),
        "submit": submit,
        "inspection": {
            "sinfo": _run_command(["sinfo", "-o", "%P|%a|%l|%D|%t|%N"]),
            "squeue": _run_command(["squeue", "-u", os.getenv("USER", ""), "-o", "%i|%P|%j|%u|%T|%M|%D|%R"]),
            "scontrol": _slurm_config_inspection(),
        },
        "raw_sacct": sacct["stdout"],
        "sacct": sacct,
        "poll": {
            "attempts": poll["attempts"],
            "elapsed_seconds": poll["elapsed_seconds"],
            "interval_seconds": config.poll_interval_seconds,
            "timeout_seconds": config.poll_timeout_seconds,
        },
        "records": records,
    }


def _prepare_shared_log_dir(config: ProductionSlurmConfig) -> Path:
    log_dir = config.workspace_root / config.run_id / "logs"
    _safe_workspace_path(config.workspace_root, log_dir)
    if log_dir.exists() and not log_dir.is_dir():
        raise ProductionValidationError(
            "PRODUCTION_SLURM_LOG_DIR_INVALID",
            f"Slurm log path exists but is not a directory: {log_dir}",
        )
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ProductionValidationError(
            "PRODUCTION_SLURM_LOG_DIR_INVALID",
            f"Failed to create Slurm log directory {log_dir}: {error}",
        ) from error
    return log_dir


def _safe_workspace_path(workspace_root: Path, path: Path) -> Path:
    root = workspace_root.expanduser()
    if root.exists() or root.is_symlink():
        _refuse_symlink_components(root)
    parent = root.parent
    if parent.exists() or parent.is_symlink():
        _refuse_symlink_components(parent)
    if path.exists() or path.is_symlink():
        _refuse_symlink_components(path)
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as error:
        raise ProductionValidationError(
            "PRODUCTION_SLURM_WORKSPACE_PATH_UNSAFE",
            "Shared Slurm workspace path must stay under workspace root.",
        ) from error
    return resolved_path


def _cleanup_shared_runtime_inputs(config: ProductionSlurmConfig) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for path in _shared_runtime_input_paths(config):
        try:
            safe_path = _safe_workspace_path(config.workspace_root, path)
        except ProductionValidationError as error:
            results.append({"path": str(path), "status": "unsafe", "error_code": error.error_code})
            continue
        try:
            safe_path.unlink(missing_ok=True)
        except OSError as error:
            results.append({"path": str(safe_path), "status": "failed", "error": str(error)})
            continue
        results.append({"path": str(safe_path), "status": "absent" if not safe_path.exists() else "failed"})
    return results


def _shared_runtime_cleanup_blockers(cleanup: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "error_code": "PRODUCTION_SLURM_SHARED_INPUT_CLEANUP_FAILED",
            "field": "workspace",
            "path": item["path"],
        }
        for item in cleanup
        if item.get("status") != "absent"
    ]


def _shared_runtime_input_paths(config: ProductionSlurmConfig) -> list[Path]:
    return [
        config.workspace_root / "runs" / config.run_id / "input" / "manifest_index.json",
        config.workspace_root / "runs" / f"{config.run_id}_success" / "input" / "manifest.json",
        config.workspace_root / "runs" / f"{config.run_id}_controlled_fail" / "input" / "manifest.json",
    ]


def _poll_sacct_for_expected_array(
    config: ProductionSlurmConfig,
    job_id: str,
    *,
    expected_task_ids: set[int],
) -> dict[str, Any]:
    command = [
        "sacct",
        "-j",
        job_id,
        "--format=JobID,State,ExitCode,Elapsed,NodeList,Partition",
        "-P",
        "--noheader",
    ]
    started = time.monotonic()
    deadline = started + config.poll_timeout_seconds
    attempts = 0
    last_sacct: dict[str, Any] | None = None
    last_records: list[dict[str, Any]] = []
    last_blockers: list[dict[str, Any]] = []

    while True:
        attempts += 1
        last_sacct = _run_command(command)
        if last_sacct["returncode"] == 0:
            last_records = parse_sacct_evidence(last_sacct["stdout"])
            last_blockers = _submitted_task_outcome_blockers(last_records, expected_task_ids=expected_task_ids)
            if not last_blockers:
                break
        else:
            last_records = []
            last_blockers = [
                {
                    "error_code": "SLURM_ARRAY_TASK_ACCOUNTING_UNAVAILABLE",
                    "field": "sacct",
                    "returncode": str(last_sacct["returncode"]),
                }
            ]

        now = time.monotonic()
        if now >= deadline:
            last_blockers = _timeout_blockers(last_blockers)
            break
        time.sleep(min(config.poll_interval_seconds, max(0.0, deadline - now)))

    return {
        "attempts": attempts,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "last_sacct": last_sacct or _run_command(command),
        "records": last_records,
        "blockers": last_blockers,
    }


def _fake_accounting(config: ProductionSlurmConfig) -> dict[str, Any]:
    raw_rows = "\n".join(
        [
            "9001|COMPLETED|0:0|00:02:31|cn04|CPU",
            "9001_0|COMPLETED|0:0|00:02:29|cn04|CPU",
            "9001_1|FAILED|2:0|00:00:17|cn04|CPU",
            "9002|CANCELLED|0:0|00:00:04|cn04|CPU",
        ]
    )
    return {
        "mode": "fake",
        "commands": _inspection_commands(config),
        "raw_sacct": raw_rows,
        "records": parse_sacct_evidence(raw_rows),
    }


def parse_sacct_evidence(stdout: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        fields = raw_line.rstrip("\n").split("|")
        if len(fields) != 6:
            raise ProductionValidationError("SACCT_EVIDENCE_INVALID", "Expected 6 sacct fields.")
        job_id, state, exit_code, elapsed, node_list, partition = fields
        normalized_state = _normalize_slurm_state(state)
        task_id = None
        if "_" in job_id and job_id.rsplit("_", 1)[1].isdigit():
            task_id = int(job_id.rsplit("_", 1)[1])
        records.append(
            {
                "job_id": job_id,
                "task_id": task_id,
                "state": normalized_state,
                "exit_code": _parse_exit_code(exit_code),
                "elapsed": elapsed,
                "node_list": node_list,
                "partition": partition,
                "error_code": (
                    map_slurm_error_code(normalized_state)
                    if normalized_state not in {"COMPLETED", "CANCELLED"}
                    else None
                ),
            }
        )
    return records


def _submitted_task_outcome_blockers(
    records: list[dict[str, Any]],
    *,
    expected_task_ids: set[int],
) -> list[dict[str, Any]]:
    task_records = {int(record["task_id"]): record for record in records if record.get("task_id") is not None}
    observed = set(task_records)
    missing = sorted(expected_task_ids - observed)
    blockers: list[dict[str, Any]] = []
    if missing:
        blockers.append(
            {
                "error_code": "SLURM_ARRAY_TASK_ACCOUNTING_MISSING",
                "field": "sacct",
                "missing_task_ids": ",".join(str(task_id) for task_id in missing),
            }
        )

    unfinished = sorted(
        task_id
        for task_id, record in task_records.items()
        if task_id in expected_task_ids and str(record.get("state", "")) not in TERMINAL_SLURM_STATES
    )
    if unfinished:
        blockers.append(
            {
                "error_code": "SLURM_ARRAY_TASK_ACCOUNTING_UNFINISHED",
                "field": "sacct",
                "unfinished_task_ids": ",".join(str(task_id) for task_id in unfinished),
            }
        )

    task0 = task_records.get(0)
    if (
        task0
        and task0.get("state") in TERMINAL_SLURM_STATES
        and (task0.get("state") != "COMPLETED" or task0.get("exit_code") != 0)
    ):
        blockers.append(
            {
                "error_code": "SLURM_ARRAY_TASK_SUCCESS_CONTRACT_FAILED",
                "field": "sacct",
                "task_id": "0",
                "state": str(task0.get("state", "")),
                "exit_code": str(task0.get("exit_code")),
            }
        )

    task1 = task_records.get(1)
    if task1 and not _is_controlled_failure_outcome(task1):
        blockers.append(
            {
                "error_code": "SLURM_ARRAY_TASK_CONTROLLED_FAILURE_MISSING",
                "field": "sacct",
                "task_id": "1",
                "state": str(task1.get("state", "")),
                "exit_code": str(task1.get("exit_code")),
            }
        )
    return blockers


def _submitted_log_blockers(
    config: ProductionSlurmConfig,
    job_id: str,
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    task_records = {int(record["task_id"]): record for record in records if record.get("task_id") is not None}
    blockers: list[dict[str, Any]] = []
    for task_id in (0, 1):
        record = task_records.get(task_id)
        if not record or str(record.get("state", "")) not in TERMINAL_SLURM_STATES:
            continue
        for suffix in ("out", "err"):
            path = _slurm_log_file(config, job_id, task_id, suffix=suffix)
            if not path.is_file():
                blockers.append(
                    {
                        "error_code": "SLURM_ARRAY_TASK_LOG_MISSING",
                        "field": f"task_{task_id}_{suffix}",
                        "task_id": str(task_id),
                        "path": str(path),
                    }
                )
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    handle.read(1)
            except OSError:
                blockers.append(
                    {
                        "error_code": "SLURM_ARRAY_TASK_LOG_UNREADABLE",
                        "field": f"task_{task_id}_{suffix}",
                        "task_id": str(task_id),
                        "path": str(path),
                    }
                )
    task1 = task_records.get(1)
    if task1 and _is_controlled_failure_outcome(task1) and not _controlled_failure_log_evidence_present(config, job_id):
        blockers.append(
            {
                "error_code": "SLURM_ARRAY_TASK_CONTROLLED_FAILURE_MARKER_MISSING",
                "field": "task_1_log",
                "task_id": "1",
                "marker": CONTROLLED_FAILURE_LOG_MARKER,
            }
        )
    return blockers


def _controlled_failure_log_evidence_present(config: ProductionSlurmConfig, job_id: str) -> bool:
    task1_logs = _read_task_logs(config, job_id, task_id=1)
    if not task1_logs:
        return False
    joined_logs = "\n".join(task1_logs)
    return CONTROLLED_FAILURE_LOG_MARKER in joined_logs and any(
        signature in joined_logs for signature in CONTROLLED_FAILURE_LOG_SIGNATURES
    )


def _controlled_failure_marker_present(config: ProductionSlurmConfig, job_id: str) -> bool:
    return any(CONTROLLED_FAILURE_LOG_MARKER in content for content in _read_task_logs(config, job_id, task_id=1))


def _read_task_logs(config: ProductionSlurmConfig, job_id: str, *, task_id: int) -> list[str]:
    contents: list[str] = []
    for suffix in ("out", "err"):
        path = _slurm_log_file(config, job_id, task_id, suffix=suffix)
        if not path.is_file():
            continue
        try:
            contents.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return contents


def _timeout_blockers(blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not blockers:
        return [
            {
                "error_code": "SLURM_ARRAY_TASK_ACCOUNTING_TIMEOUT",
                "field": "sacct",
            }
        ]
    return [
        {
            **blocker,
            "timeout": "true",
        }
        for blocker in blockers
    ]


def _normalize_slurm_state(state: str) -> str:
    return state.strip().upper().split(maxsplit=1)[0]


def _is_controlled_failure_outcome(record: dict[str, Any]) -> bool:
    state = str(record.get("state", ""))
    exit_code = record.get("exit_code")
    return state == "FAILED" and exit_code not in (0, None)


def _submitted_task_row_blockers(
    records: list[dict[str, Any]],
    *,
    expected_task_ids: set[int],
) -> list[dict[str, Any]]:
    observed = {int(record["task_id"]) for record in records if record.get("task_id") is not None}
    missing = sorted(expected_task_ids - observed)
    if not missing:
        return []
    return [
        {
            "error_code": "SLURM_ARRAY_TASK_ACCOUNTING_MISSING",
            "field": "sacct",
            "missing_task_ids": ",".join(str(task_id) for task_id in missing),
        }
    ]


def _slurm_config_inspection() -> dict[str, Any]:
    result = _run_command(["scontrol", "show", "config"])
    result["stdout"] = _allowlisted_slurm_config(str(result.get("stdout", "")))
    return result


def _allowlisted_slurm_config(stdout: str) -> str:
    allowlist = {
        "AccountingStorageType",
        "ClusterName",
        "JobAcctGatherType",
        "SelectType",
        "SlurmctldHost",
    }
    lines: list[str] = []
    for raw_line in stdout.splitlines():
        stripped = raw_line.strip()
        if not stripped or "=" not in stripped:
            continue
        key, value = stripped.split("=", maxsplit=1)
        key = key.strip()
        if key in allowlist:
            lines.append(f"{key} = {redact_text(value.strip())}")
    return "\n".join(lines) + ("\n" if lines else "")


def _partial_success_evidence(config: ProductionSlurmConfig, accounting: dict[str, Any]) -> dict[str, Any]:
    records = accounting.get("records") if isinstance(accounting.get("records"), list) else []
    task_records = [record for record in records if record.get("task_id") is not None]
    blockers = accounting.get("blockers") if isinstance(accounting.get("blockers"), list) else []
    mode = str(accounting.get("mode", ""))
    if mode == "not_submitted":
        return _blocked_partial_success(
            config,
            [{"error_code": "PRODUCTION_SLURM_PREFLIGHT_ONLY", "field": "submit"}],
            status="preflight_only",
        )
    if not task_records and mode != "fake":
        return _blocked_partial_success(
            config,
            blockers or [{"error_code": "SLURM_ARRAY_TASK_ACCOUNTING_MISSING", "field": "sacct"}],
        )
    if accounting.get("mode") == "blocked" and _has_incomplete_accounting_blocker(blockers):
        return _blocked_partial_success(config, blockers)
    if not task_records:
        if accounting.get("mode") == "blocked":
            return _blocked_partial_success(config, blockers)
        task_records = _planned_task_records()
    tasks = []
    for record in task_records:
        task_id = int(record["task_id"])
        log_blocked = _has_log_blocker(blockers, task_id)
        succeeded = (
            str(record.get("state", "")).upper() == "COMPLETED"
            and record.get("exit_code") == 0
            and not log_blocked
        )
        if task_id == 1 and _has_blocker(blockers, "SLURM_ARRAY_TASK_CONTROLLED_FAILURE_MISSING"):
            succeeded = False
            task_error_code = "SLURM_ARRAY_TASK_CONTROLLED_FAILURE_MISSING"
        elif log_blocked:
            task_error_code = _task_log_error_code(blockers, task_id)
        else:
            task_error_code = None if succeeded else record.get("error_code") or "SLURM_JOB_FAILED"
        log_verified = bool(mode == "fake" or (accounting.get("job_id") and not log_blocked))
        tasks.append(
            {
                "task_id": task_id,
                "job_id": record.get("job_id"),
                "run_id": f"{config.run_id}_{'success' if task_id == 0 else 'controlled_fail'}",
                "publishable": succeeded,
                "status": "succeeded" if succeeded else "blocked",
                "error_code": task_error_code,
                "stdout_path": _slurm_log_path(config, accounting, task_id, suffix="out")
                if accounting.get("job_id")
                else None,
                "stderr_path": _slurm_log_path(config, accounting, task_id, suffix="err")
                if accounting.get("job_id")
                else None,
                "log_verified": log_verified,
                "log_status": "verified" if log_verified else "blocked" if log_blocked else "unverified",
                "retry_count": 0 if succeeded else 1,
                "failure_stage": None if succeeded else "run_shud_forecast_array",
            }
        )
    return {
        "schema": "nhms.production_closure.slurm.partial_success.v1",
        "array_job_id": str(accounting.get("job_id") or "9001"),
        "successful_outputs_remain_publishable": any(task["publishable"] for task in tasks),
        "failed_outputs_blocked": any(not task["publishable"] for task in tasks),
        "tasks": tasks,
    }


def _planned_task_records() -> list[dict[str, Any]]:
    return [
        {
            "job_id": "pending_0",
            "task_id": 0,
            "state": "COMPLETED",
            "exit_code": 0,
            "error_code": None,
        },
        {
            "job_id": "pending_1",
            "task_id": 1,
            "state": "FAILED",
            "exit_code": 2,
            "error_code": "SLURM_JOB_FAILED",
        },
    ]


def _has_incomplete_accounting_blocker(blockers: list[dict[str, Any]]) -> bool:
    incomplete_codes = {
        "SLURM_ARRAY_TASK_ACCOUNTING_MISSING",
        "SLURM_ARRAY_TASK_ACCOUNTING_UNFINISHED",
        "SLURM_ARRAY_TASK_ACCOUNTING_TIMEOUT",
        "SLURM_ARRAY_TASK_ACCOUNTING_UNAVAILABLE",
    }
    return any(blocker.get("error_code") in incomplete_codes for blocker in blockers)


def _has_blocker(blockers: list[dict[str, Any]], error_code: str) -> bool:
    return any(blocker.get("error_code") == error_code for blocker in blockers)


def _has_log_blocker(blockers: list[dict[str, Any]], task_id: int) -> bool:
    log_codes = {
        "SLURM_ARRAY_TASK_LOG_MISSING",
        "SLURM_ARRAY_TASK_LOG_UNREADABLE",
        "SLURM_ARRAY_TASK_CONTROLLED_FAILURE_MARKER_MISSING",
    }
    return any(
        blocker.get("error_code") in log_codes and blocker.get("task_id") == str(task_id)
        for blocker in blockers
    )


def _task_log_error_code(blockers: list[dict[str, Any]], task_id: int) -> str:
    for blocker in blockers:
        if blocker.get("task_id") == str(task_id) and str(blocker.get("error_code", "")).startswith(
            "SLURM_ARRAY_TASK_"
        ):
            return str(blocker["error_code"])
    return "SLURM_ARRAY_TASK_LOG_MISSING"


def _slurm_log_path(config: ProductionSlurmConfig, accounting: dict[str, Any], task_id: int, *, suffix: str) -> str:
    array_job_id = str(accounting.get("job_id") or "9001")
    return str(_slurm_log_file(config, array_job_id, task_id, suffix=suffix))


def _slurm_log_file(config: ProductionSlurmConfig, array_job_id: str, task_id: int, *, suffix: str) -> Path:
    return config.workspace_root / config.run_id / "logs" / f"{array_job_id}_{task_id}.{suffix}"


def _blocked_partial_success(
    config: ProductionSlurmConfig,
    blockers: list[dict[str, Any]],
    *,
    status: str = "blocked",
) -> dict[str, Any]:
    error_code = str(blockers[0].get("error_code") if blockers else "SLURM_ARRAY_TASK_ACCOUNTING_MISSING")
    return {
        "schema": "nhms.production_closure.slurm.partial_success.v1",
        "array_job_id": None,
        "status": status,
        "error_code": error_code,
        "successful_outputs_remain_publishable": False,
        "failed_outputs_blocked": True,
        "tasks": [
            {
                "task_id": task_id,
                "job_id": None,
                "run_id": f"{config.run_id}_{'success' if task_id == 0 else 'controlled_fail'}",
                "publishable": False,
                "status": status,
                "error_code": error_code,
                "stdout_path": None,
                "stderr_path": None,
                "log_verified": False,
                "log_status": "blocked",
                "retry_count": 0,
                "failure_stage": "run_shud_forecast_array",
            }
            for task_id in (0, 1)
        ],
    }


def _retry_cancel_evidence(
    config: ProductionSlurmConfig,
    partial_success: dict[str, Any],
    accounting: dict[str, Any],
) -> dict[str, Any]:
    failed = next((task for task in partial_success["tasks"] if not task["publishable"]), None)
    success = next((task for task in partial_success["tasks"] if task["publishable"]), None)
    error_code = failed.get("error_code") if failed else "SLURM_JOB_FAILED"
    retry_count = failed.get("retry_count") if failed else 0
    cancel_blocked = not config.fake_slurm
    return {
        "schema": "nhms.production_closure.slurm.retry_cancel.v1",
        "retry": {
            "status": "not_executed" if cancel_blocked else "planned",
            "task_id": failed.get("task_id") if failed else None,
            "previous_error": error_code,
            "transient": is_transient_error(str(error_code)),
            "retry_count": retry_count,
            "backoff_seconds": compute_backoff_seconds(0),
            "successful_task_unchanged": success.get("task_id") if success else None,
        },
        "cancel": {
            "requested": False if cancel_blocked else True,
            "state": "not_executed" if cancel_blocked else "cancelled",
            "job_id": None if cancel_blocked else "9002",
            "successful_outputs_mutated": False,
            "error_code": "SLURM_RETRY_CANCEL_NOT_EXECUTED" if cancel_blocked else None,
            "array_job_id": accounting.get("job_id"),
        },
    }


def _qc_blocking_evidence(
    config: ProductionSlurmConfig,
    partial_success: dict[str, Any],
    accounting: dict[str, Any],
) -> dict[str, Any]:
    success = next((task for task in partial_success["tasks"] if task["publishable"]), None)
    mode = str(accounting.get("mode", ""))
    blockers = accounting.get("blockers") if isinstance(accounting.get("blockers"), list) else []
    evidence_verified = mode == "fake" or (
        mode == "submitted"
        and not _has_blocker(blockers, "SLURM_ARRAY_TASK_CONTROLLED_FAILURE_MARKER_MISSING")
        and _controlled_failure_marker_present(config, str(accounting.get("job_id") or ""))
    )
    malformed_status = "blocked" if evidence_verified else "not_verified"
    return {
        "schema": "nhms.production_closure.slurm.qc_blocking.v1",
        "malformed_task": {
            "task_id": 1,
            "run_id": f"{config.run_id}_controlled_fail",
            "status": malformed_status,
            "error_code": "NON_FINITE_FLOW" if evidence_verified else "QC_BLOCKING_NOT_VERIFIED",
            "evidence_verified": evidence_verified,
            "marker": CONTROLLED_FAILURE_LOG_MARKER,
            "failure_stage": "parse_output_array",
            "publication_blocked": evidence_verified,
            "frequency_blocked": evidence_verified,
            "tile_blocked": evidence_verified,
            "api_publication_blocked": evidence_verified,
        },
        "sibling_success": {
            "task_id": success.get("task_id") if success else 0,
            "run_id": success.get("run_id") if success else f"{config.run_id}_success",
            "publishable": bool(success),
        },
    }


def _environment_metadata(config: ProductionSlurmConfig) -> dict[str, Any]:
    keys = [
        "NHMS_PRODUCTION_SLURM_CLUSTER",
        "NHMS_PRODUCTION_SLURM_ACCOUNT",
        "NHMS_PRODUCTION_SLURM_PARTITION",
        "NHMS_PRODUCTION_SLURM_MODEL_PACKAGE_URI",
        "OBJECT_STORE_ROOT",
        "OBJECT_STORE_PREFIX",
        "SHUD_EXECUTABLE",
        "SHUD_THREADS",
        "OMP_NUM_THREADS",
        "DATABASE_URL",
        "AWS_SECRET_ACCESS_KEY",
        "CDSAPI_KEY",
    ]
    return redact_payload(
        {
            "schema": "nhms.production_closure.slurm.environment.v1",
            "run_id": config.run_id,
            "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "env": {key: os.getenv(key, "") for key in keys if key in os.environ},
            "slurm_cli": {
                name: shutil.which(name)
                for name in ("sinfo", "squeue", "sacct", "scontrol", "sbatch", "scancel")
            },
        }
    )


def _inspection_commands(config: ProductionSlurmConfig) -> list[str]:
    array_spec = f"0-1%{max(1, min(config.max_concurrent, 2))}"
    account_arg = " --account=<account>" if config.account else ""
    return [
        "sinfo -o '%P|%a|%l|%D|%t|%N'",
        'squeue -u "$USER" -o \'%i|%P|%j|%u|%T|%M|%D|%R\'',
        "sacct -j <jobid> --format=JobID,State,ExitCode,Elapsed,NodeList,Partition -P",
        "scontrol show config | allowlist ClusterName,SlurmctldHost,AccountingStorageType,JobAcctGatherType,SelectType",
        f"sbatch --parsable --array={array_spec}{account_arg} <script>",
    ]


def _parse_exit_code(raw_exit_code: str) -> int | None:
    if not raw_exit_code:
        return None
    try:
        return int(raw_exit_code.split(":", maxsplit=1)[0])
    except ValueError as error:
        raise ProductionValidationError(
            "SACCT_EXIT_CODE_INVALID",
            f"Invalid sacct exit code: {raw_exit_code}",
        ) from error


def _parse_sbatch_parsable(stdout: str) -> str:
    candidate = stdout.strip().splitlines()[0].split(";", maxsplit=1)[0] if stdout.strip() else ""
    if candidate.isdigit():
        return candidate
    raise ProductionValidationError("SBATCH_JOB_ID_INVALID", "Unable to parse job id from sbatch --parsable output.")


def _run_command(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    return redact_payload(
        {
            "argv": command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    )


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("validate-slurm")
    @click.option("--evidence-root", type=click.Path(path_type=Path), required=True)
    @click.option("--run-id")
    @click.option("--submit", is_flag=True, default=False)
    @click.option("--fake-slurm", is_flag=True, default=False)
    @click.option("--poll-interval-seconds", type=float, default=None)
    @click.option("--poll-timeout-seconds", type=float, default=None)
    @click.option("--force", is_flag=True, default=False)
    def validate_slurm_command(
        evidence_root: Path,
        run_id: str | None,
        submit: bool,
        fake_slurm: bool,
        poll_interval_seconds: float | None,
        poll_timeout_seconds: float | None,
        force: bool,
    ) -> None:
        try:
            summary = validate_slurm(
                ProductionSlurmConfig.from_env(
                    evidence_root=evidence_root,
                    run_id=run_id,
                    submit=submit,
                    fake_slurm=fake_slurm,
                    poll_interval_seconds=poll_interval_seconds,
                    poll_timeout_seconds=poll_timeout_seconds,
                    force=force,
                )
            )
            click.echo(json.dumps(redact_payload(summary), sort_keys=True))
        except (ProductionValidationError, OSError, subprocess.SubprocessError) as error:
            if isinstance(error, ProductionValidationError):
                click.echo(f"{error.error_code}: {error.message}", err=True)
            else:
                click.echo(f"PRODUCTION_SLURM_VALIDATION_FAILED: {error}", err=True)
            raise SystemExit(1) from error

    @cli.command("validate-object-store")
    @click.option("--evidence-root", type=click.Path(path_type=Path), required=True)
    @click.option("--run-id")
    @click.option("--basins-root", type=click.Path(path_type=Path), default=None)
    @click.option("--model-id", default=None)
    @click.option("--version", default=None)
    @click.option("--force", is_flag=True, default=False)
    def validate_object_store_command(
        evidence_root: Path,
        run_id: str | None,
        basins_root: Path | None,
        model_id: str | None,
        version: str | None,
        force: bool,
    ) -> None:
        try:
            summary = validate_object_store(
                ProductionObjectStoreConfig.from_env(
                    evidence_root=evidence_root,
                    run_id=run_id,
                    basins_root=basins_root,
                    model_id=model_id,
                    version=version,
                    force=force,
                )
            )
            click.echo(json.dumps(redact_payload(summary), sort_keys=True))
        except ProductionObjectStoreValidationError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error
        except Exception as error:
            to_payload = getattr(error, "to_payload", None)
            if callable(to_payload):
                click.echo(json.dumps(to_payload(), ensure_ascii=False, sort_keys=True), err=True)
            else:
                click.echo(f"PRODUCTION_OBJECT_STORE_VALIDATION_FAILED: {error}", err=True)
            raise SystemExit(1) from error

    @cli.command("validate-met")
    @click.option("--evidence-root", type=click.Path(path_type=Path), required=True)
    @click.option("--run-id")
    @click.option("--sources", default=None)
    @click.option("--cycle-start", default=None)
    @click.option("--cycle-end", default=None)
    @click.option("--forecast-hours", default=None)
    @click.option("--model-id", default=None)
    @click.option("--model-version", default=None)
    @click.option("--force", is_flag=True, default=False)
    def validate_met_command(
        evidence_root: Path,
        run_id: str | None,
        sources: str | None,
        cycle_start: str | None,
        cycle_end: str | None,
        forecast_hours: str | None,
        model_id: str | None,
        model_version: str | None,
        force: bool,
    ) -> None:
        try:
            summary = validate_met(
                ProductionMetConfig.from_env(
                    evidence_root=evidence_root,
                    run_id=run_id,
                    sources=sources,
                    cycle_start=cycle_start,
                    cycle_end=cycle_end,
                    forecast_hours=forecast_hours,
                    model_id=model_id,
                    model_version=model_version,
                    force=force,
                )
            )
            click.echo(json.dumps(redact_payload(summary), sort_keys=True))
        except ProductionMetValidationError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error
        except Exception as error:
            click.echo(f"PRODUCTION_MET_VALIDATION_FAILED: {error}", err=True)
            raise SystemExit(1) from error

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

    @cli.command("validate-scale")
    @click.option("--evidence-root", type=click.Path(path_type=Path), required=True)
    @click.option("--run-id")
    @click.option("--dataset-source", default=None)
    @click.option("--segment-count", type=int, default=None)
    @click.option("--model-count", type=int, default=None)
    @click.option("--min-segment-count", type=int, default=None)
    @click.option("--min-model-count", type=int, default=None)
    @click.option("--bbox-set", default=None)
    @click.option("--thresholds-file", type=click.Path(path_type=Path), default=None)
    @click.option("--tile-content-type-expectation", default=None)
    @click.option("--frontend-breakpoints", default=None)
    @click.option("--api-base-url", default=None)
    @click.option("--object-prefix", default=None)
    @click.option("--latency-fixture", default=None)
    @click.option("--force", is_flag=True, default=False)
    def validate_scale_command(
        evidence_root: Path,
        run_id: str | None,
        dataset_source: str | None,
        segment_count: int | None,
        model_count: int | None,
        min_segment_count: int | None,
        min_model_count: int | None,
        bbox_set: str | None,
        thresholds_file: Path | None,
        tile_content_type_expectation: str | None,
        frontend_breakpoints: str | None,
        api_base_url: str | None,
        object_prefix: str | None,
        latency_fixture: str | None,
        force: bool,
    ) -> None:
        try:
            summary = validate_scale(
                ProductionScaleConfig.from_env(
                    evidence_root=evidence_root,
                    run_id=run_id,
                    dataset_source=dataset_source,
                    segment_count=segment_count,
                    model_count=model_count,
                    min_segment_count=min_segment_count,
                    min_model_count=min_model_count,
                    bbox_set=bbox_set,
                    thresholds_file=thresholds_file,
                    tile_content_type_expectation=tile_content_type_expectation,
                    frontend_breakpoints=frontend_breakpoints,
                    api_base_url=api_base_url,
                    object_prefix=object_prefix,
                    latency_fixture=latency_fixture,
                    force=force,
                )
            )
            click.echo(json.dumps(redact_payload(summary), sort_keys=True))
        except ProductionScaleValidationError as error:
            click.echo(f"{error.error_code}: {redact_text(error.message)}", err=True)
            raise SystemExit(1) from error
        except Exception as error:
            click.echo(f"PRODUCTION_SCALE_VALIDATION_FAILED: {redact_text(str(error))}", err=True)
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
    validate_parser = subparsers.add_parser("validate-slurm")
    validate_parser.add_argument("--evidence-root", type=Path, required=True)
    validate_parser.add_argument("--run-id")
    validate_parser.add_argument("--submit", action="store_true")
    validate_parser.add_argument("--fake-slurm", action="store_true")
    validate_parser.add_argument("--poll-interval-seconds", type=float, default=None)
    validate_parser.add_argument("--poll-timeout-seconds", type=float, default=None)
    validate_parser.add_argument("--force", action="store_true")
    object_parser = subparsers.add_parser("validate-object-store")
    object_parser.add_argument("--evidence-root", type=Path, required=True)
    object_parser.add_argument("--run-id")
    object_parser.add_argument("--basins-root", type=Path, default=None)
    object_parser.add_argument("--model-id", default=None)
    object_parser.add_argument("--version", default=None)
    object_parser.add_argument("--force", action="store_true")
    met_parser = subparsers.add_parser("validate-met")
    met_parser.add_argument("--evidence-root", type=Path, required=True)
    met_parser.add_argument("--run-id")
    met_parser.add_argument("--sources", default=None)
    met_parser.add_argument("--cycle-start", default=None)
    met_parser.add_argument("--cycle-end", default=None)
    met_parser.add_argument("--forecast-hours", default=None)
    met_parser.add_argument("--model-id", default=None)
    met_parser.add_argument("--model-version", default=None)
    met_parser.add_argument("--force", action="store_true")
    e2e_parser = subparsers.add_parser("validate-e2e")
    e2e_parser.add_argument("--evidence-root", type=Path, required=True)
    e2e_parser.add_argument("--run-id")
    e2e_parser.add_argument("--source-cycle", default=None)
    e2e_parser.add_argument("--model-set", default=None)
    e2e_parser.add_argument("--db-target", default=None)
    e2e_parser.add_argument("--object-prefix", default=None)
    e2e_parser.add_argument("--slurm-partition", default=None)
    e2e_parser.add_argument("--slurm-account", default=None)
    e2e_parser.add_argument("--frontend-api-base", default=None)
    e2e_parser.add_argument("--slurm-evidence-root", type=Path, default=None)
    e2e_parser.add_argument("--object-store-evidence-root", type=Path, default=None)
    e2e_parser.add_argument("--met-evidence-root", type=Path, default=None)
    e2e_parser.add_argument("--shud-qc-fixture", default=None)
    e2e_parser.add_argument("--force", action="store_true")
    scale_parser = subparsers.add_parser("validate-scale")
    scale_parser.add_argument("--evidence-root", type=Path, required=True)
    scale_parser.add_argument("--run-id")
    scale_parser.add_argument("--dataset-source", default=None)
    scale_parser.add_argument("--segment-count", type=int, default=None)
    scale_parser.add_argument("--model-count", type=int, default=None)
    scale_parser.add_argument("--min-segment-count", type=int, default=None)
    scale_parser.add_argument("--min-model-count", type=int, default=None)
    scale_parser.add_argument("--bbox-set", default=None)
    scale_parser.add_argument("--thresholds-file", type=Path, default=None)
    scale_parser.add_argument("--tile-content-type-expectation", default=None)
    scale_parser.add_argument("--frontend-breakpoints", default=None)
    scale_parser.add_argument("--api-base-url", default=None)
    scale_parser.add_argument("--object-prefix", default=None)
    scale_parser.add_argument("--latency-fixture", default=None)
    scale_parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "validate-slurm":
        try:
            print(
                json.dumps(
                    redact_payload(
                        validate_slurm(
                            ProductionSlurmConfig.from_env(
                                evidence_root=args.evidence_root,
                                run_id=args.run_id,
                                submit=args.submit,
                                fake_slurm=args.fake_slurm,
                                poll_interval_seconds=args.poll_interval_seconds,
                                poll_timeout_seconds=args.poll_timeout_seconds,
                                force=args.force,
                            )
                        )
                    ),
                    sort_keys=True,
                )
            )
        except (ProductionValidationError, OSError, subprocess.SubprocessError) as error:
            if isinstance(error, ProductionValidationError):
                print(f"{error.error_code}: {error.message}", file=sys.stderr)
            else:
                print(f"PRODUCTION_SLURM_VALIDATION_FAILED: {error}", file=sys.stderr)
            return 1
        return 0
    if args.command == "validate-object-store":
        try:
            print(
                json.dumps(
                    redact_payload(
                        validate_object_store(
                            ProductionObjectStoreConfig.from_env(
                                evidence_root=args.evidence_root,
                                run_id=args.run_id,
                                basins_root=args.basins_root,
                                model_id=args.model_id,
                                version=args.version,
                                force=args.force,
                            )
                        )
                    ),
                    sort_keys=True,
                )
            )
        except ProductionObjectStoreValidationError as error:
            print(f"{error.error_code}: {error.message}", file=sys.stderr)
            return 1
        except Exception as error:
            to_payload = getattr(error, "to_payload", None)
            if callable(to_payload):
                print(json.dumps(to_payload(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
            else:
                print(f"PRODUCTION_OBJECT_STORE_VALIDATION_FAILED: {error}", file=sys.stderr)
            return 1
        return 0
    if args.command == "validate-met":
        try:
            print(
                json.dumps(
                    redact_payload(
                        validate_met(
                            ProductionMetConfig.from_env(
                                evidence_root=args.evidence_root,
                                run_id=args.run_id,
                                sources=args.sources,
                                cycle_start=args.cycle_start,
                                cycle_end=args.cycle_end,
                                forecast_hours=args.forecast_hours,
                                model_id=args.model_id,
                                model_version=args.model_version,
                                force=args.force,
                            )
                        )
                    ),
                    sort_keys=True,
                )
            )
        except ProductionMetValidationError as error:
            print(f"{error.error_code}: {error.message}", file=sys.stderr)
            return 1
        except Exception as error:
            print(f"PRODUCTION_MET_VALIDATION_FAILED: {error}", file=sys.stderr)
            return 1
        return 0
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
    if args.command == "validate-scale":
        try:
            print(
                json.dumps(
                    redact_payload(
                        validate_scale(
                            ProductionScaleConfig.from_env(
                                evidence_root=args.evidence_root,
                                run_id=args.run_id,
                                dataset_source=args.dataset_source,
                                segment_count=args.segment_count,
                                model_count=args.model_count,
                                min_segment_count=args.min_segment_count,
                                min_model_count=args.min_model_count,
                                bbox_set=args.bbox_set,
                                thresholds_file=args.thresholds_file,
                                tile_content_type_expectation=args.tile_content_type_expectation,
                                frontend_breakpoints=args.frontend_breakpoints,
                                api_base_url=args.api_base_url,
                                object_prefix=args.object_prefix,
                                latency_fixture=args.latency_fixture,
                                force=args.force,
                            )
                        )
                    ),
                    sort_keys=True,
                )
            )
        except ProductionScaleValidationError as error:
            print(f"{error.error_code}: {redact_text(error.message)}", file=sys.stderr)
            return 1
        except Exception as error:
            print(f"PRODUCTION_SCALE_VALIDATION_FAILED: {redact_text(str(error))}", file=sys.stderr)
            return 1
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
