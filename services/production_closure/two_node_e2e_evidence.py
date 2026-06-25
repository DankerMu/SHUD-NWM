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
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

import services.production_closure.two_node_e2e_api_lane as two_node_e2e_api_lane
import services.production_closure.two_node_e2e_browser_lane as two_node_e2e_browser_lane
import services.production_closure.two_node_e2e_cross_plane_lane as two_node_e2e_cross_plane_lane
import services.production_closure.two_node_e2e_docker_preflight as two_node_e2e_docker_preflight
import services.production_closure.two_node_e2e_docker_security as two_node_e2e_docker_security
import services.production_closure.two_node_e2e_final_aggregation as two_node_e2e_final_aggregation
import services.production_closure.two_node_e2e_logs_lane as two_node_e2e_logs_lane
import services.production_closure.two_node_e2e_manual_ops_lane as two_node_e2e_manual_ops_lane
import services.production_closure.two_node_e2e_metadata_lane as two_node_e2e_metadata_lane
import services.production_closure.two_node_e2e_readonly_db_lane as two_node_e2e_readonly_db_lane
import services.production_closure.two_node_e2e_simple_live_lane as two_node_e2e_simple_live_lane
from packages.common.redaction import redact_payload, redact_text
from packages.common.safe_fs import (
    SafeFilesystemError,
    read_bytes_limited_no_follow,
    stat_no_follow,
)
from services.production_closure.two_node_e2e_api_lane import (
    ApiLaneEvaluationHelpers,
    evaluate_api_lane,
)
from services.production_closure.two_node_e2e_browser_lane import (
    BrowserLaneEvaluationHelpers,
    evaluate_browser_lane,
)
from services.production_closure.two_node_e2e_cross_plane_lane import (
    CrossPlaneEvaluationHelpers,
    build_source_scope_results,
    evaluate_cross_plane_lane,
)
from services.production_closure.two_node_e2e_docker_preflight import (
    DockerPreflightEvaluationHelpers,
    evaluate_docker_preflight,
)
from services.production_closure.two_node_e2e_docker_security import (
    DockerSecurityEvaluationHelpers,
    evaluate_docker_security,
)
from services.production_closure.two_node_e2e_final_aggregation import (
    APPROVED_EVIDENCE_ROOTS,
    MAX_EVIDENCE_PAYLOAD_BYTES,
    REPO_ROOT,
    STATUS_BLOCKED,
    STATUS_FAIL,
    STATUS_PARTIAL,
    STATUS_PASS,
    EvidenceWriter,
    FinalAggregationHelpers,
    TwoNodeE2EEvidenceError,
)
from services.production_closure.two_node_e2e_logs_lane import (
    LogsLaneEvaluationHelpers,
    evaluate_logs_lane,
)
from services.production_closure.two_node_e2e_manual_ops_lane import (
    ManualOpsLaneEvaluationHelpers,
    evaluate_manual_ops_lane,
)
from services.production_closure.two_node_e2e_metadata_lane import (
    STRICT_IDENTITY_FIELDS,
    STRICT_LOG_IDENTITY_FIELDS,
    MetadataLaneEvaluationHelpers,
    evaluate_metadata_lane,
)
from services.production_closure.two_node_e2e_readonly_db_lane import (
    ReadonlyDbEvaluationHelpers,
    evaluate_readonly_db,
)
from services.production_closure.two_node_e2e_simple_live_lane import (
    SimpleLiveLaneEvaluationHelpers,
    evaluate_simple_live_lane,
)

DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "artifacts" / "two-node-e2e"
READONLY_DB_LIVE_SCHEMA = two_node_e2e_readonly_db_lane.READONLY_DB_LIVE_SCHEMA
FINAL_EVIDENCE_SCHEMA = two_node_e2e_final_aggregation.FINAL_EVIDENCE_SCHEMA
FINAL_AGGREGATION_OWNER = two_node_e2e_final_aggregation.FINAL_AGGREGATION_OWNER
FINAL_AGGREGATION_VERIFICATION = two_node_e2e_final_aggregation.FINAL_AGGREGATION_VERIFICATION
FINAL_AGGREGATION_GUARD_SYMBOLS = two_node_e2e_final_aggregation.FINAL_AGGREGATION_GUARD_SYMBOLS
FINAL_AGGREGATION_BLOCKER_NAMESPACES = two_node_e2e_final_aggregation.FINAL_AGGREGATION_BLOCKER_NAMESPACES
_safe_resolved_evidence_root = two_node_e2e_final_aggregation._safe_resolved_evidence_root
_path_is_relative_to = two_node_e2e_final_aggregation._path_is_relative_to
_refuse_symlink_components = two_node_e2e_final_aggregation._refuse_symlink_components
_safe_run_id = two_node_e2e_final_aggregation._safe_run_id
_public_path = two_node_e2e_final_aggregation._public_path

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
CURRENT_EVIDENCE_RUN_ID_KEYS = (
    "evidence_run_id",
    "bundle_run_id",
    "evidence_bundle_id",
    "validation_run_id",
    "current_evidence_run_id",
    "current_bundle_run_id",
    "expected_evidence_run_id",
    "parent_evidence_run_id",
    "parent_bundle_run_id",
    "parent_bundle_id",
)
PRODUCER_EVIDENCE_KEYS = (
    "source_artifacts",
    "commands",
    "requests",
    "responses",
    "browser_artifacts",
    "screenshots",
    "network",
    "artifacts",
    "evidence",
    "proofs",
)
SOURCE_SCOPED_PRODUCER_EVIDENCE_KEYS = ("source_artifacts", "evidence", "proofs", "artifacts")
PRODUCER_AUTHORITATIVE_PROOF_CONTAINER_KEYS = frozenset(
    {
        "request",
        "response",
        "producer",
        "proof",
        "record",
        "result",
    }
)
PRODUCER_NON_AUTHORITATIVE_PROOF_CONTAINER_KEYS = frozenset(
    {
        "metadata",
        "wrapper",
        "collector",
        "context",
        "diagnostics",
        "debug",
        "extra",
        "notes",
    }
)
LOG_URI_KEYS = ("log_uri", "published_log_uri")
LOG_URI_IDENTITY_FIELDS = ("source", "cycle_time", "run_id", "job_id")
LOG_URI_REQUIRED_IDENTITY_FIELDS = ("source", "run_id", "job_id")
LOG_URI_STREAM_SUFFIXES = (".out", ".err")
LOG_UNAVAILABLE_ERROR_CODES = frozenset({"JOB_LOG_NOT_PUBLISHED", "JOB_LOG_NOT_FOUND"})
PUBLISHED_LOG_ROOT_KEYS = frozenset(
    {
        "published_artifact_root",
        "published_root",
        "publish_root",
        "log_publish_root",
        "nhms_published_artifact_root",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
    }
)
DEFAULT_PUBLISHED_LOG_ROOTS = (Path("/var/lib/nhms/published"), Path("/mnt/nhms-published"))
PUBLISHED_LOG_S3_BUCKET_KEYS = frozenset(
    {
        "published_artifact_s3_bucket",
        "published_s3_bucket",
        "s3_bucket",
        "nhms_published_artifact_s3_bucket",
        "NHMS_PUBLISHED_ARTIFACT_S3_BUCKET",
    }
)
PUBLISHED_LOG_S3_PREFIX_KEYS = frozenset(
    {
        "published_artifact_s3_prefix",
        "published_s3_prefix",
        "s3_prefix",
        "nhms_published_artifact_s3_prefix",
        "NHMS_PUBLISHED_ARTIFACT_S3_PREFIX",
    }
)
LOG_URI_ENCODED_FORBIDDEN_RE = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
LOG_URI_CREDENTIAL_WORD_RE = re.compile(
    r"(token|password|passwd|pwd|secret|credential|api[_-]?key|access[_-]?key|session[_-]?key|signature)",
    re.IGNORECASE,
)
PRIVATE_LOG_PATH_TOKENS = (
    ".nhms-runs",
    "workspace",
    "basins",
    "model_asset",
    "model-assets",
    "shud",
)
PRODUCER_SOURCE_KEYS = frozenset({"source", "source_id"})
PRODUCER_CHECK_KEYS = frozenset({"check", "check_name", "operation", "route"})
PRODUCER_TEXT_IDENTITY_KEYS = frozenset(
    {
        "path",
        "url",
        "uri",
        "request_path",
        "query",
        "query_string",
        "text",
        "stdout",
        "stderr",
        "message",
        "details",
        "summary",
        "body",
        "response_body",
        "log_uri",
        "published_log_uri",
        "artifact_path",
        "proof_path",
    }
)
PRODUCER_CHECK_ALIASES: Mapping[str, tuple[str, ...]] = {
    "latest_product": ("latest_product", "latest-product", "/latest-product", "/mvp/qhh/latest-product"),
    "series": ("series", "/series"),
    "ops_status": ("ops_status", "ops-status", "/pipeline/status"),
    "ops_stages": ("ops_stages", "ops-stages", "/pipeline/stages"),
    "jobs": ("jobs", "/jobs"),
    "hydro_met": ("hydro_met", "hydro-met", "/hydro-met"),
    "ops": ("ops", "/ops"),
    "ops_jobs": ("ops_jobs", "ops-jobs"),
    "ops_job_logs": ("ops_job_logs", "ops-job-logs", "/logs"),
    "source_switch": ("source_switch", "source-switch"),
    "job_logs": ("job_logs", "job-logs", "/logs"),
}
PRODUCER_BASE_REQUIRED_IDENTITY_FIELDS = ("source", "check", "run_id", "cycle_time", "model_id")
PRODUCER_TEXT_AUTHORITY_IDENTITY_FIELDS = (*STRICT_LOG_IDENTITY_FIELDS, "check")
PRODUCER_JOB_ID_REQUIRED_CHECKS = frozenset({"job_logs", "ops_jobs", "ops_job_logs"})
MAX_BOUNDED_EVIDENCE_DEPTH = 5
MAX_BOUNDED_EVIDENCE_DICT_KEYS = 32
MAX_BOUNDED_EVIDENCE_LIST_ITEMS = 12
MAX_BOUNDED_EVIDENCE_STRING_CHARS = 512
MAX_EVIDENCE_TRAVERSAL_DEPTH = 256
MAX_EVIDENCE_TRAVERSAL_NODES = 100_000
TWO_NODE_E2E_SHARED_CONTRACT_OWNER = "services.production_closure.two_node_e2e_evidence"
TWO_NODE_E2E_SHARED_CONTRACT_VERIFICATION_PRODUCER = (
    'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "producer or source_artifact or strict_identity"'
)
TWO_NODE_E2E_SHARED_CONTRACT_VERIFICATION_METADATA = (
    'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "metadata or strict_identity or source_scope"'
)
TWO_NODE_E2E_SHARED_CONTRACT_VERIFICATION_SAFETY = (
    "uv run pytest -q tests/test_two_node_e2e_evidence.py "
    '-k "logs or log_uri or redaction or evidence_root or path_safety or stale"'
)
TWO_NODE_E2E_SHARED_CONTRACTS: Mapping[str, Mapping[str, Any]] = {
    "lane-result-adapter": {
        "owner": TWO_NODE_E2E_SHARED_CONTRACT_OWNER,
        "consumers": FINAL_REQUIRED_LANES,
        "guard_symbols": (
            "LaneEvaluation",
            "LaneEvaluation.to_summary",
            "validate_two_node_e2e_evidence",
            "FINAL_REQUIRED_LANES",
            "STATUS_PASS",
            "STATUS_PARTIAL",
            "STATUS_FAIL",
            "STATUS_BLOCKED",
        ),
        "namespaces": ("TWO_NODE_E2E_LANE_", "TWO_NODE_E2E_SOURCE_", "TWO_NODE_E2E_EVIDENCE_"),
        "verification": TWO_NODE_E2E_SHARED_CONTRACT_VERIFICATION_METADATA,
    },
    "current-run-binding": {
        "owner": TWO_NODE_E2E_SHARED_CONTRACT_OWNER,
        "consumers": FINAL_REQUIRED_LANES,
        "guard_symbols": (
            "CURRENT_EVIDENCE_RUN_ID_KEYS",
            "_current_run_blockers",
            "_recursive_current_run_blockers",
            "_explicit_bundle_run_ids",
            "_explicit_bundle_run_ids_from_value",
        ),
        "namespaces": (
            "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_",
            "TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH",
            "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
        ),
        "verification": TWO_NODE_E2E_SHARED_CONTRACT_VERIFICATION_PRODUCER,
    },
    "producer-source-artifacts": {
        "owner": TWO_NODE_E2E_SHARED_CONTRACT_OWNER,
        "consumers": (
            "docker_preflight",
            "docker_security",
            "readonly_db",
            "api",
            "browser",
            "logs",
            "cross_plane",
            "manual_ops",
            "slurm",
            "compute_summary",
            "display_summary",
        ),
        "guard_symbols": (
            "PRODUCER_EVIDENCE_KEYS",
            "SOURCE_SCOPED_PRODUCER_EVIDENCE_KEYS",
            "PRODUCER_AUTHORITATIVE_PROOF_CONTAINER_KEYS",
            "PRODUCER_NON_AUTHORITATIVE_PROOF_CONTAINER_KEYS",
            "_has_producer_backed_lane_evidence",
            "_source_lane_check_producer_blockers",
            "_source_scoped_producer_evidence_blockers",
            "_producer_source_artifact_blockers",
            "_producer_source_artifact_record_blockers",
        ),
        "namespaces": (
            "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_",
            "CHECK_PRODUCER_EVIDENCE_MISSING",
            "CHECK_PRODUCER_IDENTITY_",
        ),
        "verification": TWO_NODE_E2E_SHARED_CONTRACT_VERIFICATION_PRODUCER,
    },
    "strict-identity": {
        "owner": TWO_NODE_E2E_SHARED_CONTRACT_OWNER,
        "consumers": ("metadata", "readonly_db", "api", "browser", "logs", "cross_plane", "manual_ops"),
        "guard_symbols": (
            "two_node_e2e_metadata_lane.STRICT_IDENTITY_FIELDS",
            "two_node_e2e_metadata_lane.STRICT_LOG_IDENTITY_FIELDS",
            "LOG_URI_IDENTITY_FIELDS",
            "two_node_e2e_metadata_lane.resolve_strict_identities",
            "two_node_e2e_metadata_lane.strict_identity_metadata_issues",
            "_strict_identity_value_matches",
            "_record_identity",
        ),
        "namespaces": (
            "TWO_NODE_E2E_STRICT_IDENTITY_",
            "TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE",
            "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE",
        ),
        "verification": TWO_NODE_E2E_SHARED_CONTRACT_VERIFICATION_METADATA,
    },
    "approved-root-path-safety": {
        "owner": TWO_NODE_E2E_SHARED_CONTRACT_OWNER,
        "consumers": FINAL_REQUIRED_LANES,
        "guard_symbols": (
            "APPROVED_EVIDENCE_ROOTS",
            "EvidenceWriter",
            "_safe_resolved_evidence_root",
            "_read_json",
            "_read_json_bytes",
            "_refuse_symlink_components",
            "_recorded_path_approval_blockers",
            "_producer_source_artifact_record_blockers",
        ),
        "namespaces": (
            "TWO_NODE_E2E_EVIDENCE_ROOT_UNAPPROVED",
            "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE",
            "TWO_NODE_E2E_RECORDED_PATH_OUTSIDE_APPROVED_ROOTS",
            "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_OUTSIDE_APPROVED_ROOT",
        ),
        "verification": TWO_NODE_E2E_SHARED_CONTRACT_VERIFICATION_SAFETY,
    },
    "redaction": {
        "owner": TWO_NODE_E2E_SHARED_CONTRACT_OWNER,
        "consumers": FINAL_REQUIRED_LANES,
        "guard_symbols": (
            "LaneEvaluation.to_summary",
            "EvidenceWriter.write_json",
            "redact_payload",
            "redact_text",
            "_blocker",
            "_finding",
        ),
        "namespaces": (
            "TWO_NODE_E2E_EVIDENCE_REDACTION_DEPTH_EXCEEDED",
            "TWO_NODE_E2E_EVIDENCE_PAYLOAD_TOO_LARGE",
        ),
        "verification": TWO_NODE_E2E_SHARED_CONTRACT_VERIFICATION_SAFETY,
    },
    "log-uri-safety": {
        "owner": TWO_NODE_E2E_SHARED_CONTRACT_OWNER,
        "consumers": ("logs", "browser"),
        "guard_symbols": (
            "LOG_URI_KEYS",
            "LOG_URI_REQUIRED_IDENTITY_FIELDS",
            "PUBLISHED_LOG_ROOT_KEYS",
            "PUBLISHED_LOG_S3_BUCKET_KEYS",
            "_published_log_uri_blockers",
            "_published_log_uri_identity_blockers",
            "_safe_log_relative_path_blockers",
            "_safe_log_absolute_path_blockers",
            "_safe_log_uri_summary",
            "_unsafe_log_uri_summary",
        ),
        "namespaces": (
            "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_",
            "TWO_NODE_E2E_LOGS_PRIVATE_LOG_URI",
            "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH",
        ),
        "verification": TWO_NODE_E2E_SHARED_CONTRACT_VERIFICATION_SAFETY,
    },
}


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
        evidence = self.evidence
        if evidence is not None:
            try:
                redacted_evidence = redact_payload(evidence)
            except RecursionError as error:
                raise TwoNodeE2EEvidenceError(
                    "TWO_NODE_E2E_EVIDENCE_REDACTION_DEPTH_EXCEEDED",
                    f"Evidence lane {self.name} is too deeply nested to redact safely.",
                ) from error
        else:
            redacted_evidence = None
        payload: dict[str, Any] = {
            "status": self.status,
            "evidence_path": self.evidence_path,
            "evidence_sha256": self.evidence_sha256,
            "summary_status": self.summary_status,
            "blockers": list(self.blockers),
            "findings": list(self.findings),
        }
        if redacted_evidence is not None:
            payload["redacted_evidence"] = redacted_evidence
        return payload


def validate_two_node_e2e_evidence(config: TwoNodeE2EEvidenceConfig) -> dict[str, Any]:
    config = replace(config, evidence_root=_safe_resolved_evidence_root(config.evidence_root))
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=config.force)
    writer.prepare()

    metadata_doc = _find_first_json(
        config.run_dir,
        two_node_e2e_metadata_lane.METADATA_DOCUMENT_CANDIDATES,
    )
    metadata = metadata_doc.payload if metadata_doc is not None else {}
    metadata_result = evaluate_metadata_lane(
        metadata_doc,
        metadata,
        evidence_run_id=config.run_id,
        configured_declared_sources=config.declared_sources,
        configured_reduced_scope=config.reduced_scope,
        helpers=_metadata_lane_helpers(),
    )
    scope = metadata_result.scope.as_dict()
    metadata_lane = metadata_result.lane
    strict_identities = metadata_result.strict_identities
    simple_live_lane_helpers = _simple_live_lane_helpers()

    lane_docs = _load_lane_documents(config.run_dir)
    lanes = {
        "metadata": metadata_lane,
        "docker_preflight": evaluate_docker_preflight(
            lane_docs["docker_preflight"],
            evidence_run_id=config.run_id,
            helpers=_docker_preflight_helpers(),
        ),
        "docker_security": evaluate_docker_security(
            lane_docs["docker_security"],
            evidence_run_id=config.run_id,
            helpers=_docker_security_helpers(config.run_dir),
        ),
        "readonly_db": evaluate_readonly_db(
            lane_docs["readonly_db"],
            declared_sources=scope["declared_sources"],
            strict_identities=strict_identities,
            evidence_run_id=config.run_id,
            helpers=_readonly_db_helpers(),
        ),
        "api": evaluate_api_lane(
            lane_docs["api"],
            declared_sources=scope["declared_sources"],
            strict_identities=strict_identities,
            evidence_run_id=config.run_id,
            helpers=_api_lane_helpers(),
        ),
        "browser": evaluate_browser_lane(
            lane_docs["browser"],
            declared_sources=scope["declared_sources"],
            strict_identities=strict_identities,
            evidence_run_id=config.run_id,
            helpers=_browser_lane_helpers(),
        ),
        "logs": evaluate_logs_lane(
            lane_docs["logs"],
            declared_sources=scope["declared_sources"],
            strict_identities=strict_identities,
            evidence_run_id=config.run_id,
            docker_security_doc=lane_docs["docker_security"],
            helpers=_logs_lane_helpers(),
        ),
        "slurm": evaluate_simple_live_lane(
            two_node_e2e_simple_live_lane.SLURM_LANE_CONFIG,
            lane_docs["slurm"],
            evidence_run_id=config.run_id,
            run_dir=config.run_dir,
            helpers=simple_live_lane_helpers,
        ),
        "manual_ops": evaluate_manual_ops_lane(
            lane_docs["manual_ops"],
            declared_sources=scope["declared_sources"],
            strict_identities=strict_identities,
            evidence_run_id=config.run_id,
            helpers=_manual_ops_lane_helpers(),
        ),
        "compute_summary": evaluate_simple_live_lane(
            two_node_e2e_simple_live_lane.COMPUTE_SUMMARY_LANE_CONFIG,
            lane_docs["compute_summary"],
            evidence_run_id=config.run_id,
            run_dir=config.run_dir,
            helpers=simple_live_lane_helpers,
        ),
        "display_summary": evaluate_simple_live_lane(
            two_node_e2e_simple_live_lane.DISPLAY_SUMMARY_LANE_CONFIG,
            lane_docs["display_summary"],
            evidence_run_id=config.run_id,
            run_dir=config.run_dir,
            helpers=simple_live_lane_helpers,
        ),
    }
    cross_plane_helpers = _cross_plane_helpers()
    source_scope_results = build_source_scope_results(
        declared_sources=scope["declared_sources"],
        strict_identities=strict_identities,
        source_lanes={name: lanes[name] for name in ("api", "browser", "logs")},
        helpers=cross_plane_helpers,
    )
    lanes["cross_plane"] = evaluate_cross_plane_lane(
        lane_docs["cross_plane"],
        declared_sources=scope["declared_sources"],
        strict_identities=strict_identities,
        source_scope_results=source_scope_results,
        reduced_scope=scope["reduced_scope"],
        evidence_run_id=config.run_id,
        helpers=cross_plane_helpers,
    )

    summary = two_node_e2e_final_aggregation.build_final_summary(
        config=config,
        metadata_doc=metadata_doc,
        metadata=metadata,
        metadata_lane=metadata_lane,
        strict_identities=strict_identities,
        lanes=lanes,
        source_scope_results=source_scope_results,
        scope=scope,
        helpers=_final_aggregation_helpers(),
    )
    return two_node_e2e_final_aggregation.write_final_summary(
        writer=writer,
        config=config,
        summary=summary,
    )


def _load_lane_documents(run_dir: Path) -> dict[str, EvidenceDocument | None]:
    return {
        "docker_preflight": _find_first_json(
            run_dir,
            two_node_e2e_docker_preflight.DOCKER_PREFLIGHT_DOCUMENT_CANDIDATES,
        ),
        "docker_security": _find_first_json(
            run_dir,
            two_node_e2e_docker_security.DOCKER_SECURITY_DOCUMENT_CANDIDATES,
        ),
        "readonly_db": _find_first_json(
            run_dir,
            two_node_e2e_readonly_db_lane.READONLY_DB_DOCUMENT_CANDIDATES,
        ),
        "api": _find_first_json(run_dir, two_node_e2e_api_lane.API_DOCUMENT_CANDIDATES),
        "browser": _find_first_json(run_dir, two_node_e2e_browser_lane.BROWSER_DOCUMENT_CANDIDATES),
        "cross_plane": _find_first_json(
            run_dir,
            two_node_e2e_cross_plane_lane.CROSS_PLANE_DOCUMENT_CANDIDATES,
        ),
        "manual_ops": _find_first_json(
            run_dir,
            two_node_e2e_manual_ops_lane.MANUAL_OPS_DOCUMENT_CANDIDATES,
        ),
        "slurm": _find_first_json(
            run_dir,
            two_node_e2e_simple_live_lane.SLURM_DOCUMENT_CANDIDATES,
        ),
        "logs": _find_first_json(run_dir, two_node_e2e_logs_lane.LOGS_DOCUMENT_CANDIDATES),
        "compute_summary": _find_first_json(
            run_dir,
            two_node_e2e_simple_live_lane.COMPUTE_SUMMARY_DOCUMENT_CANDIDATES,
        ),
        "display_summary": _find_first_json(
            run_dir,
            two_node_e2e_simple_live_lane.DISPLAY_SUMMARY_DOCUMENT_CANDIDATES,
        ),
    }


def _docker_preflight_helpers() -> DockerPreflightEvaluationHelpers[LaneEvaluation]:
    return DockerPreflightEvaluationHelpers(
        missing_lane=_missing_lane,
        lane_from_status=_lane_from_status,
        normalized_status=_normalized_status,
        blocker=_blocker,
        stale_lane_blockers=_stale_lane_blockers,
        current_run_blockers=_current_run_blockers,
        recorded_path_approval_blockers=_recorded_path_approval_blockers,
        int_value=_int_value,
    )


def _docker_security_helpers(run_dir: Path) -> DockerSecurityEvaluationHelpers[LaneEvaluation]:
    return DockerSecurityEvaluationHelpers(
        run_dir=run_dir,
        missing_lane=_missing_lane,
        lane_from_status=_lane_from_status,
        normalized_status=_normalized_status,
        blocker=_blocker,
        finding=_finding,
        stale_lane_blockers=_stale_lane_blockers,
        current_run_blockers=_current_run_blockers,
        has_live_docker_evidence=_has_live_docker_evidence,
        runtime_config=_runtime_config,
        bool_lookup=_bool_lookup,
        bool_lookup_any=_bool_lookup_any,
        payload_findings=_payload_findings,
        payload_blockers=_payload_blockers,
        first_mapping_value=_first_mapping_value,
        approved_artifact_path=_approved_artifact_path,
        approved_artifact_containment_root=_approved_artifact_containment_root,
        path_is_relative_to=_path_is_relative_to,
        public_path=_public_path,
        read_json=_read_json,
        explicit_bundle_run_ids_from_value=_explicit_bundle_run_ids_from_value,
        evidence_error_type=TwoNodeE2EEvidenceError,
    )


def _metadata_lane_helpers() -> MetadataLaneEvaluationHelpers[LaneEvaluation]:
    return MetadataLaneEvaluationHelpers(
        missing_lane=_missing_lane,
        lane_from_status=_lane_from_status,
        normalized_status=_normalized_status,
        combined_status=_combined_status,
        blocker=_blocker,
        finding=_finding,
        explicit_bundle_run_ids=_explicit_bundle_run_ids,
        nested_get=_nested_get,
        sources_from_value=_sources_from_value,
        source_name=_source_name,
        identity_value=_identity_value,
        optional_bool=_optional_bool,
    )


def _readonly_db_helpers() -> ReadonlyDbEvaluationHelpers[LaneEvaluation]:
    return ReadonlyDbEvaluationHelpers(
        missing_lane=_missing_lane,
        lane_from_status=_lane_from_status,
        normalized_status=_normalized_status,
        combined_status=_combined_status,
        blocker=_blocker,
        finding=_finding,
        stale_lane_blockers=_stale_lane_blockers,
        database_url_is_redacted=_database_url_is_redacted,
        sources_from_value=_sources_from_value,
        approved_artifact_path=_approved_artifact_path,
        refuse_symlink_components=_refuse_symlink_components,
        path_is_relative_to=_path_is_relative_to,
        public_path=_public_path,
        stat_no_follow=stat_no_follow,
        read_json_value=_read_json_value,
        explicit_bundle_run_ids_from_value=_explicit_bundle_run_ids_from_value,
        read_bytes_limited_no_follow=read_bytes_limited_no_follow,
        ensure_bounded_evidence_value=_ensure_bounded_evidence_value,
        identity_match_status=_identity_match_status,
        source_name=_source_name,
        identity_value=_identity_value,
        strict_identity_value_matches=_strict_identity_value_matches,
        manual_action_name=two_node_e2e_manual_ops_lane.manual_action_name,
        manual_action_outcome_status=two_node_e2e_manual_ops_lane.manual_action_outcome_status,
        with_context=_with_context,
        evidence_error_type=TwoNodeE2EEvidenceError,
        safe_filesystem_error_type=SafeFilesystemError,
        max_evidence_payload_bytes=MAX_EVIDENCE_PAYLOAD_BYTES,
    )


def _simple_live_lane_helpers() -> SimpleLiveLaneEvaluationHelpers[LaneEvaluation]:
    return SimpleLiveLaneEvaluationHelpers(
        missing_lane=_missing_lane,
        lane_from_status=_lane_from_status,
        normalized_status=_normalized_status,
        blocker=_blocker,
        finding=_finding,
        stale_lane_blockers=_stale_lane_blockers,
        current_run_blockers=_current_run_blockers,
        recursive_current_run_blockers=_recursive_current_run_blockers,
        producer_source_artifact_blockers=_producer_source_artifact_blockers,
        has_live_lane_evidence=_has_live_lane_evidence,
        has_producer_backed_lane_evidence=_has_producer_backed_lane_evidence,
        has_mock_or_fixture=_has_mock_or_fixture,
    )


def _api_lane_helpers() -> ApiLaneEvaluationHelpers[LaneEvaluation]:
    return ApiLaneEvaluationHelpers(
        missing_lane=_missing_lane,
        lane_from_status=_lane_from_status,
        normalized_status=_normalized_status,
        blocker=_blocker,
        finding=_finding,
        stale_lane_blockers=_stale_lane_blockers,
        current_run_blockers=_current_run_blockers,
        recursive_current_run_blockers=_recursive_current_run_blockers,
        producer_source_artifact_blockers=_producer_source_artifact_blockers,
        source_lane_check_producer_blockers=_source_lane_check_producer_blockers,
        has_live_lane_evidence=lambda payload: _has_live_lane_evidence(
            payload,
            live_flag=two_node_e2e_api_lane.API_LIVE_FLAG,
        ),
        has_producer_backed_lane_evidence=_has_producer_backed_lane_evidence,
        has_mock_or_fixture=_has_mock_or_fixture,
        has_historical_latest=_has_historical_latest,
        source_records=_source_records,
        check_results=_check_results,
        identity_match_status=_identity_match_status,
        with_context=_with_context,
    )


def _browser_lane_helpers() -> BrowserLaneEvaluationHelpers[LaneEvaluation]:
    return BrowserLaneEvaluationHelpers(
        missing_lane=_missing_lane,
        lane_from_status=_lane_from_status,
        normalized_status=_normalized_status,
        blocker=_blocker,
        finding=_finding,
        stale_lane_blockers=_stale_lane_blockers,
        current_run_blockers=_current_run_blockers,
        recursive_current_run_blockers=_recursive_current_run_blockers,
        producer_source_artifact_blockers=_producer_source_artifact_blockers,
        source_lane_check_producer_blockers=_source_lane_check_producer_blockers,
        has_live_lane_evidence=lambda payload: _has_live_lane_evidence(
            payload,
            live_flag=two_node_e2e_browser_lane.BROWSER_LIVE_FLAG,
        ),
        has_producer_backed_lane_evidence=_has_producer_backed_lane_evidence,
        has_mock_or_fixture=_has_mock_or_fixture,
        has_historical_latest=_has_historical_latest,
        source_records=_source_records,
        check_results=_check_results,
        identity_match_status=_identity_match_status,
        with_context=_with_context,
    )


def _logs_lane_helpers() -> LogsLaneEvaluationHelpers[LaneEvaluation]:
    return LogsLaneEvaluationHelpers(
        missing_lane=_missing_lane,
        lane_from_status=_lane_from_status,
        normalized_status=_normalized_status,
        blocker=_blocker,
        finding=_finding,
        stale_lane_blockers=_stale_lane_blockers,
        current_run_blockers=_current_run_blockers,
        recursive_current_run_blockers=_recursive_current_run_blockers,
        producer_source_artifact_blockers=_producer_source_artifact_blockers,
        source_lane_check_producer_blockers=_source_lane_check_producer_blockers,
        logs_check_published_artifact_blockers=_logs_check_published_artifact_blockers,
        has_live_lane_evidence=lambda payload: _has_live_lane_evidence(
            payload,
            live_flag=two_node_e2e_logs_lane.LOGS_LIVE_FLAG,
        ),
        has_producer_backed_lane_evidence=_has_producer_backed_lane_evidence,
        has_mock_or_fixture=_has_mock_or_fixture,
        has_historical_latest=_has_historical_latest,
        source_records=_source_records,
        check_results=_check_results,
        identity_match_status=_identity_match_status,
        with_context=_with_context,
    )


def _cross_plane_helpers() -> CrossPlaneEvaluationHelpers[LaneEvaluation]:
    return CrossPlaneEvaluationHelpers(
        missing_lane=_missing_lane,
        lane_from_status=_lane_from_status,
        normalized_status=_normalized_status,
        combined_status=_combined_status,
        blocker=_blocker,
        finding=_finding,
        stale_lane_blockers=_stale_lane_blockers,
        current_run_blockers=_current_run_blockers,
        recursive_current_run_blockers=_recursive_current_run_blockers,
        producer_source_artifact_blockers=_producer_source_artifact_blockers,
        has_live_lane_evidence=lambda payload: _has_live_lane_evidence(
            payload,
            live_flag=two_node_e2e_cross_plane_lane.CROSS_PLANE_LIVE_FLAG,
        ),
        has_producer_backed_lane_evidence=_has_producer_backed_lane_evidence,
        has_mock_or_fixture=_has_mock_or_fixture,
        has_historical_latest=_has_historical_latest,
        source_records=_source_records,
        identity_match_status=_identity_match_status,
        identity_value=_identity_value,
        redact_payload=redact_payload,
        with_context=_with_context,
    )


def _final_aggregation_helpers() -> FinalAggregationHelpers:
    return FinalAggregationHelpers(
        blocker=_blocker,
        finding=_finding,
        with_context=_with_context,
    )


def _manual_ops_lane_helpers() -> ManualOpsLaneEvaluationHelpers[LaneEvaluation]:
    return ManualOpsLaneEvaluationHelpers(
        missing_lane=_missing_lane,
        lane_from_status=_lane_from_status,
        normalized_status=_normalized_status,
        blocker=_blocker,
        finding=_finding,
        stale_lane_blockers=_stale_lane_blockers,
        current_run_blockers=_current_run_blockers,
        identity_match_status=_identity_match_status,
        with_context=_with_context,
        record_identity=_record_identity,
        source_name=_source_name,
        identity_value=_identity_value,
        strict_identity_value_matches=_strict_identity_value_matches,
        explicit_bundle_run_ids=_explicit_bundle_run_ids,
        explicit_bundle_run_ids_from_value=_explicit_bundle_run_ids_from_value,
        approved_artifact_path=_approved_artifact_path,
        approved_artifact_containment_root=_approved_artifact_containment_root,
        path_is_relative_to=_path_is_relative_to,
        public_path=_public_path,
        read_bytes_limited_no_follow=read_bytes_limited_no_follow,
        ensure_bounded_evidence_value=_ensure_bounded_evidence_value,
        evidence_error_type=TwoNodeE2EEvidenceError,
        safe_filesystem_error_type=SafeFilesystemError,
        current_evidence_run_id_keys=CURRENT_EVIDENCE_RUN_ID_KEYS,
        strict_identity_fields=STRICT_IDENTITY_FIELDS,
        max_evidence_payload_bytes=MAX_EVIDENCE_PAYLOAD_BYTES,
    )



def _final_status(
    lanes: Mapping[str, LaneEvaluation],
    source_scope_results: Mapping[str, Mapping[str, Any]],
    scope: Mapping[str, Any],
) -> str:
    return two_node_e2e_final_aggregation.final_status(lanes, source_scope_results, scope)


def _collect_blockers_and_findings(
    lanes: Mapping[str, LaneEvaluation],
    source_scope_results: Mapping[str, Mapping[str, Any]],
    scope: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return two_node_e2e_final_aggregation.collect_blockers_and_findings(
        lanes,
        source_scope_results,
        scope,
        helpers=_final_aggregation_helpers(),
    )


def _metadata_summary(
    doc: EvidenceDocument | None,
    metadata: Mapping[str, Any],
    metadata_lane: LaneEvaluation,
) -> dict[str, Any]:
    return two_node_e2e_final_aggregation.metadata_summary(doc, metadata, metadata_lane)


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
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise TwoNodeE2EEvidenceError(
            "TWO_NODE_E2E_EVIDENCE_JSON_INVALID",
            f"Evidence file is not valid UTF-8 JSON: {path}.",
        ) from error
    if not isinstance(payload, Mapping):
        raise TwoNodeE2EEvidenceError(
            "TWO_NODE_E2E_EVIDENCE_JSON_INVALID",
            f"Evidence JSON must be an object: {path}.",
        )
    _ensure_bounded_evidence_value(payload, path=path)
    return EvidenceDocument(
        path=path.resolve(strict=False),
        payload=payload,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _read_json_value(path: Path, *, containment_root: Path) -> Any:
    content = _read_json_bytes(path, containment_root=containment_root)
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise TwoNodeE2EEvidenceError(
            "TWO_NODE_E2E_EVIDENCE_JSON_INVALID",
            f"Evidence file is not valid UTF-8 JSON: {path}.",
        ) from error
    _ensure_bounded_evidence_value(payload, path=path)
    return payload


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


def _ensure_bounded_evidence_value(value: Any, *, path: Path) -> None:
    try:
        for _parent, _key, _nested, _depth in _walk_evidence_values(value):
            pass
    except TwoNodeE2EEvidenceError as error:
        raise TwoNodeE2EEvidenceError(
            "TWO_NODE_E2E_EVIDENCE_JSON_TOO_DEEP",
            f"Evidence JSON exceeds traversal bounds: {path}.",
        ) from error


def _walk_evidence_values(value: Any):
    stack: list[tuple[Any, str | None, Any, int]] = [(None, None, value, 0)]
    visited = 0
    while stack:
        parent, key, current, depth = stack.pop()
        visited += 1
        if visited > MAX_EVIDENCE_TRAVERSAL_NODES:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_JSON_TOO_DEEP",
                "Evidence JSON traversal node limit was exceeded.",
            )
        if depth > MAX_EVIDENCE_TRAVERSAL_DEPTH:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_JSON_TOO_DEEP",
                "Evidence JSON traversal depth limit was exceeded.",
            )
        yield parent, key, current, depth
        if isinstance(current, Mapping):
            for nested_key, nested in reversed(list(current.items())):
                stack.append((current, str(nested_key), nested, depth + 1))
        elif isinstance(current, list):
            for index in range(len(current) - 1, -1, -1):
                stack.append((current, str(index), current[index], depth + 1))


def _walk_producer_proof_candidate_values(value: Any):
    stack: list[tuple[Any, str | None, Any, int, bool, bool, bool]] = [
        (None, None, value, 0, False, False, True)
    ]
    visited = 0
    while stack:
        parent, key, current, depth, hidden_text_identity, authority_blocked, candidate_position = stack.pop()
        visited += 1
        if visited > MAX_EVIDENCE_TRAVERSAL_NODES:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_JSON_TOO_DEEP",
                "Evidence JSON traversal node limit was exceeded.",
            )
        if depth > MAX_EVIDENCE_TRAVERSAL_DEPTH:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_JSON_TOO_DEEP",
                "Evidence JSON traversal depth limit was exceeded.",
            )
        current_hidden_text_identity = hidden_text_identity or (
            key is not None and key in PRODUCER_TEXT_IDENTITY_KEYS
        )
        current_authority_blocked = authority_blocked or (
            key is not None and key in PRODUCER_NON_AUTHORITATIVE_PROOF_CONTAINER_KEYS
        )
        if candidate_position and not current_hidden_text_identity and not current_authority_blocked:
            yield parent, key, current, depth
        if isinstance(current, Mapping):
            for nested_key, nested in reversed(list(current.items())):
                nested_key_text = str(nested_key)
                stack.append(
                    (
                        current,
                        nested_key_text,
                        nested,
                        depth + 1,
                        current_hidden_text_identity,
                        current_authority_blocked,
                        _producer_proof_child_is_candidate_position(
                            key=nested_key_text,
                            authority_blocked=current_authority_blocked,
                            parent_is_candidate=candidate_position,
                        ),
                    )
                )
        elif isinstance(current, list):
            for index in range(len(current) - 1, -1, -1):
                stack.append(
                    (
                        current,
                        str(index),
                        current[index],
                        depth + 1,
                        current_hidden_text_identity,
                        current_authority_blocked,
                        candidate_position,
                    )
                )


def _producer_proof_child_is_candidate_position(
    *,
    key: str,
    authority_blocked: bool,
    parent_is_candidate: bool,
) -> bool:
    if authority_blocked:
        return False
    if key in PRODUCER_TEXT_IDENTITY_KEYS or key in PRODUCER_NON_AUTHORITATIVE_PROOF_CONTAINER_KEYS:
        return False
    if key in PRODUCER_AUTHORITATIVE_PROOF_CONTAINER_KEYS:
        return True
    if not parent_is_candidate:
        return False
    if _is_producer_check_key(key):
        return True
    return False


def _walk_producer_non_text_values(value: Any):
    stack: list[tuple[Any, str | None, Any, int, bool]] = [(None, None, value, 0, False)]
    visited = 0
    while stack:
        parent, key, current, depth, hidden_text_identity = stack.pop()
        visited += 1
        if visited > MAX_EVIDENCE_TRAVERSAL_NODES:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_JSON_TOO_DEEP",
                "Evidence JSON traversal node limit was exceeded.",
            )
        if depth > MAX_EVIDENCE_TRAVERSAL_DEPTH:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_JSON_TOO_DEEP",
                "Evidence JSON traversal depth limit was exceeded.",
            )
        current_hidden_text_identity = hidden_text_identity or (
            key is not None and key in PRODUCER_TEXT_IDENTITY_KEYS
        )
        if not current_hidden_text_identity:
            yield parent, key, current, depth
        if isinstance(current, Mapping):
            for nested_key, nested in reversed(list(current.items())):
                stack.append((current, str(nested_key), nested, depth + 1, current_hidden_text_identity))
        elif isinstance(current, list):
            for index in range(len(current) - 1, -1, -1):
                stack.append((current, str(index), current[index], depth + 1, current_hidden_text_identity))


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
        evidence=_bounded_evidence_payload(doc.payload),
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
        matches = _strict_identity_value_matches(identity_field, observed_value, expected_value)
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
    for identity_field in PRODUCER_TEXT_AUTHORITY_IDENTITY_FIELDS:
        if identity_field in record and identity_field not in identity:
            identity[identity_field] = record[identity_field]
    if "source_id" in record and "source" not in identity:
        identity["source"] = record["source_id"]
    if "source_id" in identity and "source" not in identity:
        identity["source"] = identity["source_id"]
    return identity


def _identity_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _strict_identity_value_matches(field: str, observed: Any, expected: Any) -> bool:
    observed_text = _identity_text(observed)
    expected_text = _identity_text(expected)
    if observed_text is None or expected_text is None:
        return False
    if field == "source":
        return _source_name(observed_text) == _source_name(expected_text)
    if field == "cycle_time":
        return _cycle_time_identity_matches(observed_text, expected_text)
    return observed_text == expected_text


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


def _has_producer_backed_lane_evidence(payload: Mapping[str, Any]) -> bool:
    if _has_source_artifact_proof(payload):
        return True
    for key in PRODUCER_EVIDENCE_KEYS:
        if key == "source_artifacts":
            continue
        if _structured_evidence_value(payload.get(key)):
            return True
    records = _source_records(payload)
    for record in records.values():
        if _has_source_artifact_proof(record):
            return True
        if _structured_evidence_value(record.get("evidence")) or _structured_evidence_value(record.get("proofs")):
            return True
        for check in _check_results(record).values():
            if _check_has_producer_evidence(check):
                return True
    return False


def _check_has_producer_evidence(check: Mapping[str, Any]) -> bool:
    for key in PRODUCER_EVIDENCE_KEYS:
        if key == "source_artifacts":
            if _has_source_artifact_proof(check):
                return True
            continue
        if _structured_evidence_value(check.get(key)):
            return True
    return False


def _source_lane_check_producer_blockers(
    lane_name: str,
    payload: Mapping[str, Any],
    *,
    declared_sources: tuple[str, ...],
    required_checks: tuple[str, ...],
    strict_identities: Mapping[str, Mapping[str, Any]],
    evidence_run_id: str,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    records = _source_records(payload)
    for source in declared_sources:
        record = records.get(source)
        if record is None:
            continue
        check_results = _check_results(record)
        for check_name in required_checks:
            check = check_results.get(check_name)
            if check is None:
                continue
            expected_identity = strict_identities.get(source, {})
            check_blockers: list[dict[str, Any]] = []
            source_scoped_scan = _source_scoped_producer_evidence_scan(
                record,
                lane_name=lane_name,
                source=source,
                check=check_name,
                expected_identity=expected_identity,
                evidence_run_id=evidence_run_id,
            )
            source_conflict_blockers = _producer_identity_conflict_blockers(source_scoped_scan[0])
            check_blockers = _check_producer_identity_blockers(
                check,
                lane_name=lane_name,
                source=source,
                check_name=check_name,
                expected_identity=expected_identity,
                evidence_run_id=evidence_run_id,
            )
            conflict_blockers = _producer_identity_conflict_blockers(check_blockers)
            if conflict_blockers:
                blockers.extend(conflict_blockers)
                blockers.extend(source_conflict_blockers)
                continue
            if source_conflict_blockers:
                blockers.extend(source_conflict_blockers)
                continue
            if _check_has_producer_evidence(check):
                blockers.extend(check_blockers)
                continue
            source_scoped_blockers = _source_scoped_producer_evidence_blockers(
                record,
                lane_name=lane_name,
                source=source,
                check=check_name,
                expected_identity=expected_identity,
                evidence_run_id=evidence_run_id,
                scan=source_scoped_scan,
            )
            if not source_scoped_blockers:
                continue
            missing_code = f"TWO_NODE_E2E_{lane_name.upper()}_CHECK_PRODUCER_EVIDENCE_MISSING"
            if any(item.get("code") != missing_code for item in source_scoped_blockers):
                blockers.extend(source_scoped_blockers)
                continue
            if check_blockers:
                blockers.extend(check_blockers)
                continue
            blockers.append(
                _blocker(
                    f"TWO_NODE_E2E_{lane_name.upper()}_CHECK_PRODUCER_EVIDENCE_MISSING",
                    f"{lane_name} required check must include check-scoped producer evidence.",
                    source=source,
                    check=check_name,
                )
            )
    return blockers


def _source_scoped_producer_evidence_blockers(
    source_record: Mapping[str, Any],
    *,
    lane_name: str,
    source: str,
    check: str,
    expected_identity: Mapping[str, Any],
    evidence_run_id: str,
    scan: tuple[list[dict[str, Any]], bool, bool] | None = None,
) -> list[dict[str, Any]]:
    blockers, saw_structured, saw_matching = scan or _source_scoped_producer_evidence_scan(
        source_record,
        lane_name=lane_name,
        source=source,
        check=check,
        expected_identity=expected_identity,
        evidence_run_id=evidence_run_id,
    )
    if saw_matching:
        conflict_blockers = _producer_identity_conflict_blockers(blockers)
        if conflict_blockers:
            return conflict_blockers
        return []
    if blockers:
        return blockers
    if saw_structured:
        return [
            _blocker(
                f"TWO_NODE_E2E_{lane_name.upper()}_CHECK_PRODUCER_IDENTITY_UNSCOPED",
                f"{lane_name} source-scoped producer evidence must explicitly name the same source and check.",
                source=source,
                check=check,
            )
        ]
    return [
        _blocker(
            f"TWO_NODE_E2E_{lane_name.upper()}_CHECK_PRODUCER_EVIDENCE_MISSING",
            f"{lane_name} required check must include check-scoped producer evidence.",
            source=source,
            check=check,
        )
    ]


def _source_scoped_producer_evidence_scan(
    source_record: Mapping[str, Any],
    *,
    lane_name: str,
    source: str,
    check: str,
    expected_identity: Mapping[str, Any],
    evidence_run_id: str,
) -> tuple[list[dict[str, Any]], bool, bool]:
    saw_structured = False
    saw_matching = False
    blockers: list[dict[str, Any]] = []
    expected = _expected_producer_identity(
        source=source,
        check_name=check,
        expected_identity=expected_identity,
        evidence_run_id=evidence_run_id,
    )
    blockers.extend(
        _producer_full_surface_non_authoritative_structured_identity_conflict_blockers(
            source_record,
            lane_name=lane_name,
            expected=expected,
            required_identity_fields=_producer_required_identity_fields(check),
            excluded_root_keys=(*SOURCE_SCOPED_PRODUCER_EVIDENCE_KEYS, "checks", "check_results"),
        )
    )
    saw_structured = saw_structured or _producer_full_surface_non_authoritative_structured_record_present(
        source_record,
        target_check=check,
        excluded_root_keys=(*SOURCE_SCOPED_PRODUCER_EVIDENCE_KEYS, "checks", "check_results"),
    )
    source_record_text_blockers = _producer_full_scope_text_identity_blockers(
        source_record,
        lane_name=lane_name,
        expected=expected,
        excluded_root_keys=(*SOURCE_SCOPED_PRODUCER_EVIDENCE_KEYS, "checks", "check_results"),
    )
    for key in SOURCE_SCOPED_PRODUCER_EVIDENCE_KEYS:
        value = source_record.get(key)
        if value is None:
            continue
        non_artifact_source_artifacts = key == "source_artifacts" and not _source_artifact_records(value)
        for sibling_value in _source_scoped_check_keyed_sibling_values(value, target_check=check):
            blockers.extend(
                _producer_value_target_text_identity_blockers(
                    sibling_value,
                    lane_name=lane_name,
                    expected=expected,
                    target_check=check,
                    evidence_key=key,
                )
            )
            if non_artifact_source_artifacts:
                continue
            blockers.extend(
                _producer_value_hidden_record_identity_conflict_blockers(
                    sibling_value,
                    lane_name=lane_name,
                    expected=expected,
                    required_identity_fields=_producer_required_identity_fields(check),
                    default_to_target_context=False,
                )
            )
        for metadata_value in _source_scoped_keyed_metadata_text_values(value, target_check=check):
            blockers.extend(
                _producer_value_text_identity_blockers(
                    metadata_value,
                    lane_name=lane_name,
                    expected=expected,
                    evidence_key=key,
                )
            )
        for target_value in _source_scoped_target_text_values(value, target_check=check):
            blockers.extend(
                _producer_value_text_identity_blockers(
                    target_value,
                    lane_name=lane_name,
                    expected=expected,
                    evidence_key=key,
                )
            )
        if isinstance(value, list):
            blockers.extend(
                _producer_value_identity_only_record_conflict_blockers(
                    value,
                    lane_name=lane_name,
                    expected=expected,
                    required_identity_fields=_producer_required_identity_fields(check),
                    default_to_target_context=False,
                )
            )
        if non_artifact_source_artifacts:
            saw_structured = saw_structured or _producer_value_target_structured_record_present(
                value,
                target_check=check,
                default_to_target_context=False,
            )
            blockers.extend(
                _producer_value_hidden_record_identity_conflict_blockers(
                    value,
                    lane_name=lane_name,
                    expected=expected,
                    required_identity_fields=_producer_required_identity_fields(check),
                    default_to_target_context=False,
                )
            )
            continue
        metadata_blockers, metadata_saw_target_structured = (
            _source_scoped_keyed_metadata_structured_identity_conflict_blockers(
                value,
                lane_name=lane_name,
                expected=expected,
                required_identity_fields=_producer_required_identity_fields(check),
                target_check=check,
            )
        )
        blockers.extend(metadata_blockers)
        saw_structured = saw_structured or metadata_saw_target_structured
        for target_value in _source_scoped_target_proof_values(value, target_check=check):
            if not _structured_evidence_value(target_value):
                continue
            saw_structured = True
            value_blockers = _producer_value_semantic_identity_blockers(
                target_value,
                lane_name=lane_name,
                source=source,
                check_name=check,
                expected_identity=expected_identity,
                evidence_run_id=evidence_run_id,
                require_explicit_scope=True,
                include_text_conflicts=False,
            )
            if value_blockers:
                blockers.extend(value_blockers)
            else:
                saw_matching = True
    blockers.extend(source_record_text_blockers)
    return blockers, saw_structured, saw_matching


def _check_producer_identity_blockers(
    check: Mapping[str, Any],
    *,
    lane_name: str,
    source: str,
    check_name: str,
    expected_identity: Mapping[str, Any],
    evidence_run_id: str,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    saw_matching = False
    saw_structured = False
    expected = _expected_producer_identity(
        source=source,
        check_name=check_name,
        expected_identity=expected_identity,
        evidence_run_id=evidence_run_id,
    )
    blockers.extend(
        _producer_full_surface_non_authoritative_structured_identity_conflict_blockers(
            check,
            lane_name=lane_name,
            expected=expected,
            required_identity_fields=_producer_required_identity_fields(check_name),
            excluded_root_keys=PRODUCER_EVIDENCE_KEYS,
        )
    )
    saw_structured = saw_structured or _producer_full_surface_non_authoritative_structured_record_present(
        check,
        target_check=check_name,
        excluded_root_keys=PRODUCER_EVIDENCE_KEYS,
    )
    check_text_blockers = _producer_full_scope_text_identity_blockers(
        check,
        lane_name=lane_name,
        expected=expected,
        excluded_root_keys=PRODUCER_EVIDENCE_KEYS,
    )
    for key in PRODUCER_EVIDENCE_KEYS:
        if key == "source_artifacts":
            value = check.get(key)
            if value is not None:
                blockers.extend(
                    _producer_value_text_identity_blockers(
                        value,
                        lane_name=lane_name,
                        expected=expected,
                        evidence_key=key,
                        target_check=check_name,
                    )
                )
            if not _has_source_artifact_proof(check):
                if value is not None:
                    saw_structured = saw_structured or _producer_value_target_structured_record_present(
                        value,
                        target_check=check_name,
                        default_to_target_context=True,
                    )
                    blockers.extend(
                        _producer_value_hidden_record_identity_conflict_blockers(
                            value,
                            lane_name=lane_name,
                            expected=expected,
                            required_identity_fields=_producer_required_identity_fields(check_name),
                            default_to_target_context=True,
                        )
                    )
                continue
        else:
            value = check.get(key)
            if value is None:
                continue
            blockers.extend(
                _producer_value_text_identity_blockers(
                    value,
                    lane_name=lane_name,
                    expected=expected,
                    evidence_key=key,
                    target_check=check_name,
                )
            )
            blockers.extend(
                _producer_value_hidden_record_identity_conflict_blockers(
                    value,
                    lane_name=lane_name,
                    expected=expected,
                    required_identity_fields=_producer_required_identity_fields(check_name),
                    default_to_target_context=True,
                )
            )
            if not _structured_evidence_value(value):
                continue
        saw_structured = True
        value_blockers = _producer_value_semantic_identity_blockers(
            value,
            lane_name=lane_name,
            source=source,
            check_name=check_name,
            expected_identity=expected_identity,
            evidence_run_id=evidence_run_id,
            require_explicit_scope=False,
            include_text_conflicts=False,
        )
        if value_blockers:
            blockers.extend(value_blockers)
        else:
            saw_matching = True
    blockers.extend(check_text_blockers)
    if saw_matching:
        conflict_blockers = _producer_identity_conflict_blockers(blockers)
        if conflict_blockers:
            return conflict_blockers
        return []
    if blockers:
        return blockers
    if saw_structured:
        return [
            _blocker(
                f"TWO_NODE_E2E_{lane_name.upper()}_CHECK_PRODUCER_IDENTITY_UNSCOPED",
                f"{lane_name} required check producer evidence must bind to the same source and check.",
                source=source,
                check=check_name,
            )
        ]
    return []


def _producer_value_semantic_identity_blockers(
    value: Any,
    *,
    lane_name: str,
    source: str,
    check_name: str,
    expected_identity: Mapping[str, Any],
    evidence_run_id: str,
    require_explicit_scope: bool,
    include_text_conflicts: bool = True,
) -> list[dict[str, Any]]:
    expected = _expected_producer_identity(
        source=source,
        check_name=check_name,
        expected_identity=expected_identity,
        evidence_run_id=evidence_run_id,
    )
    required_identity_fields = _producer_required_identity_fields(check_name)
    blockers: list[dict[str, Any]] = []
    matching_records = 0
    scoped_records = 0
    text_blockers: list[dict[str, Any]] = []
    proof_candidate_ids: set[int] = set()
    if include_text_conflicts:
        text_blockers = _producer_value_text_identity_blockers(
            value,
            lane_name=lane_name,
            expected=expected,
            target_check=check_name,
        )
    for _parent, _key, nested, _depth in _walk_producer_proof_candidate_values(value):
        if not isinstance(nested, Mapping) or not _looks_like_evidence_record(nested):
            continue
        proof_candidate_ids.add(id(nested))
        record_blockers, record_matches, record_scoped = _producer_record_identity_blockers(
            nested,
            lane_name=lane_name,
            expected=expected,
            required_identity_fields=required_identity_fields,
        )
        blockers.extend(record_blockers)
        if record_matches:
            matching_records += 1
        if record_scoped:
            scoped_records += 1
    blockers.extend(
        _producer_value_hidden_record_identity_conflict_blockers(
            value,
            lane_name=lane_name,
            expected=expected,
            required_identity_fields=required_identity_fields,
            excluded_record_ids=proof_candidate_ids,
            default_to_target_context=True,
        )
    )
    blockers.extend(text_blockers)
    if blockers:
        return blockers
    if require_explicit_scope and scoped_records == 0:
        return [
            _blocker(
                f"TWO_NODE_E2E_{lane_name.upper()}_CHECK_PRODUCER_IDENTITY_UNSCOPED",
                f"{lane_name} source-scoped producer evidence must bind strict required check identity.",
                source=source,
                check=check_name,
                required_fields=list(required_identity_fields),
            )
        ]
    if matching_records == 0:
        return [
            _blocker(
                f"TWO_NODE_E2E_{lane_name.upper()}_CHECK_PRODUCER_IDENTITY_UNSCOPED",
                f"{lane_name} producer evidence identity is unscoped for the required check identity.",
                source=source,
                check=check_name,
                required_fields=list(required_identity_fields),
            )
        ]
    return []


def _producer_value_hidden_record_identity_conflict_blockers(
    value: Any,
    *,
    lane_name: str,
    expected: Mapping[str, str],
    required_identity_fields: Sequence[str],
    excluded_record_ids: set[int] = frozenset(),
    default_to_target_context: bool = False,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    expected_check = expected.get("check")
    for _parent, key, nested, _depth in _walk_producer_non_text_values(value):
        if not isinstance(nested, Mapping) or id(nested) in excluded_record_ids:
            continue
        if not _producer_record_has_explicit_identity(nested):
            continue
        if not _producer_record_applies_to_target_check(
            nested,
            target_check=expected_check,
            default_to_target_context=default_to_target_context,
        ):
            continue
        record_blockers, _record_matches, _record_scoped = _producer_record_identity_blockers(
            nested,
            lane_name=lane_name,
            expected=expected,
            required_identity_fields=required_identity_fields,
        )
        for blocker in record_blockers:
            if key is not None:
                blocker.setdefault("evidence_key", key)
            signature = (
                blocker.get("code"),
                blocker.get("source"),
                blocker.get("check"),
                blocker.get("field"),
                blocker.get("observed"),
                blocker.get("expected"),
            )
            if signature in seen:
                continue
            seen.add(signature)
            blockers.append(blocker)
    return blockers


def _producer_value_identity_only_record_conflict_blockers(
    value: Any,
    *,
    lane_name: str,
    expected: Mapping[str, str],
    required_identity_fields: Sequence[str],
    default_to_target_context: bool = False,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    expected_check = expected.get("check")
    for _parent, key, nested, _depth in _walk_producer_non_text_values(value):
        if not isinstance(nested, Mapping):
            continue
        if _looks_like_evidence_record(nested):
            continue
        if not _producer_record_has_explicit_identity(nested):
            continue
        if not _producer_record_applies_to_target_check(
            nested,
            target_check=expected_check,
            default_to_target_context=default_to_target_context,
        ):
            continue
        record_blockers, _record_matches, _record_scoped = _producer_record_identity_blockers(
            nested,
            lane_name=lane_name,
            expected=expected,
            required_identity_fields=required_identity_fields,
        )
        for blocker in record_blockers:
            if key is not None:
                blocker.setdefault("evidence_key", key)
            signature = (
                blocker.get("code"),
                blocker.get("source"),
                blocker.get("check"),
                blocker.get("field"),
                blocker.get("observed"),
                blocker.get("expected"),
            )
            if signature in seen:
                continue
            seen.add(signature)
            blockers.append(blocker)
    return blockers


def _producer_full_surface_non_authoritative_structured_identity_conflict_blockers(
    value: Mapping[str, Any],
    *,
    lane_name: str,
    expected: Mapping[str, str],
    required_identity_fields: Sequence[str],
    excluded_root_keys: Sequence[str],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for key, nested in _producer_full_surface_non_authoritative_structured_values(
        value,
        excluded_root_keys=frozenset(excluded_root_keys),
    ):
        wrapper_blockers = _producer_value_hidden_record_identity_conflict_blockers(
            nested,
            lane_name=lane_name,
            expected=expected,
            required_identity_fields=required_identity_fields,
            default_to_target_context=False,
        )
        for blocker in wrapper_blockers:
            blocker.setdefault("evidence_key", key)
            signature = (
                blocker.get("code"),
                blocker.get("source"),
                blocker.get("check"),
                blocker.get("field"),
                blocker.get("observed"),
                blocker.get("expected"),
                blocker.get("evidence_key"),
            )
            if signature in seen:
                continue
            seen.add(signature)
            blockers.append(blocker)
    return blockers


def _producer_full_surface_non_authoritative_structured_record_present(
    value: Mapping[str, Any],
    *,
    target_check: str,
    excluded_root_keys: Sequence[str],
) -> bool:
    for _key, nested in _producer_full_surface_non_authoritative_structured_values(
        value,
        excluded_root_keys=frozenset(excluded_root_keys),
    ):
        if _producer_value_target_structured_record_present(
            nested,
            target_check=target_check,
            default_to_target_context=False,
        ):
            return True
    return False


def _producer_full_surface_non_authoritative_structured_values(
    value: Any,
    *,
    excluded_root_keys: frozenset[str],
):
    stack: list[tuple[Any, str | None, Any, int]] = [(None, None, value, 0)]
    visited = 0
    while stack:
        parent, key, current, depth = stack.pop()
        visited += 1
        if visited > MAX_EVIDENCE_TRAVERSAL_NODES:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_JSON_TOO_DEEP",
                "Evidence JSON traversal node limit was exceeded.",
            )
        if depth > MAX_EVIDENCE_TRAVERSAL_DEPTH:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_JSON_TOO_DEEP",
                "Evidence JSON traversal depth limit was exceeded.",
            )
        if depth == 1 and key in excluded_root_keys:
            continue
        if (
            depth >= 1
            and (isinstance(current, Mapping) or isinstance(current, list))
            and _producer_value_contains_explicit_identity(current)
        ):
            yield key, current
            continue
        if isinstance(current, Mapping):
            for nested_key, nested in reversed(list(current.items())):
                stack.append((current, str(nested_key), nested, depth + 1))
        elif isinstance(current, list):
            for index in range(len(current) - 1, -1, -1):
                stack.append((current, str(index), current[index], depth + 1))


def _source_scoped_keyed_metadata_structured_identity_conflict_blockers(
    value: Any,
    *,
    lane_name: str,
    expected: Mapping[str, str],
    required_identity_fields: Sequence[str],
    target_check: str,
) -> tuple[list[dict[str, Any]], bool]:
    if not _is_check_keyed_producer_mapping(value):
        return [], False
    blockers: list[dict[str, Any]] = []
    saw_target_structured = False
    for key, nested in value.items():
        key_text = str(key)
        if _is_producer_check_key(key_text):
            continue
        metadata_value = {key_text: nested}
        if _producer_value_target_structured_record_present(
            metadata_value,
            target_check=target_check,
            default_to_target_context=False,
        ):
            saw_target_structured = True
        blockers.extend(
            _producer_value_hidden_record_identity_conflict_blockers(
                metadata_value,
                lane_name=lane_name,
                expected=expected,
                required_identity_fields=required_identity_fields,
                default_to_target_context=False,
            )
        )
    return blockers, saw_target_structured


def _source_scoped_check_keyed_sibling_values(value: Any, *, target_check: str) -> list[Any]:
    if not _is_check_keyed_producer_mapping(value):
        return []
    return [
        nested
        for key, nested in value.items()
        if _is_producer_check_key(str(key))
        and not _producer_identity_value_matches("check", str(key), target_check)
    ]


def _producer_value_target_structured_record_present(
    value: Any,
    *,
    target_check: str,
    default_to_target_context: bool,
) -> bool:
    for _parent, _key, nested, _depth in _walk_producer_non_text_values(value):
        if not isinstance(nested, Mapping):
            continue
        if not (_looks_like_evidence_record(nested) or _producer_record_has_explicit_identity(nested)):
            continue
        if _producer_record_applies_to_target_check(
            nested,
            target_check=target_check,
            default_to_target_context=default_to_target_context,
        ):
            return True
    return False


def _producer_record_applies_to_target_check(
    record: Mapping[str, Any],
    *,
    target_check: str | None,
    default_to_target_context: bool,
) -> bool:
    if not target_check:
        return True
    check_values = _producer_record_explicit_values(record, PRODUCER_CHECK_KEYS)
    if not check_values:
        return default_to_target_context
    return any(_producer_identity_value_matches("check", item, target_check) for item in check_values)


def _producer_value_contains_explicit_identity(value: Any) -> bool:
    for _parent, _key, nested, _depth in _walk_producer_non_text_values(value):
        if isinstance(nested, Mapping) and _producer_record_has_explicit_identity(nested):
            return True
    return False


def _producer_record_has_explicit_identity(record: Mapping[str, Any]) -> bool:
    identity_key_sets = (
        PRODUCER_SOURCE_KEYS,
        PRODUCER_CHECK_KEYS,
        frozenset({"run_id"}),
        frozenset({"cycle_time"}),
        frozenset({"model_id"}),
        frozenset({"job_id"}),
        frozenset(CURRENT_EVIDENCE_RUN_ID_KEYS),
    )
    return any(_producer_record_explicit_values(record, keys) for keys in identity_key_sets)


def _producer_identity_conflict_blockers(blockers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(blocker)
        for blocker in blockers
        if str(blocker.get("code", "")).endswith("_CHECK_PRODUCER_IDENTITY_MISMATCH")
    ]


def _expected_producer_identity(
    *,
    source: str,
    check_name: str,
    expected_identity: Mapping[str, Any],
    evidence_run_id: str,
) -> dict[str, str]:
    expected: dict[str, str] = {"source": _source_name(source) or str(source), "check": check_name}
    for identity_field in STRICT_LOG_IDENTITY_FIELDS:
        value = _identity_value(expected_identity, identity_field)
        if value:
            expected[identity_field] = value
    expected.setdefault("source", _source_name(source) or str(source))
    expected["evidence_run_id"] = evidence_run_id
    return expected


def _producer_required_identity_fields(check_name: str) -> tuple[str, ...]:
    if _producer_check_token(check_name) in PRODUCER_JOB_ID_REQUIRED_CHECKS:
        return (*PRODUCER_BASE_REQUIRED_IDENTITY_FIELDS, "job_id")
    return PRODUCER_BASE_REQUIRED_IDENTITY_FIELDS


def _producer_record_identity_blockers(
    record: Mapping[str, Any],
    *,
    lane_name: str,
    expected: Mapping[str, str],
    required_identity_fields: Sequence[str],
) -> tuple[list[dict[str, Any]], bool, bool]:
    blockers: list[dict[str, Any]] = []
    expected_source = expected.get("source")
    expected_check = expected.get("check")
    matched_fields: set[str] = set()
    for identity_field in ("source", "check", "run_id", "cycle_time", "model_id", "job_id", "evidence_run_id"):
        if identity_field == "source":
            values = _producer_record_explicit_values(record, PRODUCER_SOURCE_KEYS)
        elif identity_field == "check":
            values = _producer_record_explicit_values(record, PRODUCER_CHECK_KEYS)
        elif identity_field == "evidence_run_id":
            values = _producer_record_explicit_values(record, frozenset(CURRENT_EVIDENCE_RUN_ID_KEYS))
        else:
            values = _producer_record_explicit_values(record, frozenset({identity_field}))
        expected_value = expected.get(identity_field)
        if not expected_value:
            continue
        field_matched = False
        for observed in values:
            if _producer_identity_value_matches(identity_field, observed, expected_value):
                field_matched = True
            else:
                blockers.append(
                    _blocker(
                        f"TWO_NODE_E2E_{lane_name.upper()}_CHECK_PRODUCER_IDENTITY_MISMATCH",
                        f"{lane_name} producer evidence identity conflicts with the required check identity.",
                        source=expected_source,
                        check=expected_check,
                        field=identity_field,
                        observed=observed,
                        expected=expected_value,
                    )
                )
        if field_matched:
            matched_fields.add(identity_field)
    record_scoped = {"source", "check"}.issubset(matched_fields)
    if blockers:
        return blockers, False, record_scoped
    record_matches = all(field in matched_fields for field in required_identity_fields if expected.get(field))
    return [], record_matches, record_scoped


def _producer_record_explicit_values(record: Mapping[str, Any], keys: frozenset[str]) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = record.get(key)
        if value is not None and not isinstance(value, Mapping) and not isinstance(value, list):
            text = str(value).strip()
            if text:
                values.append(text)
    for key in keys:
        for value in _producer_record_authoritative_identity_values(record, key):
            text = str(value).strip()
            if text and text not in values:
                values.append(text)
    return values


def _producer_record_authoritative_identity_values(record: Mapping[str, Any], key: str) -> list[Any]:
    identity_keys = ("source", "source_id") if key in {"source", "source_id"} else (key,)
    values: list[Any] = []
    for object_key in ("identity", "strict_identity"):
        identity = record.get(object_key)
        if not isinstance(identity, Mapping):
            continue
        for identity_key in identity_keys:
            value = identity.get(identity_key)
            if value is not None:
                values.append(value)
    return values


def _producer_identity_value_matches(field: str, observed: Any, expected: str) -> bool:
    if observed is None or not expected:
        return False
    observed_text = str(observed).strip()
    expected_text = str(expected).strip()
    if not observed_text:
        return False
    if field == "source":
        return _source_name(observed_text) == _source_name(expected_text)
    if field == "check":
        return _producer_check_value_matches(observed_text, expected_text)
    if field == "cycle_time":
        return _cycle_time_identity_matches(observed_text, expected_text)
    return observed_text == expected_text


def _cycle_time_identity_matches(observed: str, expected: str) -> bool:
    if observed == expected:
        return True
    observed_normalized = _normalized_cycle_time_identity(observed)
    expected_normalized = _normalized_cycle_time_identity(expected)
    return bool(observed_normalized and expected_normalized and observed_normalized == expected_normalized)


def _normalized_cycle_time_identity(value: str) -> str | None:
    candidate = value.strip()
    try:
        if len(candidate) == 10 and candidate.isdigit():
            parsed = datetime.strptime(candidate, "%Y%m%d%H").replace(tzinfo=UTC)
        else:
            iso_candidate = f"{candidate[:-1]}+00:00" if candidate.endswith("Z") else candidate
            parsed = datetime.fromisoformat(iso_candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            else:
                parsed = parsed.astimezone(UTC)
    except ValueError:
        return None
    return parsed.strftime("%Y%m%d%H")


def _producer_check_value_matches(observed: str, expected: str) -> bool:
    observed_norm = _producer_check_token(observed)
    expected_norm = _producer_check_token(expected)
    if observed_norm == expected_norm:
        return True
    return _producer_text_mentions_check(observed, expected)


def _producer_check_token(value: str) -> str:
    text = str(value).strip().lower().replace("-", "_")
    text = text.rsplit("/", maxsplit=1)[-1]
    return text


def _producer_value_text_identity_blockers(
    value: Any,
    *,
    lane_name: str,
    expected: Mapping[str, str],
    evidence_key: str = "root",
    target_check: str | None = None,
) -> list[dict[str, Any]]:
    expected_source = expected.get("source")
    expected_check = expected.get("check")
    return [
        _blocker(
            f"TWO_NODE_E2E_{lane_name.upper()}_CHECK_PRODUCER_IDENTITY_MISMATCH",
            f"{lane_name} producer evidence text conflicts with the required check identity.",
            source=expected_source,
            check=expected_check,
            **conflict,
        )
        for conflict in _producer_value_text_identity_conflicts(
            value,
            expected=expected,
            evidence_key=evidence_key,
            target_check=target_check,
        )
    ]


def _producer_value_target_text_identity_blockers(
    value: Any,
    *,
    lane_name: str,
    expected: Mapping[str, str],
    target_check: str,
    evidence_key: str = "root",
) -> list[dict[str, Any]]:
    expected_source = expected.get("source")
    expected_check = expected.get("check")
    return [
        _blocker(
            f"TWO_NODE_E2E_{lane_name.upper()}_CHECK_PRODUCER_IDENTITY_MISMATCH",
            f"{lane_name} producer evidence text conflicts with the required check identity.",
            source=expected_source,
            check=expected_check,
            **conflict,
        )
        for conflict in _producer_value_target_text_identity_conflicts(
            value,
            expected=expected,
            target_check=target_check,
            evidence_key=evidence_key,
        )
    ]


def _producer_full_scope_text_identity_blockers(
    value: Any,
    *,
    lane_name: str,
    expected: Mapping[str, str],
    excluded_root_keys: Sequence[str],
) -> list[dict[str, Any]]:
    expected_source = expected.get("source")
    expected_check = expected.get("check")
    return [
        _blocker(
            f"TWO_NODE_E2E_{lane_name.upper()}_CHECK_PRODUCER_IDENTITY_MISMATCH",
            f"{lane_name} producer evidence text conflicts with the required check identity.",
            source=expected_source,
            check=expected_check,
            **conflict,
        )
        for conflict in _producer_full_scope_text_identity_conflicts(
            value,
            expected=expected,
            excluded_root_keys=excluded_root_keys,
        )
    ]


def _producer_full_scope_text_identity_conflicts(
    value: Any,
    *,
    expected: Mapping[str, str],
    excluded_root_keys: Sequence[str],
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    excluded_keys = frozenset(excluded_root_keys)
    for _parent, key, nested, _depth in _walk_producer_full_scope_text_values(
        value,
        excluded_root_keys=excluded_keys,
    ):
        if key not in PRODUCER_TEXT_IDENTITY_KEYS:
            if key in PRODUCER_NON_AUTHORITATIVE_PROOF_CONTAINER_KEYS:
                for text in _producer_non_authoritative_scalar_texts(nested):
                    parsed_values = _identity_values_from_text(text)
                    if _producer_text_names_only_other_check(parsed_values, expected):
                        continue
                    conflicts.extend(
                        _producer_text_identity_conflicts(
                            text,
                            expected=expected,
                            evidence_key=key,
                            parsed_values=parsed_values,
                        )
                    )
            continue
        if isinstance(nested, Mapping):
            text = json.dumps(nested, sort_keys=True, default=str)
        elif isinstance(nested, list):
            text = json.dumps(nested, sort_keys=True, default=str)
        elif isinstance(nested, str):
            text = nested
        else:
            continue
        parsed_values = _identity_values_from_text(text)
        if _producer_text_names_only_other_check(parsed_values, expected):
            continue
        conflicts.extend(
            _producer_text_identity_conflicts(
                text,
                expected=expected,
                evidence_key=key,
                parsed_values=parsed_values,
            )
        )
    return conflicts


def _walk_producer_full_scope_text_values(
    value: Any,
    *,
    excluded_root_keys: frozenset[str],
):
    stack: list[tuple[Any, str | None, Any, int]] = [(None, None, value, 0)]
    visited = 0
    while stack:
        parent, key, current, depth = stack.pop()
        visited += 1
        if visited > MAX_EVIDENCE_TRAVERSAL_NODES:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_JSON_TOO_DEEP",
                "Evidence JSON traversal node limit was exceeded.",
            )
        if depth > MAX_EVIDENCE_TRAVERSAL_DEPTH:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_JSON_TOO_DEEP",
                "Evidence JSON traversal depth limit was exceeded.",
            )
        if depth == 1 and key in excluded_root_keys:
            continue
        yield parent, key, current, depth
        if isinstance(current, Mapping):
            for nested_key, nested in reversed(list(current.items())):
                stack.append((current, str(nested_key), nested, depth + 1))
        elif isinstance(current, list):
            for index in range(len(current) - 1, -1, -1):
                stack.append((current, str(index), current[index], depth + 1))


def _producer_value_text_identity_conflicts(
    value: Any,
    *,
    expected: Mapping[str, str],
    evidence_key: str = "root",
    target_check: str | None = None,
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    target_expected = {"check": target_check} if target_check is not None else expected
    for text, text_evidence_key, root_scalar_text, parent in _producer_identity_text_context_items(
        value,
        root_evidence_key=evidence_key,
    ):
        parsed_values = _identity_values_from_text(text)
        if (root_scalar_text or target_check is not None) and _producer_text_names_only_other_check(
            parsed_values,
            target_expected,
        ):
            if root_scalar_text or not _producer_text_parent_applies_to_target_check(
                parent,
                target_check=target_check,
            ):
                continue
        conflicts.extend(
            _producer_text_identity_conflicts(
                text,
                expected=expected,
                evidence_key=text_evidence_key,
                parsed_values=parsed_values,
            )
        )
    return conflicts


def _producer_value_target_text_identity_conflicts(
    value: Any,
    *,
    expected: Mapping[str, str],
    target_check: str,
    evidence_key: str = "root",
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    value_has_target_context = _producer_value_target_structured_record_present(
        value,
        target_check=target_check,
        default_to_target_context=False,
    )
    for text, text_evidence_key, _root_scalar_text in _producer_identity_text_items(
        value,
        root_evidence_key=evidence_key,
    ):
        parsed_values = _identity_values_from_text(text)
        if not _producer_text_applies_to_target_check(
            text,
            parsed_values=parsed_values,
            target_check=target_check,
            value_has_target_context=value_has_target_context,
        ):
            continue
        conflicts.extend(
            _producer_text_identity_conflicts(
                text,
                expected=expected,
                evidence_key=text_evidence_key,
                parsed_values=parsed_values,
            )
        )
    return conflicts


def _producer_text_identity_conflicts(
    value: str,
    *,
    expected: Mapping[str, str],
    evidence_key: str,
    parsed_values: Mapping[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    if parsed_values is None:
        parsed_values = _identity_values_from_text(value)
    conflicts: list[dict[str, Any]] = []
    for identity_field in PRODUCER_TEXT_AUTHORITY_IDENTITY_FIELDS:
        expected_value = expected.get(identity_field)
        if not expected_value:
            continue
        for observed in parsed_values.get(identity_field, []):
            if not _producer_identity_value_matches(identity_field, observed, expected_value):
                conflicts.append(
                    {
                        "field": identity_field,
                        "observed": observed,
                        "expected": expected_value,
                        "evidence_key": evidence_key,
                    }
                )
    return conflicts


def _producer_record_text_mentions_identity(record: Mapping[str, Any], field: str, expected: str | None) -> bool:
    if not expected:
        return False
    for _parent, key, nested, _depth in _walk_evidence_values(record):
        if key not in PRODUCER_TEXT_IDENTITY_KEYS or not isinstance(nested, str):
            continue
        parsed_values = _identity_values_from_text(nested)
        if any(_producer_identity_value_matches(field, item, expected) for item in parsed_values.get(field, [])):
            return True
        if field == "check" and _producer_text_mentions_check(nested, expected):
            return True
    return False


def _producer_identity_text_items(
    value: Any,
    *,
    root_evidence_key: str = "root",
):
    for text, evidence_key, root_scalar_text, _parent in _producer_identity_text_context_items(
        value,
        root_evidence_key=root_evidence_key,
    ):
        yield text, evidence_key, root_scalar_text


def _producer_identity_text_context_items(
    value: Any,
    *,
    root_evidence_key: str = "root",
):
    if isinstance(value, str):
        yield value, root_evidence_key, True, None
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                yield item, root_evidence_key, True, None
    for _parent, key, nested, _depth in _walk_evidence_values(value):
        if key in PRODUCER_NON_AUTHORITATIVE_PROOF_CONTAINER_KEYS:
            for text in _producer_non_authoritative_scalar_texts(nested):
                yield text, key, True, _parent
            continue
        if key not in PRODUCER_TEXT_IDENTITY_KEYS:
            continue
        if isinstance(nested, Mapping):
            text = json.dumps(nested, sort_keys=True, default=str)
        elif isinstance(nested, list):
            text = json.dumps(nested, sort_keys=True, default=str)
        elif isinstance(nested, str):
            text = nested
        else:
            continue
        yield text, key, False, _parent


def _producer_text_parent_applies_to_target_check(parent: Any, *, target_check: str | None) -> bool:
    if not isinstance(parent, Mapping) or not target_check:
        return False
    return _producer_record_applies_to_target_check(
        parent,
        target_check=target_check,
        default_to_target_context=False,
    )


def _producer_non_authoritative_scalar_texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _producer_text_names_only_other_check(
    parsed_values: Mapping[str, list[str]],
    expected: Mapping[str, str],
) -> bool:
    expected_check = expected.get("check")
    if not expected_check:
        return False
    check_values = parsed_values.get("check", [])
    return bool(check_values) and not any(
        _producer_identity_value_matches("check", item, expected_check) for item in check_values
    )


def _producer_text_applies_to_target_check(
    value: str,
    *,
    parsed_values: Mapping[str, list[str]],
    target_check: str,
    value_has_target_context: bool = False,
) -> bool:
    expected = {"check": target_check}
    if _producer_text_names_only_other_check(parsed_values, expected):
        return False
    if _producer_text_mentions_check(value, target_check):
        return True
    return value_has_target_context and _producer_log_uri_text_applies_to_target_check(
        parsed_values,
        target_check=target_check,
    )


def _producer_log_uri_text_applies_to_target_check(
    parsed_values: Mapping[str, list[str]],
    *,
    target_check: str,
) -> bool:
    if _producer_check_token(target_check) not in PRODUCER_JOB_ID_REQUIRED_CHECKS:
        return False
    return all(parsed_values.get(field) for field in LOG_URI_REQUIRED_IDENTITY_FIELDS)


def _producer_text_mentions_check(value: str, expected: str) -> bool:
    expected_token = _producer_check_token(expected)
    if not expected_token:
        return False
    parsed_values = _identity_values_from_text(value)
    if any(_producer_check_token(item) == expected_token for item in parsed_values.get("check", [])):
        return True
    return any(
        _producer_check_alias_matches_text(alias, value)
        for alias in PRODUCER_CHECK_ALIASES.get(expected_token, ())
    )


def _identity_values_from_text(value: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {field: [] for field in PRODUCER_TEXT_AUTHORITY_IDENTITY_FIELDS}
    text = str(value)
    try:
        parsed = urlsplit(text)
    except ValueError:
        parsed = None
    query = parsed.query if parsed is not None else ""
    path = parsed.path if parsed is not None else text
    for key, item in parse_qsl(query, keep_blank_values=True):
        _append_text_identity_value(values, key, item)
    _append_log_uri_identity_values(values, text)
    for key, item in re.findall(r"([A-Za-z][A-Za-z0-9_-]*)=([^&\s,'\"}]+)", text):
        _append_text_identity_value(values, key, item)
    for key, item in re.findall(r"['\"]([A-Za-z][A-Za-z0-9_-]*)['\"]\s*:\s*['\"]([^'\"]+)['\"]", text):
        _append_text_identity_value(values, key, item)
    for source_name in _identity_sources_from_path(path):
        values["source"].append(source_name)
    values["check"].extend(_identity_checks_from_path(path))
    return {key: _dedupe_text_values(items) for key, items in values.items()}


def _producer_check_alias_matches_text(alias: str, text: str) -> bool:
    normalized_alias = _producer_check_token(alias)
    if not normalized_alias:
        return False
    alias_text = str(alias).strip()
    if not alias_text.startswith("/"):
        return any(token == normalized_alias for token in _producer_check_candidate_tokens(text))
    try:
        parsed = urlsplit(str(text))
    except ValueError:
        return False
    alias_path = alias_text.lower()
    path = unquote(parsed.path if parsed.scheme or parsed.netloc else str(text)).lower()
    return _normalized_path_matches_alias(path, alias_path)


def _producer_check_candidate_tokens(text: str) -> list[str]:
    try:
        parsed = urlsplit(str(text))
    except ValueError:
        parsed = None
    candidates: list[str] = []
    if parsed is not None:
        candidates.extend(part for part in PurePosixPath(unquote(parsed.path)).parts if part not in {"", "/"})
    candidates.extend(re.findall(r"[A-Za-z][A-Za-z0-9_-]*", str(text)))
    return [_producer_check_token(candidate) for candidate in candidates]


def _normalized_path_matches_alias(path: str, alias_path: str) -> bool:
    normalized_path = "/" + "/".join(
        _producer_check_token(part) for part in PurePosixPath(path).parts if part not in {"", "/"}
    )
    normalized_alias = "/" + "/".join(
        _producer_check_token(part) for part in PurePosixPath(alias_path).parts if part not in {"", "/"}
    )
    return normalized_path == normalized_alias or normalized_path.endswith(normalized_alias)


def _identity_checks_from_path(path: str) -> list[str]:
    parts = [unquote(part) for part in PurePosixPath(path).parts if part not in {"", "/"}]
    checks: list[str] = []
    for index, part in enumerate(parts):
        if part.lower() == "producer" and index + 2 < len(parts):
            checks.append(parts[index + 2])
    normalized_path = "/" + "/".join(_producer_check_token(part) for part in parts)
    alias_to_checks: dict[str, list[str]] = {}
    for check_name, aliases in PRODUCER_CHECK_ALIASES.items():
        for alias in aliases:
            alias_text = str(alias).strip()
            if not alias_text.startswith("/"):
                continue
            normalized_alias = "/" + "/".join(
                _producer_check_token(part) for part in PurePosixPath(alias_text).parts if part not in {"", "/"}
            )
            alias_to_checks.setdefault(normalized_alias, []).append(check_name)
    for normalized_alias, check_names in alias_to_checks.items():
        if len(check_names) != 1:
            continue
        if normalized_path == normalized_alias or normalized_path.endswith(normalized_alias):
            checks.append(check_names[0])
    return _dedupe_text_values(checks)


def _append_log_uri_identity_values(values: dict[str, list[str]], text: str) -> None:
    for uri in _candidate_log_uri_texts(text):
        for identity_field, observed_values in _parse_log_uri_identity(uri).items():
            values[identity_field].extend(observed_values)


def _parse_log_uri_identity(uri: str) -> dict[str, list[str]]:
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return {}
    if parsed.scheme not in {"published", "file", "s3"}:
        return {}
    path_parts = [unquote(part) for part in PurePosixPath(parsed.path).parts if part not in {"", "/"}]
    if parsed.scheme == "published" and parsed.netloc:
        path_parts.insert(0, unquote(parsed.netloc))
    try:
        logs_index = next(index for index, part in enumerate(path_parts) if part.lower() == "logs")
    except StopIteration:
        return {}
    log_parts = path_parts[logs_index + 1 :]
    if len(log_parts) not in {3, 4}:
        return {}
    source = log_parts[0]
    job_file = log_parts[3] if len(log_parts) == 4 else log_parts[2]
    job_id = _log_job_id_from_filename(job_file)
    if not source or not job_id:
        return {}
    if len(log_parts) == 4:
        cycle_time, run_id = log_parts[1], log_parts[2]
        return {
            "source": [source],
            "cycle_time": [cycle_time],
            "run_id": [run_id],
            "job_id": [job_id],
        }
    run_id = log_parts[1]
    return {"source": [source], "run_id": [run_id], "job_id": [job_id]}


def _log_job_id_from_filename(value: str) -> str | None:
    for suffix in LOG_URI_STREAM_SUFFIXES:
        if value.endswith(suffix):
            job_id = value[: -len(suffix)]
            return job_id or None
    return None


def _candidate_log_uri_texts(text: str) -> list[str]:
    stripped = text.strip()
    candidates = [stripped]
    candidates.extend(
        match.rstrip(".,;)]}'\"")
        for match in re.findall(r"(?:published|file|s3)://[^\s,'\"}]+", text)
    )
    return _dedupe_text_values([candidate for candidate in candidates if candidate])


def _identity_sources_from_path(path: str) -> list[str]:
    parts = [unquote(part) for part in PurePosixPath(path).parts if part not in {"", "/"}]
    sources: list[str] = []
    for index, part in enumerate(parts):
        if part.lower() == "logs" and index + 1 < len(parts):
            sources.append(parts[index + 1])
        if part.lower() == "producer" and index + 1 < len(parts):
            sources.append(parts[index + 1])
    return sources


def _append_text_identity_value(values: dict[str, list[str]], raw_key: str, raw_value: str) -> None:
    key = raw_key.strip().lower().replace("-", "_")
    value = unquote(str(raw_value).strip())
    if not value:
        return
    if key in {"source", "source_id"}:
        values["source"].append(value)
    elif key in {"run_id", "runid"}:
        values["run_id"].append(value)
    elif key in {"cycle_time", "cycletime"}:
        values["cycle_time"].append(value)
    elif key in {"model_id", "modelid"}:
        values["model_id"].append(value)
    elif key in {"job_id", "jobid"}:
        values["job_id"].append(value)
    elif key in {"check", "check_name", "operation", "route"}:
        values["check"].append(value)


def _dedupe_text_values(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _source_scoped_target_text_values(value: Any, *, target_check: str) -> list[Any]:
    if _is_check_keyed_producer_mapping(value):
        return [
            nested
            for key, nested in value.items()
            if _producer_identity_value_matches("check", str(key), target_check)
        ]
    if isinstance(value, list):
        return [
            item
            for item in value
            if not _producer_value_explicitly_identifies_other_check(item, target_check=target_check)
            or _producer_value_text_mentions_check(item, target_check)
        ]
    return [value]


def _source_scoped_target_proof_values(value: Any, *, target_check: str) -> list[Any]:
    if _is_check_keyed_producer_mapping(value):
        return _source_scoped_target_text_values(value, target_check=target_check)
    if isinstance(value, list):
        return [
            item
            for item in value
            if not _producer_value_explicitly_identifies_other_check(item, target_check=target_check)
        ]
    if _producer_value_explicitly_identifies_other_check(value, target_check=target_check):
        return []
    return [value]


def _source_scoped_keyed_metadata_text_values(value: Any, *, target_check: str) -> list[Mapping[str, Any]]:
    if not _is_check_keyed_producer_mapping(value):
        return []
    metadata_values: list[Mapping[str, Any]] = []
    for key, nested in value.items():
        key_text = str(key)
        if _is_producer_check_key(key_text):
            continue
        metadata_value = {key_text: nested}
        if _producer_value_text_mentions_check(
            metadata_value,
            target_check,
        ) or _source_scoped_keyed_log_uri_metadata_applies(
            metadata_value,
            target_check=target_check,
        ):
            metadata_values.append(metadata_value)
    return metadata_values


def _source_scoped_keyed_log_uri_metadata_applies(value: Any, *, target_check: str) -> bool:
    if _producer_check_token(target_check) not in PRODUCER_JOB_ID_REQUIRED_CHECKS:
        return False
    for text, _evidence_key, _root_scalar_text in _producer_identity_text_items(value):
        if _text_contains_log_uri_identity(text):
            return True
    return False


def _text_contains_log_uri_identity(text: str) -> bool:
    for uri in _candidate_log_uri_texts(str(text)):
        try:
            parsed = urlsplit(uri)
        except ValueError:
            continue
        if parsed.scheme not in {"published", "file", "s3"}:
            continue
        uri_identity = _log_uri_identity_values(uri)
        if all(uri_identity.get(field) for field in LOG_URI_REQUIRED_IDENTITY_FIELDS):
            return True
    return False


def _is_check_keyed_producer_mapping(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    for key in value:
        if _is_producer_check_key(str(key)):
            return True
    return False


def _is_producer_check_key(key: str) -> bool:
    known_checks = frozenset(PRODUCER_CHECK_ALIASES)
    key_token = _producer_check_token(key)
    if key_token in known_checks:
        return True
    return any(_producer_identity_value_matches("check", key, check_name) for check_name in known_checks)


def _producer_value_explicitly_identifies_other_check(value: Any, *, target_check: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    check_values: list[str] = []
    for _parent, _key, nested, _depth in _walk_producer_non_text_values(value):
        if not isinstance(nested, Mapping):
            continue
        check_values.extend(_producer_record_explicit_values(nested, PRODUCER_CHECK_KEYS))
    check_values.extend(_producer_value_hidden_text_check_values(value))
    return bool(check_values) and not any(
        _producer_identity_value_matches("check", item, target_check) for item in check_values
    )


def _producer_value_hidden_text_check_values(value: Any) -> list[str]:
    check_values: list[str] = []
    for text, _evidence_key, _root_scalar_text in _producer_identity_text_items(value):
        check_values.extend(_identity_values_from_text(text).get("check", []))
    return _dedupe_text_values(check_values)


def _producer_value_text_mentions_check(value: Any, target_check: str) -> bool:
    for text, _evidence_key, _root_scalar_text in _producer_identity_text_items(value):
        if _producer_text_mentions_check(text, target_check):
            return True
    return False


def _producer_value_mentions_scope(value: Any, *, source: str, check: str) -> bool:
    source_name = _source_name(source)
    for _parent, _key, nested, _depth in _walk_evidence_values(value):
        if not isinstance(nested, Mapping):
            continue
        if not _looks_like_evidence_record(nested):
            continue
        nested_source = _source_name(nested.get("source") or nested.get("source_id"))
        nested_check = str(
            nested.get("check")
            or nested.get("check_name")
            or nested.get("name")
            or nested.get("route")
            or nested.get("operation")
            or ""
        ).strip()
        if nested_source == source_name and nested_check == check:
            return True
    return False


def _recursive_current_run_blockers(
    value: Any,
    evidence_run_id: str,
    *,
    lane_name: str,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for _parent, key, nested, depth in _walk_evidence_values(value):
        if depth == 0 or key not in CURRENT_EVIDENCE_RUN_ID_KEYS:
            continue
        if nested is None or not str(nested).strip():
            continue
        if str(nested) != evidence_run_id:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH",
                    "Nested producer evidence belongs to a different evidence bundle.",
                    lane=lane_name,
                    key=key,
                    evidence_run_id=nested,
                    expected_evidence_run_id=evidence_run_id,
                )
            )
    return blockers


def _producer_source_artifact_blockers(
    value: Any,
    *,
    evidence_run_id: str,
    lane_name: str,
    run_dir: Path,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for record in _producer_source_artifact_records(value):
        artifact_blockers = _producer_source_artifact_record_blockers(
            record,
            evidence_run_id=evidence_run_id,
            lane_name=lane_name,
            run_dir=run_dir,
        )
        blockers.extend(artifact_blockers)
    return blockers


def _producer_source_artifact_records(value: Any) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for _parent, key, nested, _depth in _walk_evidence_values(value):
        if key != "source_artifacts":
            continue
        candidates: list[Any]
        if isinstance(nested, Mapping):
            candidates = list(nested.values())
        elif isinstance(nested, list):
            candidates = list(nested)
        else:
            continue
        for candidate in candidates:
            if isinstance(candidate, Mapping) and _source_artifact_records([candidate]):
                records.append(candidate)
    return records


def _producer_source_artifact_record_blockers(
    record: Mapping[str, Any],
    *,
    evidence_run_id: str,
    lane_name: str,
    run_dir: Path,
) -> list[dict[str, Any]]:
    raw_path = record.get("path") or record.get("artifact_path")
    raw_sha256 = record.get("sha256") or record.get("digest")
    blockers: list[dict[str, Any]] = []
    if not isinstance(raw_path, str) or not raw_path.strip():
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_PATH_MISSING",
                "Producer source artifact proof must include a path.",
                lane=lane_name,
            )
        )
        return blockers
    if not isinstance(raw_sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", raw_sha256.strip()):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_SHA_MISSING",
                "Producer source artifact proof must include a sha256 digest.",
                lane=lane_name,
                path=raw_path,
            )
        )
        return blockers
    try:
        path = _approved_artifact_path(raw_path)
    except TwoNodeE2EEvidenceError:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_OUTSIDE_APPROVED_ROOT",
                "Producer source artifact path must stay under approved evidence roots.",
                lane=lane_name,
                path=raw_path,
            )
        )
        return blockers
    explicit_ids = _explicit_bundle_run_ids(record)
    if not _path_is_relative_to(path, run_dir) and not (
        explicit_ids and all(str(value) == evidence_run_id for _, value in explicit_ids)
    ):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_STALE_OR_UNSCOPED",
                "Producer source artifact must be in the current run or explicitly bind to it.",
                lane=lane_name,
                path=_public_path(path),
                expected_evidence_run_id=evidence_run_id,
            )
        )
        return blockers
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
                "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_MISSING",
                "Producer source artifact file is missing.",
                lane=lane_name,
                path=_public_path(path),
            )
        )
        return blockers
    except SafeFilesystemError:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_PATH_UNSAFE",
                "Producer source artifact path is unsafe.",
                lane=lane_name,
                path=_public_path(path),
            )
        )
        return blockers
    if len(content) > MAX_EVIDENCE_PAYLOAD_BYTES:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_TOO_LARGE",
                "Producer source artifact file is too large.",
                lane=lane_name,
                path=_public_path(path),
            )
        )
        return blockers
    if hashlib.sha256(content).hexdigest() != raw_sha256.lower():
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_HASH_MISMATCH",
                "Producer source artifact sha256 does not match file content.",
                lane=lane_name,
                path=_public_path(path),
            )
        )
    try:
        payload = json.loads(content.decode("utf-8"))
        _ensure_bounded_evidence_value(payload, path=path)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TwoNodeE2EEvidenceError):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_JSON_INVALID",
                "Producer source artifact must be bounded valid JSON.",
                lane=lane_name,
                path=_public_path(path),
            )
        )
        return blockers
    nested_ids = _explicit_bundle_run_ids_from_value(payload)
    if not nested_ids:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_RUN_ID_MISSING",
                "Producer source artifact payload must bind to the current evidence run.",
                lane=lane_name,
                path=_public_path(path),
                expected_evidence_run_id=evidence_run_id,
            )
        )
    for key, value in nested_ids:
        if str(value) != evidence_run_id:
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_RUN_ID_MISMATCH",
                    "Producer source artifact payload belongs to a different evidence run.",
                    lane=lane_name,
                    path=_public_path(path),
                    key=key,
                    evidence_run_id=value,
                    expected_evidence_run_id=evidence_run_id,
                )
            )
    return blockers


def _logs_check_published_artifact_blockers(
    check: Mapping[str, Any],
    *,
    source_record: Mapping[str, Any],
    lane_payload: Mapping[str, Any],
    expected_identity: Mapping[str, Any],
    docker_security_payload: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    observed_identity = _record_identity(check)
    if not _identity_has_required_fields(observed_identity, STRICT_LOG_IDENTITY_FIELDS):
        return []
    blockers: list[dict[str, Any]] = []
    log_uri_entries = _log_uri_entries(check)
    invalid_log_uris: list[dict[str, Any]] = []
    allowed_log_uri_entries: list[tuple[str, str]] = []
    for evidence_key, uri in log_uri_entries:
        uri_blockers = _published_log_uri_blockers(
            uri,
            lane_payload=lane_payload,
            source_record=source_record,
            check=check,
            docker_security_payload=docker_security_payload,
        )
        if uri_blockers:
            invalid_log_uris.extend(uri_blockers)
        else:
            allowed_log_uri_entries.append((evidence_key, uri))
    if invalid_log_uris:
        return invalid_log_uris
    allowed_log_uris = [uri for _evidence_key, uri in allowed_log_uri_entries]
    if allowed_log_uris:
        blockers.extend(
            _published_log_uri_identity_blockers(
                allowed_log_uri_entries,
                observed_identity=observed_identity,
            )
        )
        if not _structured_evidence_value(check):
            blockers.append(
                _blocker(
                    "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_READ_EVIDENCE_MISSING",
                    "Logs job_logs check must include producer evidence for published log read result.",
                    log_uri=allowed_log_uris[0],
                )
            )
        return blockers
    unavailable = _published_log_unavailable_records(check)
    if unavailable:
        for record in unavailable:
            blockers.extend(
                _published_log_unavailable_binding_blockers(
                    record,
                    observed_identity=observed_identity,
                    expected_identity=expected_identity,
                )
            )
        return blockers
    return [
        _blocker(
            "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_EVIDENCE_MISSING",
            "Logs job_logs check must include an allowed published log URI or a typed published-log unavailable "
            "response.",
        )
    ]


def _log_uri_values(value: Any) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for _evidence_key, uri in _log_uri_entries(value):
        if uri not in seen:
            values.append(uri)
            seen.add(uri)
    return values


def _log_uri_entries(value: Any) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for _parent, key, nested, _depth in _walk_evidence_values(value):
        if key not in LOG_URI_KEYS or not isinstance(nested, str) or not nested.strip():
            continue
        uri = nested.strip()
        entry = (key, uri)
        if entry not in seen:
            values.append(entry)
            seen.add(entry)
    return values


def _published_log_uri_identity_blockers(
    log_uri_entries: Sequence[tuple[str, str]],
    *,
    observed_identity: Mapping[str, Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for evidence_key, uri in log_uri_entries:
        uri_identity = _log_uri_identity_values(uri)
        for identity_field in LOG_URI_REQUIRED_IDENTITY_FIELDS:
            expected = _identity_value(observed_identity, identity_field)
            if not expected:
                continue
            observed_values = uri_identity.get(identity_field, [])
            if not observed_values:
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH",
                        "logs published log URI identity is missing required check identity.",
                        field=identity_field,
                        observed=None,
                        expected=expected,
                        evidence_key=evidence_key,
                        log_uri=_safe_log_uri_summary(uri),
                    )
                )
                continue
            for observed in observed_values:
                if _producer_identity_value_matches(identity_field, observed, expected):
                    continue
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH",
                        "logs published log URI identity conflicts with the required check identity.",
                        field=identity_field,
                        observed=observed,
                        expected=expected,
                        evidence_key=evidence_key,
                        log_uri=_safe_log_uri_summary(uri),
                    )
                )
        for identity_field in ("cycle_time",):
            expected = _identity_value(observed_identity, identity_field)
            if not expected:
                continue
            for observed in uri_identity.get(identity_field, []):
                if _producer_identity_value_matches(identity_field, observed, expected):
                    continue
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_LOGS_CHECK_PRODUCER_IDENTITY_MISMATCH",
                        "logs published log URI identity conflicts with the required check identity.",
                        field=identity_field,
                        observed=observed,
                        expected=expected,
                        evidence_key=evidence_key,
                        log_uri=_safe_log_uri_summary(uri),
                    )
                )
    return blockers


def _log_uri_identity_values(uri: str) -> dict[str, list[str]]:
    parsed_values = _identity_values_from_text(uri)
    return {field: parsed_values.get(field, []) for field in LOG_URI_IDENTITY_FIELDS}


def _published_log_uri_blockers(
    uri: str,
    *,
    lane_payload: Mapping[str, Any],
    source_record: Mapping[str, Any],
    check: Mapping[str, Any],
    docker_security_payload: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    generic_blockers = _published_log_generic_uri_blockers(uri)
    if generic_blockers:
        return generic_blockers
    if uri.startswith("published://"):
        suffix = uri.removeprefix("published://").lstrip("/")
        return _published_log_relative_path_blockers(uri, suffix)
    if uri.startswith("file://"):
        return _published_log_file_uri_blockers(
            uri,
            lane_payload=lane_payload,
            source_record=source_record,
            check=check,
            docker_security_payload=docker_security_payload,
        )
    if uri.startswith("s3://"):
        return _published_log_s3_uri_blockers(uri, lane_payload=lane_payload, source_record=source_record, check=check)
    parsed = urlsplit(uri)
    if parsed.scheme:
        return [
            _blocker(
                "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED",
                "Logs evidence uses an unsupported log URI scheme.",
                log_uri=_safe_log_uri_summary(uri),
                scheme=parsed.scheme,
            )
        ]
    return [
        _blocker(
            "TWO_NODE_E2E_LOGS_PRIVATE_LOG_URI",
            "Logs evidence must not use private workspace or local log paths.",
            log_uri=_safe_log_uri_summary(uri),
        )
    ]


def _published_log_generic_uri_blockers(uri: str) -> list[dict[str, Any]]:
    try:
        parsed = urlsplit(uri)
        parsed.hostname
        parsed.port
    except ValueError:
        return [
            _blocker(
                "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED",
                "Logs evidence uses a malformed log URI.",
                log_uri=_safe_log_uri_summary(uri),
                reason="malformed_uri",
            )
        ]
    if parsed.username or parsed.password:
        return [
            _blocker(
                "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED",
                "Logs evidence must not include URI userinfo or credentials.",
                log_uri=_unsafe_log_uri_summary(_safe_log_uri_summary(uri)),
                reason="userinfo_forbidden",
            )
        ]
    if parsed.query or parsed.fragment:
        return [
            _blocker(
                "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED",
                "Logs evidence must not include query strings or fragments.",
                log_uri=_unsafe_log_uri_summary(_safe_log_uri_summary(uri)),
                reason="query_or_fragment_forbidden",
            )
        ]
    if parsed.scheme in {"published", "file", "s3"} and _path_has_credential_like_part(parsed.path):
        return [
            _blocker(
                "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED",
                "Logs evidence must not include credential-like URI path components.",
                log_uri=_unsafe_log_uri_summary(_safe_log_uri_summary(uri)),
                reason="credential_path_component",
            )
        ]
    return []


def _published_log_relative_path_blockers(uri: str, relative: str) -> list[dict[str, Any]]:
    blockers = _safe_log_relative_path_blockers(relative, require_logs_prefix=True)
    return [
        _blocker(
            "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSAFE",
            "Published log URI contains unsafe path components.",
            log_uri=_safe_log_uri_summary(uri),
            reason=reason,
        )
        for reason in blockers
    ]


def _published_log_file_uri_blockers(
    uri: str,
    *,
    lane_payload: Mapping[str, Any],
    source_record: Mapping[str, Any],
    check: Mapping[str, Any],
    docker_security_payload: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    parsed = urlsplit(uri)
    if parsed.netloc not in {"", "localhost"}:
        return [
            _blocker(
                "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED",
                "File log URI host is unsupported.",
                log_uri=_safe_log_uri_summary(uri),
                host=parsed.netloc,
            )
        ]
    if not parsed.path.startswith("/"):
        return [
            _blocker(
                "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSAFE",
                "File log URI must use an absolute allowed published artifact path.",
                log_uri=_safe_log_uri_summary(uri),
            )
        ]
    raw_path_blockers = _safe_log_absolute_path_blockers(parsed.path)
    if raw_path_blockers:
        return [
            _blocker(
                "TWO_NODE_E2E_LOGS_PRIVATE_LOG_URI",
                "Logs evidence must not use private workspace or unsafe local paths.",
                log_uri=_safe_log_uri_summary(uri),
                reason=reason,
            )
            for reason in raw_path_blockers
        ]
    path = Path(unquote(parsed.path)).resolve(strict=False)
    if _is_suspicious_private_log_path(path):
        return [
            _blocker(
                "TWO_NODE_E2E_LOGS_PRIVATE_LOG_URI",
                "Logs evidence must not use private workspace or local compute paths.",
                log_uri=_safe_log_uri_summary(uri),
                reason="private_workspace_path",
            )
        ]
    roots = _published_log_roots(
        lane_payload,
        source_record,
        check,
        docker_security_payload=docker_security_payload,
    )
    for root in roots:
        root_resolved = root.expanduser().resolve(strict=False)
        if _path_is_relative_to(path, root_resolved):
            relative = _relative_posix(path, root_resolved)
            blockers = _safe_log_relative_path_blockers(relative, require_logs_prefix=True)
            return [
                _blocker(
                    "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSAFE",
                    "Published file log URI contains unsafe path components.",
                    log_uri=_safe_log_uri_summary(uri),
                    reason=reason,
                )
                for reason in blockers
            ]
    return [
        _blocker(
            "TWO_NODE_E2E_LOGS_PRIVATE_LOG_URI",
            "File log URI must be under an allowed published artifact root.",
            log_uri=_safe_log_uri_summary(uri),
            allowed_roots=[_public_path(root) for root in roots],
        )
    ]


def _published_log_s3_uri_blockers(
    uri: str,
    *,
    lane_payload: Mapping[str, Any],
    source_record: Mapping[str, Any],
    check: Mapping[str, Any],
) -> list[dict[str, Any]]:
    parsed = urlsplit(uri)
    bucket = parsed.netloc
    if not bucket:
        return [
            _blocker(
                "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED",
                "S3 log URI is missing a bucket.",
                log_uri=_safe_log_uri_summary(uri),
            )
        ]
    key = parsed.path.lstrip("/")
    blockers = _safe_log_relative_path_blockers(key, require_logs_prefix=False)
    if blockers:
        return [
            _blocker(
                "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSAFE",
                "S3 log URI contains unsafe key components.",
                log_uri=_safe_log_uri_summary(uri),
                reason=reason,
            )
            for reason in blockers
        ]
    allowed_bucket = _first_nested_text(lane_payload, source_record, check, keys=PUBLISHED_LOG_S3_BUCKET_KEYS)
    if not allowed_bucket:
        return [
            _blocker(
                "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_URI_UNSUPPORTED",
                "S3 log URI requires an explicit published-artifact bucket allowlist.",
                log_uri=_safe_log_uri_summary(uri),
            )
        ]
    allowed_prefix = (
        _first_nested_text(lane_payload, source_record, check, keys=PUBLISHED_LOG_S3_PREFIX_KEYS) or ""
    ).strip("/")
    if bucket != allowed_bucket or not _s3_key_matches_published_log_prefix(key, allowed_prefix):
        return [
            _blocker(
                "TWO_NODE_E2E_LOGS_PRIVATE_LOG_URI",
                "S3 log URI must be under the allowed published artifact bucket/prefix.",
                log_uri=_safe_log_uri_summary(uri),
                allowed_bucket=allowed_bucket,
                allowed_prefix=allowed_prefix,
            )
        ]
    return []


def _safe_log_relative_path_blockers(relative: str, *, require_logs_prefix: bool) -> list[str]:
    try:
        decoded = _safe_log_decoded_path(relative)
    except ValueError as error:
        return [str(error)]
    if decoded.startswith("/"):
        return ["absolute_path_forbidden"]
    parts = PurePosixPath(decoded).parts
    if not parts:
        return ["empty_path"]
    if any(part in {"", ".", ".."} for part in parts):
        return ["unsafe_path_component"]
    if ".nhms-runs" in parts:
        return ["private_workspace_path"]
    if require_logs_prefix and parts[0] != "logs":
        return ["published_logs_prefix_required"]
    return []


def _safe_log_absolute_path_blockers(raw_path: str) -> list[str]:
    try:
        decoded = _safe_log_decoded_path(raw_path)
    except ValueError as error:
        return [str(error)]
    parts = PurePosixPath(decoded).parts
    if any(part in {".", ".."} for part in parts):
        return ["unsafe_path_component"]
    lowered_parts = tuple(part.lower() for part in parts)
    if ".nhms-runs" in lowered_parts:
        return ["private_workspace_path"]
    return []


def _safe_log_decoded_path(raw_path: str) -> str:
    if "\\" in raw_path or LOG_URI_ENCODED_FORBIDDEN_RE.search(raw_path):
        raise ValueError("encoded_or_backslash_path")
    decoded = unquote(raw_path)
    if "\\" in decoded:
        raise ValueError("backslash_path")
    if any(ord(character) < 32 for character in decoded):
        raise ValueError("malformed_path")
    return decoded


def _path_has_credential_like_part(raw_path: str) -> bool:
    if not raw_path:
        return False
    try:
        path = unquote(raw_path)
    except Exception:
        path = raw_path
    return any(LOG_URI_CREDENTIAL_WORD_RE.search(part) for part in PurePosixPath(path).parts)


def _published_log_roots(
    *values: Mapping[str, Any],
    docker_security_payload: Mapping[str, Any] | None = None,
) -> list[Path]:
    roots: list[Path] = list(DEFAULT_PUBLISHED_LOG_ROOTS)
    if docker_security_payload is not None:
        roots.extend(_authoritative_published_log_roots(docker_security_payload))
    candidate_roots: list[Path] = []
    for value in values:
        for _parent, key, nested, _depth in _walk_evidence_values(value):
            if key not in PUBLISHED_LOG_ROOT_KEYS or not isinstance(nested, str) or not nested.strip():
                continue
            candidate_roots.append(Path(nested.strip()))
    authoritative = {
        str(root.expanduser().resolve(strict=False))
        for root in roots
        if not _is_suspicious_private_log_path(root)
    }
    for root in candidate_roots:
        resolved = str(root.expanduser().resolve(strict=False))
        if resolved in authoritative:
            roots.append(root)
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        resolved = str(root.expanduser().resolve(strict=False))
        if resolved not in seen and not _is_suspicious_private_log_path(root):
            deduped.append(root)
            seen.add(resolved)
    return deduped


def _authoritative_published_log_roots(payload: Mapping[str, Any]) -> list[Path]:
    if _normalized_status(payload.get("status")) != STATUS_PASS:
        return []
    if two_node_e2e_docker_security.docker_display_security_proofs(payload).get(
        "published_artifacts_readonly"
    ) is not True:
        return []
    roots: list[Path] = []
    for _parent, key, nested, _depth in _walk_evidence_values(payload):
        if key not in PUBLISHED_LOG_ROOT_KEYS or not isinstance(nested, str) or not nested.strip():
            continue
        root = Path(nested.strip())
        if not _is_suspicious_private_log_path(root):
            roots.append(root)
    return roots


def _is_suspicious_private_log_path(path: Path | str) -> bool:
    normalized = _normalize_posix_path(str(path)).lower()
    parts = PurePosixPath(normalized).parts
    if any(part in PRIVATE_LOG_PATH_TOKENS for part in parts):
        return True
    if any(token in normalized for token in ("/workspace", "/basins", "/shud", "model_asset", "model-assets")):
        return True
    if normalized in {"/scratch", "/tmp"} or normalized.startswith("/tmp/"):
        return True
    if normalized.startswith("/scratch/") and not normalized.startswith("/scratch/frd_muziyao/nhms-published"):
        return True
    return False


def _s3_key_matches_published_log_prefix(key: str, prefix: str) -> bool:
    if not prefix:
        return key.startswith("logs/")
    if key.startswith(f"{prefix}/logs/"):
        return True
    parts = PurePosixPath(key).parts
    prefix_parts = PurePosixPath(prefix).parts
    if not prefix_parts or parts[: len(prefix_parts)] != prefix_parts:
        return False
    remainder = parts[len(prefix_parts) :]
    if remainder and remainder[0] == "runs":
        return len(remainder) >= 4 and remainder[2] == "logs"
    return len(remainder) >= 2 and remainder[0] == "logs"


def _published_log_unavailable_records(check: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for _parent, _key, nested, _depth in _walk_evidence_values(check):
        if not isinstance(nested, Mapping):
            continue
        code = str(
            nested.get("error_code")
            or nested.get("code")
            or _nested_get(nested, ("error", "code"))
            or _nested_get(nested, ("body", "error", "code"))
            or ""
        ).strip()
        if code in LOG_UNAVAILABLE_ERROR_CODES:
            records.append(nested)
    return records


def _published_log_unavailable_binding_blockers(
    record: Mapping[str, Any],
    *,
    observed_identity: Mapping[str, Any],
    expected_identity: Mapping[str, Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    status_code = record.get("status_code") or record.get("http_status")
    if status_code is not None and str(status_code) not in {"404"}:
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_UNAVAILABLE_STATUS_INVALID",
                "Typed published-log unavailable response must be a 404 response.",
                status_code=status_code,
            )
        )
    record_identity = _record_identity(record)
    if record_identity:
        for field in STRICT_LOG_IDENTITY_FIELDS:
            expected_value = _identity_value(expected_identity, field) or _identity_value(observed_identity, field)
            observed_value = _identity_value(record_identity, field)
            if expected_value and observed_value and not _strict_identity_value_matches(
                field,
                observed_value,
                expected_value,
            ):
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_UNAVAILABLE_IDENTITY_MISMATCH",
                        "Typed published-log unavailable response identity must match the job log check identity.",
                        field=field,
                        observed=observed_value,
                        expected=expected_value,
                    )
                )
    else:
        for field in STRICT_LOG_IDENTITY_FIELDS:
            value = record.get(field)
            if value is None and field == "source":
                value = record.get("source_id")
            if value is None:
                continue
            expected_value = _identity_value(expected_identity, field) or _identity_value(observed_identity, field)
            if expected_value and not _strict_identity_value_matches(field, value, expected_value):
                blockers.append(
                    _blocker(
                        "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_UNAVAILABLE_IDENTITY_MISMATCH",
                        "Typed published-log unavailable response identity must match the job log check identity.",
                        field=field,
                        observed=value,
                        expected=expected_value,
                    )
                )
    expected_job_id = _identity_value(expected_identity, "job_id") or _identity_value(observed_identity, "job_id")
    if not _record_mentions_job_id(record, expected_job_id):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_UNAVAILABLE_JOB_ID_MISSING",
                "Typed published-log unavailable response must bind to the same job_id.",
                expected_job_id=expected_job_id,
            )
        )
    if not _structured_evidence_value(record):
        blockers.append(
            _blocker(
                "TWO_NODE_E2E_LOGS_PUBLISHED_LOG_UNAVAILABLE_EVIDENCE_MISSING",
                "Typed published-log unavailable response must include producer response evidence.",
            )
        )
    return blockers


def _record_mentions_job_id(record: Mapping[str, Any], expected_job_id: str | None) -> bool:
    if not expected_job_id:
        return False
    for _parent, key, nested, _depth in _walk_evidence_values(record):
        if key == "job_id" and str(nested) == expected_job_id:
            return True
        if key in {"path", "url"} and isinstance(nested, str) and expected_job_id in nested:
            return True
    return False


def _identity_has_required_fields(identity: Mapping[str, Any], fields: Sequence[str]) -> bool:
    return all(_identity_value(identity, field) for field in fields)


def _first_nested_text(*values: Any, keys: frozenset[str]) -> str | None:
    for value in values:
        for _parent, key, nested, _depth in _walk_evidence_values(value):
            if key in keys and isinstance(nested, str) and nested.strip():
                return nested.strip()
    return None


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return path.resolve(strict=False).as_posix().lstrip("/")


def _safe_log_uri_summary(uri: str) -> str:
    try:
        parsed = urlsplit(uri)
        parsed.hostname
        parsed.port
    except ValueError:
        return "[redacted]"
    if parsed.scheme == "file":
        return "file://redacted"
    if not parsed.scheme:
        return "[redacted-local-log-path]"
    if (
        parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or _path_has_credential_like_part(parsed.path)
    ):
        return _unsafe_log_uri_summary(uri)
    return uri


def _unsafe_log_uri_summary(uri: str | None) -> str:
    if uri is None:
        return "[redacted]"
    try:
        parsed = urlsplit(str(uri))
        parsed.hostname
        parsed.port
    except ValueError:
        return "[redacted]"
    if parsed.scheme == "file":
        return "file://redacted"
    if parsed.scheme == "published":
        namespace = f"{parsed.netloc}/{parsed.path.lstrip('/')}" if parsed.netloc else parsed.path.lstrip("/")
        if namespace.split("/", maxsplit=1)[0] == "logs":
            return "published://logs/[redacted]"
        return "published://redacted/[redacted]"
    if parsed.scheme and parsed.netloc:
        return urlunsplit((parsed.scheme, parsed.hostname or "[redacted]", "[redacted]", "", ""))
    if parsed.scheme:
        return f"{parsed.scheme}://[redacted]"
    return "[redacted]"


def _has_source_artifact_proof(payload: Mapping[str, Any]) -> bool:
    return _source_artifact_records(payload.get("source_artifacts"))


def _source_artifact_records(value: Any) -> bool:
    records: list[Mapping[str, Any]]
    if isinstance(value, Mapping):
        records = [record for record in value.values() if isinstance(record, Mapping)]
    elif isinstance(value, list):
        records = [record for record in value if isinstance(record, Mapping)]
    else:
        return False
    for record in records:
        path = record.get("path") or record.get("artifact_path")
        sha256 = record.get("sha256") or record.get("digest")
        if isinstance(path, str) and path.strip() and isinstance(sha256, str) and re.fullmatch(
            r"[a-fA-F0-9]{64}",
            sha256.strip(),
        ):
            return True
    return False


def _structured_evidence_value(value: Any) -> bool:
    if isinstance(value, Mapping):
        if _looks_like_evidence_record(value):
            return True
        return any(_structured_evidence_value(nested) for nested in value.values())
    if isinstance(value, list):
        return any(_structured_evidence_value(item) for item in value)
    return False


def _looks_like_evidence_record(value: Mapping[str, Any]) -> bool:
    if _source_artifact_records([value]):
        return True
    if "returncode" in value:
        return True
    if any(key in value for key in ("method", "url", "path", "status_code", "http_status", "log_uri")):
        return True
    if any(key in value for key in ("sha256", "screenshot_path", "artifact_path", "trace_path")):
        return True
    if any(key in value for key in ("request", "response", "stdout", "stderr", "duration_ms")):
        return True
    return False


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


def _payload_blockers(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    blockers = payload.get("blockers")
    if isinstance(blockers, list):
        return [item for item in blockers if isinstance(item, Mapping)]
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
    for _parent, _key, nested, _depth in _walk_evidence_values(value):
        if isinstance(nested, Mapping):
            result.extend(_explicit_bundle_run_ids(nested))
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
    for _parent, key, nested, _depth in _walk_evidence_values(value):
        if key in MOCK_KEYS and nested is True:
            return True
        if key == "execution_mode" and str(nested).lower() in {
            "mock",
            "mocked",
            "deterministic_fixture",
            "fixture",
            "fixture_only",
        }:
            return True
    return False


def _has_historical_latest(value: Any) -> bool:
    for _parent, key, nested, _depth in _walk_evidence_values(value):
        if key in HISTORICAL_KEYS and nested is True:
            return True
        if key in {"latest_mode", "selection_mode"} and str(nested).lower() in {
            "historical_latest",
            "source_only",
            "fallback_latest",
        }:
            return True
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


def _bool_lookup_values(value: Any, keys: frozenset[str]) -> list[bool]:
    values: list[bool] = []
    for _parent, key, nested, _depth in _walk_evidence_values(value):
        if key in keys:
            parsed = _raw_bool(nested)
            if parsed is not None:
                values.append(parsed)
    return values


def _deep_bool_lookup(value: Any, keys: frozenset[str]) -> bool | None:
    for _parent, key, nested, _depth in _walk_evidence_values(value):
        if key in keys:
            parsed = _raw_bool(nested)
            if parsed is not None:
                return parsed
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
