from __future__ import annotations

import json
import os
import platform
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from packages.common.redaction import redact_payload, redact_text
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
)
from services.production_closure import readiness_item_contracts as _readiness_item_contracts

ProductionReadinessValidationError = _readiness_item_contracts.ProductionReadinessValidationError

MAX_EVIDENCE_PAYLOAD_BYTES = 768 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 1200
MAX_STRING_LENGTH = 2048
PATH_TOKEN_RE = re.compile(
    r"\bfile://(?:localhost)?/[^\s\"'<>),;]+"
    r"|\\\\[^\s\"'<>),;]+\\[^\s\"'<>),;]+"
    r"|\b[A-Za-z]:\\[^\s\"'<>),;]+"
    r"|(?<![:/\w])/(?:[^\s\"'<>),;]+)"
)

DEPENDENCY_ROOT_ENV = {
    "slurm": "NHMS_PRODUCTION_READINESS_SLURM_EVIDENCE_ROOT",
    "object_store": "NHMS_PRODUCTION_READINESS_OBJECT_STORE_EVIDENCE_ROOT",
    "source": "NHMS_PRODUCTION_READINESS_SOURCE_EVIDENCE_ROOT",
    "e2e": "NHMS_PRODUCTION_READINESS_E2E_EVIDENCE_ROOT",
    "mvt": "NHMS_PRODUCTION_READINESS_MVT_EVIDENCE_ROOT",
}
SCHEDULER_EVIDENCE_ROOT_ENV = "NHMS_PRODUCTION_READINESS_SCHEDULER_EVIDENCE_ROOT"
SCHEDULER_EVIDENCE_FILE_ENV = "NHMS_PRODUCTION_READINESS_SCHEDULER_EVIDENCE_FILE"
PROOF_FILE_ENV = {
    "auth": "NHMS_PRODUCTION_READINESS_AUTH_PROOF_FILE",
    "alert": "NHMS_PRODUCTION_READINESS_ALERT_PROOF_FILE",
    "rollback": "NHMS_PRODUCTION_READINESS_ROLLBACK_PROOF_FILE",
    "scheduler": "NHMS_PRODUCTION_READINESS_SCHEDULER_PROOF_FILE",
    "slurm": "NHMS_PRODUCTION_READINESS_SLURM_PROOF_FILE",
    "object_store": "NHMS_PRODUCTION_READINESS_OBJECT_STORE_PROOF_FILE",
    "source": "NHMS_PRODUCTION_READINESS_SOURCE_PROOF_FILE",
    "e2e": "NHMS_PRODUCTION_READINESS_E2E_PROOF_FILE",
    "mvt": "NHMS_PRODUCTION_READINESS_MVT_PROOF_FILE",
    "target_env": "NHMS_PRODUCTION_READINESS_TARGET_ENV_PROOF_FILE",
}


@dataclass(frozen=True)
class BoundedPayloadResult:
    payload: Any
    node_truncated: bool = False
    depth_truncated: bool = False


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


def _preflight_payload(
    config: Any,
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
        "scheduler_evidence_root": _path_for_evidence(config.scheduler_evidence_root, config=config),
        "scheduler_evidence_file": _path_for_evidence(config.scheduler_evidence_file, config=config),
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


def _environment_payload(config: Any) -> dict[str, Any]:
    env_keys = [
        "NHMS_RUN_PRODUCTION_CLOSURE",
        *DEPENDENCY_ROOT_ENV.values(),
        SCHEDULER_EVIDENCE_ROOT_ENV,
        SCHEDULER_EVIDENCE_FILE_ENV,
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


def _bounded_payload(value: Any) -> BoundedPayloadResult:
    nodes = 0
    node_truncated = False
    depth_truncated = False

    def bounded_key(key: Any) -> str:
        current = str(key)
        if len(current) > MAX_STRING_LENGTH:
            return f"{current[:MAX_STRING_LENGTH]}[truncated]"
        return current

    def walk(current: Any, depth: int) -> Any:
        nonlocal nodes
        nonlocal node_truncated, depth_truncated
        nodes += 1
        if nodes > MAX_JSON_NODES:
            node_truncated = True
            return "[truncated:max-nodes]"
        if depth > MAX_JSON_DEPTH:
            depth_truncated = True
            return "[truncated:max-depth]"
        if isinstance(current, Mapping):
            return {bounded_key(key): walk(nested, depth + 1) for key, nested in current.items()}
        if isinstance(current, list):
            return [walk(item, depth + 1) for item in current[:MAX_JSON_NODES]]
        if isinstance(current, tuple):
            return [walk(item, depth + 1) for item in current[:MAX_JSON_NODES]]
        if isinstance(current, str):
            if len(current) > MAX_STRING_LENGTH:
                return f"{current[:MAX_STRING_LENGTH]}[truncated]"
            return current
        return current

    return BoundedPayloadResult(
        payload=walk(value, 0),
        node_truncated=node_truncated,
        depth_truncated=depth_truncated,
    )


def _bounded_redacted_payload(value: Any, *, config: Any) -> Any:
    nodes = 0

    def redacted_key(key: Any) -> str:
        current = str(key)
        redacted = _redact_paths(current[:MAX_STRING_LENGTH], config=config)
        if len(current) > MAX_STRING_LENGTH:
            return f"{redacted}[truncated]"
        return redacted

    def walk(current: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES:
            return "[truncated:max-nodes]"
        if depth > MAX_JSON_DEPTH:
            return "[truncated:max-depth]"
        if isinstance(current, Mapping):
            return {redacted_key(key): walk(nested, depth + 1) for key, nested in current.items()}
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


def _path_for_evidence(path: Path | None, *, config: Any) -> str | None:
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


def _redact_paths(value: str, *, config: Any) -> str:
    if str(config.evidence_root) in value:
        value = value.replace(str(config.evidence_root), "evidence-root")
    cwd = str(Path.cwd())
    if cwd in value:
        value = value.replace(cwd, "workspace")
    value = PATH_TOKEN_RE.sub("[redacted-path]", value)
    return redact_text(value)


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
