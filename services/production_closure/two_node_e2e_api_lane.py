from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

STATUS_PASS = "PASS"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

API_DOCUMENT_CANDIDATES = (
    "api/summary.json",
    "api/evidence.json",
)
API_REQUIRED_CHECKS = (
    "latest_product",
    "series",
    "ops_status",
    "ops_stages",
    "jobs",
)
API_LIVE_FLAG = "live_api_evidence"
API_LANE_OWNER = "services.production_closure.two_node_e2e_api_lane"
API_LANE_VERIFICATION = 'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "api"'
API_LANE_GUARD_SYMBOLS = (
    "API_DOCUMENT_CANDIDATES",
    "API_REQUIRED_CHECKS",
    "API_LIVE_FLAG",
    "API_LANE_OWNER",
    "API_LANE_VERIFICATION",
    "API_LANE_BLOCKER_NAMESPACES",
    "ApiLaneEvaluationHelpers",
    "evaluate_api_lane",
    "_ApiLaneEvaluator",
)
API_LANE_BLOCKER_NAMESPACES = (
    "TWO_NODE_E2E_API_",
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


class SourceLaneCheckProducerBlockers(Protocol):
    def __call__(
        self,
        lane_name: str,
        payload: Mapping[str, Any],
        *,
        declared_sources: tuple[str, ...],
        required_checks: tuple[str, ...],
        strict_identities: Mapping[str, Mapping[str, Any]],
        evidence_run_id: str,
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
class ApiLaneEvaluationHelpers(Generic[LaneResultT]):
    missing_lane: MissingLaneAdapter[LaneResultT]
    lane_from_status: LaneFromStatusAdapter[LaneResultT]
    normalized_status: NormalizedStatus
    blocker: BlockerFactory
    finding: FindingFactory
    stale_lane_blockers: Callable[[Mapping[str, Any]], list[dict[str, Any]]]
    current_run_blockers: CurrentRunBlockers
    recursive_current_run_blockers: RecursiveCurrentRunBlockers
    producer_source_artifact_blockers: ProducerSourceArtifactBlockers
    source_lane_check_producer_blockers: SourceLaneCheckProducerBlockers
    has_live_lane_evidence: Callable[[Mapping[str, Any]], bool]
    has_producer_backed_lane_evidence: Callable[[Mapping[str, Any]], bool]
    has_mock_or_fixture: Callable[[Mapping[str, Any]], bool]
    has_historical_latest: Callable[[Mapping[str, Any]], bool]
    source_records: Callable[[Mapping[str, Any]], dict[str, Mapping[str, Any]]]
    check_results: Callable[[Mapping[str, Any]], dict[str, Mapping[str, Any]]]
    identity_match_status: IdentityMatchStatus
    with_context: Callable[..., dict[str, Any]]


def evaluate_api_lane(
    doc: EvidenceDocumentLike | None,
    *,
    declared_sources: tuple[str, ...],
    strict_identities: Mapping[str, Mapping[str, Any]],
    evidence_run_id: str,
    helpers: ApiLaneEvaluationHelpers[LaneResultT],
) -> LaneResultT:
    return _ApiLaneEvaluator(helpers).evaluate(
        doc,
        declared_sources=declared_sources,
        strict_identities=strict_identities,
        evidence_run_id=evidence_run_id,
    )


@dataclass(frozen=True)
class _ApiLaneEvaluator(Generic[LaneResultT]):
    helpers: ApiLaneEvaluationHelpers[LaneResultT]

    def evaluate(
        self,
        doc: EvidenceDocumentLike | None,
        *,
        declared_sources: tuple[str, ...],
        strict_identities: Mapping[str, Mapping[str, Any]],
        evidence_run_id: str,
    ) -> LaneResultT:
        name = "api"
        if doc is None:
            return self.helpers.missing_lane(name, "TWO_NODE_E2E_API_EVIDENCE_MISSING")

        payload = doc.payload
        status = self.helpers.normalized_status(payload.get("status"))
        blockers = list(self.helpers.stale_lane_blockers(payload))
        findings: list[dict[str, Any]] = []
        partial_sources = False

        if self.helpers.has_mock_or_fixture(payload):
            findings.append(
                self.helpers.finding(
                    "TWO_NODE_E2E_API_MOCK_EVIDENCE",
                    "api evidence uses mock or deterministic fixture data.",
                )
            )
        if self.helpers.has_historical_latest(payload):
            findings.append(
                self.helpers.finding(
                    "TWO_NODE_E2E_API_HISTORICAL_LATEST",
                    "api evidence uses historical latest or source-only fallback.",
                )
            )

        if status == STATUS_PASS:
            producer_scope_dir = doc.path.parents[1] if len(doc.path.parents) > 1 else doc.path.parent
            blockers.extend(self.helpers.current_run_blockers(payload, evidence_run_id, lane_name=name))
            blockers.extend(self.helpers.recursive_current_run_blockers(payload, evidence_run_id, lane_name=name))
            blockers.extend(
                self.helpers.producer_source_artifact_blockers(
                    payload,
                    evidence_run_id=evidence_run_id,
                    lane_name=name,
                    run_dir=producer_scope_dir,
                )
            )
            if not self.helpers.has_live_lane_evidence(payload):
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_API_LIVE_EVIDENCE_MISSING",
                        "api PASS requires live evidence.",
                    )
                )
            if not self.helpers.has_producer_backed_lane_evidence(payload):
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_API_PRODUCER_EVIDENCE_MISSING",
                        "api PASS requires producer-backed command, artifact, request/response, browser, "
                        "network, or per-check evidence.",
                    )
                )
            blockers.extend(
                self.helpers.source_lane_check_producer_blockers(
                    name,
                    payload,
                    declared_sources=declared_sources,
                    required_checks=API_REQUIRED_CHECKS,
                    strict_identities=strict_identities,
                    evidence_run_id=evidence_run_id,
                )
            )

        records = self.helpers.source_records(payload)
        missing_sources = [source for source in declared_sources if source not in records]
        for source in missing_sources:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_API_SOURCE_MISSING",
                    f"api evidence is missing declared source {source}.",
                    source=source,
                )
            )

        for source in declared_sources:
            record = records.get(source)
            if record is None:
                continue
            source_status = self.helpers.normalized_status(record.get("status"))
            if source_status == STATUS_FAIL:
                findings.append(
                    self.helpers.finding(
                        "TWO_NODE_E2E_API_SOURCE_FAILED",
                        "api source evidence failed.",
                        source=source,
                    )
                )
            elif source_status == STATUS_BLOCKED:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_API_SOURCE_BLOCKED",
                        "api source evidence is blocked.",
                        source=source,
                    )
                )
            elif source_status == STATUS_PARTIAL:
                partial_sources = True

            _, identity_findings, identity_blockers = self.helpers.identity_match_status(
                source,
                record,
                strict_identities,
            )
            findings.extend(self.helpers.with_context(item, lane=name, source=source) for item in identity_findings)
            blockers.extend(self.helpers.with_context(item, lane=name, source=source) for item in identity_blockers)

            check_results = self.helpers.check_results(record)
            for check in API_REQUIRED_CHECKS:
                check_result = check_results.get(check)
                if check_result is None:
                    blockers.append(
                        self.helpers.blocker(
                            "TWO_NODE_E2E_API_CHECK_MISSING",
                            f"api source evidence is missing required check {check}.",
                            source=source,
                            check=check,
                        )
                    )
                    continue
                check_status = self.helpers.normalized_status(check_result.get("status"))
                if self.helpers.has_mock_or_fixture(check_result):
                    findings.append(
                        self.helpers.finding(
                            "TWO_NODE_E2E_API_MOCK_CHECK",
                            "api check uses mock or fixture data.",
                            source=source,
                            check=check,
                        )
                    )
                if self.helpers.has_historical_latest(check_result):
                    findings.append(
                        self.helpers.finding(
                            "TWO_NODE_E2E_API_HISTORICAL_CHECK",
                            "api check uses historical latest or source-only fallback.",
                            source=source,
                            check=check,
                        )
                    )
                if check_status == STATUS_FAIL:
                    findings.append(
                        self.helpers.finding(
                            "TWO_NODE_E2E_API_CHECK_FAILED",
                            "api required check failed.",
                            source=source,
                            check=check,
                        )
                    )
                elif check_status != STATUS_PASS:
                    blockers.append(
                        self.helpers.blocker(
                            "TWO_NODE_E2E_API_CHECK_BLOCKED",
                            "api required check is not PASS.",
                            source=source,
                            check=check,
                            check_status=check_status,
                        )
                    )

                _, check_findings, check_blockers = self.helpers.identity_match_status(
                    source,
                    check_result,
                    strict_identities,
                )
                findings.extend(
                    self.helpers.with_context(item, lane=name, source=source, check=check)
                    for item in check_findings
                )
                blockers.extend(
                    self.helpers.with_context(item, lane=name, source=source, check=check)
                    for item in check_blockers
                )

        if findings:
            status = STATUS_FAIL
        elif status == STATUS_PASS:
            if blockers:
                status = STATUS_BLOCKED
            elif partial_sources:
                status = STATUS_PARTIAL

        return self.helpers.lane_from_status(
            name,
            doc,
            status=status,
            summary_status=str(payload.get("status", "unknown")),
            blockers=blockers,
            findings=findings,
        )
