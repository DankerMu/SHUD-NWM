from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from services.production_closure.two_node_e2e_metadata_lane import (
    FULL_PASS_SOURCE_SET,
    STRICT_LOG_IDENTITY_FIELDS,
)

STATUS_PASS = "PASS"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

CROSS_PLANE_DOCUMENT_CANDIDATES = (
    "cross-plane/summary.json",
    "cross-plane/evidence.json",
)
CROSS_PLANE_LIVE_FLAG = "live_cross_plane_evidence"
CROSS_PLANE_LANE_OWNER = "services.production_closure.two_node_e2e_cross_plane_lane"
CROSS_PLANE_LANE_VERIFICATION = (
    'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "cross_plane or source_scope or reduced_scope"'
)
CROSS_PLANE_LANE_GUARD_SYMBOLS = (
    "CROSS_PLANE_DOCUMENT_CANDIDATES",
    "CROSS_PLANE_LIVE_FLAG",
    "CROSS_PLANE_LANE_OWNER",
    "CROSS_PLANE_LANE_VERIFICATION",
    "CROSS_PLANE_BLOCKER_NAMESPACES",
    "CrossPlaneEvaluationHelpers",
    "build_source_scope_results",
    "evaluate_cross_plane_lane",
    "is_full_scope_sources",
    "is_full_scope_pass",
    "_CrossPlaneEvaluator",
)
CROSS_PLANE_BLOCKER_NAMESPACES = (
    "TWO_NODE_E2E_CROSS_PLANE_",
    "TWO_NODE_E2E_SOURCE_",
    "TWO_NODE_E2E_REDUCED_SOURCE_SCOPE",
    "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_",
    "TWO_NODE_E2E_STRICT_IDENTITY_",
    "TWO_NODE_E2E_EXPECTED_STRICT_IDENTITY_INCOMPLETE",
    "TWO_NODE_E2E_OBSERVED_STRICT_IDENTITY_INCOMPLETE",
    "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_",
    "TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH",
    "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
)

LaneResultT = TypeVar("LaneResultT")
LaneResultT_co = TypeVar("LaneResultT_co", covariant=True)


class EvidenceDocumentLike(Protocol):
    path: Path
    payload: Mapping[str, Any]
    sha256: str


class LaneEvaluationLike(Protocol):
    status: str
    blockers: Sequence[Mapping[str, Any]]
    findings: Sequence[Mapping[str, Any]]


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


class NormalizedStatus(Protocol):
    def __call__(self, value: Any, *, pass_aliases: Sequence[str] = (STATUS_PASS,)) -> str: ...


class CurrentRunBlockers(Protocol):
    def __call__(
        self,
        payload: Mapping[str, Any],
        evidence_run_id: str,
        *,
        lane_name: str,
    ) -> list[dict[str, Any]]: ...


class RecursiveCurrentRunBlockers(Protocol):
    def __call__(
        self,
        value: Any,
        evidence_run_id: str,
        *,
        lane_name: str,
    ) -> list[dict[str, Any]]: ...


class ProducerSourceArtifactBlockers(Protocol):
    def __call__(
        self,
        value: Any,
        *,
        evidence_run_id: str,
        lane_name: str,
        run_dir: Path,
    ) -> list[dict[str, Any]]: ...


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
class CrossPlaneEvaluationHelpers(Generic[LaneResultT]):
    missing_lane: MissingLaneAdapter[LaneResultT]
    lane_from_status: LaneFromStatusAdapter[LaneResultT]
    normalized_status: NormalizedStatus
    combined_status: Callable[..., str]
    blocker: BlockerFactory
    finding: FindingFactory
    stale_lane_blockers: Callable[[Mapping[str, Any]], list[dict[str, Any]]]
    current_run_blockers: CurrentRunBlockers
    recursive_current_run_blockers: RecursiveCurrentRunBlockers
    producer_source_artifact_blockers: ProducerSourceArtifactBlockers
    has_live_lane_evidence: Callable[[Mapping[str, Any]], bool]
    has_producer_backed_lane_evidence: Callable[[Mapping[str, Any]], bool]
    has_mock_or_fixture: Callable[[Mapping[str, Any]], bool]
    has_historical_latest: Callable[[Mapping[str, Any]], bool]
    source_records: Callable[[Mapping[str, Any]], dict[str, Mapping[str, Any]]]
    identity_match_status: IdentityMatchStatus
    identity_value: Callable[[Mapping[str, Any], str], str | None]
    redact_payload: Callable[[Any], Any]
    with_context: Callable[..., dict[str, Any]]


def build_source_scope_results(
    *,
    declared_sources: tuple[str, ...],
    strict_identities: Mapping[str, Mapping[str, Any]],
    source_lanes: Mapping[str, LaneEvaluationLike],
    helpers: CrossPlaneEvaluationHelpers[Any],
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for source in declared_sources:
        blockers: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        identity = dict(strict_identities.get(source, {}))
        missing_identity = [
            field for field in STRICT_LOG_IDENTITY_FIELDS if not helpers.identity_value(identity, field)
        ]
        if missing_identity:
            blockers.append(
                helpers.blocker(
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
        status = helpers.combined_status(
            [str(value) for value in lane_statuses.values()],
            findings=findings,
            blockers=blockers,
        )
        results[source] = {
            "status": status,
            "identity": helpers.redact_payload(identity),
            "lane_statuses": lane_statuses,
            "blockers": blockers,
            "findings": findings,
        }
    return results


def evaluate_cross_plane_lane(
    doc: EvidenceDocumentLike | None,
    *,
    declared_sources: tuple[str, ...],
    strict_identities: Mapping[str, Mapping[str, Any]],
    source_scope_results: Mapping[str, Mapping[str, Any]],
    reduced_scope: bool,
    evidence_run_id: str,
    helpers: CrossPlaneEvaluationHelpers[LaneResultT],
) -> LaneResultT:
    evaluator = _CrossPlaneEvaluator(
        doc,
        declared_sources=declared_sources,
        strict_identities=strict_identities,
        source_scope_results=source_scope_results,
        reduced_scope=reduced_scope,
        evidence_run_id=evidence_run_id,
        helpers=helpers,
    )
    return evaluator.evaluate()


def is_full_scope_sources(declared_sources: tuple[str, ...]) -> bool:
    return frozenset(declared_sources) == FULL_PASS_SOURCE_SET


def is_full_scope_pass(
    declared_sources: tuple[str, ...],
    source_scope_results: Mapping[str, Mapping[str, Any]],
) -> bool:
    return is_full_scope_sources(declared_sources) and all(
        source_scope_results.get(source, {}).get("status") == STATUS_PASS
        for source in sorted(FULL_PASS_SOURCE_SET)
    )


class _CrossPlaneEvaluator(Generic[LaneResultT]):
    def __init__(
        self,
        doc: EvidenceDocumentLike | None,
        *,
        declared_sources: tuple[str, ...],
        strict_identities: Mapping[str, Mapping[str, Any]],
        source_scope_results: Mapping[str, Mapping[str, Any]],
        reduced_scope: bool,
        evidence_run_id: str,
        helpers: CrossPlaneEvaluationHelpers[LaneResultT],
    ) -> None:
        self.doc = doc
        self.declared_sources = declared_sources
        self.strict_identities = strict_identities
        self.source_scope_results = source_scope_results
        self.reduced_scope = reduced_scope
        self.evidence_run_id = evidence_run_id
        self.helpers = helpers

    def evaluate(self) -> LaneResultT:
        if self.doc is None:
            return self.helpers.missing_lane("cross_plane", "TWO_NODE_E2E_CROSS_PLANE_EVIDENCE_MISSING")
        payload = self.doc.payload
        status = self.helpers.normalized_status(payload.get("status"))
        blockers = list(self.helpers.stale_lane_blockers(payload))
        findings: list[dict[str, Any]] = []
        if status == STATUS_PASS:
            blockers.extend(
                self.helpers.current_run_blockers(
                    payload,
                    self.evidence_run_id,
                    lane_name="cross_plane",
                )
            )
            blockers.extend(
                self.helpers.recursive_current_run_blockers(
                    payload,
                    self.evidence_run_id,
                    lane_name="cross_plane",
                )
            )
            blockers.extend(
                self.helpers.producer_source_artifact_blockers(
                    payload,
                    evidence_run_id=self.evidence_run_id,
                    lane_name="cross_plane",
                    run_dir=self.doc.path.parents[1],
                )
            )
            if not self.helpers.has_producer_backed_lane_evidence(payload):
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_CROSS_PLANE_PRODUCER_EVIDENCE_MISSING",
                        "Cross-plane PASS requires producer-backed command, artifact, request/response, browser, "
                        "network, or per-check evidence.",
                    )
                )
        if self.helpers.has_mock_or_fixture(payload):
            findings.append(
                self.helpers.finding(
                    "TWO_NODE_E2E_CROSS_PLANE_MOCK_EVIDENCE",
                    "Cross-plane evidence uses mock or deterministic fixture data.",
                )
            )
        if self.helpers.has_historical_latest(payload):
            findings.append(
                self.helpers.finding(
                    "TWO_NODE_E2E_CROSS_PLANE_HISTORICAL_LATEST",
                    "Cross-plane evidence uses historical latest or source-only fallback.",
                )
            )
        if status == STATUS_PASS and not self.helpers.has_live_lane_evidence(payload):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_CROSS_PLANE_LIVE_EVIDENCE_MISSING",
                    "Cross-plane PASS requires live identity-bound evidence.",
                )
            )
        records = self.helpers.source_records(payload)
        for source in self.declared_sources:
            record = records.get(source)
            if record is None:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_CROSS_PLANE_SOURCE_MISSING",
                        "Cross-plane evidence is missing a declared source.",
                        source=source,
                    )
                )
                continue
            _, identity_findings, identity_blockers = self.helpers.identity_match_status(
                source,
                record,
                self.strict_identities,
            )
            findings.extend(
                self.helpers.with_context(item, lane="cross_plane", source=source)
                for item in identity_findings
            )
            blockers.extend(
                self.helpers.with_context(item, lane="cross_plane", source=source)
                for item in identity_blockers
            )
        source_statuses = {
            source: result.get("status")
            for source, result in self.source_scope_results.items()
        }
        if status == STATUS_FAIL or any(value == STATUS_FAIL for value in source_statuses.values()):
            status = STATUS_FAIL
        elif status == STATUS_BLOCKED or any(value == STATUS_BLOCKED for value in source_statuses.values()):
            status = STATUS_BLOCKED
        if findings:
            status = STATUS_FAIL
        elif blockers:
            status = STATUS_BLOCKED
        elif status == STATUS_PASS and (
            not is_full_scope_pass(self.declared_sources, self.source_scope_results) or self.reduced_scope
        ):
            status = STATUS_PARTIAL
        return self.helpers.lane_from_status(
            "cross_plane",
            self.doc,
            status=status,
            summary_status=str(payload.get("status", "unknown")),
            blockers=blockers,
            findings=findings,
        )
