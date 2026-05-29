from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from packages.common.redaction import redact_payload, redact_text
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "artifacts" / "two-node-e2e"
APPROVED_EVIDENCE_ROOTS = (REPO_ROOT / "artifacts", Path("/scratch/frd_muziyao"))
SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_EVIDENCE_PAYLOAD_BYTES = 1024 * 1024

STATUS_PASS = "PASS"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"
FINAL_EVIDENCE_SCHEMA = "nhms.two_node_e2e.final_evidence.v1"
RUN_METADATA_SCHEMAS = frozenset(
    {
        "nhms.two_node_e2e.run.v1",
        "nhms.two_node_e2e.bundle.v1",
        "nhms.two_node_e2e.identity.v1",
    }
)
READONLY_DB_LIVE_SCHEMA = "nhms.readonly_db_boundary.evidence.v1"

FINAL_REQUIRED_LANES = (
    "docker_preflight",
    "docker_security",
    "readonly_db",
    "api",
    "browser",
    "cross_plane",
    "manual_ops",
    "slurm",
    "logs",
    "compute_summary",
    "display_summary",
)

STRICT_IDENTITY_FIELDS = ("run_id", "source", "cycle_time", "model_id")
STRICT_LOG_IDENTITY_FIELDS = (*STRICT_IDENTITY_FIELDS, "job_id")
FULL_PASS_SOURCE_SET = frozenset({"GFS", "IFS"})
LIVE_EXECUTION_MODES = frozenset(
    {
        "live",
        "live_evidence",
        "live_executed",
        "live_api",
        "live_api_evidence",
        "live_browser",
        "live_browser_evidence",
        "live_cross_plane",
        "live_log_evidence",
        "live_slurm",
        "live_docker",
        "live_docker_container",
        "production",
        "production_evidence",
    }
)
MOCK_KEYS = (
    "mocked",
    "mock",
    "mock_api",
    "mock_api_data",
    "mock_browser",
    "mock_browser_data",
    "fixture_only",
    "deterministic_fixture",
)
HISTORICAL_KEYS = ("historical_latest", "latest_fallback", "used_latest_fallback", "source_only_fallback")
DOCKER_FORBIDDEN_BOOL_KEYS = (
    "slurm_route_available",
    "slurm_routes_enabled",
    "slurm_cli_present",
    "slurm_config_present",
    "slurm_socket_present",
    "munge_path_present",
    "docker_socket_present",
    "forbidden_hostconfig_hazard",
    "forbidden_mount_hazard",
    "forbidden_env_hazard",
    "writable_published_artifact_mount",
    "display_write_capability_present",
)
DOCKER_FORBIDDEN_FINDING_TOKENS = (
    "SLURM",
    "MUNGE",
    "DOCKER_SOCKET",
    "HOSTCONFIG",
    "FORBIDDEN_MOUNT",
    "FORBIDDEN_ENV",
    "WRITABLE",
)
CURRENT_EVIDENCE_RUN_ID_KEYS = (
    "evidence_run_id",
    "bundle_run_id",
    "evidence_bundle_id",
    "validation_run_id",
)


class TwoNodeE2EEvidenceError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class TwoNodeE2EEvidenceConfig:
    evidence_root: Path
    run_id: str
    declared_sources: tuple[str, ...] = ()
    reduced_scope: bool | None = None
    force: bool = False

    @property
    def run_dir(self) -> Path:
        return self.evidence_root / self.run_id

    @property
    def lane_dir(self) -> Path:
        return self.run_dir / "final-e2e-evidence"

    @classmethod
    def from_env(
        cls,
        *,
        evidence_root: Path | None = None,
        run_id: str | None = None,
        declared_sources: Sequence[str] | None = None,
        reduced_scope: bool | None = None,
        force: bool = False,
    ) -> TwoNodeE2EEvidenceConfig:
        selected_root = evidence_root or _path_env("NHMS_TWO_NODE_E2E_EVIDENCE_ROOT", DEFAULT_EVIDENCE_ROOT)
        selected_run_id = run_id or os.getenv("NHMS_TWO_NODE_E2E_RUN_ID") or _default_run_id()
        selected_sources = tuple(declared_sources or _split_sources(os.getenv("NHMS_TWO_NODE_E2E_SOURCES")))
        env_reduced_scope = _optional_bool(os.getenv("NHMS_TWO_NODE_E2E_REDUCED_SCOPE"))
        return cls(
            evidence_root=_safe_resolved_evidence_root(selected_root),
            run_id=_safe_run_id(selected_run_id),
            declared_sources=_dedupe_sources(selected_sources),
            reduced_scope=reduced_scope if reduced_scope is not None else env_reduced_scope,
            force=force,
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
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE",
                "Final evidence lane directory must stay under the approved evidence root.",
            ) from error
        _refuse_symlink_components(evidence_root)
        _refuse_symlink_components(lane_dir.parent)
        if lane_dir.exists() and lane_dir.is_symlink():
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE",
                f"Final evidence lane path must not be a symlink: {lane_dir}.",
            )
        if lane_dir.exists() and not lane_dir.is_dir():
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE",
                f"Final evidence lane path must be a directory: {lane_dir}.",
            )
        if lane_dir.exists() and any(lane_dir.iterdir()) and not self.force:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_EXISTS",
                f"Final evidence bundle already exists: {lane_dir}. Use --force to overwrite this run_id.",
            )
        try:
            ensure_directory_no_follow(evidence_root)
            ensure_directory_no_follow(lane_dir, containment_root=evidence_root)
        except SafeFilesystemError as error:
            error_code = (
                "TWO_NODE_E2E_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE"
            )
            raise TwoNodeE2EEvidenceError(error_code, f"Failed to prepare final evidence lane: {error}") from error

    def write_json(self, path: Path, payload: Any) -> None:
        safe_path = self._safe_file_path(path)
        if safe_path.exists() and safe_path not in self._created_paths and not self.force:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_EXISTS",
                f"Final evidence file already exists: {safe_path}. Use --force to overwrite this run_id.",
            )
        content = json.dumps(redact_payload(payload), indent=2, sort_keys=True).encode("utf-8") + b"\n"
        if len(content) > MAX_EVIDENCE_PAYLOAD_BYTES:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_PAYLOAD_TOO_LARGE",
                f"Final evidence payload exceeds {MAX_EVIDENCE_PAYLOAD_BYTES} bytes.",
            )
        try:
            atomic_write_bytes_no_follow(safe_path, content, containment_root=self.lane_dir)
            self._created_paths.add(safe_path)
        except SafeFilesystemError as error:
            error_code = (
                "TWO_NODE_E2E_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE"
            )
            raise TwoNodeE2EEvidenceError(error_code, f"Failed to write final evidence file: {error}") from error

    def _safe_file_path(self, path: Path) -> Path:
        if path.is_symlink():
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE",
                f"Final evidence file must not be a symlink: {path}.",
            )
        lane_dir = self.lane_dir.resolve(strict=False)
        parent = path.parent.resolve(strict=False)
        try:
            parent.relative_to(lane_dir)
        except ValueError as error:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE",
                "Final evidence file path must stay under the final evidence lane.",
            ) from error
        _refuse_symlink_components(path.parent)
        try:
            ensure_directory_no_follow(path.parent, containment_root=self.lane_dir)
        except SafeFilesystemError as error:
            error_code = (
                "TWO_NODE_E2E_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE"
            )
            raise TwoNodeE2EEvidenceError(error_code, f"Failed to prepare final evidence parent: {error}") from error
        return parent / path.name


@dataclass(frozen=True)
class EvidenceDocument:
    path: Path
    payload: Mapping[str, Any]
    sha256: str


@dataclass(frozen=True)
class LaneEvaluation:
    name: str
    status: str
    evidence_path: str | None = None
    evidence_sha256: str | None = None
    summary_status: str | None = None
    blockers: tuple[dict[str, Any], ...] = ()
    findings: tuple[dict[str, Any], ...] = ()
    evidence: Mapping[str, Any] | None = None

    def to_summary(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "evidence_path": self.evidence_path,
            "evidence_sha256": self.evidence_sha256,
            "summary_status": self.summary_status,
            "blockers": list(self.blockers),
            "findings": list(self.findings),
        }
        if self.evidence is not None:
            payload["redacted_evidence"] = redact_payload(self.evidence)
        return payload


def validate_two_node_e2e_evidence(config: TwoNodeE2EEvidenceConfig) -> dict[str, Any]:
    config = replace(config, evidence_root=_safe_resolved_evidence_root(config.evidence_root))
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=config.force)
    writer.prepare()

    metadata_doc = _find_first_json(
        config.run_dir,
        (
            "run.json",
            "identity.json",
            "metadata.json",
            "cross-plane/run.json",
            "cross-plane/identity.json",
        ),
    )
    metadata = metadata_doc.payload if metadata_doc is not None else {}
    scope = _resolve_scope(config, metadata)
    strict_identities = _resolve_strict_identities(metadata, declared_sources=scope["declared_sources"])

    lane_docs = _load_lane_documents(config.run_dir)
    lanes = {
        "docker_preflight": _evaluate_docker_preflight(
            lane_docs["docker_preflight"], evidence_run_id=config.run_id
        ),
        "docker_security": _evaluate_docker_security(
            lane_docs["docker_security"], evidence_run_id=config.run_id
        ),
        "readonly_db": _evaluate_readonly_db(lane_docs["readonly_db"], evidence_run_id=config.run_id),
        "api": _evaluate_source_lane(
            "api",
            lane_docs["api"],
            declared_sources=scope["declared_sources"],
            strict_identities=strict_identities,
            required_checks=("latest_product", "series", "ops_status", "ops_stages", "jobs"),
            live_flag="live_api_evidence",
            evidence_run_id=config.run_id,
        ),
        "browser": _evaluate_source_lane(
            "browser",
            lane_docs["browser"],
            declared_sources=scope["declared_sources"],
            strict_identities=strict_identities,
            required_checks=_browser_required_checks(scope["declared_sources"]),
            live_flag="live_browser_evidence",
            evidence_run_id=config.run_id,
        ),
        "logs": _evaluate_source_lane(
            "logs",
            lane_docs["logs"],
            declared_sources=scope["declared_sources"],
            strict_identities=strict_identities,
            required_checks=("job_logs",),
            live_flag="live_log_evidence",
            require_job_id=True,
            evidence_run_id=config.run_id,
        ),
        "slurm": _evaluate_simple_live_lane(
            "slurm",
            lane_docs["slurm"],
            live_flag="live_slurm_evidence",
            evidence_run_id=config.run_id,
        ),
        "manual_ops": _evaluate_manual_ops(
            lane_docs["manual_ops"],
            strict_identities=strict_identities,
            evidence_run_id=config.run_id,
        ),
        "compute_summary": _evaluate_simple_live_lane(
            "compute_summary",
            lane_docs["compute_summary"],
            live_flag="live_compute_evidence",
            allowed_statuses=(STATUS_PASS, "ready", "submitted"),
            evidence_run_id=config.run_id,
        ),
        "display_summary": _evaluate_simple_live_lane(
            "display_summary",
            lane_docs["display_summary"],
            live_flag="live_display_evidence",
            allowed_statuses=(STATUS_PASS, "ready"),
            evidence_run_id=config.run_id,
        ),
    }
    source_scope_results = _source_scope_results(
        declared_sources=scope["declared_sources"],
        strict_identities=strict_identities,
        source_lanes={name: lanes[name] for name in ("api", "browser", "logs")},
    )
    lanes["cross_plane"] = _evaluate_cross_plane(
        lane_docs["cross_plane"],
        declared_sources=scope["declared_sources"],
        strict_identities=strict_identities,
        source_scope_results=source_scope_results,
        reduced_scope=scope["reduced_scope"],
        evidence_run_id=config.run_id,
    )

    final_status = _final_status(lanes, source_scope_results, scope)
    blockers, findings = _collect_blockers_and_findings(lanes, source_scope_results, scope)
    summary = {
        "schema": FINAL_EVIDENCE_SCHEMA,
        "status": final_status,
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": config.run_id,
        "evidence_root": _public_path(config.evidence_root),
        "run_dir": _public_path(config.run_dir),
        "evidence_dir": _public_path(config.lane_dir),
        "metadata": _metadata_summary(metadata_doc, metadata),
        "strict_identity": {
            "declared_sources": list(scope["declared_sources"]),
            "reduced_scope": scope["reduced_scope"],
            "reduced_scope_declared": scope["reduced_scope_declared"],
            "full_pass_source_set": sorted(FULL_PASS_SOURCE_SET),
            "sources": redact_payload(strict_identities),
        },
        "lane_summaries": {name: lane.to_summary() for name, lane in lanes.items()},
        "source_scope_results": source_scope_results,
        "blockers": blockers,
        "findings": findings,
        "redaction": {
            "sensitive_values_redacted": True,
            "raw_secret_material_written": False,
            "evidence_root_approved": True,
        },
    }
    writer.write_json(config.lane_dir / "summary.json", summary)
    return redact_payload(summary)


def _load_lane_documents(run_dir: Path) -> dict[str, EvidenceDocument | None]:
    return {
        "docker_preflight": _find_first_json(
            run_dir,
            (
                "docker-preflight/summary.json",
                "docker-preflight/docker-preflight.json",
                "docker-preflight.json",
            ),
        ),
        "docker_security": _find_first_json(
            run_dir,
            (
                "docker-security/summary.json",
                "docker-security/display-isolation.json",
                "docker-security/docker-smoke.json",
                "docker-smoke/docker-smoke.json",
                "docker-smoke.json",
            ),
        ),
        "readonly_db": _find_first_json(
            run_dir,
            (
                "db/readonly-db-boundary/summary.json",
                "db/summary.json",
            ),
        ),
        "api": _find_first_json(run_dir, ("api/summary.json", "api/evidence.json")),
        "browser": _find_first_json(run_dir, ("browser/summary.json", "browser/evidence.json")),
        "cross_plane": _find_first_json(run_dir, ("cross-plane/summary.json", "cross-plane/evidence.json")),
        "manual_ops": _find_first_json(run_dir, ("manual-ops/summary.json", "manual-ops/evidence.json")),
        "slurm": _find_first_json(run_dir, ("slurm/summary.json", "slurm/evidence.json")),
        "logs": _find_first_json(run_dir, ("logs/summary.json", "logs/evidence.json")),
        "compute_summary": _find_first_json(
            run_dir,
            (
                "22-compute/summary.json",
                "compute/summary.json",
                "compute-summary.json",
            ),
        ),
        "display_summary": _find_first_json(
            run_dir,
            (
                "27-display/summary.json",
                "display/summary.json",
                "display-summary.json",
            ),
        ),
    }


def _evaluate_docker_preflight(doc: EvidenceDocument | None, *, evidence_run_id: str) -> LaneEvaluation:
    if doc is None:
        return _missing_lane("docker_preflight", "TWO_NODE_E2E_DOCKER_PREFLIGHT_MISSING")
    payload = doc.payload
    status = _normalized_status(payload.get("status"))
    blockers = list(_stale_lane_blockers(payload))
    summary_status = str(payload.get("status", "unknown"))
    if status == STATUS_PASS:
        blockers.extend(_current_run_blockers(payload, evidence_run_id, lane_name="docker_preflight"))
        commands = payload.get("commands")
        if not isinstance(commands, Mapping):
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_DOCKER_PREFLIGHT_COMMANDS_MISSING",
                    "Docker preflight PASS must include live docker command evidence.",
                )
            )
        else:
            for command_name in (
                "docker_version",
                "docker_compose_version",
                "docker_info_docker_root",
                "docker_system_df",
            ):
                command = commands.get(command_name)
                if not isinstance(command, Mapping) or command.get("returncode") != 0:
                    blockers.append(
                        _blocker(
                            "TWO_NODE_E2E_DOCKER_PREFLIGHT_COMMAND_FAILED",
                            f"Docker preflight command {command_name} is missing or did not succeed.",
                            command=command_name,
                        )
                    )
        if not payload.get("docker_root_dir"):
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_DOCKER_ROOT_MISSING",
                    "Docker preflight PASS must record DockerRootDir.",
                )
            )
    return _lane_from_status(
        "docker_preflight",
        doc,
        status=STATUS_BLOCKED if blockers and status == STATUS_PASS else status,
        summary_status=summary_status,
        blockers=blockers,
    )


def _evaluate_docker_security(doc: EvidenceDocument | None, *, evidence_run_id: str) -> LaneEvaluation:
    if doc is None:
        return _missing_lane("docker_security", "TWO_NODE_E2E_DOCKER_SECURITY_MISSING")
    payload = doc.payload
    status = _normalized_status(payload.get("status"))
    blockers = list(_stale_lane_blockers(payload))
    findings: list[dict[str, Any]] = []
    summary_status = str(payload.get("status", "unknown"))
    if status == STATUS_PASS:
        blockers.extend(_current_run_blockers(payload, evidence_run_id, lane_name="docker_security"))
        if not _has_live_docker_evidence(payload):
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_DOCKER_LIVE_CONTAINER_EVIDENCE_MISSING",
                    "Docker display security PASS requires live Docker/container evidence.",
                )
            )
    runtime = _runtime_config(payload)
    if runtime:
        if runtime.get("service_role") != "display_readonly" or runtime.get("display_readonly") is not True:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_DISPLAY_RUNTIME_ROLE_INVALID",
                    "Display runtime config must report display_readonly.",
                    runtime_config=runtime,
                )
            )
        if runtime.get("slurm_routes_enabled") is not False:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_DISPLAY_SLURM_ROUTES_ENABLED",
                    "display_readonly runtime config must report Slurm routes disabled.",
                    runtime_config=runtime,
                )
            )
    if status == STATUS_PASS:
        if not runtime:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_DISPLAY_RUNTIME_ROLE_INVALID",
                    "Display runtime config must report display_readonly.",
                    runtime_config=runtime,
                )
            )
    if _bool_lookup(payload, "slurm_routes_unavailable") is False:
        findings.append(
            _finding(
                "TWO_NODE_E2E_DISPLAY_SLURM_ROUTE_AVAILABLE",
                "Display Docker evidence shows a Slurm route is reachable.",
            )
        )
    if _bool_lookup(payload, "published_artifacts_readonly") is False:
        findings.append(
            _finding(
                "TWO_NODE_E2E_DISPLAY_PUBLISHED_ARTIFACTS_WRITABLE",
                "Display Docker evidence does not prove readonly published artifacts.",
            )
        )
    for key in DOCKER_FORBIDDEN_BOOL_KEYS:
        value = _bool_lookup(payload, key)
        if value is True:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_DOCKER_DISPLAY_FORBIDDEN_CAPABILITY",
                    f"Display Docker evidence exposes forbidden capability {key}.",
                    capability=key,
                )
            )
    for finding in _payload_findings(payload):
        code = str(finding.get("code") or "")
        if any(token in code.upper() for token in DOCKER_FORBIDDEN_FINDING_TOKENS):
            findings.append(
                _finding(
                    "TWO_NODE_E2E_DOCKER_DISPLAY_SECURITY_FINDING",
                    "Display Docker evidence contains a forbidden security finding.",
                    source_code=code,
                )
            )
    forbidden = _first_mapping_value(payload, ("forbidden_capabilities", "capability_leaks"))
    if isinstance(forbidden, Sequence) and not isinstance(forbidden, str | bytes | bytearray) and forbidden:
        findings.append(
            _finding(
                "TWO_NODE_E2E_DOCKER_DISPLAY_FORBIDDEN_CAPABILITY",
                "Display Docker evidence lists forbidden capabilities.",
                capabilities=list(forbidden),
            )
        )
    if findings:
        status = STATUS_FAIL
    elif blockers and status == STATUS_PASS:
        status = STATUS_BLOCKED
    return _lane_from_status(
        "docker_security",
        doc,
        status=status,
        summary_status=summary_status,
        blockers=blockers,
        findings=findings,
    )


def _evaluate_readonly_db(doc: EvidenceDocument | None, *, evidence_run_id: str) -> LaneEvaluation:
    if doc is None:
        return _missing_lane("readonly_db", "TWO_NODE_E2E_READONLY_DB_SUMMARY_MISSING")
    payload = doc.payload
    status = _normalized_status(payload.get("status"))
    blockers = list(_stale_lane_blockers(payload))
    findings: list[dict[str, Any]] = []
    summary_status = str(payload.get("status", "unknown"))
    if payload.get("run_id") != evidence_run_id:
        findings.append(
            _finding(
                "TWO_NODE_E2E_READONLY_DB_STALE_RUN",
                "Readonly DB summary run_id must match the current evidence bundle.",
                expected_run_id=evidence_run_id,
                observed_run_id=payload.get("run_id"),
            )
        )
    if payload.get("schema") != READONLY_DB_LIVE_SCHEMA:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_READONLY_DB_LIVE_SCHEMA_MISSING",
                "Readonly DB PASS requires real live readonly DB evidence, not simulated or unknown evidence.",
                schema=payload.get("schema"),
            )
        )
    provenance = payload.get("validation_provenance", {})
    if not isinstance(provenance, Mapping) or provenance.get("live_readonly_proof") is not True:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_READONLY_DB_LIVE_PROOF_MISSING",
                "Readonly DB PASS requires live_readonly_proof=true.",
            )
        )
    database_url = payload.get("database_url")
    if not isinstance(database_url, str) or not database_url.strip() or not _database_url_is_redacted(database_url):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_READONLY_DB_REDACTED_DSN_MISSING",
                "Readonly DB evidence must include a redacted database URL.",
            )
        )
    role = payload.get("role", {})
    if not isinstance(role, Mapping) or not role.get("current_user"):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_READONLY_DB_ROLE_MISSING",
                "Readonly DB evidence must include current_user role evidence.",
            )
        )
    elif role.get("role_type") != "readonly_candidate":
        findings.append(
            _finding(
                "TWO_NODE_E2E_READONLY_DB_WRITER_ROLE",
                "Readonly DB evidence identifies a writer or mutating role.",
                role_type=role.get("role_type"),
            )
        )
    for key in ("route_smoke", "permission_probes", "manual_action_probes"):
        value = payload.get(key)
        if not isinstance(value, list) or not value:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_AUTHORITATIVE_EVIDENCE_MISSING",
                    f"Readonly DB evidence must include non-empty {key}.",
                    evidence_key=key,
                )
            )
    for operation in _permission_operations(payload):
        if operation.get("privilege_allowed") is True:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_READONLY_DB_MUTATING_PRIVILEGE",
                    "Readonly DB evidence contains a mutating catalog privilege.",
                    operation=operation.get("operation"),
                    reason=operation.get("reason"),
                )
            )
        if operation.get("execution_outcome") == "succeeded":
            findings.append(
                _finding(
                    "TWO_NODE_E2E_READONLY_DB_SUCCESSFUL_MUTATION_PROBE",
                    "Readonly DB evidence contains a successful DML/DDL probe.",
                    operation=operation.get("operation"),
                    reason=operation.get("reason"),
                )
            )
    sibling_blockers = _readonly_db_sibling_blockers(doc.path, evidence_run_id=evidence_run_id)
    blockers.extend(sibling_blockers)
    status = _combined_status([status], findings=findings, blockers=blockers)
    return _lane_from_status(
        "readonly_db",
        doc,
        status=status,
        summary_status=summary_status,
        blockers=blockers,
        findings=findings,
    )


def _evaluate_source_lane(
    name: str,
    doc: EvidenceDocument | None,
    *,
    declared_sources: tuple[str, ...],
    strict_identities: Mapping[str, Mapping[str, Any]],
    required_checks: tuple[str, ...],
    live_flag: str,
    require_job_id: bool = False,
    evidence_run_id: str,
) -> LaneEvaluation:
    if doc is None:
        return _missing_lane(name, f"TWO_NODE_E2E_{name.upper()}_EVIDENCE_MISSING")
    payload = doc.payload
    status = _normalized_status(payload.get("status"))
    blockers = list(_stale_lane_blockers(payload))
    findings: list[dict[str, Any]] = []
    partial_sources = False
    if _has_mock_or_fixture(payload):
        findings.append(
            _finding(
                f"TWO_NODE_E2E_{name.upper()}_MOCK_EVIDENCE",
                f"{name} evidence uses mock or deterministic fixture data.",
            )
        )
    if _has_historical_latest(payload):
        findings.append(
            _finding(
                f"TWO_NODE_E2E_{name.upper()}_HISTORICAL_LATEST",
                f"{name} evidence uses historical latest or source-only fallback.",
            )
        )
    if status == STATUS_PASS:
        blockers.extend(_current_run_blockers(payload, evidence_run_id, lane_name=name))
        if not _has_live_lane_evidence(payload, live_flag=live_flag):
            blockers.append(
                _blocker(
                    f"TWO_NODE_E2E_{name.upper()}_LIVE_EVIDENCE_MISSING",
                    f"{name} PASS requires live evidence.",
                )
            )
    records = _source_records(payload)
    missing_sources = [source for source in declared_sources if source not in records]
    for source in missing_sources:
        blockers.append(
            _blocker(
                f"TWO_NODE_E2E_{name.upper()}_SOURCE_MISSING",
                f"{name} evidence is missing declared source {source}.",
                source=source,
            )
        )
    for source in declared_sources:
        record = records.get(source)
        if record is None:
            continue
        source_status = _normalized_status(record.get("status"))
        if source_status == STATUS_FAIL:
            findings.append(
                _finding(
                    f"TWO_NODE_E2E_{name.upper()}_SOURCE_FAILED",
                    f"{name} source evidence failed.",
                    source=source,
                )
            )
        elif source_status == STATUS_BLOCKED:
            blockers.append(
                _blocker(
                    f"TWO_NODE_E2E_{name.upper()}_SOURCE_BLOCKED",
                    f"{name} source evidence is blocked.",
                    source=source,
                )
            )
        elif source_status == STATUS_PARTIAL:
            partial_sources = True
        _, identity_findings, identity_blockers = _identity_match_status(
            source,
            record,
            strict_identities,
            require_job_id=require_job_id,
        )
        findings.extend(
            _with_context(item, lane=name, source=source)
            for item in identity_findings
        )
        blockers.extend(
            _with_context(item, lane=name, source=source)
            for item in identity_blockers
        )
        check_results = _check_results(record)
        for check in required_checks:
            check_result = check_results.get(check)
            if check_result is None:
                blockers.append(
                    _blocker(
                        f"TWO_NODE_E2E_{name.upper()}_CHECK_MISSING",
                        f"{name} source evidence is missing required check {check}.",
                        source=source,
                        check=check,
                    )
                )
                continue
            check_status = _normalized_status(check_result.get("status"))
            if _has_mock_or_fixture(check_result):
                findings.append(
                    _finding(
                        f"TWO_NODE_E2E_{name.upper()}_MOCK_CHECK",
                        f"{name} check uses mock or fixture data.",
                        source=source,
                        check=check,
                    )
                )
            if _has_historical_latest(check_result):
                findings.append(
                    _finding(
                        f"TWO_NODE_E2E_{name.upper()}_HISTORICAL_CHECK",
                        f"{name} check uses historical latest or source-only fallback.",
                        source=source,
                        check=check,
                    )
                )
            if check_status == STATUS_FAIL:
                findings.append(
                    _finding(
                        f"TWO_NODE_E2E_{name.upper()}_CHECK_FAILED",
                        f"{name} required check failed.",
                        source=source,
                        check=check,
                    )
                )
            elif check_status != STATUS_PASS:
                blockers.append(
                    _blocker(
                        f"TWO_NODE_E2E_{name.upper()}_CHECK_BLOCKED",
                        f"{name} required check is not PASS.",
                        source=source,
                        check=check,
                        check_status=check_status,
                    )
                )
            _, check_findings, check_blockers = _identity_match_status(
                source,
                check_result,
                strict_identities,
                require_job_id=require_job_id or check == "job_logs",
            )
            findings.extend(
                _with_context(item, lane=name, source=source, check=check)
                for item in check_findings
            )
            blockers.extend(
                _with_context(item, lane=name, source=source, check=check)
                for item in check_blockers
            )
    if findings:
        status = STATUS_FAIL
    elif status == STATUS_PASS:
        if blockers:
            status = STATUS_BLOCKED
        elif partial_sources:
            status = STATUS_PARTIAL
    return _lane_from_status(
        name,
        doc,
        status=status,
        summary_status=str(payload.get("status", "unknown")),
        blockers=blockers,
        findings=findings,
    )


def _evaluate_simple_live_lane(
    name: str,
    doc: EvidenceDocument | None,
    *,
    live_flag: str,
    allowed_statuses: Sequence[str] = (STATUS_PASS,),
    evidence_run_id: str,
) -> LaneEvaluation:
    if doc is None:
        return _missing_lane(name, f"TWO_NODE_E2E_{name.upper()}_EVIDENCE_MISSING")
    payload = doc.payload
    status = _normalized_status(payload.get("status"), pass_aliases=tuple(allowed_statuses))
    blockers = list(_stale_lane_blockers(payload))
    findings: list[dict[str, Any]] = []
    if status == STATUS_PASS:
        blockers.extend(_current_run_blockers(payload, evidence_run_id, lane_name=name))
        if not _has_live_lane_evidence(payload, live_flag=live_flag):
            blockers.append(
                _blocker(
                    f"TWO_NODE_E2E_{name.upper()}_LIVE_EVIDENCE_MISSING",
                    f"{name} PASS requires live evidence.",
                )
            )
    if status == STATUS_PASS and _has_mock_or_fixture(payload):
        findings.append(
            _finding(
                f"TWO_NODE_E2E_{name.upper()}_MOCK_EVIDENCE",
                f"{name} evidence uses mock or deterministic fixture data.",
            )
        )
    if status == STATUS_PASS:
        if findings:
            status = STATUS_FAIL
        elif blockers:
            status = STATUS_BLOCKED
    return _lane_from_status(
        name,
        doc,
        status=status,
        summary_status=str(payload.get("status", "unknown")),
        blockers=blockers,
        findings=findings,
    )


def _evaluate_manual_ops(
    doc: EvidenceDocument | None,
    *,
    strict_identities: Mapping[str, Mapping[str, Any]],
    evidence_run_id: str,
) -> LaneEvaluation:
    if doc is None:
        return _missing_lane("manual_ops", "TWO_NODE_E2E_MANUAL_OPS_EVIDENCE_MISSING")
    payload = doc.payload
    status = _normalized_status(payload.get("status"))
    blockers = list(_stale_lane_blockers(payload))
    findings: list[dict[str, Any]] = []
    display_actions = _first_mapping_value(payload, ("display_actions", "display_action_probes", "readonly_actions"))
    if isinstance(display_actions, list):
        for action in display_actions:
            if not isinstance(action, Mapping):
                continue
            if _node_number(action) == "27" and _action_wrote_or_executed(action):
                findings.append(
                    _finding(
                        "TWO_NODE_E2E_MANUAL_OPS_27_MUTATION",
                        "27 display retry/cancel evidence executed or wrote a control action.",
                        action=action.get("action") or action.get("name"),
                    )
                )
            outcome = str(action.get("outcome") or action.get("result") or action.get("error_code") or "")
            if _node_number(action) == "27" and not _is_manual_action_outcome(outcome):
                findings.append(
                    _finding(
                        "TWO_NODE_E2E_MANUAL_OPS_27_NOT_FAIL_CLOSED",
                        "27 display retry/cancel evidence did not fail closed as manual action.",
                        action=action.get("action") or action.get("name"),
                        outcome=outcome,
                    )
                )
    receipts = _first_mapping_value(payload, ("control_receipts", "retry_cancel_receipts", "receipts"))
    actual_22_receipt_count = 0
    if isinstance(receipts, list):
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            producer = _node_number(receipt) or str(receipt.get("producer") or receipt.get("producer_role") or "")
            if _is_actual_control_receipt(receipt) and producer != "22":
                findings.append(
                    _finding(
                        "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PRODUCED_BY_27",
                        "Actual retry/cancel receipts must be produced by node 22.",
                        producer=producer,
                        action=receipt.get("action"),
                    )
                )
            elif _is_actual_control_receipt(receipt) and producer == "22":
                actual_22_receipt_count += 1
            source = _source_name(receipt.get("source") or receipt.get("source_id"))
            if source and source in strict_identities:
                _, identity_findings, identity_blockers = _identity_match_status(
                    source,
                    receipt,
                    strict_identities,
                    require_job_id=False,
                )
                findings.extend(_with_context(item, lane="manual_ops", source=source) for item in identity_findings)
                blockers.extend(_with_context(item, lane="manual_ops", source=source) for item in identity_blockers)
    if status == STATUS_PASS:
        blockers.extend(_current_run_blockers(payload, evidence_run_id, lane_name="manual_ops"))
        if _bool_lookup(payload, "production_operator_auth_evidence") is not True:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_PRODUCTION_AUTH_MISSING",
                    "Manual ops PASS requires real production operator auth evidence.",
                )
            )
        if not isinstance(display_actions, list) or not display_actions:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_ACTIONS_MISSING",
                    "Manual ops evidence must include 27 display retry/cancel fail-closed probes.",
                )
            )
        if isinstance(receipts, list):
            if actual_22_receipt_count == 0:
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_MANUAL_OPS_22_RECEIPT_MISSING",
                        "Manual ops PASS requires actual retry/cancel receipt evidence produced by node 22.",
                    )
                )
        elif receipts is None:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPTS_MISSING",
                    "Manual ops PASS requires explicit receipt evidence or an empty receipt list.",
                )
            )
    if findings:
        status = STATUS_FAIL
    elif status == STATUS_PASS and blockers:
        status = STATUS_BLOCKED
    return _lane_from_status(
        "manual_ops",
        doc,
        status=status,
        summary_status=str(payload.get("status", "unknown")),
        blockers=blockers,
        findings=findings,
    )


def _evaluate_cross_plane(
    doc: EvidenceDocument | None,
    *,
    declared_sources: tuple[str, ...],
    strict_identities: Mapping[str, Mapping[str, Any]],
    source_scope_results: Mapping[str, Mapping[str, Any]],
    reduced_scope: bool,
    evidence_run_id: str,
) -> LaneEvaluation:
    if doc is None:
        return _missing_lane("cross_plane", "TWO_NODE_E2E_CROSS_PLANE_EVIDENCE_MISSING")
    payload = doc.payload
    status = _normalized_status(payload.get("status"))
    blockers = list(_stale_lane_blockers(payload))
    findings: list[dict[str, Any]] = []
    if status == STATUS_PASS:
        blockers.extend(_current_run_blockers(payload, evidence_run_id, lane_name="cross_plane"))
    if _has_mock_or_fixture(payload):
        findings.append(
            _finding(
                "TWO_NODE_E2E_CROSS_PLANE_MOCK_EVIDENCE",
                "Cross-plane evidence uses mock or deterministic fixture data.",
            )
        )
    if _has_historical_latest(payload):
        findings.append(
            _finding(
                "TWO_NODE_E2E_CROSS_PLANE_HISTORICAL_LATEST",
                "Cross-plane evidence uses historical latest or source-only fallback.",
            )
        )
    if status == STATUS_PASS and not _has_live_lane_evidence(payload, live_flag="live_cross_plane_evidence"):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_CROSS_PLANE_LIVE_EVIDENCE_MISSING",
                "Cross-plane PASS requires live identity-bound evidence.",
            )
        )
    records = _source_records(payload)
    for source in declared_sources:
        record = records.get(source)
        if record is None:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_CROSS_PLANE_SOURCE_MISSING",
                    "Cross-plane evidence is missing a declared source.",
                    source=source,
                )
            )
            continue
        _, identity_findings, identity_blockers = _identity_match_status(source, record, strict_identities)
        findings.extend(_with_context(item, lane="cross_plane", source=source) for item in identity_findings)
        blockers.extend(_with_context(item, lane="cross_plane", source=source) for item in identity_blockers)
    source_statuses = {source: result.get("status") for source, result in source_scope_results.items()}
    if any(value == STATUS_FAIL for value in source_statuses.values()):
        status = STATUS_FAIL
    if findings:
        status = STATUS_FAIL
    elif blockers:
        status = STATUS_BLOCKED
    elif not _is_full_scope_pass(declared_sources, source_scope_results) or reduced_scope:
        status = STATUS_PARTIAL
    return _lane_from_status(
        "cross_plane",
        doc,
        status=status,
        summary_status=str(payload.get("status", "unknown")),
        blockers=blockers,
        findings=findings,
    )


def _source_scope_results(
    *,
    declared_sources: tuple[str, ...],
    strict_identities: Mapping[str, Mapping[str, Any]],
    source_lanes: Mapping[str, LaneEvaluation],
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for source in declared_sources:
        blockers: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        identity = dict(strict_identities.get(source, {}))
        missing_identity = [field for field in STRICT_LOG_IDENTITY_FIELDS if not _identity_value(identity, field)]
        if missing_identity:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_SOURCE_STRICT_IDENTITY_INCOMPLETE",
                    "Source strict identity is incomplete.",
                    source=source,
                    missing_fields=missing_identity,
                )
            )
        lane_statuses: dict[str, str] = {}
        for lane_name, lane in source_lanes.items():
            lane_statuses[lane_name] = lane.status
            for finding in lane.findings:
                if finding.get("source") == source:
                    findings.append(dict(finding))
            for blocker in lane.blockers:
                if blocker.get("source") == source:
                    blockers.append(dict(blocker))
        status = _combined_status(
            [str(value) for value in lane_statuses.values()],
            findings=findings,
            blockers=blockers,
        )
        results[source] = {
            "status": status,
            "identity": redact_payload(identity),
            "lane_statuses": lane_statuses,
            "blockers": blockers,
            "findings": findings,
        }
    return results


def _final_status(
    lanes: Mapping[str, LaneEvaluation],
    source_scope_results: Mapping[str, Mapping[str, Any]],
    scope: Mapping[str, Any],
) -> str:
    lane_statuses = [lane.status for lane in lanes.values()]
    source_statuses = [str(result.get("status")) for result in source_scope_results.values()]
    if STATUS_FAIL in lane_statuses or STATUS_FAIL in source_statuses:
        return STATUS_FAIL
    if STATUS_BLOCKED in lane_statuses:
        return STATUS_BLOCKED
    if not _is_full_scope_pass(tuple(scope["declared_sources"]), source_scope_results):
        return STATUS_PARTIAL
    if STATUS_PARTIAL in lane_statuses or STATUS_PARTIAL in source_statuses:
        return STATUS_PARTIAL
    return STATUS_PASS


def _collect_blockers_and_findings(
    lanes: Mapping[str, LaneEvaluation],
    source_scope_results: Mapping[str, Mapping[str, Any]],
    scope: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    if not scope["declared_sources"]:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DECLARED_SOURCES_MISSING",
                "Final evidence requires declared source scope.",
            )
        )
    if not _is_full_scope_sources(tuple(scope["declared_sources"])):
        findings.append(
            _finding(
                "TWO_NODE_E2E_REDUCED_SOURCE_SCOPE",
                "Final evidence is not full GFS/IFS scope and cannot be full PASS.",
                declared_sources=list(scope["declared_sources"]),
                reduced_scope=scope["reduced_scope"],
            )
        )
    for lane_name, lane in lanes.items():
        for blocker in lane.blockers:
            blockers.append(_with_context(blocker, lane=lane_name))
        for finding in lane.findings:
            findings.append(_with_context(finding, lane=lane_name))
        if lane.status == STATUS_BLOCKED:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_LANE_BLOCKED",
                    f"Required lane {lane_name} is BLOCKED.",
                    lane=lane_name,
                )
            )
        elif lane.status == STATUS_FAIL:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_LANE_FAILED",
                    f"Required lane {lane_name} failed.",
                    lane=lane_name,
                )
            )
    for source, result in source_scope_results.items():
        if result.get("status") == STATUS_BLOCKED:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_SOURCE_BLOCKED",
                    "Declared source scope is blocked.",
                    source=source,
                )
            )
        elif result.get("status") == STATUS_FAIL:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_SOURCE_FAILED",
                    "Declared source scope failed.",
                    source=source,
                )
            )
    return blockers, findings


def _resolve_scope(
    config: TwoNodeE2EEvidenceConfig,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    declared = config.declared_sources
    if not declared:
        declared = _sources_from_value(
            metadata.get("declared_sources")
            or metadata.get("sources")
            or metadata.get("source_scope")
            or metadata.get("source_scope_results")
        )
    if not declared:
        declared = _sources_from_value(
            metadata.get("strict_identities")
            or metadata.get("source_identities")
            or _nested_get(metadata, ("strict_identity", "sources"))
        )
    reduced_scope_value = config.reduced_scope
    if reduced_scope_value is None:
        reduced_scope_value = _optional_bool(metadata.get("reduced_scope"))
    if reduced_scope_value is None:
        reduced_scope_value = str(metadata.get("scope") or "").lower() in {"reduced", "single_source"}
    reduced_declared = config.reduced_scope is not None or "reduced_scope" in metadata or "scope" in metadata
    if declared and not _is_full_scope_sources(declared):
        reduced_scope_value = True
    return {
        "declared_sources": declared,
        "reduced_scope": bool(reduced_scope_value),
        "reduced_scope_declared": bool(reduced_declared),
    }


def _resolve_strict_identities(
    metadata: Mapping[str, Any],
    *,
    declared_sources: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    raw = (
        metadata.get("strict_identities")
        or metadata.get("source_identities")
        or _nested_get(metadata, ("strict_identity", "sources"))
        or {}
    )
    identities: dict[str, dict[str, Any]] = {}
    if isinstance(raw, Mapping):
        for source, value in raw.items():
            source_name = _source_name(source)
            if source_name and isinstance(value, Mapping):
                identity = dict(value)
                identity.setdefault("source", source_name)
                identities[source_name] = identity
    elif isinstance(raw, list):
        for value in raw:
            if isinstance(value, Mapping):
                source_name = _source_name(value.get("source") or value.get("source_id"))
                if source_name:
                    identities[source_name] = dict(value)
    for source in declared_sources:
        identities.setdefault(source, {"source": source})
    return identities


def _metadata_summary(doc: EvidenceDocument | None, metadata: Mapping[str, Any]) -> dict[str, Any]:
    if doc is None:
        return {
            "status": STATUS_BLOCKED,
            "evidence_path": None,
            "schema": None,
            "reason": "No run metadata/identity file was found.",
        }
    schema = metadata.get("schema")
    status = STATUS_PASS if schema in RUN_METADATA_SCHEMAS or schema is None else STATUS_BLOCKED
    return {
        "status": status,
        "evidence_path": _public_path(doc.path),
        "evidence_sha256": doc.sha256,
        "schema": schema,
    }


def _find_first_json(run_dir: Path, candidates: Sequence[str]) -> EvidenceDocument | None:
    for relative in candidates:
        path = run_dir / relative
        try:
            st = stat_no_follow(path, containment_root=run_dir)
        except FileNotFoundError:
            continue
        except SafeFilesystemError as error:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE",
                f"Unsafe evidence path {path}: {error}",
            ) from error
        if not stat.S_ISREG(st.st_mode):
            continue
        return _read_json(path, containment_root=run_dir)
    return None


def _read_json(path: Path, *, containment_root: Path) -> EvidenceDocument:
    content = _read_json_bytes(path, containment_root=containment_root)
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TwoNodeE2EEvidenceError(
            "TWO_NODE_E2E_EVIDENCE_JSON_INVALID",
            f"Evidence file is not valid UTF-8 JSON: {path}.",
        ) from error
    if not isinstance(payload, Mapping):
        raise TwoNodeE2EEvidenceError(
            "TWO_NODE_E2E_EVIDENCE_JSON_INVALID",
            f"Evidence JSON must be an object: {path}.",
        )
    return EvidenceDocument(
        path=path.resolve(strict=False),
        payload=payload,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _read_json_value(path: Path, *, containment_root: Path) -> Any:
    content = _read_json_bytes(path, containment_root=containment_root)
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TwoNodeE2EEvidenceError(
            "TWO_NODE_E2E_EVIDENCE_JSON_INVALID",
            f"Evidence file is not valid UTF-8 JSON: {path}.",
        ) from error


def _read_json_bytes(path: Path, *, containment_root: Path) -> bytes:
    try:
        content = read_bytes_limited_no_follow(
            path,
            max_bytes=MAX_EVIDENCE_PAYLOAD_BYTES,
            containment_root=containment_root,
        )
    except SafeFilesystemError as error:
        raise TwoNodeE2EEvidenceError(
            "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE",
            f"Unsafe evidence file {path}: {error}",
        ) from error
    if len(content) > MAX_EVIDENCE_PAYLOAD_BYTES:
        raise TwoNodeE2EEvidenceError(
            "TWO_NODE_E2E_EVIDENCE_PAYLOAD_TOO_LARGE",
            f"Evidence file exceeds {MAX_EVIDENCE_PAYLOAD_BYTES} bytes: {path}.",
        )
    return content


def _missing_lane(name: str, code: str) -> LaneEvaluation:
    return LaneEvaluation(
        name=name,
        status=STATUS_BLOCKED,
        blockers=(
            _blocker(
                code,
                f"Required final two-node evidence lane {name} is missing.",
            ),
        ),
    )


def _lane_from_status(
    name: str,
    doc: EvidenceDocument,
    *,
    status: str,
    summary_status: str,
    blockers: Sequence[Mapping[str, Any]] = (),
    findings: Sequence[Mapping[str, Any]] = (),
) -> LaneEvaluation:
    return LaneEvaluation(
        name=name,
        status=status,
        evidence_path=_public_path(doc.path),
        evidence_sha256=doc.sha256,
        summary_status=summary_status,
        blockers=tuple(dict(item) for item in blockers),
        findings=tuple(dict(item) for item in findings),
        evidence=redact_payload(_bounded_evidence_payload(doc.payload)),
    )


def _normalized_status(value: Any, *, pass_aliases: Sequence[str] = (STATUS_PASS,)) -> str:
    text = str(value or "unknown").strip()
    upper = text.upper()
    alias_upper = {str(item).upper() for item in pass_aliases}
    if upper in alias_upper or text in pass_aliases:
        return STATUS_PASS
    if upper in {STATUS_PARTIAL, "READY_WITH_WARNINGS", "REDUCED_SCOPE"}:
        return STATUS_PARTIAL
    if upper in {STATUS_FAIL, "FAILED", "FAILURE", "ERROR", "RELEASE_BLOCKED"}:
        return STATUS_FAIL
    if upper in {STATUS_BLOCKED, "BLOCK", "SKIPPED", "MISSING", "NOT_EXECUTED", "UNKNOWN"}:
        return STATUS_BLOCKED
    return STATUS_BLOCKED


def _combined_status(
    statuses: Sequence[str],
    *,
    findings: Sequence[Mapping[str, Any]] = (),
    blockers: Sequence[Mapping[str, Any]] = (),
) -> str:
    if findings or STATUS_FAIL in statuses:
        return STATUS_FAIL
    if blockers or STATUS_BLOCKED in statuses:
        return STATUS_BLOCKED
    if STATUS_PARTIAL in statuses:
        return STATUS_PARTIAL
    return STATUS_PASS


def _source_records(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = (
        payload.get("sources")
        or payload.get("source_results")
        or payload.get("source_scope_results")
        or _nested_get(payload, ("strict_identity", "sources"))
    )
    records: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw, Mapping):
        for source, record in raw.items():
            source_name = _source_name(source)
            if not source_name:
                continue
            if isinstance(record, Mapping):
                item = dict(record)
                item.setdefault("source", source_name)
                records[source_name] = item
    elif isinstance(raw, list):
        for record in raw:
            if not isinstance(record, Mapping):
                continue
            source_name = _source_name(record.get("source") or record.get("source_id"))
            if source_name:
                records[source_name] = record
    else:
        source_name = _source_name(payload.get("source") or payload.get("source_id"))
        if source_name:
            records[source_name] = payload
    return records


def _check_results(record: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = record.get("checks") or record.get("check_results") or {}
    checks: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw, Mapping):
        for name, value in raw.items():
            if isinstance(value, Mapping):
                checks[str(name)] = value
            else:
                checks[str(name)] = {"status": value}
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, Mapping):
                name = str(item.get("name") or item.get("check") or "")
                if name:
                    checks[name] = item
    return checks


def _identity_match_status(
    source: str,
    record: Mapping[str, Any],
    strict_identities: Mapping[str, Mapping[str, Any]],
    *,
    require_job_id: bool = False,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    expected = strict_identities.get(source, {})
    observed = _record_identity(record)
    required_fields = STRICT_LOG_IDENTITY_FIELDS if require_job_id else STRICT_IDENTITY_FIELDS
    findings: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    missing_expected = [field for field in required_fields if not _identity_value(expected, field)]
    missing_observed = [field for field in required_fields if not _identity_value(observed, field)]
    if missing_expected:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE",
                "Expected strict identity is incomplete.",
                missing_fields=missing_expected,
            )
        )
    if missing_observed:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE",
                "Observed strict identity evidence is incomplete.",
                missing_fields=missing_observed,
            )
        )
    for identity_field in required_fields:
        expected_value = _identity_value(expected, identity_field)
        observed_value = _identity_value(observed, identity_field)
        if not expected_value or not observed_value:
            continue
        if identity_field == "source":
            matches = _source_name(expected_value) == _source_name(observed_value)
        else:
            matches = str(expected_value) == str(observed_value)
        if not matches:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_STRICT_IDENTITY_MISMATCH",
                    "Observed evidence identity does not match the strict 22-produced identity.",
                    field=identity_field,
                    expected=expected_value,
                    observed=observed_value,
                )
            )
    status = _combined_status([], findings=findings, blockers=blockers)
    return status, findings, blockers


def _record_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("identity") or record.get("strict_identity") or record.get("lineage") or {}
    identity = dict(raw) if isinstance(raw, Mapping) else {}
    for identity_field in STRICT_LOG_IDENTITY_FIELDS:
        if identity_field in record and identity_field not in identity:
            identity[identity_field] = record[identity_field]
    if "source_id" in record and "source" not in identity:
        identity["source"] = record["source_id"]
    if "source_id" in identity and "source" not in identity:
        identity["source"] = identity["source_id"]
    return identity


def _permission_operations(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    operations = []
    probes = payload.get("permission_probes", [])
    if isinstance(probes, list):
        for probe in probes:
            if isinstance(probe, Mapping):
                raw_operations = probe.get("operations", [])
                if isinstance(raw_operations, list):
                    operations.extend(item for item in raw_operations if isinstance(item, Mapping))
    return operations


def _readonly_db_sibling_blockers(summary_path: Path, *, evidence_run_id: str) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    lane_dir = summary_path.parent
    for filename in ("role.json", "route_smoke.json", "permission_probes.json"):
        path = lane_dir / filename
        try:
            sibling_stat = stat_no_follow(path, containment_root=lane_dir)
        except FileNotFoundError:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_AUTHORITATIVE_FILE_MISSING",
                    "Readonly DB PASS requires current authoritative sibling evidence files.",
                    filename=filename,
                )
            )
            continue
        except SafeFilesystemError as error:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE",
                f"Unsafe readonly DB sibling evidence path {path}: {error}",
            ) from error
        if not stat.S_ISREG(sibling_stat.st_mode):
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_AUTHORITATIVE_FILE_INVALID",
                    "Readonly DB authoritative sibling evidence must be a regular JSON file.",
                    filename=filename,
                )
            )
            continue
        sibling_payload = _read_json_value(path, containment_root=lane_dir)
        if isinstance(sibling_payload, Mapping):
            for key, value in _explicit_bundle_run_ids(sibling_payload):
                if str(value) != evidence_run_id:
                    blockers.append(
                        _blocker(
                            "TWO_NODE_E2E_READONLY_DB_AUTHORITATIVE_FILE_STALE",
                            "Readonly DB authoritative sibling evidence belongs to an older evidence run.",
                            filename=filename,
                            key=key,
                            evidence_run_id=value,
                            expected_evidence_run_id=evidence_run_id,
                        )
                    )
    return blockers


def _has_live_docker_evidence(payload: Mapping[str, Any]) -> bool:
    if (
        _bool_lookup(payload, "live_docker_evidence") is True
        or _bool_lookup(payload, "live_container_evidence") is True
    ):
        return True
    if str(payload.get("execution_mode") or "").lower() in LIVE_EXECUTION_MODES:
        return True
    commands = payload.get("commands")
    if not isinstance(commands, Mapping):
        return False
    required_success = ("image_absence_probe", "display_startup_start", "display_startup_probe")
    return all(
        isinstance(commands.get(name), Mapping) and commands[name].get("returncode") == 0
        for name in required_success
    )


def _has_live_lane_evidence(payload: Mapping[str, Any], *, live_flag: str) -> bool:
    if _bool_lookup(payload, live_flag) is True:
        return True
    if _bool_lookup(payload, "live_evidence") is True:
        return True
    mode = str(payload.get("execution_mode") or payload.get("mode") or "").lower()
    return mode in LIVE_EXECUTION_MODES


def _runtime_config(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = (
        payload.get("runtime_config")
        or payload.get("runtime")
        or _nested_get(payload, ("checks", "runtime_config"))
        or _nested_get(payload, ("display", "runtime_config"))
        or {}
    )
    if isinstance(value, Mapping):
        data = value.get("data")
        if isinstance(data, Mapping):
            merged = dict(value)
            merged.update(data)
            return merged
        return dict(value)
    return {}


def _payload_findings(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    findings = payload.get("findings")
    if isinstance(findings, list):
        return [item for item in findings if isinstance(item, Mapping)]
    return []


def _stale_lane_blockers(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence_run_id = payload.get("evidence_run_id") or payload.get("bundle_run_id")
    expected_run_id = payload.get("expected_evidence_run_id")
    blockers: list[dict[str, Any]] = []
    if expected_run_id is not None and evidence_run_id is not None and str(evidence_run_id) != str(expected_run_id):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
                "Evidence run identifier does not match its expected current bundle.",
                evidence_run_id=evidence_run_id,
                expected_evidence_run_id=expected_run_id,
            )
        )
    return blockers


def _current_run_blockers(payload: Mapping[str, Any], evidence_run_id: str, *, lane_name: str) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    explicit_ids = _explicit_bundle_run_ids(payload)
    if not explicit_ids:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_MISSING",
                "PASS lane evidence must include evidence_run_id or bundle_run_id for stale-run protection.",
                lane=lane_name,
                expected_evidence_run_id=evidence_run_id,
            )
        )
        return blockers
    for key, value in explicit_ids:
        if str(value) != evidence_run_id:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_MISMATCH",
                    "PASS lane evidence belongs to a different evidence bundle.",
                    lane=lane_name,
                    key=key,
                    evidence_run_id=value,
                    expected_evidence_run_id=evidence_run_id,
                )
            )
    return blockers


def _explicit_bundle_run_ids(payload: Mapping[str, Any]) -> list[tuple[str, Any]]:
    result: list[tuple[str, Any]] = []
    for key in CURRENT_EVIDENCE_RUN_ID_KEYS:
        value = payload.get(key)
        if value is not None and str(value).strip():
            result.append((key, value))
    return result


def _database_url_is_redacted(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.username or parsed.password:
        return False
    if parsed.query or parsed.fragment:
        return False
    lowered = value.lower()
    return not any(token in lowered for token in ("password=", "token=", "secret=", "access_key", "signature="))


def _has_mock_or_fixture(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            if key_text in MOCK_KEYS and nested is True:
                return True
            if key_text == "execution_mode" and str(nested).lower() in {
                "mock",
                "mocked",
                "deterministic_fixture",
                "fixture",
                "fixture_only",
            }:
                return True
            if _has_mock_or_fixture(nested):
                return True
    elif isinstance(value, list):
        return any(_has_mock_or_fixture(item) for item in value)
    return False


def _has_historical_latest(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            if key_text in HISTORICAL_KEYS and nested is True:
                return True
            if key_text in {"latest_mode", "selection_mode"} and str(nested).lower() in {
                "historical_latest",
                "source_only",
                "fallback_latest",
            }:
                return True
            if _has_historical_latest(nested):
                return True
    elif isinstance(value, list):
        return any(_has_historical_latest(item) for item in value)
    return False


def _bool_lookup(payload: Mapping[str, Any], key: str) -> bool | None:
    direct = payload.get(key)
    if isinstance(direct, bool):
        return direct
    for nested_key in ("checks", "security", "display", "runtime", "capabilities", "mounts"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            value = nested.get(key)
            if isinstance(value, bool):
                return value
    return None


def _first_mapping_value(payload: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _nested_get(payload: Mapping[str, Any], keys: Sequence[str]) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _sources_from_value(value: Any) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        if "declared_sources" in value:
            return _sources_from_value(value.get("declared_sources"))
        return _dedupe_sources(_source_name(key) for key in value.keys())
    if isinstance(value, str):
        return _dedupe_sources(_split_sources(value))
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        sources = []
        for item in value:
            if isinstance(item, Mapping):
                sources.append(_source_name(item.get("source") or item.get("source_id")))
            else:
                sources.append(_source_name(item))
        return _dedupe_sources(sources)
    return ()


def _split_sources(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in re.split(r"[, ]+", value) if part.strip())


def _dedupe_sources(sources: Sequence[str | None]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for source in sources:
        source_name = _source_name(source)
        if source_name and source_name not in seen:
            seen.add(source_name)
            result.append(source_name)
    return tuple(result)


def _source_name(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.upper()


def _identity_value(identity: Mapping[str, Any], field: str) -> str | None:
    value = identity.get(field)
    if value is None and field == "source":
        value = identity.get("source_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _browser_required_checks(declared_sources: tuple[str, ...]) -> tuple[str, ...]:
    checks = ["hydro_met", "ops"]
    if len(declared_sources) > 1:
        checks.append("source_switch")
    return tuple(checks)


def _is_full_scope_sources(declared_sources: tuple[str, ...]) -> bool:
    return frozenset(declared_sources) == FULL_PASS_SOURCE_SET


def _is_full_scope_pass(
    declared_sources: tuple[str, ...],
    source_scope_results: Mapping[str, Mapping[str, Any]],
) -> bool:
    return _is_full_scope_sources(declared_sources) and all(
        source_scope_results.get(source, {}).get("status") == STATUS_PASS
        for source in sorted(FULL_PASS_SOURCE_SET)
    )


def _action_wrote_or_executed(action: Mapping[str, Any]) -> bool:
    return any(
        action.get(key) is True
        for key in (
            "write_executed",
            "db_write_executed",
            "control_executed",
            "gateway_called",
            "receipt_created",
        )
    )


def _is_manual_action_outcome(outcome: str) -> bool:
    lowered = outcome.lower()
    return any(
        marker in lowered
        for marker in (
            "manual_action",
            "control_plane_manual_action_required",
            "auth_required",
            "not_authorized",
            "forbidden",
            "401",
            "403",
            "409",
        )
    )


def _is_actual_control_receipt(receipt: Mapping[str, Any]) -> bool:
    if receipt.get("actual") is True or receipt.get("receipt_created") is True:
        return True
    kind = str(receipt.get("type") or receipt.get("receipt_type") or "").lower()
    action = str(receipt.get("action") or "").lower()
    return kind in {"retry", "cancel", "control"} or action in {"retry", "cancel"}


def _node_number(value: Mapping[str, Any]) -> str | None:
    node = value.get("node") or value.get("producer_node") or value.get("host_node")
    if node is None:
        role = str(value.get("producer_role") or value.get("service_role") or "").lower()
        if role == "compute_control":
            return "22"
        if role == "display_readonly":
            return "27"
        return None
    text = str(node)
    if "22" in text or text.lower() in {"compute", "compute_control"}:
        return "22"
    if "27" in text or text.lower() in {"display", "display_readonly"}:
        return "27"
    return text


def _blocker(code: str, message: str, **details: Any) -> dict[str, Any]:
    payload = {"code": code, "message": redact_text(message)}
    payload.update({key: redact_payload(value) for key, value in details.items()})
    return payload


def _finding(code: str, message: str, **details: Any) -> dict[str, Any]:
    payload = {"code": code, "message": redact_text(message)}
    payload.update({key: redact_payload(value) for key, value in details.items()})
    return payload


def _with_context(item: Mapping[str, Any], **context: Any) -> dict[str, Any]:
    merged = dict(item)
    for key, value in context.items():
        merged.setdefault(key, value)
    return merged


def _bounded_evidence_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return payload


def _safe_resolved_evidence_root(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    approved_roots = tuple(root.expanduser().resolve(strict=False) for root in APPROVED_EVIDENCE_ROOTS)
    for root in approved_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise TwoNodeE2EEvidenceError(
        "TWO_NODE_E2E_EVIDENCE_ROOT_UNAPPROVED",
        "Two-node E2E evidence root must be under repository artifacts/ or /scratch/frd_muziyao.",
    )


def _refuse_symlink_components(path: Path) -> None:
    current = path.expanduser()
    for component in (current, *current.parents):
        if component.exists() and component.is_symlink():
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE",
                f"Evidence path component must not be a symlink: {component}.",
            )


def _safe_run_id(value: str) -> str:
    text = value.strip()
    if not SAFE_RUN_ID_RE.fullmatch(text) or ".." in text:
        raise TwoNodeE2EEvidenceError(
            "TWO_NODE_E2E_RUN_ID_UNSAFE",
            "run_id must be a bounded alphanumeric identifier using only '.', '_' or '-'.",
        )
    return text


def _default_run_id() -> str:
    return f"two-node-e2e-final-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def _path_env(name: str, default: Path) -> Path:
    value = os.getenv(name, "").strip()
    return Path(value) if value else default


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "on", "reduced"}:
        return True
    if text in {"0", "false", "no", "off", "full"}:
        return False
    return None


def _public_path(path: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        return str(resolved)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate final two-node E2E evidence closure.")
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=None,
        help="Root directory containing two-node E2E evidence bundles.",
    )
    parser.add_argument("--run-id", help="Evidence bundle ID under --evidence-root.")
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        help="Declared source for the final source scope. Can be repeated.",
    )
    parser.add_argument(
        "--reduced-scope",
        action="store_true",
        default=None,
        help="Declare this evidence bundle as reduced source scope.",
    )
    parser.add_argument(
        "--full-scope",
        action="store_false",
        dest="reduced_scope",
        help="Declare this evidence bundle as intended full source scope.",
    )
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        summary = validate_two_node_e2e_evidence(
            TwoNodeE2EEvidenceConfig.from_env(
                evidence_root=args.evidence_root,
                run_id=args.run_id,
                declared_sources=args.sources,
                reduced_scope=args.reduced_scope,
                force=args.force,
            )
        )
    except TwoNodeE2EEvidenceError as error:
        print(f"{error.error_code}: {redact_text(error.message)}", file=sys.stderr)
        return 1
    print(json.dumps(redact_payload(summary), sort_keys=True))
    if summary.get("status") == STATUS_PASS:
        return 0
    if summary.get("status") in {STATUS_PARTIAL, STATUS_BLOCKED}:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
