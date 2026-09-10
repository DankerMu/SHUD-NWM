from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from packages.common.redaction import redact_payload, redact_text
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_limited_no_follow,
    unlink_no_follow,
)
from services.production_closure.readonly_db_types import (
    AUTHORITATIVE_EVIDENCE_FILENAMES,
    FULL_PASS_SOURCES,
    LIVE_EVIDENCE_SCHEMA,
    MAX_EVIDENCE_PAYLOAD_BYTES,
    MAX_EVIDENCE_TRAVERSAL_DEPTH,
    MAX_EVIDENCE_TRAVERSAL_NODES,
    READONLY_DB_URL_ENVS,
    REPO_ROOT,
    SIMULATED_EVIDENCE_SCHEMA,
    STATUS_BLOCKED,
    STATUS_FAIL,
    STATUS_PASS,
    VALIDATION_CONNECT_TIMEOUT_SECONDS,
    VALIDATION_IDLE_TIMEOUT_MS,
    VALIDATION_LOCK_TIMEOUT_MS,
    VALIDATION_STATEMENT_TIMEOUT_MS,
    ReadonlyDbMergeSourceEvidence,
    ReadonlyDbValidationConfig,
    ReadonlyDbValidationError,
    _refuse_symlink_components,
    _safe_resolved_evidence_root,
)


@dataclass
class EvidenceWriter:
    evidence_root: Path
    lane_dir: Path
    force: bool = False
    _created_paths: set[Path] = field(default_factory=set)

    def prepare(self) -> None:
        evidence_root = _safe_resolved_evidence_root(self.evidence_root)
        lane_dir = self.lane_dir.resolve(strict=False)
        try:
            lane_dir.relative_to(evidence_root)
        except ValueError as error:
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_PATH_UNSAFE",
                "Evidence lane directory must stay under the approved evidence root.",
            ) from error
        _refuse_symlink_components(evidence_root)
        _refuse_symlink_components(lane_dir.parent)
        if lane_dir.exists() and lane_dir.is_symlink():
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_PATH_UNSAFE",
                f"Evidence lane path must not be a symlink: {lane_dir}.",
            )
        if lane_dir.exists() and not lane_dir.is_dir():
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_PATH_UNSAFE",
                f"Evidence lane path must be a directory: {lane_dir}.",
            )
        if lane_dir.exists() and any(lane_dir.iterdir()) and not self.force:
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_EXISTS",
                f"Evidence bundle already exists: {lane_dir}. Use --force to overwrite this run_id.",
            )
        try:
            ensure_directory_no_follow(evidence_root)
            ensure_directory_no_follow(lane_dir, containment_root=evidence_root)
        except SafeFilesystemError as error:
            error_code = (
                "READONLY_DB_EVIDENCE_WRITE_FAILED" if error.kind == "io" else "READONLY_DB_EVIDENCE_PATH_UNSAFE"
            )
            raise ReadonlyDbValidationError(error_code, f"Failed to prepare evidence directory: {error}") from error

    def write_json(self, path: Path, payload: Any) -> None:
        safe_path = self._safe_file_path(path)
        if safe_path.exists() and safe_path not in self._created_paths and not self.force:
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_EXISTS",
                f"Evidence file already exists: {safe_path}. Use --force to overwrite this run_id.",
            )
        try:
            content = json.dumps(redact_payload(payload), indent=2, sort_keys=True).encode("utf-8") + b"\n"
        except RecursionError as error:
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_REDACTION_DEPTH_EXCEEDED",
                "Readonly DB evidence payload is too deeply nested to redact safely.",
            ) from error
        try:
            atomic_write_bytes_no_follow(safe_path, content, containment_root=self.lane_dir, mode=0o600)
            self._created_paths.add(safe_path)
        except SafeFilesystemError as error:
            error_code = (
                "READONLY_DB_EVIDENCE_WRITE_FAILED" if error.kind == "io" else "READONLY_DB_EVIDENCE_PATH_UNSAFE"
            )
            raise ReadonlyDbValidationError(error_code, f"Failed to write evidence file: {error}") from error

    def remove_json(self, path: Path) -> None:
        safe_path = self._safe_file_path(path)
        try:
            unlink_no_follow(safe_path, containment_root=self.lane_dir, missing_ok=True)
            self._created_paths.discard(safe_path)
        except SafeFilesystemError as error:
            error_code = (
                "READONLY_DB_EVIDENCE_WRITE_FAILED" if error.kind == "io" else "READONLY_DB_EVIDENCE_PATH_UNSAFE"
            )
            raise ReadonlyDbValidationError(error_code, f"Failed to remove stale evidence file: {error}") from error

    def _safe_file_path(self, path: Path) -> Path:
        if path.is_symlink():
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_PATH_UNSAFE",
                f"Evidence file must not be a symlink: {path}.",
            )
        resolved_lane = self.lane_dir.resolve(strict=False)
        resolved_parent = path.parent.resolve(strict=False)
        try:
            resolved_parent.relative_to(resolved_lane)
        except ValueError as error:
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_PATH_UNSAFE",
                "Evidence file path must stay under the readonly DB evidence lane.",
            ) from error
        _refuse_symlink_components(path.parent)
        try:
            ensure_directory_no_follow(path.parent, containment_root=self.lane_dir)
        except SafeFilesystemError as error:
            error_code = (
                "READONLY_DB_EVIDENCE_WRITE_FAILED" if error.kind == "io" else "READONLY_DB_EVIDENCE_PATH_UNSAFE"
            )
            raise ReadonlyDbValidationError(error_code, f"Failed to prepare evidence file parent: {error}") from error
        return resolved_parent / path.name


def merge_readonly_db_source_evidence(
    *,
    evidence_root: Path,
    run_id: str,
    source_dirs: Sequence[Path],
    declared_sources: Sequence[str] | None = None,
    reduced_scope: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=evidence_root,
        run_id=run_id,
        database_url="postgresql://redacted/nhms",
        force=force,
    )
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=config.force)
    writer.prepare()
    if config.force:
        for filename in AUTHORITATIVE_EVIDENCE_FILENAMES:
            writer.remove_json(config.lane_dir / filename)
    source_evidence = [
        _read_source_evidence(
            source_dir,
            expected_run_id=config.run_id,
            expected_evidence_parent=config.evidence_root,
        )
        for source_dir in source_dirs
    ]
    summary = _merged_readonly_db_summary(
        config,
        source_evidence=source_evidence,
        declared_sources=tuple(_normalize_source_name(source) for source in (declared_sources or FULL_PASS_SOURCES)),
        reduced_scope=reduced_scope,
    )
    writer.write_json(config.lane_dir / "role.json", summary["role"])
    writer.write_json(config.lane_dir / "route_smoke.json", summary["route_smoke"])
    writer.write_json(config.lane_dir / "permission_probes.json", summary["permission_probes"])
    writer.write_json(config.lane_dir / "summary.json", summary)
    return redact_payload(summary)


def _read_source_evidence(
    source_dir: Path,
    *,
    expected_run_id: str,
    expected_evidence_parent: Path,
) -> ReadonlyDbMergeSourceEvidence:
    resolved_dir = _safe_merge_source_dir(source_dir)
    artifacts: dict[str, dict[str, Any]] = {}
    payloads: dict[str, Any] = {}
    for filename in AUTHORITATIVE_EVIDENCE_FILENAMES:
        path = resolved_dir / filename
        payload, sha256 = _read_source_json_file(path, source_dir=resolved_dir)
        payloads[filename] = payload
        artifacts[filename] = _source_artifact_record(path, payload, sha256=sha256)
    summary = payloads["summary.json"]
    if not isinstance(summary, dict):
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_JSON_INVALID",
            f"Readonly DB source summary must be a JSON object: {resolved_dir / 'summary.json'}",
        )
    summary_run_id = summary.get("run_id")
    for artifact in artifacts.values():
        if artifact.get("run_id") is None and isinstance(summary_run_id, str) and summary_run_id.strip():
            artifact["run_id"] = summary_run_id
    _validate_source_siblings(resolved_dir, summary, payloads)
    parent_binding_field = _validate_merge_source_run(
        summary,
        artifacts=artifacts,
        expected_run_id=expected_run_id,
        source_dir=resolved_dir,
        expected_evidence_parent=expected_evidence_parent,
    )
    _validate_merge_source_live_provenance(summary)
    return ReadonlyDbMergeSourceEvidence(
        source_dir=resolved_dir,
        summary=summary,
        artifacts=artifacts,
        parent_binding_field=parent_binding_field,
    )


def _safe_merge_source_dir(source_dir: Path) -> Path:
    _refuse_symlink_components(source_dir)
    resolved = _safe_resolved_evidence_root(source_dir)
    _refuse_symlink_components(resolved)
    if resolved.exists() and not resolved.is_dir():
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_INVALID",
            f"Readonly DB merge source must be a directory: {resolved}",
        )
    return resolved


def _read_source_json_file(path: Path, *, source_dir: Path) -> tuple[Any, str]:
    try:
        if path.is_symlink():
            raise ReadonlyDbValidationError(
                "READONLY_DB_MERGE_SOURCE_SYMLINK",
                f"Readonly DB merge source file must not be a symlink: {path}",
            )
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.resolve(strict=False).parent != source_dir:
            raise ReadonlyDbValidationError(
                "READONLY_DB_MERGE_SOURCE_PATH_UNSAFE",
                f"Readonly DB merge source file must stay in source dir: {path}",
            )
        content = read_bytes_limited_no_follow(
            path,
            max_bytes=MAX_EVIDENCE_PAYLOAD_BYTES,
            containment_root=source_dir,
        )
        if len(content) > MAX_EVIDENCE_PAYLOAD_BYTES:
            raise ReadonlyDbValidationError(
                "READONLY_DB_MERGE_SOURCE_TOO_LARGE",
                f"Readonly DB source evidence exceeds {MAX_EVIDENCE_PAYLOAD_BYTES} bytes: {path}",
            )
        payload = json.loads(content.decode("utf-8"))
        _ensure_bounded_json_value(payload, path=path)
        sha256 = hashlib.sha256(content).hexdigest()
    except FileNotFoundError as error:
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_MISSING",
            f"Readonly DB source authoritative evidence is missing: {path}",
        ) from error
    except SafeFilesystemError as error:
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_PATH_UNSAFE",
            f"Readonly DB source evidence path is unsafe: {path}",
        ) from error
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_JSON_INVALID",
            f"Readonly DB source evidence is invalid JSON: {path}",
        ) from error
    except ReadonlyDbValidationError:
        raise
    return payload, sha256


def _source_artifact_record(path: Path, payload: Any, *, sha256: str) -> dict[str, Any]:
    return {
        "path": _public_path(path),
        "sha256": sha256,
        "run_id": _artifact_run_id(payload),
    }


def _artifact_run_id(payload: Any) -> str | None:
    if isinstance(payload, Mapping):
        schema = str(payload.get("schema") or payload.get("schema_version") or "")
        if schema in {LIVE_EVIDENCE_SCHEMA, SIMULATED_EVIDENCE_SCHEMA}:
            value = payload.get("run_id") or payload.get("evidence_run_id") or payload.get("bundle_run_id")
            if value is not None and str(value).strip():
                return str(value)
        value = payload.get("evidence_run_id") or payload.get("bundle_run_id")
        if value is not None and str(value).strip():
            return str(value)
    return None


def _ensure_bounded_json_value(value: Any, *, path: Path) -> None:
    try:
        for _parent, _key, _nested, _depth in _walk_json_values(value):
            pass
    except ReadonlyDbValidationError as error:
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_JSON_TOO_DEEP",
            f"Readonly DB source evidence exceeds traversal bounds: {path}",
        ) from error


def _walk_json_values(value: Any):
    stack: list[tuple[Any, str | None, Any, int]] = [(None, None, value, 0)]
    visited = 0
    while stack:
        parent, key, current, depth = stack.pop()
        visited += 1
        if visited > MAX_EVIDENCE_TRAVERSAL_NODES:
            raise ReadonlyDbValidationError(
                "READONLY_DB_MERGE_SOURCE_JSON_TOO_DEEP",
                "Readonly DB source evidence traversal node limit was exceeded.",
            )
        if depth > MAX_EVIDENCE_TRAVERSAL_DEPTH:
            raise ReadonlyDbValidationError(
                "READONLY_DB_MERGE_SOURCE_JSON_TOO_DEEP",
                "Readonly DB source evidence traversal depth limit was exceeded.",
            )
        yield parent, key, current, depth
        if isinstance(current, Mapping):
            for nested_key, nested in reversed(list(current.items())):
                stack.append((current, str(nested_key), nested, depth + 1))
        elif isinstance(current, list):
            for index in range(len(current) - 1, -1, -1):
                stack.append((current, str(index), current[index], depth + 1))


def _validate_source_siblings(source_dir: Path, summary: Mapping[str, Any], payloads: Mapping[str, Any]) -> None:
    expected = {
        "role.json": summary.get("role"),
        "route_smoke.json": summary.get("route_smoke"),
        "permission_probes.json": summary.get("permission_probes"),
    }
    for filename, expected_payload in expected.items():
        if payloads.get(filename) != expected_payload:
            raise ReadonlyDbValidationError(
                "READONLY_DB_MERGE_SOURCE_SIBLING_MISMATCH",
                f"Readonly DB merge source {filename} must match summary.json: {source_dir}",
            )


def _validate_merge_source_run(
    summary: Mapping[str, Any],
    *,
    artifacts: Mapping[str, Mapping[str, Any]],
    expected_run_id: str,
    source_dir: Path,
    expected_evidence_parent: Path,
) -> str:
    run_id = summary.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_RUN_ID_MISSING",
            "Readonly DB merge source summary must include run_id.",
        )
    if run_id == expected_run_id:
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_RUN_LAYOUT_INVALID",
            "Readonly DB merge source must be a per-source lane, not the final merge lane.",
        )
    parent_binding = _merge_source_parent_binding(summary)
    if parent_binding is not None and parent_binding[1] != expected_run_id:
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_PARENT_RUN_MISMATCH",
            "Readonly DB merge source parent/current bundle binding must match the final bundle.",
        )
    expected_parent = expected_evidence_parent.expanduser().resolve(strict=False)
    observed_parent = source_dir.parent.parent.parent.expanduser().resolve(strict=False)
    if parent_binding is None and _is_expected_per_source_run_id(run_id, expected_run_id):
        if observed_parent != expected_parent:
            raise ReadonlyDbValidationError(
                "READONLY_DB_MERGE_SOURCE_PARENT_ROOT_MISMATCH",
                "Readonly DB run_id-prefix merge source must live under the current final evidence parent.",
            )
    elif parent_binding is None:
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_PARENT_RUN_MISMATCH",
            "Readonly DB merge source must be a current per-source lane or explicitly bind to the final bundle.",
        )
    else:
        root_binding = _merge_source_root_binding(summary)
        if root_binding is None:
            raise ReadonlyDbValidationError(
                "READONLY_DB_MERGE_SOURCE_PARENT_ROOT_MISSING",
                "Readonly DB external merge source must explicitly bind to the current final evidence root.",
            )
        observed_root = Path(root_binding[1]).expanduser().resolve(strict=False)
        expected_run_dir = expected_parent / expected_run_id
        if observed_root not in {expected_parent, expected_run_dir.resolve(strict=False)}:
            raise ReadonlyDbValidationError(
                "READONLY_DB_MERGE_SOURCE_PARENT_ROOT_MISMATCH",
                "Readonly DB external merge source root binding must match the final bundle root.",
            )
    for filename, artifact in artifacts.items():
        artifact_run_id = artifact.get("run_id")
        if artifact_run_id is not None and artifact_run_id != run_id:
            raise ReadonlyDbValidationError(
                "READONLY_DB_MERGE_SOURCE_RUN_ID_MISMATCH",
                f"Readonly DB merge source artifact {filename} run_id must match summary.json.",
            )
    return parent_binding[0] if parent_binding is not None else "run_id_prefix"


def _validate_merge_source_live_provenance(summary: Mapping[str, Any]) -> None:
    if summary.get("schema") != LIVE_EVIDENCE_SCHEMA:
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_SCHEMA_INVALID",
            "Readonly DB merge source must use the live evidence schema.",
        )
    if summary.get("status") != STATUS_PASS:
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_NOT_PASS",
            "Readonly DB merge source must be PASS before it can be merged.",
        )
    provenance = summary.get("validation_provenance")
    if not isinstance(provenance, Mapping):
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_PROVENANCE_MISSING",
            "Readonly DB merge source must include validation_provenance.",
        )
    if provenance.get("mode") != "live" or provenance.get("live_readonly_proof") is not True:
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_LIVE_PROOF_MISSING",
            "Readonly DB merge source must carry live validation provenance.",
        )


def _merge_source_parent_binding(summary: Mapping[str, Any]) -> tuple[str, str] | None:
    for key in (
        "parent_evidence_run_id",
        "parent_bundle_run_id",
        "parent_bundle_id",
        "current_evidence_run_id",
        "current_bundle_run_id",
        "expected_evidence_run_id",
    ):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            return key, value
    provenance = summary.get("validation_provenance")
    if isinstance(provenance, Mapping):
        for key in (
            "parent_evidence_run_id",
            "parent_bundle_run_id",
            "parent_bundle_id",
            "current_evidence_run_id",
            "current_bundle_run_id",
            "expected_evidence_run_id",
        ):
            value = provenance.get(key)
            if isinstance(value, str) and value.strip():
                return f"validation_provenance.{key}", value
    return None


def _merge_source_root_binding(summary: Mapping[str, Any]) -> tuple[str, str] | None:
    for key in (
        "parent_evidence_root",
        "parent_bundle_root",
        "current_evidence_root",
        "current_bundle_root",
        "final_evidence_root",
        "final_run_dir",
    ):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            return key, value
    provenance = summary.get("validation_provenance")
    if isinstance(provenance, Mapping):
        for key in (
            "parent_evidence_root",
            "parent_bundle_root",
            "current_evidence_root",
            "current_bundle_root",
            "final_evidence_root",
            "final_run_dir",
        ):
            value = provenance.get(key)
            if isinstance(value, str) and value.strip():
                return f"validation_provenance.{key}", value
    return None


def _is_expected_per_source_run_id(run_id: str, expected_run_id: str) -> bool:
    run_id_lower = run_id.lower()
    expected_lower = expected_run_id.lower()
    return run_id_lower in {
        f"{expected_lower}-gfs",
        f"{expected_lower}-ifs",
        f"{expected_lower}-db-gfs",
        f"{expected_lower}-db-ifs",
    }


def _merged_readonly_db_summary(
    config: ReadonlyDbValidationConfig,
    *,
    source_evidence: Sequence[ReadonlyDbMergeSourceEvidence],
    declared_sources: tuple[str, ...],
    reduced_scope: bool,
) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    route_smoke: list[dict[str, Any]] = []
    display_identity: dict[str, Any] = {}
    permission_probes: list[dict[str, Any]] | None = None
    role: dict[str, Any] | None = None
    database_url = "postgresql://db.example:5432/nhms"
    source_artifacts: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for index, evidence in enumerate(source_evidence):
        payload = evidence.summary
        if payload.get("schema") != LIVE_EVIDENCE_SCHEMA:
            blockers.append(
                {
                    "code": "READONLY_DB_MERGE_SOURCE_SCHEMA_INVALID",
                    "source_index": index,
                    "schema": payload.get("schema"),
                }
            )
        if payload.get("status") != STATUS_PASS:
            blockers.append(
                {
                    "code": "READONLY_DB_MERGE_SOURCE_NOT_PASS",
                    "source_index": index,
                    "status": payload.get("status"),
                }
            )
        database_url = str(payload.get("database_url") or database_url)
        source_names = _merge_payload_sources(payload)
        if not source_names:
            blockers.append({"code": "READONLY_DB_MERGE_SOURCE_IDENTITY_MISSING", "source_index": index})
        for source_name in source_names:
            if source_name in seen_sources:
                blockers.append(
                    {
                        "code": "READONLY_DB_MERGE_DUPLICATE_SOURCE",
                        "source_index": index,
                        "source": source_name,
                    }
                )
            seen_sources.add(source_name)
        source_artifacts.append(
            {
                "source_index": index,
                "sources": sorted(source_names),
                "source_dir": _public_path(evidence.source_dir),
                "summary_run_id": payload.get("run_id"),
                "parent_binding": evidence.parent_binding_field,
                "validation_provenance": _merge_source_validation_provenance(payload),
                "artifacts": evidence.artifacts,
            }
        )
        payload_role = payload.get("role")
        if isinstance(payload_role, Mapping):
            role = dict(payload_role) if role is None else role
            if role != payload_role:
                blockers.append({"code": "READONLY_DB_MERGE_ROLE_MISMATCH", "source_index": index})
        payload_permission_probes = _normalized_merge_permission_probes(payload.get("permission_probes"))
        if permission_probes is None and payload_permission_probes is not None:
            permission_probes = payload_permission_probes
        elif payload_permission_probes != permission_probes:
            blockers.append({"code": "READONLY_DB_MERGE_PERMISSION_MATRIX_MISMATCH", "source_index": index})
        for route in payload.get("route_smoke", []):
            if isinstance(route, Mapping):
                if route.get("name") in {"health", "runtime_config", "models"} and any(
                    existing.get("name") == route.get("name") for existing in route_smoke
                ):
                    continue
                route_record = dict(route)
                identity = route_record.get("strict_identity") or route_record.get("identity")
                if "source" not in route_record and isinstance(identity, Mapping) and identity.get("source"):
                    route_record["source"] = str(identity["source"])
                if "source" not in route_record:
                    source_from_path = _source_from_route_path(str(route_record.get("path") or ""))
                    if source_from_path:
                        route_record["source"] = source_from_path
                route_smoke.append(route_record)
        source_identity = payload.get("display_identity")
        if isinstance(source_identity, Mapping):
            source_name = _identity_text(source_identity, "source")
            if source_name:
                display_identity[source_name] = dict(source_identity)
            else:
                for key, value in source_identity.items():
                    if isinstance(value, Mapping):
                        display_identity[str(key)] = dict(value)
    if role is None:
        role = {"current_user": None, "role_type": "readonly_candidate"}
        blockers.append({"code": "READONLY_DB_MERGE_ROLE_MISSING"})
    if permission_probes is None:
        permission_probes = []
        blockers.append({"code": "READONLY_DB_MERGE_PERMISSION_MATRIX_MISSING"})
    expected_sources = set(declared_sources)
    missing_sources = sorted(expected_sources - seen_sources)
    if missing_sources:
        blockers.append(
            {
                "code": "READONLY_DB_MERGE_SOURCE_MISSING",
                "missing_sources": missing_sources,
                "observed_sources": sorted(seen_sources),
                "declared_sources": sorted(expected_sources),
                "reduced_scope": reduced_scope,
            }
        )
    if not reduced_scope and expected_sources != FULL_PASS_SOURCES:
        blockers.append(
            {
                "code": "READONLY_DB_MERGE_SCOPE_INVALID",
                "declared_sources": sorted(expected_sources),
                "message": "Full-scope readonly DB merge must declare both GFS and IFS unless --reduced-scope is set.",
            }
        )
    status = STATUS_BLOCKED if blockers else STATUS_PASS
    summary = {
        "schema": LIVE_EVIDENCE_SCHEMA,
        "status": status,
        "run_id": config.run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "evidence_dir": _public_path(config.lane_dir),
        "database_url": _redact_database_url(database_url),
        "source_env_vars": list(READONLY_DB_URL_ENVS),
        "validation_provenance": {
            "mode": "live",
            "live_readonly_proof": not blockers,
            "merged_source_evidence": True,
            "declared_sources": sorted(expected_sources),
            "reduced_scope": reduced_scope,
            "source_bundle_count": len(source_evidence),
            "source_artifacts": source_artifacts,
        },
        "validation_timeouts": _validation_timeout_evidence(),
        "runtime": {
            "service_role": "display_readonly",
            "control_mutations_expected": False,
        },
        "role": role,
        "display_identity": display_identity,
        "route_smoke": route_smoke,
        "manual_action_probes": _merged_manual_actions([evidence.summary for evidence in source_evidence]),
        "permission_probe_summary": _permission_summary(permission_probes),
        "permission_probes": permission_probes,
        "redaction": {
            "database_url_redacted": True,
            "sensitive_values_redacted": True,
            "evidence_root_approved": True,
        },
    }
    if blockers:
        summary["blockers"] = blockers
    return summary


def _merged_manual_actions(source_payloads: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    for payload in source_payloads:
        manual_actions = payload.get("manual_action_probes")
        if isinstance(manual_actions, list) and manual_actions:
            return [dict(item) for item in manual_actions if isinstance(item, Mapping)]
    return []


def _merge_source_validation_provenance(payload: Mapping[str, Any]) -> dict[str, Any]:
    provenance = payload.get("validation_provenance")
    if not isinstance(provenance, Mapping):
        return {}
    return {
        "mode": provenance.get("mode"),
        "live_readonly_proof": provenance.get("live_readonly_proof"),
        **{
            key: provenance[key]
            for key in (
                "parent_evidence_run_id",
                "parent_bundle_run_id",
                "parent_bundle_id",
                "current_evidence_run_id",
                "current_bundle_run_id",
                "expected_evidence_run_id",
                "parent_evidence_root",
                "parent_bundle_root",
                "current_evidence_root",
                "current_bundle_root",
                "final_evidence_root",
                "final_run_dir",
            )
            if key in provenance
        },
    }


def _merge_payload_sources(payload: Mapping[str, Any]) -> set[str]:
    sources: set[str] = set()
    display_identity = payload.get("display_identity")
    if isinstance(display_identity, Mapping):
        source_name = _identity_text(display_identity, "source")
        if source_name:
            sources.add(source_name.upper())
        for key, value in display_identity.items():
            if str(key).upper() in {"GFS", "IFS"} and isinstance(value, Mapping):
                sources.add(str(key).upper())
            if isinstance(value, Mapping):
                nested_source = _identity_text(value, "source")
                if nested_source:
                    sources.add(nested_source.upper())
    route_smoke = payload.get("route_smoke")
    if isinstance(route_smoke, list):
        for route in route_smoke:
            if not isinstance(route, Mapping):
                continue
            source = _identity_text(route, "source") or _identity_text(route, "source_id")
            identity = route.get("strict_identity") or route.get("identity")
            if not source and isinstance(identity, Mapping):
                source = _identity_text(identity, "source") or _identity_text(identity, "source_id")
            if source:
                sources.add(source.upper())
    return sources


def _normalize_source_name(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_DECLARED_SOURCE_INVALID",
            "Readonly DB merge declared sources must be non-empty.",
        )
    return text


def _source_from_route_path(path: str) -> str | None:
    try:
        query = parse_qsl(urlsplit(path).query, keep_blank_values=True)
    except ValueError:
        return None
    values = {key: value for key, value in query}
    return values.get("source") or values.get("source_id")


def _normalized_merge_permission_probes(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    probes: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        probe = dict(item)
        operations = probe.get("operations")
        if isinstance(operations, list):
            normalized_operations = []
            for operation in operations:
                if not isinstance(operation, Mapping):
                    continue
                normalized = dict(operation)
                normalized.pop("command", None)
                normalized_operations.append(normalized)
            probe["operations"] = normalized_operations
        probes.append(probe)
    return probes


def _permission_summary(permission_probes: list[dict[str, Any]]) -> dict[str, Any]:
    operations = [operation for target in permission_probes for operation in target.get("operations", [])]
    return {
        "target_count": len(permission_probes),
        "operation_count": len(operations),
        "passed_denial_count": sum(1 for operation in operations if operation.get("status") == STATUS_PASS),
        "failed_mutating_count": sum(1 for operation in operations if operation.get("status") == STATUS_FAIL),
        "blocked_count": sum(1 for target in permission_probes if target.get("status") == STATUS_BLOCKED),
    }


def _identity_text(identity: Mapping[str, Any], key: str) -> str | None:
    value = identity.get(key)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _redact_database_url(database_url: str | None) -> str | None:
    if not database_url:
        return None
    try:
        parsed = urlsplit(database_url)
    except ValueError:
        return "[redacted]"
    if not parsed.scheme:
        return redact_text(database_url)
    host = parsed.hostname or ""
    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _validation_timeout_evidence() -> dict[str, int]:
    return {
        "connect_timeout_seconds": VALIDATION_CONNECT_TIMEOUT_SECONDS,
        "statement_timeout_ms": VALIDATION_STATEMENT_TIMEOUT_MS,
        "lock_timeout_ms": VALIDATION_LOCK_TIMEOUT_MS,
        "idle_in_transaction_session_timeout_ms": VALIDATION_IDLE_TIMEOUT_MS,
    }


def _public_path(path: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        return str(resolved)
