from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from services.production_closure.two_node_e2e_metadata_lane import (
    STRICT_IDENTITY_FIELDS,
    STRICT_LOG_IDENTITY_FIELDS,
)

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

READONLY_DB_LIVE_SCHEMA = "nhms.readonly_db_boundary.evidence.v1"
READONLY_DB_DOCUMENT_CANDIDATES = (
    "db/readonly-db-boundary/summary.json",
    "db/summary.json",
)
READONLY_DB_LANE_OWNER = "services.production_closure.two_node_e2e_readonly_db_lane"
READONLY_DB_LANE_VERIFICATION = 'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "readonly_db"'
READONLY_DB_LANE_GUARD_SYMBOLS = (
    "READONLY_DB_LIVE_SCHEMA",
    "READONLY_DB_DOCUMENT_CANDIDATES",
    "READONLY_DB_REQUIRED_ROUTE_NAMES",
    "READONLY_DB_STRICT_ROUTE_FIELDS",
    "READONLY_DB_REQUIRED_PERMISSION_TARGETS",
    "ReadonlyDbEvaluationHelpers",
    "evaluate_readonly_db",
    "_ReadonlyDbLaneEvaluator",
)
READONLY_DB_BLOCKER_NAMESPACES = (
    "TWO_NODE_E2E_READONLY_DB_",
    "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_",
    "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
    "TWO_NODE_E2E_STRICT_IDENTITY_",
    "TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE",
    "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE",
)
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
READONLY_DB_SOURCE_PARENT_BINDING_KEYS = (
    "parent_evidence_run_id",
    "parent_bundle_run_id",
    "parent_bundle_id",
    "current_evidence_run_id",
    "current_bundle_run_id",
    "expected_evidence_run_id",
)
READONLY_DB_SOURCE_ROOT_BINDING_KEYS = (
    "parent_evidence_root",
    "parent_bundle_root",
    "current_evidence_root",
    "current_bundle_root",
    "final_evidence_root",
    "final_run_dir",
)
READONLY_DB_SOURCE_ARTIFACT_FILENAMES = (
    "summary.json",
    "role.json",
    "route_smoke.json",
    "permission_probes.json",
)
READONLY_DB_MANUAL_WRITE_PROOF_ALIASES: Mapping[str, tuple[str, ...]] = {
    "write_dependency_constructed": (
        "write_dependency_constructed",
        "db_write_dependency_constructed",
        "control_write_dependency_constructed",
        "state_mutation_dependency_constructed",
    ),
    "write_executed": (
        "write_executed",
        "db_write_executed",
        "control_executed",
        "state_mutation_executed",
    ),
}

LaneResultT = TypeVar("LaneResultT")
LaneResultT_co = TypeVar("LaneResultT_co", covariant=True)


class EvidenceDocumentLike(Protocol):
    path: Path
    payload: Mapping[str, Any]
    sha256: str


class BlockerFactory(Protocol):
    def __call__(self, code: str, message: str, **details: Any) -> dict[str, Any]: ...


class FindingFactory(Protocol):
    def __call__(self, code: str, message: str, **details: Any) -> dict[str, Any]: ...


class MissingLaneAdapter(Protocol[LaneResultT_co]):
    def __call__(self, name: str, code: str) -> LaneResultT_co: ...


class LaneFromStatusAdapter(Protocol[LaneResultT_co]):
    def __call__(
        self,
        name: str,
        doc: EvidenceDocumentLike,
        *,
        status: str,
        summary_status: str,
        blockers: Sequence[Mapping[str, Any]] = (),
        findings: Sequence[Mapping[str, Any]] = (),
    ) -> LaneResultT_co: ...


class StatNoFollow(Protocol):
    def __call__(self, path: Path, *, containment_root: Path) -> Any: ...


class ReadJsonValue(Protocol):
    def __call__(self, path: Path, *, containment_root: Path) -> Any: ...


class ReadBytesLimitedNoFollow(Protocol):
    def __call__(self, path: Path, *, max_bytes: int, containment_root: Path) -> bytes: ...


class IdentityMatchStatus(Protocol):
    def __call__(
        self,
        source: str,
        record: Mapping[str, Any],
        strict_identities: Mapping[str, Mapping[str, Any]],
        *,
        require_job_id: bool = False,
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]: ...


@dataclass(frozen=True)
class ReadonlyDbEvaluationHelpers(Generic[LaneResultT]):
    missing_lane: MissingLaneAdapter[LaneResultT]
    lane_from_status: LaneFromStatusAdapter[LaneResultT]
    normalized_status: Callable[[Any], str]
    combined_status: Callable[..., str]
    blocker: BlockerFactory
    finding: FindingFactory
    stale_lane_blockers: Callable[[Mapping[str, Any]], list[dict[str, Any]]]
    database_url_is_redacted: Callable[[str], bool]
    sources_from_value: Callable[[Any], tuple[str, ...]]
    approved_artifact_path: Callable[[str], Path]
    refuse_symlink_components: Callable[[Path], None]
    path_is_relative_to: Callable[[Path, Path], bool]
    public_path: Callable[[Path], str]
    stat_no_follow: StatNoFollow
    read_json_value: ReadJsonValue
    explicit_bundle_run_ids_from_value: Callable[[Any], list[tuple[str, Any]]]
    read_bytes_limited_no_follow: ReadBytesLimitedNoFollow
    ensure_bounded_evidence_value: Callable[[Any], None]
    identity_match_status: IdentityMatchStatus
    source_name: Callable[[Any], str | None]
    identity_value: Callable[[Mapping[str, Any], str], str | None]
    strict_identity_value_matches: Callable[[str, Any, Any], bool]
    manual_action_name: Callable[[Mapping[str, Any]], str]
    manual_action_outcome_status: Callable[[Mapping[str, Any]], str]
    with_context: Callable[..., dict[str, Any]]
    evidence_error_type: type[Exception]
    safe_filesystem_error_type: type[Exception]
    max_evidence_payload_bytes: int


def evaluate_readonly_db(
    doc: EvidenceDocumentLike | None,
    *,
    declared_sources: tuple[str, ...],
    strict_identities: Mapping[str, Mapping[str, Any]],
    evidence_run_id: str,
    helpers: ReadonlyDbEvaluationHelpers[LaneResultT],
) -> LaneResultT:
    return _ReadonlyDbLaneEvaluator(helpers).evaluate(
        doc,
        declared_sources=declared_sources,
        strict_identities=strict_identities,
        evidence_run_id=evidence_run_id,
    )


@dataclass(frozen=True)
class _ReadonlyDbLaneEvaluator(Generic[LaneResultT]):
    helpers: ReadonlyDbEvaluationHelpers[LaneResultT]

    def evaluate(
        self,
        doc: EvidenceDocumentLike | None,
        *,
        declared_sources: tuple[str, ...],
        strict_identities: Mapping[str, Mapping[str, Any]],
        evidence_run_id: str,
    ) -> LaneResultT:
        if doc is None:
            return self.helpers.missing_lane("readonly_db", "TWO_NODE_E2E_READONLY_DB_SUMMARY_MISSING")
        payload = doc.payload
        status = self.helpers.normalized_status(payload.get("status"))
        blockers = list(self.helpers.stale_lane_blockers(payload))
        findings: list[dict[str, Any]] = []
        summary_status = str(payload.get("status", "unknown"))
        if payload.get("run_id") != evidence_run_id:
            findings.append(
                self.helpers.finding(
                    "TWO_NODE_E2E_READONLY_DB_STALE_RUN",
                    "Readonly DB summary run_id must match the current evidence bundle.",
                    expected_run_id=evidence_run_id,
                    observed_run_id=payload.get("run_id"),
                )
            )
        if payload.get("schema") != READONLY_DB_LIVE_SCHEMA:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_LIVE_SCHEMA_MISSING",
                    "Readonly DB PASS requires real live readonly DB evidence, not simulated or unknown evidence.",
                    schema=payload.get("schema"),
                )
            )
        provenance = payload.get("validation_provenance", {})
        if not isinstance(provenance, Mapping) or provenance.get("mode") != "live":
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_LIVE_MODE_MISSING",
                    "Readonly DB PASS requires validation_provenance.mode=live.",
                )
            )
        if not isinstance(provenance, Mapping) or provenance.get("live_readonly_proof") is not True:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_LIVE_PROOF_MISSING",
                    "Readonly DB PASS requires live_readonly_proof=true.",
                )
            )
        database_url = payload.get("database_url")
        if (
            not isinstance(database_url, str)
            or not database_url.strip()
            or not self.helpers.database_url_is_redacted(database_url)
        ):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_REDACTED_DSN_MISSING",
                    "Readonly DB evidence must include a redacted database URL.",
                )
            )
        role = payload.get("role", {})
        if not isinstance(role, Mapping) or not role.get("current_user"):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_ROLE_MISSING",
                    "Readonly DB evidence must include current_user role evidence.",
                )
            )
        elif role.get("role_type") != "readonly_candidate":
            findings.append(
                self.helpers.finding(
                    "TWO_NODE_E2E_READONLY_DB_WRITER_ROLE",
                    "Readonly DB evidence identifies a writer or mutating role.",
                    role_type=role.get("role_type"),
                )
            )
        for key in ("route_smoke", "permission_probes", "manual_action_probes"):
            value = payload.get(key)
            if not isinstance(value, list) or not value:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_AUTHORITATIVE_EVIDENCE_MISSING",
                        f"Readonly DB evidence must include non-empty {key}.",
                        evidence_key=key,
                    )
                )
        for operation in self._permission_operations(payload):
            if operation.get("privilege_allowed") is True:
                findings.append(
                    self.helpers.finding(
                        "TWO_NODE_E2E_READONLY_DB_MUTATING_PRIVILEGE",
                        "Readonly DB evidence contains a mutating catalog privilege.",
                        operation=operation.get("operation"),
                        reason=operation.get("reason"),
                    )
                )
            if operation.get("execution_outcome") == "succeeded":
                findings.append(
                    self.helpers.finding(
                        "TWO_NODE_E2E_READONLY_DB_SUCCESSFUL_MUTATION_PROBE",
                        "Readonly DB evidence contains a successful DML/DDL probe.",
                        operation=operation.get("operation"),
                        reason=operation.get("reason"),
                    )
                )
        child_blockers, child_findings = self._readonly_db_child_evidence_issues(
            payload,
            declared_sources=declared_sources,
            strict_identities=strict_identities,
        )
        blockers.extend(child_blockers)
        findings.extend(child_findings)
        sibling_blockers, sibling_findings = self._readonly_db_sibling_issues(
            doc.path,
            payload,
            evidence_run_id=evidence_run_id,
        )
        blockers.extend(sibling_blockers)
        findings.extend(sibling_findings)
        source_artifact_blockers, source_artifact_findings = self._readonly_db_source_artifact_issues(
            doc.path,
            payload,
            declared_sources=declared_sources,
            evidence_run_id=evidence_run_id,
        )
        blockers.extend(source_artifact_blockers)
        findings.extend(source_artifact_findings)
        recomputed_status = self._readonly_db_recomputed_status(
            payload,
            declared_sources=declared_sources,
            strict_identities=strict_identities,
        )
        if status == STATUS_PASS and recomputed_status != STATUS_PASS:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_RECOMPUTED_STATUS_NOT_PASS",
                    "Readonly DB summary PASS contradicts recomputed child evidence status.",
                    recomputed_status=recomputed_status,
                )
            )
        status = self.helpers.combined_status([status], findings=findings, blockers=blockers)
        return self.helpers.lane_from_status(
            "readonly_db",
            doc,
            status=status,
            summary_status=summary_status,
            blockers=blockers,
            findings=findings,
        )


    def _permission_operations(self, payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        operations = []
        probes = payload.get("permission_probes", [])
        if isinstance(probes, list):
            for probe in probes:
                if isinstance(probe, Mapping):
                    raw_operations = probe.get("operations", [])
                    if isinstance(raw_operations, list):
                        operations.extend(item for item in raw_operations if isinstance(item, Mapping))
        return operations


    def _readonly_db_sibling_issues(
        self,
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
                sibling_stat = self.helpers.stat_no_follow(path, containment_root=lane_dir)
            except FileNotFoundError:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_AUTHORITATIVE_FILE_MISSING",
                        "Readonly DB PASS requires current authoritative sibling evidence files.",
                        filename=filename,
                    )
                )
                continue
            except self.helpers.safe_filesystem_error_type as error:
                raise self.helpers.evidence_error_type(
                    "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE",
                    f"Unsafe readonly DB sibling evidence path {path}: {error}",
                ) from error
            if not stat.S_ISREG(sibling_stat.st_mode):
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_AUTHORITATIVE_FILE_INVALID",
                        "Readonly DB authoritative sibling evidence must be a regular JSON file.",
                        filename=filename,
                    )
                )
                continue
            sibling_payload = self.helpers.read_json_value(path, containment_root=lane_dir)
            sibling_payloads[filename] = sibling_payload
            for key, value in self.helpers.explicit_bundle_run_ids_from_value(sibling_payload):
                if str(value) != evidence_run_id:
                    blockers.append(
                        self.helpers.blocker(
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
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_AUTHORITATIVE_FILE_MISMATCH",
                        "Readonly DB role.json must match the role object embedded in summary.json.",
                        filename="role.json",
                    )
                )
            if isinstance(role_payload, Mapping) and role_payload.get("role_type") != "readonly_candidate":
                findings.append(
                    self.helpers.finding(
                        "TWO_NODE_E2E_READONLY_DB_WRITER_ROLE",
                        "Readonly DB role.json identifies a writer or mutating role.",
                        role_type=role_payload.get("role_type"),
                    )
                )
        route_payload = sibling_payloads.get("route_smoke.json")
        if route_payload is not None:
            if route_payload != summary_payload.get("route_smoke"):
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_AUTHORITATIVE_FILE_MISMATCH",
                        "Readonly DB route_smoke.json must match the route_smoke list embedded in summary.json.",
                        filename="route_smoke.json",
                    )
                )
        permission_payload = sibling_payloads.get("permission_probes.json")
        if permission_payload is not None:
            if permission_payload != summary_payload.get("permission_probes"):
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_AUTHORITATIVE_FILE_MISMATCH",
                        "Readonly DB permission_probes.json must match the permission_probes list embedded in "
                        "summary.json.",
                        filename="permission_probes.json",
                    )
                )
            if isinstance(permission_payload, list):
                for operation in self._permission_operations_from_targets(permission_payload):
                    findings.extend(self._readonly_db_operation_findings(operation))
        return blockers, findings


    def _readonly_db_source_artifact_issues(
        self,
        summary_path: Path,
        summary_payload: Mapping[str, Any],
        *,
        declared_sources: tuple[str, ...],
        evidence_run_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        blockers: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        provenance = summary_payload.get("validation_provenance")
        if not isinstance(provenance, Mapping):
            return (
                [
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACTS_MISSING",
                        "Readonly DB final PASS requires validation_provenance.source_artifacts.",
                    )
                ],
                findings,
            )
        if provenance.get("merged_source_evidence") is not True:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_MERGED_SOURCE_EVIDENCE_MISSING",
                    "Readonly DB final PASS requires merged_source_evidence=true.",
                )
            )
        declared_in_provenance = self.helpers.sources_from_value(provenance.get("declared_sources"))
        if declared_in_provenance and set(declared_in_provenance) != set(declared_sources):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_DECLARED_SOURCE_SCOPE_MISMATCH",
                    "Readonly DB merge declared source scope must match final evidence scope.",
                    declared_sources=list(declared_sources),
                    db_declared_sources=sorted(declared_in_provenance),
                )
            )
        source_artifacts = provenance.get("source_artifacts")
        if not isinstance(source_artifacts, list) or not source_artifacts:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACTS_MISSING",
                    "Readonly DB final PASS requires non-empty merged source_artifacts.",
                )
            )
            return blockers, findings
        lane_dir = summary_path.parent
        observed_sources: set[str] = set()
        seen_dirs: set[str] = set()
        for index, record in enumerate(source_artifacts):
            if not isinstance(record, Mapping):
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_INVALID",
                        "Readonly DB source artifact record must be an object.",
                        source_index=index,
                    )
                )
                continue
            record_blockers, record_findings, payload_sources = self._readonly_db_single_source_artifact_issues(
                record,
                source_index=index,
                lane_dir=lane_dir,
                evidence_run_id=evidence_run_id,
            )
            blockers.extend(record_blockers)
            findings.extend(record_findings)
            duplicate_sources = sorted(source for source in payload_sources if source in observed_sources)
            if duplicate_sources:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_DUPLICATE_SOURCE",
                        "Readonly DB source artifacts must not duplicate source coverage.",
                        source_index=index,
                        duplicate_sources=duplicate_sources,
                    )
                )
            observed_sources.update(payload_sources)
            source_dir = str(record.get("source_dir") or "")
            if source_dir in seen_dirs:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_DUPLICATE_SOURCE_DIR",
                        "Readonly DB source artifacts must not duplicate source directories.",
                        source_index=index,
                        source_dir=source_dir,
                    )
                )
            if source_dir:
                seen_dirs.add(source_dir)
        missing_sources = sorted(source for source in declared_sources if source not in observed_sources)
        if missing_sources:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_SOURCE_COVERAGE_MISSING",
                    "Readonly DB source artifacts must cover every declared source.",
                    missing_sources=missing_sources,
                    observed_sources=sorted(observed_sources),
                )
            )
        return blockers, findings


    def _readonly_db_single_source_artifact_issues(
        self,
        record: Mapping[str, Any],
        *,
        source_index: int,
        lane_dir: Path,
        evidence_run_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
        blockers: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        claimed_sources = set(self.helpers.sources_from_value(record.get("sources")))
        payload_sources: set[str] = set()
        if not claimed_sources:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_SOURCE_CLAIM_MISSING",
                    "Readonly DB source artifact record must claim non-empty source coverage.",
                    source_index=source_index,
                )
            )
        source_dir = self._readonly_db_source_artifact_dir(record, source_index=source_index, lane_dir=lane_dir)
        if source_dir is None:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_DIR_UNSAFE",
                    "Readonly DB source artifact record must include a safe approved source_dir.",
                    source_index=source_index,
                    source_dir=record.get("source_dir"),
                )
            )
            return blockers, findings, payload_sources
        if self.helpers.path_is_relative_to(lane_dir, source_dir) or source_dir == lane_dir.resolve(strict=False):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_FINAL_LANE_REUSE",
                    "Readonly DB source artifact must point to per-source evidence, not the final merge lane.",
                    source_index=source_index,
                    source_dir=self.helpers.public_path(source_dir),
                )
            )
        parent_blocker = self._readonly_db_source_parent_binding_blocker(
            record,
            evidence_run_id=evidence_run_id,
            source_index=source_index,
            source_dir=source_dir,
            lane_dir=lane_dir,
        )
        if parent_blocker is not None:
            blockers.append(parent_blocker)
        provenance = record.get("validation_provenance")
        if not isinstance(provenance, Mapping) or provenance.get("mode") != "live" or provenance.get(
            "live_readonly_proof"
        ) is not True:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_LIVE_PROVENANCE_MISSING",
                    "Readonly DB source artifact must carry live source validation provenance.",
                    source_index=source_index,
                )
            )
        summary_run_id = record.get("summary_run_id")
        if not isinstance(summary_run_id, str) or not summary_run_id.strip():
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_RUN_ID_MISSING",
                    "Readonly DB source artifact record must include summary_run_id.",
                    source_index=source_index,
                )
            )
        artifacts = record.get("artifacts")
        if not isinstance(artifacts, Mapping):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACTS_MISSING",
                    "Readonly DB source artifact record must include authoritative artifact metadata.",
                    source_index=source_index,
                )
            )
            return blockers, findings, payload_sources
        missing_filenames = [
            filename for filename in READONLY_DB_SOURCE_ARTIFACT_FILENAMES if filename not in artifacts
        ]
        if missing_filenames:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_FILE_MISSING",
                    "Readonly DB source artifact record is missing authoritative artifact metadata.",
                    source_index=source_index,
                    missing_filenames=missing_filenames,
                )
            )
        source_payloads: dict[str, Any] = {}
        source_hashes: dict[str, str] = {}
        for filename in READONLY_DB_SOURCE_ARTIFACT_FILENAMES:
            artifact = artifacts.get(filename)
            if not isinstance(artifact, Mapping):
                continue
            artifact_blockers, payload, sha256 = self._readonly_db_source_artifact_file_issues(
                artifact,
                filename=filename,
                source_index=source_index,
                source_dir=source_dir,
                summary_run_id=summary_run_id if isinstance(summary_run_id, str) else None,
            )
            blockers.extend(artifact_blockers)
            if payload is not None:
                source_payloads[filename] = payload
                source_hashes[filename] = sha256 or ""
        sibling_blockers, sibling_findings, payload_sources = self._readonly_db_source_artifact_payload_issues(
            source_payloads,
            source_index=source_index,
            evidence_run_id=evidence_run_id,
            summary_run_id=summary_run_id if isinstance(summary_run_id, str) else None,
            source_dir=source_dir,
            lane_dir=lane_dir,
        )
        blockers.extend(sibling_blockers)
        findings.extend(sibling_findings)
        if claimed_sources != payload_sources:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_SOURCE_MISMATCH",
                    "Readonly DB source artifact claimed sources must match payload-proven sources.",
                    source_index=source_index,
                    claimed_sources=sorted(claimed_sources),
                    payload_sources=sorted(payload_sources),
                )
            )
        return blockers, findings, payload_sources


    def _readonly_db_source_artifact_dir(
        self,
        record: Mapping[str, Any],
        *,
        source_index: int,
        lane_dir: Path,
    ) -> Path | None:
        raw_source_dir = record.get("source_dir")
        if not isinstance(raw_source_dir, str) or not raw_source_dir.strip():
            return None
        try:
            source_dir = self.helpers.approved_artifact_path(raw_source_dir)
        except self.helpers.evidence_error_type:
            return None
        try:
            self.helpers.refuse_symlink_components(source_dir)
        except self.helpers.evidence_error_type:
            return None
        if source_dir.exists() and not source_dir.is_dir():
            return None
        if source_dir == lane_dir.resolve(strict=False):
            return source_dir
        return source_dir


    def _final_run_dir_from_lane_dir(self, lane_dir: Path) -> Path:
        if lane_dir.name == "readonly-db-boundary" and lane_dir.parent.name == "db":
            return lane_dir.parents[1].resolve(strict=False)
        if lane_dir.name == "db":
            return lane_dir.parent.resolve(strict=False)
        if len(lane_dir.parents) > 1:
            return lane_dir.parents[1].resolve(strict=False)
        return lane_dir.parent.resolve(strict=False)


    def _readonly_db_source_parent_binding_blocker(
        self,
        record: Mapping[str, Any],
        *,
        evidence_run_id: str,
        source_index: int,
        source_dir: Path | None,
        lane_dir: Path | None,
    ) -> dict[str, Any] | None:
        parent_binding = record.get("parent_binding")
        provenance = record.get("validation_provenance")
        parent_value = None
        parent_key = None
        root_value = None
        root_key = None
        if isinstance(provenance, Mapping):
            for key in READONLY_DB_SOURCE_PARENT_BINDING_KEYS:
                value = provenance.get(key)
                if isinstance(value, str) and value.strip():
                    parent_key = f"validation_provenance.{key}"
                    parent_value = value
                    break
            for key in READONLY_DB_SOURCE_ROOT_BINDING_KEYS:
                value = provenance.get(key)
                if isinstance(value, str) and value.strip():
                    root_key = f"validation_provenance.{key}"
                    root_value = value
                    break
        if isinstance(parent_binding, str) and parent_binding in {"run_id_prefix"}:
            if source_dir is not None and lane_dir is not None:
                final_run_dir = self._final_run_dir_from_lane_dir(lane_dir)
                expected_source_parent = final_run_dir.parent.resolve(strict=False)
                observed_source_parent = source_dir.parent.parent.parent.resolve(strict=False)
                if observed_source_parent != expected_source_parent:
                    return self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_PARENT_ROOT_MISMATCH",
                        "Readonly DB run_id-prefix source artifact must live under the current final evidence parent.",
                        source_index=source_index,
                        source_dir=self.helpers.public_path(source_dir),
                        observed_source_parent=self.helpers.public_path(observed_source_parent),
                        expected_source_parent=self.helpers.public_path(expected_source_parent),
                    )
            return None
        if parent_value is None:
            return self.helpers.blocker(
                "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_PARENT_BINDING_MISSING",
                "Readonly DB source artifact must be current-run-prefixed or explicitly parent/root-bound.",
                source_index=source_index,
                parent_binding=parent_binding,
            )
        if str(parent_value) != evidence_run_id:
            return self.helpers.blocker(
                "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_PARENT_BINDING_MISMATCH",
                "Readonly DB source artifact parent/current binding must match the final evidence run.",
                source_index=source_index,
                parent_binding=parent_key,
                evidence_run_id=parent_value,
                expected_evidence_run_id=evidence_run_id,
                )
        if source_dir is not None and lane_dir is not None:
            if root_value is None:
                return self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_PARENT_ROOT_MISSING",
                    "Readonly DB external source artifact must explicitly bind to the current final evidence root.",
                    source_index=source_index,
                    parent_binding=parent_key,
                )
            final_run_dir = self._final_run_dir_from_lane_dir(lane_dir)
            final_parent = final_run_dir.parent.resolve(strict=False)
            observed_root = Path(root_value).expanduser().resolve(strict=False)
            if observed_root not in {final_run_dir, final_parent}:
                return self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_PARENT_ROOT_MISMATCH",
                    "Readonly DB external source artifact root binding must match the current final evidence root.",
                    source_index=source_index,
                    parent_binding=parent_key,
                    root_binding=root_key,
                    observed_root=self.helpers.public_path(observed_root),
                    expected_roots=sorted(
                        (
                            self.helpers.public_path(final_parent),
                            self.helpers.public_path(final_run_dir),
                        )
                    ),
                )
        return None


    def _readonly_db_source_artifact_file_issues(
        self,
        artifact: Mapping[str, Any],
        *,
        filename: str,
        source_index: int,
        source_dir: Path,
        summary_run_id: str | None,
    ) -> tuple[list[dict[str, Any]], Any | None, str | None]:
        blockers: list[dict[str, Any]] = []
        raw_path = artifact.get("path")
        raw_sha256 = artifact.get("sha256")
        if not isinstance(raw_path, str) or not raw_path.strip():
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_PATH_MISSING",
                    "Readonly DB source artifact file metadata must include a path.",
                    source_index=source_index,
                    filename=filename,
                )
            )
            return blockers, None, None
        if not isinstance(raw_sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", raw_sha256.strip()):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_SHA_MISSING",
                    "Readonly DB source artifact file metadata must include a sha256 digest.",
                    source_index=source_index,
                    filename=filename,
                    path=raw_path,
                )
            )
            return blockers, None, None
        try:
            path = self.helpers.approved_artifact_path(raw_path)
        except self.helpers.evidence_error_type:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_PATH_UNSAFE",
                    "Readonly DB source artifact file path must stay under approved evidence roots.",
                    source_index=source_index,
                    filename=filename,
                    path=raw_path,
                )
            )
            return blockers, None, None
        if path.name != filename or path.resolve(strict=False).parent != source_dir.resolve(strict=False):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_PATH_UNSAFE",
                    "Readonly DB source artifact file path must stay in source_dir with the authoritative filename.",
                    source_index=source_index,
                    filename=filename,
                    path=self.helpers.public_path(path),
                    source_dir=self.helpers.public_path(source_dir),
                )
            )
            return blockers, None, None
        try:
            content = self.helpers.read_bytes_limited_no_follow(
                path,
                max_bytes=self.helpers.max_evidence_payload_bytes,
                containment_root=source_dir,
            )
            payload = json.loads(content.decode("utf-8"))
            self.helpers.ensure_bounded_evidence_value(payload, path=path)
        except FileNotFoundError:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_FILE_MISSING",
                    "Readonly DB source authoritative artifact is missing.",
                    source_index=source_index,
                    filename=filename,
                    path=self.helpers.public_path(path),
                )
            )
            return blockers, None, None
        except self.helpers.safe_filesystem_error_type:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_PATH_UNSAFE",
                    "Readonly DB source authoritative artifact path is unsafe.",
                    source_index=source_index,
                    filename=filename,
                    path=self.helpers.public_path(path),
                )
            )
            return blockers, None, None
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, self.helpers.evidence_error_type):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_JSON_INVALID",
                    "Readonly DB source authoritative artifact must be bounded valid JSON.",
                    source_index=source_index,
                    filename=filename,
                    path=self.helpers.public_path(path),
                )
            )
            return blockers, None, None
        sha256 = hashlib.sha256(content).hexdigest()
        if sha256 != raw_sha256.lower():
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_HASH_MISMATCH",
                    "Readonly DB source authoritative artifact sha256 does not match file content.",
                    source_index=source_index,
                    filename=filename,
                    path=self.helpers.public_path(path),
                )
            )
        artifact_run_id = artifact.get("run_id")
        if summary_run_id and artifact_run_id is not None and str(artifact_run_id) != summary_run_id:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_RUN_ID_MISMATCH",
                    "Readonly DB source artifact metadata run_id must match source summary run_id.",
                    source_index=source_index,
                    filename=filename,
                    artifact_run_id=artifact_run_id,
                    summary_run_id=summary_run_id,
                )
            )
        if artifact_run_id is None:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_RUN_ID_MISSING",
                    "Readonly DB source artifact metadata must include run_id.",
                    source_index=source_index,
                    filename=filename,
                )
            )
        return blockers, payload, sha256


    def _readonly_db_source_artifact_payload_issues(
        self,
        payloads: Mapping[str, Any],
        *,
        source_index: int,
        evidence_run_id: str,
        summary_run_id: str | None,
        source_dir: Path,
        lane_dir: Path,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
        blockers: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        sources: set[str] = set()
        summary = payloads.get("summary.json")
        if not isinstance(summary, Mapping):
            if "summary.json" in payloads:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_JSON_INVALID",
                        "Readonly DB source summary artifact must be a JSON object.",
                        source_index=source_index,
                        filename="summary.json",
                    )
                )
            return blockers, findings, sources
        if summary.get("schema") != READONLY_DB_LIVE_SCHEMA:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_LIVE_SCHEMA_MISSING",
                    "Readonly DB source summary artifact must use the live evidence schema.",
                    source_index=source_index,
                    schema=summary.get("schema"),
                )
            )
        if summary.get("status") != STATUS_PASS:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_NOT_PASS",
                    "Readonly DB source summary artifact must be PASS before final DB PASS.",
                    source_index=source_index,
                    status=summary.get("status"),
                )
            )
        if summary_run_id and str(summary.get("run_id") or "") != summary_run_id:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_RUN_ID_MISMATCH",
                    "Readonly DB source summary run_id must match recorded summary_run_id.",
                    source_index=source_index,
                    observed_run_id=summary.get("run_id"),
                    summary_run_id=summary_run_id,
                )
            )
        parent_blocker = self._readonly_db_source_parent_binding_blocker(
            {
                "parent_binding": "run_id_prefix"
                if self._is_source_run_prefix(str(summary.get("run_id") or ""), evidence_run_id)
                else None,
                "validation_provenance": summary.get("validation_provenance"),
            },
            evidence_run_id=evidence_run_id,
            source_index=source_index,
            source_dir=source_dir,
            lane_dir=lane_dir,
        )
        if parent_blocker is not None:
            blockers.append(parent_blocker)
        provenance = summary.get("validation_provenance")
        if not isinstance(provenance, Mapping) or provenance.get("mode") != "live" or provenance.get(
            "live_readonly_proof"
        ) is not True:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_LIVE_PROVENANCE_MISSING",
                    "Readonly DB source summary artifact must carry live mode/proof provenance.",
                    source_index=source_index,
                )
            )
        role_payload = payloads.get("role.json")
        if role_payload != summary.get("role"):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_SIBLING_MISMATCH",
                    "Readonly DB source role.json must match source summary role.",
                    source_index=source_index,
                    filename="role.json",
                )
            )
        route_payload = payloads.get("route_smoke.json")
        if route_payload != summary.get("route_smoke"):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_SIBLING_MISMATCH",
                    "Readonly DB source route_smoke.json must match source summary route_smoke.",
                    source_index=source_index,
                    filename="route_smoke.json",
                )
            )
        permission_payload = payloads.get("permission_probes.json")
        if permission_payload != summary.get("permission_probes"):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_SOURCE_ARTIFACT_SIBLING_MISMATCH",
                    "Readonly DB source permission_probes.json must match source summary permission_probes.",
                    source_index=source_index,
                    filename="permission_probes.json",
                )
            )
        sources.update(self._readonly_db_payload_proven_sources(summary))
        return blockers, findings, sources


    def _is_source_run_prefix(self, run_id: str, evidence_run_id: str) -> bool:
        run_id_lower = run_id.lower()
        expected_lower = evidence_run_id.lower()
        return run_id_lower in {
            f"{expected_lower}-gfs",
            f"{expected_lower}-ifs",
            f"{expected_lower}-db-gfs",
            f"{expected_lower}-db-ifs",
        }


    def _readonly_db_child_evidence_issues(
        self,
        payload: Mapping[str, Any],
        *,
        declared_sources: tuple[str, ...],
        strict_identities: Mapping[str, Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        blockers: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        route_smoke = payload.get("route_smoke")
        if isinstance(route_smoke, list):
            route_blockers, route_findings = self._readonly_db_route_issues(
                route_smoke,
                declared_sources=declared_sources,
                strict_identities=strict_identities,
                display_identity=payload.get("display_identity"),
            )
            blockers.extend(route_blockers)
            findings.extend(route_findings)
        manual_actions = payload.get("manual_action_probes")
        if isinstance(manual_actions, list):
            blockers.extend(self._readonly_db_manual_action_issues(manual_actions))
        permission_probes = payload.get("permission_probes")
        if isinstance(permission_probes, list):
            permission_blockers, permission_findings = self._readonly_db_permission_issues(permission_probes)
            blockers.extend(permission_blockers)
            findings.extend(permission_findings)
        blockers.extend(
            self._readonly_db_source_coverage_blockers(
                payload,
                declared_sources=declared_sources,
            )
        )
        return blockers, findings


    def _readonly_db_route_issues(
        self,
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
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_ROUTE_COVERAGE_MISSING",
                    "Readonly DB route smoke must cover all required display read routes.",
                    missing_routes=missing_routes,
                )
            )
        strict_route_sources: dict[str, set[str]] = {route: set() for route in READONLY_DB_STRICT_ROUTE_FIELDS}
        for route in routes:
            name = str(route.get("name") or "")
            route_status = self.helpers.normalized_status(route.get("status"))
            if route_status != STATUS_PASS:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_ROUTE_CHILD_NOT_PASS",
                        "Readonly DB route smoke child must be PASS before the DB lane can PASS.",
                        route=name,
                        child_status=route_status,
                    )
                )
            required_fields = READONLY_DB_STRICT_ROUTE_FIELDS.get(name)
            if required_fields is None:
                continue
            if route_status == STATUS_PASS:
                response_identity_blockers = route.get("identity_blockers")
                if isinstance(response_identity_blockers, list) and response_identity_blockers:
                    blockers.append(
                        self.helpers.blocker(
                            "TWO_NODE_E2E_READONLY_DB_ROUTE_RESPONSE_IDENTITY_INVALID",
                            "Readonly DB route smoke PASS contains response identity blockers.",
                            route=name,
                            blocker_count=len(response_identity_blockers),
                        )
                    )
            identity = self._readonly_route_identity(route, required_fields=required_fields)
            route_source = self.helpers.source_name(route.get("source") or route.get("source_id"))
            identity_source = self.helpers.source_name(identity.get("source") or identity.get("source_id"))
            source = route_source or identity_source
            missing_identity = [field for field in required_fields if not self.helpers.identity_value(identity, field)]
            if missing_identity:
                blockers.append(
                    self.helpers.blocker(
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
                    self.helpers.finding(
                        "TWO_NODE_E2E_READONLY_DB_ROUTE_SOURCE_IDENTITY_MISMATCH",
                        "Readonly DB route source key must match its embedded strict identity source.",
                        route=name,
                        source=route_source,
                        embedded_source=identity_source,
                    )
                )
            if source is None:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_ROUTE_SOURCE_MISSING",
                        "Readonly DB identity-bound route smoke child must identify its declared source.",
                        route=name,
                    )
                )
                continue
            if source not in declared_sources:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_ROUTE_SOURCE_UNDECLARED",
                        "Readonly DB identity-bound route smoke source is not declared in scope.",
                        route=name,
                        source=source,
                        declared_sources=list(declared_sources),
                    )
                )
                continue
            strict_route_sources[name].add(source)
            _, identity_findings, identity_blockers = self.helpers.identity_match_status(
                source,
                {"identity": identity},
                strict_identities,
                require_job_id="job_id" in required_fields,
            )
            findings.extend(self.helpers.with_context(item, route=name, source=source) for item in identity_findings)
            blockers.extend(self.helpers.with_context(item, route=name, source=source) for item in identity_blockers)
            expected_identity = self._readonly_display_identity_for_source(display_identity, source)
            for identity_field in required_fields:
                expected = self.helpers.identity_value(expected_identity, identity_field)
                observed = self.helpers.identity_value(identity, identity_field)
                if expected and observed and not self.helpers.strict_identity_value_matches(
                    identity_field,
                    observed,
                    expected,
                ):
                    findings.append(
                        self.helpers.finding(
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
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_ROUTE_SOURCE_COVERAGE_MISSING",
                        "Readonly DB route smoke must include identity-bound evidence for every declared source.",
                        route=route_name,
                        missing_sources=missing_sources,
                        observed_sources=sorted(observed_sources),
                    )
                )
        return blockers, findings


    def _readonly_db_source_coverage_blockers(
        self,
        payload: Mapping[str, Any],
        *,
        declared_sources: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if not declared_sources:
            return []
        observed_sources = self._readonly_db_evidence_sources(payload)
        missing_sources = sorted(source for source in declared_sources if source not in observed_sources)
        if not missing_sources:
            return []
        return [
            self.helpers.blocker(
                "TWO_NODE_E2E_READONLY_DB_SOURCE_COVERAGE_MISSING",
                "Readonly DB evidence must include producer-complete source identities for every declared source.",
                missing_sources=missing_sources,
                observed_sources=sorted(observed_sources),
            )
        ]


    def _readonly_db_evidence_sources(self, payload: Mapping[str, Any]) -> set[str]:
        sources: set[str] = set()
        display_identity = payload.get("display_identity")
        if isinstance(display_identity, Mapping):
            flat_source = self.helpers.source_name(display_identity.get("source") or display_identity.get("source_id"))
            if flat_source:
                sources.add(flat_source)
            for key, value in display_identity.items():
                key_source = self.helpers.source_name(key)
                if key_source and isinstance(value, Mapping):
                    sources.add(key_source)
                value_source = self.helpers.source_name(value.get("source") if isinstance(value, Mapping) else None)
                if value_source:
                    sources.add(value_source)
            for nested_key in ("sources", "strict_identities"):
                nested = display_identity.get(nested_key)
                if isinstance(nested, Mapping):
                    for key, value in nested.items():
                        key_source = self.helpers.source_name(key)
                        if key_source and isinstance(value, Mapping):
                            sources.add(key_source)
                        value_source = self.helpers.source_name(
                            value.get("source") if isinstance(value, Mapping) else None
                        )
                        if value_source:
                            sources.add(value_source)
        identity_source = self.helpers.source_name(payload.get("source") or payload.get("source_id"))
        if identity_source:
            sources.add(identity_source)
        return sources


    def _readonly_db_payload_proven_sources(self, payload: Mapping[str, Any]) -> set[str]:
        sources = self._readonly_db_evidence_sources(payload)
        route_smoke = payload.get("route_smoke")
        if isinstance(route_smoke, list):
            for route in route_smoke:
                if not isinstance(route, Mapping):
                    continue
                route_source = self.helpers.source_name(route.get("source") or route.get("source_id"))
                if route_source:
                    sources.add(route_source)
                required_fields = READONLY_DB_STRICT_ROUTE_FIELDS.get(
                    str(route.get("name") or ""),
                    STRICT_IDENTITY_FIELDS,
                )
                route_identity = self._readonly_route_identity(route, required_fields=required_fields)
                if isinstance(route_identity, Mapping):
                    identity_source = self.helpers.source_name(
                        route_identity.get("source") or route_identity.get("source_id")
                    )
                    if identity_source:
                        sources.add(identity_source)
        return sources


    def _readonly_display_identity_for_source(self, display_identity: Any, source: str) -> Mapping[str, Any]:
        if not isinstance(display_identity, Mapping):
            return {}
        source_key = self.helpers.source_name(source)
        source_scoped = display_identity.get(source) or display_identity.get(source_key or "")
        if isinstance(source_scoped, Mapping):
            return source_scoped
        nested_sources = display_identity.get("sources") or display_identity.get("strict_identities")
        if isinstance(nested_sources, Mapping):
            nested = nested_sources.get(source) or nested_sources.get(source_key or "")
            if isinstance(nested, Mapping):
                return nested
        identity_source = self.helpers.source_name(display_identity.get("source") or display_identity.get("source_id"))
        if identity_source == source_key:
            return display_identity
        return {}


    def _readonly_route_identity(self, route: Mapping[str, Any], *, required_fields: tuple[str, ...]) -> dict[str, Any]:
        raw = route.get("response_identity") or route.get("response_strict_identity") or route.get("body_identity")
        if not isinstance(raw, Mapping):
            return {}
        identity = dict(raw)
        if "source" not in identity and "source_id" in identity:
            identity["source"] = identity["source_id"]
        if all(self.helpers.identity_value(identity, field) for field in required_fields):
            return identity
        return {}


    def _readonly_db_manual_action_issues(self, actions: list[Any]) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        records = [item for item in actions if isinstance(item, Mapping)]
        observed_actions = {self.helpers.manual_action_name(item) for item in records}
        missing_actions = sorted(READONLY_DB_REQUIRED_MANUAL_ACTIONS - observed_actions)
        if missing_actions:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_MANUAL_ACTION_COVERAGE_MISSING",
                    "Readonly DB manual action probes must cover retry and cancel.",
                    missing_actions=missing_actions,
                )
            )
        for action in records:
            action_name = self.helpers.manual_action_name(action)
            action_status = self.helpers.normalized_status(action.get("status"))
            if action_status != STATUS_PASS:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_MANUAL_ACTION_CHILD_NOT_PASS",
                        "Readonly DB manual action child must be PASS before the DB lane can PASS.",
                        action=action_name,
                        child_status=action_status,
                    )
                )
            if self.helpers.manual_action_outcome_status(action) != STATUS_PASS:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_MANUAL_ACTION_OUTCOME_INVALID",
                        "Readonly DB manual action child must prove display retry/cancel returns manual action.",
                        action=action_name,
                        http_status=action.get("http_status") or action.get("status_code"),
                        observed_error_code=action.get("observed_error_code") or action.get("error_code"),
                    )
                )
            no_write_blockers = self._readonly_db_manual_action_no_write_issues(
                action,
                action_name=action_name,
            )
            blockers.extend(no_write_blockers)
        return blockers


    def _readonly_db_manual_action_no_write_issues(
        self,
        action: Mapping[str, Any],
        *,
        action_name: str,
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        for proof_name, aliases in READONLY_DB_MANUAL_WRITE_PROOF_ALIASES.items():
            observed = [(key, action.get(key)) for key in aliases if isinstance(action.get(key), bool)]
            if any(value is True for _key, value in observed):
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_MANUAL_ACTION_WRITE_PROOF_FAILED",
                        "Readonly DB manual action child recorded a write dependency or write execution.",
                        action=action_name,
                        proof=proof_name,
                        true_fields=[key for key, value in observed if value is True],
                    )
                )
            elif not observed:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_MANUAL_ACTION_NO_WRITE_PROOF_MISSING",
                        "Readonly DB manual action child must explicitly prove no write dependency and no write "
                        "execution.",
                        action=action_name,
                        proof=proof_name,
                        accepted_fields=list(aliases),
                    )
                )
        return blockers


    def _readonly_db_permission_issues(
        self,
        permission_probes: list[Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
                self.helpers.blocker(
                    "TWO_NODE_E2E_READONLY_DB_PERMISSION_COVERAGE_MISSING",
                    "Readonly DB permission probes must cover all required database mutation surfaces.",
                    missing_targets=missing_targets,
                )
            )
        for target in targets:
            target_status = self.helpers.normalized_status(target.get("status"))
            target_name = str(target.get("target") or "")
            operations = target.get("operations")
            reachable_findings = target.get("reachable_role_findings")
            if not isinstance(operations, list) or (not operations and target_name != "reachable_roles"):
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_PERMISSION_OPERATIONS_MISSING",
                        "Readonly DB permission target must include operation-level evidence.",
                        target=target_name,
                    )
                )
            if target_name == "reachable_roles":
                if isinstance(reachable_findings, list) and reachable_findings:
                    findings.append(
                        self.helpers.finding(
                            "TWO_NODE_E2E_READONLY_DB_REACHABLE_ROLE_FINDING",
                            "Readonly DB reachable role inventory found a mutating reachable role.",
                            target=target_name,
                            reachable_role_finding_count=len(reachable_findings),
                        )
                    )
                elif operations not in ([], None):
                    blockers.append(
                        self.helpers.blocker(
                            "TWO_NODE_E2E_READONLY_DB_REACHABLE_ROLE_OPERATIONS_UNEXPECTED",
                            "Readonly DB reachable_roles may use operations=[] only when no reachable role "
                            "findings exist.",
                            target=target_name,
                        )
                    )
            if target_status == STATUS_FAIL:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_PERMISSION_CHILD_FAILED",
                        "Readonly DB permission child failed and must not be summarized as PASS.",
                        target=target_name,
                    )
                )
            elif target_status == STATUS_BLOCKED:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_READONLY_DB_PERMISSION_CHILD_BLOCKED",
                        "Readonly DB permission child is blocked.",
                        target=target_name,
                    )
                )
            blockers.extend(self._readonly_db_permission_operation_coverage_blockers(target, operations))
            findings.extend(self._readonly_db_permission_catalog_findings(target))
            if isinstance(operations, list):
                for operation in operations:
                    if not isinstance(operation, Mapping):
                        continue
                    findings.extend(self._readonly_db_operation_findings(operation))
                    operation_status = self.helpers.normalized_status(operation.get("status"))
                    if operation_status == STATUS_BLOCKED:
                        blockers.append(
                            self.helpers.blocker(
                                "TWO_NODE_E2E_READONLY_DB_PERMISSION_OPERATION_BLOCKED",
                                "Readonly DB permission operation is blocked.",
                                target=target_name,
                                operation=operation.get("operation"),
                            )
                        )
        return blockers, findings


    def _readonly_db_operation_findings(self, operation: Mapping[str, Any]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        if operation.get("privilege_allowed") is True:
            findings.append(
                self.helpers.finding(
                    "TWO_NODE_E2E_READONLY_DB_MUTATING_PRIVILEGE",
                    "Readonly DB permission evidence contains a mutating privilege.",
                    operation=operation.get("operation"),
                    reason=operation.get("reason"),
                )
            )
        if operation.get("execution_outcome") == "succeeded":
            findings.append(
                self.helpers.finding(
                    "TWO_NODE_E2E_READONLY_DB_SUCCESSFUL_MUTATION_PROBE",
                    "Readonly DB permission evidence contains a successful DML/DDL probe.",
                    operation=operation.get("operation"),
                    reason=operation.get("reason"),
                )
            )
        return findings


    def _readonly_db_permission_operation_coverage_blockers(
        self,
        target: Mapping[str, Any],
        operations: Any,
    ) -> list[dict[str, Any]]:
        target_name = self._canonical_permission_target_name(target)
        if target_name == "reachable_roles":
            reachable_findings = target.get("reachable_role_findings")
            if operations == [] and reachable_findings == []:
                return []
        required_operations = self._readonly_db_required_operations_for_target(target_name)
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
            self.helpers.blocker(
                "TWO_NODE_E2E_READONLY_DB_PERMISSION_OPERATION_COVERAGE_MISSING",
                "Readonly DB permission target is missing required operation-level evidence.",
                target=target_name,
                missing_operations=missing_operations,
                observed_operations=sorted(observed_operations),
            )
        ]


    def _canonical_permission_target_name(self, target: Mapping[str, Any]) -> str:
        target_name = str(target.get("target") or "")
        surface = str(target.get("surface") or "")
        if target_name in {"current_database", "nhms", ""} and surface == "current_database_create_catalog":
            return "current_database"
        return target_name


    def _readonly_db_required_operations_for_target(self, target_name: str) -> frozenset[str]:
        if target_name in READONLY_DB_TABLE_PERMISSION_TARGETS:
            return READONLY_DB_TABLE_REQUIRED_OPERATIONS
        if target_name in READONLY_DB_SCHEMA_PERMISSION_TARGETS:
            return READONLY_DB_SCHEMA_REQUIRED_OPERATIONS
        if target_name == "current_database":
            return READONLY_DB_DATABASE_REQUIRED_OPERATIONS
        if target_name == "audited_schema_sequences":
            return READONLY_DB_SEQUENCE_REQUIRED_OPERATIONS
        return frozenset()


    def _readonly_db_permission_catalog_findings(self, target: Mapping[str, Any]) -> list[dict[str, Any]]:
        target_name = self._canonical_permission_target_name(target)
        findings: list[dict[str, Any]] = []
        if target_name in READONLY_DB_TABLE_PERMISSION_TARGETS:
            for field in READONLY_DB_TABLE_MUTATING_FIELDS:
                value = target.get(field)
                if self._catalog_value_has_mutating_privilege(value):
                    findings.append(
                        self.helpers.finding(
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
                    self.helpers.finding(
                        "TWO_NODE_E2E_READONLY_DB_SCHEMA_CREATE_PRIVILEGE",
                        "Readonly DB schema permission evidence contains CREATE privilege.",
                        target=target_name,
                    )
                )
        if target_name == "current_database":
            database_privileges = target.get("database_privileges")
            if isinstance(database_privileges, Mapping) and database_privileges.get("create") is True:
                findings.append(
                    self.helpers.finding(
                        "TWO_NODE_E2E_READONLY_DB_DATABASE_CREATE_PRIVILEGE",
                        "Readonly DB current database permission evidence contains CREATE privilege.",
                        target=target_name,
                    )
                )
        if target_name == "audited_schema_sequences" and self._catalog_value_has_mutating_privilege(
            target.get("sequence_privileges")
        ):
            findings.append(
                self.helpers.finding(
                    "TWO_NODE_E2E_READONLY_DB_AUDITED_SEQUENCE_MUTATING_PRIVILEGE",
                    "Readonly DB audited schema sequence evidence contains USAGE/UPDATE privilege.",
                    target=target_name,
                )
            )
        return findings


    def _catalog_value_has_mutating_privilege(self, value: Any) -> bool:
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
                if self._catalog_value_has_mutating_privilege(nested):
                    return True
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return True
                if self._catalog_value_has_mutating_privilege(item):
                    return True
        return False


    def _readonly_db_recomputed_status(
        self,
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
        child_blockers, child_findings = self._readonly_db_child_evidence_issues(
            payload,
            declared_sources=declared_sources,
            strict_identities=strict_identities,
        )
        blockers.extend(child_blockers)
        findings.extend(child_findings)
        return self.helpers.combined_status([STATUS_PASS], findings=findings, blockers=blockers)


    def _permission_operations_from_targets(self, targets: list[Any]) -> list[Mapping[str, Any]]:
        operations: list[Mapping[str, Any]] = []
        for target in targets:
            if not isinstance(target, Mapping):
                continue
            raw_operations = target.get("operations", [])
            if isinstance(raw_operations, list):
                operations.extend(item for item in raw_operations if isinstance(item, Mapping))
        return operations
