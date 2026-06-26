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
    read_bytes_limited_no_follow,
    write_bytes_no_follow_exclusive,
)
from services.production_closure import readiness_item_contracts as _readiness_item_contracts

ProductionReadinessValidationError = _readiness_item_contracts.ProductionReadinessValidationError

MAX_EVIDENCE_PAYLOAD_BYTES = 768 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 1200
MAX_STRING_LENGTH = 2048
MAX_RECEIPT_BYTES = 64 * 1024
MAX_RECEIPT_PREVIEW_BYTES = 2048
LIVE_PROOF_SCHEMA = "nhms.production_readiness.live_proof.v1"
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
            if self.force or safe_path in self._created_paths:
                atomic_write_bytes_no_follow(safe_path, content, containment_root=self.lane_dir)
            else:
                write_bytes_no_follow_exclusive(safe_path, content, containment_root=self.lane_dir)
            self._created_paths.add(safe_path)
        except FileExistsError as error:
            raise ProductionReadinessValidationError(
                "PRODUCTION_READINESS_EVIDENCE_EXISTS",
                f"Evidence file already exists: {safe_path}. Use --force to overwrite an existing run_id bundle.",
            ) from error
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


def _load_proof(
    surface: str,
    proof_json: str | None,
    proof_file: Path | None,
    *,
    config: Any,
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
                "reason": _redact_paths(str(error), config=config),
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


def _receipt_artifact(config: Any, receipts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
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


def _receipt_details(receipt: Mapping[str, Any], *, config: Any) -> dict[str, Any]:
    return _bounded_redacted_payload(
        {key: value for key, value in receipt.items() if key not in {"payload", "raw_payload"}}
        | ({"payload": receipt.get("payload")} if "payload" in receipt else {}),
        config=config,
    )


def _receipt_validation_payload(receipt: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = receipt.get("raw_payload", receipt.get("payload"))
    return payload if isinstance(payload, Mapping) else {}


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
            bounded: dict[str, Any] = {}
            for key, nested in current.items():
                if nodes >= MAX_JSON_NODES:
                    node_truncated = True
                    bounded["[truncated:max-nodes]"] = "[truncated:max-nodes]"
                    break
                bounded[bounded_key(key)] = walk(nested, depth + 1)
            return bounded
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
            bounded: dict[str, Any] = {}
            for key, nested in current.items():
                if nodes >= MAX_JSON_NODES:
                    bounded["[truncated:max-nodes]"] = "[truncated:max-nodes]"
                    break
                bounded[redacted_key(key)] = walk(nested, depth + 1)
            return bounded
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


def _redacted_preview(raw: bytes, *, config: Any) -> str:
    preview = raw[:MAX_RECEIPT_PREVIEW_BYTES].decode("utf-8", errors="replace")
    if len(raw) > MAX_RECEIPT_PREVIEW_BYTES:
        preview += "[truncated]"
    return str(_bounded_redacted_payload(preview, config=config))


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
