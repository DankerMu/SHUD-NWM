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
from urllib.parse import parse_qs, unquote, urlsplit

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
DOCKER_SECURITY_SUMMARY_SCHEMA = "nhms.two_node_docker.security_summary.v1"
DOCKER_SECURITY_CHILD_SCHEMAS: Mapping[str, str] = {
    "source_trust": "nhms.two_node_docker.source_trust.v1",
    "static": "nhms.two_node_docker.static_check.v1",
    "smoke": "nhms.two_node_docker.app_smoke.v1",
}
MANUAL_OPS_SCHEMA = "nhms.two_node_e2e.manual_ops.v1"

FINAL_REQUIRED_LANES = (
    "metadata",
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
DOCKER_PREFLIGHT_SCHEMA = "nhms.two_node_docker.preflight.v1"
DOCKER_PREFLIGHT_REQUIRED_COMMANDS = (
    "docker_version",
    "docker_compose_version",
    "docker_info_docker_root",
    "docker_system_df",
    "df_h",
)
DOCKER_PREFLIGHT_REQUIRED_DISK_LABELS = ("evidence_root", "tmpdir", "docker_root")
DOCKER_REQUIRED_FALSE_PROOFS: Mapping[str, tuple[str, ...]] = {
    "slurm_routes_enabled": ("slurm_routes_enabled",),
    "slurm_route_available": ("slurm_route_available",),
    "slurm_cli_present": ("slurm_cli_present",),
    "slurm_config_present": ("slurm_config_present",),
    "slurm_socket_present": ("slurm_socket_present",),
    "munge_path_present": ("munge_path_present", "munge_socket_present"),
    "docker_socket_present": ("docker_socket_present", "docker_socket_mount_present"),
    "privileged": ("privileged", "hostconfig_privileged", "display_privileged"),
    "host_network": ("host_network", "host_network_mode", "network_mode_host"),
    "host_pid": ("host_pid", "host_pid_mode", "pid_mode_host"),
    "host_ipc": ("host_ipc", "host_ipc_mode", "ipc_mode_host"),
    "cap_add_present": ("cap_add_present", "capabilities_added", "linux_capabilities_added"),
    "forbidden_hostconfig_hazard": ("forbidden_hostconfig_hazard",),
    "forbidden_mount_hazard": ("forbidden_mount_hazard", "forbidden_mount_present"),
    "forbidden_env_hazard": ("forbidden_env_hazard", "forbidden_env_present"),
    "broad_host_bind_present": ("broad_host_bind_present", "broad_bind_present"),
    "private_workspace_bind_present": ("private_workspace_bind_present", "private_workspace_mount_present"),
    "workspace_mount_present": ("workspace_mount_present", "workspace_bind_present"),
    "writable_published_artifact_mount": (
        "writable_published_artifact_mount",
        "published_artifact_mount_writable",
    ),
    "display_write_capability_present": ("display_write_capability_present", "control_mutations_enabled"),
}
DOCKER_REQUIRED_TRUE_PROOFS: Mapping[str, tuple[str, ...]] = {
    "published_artifacts_readonly": ("published_artifacts_readonly", "published_artifact_mount_readonly"),
    "root_filesystem_readonly": ("root_filesystem_readonly", "readonly_root_filesystem", "read_only_root_filesystem"),
    "cap_drop_all": ("cap_drop_all", "all_capabilities_dropped"),
}
DOCKER_DISPLAY_FORBIDDEN_ENV_KEYS = frozenset(
    {
        "SLURM_GATEWAY_URL",
        "SLURM_GATEWAY_BACKEND",
        "SLURM_GATEWAY_TEMPLATE_DIR",
        "SLURM_GATEWAY_WORKSPACE_DIR",
        "WORKSPACE_ROOT",
        "RUN_WORKSPACE_ROOT",
        "SHARED_LOG_ROOT",
        "OBJECT_STORE_ROOT",
        "NHMS_BASINS_ROOT",
        "NHMS_MODEL_ASSET_ROOT",
        "SHUD_EXECUTABLE",
        "MUNGE_SOCKET",
        "MUNGE_KEY",
        "DOCKER_HOST",
    }
)
DOCKER_FORBIDDEN_MOUNT_TOKENS = (
    "/etc/slurm",
    "/etc/munge",
    "/run/munge",
    "/var/run/munge",
    "munge.key",
    ".nhms-runs",
    "WORKSPACE_ROOT",
    "NHMS_BASINS_ROOT",
    "NHMS_MODEL_ASSET_ROOT",
    "MUNGE_SOCKET",
    "MUNGE_KEY",
)
DOCKER_BROAD_HOST_ROOTS = frozenset({"/", "/root", "/home", "/etc", "/run", "/var", "/scratch"})
MANUAL_OPS_REQUIRED_DISPLAY_ACTIONS = frozenset({"retry", "cancel"})
MANUAL_OPS_SIDE_EFFECT_CATEGORIES: Mapping[str, tuple[str, ...]] = {
    "write": (
        "write_executed",
        "db_write_executed",
        "control_executed",
        "state_mutation_executed",
        "write_dependency_constructed",
    ),
    "gateway": ("gateway_called", "slurm_gateway_called", "gateway_dependency_constructed"),
    "receipt": ("receipt_created", "control_receipt_created"),
}
READONLY_DB_REQUIRED_ROUTE_NAMES = frozenset(
    {
        "health",
        "runtime_config",
        "models",
        "stations",
        "latest_product",
        "pipeline_status",
        "pipeline_stages",
        "jobs",
        "job_logs",
    }
)
READONLY_DB_STRICT_ROUTE_FIELDS: Mapping[str, tuple[str, ...]] = {
    "latest_product": STRICT_IDENTITY_FIELDS,
    "pipeline_status": STRICT_IDENTITY_FIELDS,
    "pipeline_stages": STRICT_IDENTITY_FIELDS,
    "jobs": STRICT_IDENTITY_FIELDS,
    "job_logs": STRICT_LOG_IDENTITY_FIELDS,
}
READONLY_DB_REQUIRED_MANUAL_ACTIONS = frozenset({"retry", "cancel"})
READONLY_DB_REQUIRED_PERMISSION_TARGETS = frozenset(
    {
        "hydro.hydro_run",
        "hydro.river_timeseries",
        "met.forecast_cycle",
        "met.forcing_station_timeseries",
        "ops.pipeline_job",
        "ops.pipeline_event",
        "reachable_roles",
        "audited_schema_sequences",
        "current_database",
        "hydro.*",
        "met.*",
        "ops.*",
    }
)
READONLY_DB_TABLE_PERMISSION_TARGETS = frozenset(
    {
        "hydro.hydro_run",
        "hydro.river_timeseries",
        "met.forecast_cycle",
        "met.forcing_station_timeseries",
        "ops.pipeline_job",
        "ops.pipeline_event",
    }
)
READONLY_DB_SCHEMA_PERMISSION_TARGETS = frozenset({"hydro.*", "met.*", "ops.*"})
READONLY_DB_TABLE_REQUIRED_OPERATIONS = frozenset({"INSERT", "UPDATE", "DELETE"})
READONLY_DB_SCHEMA_REQUIRED_OPERATIONS = frozenset({"DDL_CREATE_TABLE"})
READONLY_DB_DATABASE_REQUIRED_OPERATIONS = frozenset({"DATABASE_CREATE"})
READONLY_DB_SEQUENCE_REQUIRED_OPERATIONS = frozenset({"AUDITED_SCHEMA_SEQUENCE_USAGE_UPDATE"})
READONLY_DB_TABLE_MUTATING_FIELDS = ("table_privileges", "column_privileges", "sequence_privileges")
MAX_BOUNDED_EVIDENCE_DEPTH = 5
MAX_BOUNDED_EVIDENCE_DICT_KEYS = 32
MAX_BOUNDED_EVIDENCE_LIST_ITEMS = 12
MAX_BOUNDED_EVIDENCE_STRING_CHARS = 512


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
    metadata_lane = _evaluate_metadata(
        metadata_doc,
        metadata,
        evidence_run_id=config.run_id,
        declared_sources=scope["declared_sources"],
    )
    strict_identities = _resolve_strict_identities(
        metadata if metadata_lane.status == STATUS_PASS else {},
        declared_sources=scope["declared_sources"],
    )

    lane_docs = _load_lane_documents(config.run_dir)
    lanes = {
        "metadata": metadata_lane,
        "docker_preflight": _evaluate_docker_preflight(
            lane_docs["docker_preflight"], evidence_run_id=config.run_id, run_dir=config.run_dir
        ),
        "docker_security": _evaluate_docker_security(
            lane_docs["docker_security"], evidence_run_id=config.run_id
        ),
        "readonly_db": _evaluate_readonly_db(
            lane_docs["readonly_db"],
            declared_sources=scope["declared_sources"],
            strict_identities=strict_identities,
            evidence_run_id=config.run_id,
        ),
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
            declared_sources=scope["declared_sources"],
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
        "metadata": _metadata_summary(metadata_doc, metadata, metadata_lane),
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


def _evaluate_docker_preflight(
    doc: EvidenceDocument | None,
    *,
    evidence_run_id: str,
    run_dir: Path,
) -> LaneEvaluation:
    if doc is None:
        return _missing_lane("docker_preflight", "TWO_NODE_E2E_DOCKER_PREFLIGHT_MISSING")
    payload = doc.payload
    status = _normalized_status(payload.get("status"))
    blockers = list(_stale_lane_blockers(payload))
    summary_status = str(payload.get("status", "unknown"))
    if status == STATUS_PASS:
        preflight_contract_blockers = _docker_preflight_contract_blockers(payload)
        blockers.extend(preflight_contract_blockers)
        blockers.extend(
            _docker_preflight_current_run_blockers(
                doc,
                payload,
                evidence_run_id=evidence_run_id,
                run_dir=run_dir,
                contract_complete=not preflight_contract_blockers,
            )
        )
        commands = payload.get("commands")
        if not isinstance(commands, Mapping):
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_DOCKER_PREFLIGHT_COMMANDS_MISSING",
                    "Docker preflight PASS must include live docker command evidence.",
                )
            )
        else:
            for command_name in DOCKER_PREFLIGHT_REQUIRED_COMMANDS:
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
    docker_proofs = _docker_display_security_proofs(payload)
    if status == STATUS_PASS:
        blockers.extend(_current_run_blockers(payload, evidence_run_id, lane_name="docker_security"))
        contract_blockers, contract_findings = _docker_security_summary_contract_issues(
            doc,
            payload,
            evidence_run_id=evidence_run_id,
        )
        blockers.extend(contract_blockers)
        findings.extend(contract_findings)
        if not _has_live_docker_evidence(payload):
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_DOCKER_LIVE_CONTAINER_EVIDENCE_MISSING",
                    "Docker display security PASS requires live Docker/container evidence.",
                )
            )
        blockers.extend(_docker_missing_required_proof_blockers(docker_proofs))
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
    findings.extend(_docker_proof_findings(docker_proofs))
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


def _evaluate_readonly_db(
    doc: EvidenceDocument | None,
    *,
    declared_sources: tuple[str, ...],
    strict_identities: Mapping[str, Mapping[str, Any]],
    evidence_run_id: str,
) -> LaneEvaluation:
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
    child_blockers, child_findings = _readonly_db_child_evidence_issues(
        payload,
        declared_sources=declared_sources,
        strict_identities=strict_identities,
    )
    blockers.extend(child_blockers)
    findings.extend(child_findings)
    sibling_blockers, sibling_findings = _readonly_db_sibling_issues(
        doc.path,
        payload,
        evidence_run_id=evidence_run_id,
    )
    blockers.extend(sibling_blockers)
    findings.extend(sibling_findings)
    recomputed_status = _readonly_db_recomputed_status(
        payload,
        declared_sources=declared_sources,
        strict_identities=strict_identities,
    )
    if status == STATUS_PASS and recomputed_status != STATUS_PASS:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_READONLY_DB_RECOMPUTED_STATUS_NOT_PASS",
                "Readonly DB summary PASS contradicts recomputed child evidence status.",
                recomputed_status=recomputed_status,
            )
        )
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
    declared_sources: tuple[str, ...],
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
    stable_27_actions: set[str] = set()
    observed_27_actions: set[str] = set()
    if isinstance(display_actions, list):
        for action in display_actions:
            if not isinstance(action, Mapping):
                continue
            if _node_number(action) != "27":
                continue
            action_name = _manual_action_name(action)
            if action_name in MANUAL_OPS_REQUIRED_DISPLAY_ACTIONS:
                observed_27_actions.add(action_name)
            side_effect_findings, side_effect_blockers = _manual_action_side_effect_issues(action)
            findings.extend(side_effect_findings)
            blockers.extend(side_effect_blockers)
            outcome_status = _manual_action_outcome_status(action)
            if outcome_status == STATUS_PASS and action_name in MANUAL_OPS_REQUIRED_DISPLAY_ACTIONS:
                stable_27_actions.add(action_name)
            elif outcome_status == STATUS_BLOCKED:
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_MANUAL_OPS_27_AUTH_ONLY_OR_UNSTABLE",
                        "27 display retry/cancel evidence must include stable manual-action outcome, "
                        "not only auth rejection.",
                        action=action.get("action") or action.get("name"),
                        outcome=_manual_action_outcome_text(action),
                        http_status=action.get("http_status") or action.get("status_code"),
                    )
                )
            else:
                findings.append(
                    _finding(
                        "TWO_NODE_E2E_MANUAL_OPS_27_NOT_FAIL_CLOSED",
                        "27 display retry/cancel evidence did not fail closed as manual action.",
                        action=action.get("action") or action.get("name"),
                        outcome=_manual_action_outcome_text(action),
                    )
                )
    receipts = _first_mapping_value(payload, ("control_receipts", "retry_cancel_receipts", "receipts"))
    actual_22_receipt_count = 0
    actual_22_receipt_sources: set[str] = set()
    run_dir = doc.path.parent.parent
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
            if _is_actual_control_receipt(receipt) and producer == "22":
                receipt_identity = _record_identity(receipt)
                source = _source_name(receipt_identity.get("source") or receipt_identity.get("source_id"))
                if not source:
                    blockers.append(
                        _blocker(
                            "TWO_NODE_E2E_MANUAL_OPS_22_RECEIPT_IDENTITY_MISSING",
                            "Actual node 22 retry/cancel receipt must include strict source identity.",
                            action=receipt.get("action"),
                        )
                    )
                    continue
                if source not in strict_identities:
                    blockers.append(
                        _blocker(
                            "TWO_NODE_E2E_MANUAL_OPS_22_RECEIPT_SOURCE_UNDECLARED",
                            "Actual node 22 retry/cancel receipt source is not in strict identity scope.",
                            source=source,
                            action=receipt.get("action"),
                        )
                    )
                    continue
                actual_22_receipt_sources.add(source)
                _, identity_findings, identity_blockers = _identity_match_status(
                    source,
                    receipt,
                    strict_identities,
                    require_job_id=False,
                )
                findings.extend(_with_context(item, lane="manual_ops", source=source) for item in identity_findings)
                blockers.extend(_with_context(item, lane="manual_ops", source=source) for item in identity_blockers)
                provenance_blockers = _manual_ops_receipt_provenance_blockers(
                    receipt,
                    source=source,
                    evidence_run_id=evidence_run_id,
                    run_dir=run_dir,
                )
                blockers.extend(_with_context(item, lane="manual_ops", source=source) for item in provenance_blockers)
    if status == STATUS_PASS:
        blockers.extend(_current_run_blockers(payload, evidence_run_id, lane_name="manual_ops"))
        blockers.extend(_manual_ops_contract_blockers(payload, display_actions, receipts))
        blockers.extend(_manual_ops_operator_auth_blockers(payload))
        if not isinstance(display_actions, list) or not display_actions:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_ACTIONS_MISSING",
                    "Manual ops evidence must include 27 display retry/cancel fail-closed probes.",
                )
            )
        missing_display_actions = sorted(MANUAL_OPS_REQUIRED_DISPLAY_ACTIONS - stable_27_actions)
        if missing_display_actions:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_27_RETRY_CANCEL_MISSING",
                    "Manual ops PASS requires stable 27 retry and cancel manual-action probes.",
                    missing_actions=missing_display_actions,
                    observed_27_actions=sorted(observed_27_actions),
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
            else:
                missing_receipt_sources = sorted(
                    source for source in declared_sources if source not in actual_22_receipt_sources
                )
                if missing_receipt_sources:
                    blockers.append(
                        _blocker(
                            "TWO_NODE_E2E_MANUAL_OPS_22_RECEIPT_SOURCE_COVERAGE_MISSING",
                            "Manual ops PASS requires actual node 22 receipt strict identity coverage for every "
                            "declared source.",
                            missing_sources=missing_receipt_sources,
                            observed_sources=sorted(actual_22_receipt_sources),
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


def _evaluate_metadata(
    doc: EvidenceDocument | None,
    metadata: Mapping[str, Any],
    *,
    evidence_run_id: str,
    declared_sources: tuple[str, ...],
) -> LaneEvaluation:
    if doc is None:
        return _missing_lane("metadata", "TWO_NODE_E2E_METADATA_MISSING")
    schema = metadata.get("schema")
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    summary_status = str(metadata.get("status", STATUS_PASS))
    if schema not in RUN_METADATA_SCHEMAS:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_METADATA_SCHEMA_UNSUPPORTED",
                "Run metadata must use a recognized two-node E2E schema.",
                schema=schema,
                recognized_schemas=sorted(RUN_METADATA_SCHEMAS),
            )
        )
    metadata_declared_sources = _sources_from_value(
        metadata.get("declared_sources")
        or metadata.get("sources")
        or metadata.get("source_scope")
    )
    if not metadata_declared_sources:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_METADATA_DECLARED_SOURCES_MISSING",
                "Run metadata must declare source scope.",
            )
        )
    elif set(metadata_declared_sources) != set(declared_sources):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_METADATA_DECLARED_SOURCES_MISMATCH",
                "Run metadata declared source scope must match the final configured scope.",
                metadata_declared_sources=list(metadata_declared_sources),
                configured_declared_sources=list(declared_sources),
            )
        )
    explicit_ids = _explicit_bundle_run_ids(metadata)
    if not explicit_ids:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_METADATA_CURRENT_BUNDLE_ID_MISSING",
                "Run metadata must declare the current evidence bundle id.",
                expected_evidence_run_id=evidence_run_id,
            )
        )
    else:
        for key, value in explicit_ids:
            if str(value) != evidence_run_id:
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_METADATA_STALE_BUNDLE_ID",
                        "Run metadata belongs to a different evidence bundle.",
                        key=key,
                        evidence_run_id=value,
                        expected_evidence_run_id=evidence_run_id,
                    )
                )
    if not declared_sources:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DECLARED_SOURCES_MISSING",
                "Final evidence requires declared source scope.",
            )
        )
    identity_blockers, identity_findings = _strict_identity_metadata_issues(
        metadata,
        declared_sources=declared_sources,
    )
    blockers.extend(identity_blockers)
    findings.extend(identity_findings)
    status = _combined_status(
        [_normalized_status(metadata.get("status", STATUS_PASS), pass_aliases=(STATUS_PASS, "ready", "current"))],
        findings=findings,
        blockers=blockers,
    )
    return _lane_from_status(
        "metadata",
        doc,
        status=status,
        summary_status=summary_status,
        blockers=blockers,
        findings=findings,
    )


def _strict_identity_metadata_issues(
    metadata: Mapping[str, Any],
    *,
    declared_sources: tuple[str, ...],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    entries: list[tuple[str, dict[str, Any]]] = []
    raw = (
        metadata.get("strict_identities")
        or metadata.get("source_identities")
        or _nested_get(metadata, ("strict_identity", "sources"))
    )
    if raw is None:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_METADATA_STRICT_IDENTITIES_MISSING",
                "Run metadata must include strict identities for declared sources.",
            )
        )
        return blockers, findings
    if isinstance(raw, Mapping):
        for source_key, value in raw.items():
            if not isinstance(value, Mapping):
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_METADATA_STRICT_IDENTITY_INVALID",
                        "Strict identity entry must be an object.",
                        source_key=source_key,
                    )
                )
                continue
            entries.append((str(source_key), dict(value)))
    elif isinstance(raw, list):
        for index, value in enumerate(raw):
            if not isinstance(value, Mapping):
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_METADATA_STRICT_IDENTITY_INVALID",
                        "Strict identity list entry must be an object.",
                        entry_index=index,
                    )
                )
                continue
            key = value.get("source") or value.get("source_id") or f"entry[{index}]"
            entries.append((str(key), dict(value)))
    else:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_METADATA_STRICT_IDENTITIES_INVALID",
                "Run metadata strict identities must be a mapping or list.",
            )
        )
        return blockers, findings
    declared_set = set(declared_sources)
    seen_embedded_sources: dict[str, str] = {}
    seen_keys: set[str] = set()
    for raw_key, identity in entries:
        source_from_key = _source_name(raw_key)
        source_from_identity = _source_name(identity.get("source") or identity.get("source_id"))
        if not source_from_key:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_METADATA_STRICT_IDENTITY_SOURCE_KEY_MISSING",
                    "Strict identity entry key must identify a source.",
                    source_key=raw_key,
                )
            )
            continue
        if source_from_key in seen_keys:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_METADATA_DUPLICATE_SOURCE_KEY",
                    "Strict identity contains duplicate source keys.",
                    source=source_from_key,
                )
            )
        seen_keys.add(source_from_key)
        if source_from_identity is None:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_METADATA_STRICT_IDENTITY_SOURCE_MISSING",
                    "Strict identity entry must declare its embedded source.",
                    source_key=source_from_key,
                )
            )
        elif source_from_identity != source_from_key:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_METADATA_SOURCE_KEY_MISMATCH",
                    "Strict identity source key must match its embedded source.",
                    source_key=source_from_key,
                    embedded_source=source_from_identity,
                )
            )
        if source_from_key not in declared_set:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_METADATA_UNDECLARED_STRICT_IDENTITY_SOURCE",
                    "Strict identity source is not declared in source scope.",
                    source=source_from_key,
                    declared_sources=list(declared_sources),
                )
            )
        if source_from_identity:
            previous_key = seen_embedded_sources.get(source_from_identity)
            if previous_key and previous_key != source_from_key:
                findings.append(
                    _finding(
                        "TWO_NODE_E2E_METADATA_DUPLICATE_EMBEDDED_SOURCE",
                        "Strict identity embeds the same source under multiple keys.",
                        embedded_source=source_from_identity,
                        source_keys=[previous_key, source_from_key],
                    )
                )
            seen_embedded_sources.setdefault(source_from_identity, source_from_key)
        missing_fields = [field for field in STRICT_LOG_IDENTITY_FIELDS if not _identity_value(identity, field)]
        if missing_fields:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_METADATA_STRICT_IDENTITY_INCOMPLETE",
                    "Strict identity entry is incomplete.",
                    source=source_from_key,
                    missing_fields=missing_fields,
                )
            )
    for source in declared_sources:
        if source not in seen_keys:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_METADATA_DECLARED_SOURCE_IDENTITY_MISSING",
                    "Run metadata is missing strict identity for a declared source.",
                    source=source,
                )
            )
    return blockers, findings


def _metadata_summary(
    doc: EvidenceDocument | None,
    metadata: Mapping[str, Any],
    metadata_lane: LaneEvaluation,
) -> dict[str, Any]:
    if doc is None:
        return {
            "status": metadata_lane.status,
            "evidence_path": None,
            "schema": None,
            "reason": "No run metadata/identity file was found.",
        }
    return {
        "status": metadata_lane.status,
        "evidence_path": _public_path(doc.path),
        "evidence_sha256": doc.sha256,
        "schema": metadata.get("schema"),
        "blockers": list(metadata_lane.blockers),
        "findings": list(metadata_lane.findings),
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


def _docker_preflight_contract_blockers(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if payload.get("schema_version") != DOCKER_PREFLIGHT_SCHEMA and payload.get("schema") != DOCKER_PREFLIGHT_SCHEMA:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_PREFLIGHT_SCHEMA_UNRECOGNIZED",
                "Docker preflight PASS must use the known preflight producer schema.",
                schema=payload.get("schema") or payload.get("schema_version"),
                expected_schema=DOCKER_PREFLIGHT_SCHEMA,
            )
        )
    for key in ("evidence_root", "tmpdir", "docker_root_dir", "min_free_bytes"):
        value = payload.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_DOCKER_PREFLIGHT_RESOURCE_EVIDENCE_MISSING",
                    "Docker preflight PASS must record DockerRootDir, TMPDIR, evidence root, and min-free evidence.",
                    evidence_key=key,
                )
            )
    blockers.extend(
        _recorded_path_approval_blockers(payload, ("evidence_root", "tmpdir"), lane_name="docker_preflight")
    )
    producer_blockers = payload.get("blockers")
    if isinstance(producer_blockers, list) and producer_blockers:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_PREFLIGHT_PRODUCER_BLOCKERS_PRESENT",
                "Docker preflight PASS cannot contain producer blockers.",
                producer_blocker_count=len(producer_blockers),
            )
        )
    disk = payload.get("disk")
    if not isinstance(disk, Mapping):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_PREFLIGHT_DISK_EVIDENCE_MISSING",
                "Docker preflight PASS must include disk evidence.",
                )
            )
    else:
        min_free_bytes = _int_value(payload.get("min_free_bytes"))
        for label in DOCKER_PREFLIGHT_REQUIRED_DISK_LABELS:
            snapshot = disk.get(label)
            if not isinstance(snapshot, Mapping) or snapshot.get("free_bytes") is None:
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_DOCKER_PREFLIGHT_DISK_EVIDENCE_MISSING",
                        "Docker preflight PASS must include free-space evidence for required disk labels.",
                        label=label,
                    )
                )
                continue
            free_bytes = _int_value(snapshot.get("free_bytes"))
            if free_bytes is None:
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_DOCKER_PREFLIGHT_DISK_EVIDENCE_INVALID",
                        "Docker preflight disk free_bytes must be numeric.",
                        label=label,
                    )
                )
            elif min_free_bytes is not None and free_bytes < min_free_bytes:
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_DOCKER_PREFLIGHT_LOW_DISK_SPACE",
                        "Docker preflight PASS contradicts required free-space minimum.",
                        label=label,
                        free_bytes=free_bytes,
                        min_free_bytes=min_free_bytes,
                    )
                )
    commands = payload.get("commands")
    if not isinstance(commands, Mapping):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_PREFLIGHT_COMMANDS_MISSING",
                "Docker preflight PASS must include command evidence.",
            )
        )
    else:
        for command_name in DOCKER_PREFLIGHT_REQUIRED_COMMANDS:
            if command_name not in commands:
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_DOCKER_PREFLIGHT_COMMAND_EVIDENCE_MISSING",
                        "Docker preflight PASS is missing required command evidence.",
                        command=command_name,
                    )
                )
    return blockers


def _docker_preflight_current_run_blockers(
    doc: EvidenceDocument,
    payload: Mapping[str, Any],
    *,
    evidence_run_id: str,
    run_dir: Path,
    contract_complete: bool,
) -> list[dict[str, Any]]:
    explicit_ids = _explicit_bundle_run_ids(payload)
    if explicit_ids:
        return _current_run_blockers(payload, evidence_run_id, lane_name="docker_preflight")
    if (
        contract_complete
        and (
            payload.get("schema_version") == DOCKER_PREFLIGHT_SCHEMA
            or payload.get("schema") == DOCKER_PREFLIGHT_SCHEMA
        )
        and _path_is_relative_to(doc.path, run_dir)
    ):
        return []
    return _current_run_blockers(payload, evidence_run_id, lane_name="docker_preflight")


def _recorded_path_approval_blockers(
    payload: Mapping[str, Any],
    keys: Sequence[str],
    *,
    lane_name: str,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for key in keys:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            _safe_resolved_evidence_root(Path(value))
        except TwoNodeE2EEvidenceError:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_RECORDED_PATH_OUTSIDE_APPROVED_ROOTS",
                    "Recorded evidence/temp path must stay under approved roots.",
                    lane=lane_name,
                    evidence_key=key,
                    path=value,
                )
            )
    return blockers


def _docker_display_security_proofs(payload: Mapping[str, Any]) -> dict[str, bool | None]:
    proofs: dict[str, bool | None] = {}
    for proof_name, aliases in DOCKER_REQUIRED_FALSE_PROOFS.items():
        proofs[proof_name] = _governed_false_proof(payload, aliases)
    for proof_name, aliases in DOCKER_REQUIRED_TRUE_PROOFS.items():
        proofs[proof_name] = _governed_true_proof(payload, aliases)
    slurm_unavailable = _governed_true_proof(payload, ("slurm_routes_unavailable",))
    _merge_governed_docker_proof(
        proofs,
        "slurm_route_available",
        None if slurm_unavailable is None else not slurm_unavailable,
    )
    raw = _raw_docker_security_analysis(payload)
    for proof_name, value in raw.items():
        _merge_governed_docker_proof(proofs, proof_name, value)
    return proofs


def _docker_security_summary_contract_issues(
    doc: EvidenceDocument,
    payload: Mapping[str, Any],
    *,
    evidence_run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    observed_schema = payload.get("schema_version") or payload.get("schema")
    if observed_schema != DOCKER_SECURITY_SUMMARY_SCHEMA:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SUMMARY_SCHEMA_MISSING",
                "Docker security PASS requires a producer security-summary schema.",
                expected_schema=DOCKER_SECURITY_SUMMARY_SCHEMA,
                schema=observed_schema,
            )
        )
    source_artifacts = payload.get("source_artifacts")
    if not isinstance(source_artifacts, Mapping):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACTS_MISSING",
                "Docker security PASS requires source_trust, static, and smoke source artifacts.",
            )
        )
        return blockers, findings
    for artifact_name, expected_schema in DOCKER_SECURITY_CHILD_SCHEMAS.items():
        artifact = source_artifacts.get(artifact_name)
        if not isinstance(artifact, Mapping):
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_MISSING",
                    "Docker security summary is missing a required source artifact.",
                    artifact=artifact_name,
                )
            )
            continue
        artifact_blockers, artifact_findings = _docker_security_child_artifact_issues(
            doc,
            artifact_name,
            artifact,
            expected_schema=expected_schema,
            evidence_run_id=evidence_run_id,
        )
        blockers.extend(artifact_blockers)
        findings.extend(artifact_findings)
    return blockers, findings


def _docker_security_child_artifact_issues(
    doc: EvidenceDocument,
    artifact_name: str,
    artifact: Mapping[str, Any],
    *,
    expected_schema: str,
    evidence_run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    raw_path = artifact.get("path")
    raw_sha256 = artifact.get("sha256")
    if not isinstance(raw_path, str) or not raw_path.strip():
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_PATH_MISSING",
                "Docker security source artifact must include a path.",
                artifact=artifact_name,
            )
        )
        return blockers, findings
    if not isinstance(raw_sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", raw_sha256.strip()):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_SHA_MISSING",
                "Docker security source artifact must include a sha256 digest.",
                artifact=artifact_name,
                path=raw_path,
            )
        )
        return blockers, findings
    try:
        path = _approved_artifact_path(raw_path)
    except TwoNodeE2EEvidenceError:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_OUTSIDE_APPROVED_ROOT",
                "Docker security source artifact path must stay under approved evidence roots.",
                artifact=artifact_name,
                path=raw_path,
            )
        )
        return blockers, findings
    run_dir = doc.path.parent.parent
    if not _path_is_relative_to(path, run_dir):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_STALE_OR_UNSCOPED",
                "Docker security source artifact must come from the current evidence run directory.",
                artifact=artifact_name,
                path=_public_path(path),
                expected_run_dir=_public_path(run_dir),
            )
        )
        return blockers, findings
    try:
        child_doc = _read_json(path, containment_root=run_dir)
    except FileNotFoundError:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_MISSING",
                "Docker security source artifact file is missing.",
                artifact=artifact_name,
                path=_public_path(path),
            )
        )
        return blockers, findings
    if child_doc.sha256 != raw_sha256.lower():
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_HASH_MISMATCH",
                "Docker security source artifact sha256 does not match file content.",
                artifact=artifact_name,
                path=_public_path(path),
            )
        )
    child_payload = child_doc.payload
    child_schema = child_payload.get("schema_version") or child_payload.get("schema")
    if child_schema != expected_schema:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_SCHEMA_INVALID",
                "Docker security source artifact has an unexpected schema.",
                artifact=artifact_name,
                expected_schema=expected_schema,
                schema=child_schema,
            )
        )
    child_status = _normalized_status(child_payload.get("status"))
    if child_status == STATUS_FAIL:
        findings.append(
            _finding(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_FAILED",
                "Docker security source artifact failed and must not be summarized as PASS.",
                artifact=artifact_name,
                child_status=child_status,
            )
        )
    elif child_status != STATUS_PASS:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_NOT_PASS",
                "Docker security source artifact must be PASS before final Docker security can PASS.",
                artifact=artifact_name,
                child_status=child_status,
            )
        )
    if not _docker_security_child_current_run_compatible(path, run_dir, child_payload, evidence_run_id):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_STALE_OR_UNSCOPED",
                "Docker security source artifact must be current-run-compatible.",
                artifact=artifact_name,
                path=_public_path(path),
                expected_evidence_run_id=evidence_run_id,
            )
        )
    return blockers, findings


def _docker_security_child_current_run_compatible(
    path: Path,
    run_dir: Path,
    child_payload: Mapping[str, Any],
    evidence_run_id: str,
) -> bool:
    try:
        path.relative_to(run_dir.resolve(strict=False))
        return True
    except ValueError:
        pass
    explicit_ids = _explicit_bundle_run_ids(child_payload)
    return bool(explicit_ids) and all(str(value) == evidence_run_id for _, value in explicit_ids)


def _governed_false_proof(payload: Mapping[str, Any], aliases: Sequence[str]) -> bool | None:
    values = _bool_lookup_values(payload, frozenset(aliases))
    if any(value is True for value in values):
        return True
    if any(value is False for value in values):
        return False
    return None


def _governed_true_proof(payload: Mapping[str, Any], aliases: Sequence[str]) -> bool | None:
    values = _bool_lookup_values(payload, frozenset(aliases))
    if any(value is False for value in values):
        return False
    if any(value is True for value in values):
        return True
    return None


def _merge_governed_docker_proof(
    proofs: dict[str, bool | None],
    proof_name: str,
    value: bool | None,
) -> None:
    if value is None:
        return
    if proof_name in DOCKER_REQUIRED_FALSE_PROOFS:
        if value is True:
            proofs[proof_name] = True
        else:
            proofs.setdefault(proof_name, False)
    elif proof_name in DOCKER_REQUIRED_TRUE_PROOFS:
        if value is False:
            proofs[proof_name] = False
        else:
            proofs.setdefault(proof_name, True)
    else:
        proofs[proof_name] = value


def _docker_missing_required_proof_blockers(proofs: Mapping[str, bool | None]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for proof_name in (*DOCKER_REQUIRED_FALSE_PROOFS.keys(), *DOCKER_REQUIRED_TRUE_PROOFS.keys()):
        if proofs.get(proof_name) is None:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_DOCKER_DISPLAY_PROOF_MISSING",
                    "Docker security PASS requires explicit no-capability/read-only proof for every governed "
                    "display surface.",
                    proof=proof_name,
                )
            )
    return blockers


def _docker_proof_findings(proofs: Mapping[str, bool | None]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for proof_name in DOCKER_REQUIRED_FALSE_PROOFS:
        if proofs.get(proof_name) is True:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_DOCKER_DISPLAY_FORBIDDEN_CAPABILITY",
                    "Display Docker evidence exposes a forbidden capability.",
                    capability=proof_name,
                )
            )
    for proof_name in DOCKER_REQUIRED_TRUE_PROOFS:
        if proofs.get(proof_name) is False:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_DOCKER_DISPLAY_REQUIRED_READONLY_PROOF_FALSE",
                    "Display Docker evidence contradicts required readonly/no-capability proof.",
                    proof=proof_name,
                )
            )
    return findings


def _raw_docker_security_analysis(payload: Mapping[str, Any]) -> dict[str, bool | None]:
    raw_proofs: dict[str, bool | None] = {}
    mount_hazards = _empty_raw_mount_hazards()
    env_hazard = False
    published_readonly_seen = False
    for inspect_object in _docker_inspect_objects(payload):
        host_config = _first_raw_mapping(inspect_object, ("HostConfig", "host_config"))
        if host_config:
            _merge_raw_proof(
                raw_proofs,
                "privileged",
                _raw_bool(_first_present(host_config, ("Privileged", "privileged"))),
            )
            _merge_raw_proof(
                raw_proofs,
                "host_network",
                _mode_is_host_or_shared(_first_present(host_config, ("NetworkMode", "network_mode"))),
            )
            _merge_raw_proof(
                raw_proofs,
                "host_pid",
                _mode_is_host_or_shared(_first_present(host_config, ("PidMode", "pid", "pid_mode"))),
            )
            _merge_raw_proof(
                raw_proofs,
                "host_ipc",
                _mode_is_host_or_shared(_first_present(host_config, ("IpcMode", "ipc", "ipc_mode"))),
            )
            _merge_raw_proof(
                raw_proofs,
                "cap_add_present",
                _sequence_has_values(_first_present(host_config, ("CapAdd", "cap_add"))),
            )
            _merge_raw_proof(
                raw_proofs,
                "cap_drop_all",
                _cap_drop_all(_first_present(host_config, ("CapDrop", "cap_drop"))),
            )
            _merge_raw_proof(
                raw_proofs,
                "root_filesystem_readonly",
                _raw_bool(
                    _first_present(
                        host_config,
                        ("ReadonlyRootfs", "ReadonlyRootFS", "read_only", "readonly_rootfs"),
                    )
                ),
            )
            _merge_raw_proof(raw_proofs, "forbidden_hostconfig_hazard", _hostconfig_hazard(host_config))
            _merge_mount_hazards(mount_hazards, _raw_bind_mounts(_first_present(host_config, ("Binds", "binds"))))
        config = _first_raw_mapping(inspect_object, ("Config", "config"))
        if config:
            env_hazard = env_hazard or _env_has_forbidden_keys(config.get("Env") or config.get("env"))
        _merge_mount_hazards(
            mount_hazards,
            _raw_structured_mounts(inspect_object.get("Mounts") or inspect_object.get("mounts")),
        )
    for compose_service in _docker_compose_service_objects(payload):
        _merge_raw_proof(raw_proofs, "privileged", _raw_bool(compose_service.get("privileged")))
        _merge_raw_proof(raw_proofs, "host_network", _mode_is_host_or_shared(compose_service.get("network_mode")))
        _merge_raw_proof(raw_proofs, "host_pid", _mode_is_host_or_shared(compose_service.get("pid")))
        _merge_raw_proof(raw_proofs, "host_ipc", _mode_is_host_or_shared(compose_service.get("ipc")))
        _merge_raw_proof(raw_proofs, "cap_add_present", _sequence_has_values(compose_service.get("cap_add")))
        _merge_raw_proof(raw_proofs, "cap_drop_all", _cap_drop_all(compose_service.get("cap_drop")))
        _merge_raw_proof(raw_proofs, "root_filesystem_readonly", _raw_bool(compose_service.get("read_only")))
        _merge_mount_hazards(mount_hazards, _compose_mount_hazards(compose_service.get("volumes")))
        env_hazard = env_hazard or _env_has_forbidden_keys(compose_service.get("environment"))
    if mount_hazards["docker_socket"]:
        raw_proofs["docker_socket_present"] = True
    elif mount_hazards["mount_evidence"]:
        raw_proofs.setdefault("docker_socket_present", False)
    if mount_hazards["forbidden_mount"]:
        raw_proofs["forbidden_mount_hazard"] = True
    elif mount_hazards["mount_evidence"]:
        raw_proofs.setdefault("forbidden_mount_hazard", False)
    if mount_hazards["broad_host_bind"]:
        raw_proofs["broad_host_bind_present"] = True
    elif mount_hazards["mount_evidence"]:
        raw_proofs.setdefault("broad_host_bind_present", False)
    if mount_hazards["private_workspace_bind"]:
        raw_proofs["private_workspace_bind_present"] = True
        raw_proofs["workspace_mount_present"] = True
    elif mount_hazards["mount_evidence"]:
        raw_proofs.setdefault("private_workspace_bind_present", False)
        raw_proofs.setdefault("workspace_mount_present", False)
    if mount_hazards["writable_published"]:
        raw_proofs["writable_published_artifact_mount"] = True
        raw_proofs["published_artifacts_readonly"] = False
    elif mount_hazards["published_mount_seen"]:
        raw_proofs.setdefault("writable_published_artifact_mount", False)
        published_readonly_seen = True
    if env_hazard:
        raw_proofs["forbidden_env_hazard"] = True
    elif _raw_env_evidence_present(payload):
        raw_proofs.setdefault("forbidden_env_hazard", False)
    if published_readonly_seen:
        raw_proofs.setdefault("published_artifacts_readonly", True)
    return raw_proofs


def _docker_inspect_objects(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    objects: list[Mapping[str, Any]] = []
    for key in (
        "docker_inspect",
        "container_inspect",
        "display_container_inspect",
        "inspect",
        "inspect_data",
        "container",
    ):
        for item in _mapping_objects(payload.get(key)):
            objects.append(item)
            if any(top_key in item for top_key in ("pid", "ipc", "cap_drop")):
                objects.append({"host_config": item})
    if any(key in payload for key in ("HostConfig", "host_config", "Config", "config", "Mounts", "mounts")):
        objects.append(payload)
    return objects


def _docker_compose_service_objects(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    services: list[Mapping[str, Any]] = []
    for key in ("compose_service", "display_service", "service"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            services.append(value)
    for key in ("compose", "compose_config", "docker_compose_config"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            raw_services = value.get("services")
            if isinstance(raw_services, Mapping):
                services.extend(item for item in raw_services.values() if isinstance(item, Mapping))
    raw_services = payload.get("services")
    if isinstance(raw_services, Mapping):
        services.extend(item for item in raw_services.values() if isinstance(item, Mapping))
    if any(
        key in payload
        for key in ("privileged", "network_mode", "pid", "ipc", "cap_add", "cap_drop", "volumes", "read_only")
    ):
        services.append(payload)
    return services


def _mapping_objects(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _first_raw_mapping(payload: Mapping[str, Any], keys: Sequence[str]) -> Mapping[str, Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _first_present(payload: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _merge_raw_proof(proofs: dict[str, bool | None], key: str, value: bool | None) -> None:
    if value is None:
        return
    if value is True:
        proofs[key] = True
    else:
        proofs.setdefault(key, False)


def _empty_raw_mount_hazards() -> dict[str, bool]:
    return {
        "mount_evidence": False,
        "published_mount_seen": False,
        "docker_socket": False,
        "forbidden_mount": False,
        "broad_host_bind": False,
        "private_workspace_bind": False,
        "writable_published": False,
    }


def _merge_mount_hazards(target: dict[str, bool], source: Mapping[str, bool]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, False) or bool(value)


def _raw_bind_mounts(value: Any) -> dict[str, bool]:
    hazards = _empty_raw_mount_hazards()
    if not isinstance(value, list):
        return hazards
    for item in value:
        if not isinstance(item, str):
            continue
        parts = item.split(":")
        source = parts[0] if parts else ""
        target = parts[1] if len(parts) > 1 else ""
        mode = ":".join(parts[2:]) if len(parts) > 2 else ""
        read_only = "ro" in {part.strip().lower() for part in mode.split(",")}
        _record_mount_hazard(hazards, source=source, target=target, read_only=read_only)
    return hazards


def _raw_structured_mounts(value: Any) -> dict[str, bool]:
    hazards = _empty_raw_mount_hazards()
    if not isinstance(value, list):
        return hazards
    for item in value:
        if not isinstance(item, Mapping):
            continue
        source = str(item.get("Source") or item.get("source") or "")
        target = str(item.get("Destination") or item.get("Target") or item.get("target") or "")
        read_only = _mount_read_only(item)
        _record_mount_hazard(hazards, source=source, target=target, read_only=read_only)
    return hazards


def _compose_mount_hazards(value: Any) -> dict[str, bool]:
    hazards = _empty_raw_mount_hazards()
    if not isinstance(value, list):
        return hazards
    for item in value:
        if isinstance(item, str):
            parts = item.split(":")
            source = parts[0] if parts else ""
            target = parts[1] if len(parts) > 1 else ""
            mode = ":".join(parts[2:]) if len(parts) > 2 else ""
            read_only = "ro" in {part.strip().lower() for part in mode.split(",")}
            _record_mount_hazard(hazards, source=source, target=target, read_only=read_only)
        elif isinstance(item, Mapping):
            source = str(item.get("source") or item.get("src") or "")
            target = str(item.get("target") or item.get("dst") or item.get("destination") or "")
            read_only = _mount_read_only(item)
            _record_mount_hazard(hazards, source=source, target=target, read_only=read_only)
    return hazards


def _record_mount_hazard(
    hazards: dict[str, bool],
    *,
    source: str,
    target: str,
    read_only: bool | None,
) -> None:
    if not source and not target:
        return
    hazards["mount_evidence"] = True
    if _is_docker_socket_path(source) or _is_docker_socket_path(target):
        hazards["docker_socket"] = True
    if _is_forbidden_mount_path(source) or _is_forbidden_mount_path(target):
        hazards["forbidden_mount"] = True
    if _is_broad_host_bind_source(source):
        hazards["broad_host_bind"] = True
    if _is_private_workspace_path(source) or _is_private_workspace_path(target):
        hazards["private_workspace_bind"] = True
    if _is_published_artifact_path(source) or _is_published_artifact_path(target):
        hazards["published_mount_seen"] = True
        if read_only is False:
            hazards["writable_published"] = True


def _mount_read_only(mount: Mapping[str, Any]) -> bool | None:
    if "RW" in mount:
        raw = _raw_bool(mount.get("RW"))
        return None if raw is None else not raw
    if "read_only" in mount:
        return _raw_bool(mount.get("read_only"))
    if "readonly" in mount:
        return _raw_bool(mount.get("readonly"))
    mode = str(mount.get("Mode") or mount.get("mode") or "")
    if mode:
        return "ro" in {part.strip().lower() for part in mode.split(",")}
    return None


def _raw_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return None


def _int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _mode_is_host_or_shared(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return False
    return text == "host" or text.startswith("container:") or text.startswith("service:")


def _sequence_has_values(value: Any) -> bool | None:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return bool(value)
    return None


def _cap_drop_all(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().upper() == "ALL"
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return any(str(item).strip().upper() == "ALL" for item in value)
    return None


def _hostconfig_hazard(host_config: Mapping[str, Any]) -> bool:
    return any(
        value is True
        for value in (
            _raw_bool(_first_present(host_config, ("Privileged", "privileged"))),
            _mode_is_host_or_shared(_first_present(host_config, ("NetworkMode", "network_mode"))),
            _mode_is_host_or_shared(_first_present(host_config, ("PidMode", "pid", "pid_mode"))),
            _mode_is_host_or_shared(_first_present(host_config, ("IpcMode", "ipc", "ipc_mode"))),
            _sequence_has_values(_first_present(host_config, ("CapAdd", "cap_add"))),
        )
    )


def _env_has_forbidden_keys(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(str(key) in DOCKER_DISPLAY_FORBIDDEN_ENV_KEYS for key in value.keys())
    if isinstance(value, list):
        for item in value:
            key = str(item).split("=", 1)[0].strip()
            if key in DOCKER_DISPLAY_FORBIDDEN_ENV_KEYS:
                return True
    return False


def _raw_env_evidence_present(payload: Mapping[str, Any]) -> bool:
    for inspect_object in _docker_inspect_objects(payload):
        config = _first_raw_mapping(inspect_object, ("Config", "config"))
        if config and ("Env" in config or "env" in config):
            return True
    return any("environment" in service for service in _docker_compose_service_objects(payload))


def _is_docker_socket_path(value: str) -> bool:
    normalized = _normalize_posix_path(value)
    return normalized in {"/var/run/docker.sock", "/run/docker.sock"} or (
        normalized.startswith("/") and normalized.endswith("/docker.sock")
    )


def _is_forbidden_mount_path(value: str) -> bool:
    normalized = _normalize_posix_path(value)
    lowered = normalized.lower()
    return any(token.lower() in lowered for token in DOCKER_FORBIDDEN_MOUNT_TOKENS) or _is_munge_path(normalized)


def _is_munge_path(value: str) -> bool:
    normalized = _normalize_posix_path(value)
    return (
        normalized in {"/run/munge", "/var/run/munge", "/etc/munge"}
        or normalized.startswith("/run/munge/")
        or normalized.startswith("/var/run/munge/")
        or normalized.startswith("/etc/munge/")
        or normalized.endswith("/munge.key")
        or normalized.endswith("/munge.socket")
    )


def _is_broad_host_bind_source(value: str) -> bool:
    normalized = _normalize_posix_path(value)
    return normalized in DOCKER_BROAD_HOST_ROOTS or normalized.startswith("/scratch/")


def _is_private_workspace_path(value: str) -> bool:
    normalized = _normalize_posix_path(value).lower()
    return any(token in normalized for token in ("workspace", ".nhms-runs", "/basins", "/shud", "model_asset"))


def _is_published_artifact_path(value: str) -> bool:
    normalized = _normalize_posix_path(value).lower()
    return "published" in normalized and ("artifact" in normalized or "nhms" in normalized or "/var/lib" in normalized)


def _readonly_db_sibling_issues(
    summary_path: Path,
    summary_payload: Mapping[str, Any],
    *,
    evidence_run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    lane_dir = summary_path.parent
    sibling_payloads: dict[str, Any] = {}
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
        sibling_payloads[filename] = sibling_payload
        for key, value in _explicit_bundle_run_ids_from_value(sibling_payload):
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
    role_payload = sibling_payloads.get("role.json")
    if role_payload is not None:
        if role_payload != summary_payload.get("role"):
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_AUTHORITATIVE_FILE_MISMATCH",
                    "Readonly DB role.json must match the role object embedded in summary.json.",
                    filename="role.json",
                )
            )
        if isinstance(role_payload, Mapping) and role_payload.get("role_type") != "readonly_candidate":
            findings.append(
                _finding(
                    "TWO_NODE_E2E_READONLY_DB_WRITER_ROLE",
                    "Readonly DB role.json identifies a writer or mutating role.",
                    role_type=role_payload.get("role_type"),
                )
            )
    route_payload = sibling_payloads.get("route_smoke.json")
    if route_payload is not None:
        if route_payload != summary_payload.get("route_smoke"):
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_AUTHORITATIVE_FILE_MISMATCH",
                    "Readonly DB route_smoke.json must match the route_smoke list embedded in summary.json.",
                    filename="route_smoke.json",
                )
            )
    permission_payload = sibling_payloads.get("permission_probes.json")
    if permission_payload is not None:
        if permission_payload != summary_payload.get("permission_probes"):
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_AUTHORITATIVE_FILE_MISMATCH",
                    "Readonly DB permission_probes.json must match the permission_probes list embedded in "
                    "summary.json.",
                    filename="permission_probes.json",
                )
            )
        if isinstance(permission_payload, list):
            for operation in _permission_operations_from_targets(permission_payload):
                findings.extend(_readonly_db_operation_findings(operation))
    return blockers, findings


def _readonly_db_child_evidence_issues(
    payload: Mapping[str, Any],
    *,
    declared_sources: tuple[str, ...],
    strict_identities: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    route_smoke = payload.get("route_smoke")
    if isinstance(route_smoke, list):
        route_blockers, route_findings = _readonly_db_route_issues(
            route_smoke,
            declared_sources=declared_sources,
            strict_identities=strict_identities,
            display_identity=payload.get("display_identity"),
        )
        blockers.extend(route_blockers)
        findings.extend(route_findings)
    manual_actions = payload.get("manual_action_probes")
    if isinstance(manual_actions, list):
        blockers.extend(_readonly_db_manual_action_issues(manual_actions))
    permission_probes = payload.get("permission_probes")
    if isinstance(permission_probes, list):
        permission_blockers, permission_findings = _readonly_db_permission_issues(permission_probes)
        blockers.extend(permission_blockers)
        findings.extend(permission_findings)
    blockers.extend(
        _readonly_db_source_coverage_blockers(
            payload,
            declared_sources=declared_sources,
        )
    )
    return blockers, findings


def _readonly_db_route_issues(
    route_smoke: list[Any],
    *,
    declared_sources: tuple[str, ...],
    strict_identities: Mapping[str, Mapping[str, Any]],
    display_identity: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    routes = [item for item in route_smoke if isinstance(item, Mapping)]
    route_names = {str(item.get("name") or "") for item in routes}
    missing_routes = sorted(READONLY_DB_REQUIRED_ROUTE_NAMES - route_names)
    if missing_routes:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_READONLY_DB_ROUTE_COVERAGE_MISSING",
                "Readonly DB route smoke must cover all required display read routes.",
                missing_routes=missing_routes,
            )
        )
    strict_route_sources: dict[str, set[str]] = {route: set() for route in READONLY_DB_STRICT_ROUTE_FIELDS}
    for route in routes:
        name = str(route.get("name") or "")
        route_status = _normalized_status(route.get("status"))
        if route_status != STATUS_PASS:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_ROUTE_CHILD_NOT_PASS",
                    "Readonly DB route smoke child must be PASS before the DB lane can PASS.",
                    route=name,
                    child_status=route_status,
                )
            )
        required_fields = READONLY_DB_STRICT_ROUTE_FIELDS.get(name)
        if required_fields is None:
            continue
        identity = _readonly_route_identity(route, required_fields=required_fields)
        route_source = _source_name(route.get("source") or route.get("source_id"))
        identity_source = _source_name(identity.get("source") or identity.get("source_id"))
        source = route_source or identity_source
        missing_identity = [field for field in required_fields if not _identity_value(identity, field)]
        if missing_identity:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_ROUTE_STRICT_IDENTITY_INCOMPLETE",
                    "Readonly DB identity-bound route smoke child is missing strict identity.",
                    route=name,
                    source=source,
                    missing_fields=missing_identity,
                )
            )
            continue
        if route_source and identity_source and route_source != identity_source:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_READONLY_DB_ROUTE_SOURCE_IDENTITY_MISMATCH",
                    "Readonly DB route source key must match its embedded strict identity source.",
                    route=name,
                    source=route_source,
                    embedded_source=identity_source,
                )
            )
        if source is None:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_ROUTE_SOURCE_MISSING",
                    "Readonly DB identity-bound route smoke child must identify its declared source.",
                    route=name,
                )
            )
            continue
        if source not in declared_sources:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_ROUTE_SOURCE_UNDECLARED",
                    "Readonly DB identity-bound route smoke source is not declared in scope.",
                    route=name,
                    source=source,
                    declared_sources=list(declared_sources),
                )
            )
            continue
        strict_route_sources[name].add(source)
        _, identity_findings, identity_blockers = _identity_match_status(
            source,
            {"identity": identity},
            strict_identities,
            require_job_id="job_id" in required_fields,
        )
        findings.extend(_with_context(item, route=name, source=source) for item in identity_findings)
        blockers.extend(_with_context(item, route=name, source=source) for item in identity_blockers)
        expected_identity = _readonly_display_identity_for_source(display_identity, source)
        for identity_field in required_fields:
            expected = _identity_value(expected_identity, identity_field)
            observed = _identity_value(identity, identity_field)
            if expected and observed and str(expected) != str(observed):
                findings.append(
                    _finding(
                        "TWO_NODE_E2E_READONLY_DB_ROUTE_DISPLAY_IDENTITY_MISMATCH",
                        "Readonly DB route smoke strict identity contradicts display_identity.",
                        route=name,
                        source=source,
                        field=identity_field,
                        expected=expected,
                        observed=observed,
                    )
                )
    for route_name, observed_sources in strict_route_sources.items():
        missing_sources = sorted(source for source in declared_sources if source not in observed_sources)
        if missing_sources:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_ROUTE_SOURCE_COVERAGE_MISSING",
                    "Readonly DB route smoke must include identity-bound evidence for every declared source.",
                    route=route_name,
                    missing_sources=missing_sources,
                    observed_sources=sorted(observed_sources),
                )
            )
    return blockers, findings


def _readonly_db_source_coverage_blockers(
    payload: Mapping[str, Any],
    *,
    declared_sources: tuple[str, ...],
) -> list[dict[str, Any]]:
    if not declared_sources:
        return []
    observed_sources = _readonly_db_evidence_sources(payload)
    missing_sources = sorted(source for source in declared_sources if source not in observed_sources)
    if not missing_sources:
        return []
    return [
        _blocker(
            "TWO_NODE_E2E_READONLY_DB_SOURCE_COVERAGE_MISSING",
            "Readonly DB evidence must include producer-complete source identities for every declared source.",
            missing_sources=missing_sources,
            observed_sources=sorted(observed_sources),
        )
    ]


def _readonly_db_evidence_sources(payload: Mapping[str, Any]) -> set[str]:
    sources: set[str] = set()
    display_identity = payload.get("display_identity")
    if isinstance(display_identity, Mapping):
        for key, value in display_identity.items():
            key_source = _source_name(key)
            if key_source and isinstance(value, Mapping):
                sources.add(key_source)
            value_source = _source_name(value.get("source") if isinstance(value, Mapping) else None)
            if value_source:
                sources.add(value_source)
        for nested_key in ("sources", "strict_identities"):
            nested = display_identity.get(nested_key)
            if isinstance(nested, Mapping):
                for key, value in nested.items():
                    key_source = _source_name(key)
                    if key_source and isinstance(value, Mapping):
                        sources.add(key_source)
                    value_source = _source_name(value.get("source") if isinstance(value, Mapping) else None)
                    if value_source:
                        sources.add(value_source)
    identity_source = _source_name(payload.get("source") or payload.get("source_id"))
    if identity_source:
        sources.add(identity_source)
    return sources


def _readonly_display_identity_for_source(display_identity: Any, source: str) -> Mapping[str, Any]:
    if not isinstance(display_identity, Mapping):
        return {}
    source_key = _source_name(source)
    source_scoped = display_identity.get(source) or display_identity.get(source_key or "")
    if isinstance(source_scoped, Mapping):
        return source_scoped
    nested_sources = display_identity.get("sources") or display_identity.get("strict_identities")
    if isinstance(nested_sources, Mapping):
        nested = nested_sources.get(source) or nested_sources.get(source_key or "")
        if isinstance(nested, Mapping):
            return nested
    identity_source = _source_name(display_identity.get("source") or display_identity.get("source_id"))
    if identity_source == source_key:
        return display_identity
    return {}


def _readonly_route_identity(route: Mapping[str, Any], *, required_fields: tuple[str, ...]) -> dict[str, Any]:
    raw = route.get("strict_identity") or route.get("identity") or {}
    identity = dict(raw) if isinstance(raw, Mapping) else {}
    path = str(route.get("path") or "")
    if path:
        parsed = urlsplit(path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        for field in required_fields:
            if field not in identity and query.get(field):
                identity[field] = query[field][0]
        if "source" not in identity and query.get("source_id"):
            identity["source"] = query["source_id"][0]
        if "job_id" in required_fields and "job_id" not in identity:
            parts = [unquote(part) for part in parsed.path.split("/") if part]
            if len(parts) >= 4 and parts[-1] == "logs":
                identity["job_id"] = parts[-2]
    return identity


def _readonly_db_manual_action_issues(actions: list[Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    records = [item for item in actions if isinstance(item, Mapping)]
    observed_actions = {_manual_action_name(item) for item in records}
    missing_actions = sorted(READONLY_DB_REQUIRED_MANUAL_ACTIONS - observed_actions)
    if missing_actions:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_READONLY_DB_MANUAL_ACTION_COVERAGE_MISSING",
                "Readonly DB manual action probes must cover retry and cancel.",
                missing_actions=missing_actions,
            )
        )
    for action in records:
        action_name = _manual_action_name(action)
        action_status = _normalized_status(action.get("status"))
        if action_status != STATUS_PASS:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_MANUAL_ACTION_CHILD_NOT_PASS",
                    "Readonly DB manual action child must be PASS before the DB lane can PASS.",
                    action=action_name,
                    child_status=action_status,
                )
            )
        if _manual_action_outcome_status(action) != STATUS_PASS:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_MANUAL_ACTION_OUTCOME_INVALID",
                    "Readonly DB manual action child must prove display retry/cancel returns manual action.",
                    action=action_name,
                    http_status=action.get("http_status") or action.get("status_code"),
                    observed_error_code=action.get("observed_error_code") or action.get("error_code"),
                )
            )
        if action.get("write_dependency_constructed") is not False and action.get("write_executed") is not False:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_MANUAL_ACTION_NO_WRITE_PROOF_MISSING",
                    "Readonly DB manual action child must explicitly prove no write dependency was constructed.",
                    action=action_name,
                )
            )
    return blockers


def _readonly_db_permission_issues(permission_probes: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    targets = [item for item in permission_probes if isinstance(item, Mapping)]
    target_names = {str(item.get("target") or "") for item in targets}
    surfaces = {str(item.get("surface") or "") for item in targets}
    covered = set(target_names)
    if "current_database_create_catalog" in surfaces:
        covered.add("current_database")
    missing_targets = sorted(READONLY_DB_REQUIRED_PERMISSION_TARGETS - covered)
    if missing_targets:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_READONLY_DB_PERMISSION_COVERAGE_MISSING",
                "Readonly DB permission probes must cover all required database mutation surfaces.",
                missing_targets=missing_targets,
            )
        )
    for target in targets:
        target_status = _normalized_status(target.get("status"))
        target_name = str(target.get("target") or "")
        operations = target.get("operations")
        reachable_findings = target.get("reachable_role_findings")
        if not isinstance(operations, list) or (not operations and target_name != "reachable_roles"):
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_PERMISSION_OPERATIONS_MISSING",
                    "Readonly DB permission target must include operation-level evidence.",
                    target=target_name,
                )
            )
        if target_name == "reachable_roles":
            if isinstance(reachable_findings, list) and reachable_findings:
                findings.append(
                    _finding(
                        "TWO_NODE_E2E_READONLY_DB_REACHABLE_ROLE_FINDING",
                        "Readonly DB reachable role inventory found a mutating reachable role.",
                        target=target_name,
                        reachable_role_finding_count=len(reachable_findings),
                    )
                )
            elif operations not in ([], None):
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_READONLY_DB_REACHABLE_ROLE_OPERATIONS_UNEXPECTED",
                        "Readonly DB reachable_roles may use operations=[] only when no reachable role findings exist.",
                        target=target_name,
                    )
                )
        if target_status == STATUS_FAIL:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_PERMISSION_CHILD_FAILED",
                    "Readonly DB permission child failed and must not be summarized as PASS.",
                    target=target_name,
                )
            )
        elif target_status == STATUS_BLOCKED:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_READONLY_DB_PERMISSION_CHILD_BLOCKED",
                    "Readonly DB permission child is blocked.",
                    target=target_name,
                )
            )
        blockers.extend(_readonly_db_permission_operation_coverage_blockers(target, operations))
        findings.extend(_readonly_db_permission_catalog_findings(target))
        if isinstance(operations, list):
            for operation in operations:
                if not isinstance(operation, Mapping):
                    continue
                findings.extend(_readonly_db_operation_findings(operation))
                operation_status = _normalized_status(operation.get("status"))
                if operation_status == STATUS_BLOCKED:
                    blockers.append(
                        _blocker(
                            "TWO_NODE_E2E_READONLY_DB_PERMISSION_OPERATION_BLOCKED",
                            "Readonly DB permission operation is blocked.",
                            target=target_name,
                            operation=operation.get("operation"),
                        )
                    )
    return blockers, findings


def _readonly_db_operation_findings(operation: Mapping[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if operation.get("privilege_allowed") is True:
        findings.append(
            _finding(
                "TWO_NODE_E2E_READONLY_DB_MUTATING_PRIVILEGE",
                "Readonly DB permission evidence contains a mutating privilege.",
                operation=operation.get("operation"),
                reason=operation.get("reason"),
            )
        )
    if operation.get("execution_outcome") == "succeeded":
        findings.append(
            _finding(
                "TWO_NODE_E2E_READONLY_DB_SUCCESSFUL_MUTATION_PROBE",
                "Readonly DB permission evidence contains a successful DML/DDL probe.",
                operation=operation.get("operation"),
                reason=operation.get("reason"),
            )
        )
    return findings


def _readonly_db_permission_operation_coverage_blockers(
    target: Mapping[str, Any],
    operations: Any,
) -> list[dict[str, Any]]:
    target_name = _canonical_permission_target_name(target)
    if target_name == "reachable_roles":
        reachable_findings = target.get("reachable_role_findings")
        if operations == [] and reachable_findings == []:
            return []
    required_operations = _readonly_db_required_operations_for_target(target_name)
    if not required_operations:
        return []
    observed_operations = {
        str(operation.get("operation") or "").upper()
        for operation in operations
        if isinstance(operation, Mapping)
    } if isinstance(operations, list) else set()
    missing_operations = sorted(required_operations - observed_operations)
    if not missing_operations:
        return []
    return [
        _blocker(
            "TWO_NODE_E2E_READONLY_DB_PERMISSION_OPERATION_COVERAGE_MISSING",
            "Readonly DB permission target is missing required operation-level evidence.",
            target=target_name,
            missing_operations=missing_operations,
            observed_operations=sorted(observed_operations),
        )
    ]


def _canonical_permission_target_name(target: Mapping[str, Any]) -> str:
    target_name = str(target.get("target") or "")
    surface = str(target.get("surface") or "")
    if target_name in {"current_database", "nhms", ""} and surface == "current_database_create_catalog":
        return "current_database"
    return target_name


def _readonly_db_required_operations_for_target(target_name: str) -> frozenset[str]:
    if target_name in READONLY_DB_TABLE_PERMISSION_TARGETS:
        return READONLY_DB_TABLE_REQUIRED_OPERATIONS
    if target_name in READONLY_DB_SCHEMA_PERMISSION_TARGETS:
        return READONLY_DB_SCHEMA_REQUIRED_OPERATIONS
    if target_name == "current_database":
        return READONLY_DB_DATABASE_REQUIRED_OPERATIONS
    if target_name == "audited_schema_sequences":
        return READONLY_DB_SEQUENCE_REQUIRED_OPERATIONS
    return frozenset()


def _readonly_db_permission_catalog_findings(target: Mapping[str, Any]) -> list[dict[str, Any]]:
    target_name = _canonical_permission_target_name(target)
    findings: list[dict[str, Any]] = []
    if target_name in READONLY_DB_TABLE_PERMISSION_TARGETS:
        for field in READONLY_DB_TABLE_MUTATING_FIELDS:
            value = target.get(field)
            if _catalog_value_has_mutating_privilege(value):
                findings.append(
                    _finding(
                        "TWO_NODE_E2E_READONLY_DB_MUTATING_CATALOG_FIELD",
                        "Readonly DB table permission evidence contains a mutating catalog field.",
                        target=target_name,
                        catalog_field=field,
                    )
                )
    if target_name in READONLY_DB_SCHEMA_PERMISSION_TARGETS:
        schema_privileges = target.get("schema_privileges")
        if isinstance(schema_privileges, Mapping) and schema_privileges.get("create") is True:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_READONLY_DB_SCHEMA_CREATE_PRIVILEGE",
                    "Readonly DB schema permission evidence contains CREATE privilege.",
                    target=target_name,
                )
            )
    if target_name == "current_database":
        database_privileges = target.get("database_privileges")
        if isinstance(database_privileges, Mapping) and database_privileges.get("create") is True:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_READONLY_DB_DATABASE_CREATE_PRIVILEGE",
                    "Readonly DB current database permission evidence contains CREATE privilege.",
                    target=target_name,
                )
            )
    if target_name == "audited_schema_sequences" and _catalog_value_has_mutating_privilege(
        target.get("sequence_privileges")
    ):
        findings.append(
            _finding(
                "TWO_NODE_E2E_READONLY_DB_AUDITED_SEQUENCE_MUTATING_PRIVILEGE",
                "Readonly DB audited schema sequence evidence contains USAGE/UPDATE privilege.",
                target=target_name,
            )
        )
    return findings


def _catalog_value_has_mutating_privilege(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key).lower()
            if key_text in {
                "insert",
                "update",
                "delete",
                "truncate",
                "references",
                "trigger",
                "maintain",
                "usage",
                "create",
                "mutating_privilege_allowed",
            } and nested is True:
                return True
            if key_text in {"columns", "column_privilege_columns", "sequence_privilege_sequences"} and nested:
                return True
            if _catalog_value_has_mutating_privilege(nested):
                return True
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return True
            if _catalog_value_has_mutating_privilege(item):
                return True
    return False


def _readonly_db_recomputed_status(
    payload: Mapping[str, Any],
    *,
    declared_sources: tuple[str, ...],
    strict_identities: Mapping[str, Mapping[str, Any]],
) -> str:
    findings: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    role = payload.get("role")
    if isinstance(role, Mapping) and role.get("role_type") != "readonly_candidate":
        findings.append({"code": "writer_role"})
    child_blockers, child_findings = _readonly_db_child_evidence_issues(
        payload,
        declared_sources=declared_sources,
        strict_identities=strict_identities,
    )
    blockers.extend(child_blockers)
    findings.extend(child_findings)
    return _combined_status([STATUS_PASS], findings=findings, blockers=blockers)


def _permission_operations_from_targets(targets: list[Any]) -> list[Mapping[str, Any]]:
    operations: list[Mapping[str, Any]] = []
    for target in targets:
        if not isinstance(target, Mapping):
            continue
        raw_operations = target.get("operations", [])
        if isinstance(raw_operations, list):
            operations.extend(item for item in raw_operations if isinstance(item, Mapping))
    return operations


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


def _explicit_bundle_run_ids_from_value(value: Any) -> list[tuple[str, Any]]:
    result: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        result.extend(_explicit_bundle_run_ids(value))
        for nested in value.values():
            result.extend(_explicit_bundle_run_ids_from_value(nested))
    elif isinstance(value, list):
        for nested in value:
            result.extend(_explicit_bundle_run_ids_from_value(nested))
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


def _bool_lookup_any(payload: Mapping[str, Any], keys: Sequence[str]) -> bool | None:
    for key in keys:
        value = _bool_lookup(payload, key)
        if value is not None:
            return value
    return _deep_bool_lookup(payload, frozenset(keys))


def _bool_lookup_values(value: Any, keys: frozenset[str]) -> list[bool]:
    values: list[bool] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key) in keys:
                parsed = _raw_bool(nested)
                if parsed is not None:
                    values.append(parsed)
            values.extend(_bool_lookup_values(nested, keys))
    elif isinstance(value, list):
        for nested in value:
            values.extend(_bool_lookup_values(nested, keys))
    return values


def _deep_bool_lookup(value: Any, keys: frozenset[str]) -> bool | None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key) in keys:
                parsed = _raw_bool(nested)
                if parsed is not None:
                    return parsed
        for nested in value.values():
            found = _deep_bool_lookup(nested, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _deep_bool_lookup(nested, keys)
            if found is not None:
                return found
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


def _manual_action_name(action: Mapping[str, Any]) -> str:
    raw = str(action.get("action") or action.get("name") or action.get("path") or "").lower()
    if "retry" in raw:
        return "retry"
    if "cancel" in raw:
        return "cancel"
    return raw.strip()


def _manual_action_side_effect_issues(
    action: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    action_name = _manual_action_name(action)
    for category, keys in MANUAL_OPS_SIDE_EFFECT_CATEGORIES.items():
        observed = [(key, action.get(key)) for key in keys if isinstance(action.get(key), bool)]
        if not observed:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_27_SIDE_EFFECT_PROOF_MISSING",
                    "27 display retry/cancel probe must explicitly prove no write/gateway/receipt side effects.",
                    action=action_name,
                    side_effect=category,
                    accepted_fields=list(keys),
                )
            )
            continue
        true_fields = [key for key, value in observed if value is True]
        if true_fields:
            findings.append(
                _finding(
                    "TWO_NODE_E2E_MANUAL_OPS_27_MUTATION",
                    "27 display retry/cancel evidence executed or wrote a control action.",
                    action=action_name,
                    side_effect=category,
                    fields=true_fields,
                )
            )
    return findings, blockers


def _manual_ops_contract_blockers(
    payload: Mapping[str, Any],
    display_actions: Any,
    receipts: Any,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if payload.get("schema") != MANUAL_OPS_SCHEMA:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_SCHEMA_MISSING",
                "Manual ops PASS requires the accepted manual ops evidence schema, not boolean assertions.",
                expected_schema=MANUAL_OPS_SCHEMA,
                schema=payload.get("schema"),
            )
        )
    if not isinstance(display_actions, list):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_EVIDENCE_MISSING",
                "Manual ops PASS requires 27 display retry/cancel response evidence.",
            )
        )
    elif any(not isinstance(action, Mapping) or "response_evidence" not in action for action in display_actions):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_EVIDENCE_MISSING",
                "Manual ops 27 actions must include metadata-only response evidence.",
            )
        )
    no_side_effect = payload.get("no_side_effect_proof")
    if not isinstance(no_side_effect, Mapping) or no_side_effect.get("node") not in {"27", 27}:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_NO_SIDE_EFFECT_PROOF_MISSING",
                "Manual ops PASS requires node 27 no-side-effect proof.",
            )
        )
    else:
        for key in ("db_writes", "gateway_calls", "control_receipts_created"):
            if no_side_effect.get(key) is not False:
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_MANUAL_OPS_NO_SIDE_EFFECT_PROOF_MISSING",
                        "Manual ops node 27 no-side-effect proof must explicitly record false side effects.",
                        side_effect=key,
                    )
                )
    if not isinstance(receipts, list):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_MISSING",
                "Manual ops PASS requires node 22 receipt provenance for declared sources.",
            )
        )
    return blockers


def _manual_ops_receipt_provenance_blockers(
    receipt: Mapping[str, Any],
    *,
    source: str,
    evidence_run_id: str,
    run_dir: Path,
) -> list[dict[str, Any]]:
    provenance = receipt.get("provenance")
    if not isinstance(provenance, Mapping) or not provenance:
        return [
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_MISSING",
                "Actual node 22 manual ops receipts must include producer provenance.",
                action=receipt.get("action"),
            )
        ]
    blockers: list[dict[str, Any]] = []
    raw_node = provenance.get("producer_node") or provenance.get("node") or provenance.get("host_node")
    if raw_node is None or _node_number(provenance) != "22":
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_PRODUCER_INVALID",
                "Manual ops receipt provenance must identify node 22 as the producer.",
                producer_node=raw_node,
            )
        )
    producer_role = str(provenance.get("producer_role") or provenance.get("service_role") or "").strip()
    if producer_role != "compute_control":
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_PRODUCER_INVALID",
                "Manual ops receipt provenance must identify the compute_control producer role.",
                producer_role=producer_role,
            )
        )
    if not any(str(provenance.get(key) or "").strip() for key in ("receipt_id", "command_id")):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_ID_MISSING",
                "Manual ops receipt provenance must include a receipt_id or command_id.",
            )
        )
    provenance_source = _source_name(provenance.get("source") or provenance.get("source_id"))
    if not provenance_source:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_SOURCE_MISSING",
                "Manual ops receipt provenance must include the strict source.",
            )
        )
    elif provenance_source != source:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_SOURCE_MISMATCH",
                "Manual ops receipt provenance source must match the receipt strict source.",
                source=provenance_source,
                expected_source=source,
            )
        )
    if provenance.get("redacted") is not True:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_UNREDACTED",
                "Manual ops receipt provenance must be redacted metadata only.",
            )
        )
    explicit_ids = _explicit_bundle_run_ids(provenance)
    if not explicit_ids:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_RUN_ID_MISSING",
                "Manual ops receipt provenance must bind to the current evidence run.",
                expected_evidence_run_id=evidence_run_id,
            )
        )
    else:
        for key, value in explicit_ids:
            if str(value) != evidence_run_id:
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_RUN_ID_MISMATCH",
                        "Manual ops receipt provenance belongs to a different evidence run.",
                        key=key,
                        evidence_run_id=value,
                        expected_evidence_run_id=evidence_run_id,
                    )
                )
    blockers.extend(
        _manual_ops_receipt_artifact_blockers(
            provenance,
            evidence_run_id=evidence_run_id,
            run_dir=run_dir,
        )
    )
    return blockers


def _manual_ops_receipt_artifact_blockers(
    provenance: Mapping[str, Any],
    *,
    evidence_run_id: str,
    run_dir: Path,
) -> list[dict[str, Any]]:
    raw_path = provenance.get("artifact_path") or provenance.get("path")
    raw_sha256 = provenance.get("sha256") or provenance.get("artifact_sha256")
    if raw_path is None and raw_sha256 is None:
        return []
    blockers: list[dict[str, Any]] = []
    if not isinstance(raw_path, str) or not raw_path.strip():
        return [
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_PATH_MISSING",
                "Manual ops receipt artifact provenance must include a path.",
            )
        ]
    if not isinstance(raw_sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", raw_sha256.strip()):
        return [
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_SHA_MISSING",
                "Manual ops receipt artifact provenance must include a sha256 digest.",
                path=raw_path,
            )
        ]
    try:
        path = _approved_artifact_path(raw_path)
    except TwoNodeE2EEvidenceError:
        return [
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_OUTSIDE_APPROVED_ROOT",
                "Manual ops receipt artifact path must stay under approved evidence roots.",
                path=raw_path,
            )
        ]
    explicit_ids = _explicit_bundle_run_ids(provenance)
    if not _path_is_relative_to(path, run_dir) and not (
        explicit_ids and all(str(value) == evidence_run_id for _, value in explicit_ids)
    ):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_STALE_OR_UNSCOPED",
                "Manual ops receipt artifact must be in the current run or explicitly bind to it.",
                path=_public_path(path),
                expected_evidence_run_id=evidence_run_id,
            )
        )
    containment_root = _approved_artifact_containment_root(path)
    try:
        content = read_bytes_limited_no_follow(
            path,
            max_bytes=MAX_EVIDENCE_PAYLOAD_BYTES,
            containment_root=containment_root,
        )
    except FileNotFoundError:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_MISSING",
                "Manual ops receipt artifact file is missing.",
                path=_public_path(path),
            )
        )
        return blockers
    except SafeFilesystemError:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_PATH_UNSAFE",
                "Manual ops receipt artifact path is unsafe.",
                path=_public_path(path),
            )
        )
        return blockers
    if len(content) > MAX_EVIDENCE_PAYLOAD_BYTES:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_TOO_LARGE",
                "Manual ops receipt artifact file is too large.",
                path=_public_path(path),
            )
        )
        return blockers
    if hashlib.sha256(content).hexdigest() != raw_sha256.lower():
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_HASH_MISMATCH",
                "Manual ops receipt artifact sha256 does not match file content.",
                path=_public_path(path),
            )
        )
    return blockers


def _manual_ops_operator_auth_blockers(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    auth = payload.get("production_operator_auth")
    if not isinstance(auth, Mapping):
        return [
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_PRODUCTION_AUTH_MISSING",
                "Manual ops PASS requires metadata-only production operator auth evidence.",
            )
        ]
    blockers: list[dict[str, Any]] = []
    if auth.get("status") != STATUS_PASS:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_PRODUCTION_AUTH_MISSING",
                "Production operator auth evidence must be PASS.",
                auth_status=auth.get("status"),
            )
        )
    if auth.get("redacted") is not True or auth.get("secret_material_written") is not False:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_PRODUCTION_AUTH_UNREDACTED",
                "Production operator auth evidence must be redacted metadata only.",
            )
        )
    if not any(auth.get(key) for key in ("auth_source", "header_source", "token_source", "principal")):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_MANUAL_OPS_PRODUCTION_AUTH_MISSING",
                "Production operator auth evidence must include redacted source metadata.",
            )
        )
    return blockers


def _manual_action_outcome_status(action: Mapping[str, Any]) -> str:
    outcome_text = _manual_action_outcome_text(action)
    http_status = str(action.get("http_status") or action.get("status_code") or "")
    if _is_manual_action_outcome(outcome_text) and (not http_status or http_status == "409"):
        return STATUS_PASS
    lowered = outcome_text.lower()
    if http_status in {"401", "403"} or any(
        marker in lowered
        for marker in ("auth_required", "not_authorized", "unauthorized", "forbidden", "401", "403")
    ):
        return STATUS_BLOCKED
    return STATUS_FAIL


def _manual_action_outcome_text(action: Mapping[str, Any]) -> str:
    return str(
        action.get("observed_error_code")
        or action.get("error_code")
        or action.get("outcome")
        or action.get("result")
        or ""
    )


def _is_manual_action_outcome(outcome: str) -> bool:
    lowered = outcome.lower()
    return any(
        marker in lowered
        for marker in (
            "manual_action",
            "control_plane_manual_action_required",
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
    bounded, truncated = _bounded_value(payload, depth=0)
    if isinstance(bounded, Mapping):
        result = dict(bounded)
    else:
        result = {"value": bounded}
    result["_bounded_evidence"] = {
        "max_depth": MAX_BOUNDED_EVIDENCE_DEPTH,
        "max_dict_keys": MAX_BOUNDED_EVIDENCE_DICT_KEYS,
        "max_list_items": MAX_BOUNDED_EVIDENCE_LIST_ITEMS,
        "max_string_chars": MAX_BOUNDED_EVIDENCE_STRING_CHARS,
        "truncated": truncated,
    }
    return result


def _bounded_value(value: Any, *, depth: int) -> tuple[Any, bool]:
    if depth >= MAX_BOUNDED_EVIDENCE_DEPTH:
        return "[truncated:max-depth]", True
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        truncated = False
        items = list(value.items())
        for key, nested in items[:MAX_BOUNDED_EVIDENCE_DICT_KEYS]:
            bounded, nested_truncated = _bounded_value(nested, depth=depth + 1)
            result[str(key)] = bounded
            truncated = truncated or nested_truncated
        if len(items) > MAX_BOUNDED_EVIDENCE_DICT_KEYS:
            result["_omitted_keys"] = len(items) - MAX_BOUNDED_EVIDENCE_DICT_KEYS
            truncated = True
        return result, truncated
    if isinstance(value, list):
        result = []
        truncated = False
        for nested in value[:MAX_BOUNDED_EVIDENCE_LIST_ITEMS]:
            bounded, nested_truncated = _bounded_value(nested, depth=depth + 1)
            result.append(bounded)
            truncated = truncated or nested_truncated
        if len(value) > MAX_BOUNDED_EVIDENCE_LIST_ITEMS:
            result.append({"_omitted_items": len(value) - MAX_BOUNDED_EVIDENCE_LIST_ITEMS})
            truncated = True
        return result, truncated
    if isinstance(value, str):
        raw = value.encode("utf-8", errors="replace")
        if len(raw) <= MAX_BOUNDED_EVIDENCE_STRING_CHARS:
            return value, False
        bounded = raw[:MAX_BOUNDED_EVIDENCE_STRING_CHARS].decode("utf-8", errors="ignore")
        return f"{bounded}[truncated:{len(raw)}B]", True
    return value, False


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


def _approved_artifact_path(value: str) -> Path:
    path = Path(value).expanduser()
    resolved = path.resolve(strict=False)
    approved_roots = tuple(root.expanduser().resolve(strict=False) for root in APPROVED_EVIDENCE_ROOTS)
    if not any(_path_is_relative_to(resolved, root) for root in approved_roots):
        raise TwoNodeE2EEvidenceError(
            "TWO_NODE_E2E_EVIDENCE_ROOT_UNAPPROVED",
            "Evidence artifact path must be under repository artifacts/ or /scratch/frd_muziyao.",
        )
    _refuse_symlink_components(resolved.parent)
    return resolved


def _approved_artifact_containment_root(path: Path) -> Path:
    resolved = path.resolve(strict=False)
    approved_roots = tuple(root.expanduser().resolve(strict=False) for root in APPROVED_EVIDENCE_ROOTS)
    for root in approved_roots:
        if _path_is_relative_to(resolved, root):
            return root
    raise TwoNodeE2EEvidenceError(
        "TWO_NODE_E2E_EVIDENCE_ROOT_UNAPPROVED",
        "Evidence artifact path must be under repository artifacts/ or /scratch/frd_muziyao.",
    )


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _normalize_posix_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if len(normalized) > 1 and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    return normalized


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
