from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from packages.common.redaction import redact_payload
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
)
from services.production_closure.two_node_e2e_cross_plane_lane import (
    is_full_scope_pass,
    is_full_scope_sources,
)
from services.production_closure.two_node_e2e_metadata_lane import FULL_PASS_SOURCE_SET

REPO_ROOT = Path(__file__).resolve().parents[2]
APPROVED_EVIDENCE_ROOTS = (REPO_ROOT / "artifacts", Path("/scratch/frd_muziyao"))
SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_EVIDENCE_PAYLOAD_BYTES = 1024 * 1024

STATUS_PASS = "PASS"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"
FINAL_EVIDENCE_SCHEMA = "nhms.two_node_e2e.final_evidence.v1"

FINAL_AGGREGATION_OWNER = "services.production_closure.two_node_e2e_final_aggregation"
FINAL_AGGREGATION_VERIFICATION = (
    'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "final or redaction or evidence_root or stale"'
)
FINAL_AGGREGATION_GUARD_SYMBOLS = (
    "FINAL_EVIDENCE_SCHEMA",
    "FULL_PASS_SOURCE_SET",
    "SAFE_RUN_ID_RE",
    "STATUS_PASS",
    "STATUS_PARTIAL",
    "STATUS_FAIL",
    "STATUS_BLOCKED",
    "TwoNodeE2EEvidenceError",
    "APPROVED_EVIDENCE_ROOTS",
    "EvidenceWriter",
    "FinalAggregationHelpers",
    "build_final_summary",
    "write_final_summary",
    "final_status",
    "collect_blockers_and_findings",
    "metadata_summary",
    "_safe_resolved_evidence_root",
    "_path_is_relative_to",
    "_refuse_symlink_components",
    "_safe_run_id",
    "_public_path",
)
FINAL_AGGREGATION_BLOCKER_NAMESPACES = (
    "TWO_NODE_E2E_LANE_",
    "TWO_NODE_E2E_SOURCE_",
    "TWO_NODE_E2E_DECLARED_SOURCES_MISSING",
    "TWO_NODE_E2E_REDUCED_SOURCE_SCOPE",
    "TWO_NODE_E2E_EVIDENCE_",
    "TWO_NODE_E2E_RUN_ID_UNSAFE",
    "TWO_NODE_E2E_EVIDENCE_ROOT_UNAPPROVED",
)


class TwoNodeE2EEvidenceError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class EvidenceDocumentLike(Protocol):
    path: Path
    payload: Mapping[str, Any]
    sha256: str


class LaneEvaluationLike(Protocol):
    name: str
    status: str
    blockers: Sequence[Mapping[str, Any]]
    findings: Sequence[Mapping[str, Any]]

    def to_summary(self) -> dict[str, Any]: ...


class FinalEvidenceConfigLike(Protocol):
    evidence_root: Path
    run_id: str

    @property
    def run_dir(self) -> Path: ...

    @property
    def lane_dir(self) -> Path: ...


class BlockerFactory(Protocol):
    def __call__(self, code: str, message: str, **details: Any) -> dict[str, Any]: ...


class FindingFactory(Protocol):
    def __call__(self, code: str, message: str, **details: Any) -> dict[str, Any]: ...


class WithContext(Protocol):
    def __call__(self, item: Mapping[str, Any], **context: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class FinalAggregationHelpers:
    blocker: BlockerFactory
    finding: FindingFactory
    with_context: WithContext


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
        try:
            content = json.dumps(redact_payload(payload), indent=2, sort_keys=True).encode("utf-8") + b"\n"
        except RecursionError as error:
            raise TwoNodeE2EEvidenceError(
                "TWO_NODE_E2E_EVIDENCE_REDACTION_DEPTH_EXCEEDED",
                "Final evidence payload is too deeply nested to redact safely.",
            ) from error
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


def build_final_summary(
    *,
    config: FinalEvidenceConfigLike,
    metadata_doc: EvidenceDocumentLike | None,
    metadata: Mapping[str, Any],
    metadata_lane: LaneEvaluationLike,
    strict_identities: Mapping[str, Mapping[str, Any]],
    lanes: Mapping[str, LaneEvaluationLike],
    source_scope_results: Mapping[str, Mapping[str, Any]],
    scope: Mapping[str, Any],
    helpers: FinalAggregationHelpers,
    generated_at: str | None = None,
) -> dict[str, Any]:
    blockers, findings = collect_blockers_and_findings(
        lanes,
        source_scope_results,
        scope,
        helpers=helpers,
    )
    return {
        "schema": FINAL_EVIDENCE_SCHEMA,
        "status": final_status(lanes, source_scope_results, scope),
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "run_id": config.run_id,
        "evidence_root": _public_path(config.evidence_root),
        "run_dir": _public_path(config.run_dir),
        "evidence_dir": _public_path(config.lane_dir),
        "metadata": metadata_summary(metadata_doc, metadata, metadata_lane),
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


def write_final_summary(
    *,
    writer: EvidenceWriter,
    config: FinalEvidenceConfigLike,
    summary: Mapping[str, Any],
) -> Any:
    writer.write_json(config.lane_dir / "summary.json", summary)
    return redact_payload(summary)


def final_status(
    lanes: Mapping[str, LaneEvaluationLike],
    source_scope_results: Mapping[str, Mapping[str, Any]],
    scope: Mapping[str, Any],
) -> str:
    lane_statuses = [lane.status for lane in lanes.values()]
    source_statuses = [str(result.get("status")) for result in source_scope_results.values()]
    if STATUS_FAIL in lane_statuses or STATUS_FAIL in source_statuses:
        return STATUS_FAIL
    if STATUS_BLOCKED in lane_statuses or STATUS_BLOCKED in source_statuses:
        return STATUS_BLOCKED
    if not is_full_scope_pass(
        tuple(scope["declared_sources"]),
        source_scope_results,
    ):
        return STATUS_PARTIAL
    if STATUS_PARTIAL in lane_statuses or STATUS_PARTIAL in source_statuses:
        return STATUS_PARTIAL
    return STATUS_PASS


def collect_blockers_and_findings(
    lanes: Mapping[str, LaneEvaluationLike],
    source_scope_results: Mapping[str, Mapping[str, Any]],
    scope: Mapping[str, Any],
    *,
    helpers: FinalAggregationHelpers,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    if not scope["declared_sources"]:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DECLARED_SOURCES_MISSING",
                "Final evidence requires declared source scope.",
            )
        )
    if not is_full_scope_sources(tuple(scope["declared_sources"])):
        findings.append(
            helpers.finding(
                "TWO_NODE_E2E_REDUCED_SOURCE_SCOPE",
                "Final evidence is not full GFS/IFS scope and cannot be full PASS.",
                declared_sources=list(scope["declared_sources"]),
                reduced_scope=scope["reduced_scope"],
            )
        )
    for lane_name, lane in lanes.items():
        for blocker in lane.blockers:
            blockers.append(helpers.with_context(blocker, lane=lane_name))
        for finding in lane.findings:
            findings.append(helpers.with_context(finding, lane=lane_name))
        if lane.status == STATUS_BLOCKED:
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_LANE_BLOCKED",
                    f"Required lane {lane_name} is BLOCKED.",
                    lane=lane_name,
                )
            )
        elif lane.status == STATUS_FAIL:
            findings.append(
                helpers.finding(
                    "TWO_NODE_E2E_LANE_FAILED",
                    f"Required lane {lane_name} failed.",
                    lane=lane_name,
                )
            )
    for source, result in source_scope_results.items():
        if result.get("status") == STATUS_BLOCKED:
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_SOURCE_BLOCKED",
                    "Declared source scope is blocked.",
                    source=source,
                )
            )
        elif result.get("status") == STATUS_FAIL:
            findings.append(
                helpers.finding(
                    "TWO_NODE_E2E_SOURCE_FAILED",
                    "Declared source scope failed.",
                    source=source,
                )
            )
    return blockers, findings


def metadata_summary(
    doc: EvidenceDocumentLike | None,
    metadata: Mapping[str, Any],
    metadata_lane: LaneEvaluationLike,
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


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


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


def _public_path(path: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        return str(resolved)
