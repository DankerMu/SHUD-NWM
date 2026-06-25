from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

STATUS_PASS = "PASS"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

BROWSER_DOCUMENT_CANDIDATES = (
    "browser/summary.json",
    "browser/evidence.json",
)
BROWSER_BASE_REQUIRED_CHECKS = (
    "hydro_met",
    "ops",
    "ops_jobs",
    "ops_job_logs",
)
BROWSER_SOURCE_SWITCH_CHECK = "source_switch"
BROWSER_JOB_ID_REQUIRED_CHECKS = ("ops_jobs", "ops_job_logs")
BROWSER_LIVE_FLAG = "live_browser_evidence"
BROWSER_LANE_OWNER = "services.production_closure.two_node_e2e_browser_lane"
BROWSER_LANE_VERIFICATION = 'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "browser"'
BROWSER_LANE_GUARD_SYMBOLS = (
    "BROWSER_DOCUMENT_CANDIDATES",
    "BROWSER_BASE_REQUIRED_CHECKS",
    "BROWSER_SOURCE_SWITCH_CHECK",
    "BROWSER_JOB_ID_REQUIRED_CHECKS",
    "BROWSER_LIVE_FLAG",
    "BROWSER_LANE_OWNER",
    "BROWSER_LANE_VERIFICATION",
    "BROWSER_LANE_BLOCKER_NAMESPACES",
    "BrowserLaneEvaluationHelpers",
    "browser_required_checks",
    "evaluate_browser_lane",
    "_BrowserLaneEvaluator",
)
BROWSER_LANE_BLOCKER_NAMESPACES = (
    "TWO_NODE_E2E_BROWSER_",
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
class BrowserLaneEvaluationHelpers(Generic[LaneResultT]):
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


def browser_required_checks(declared_sources: Sequence[str]) -> tuple[str, ...]:
    checks = list(BROWSER_BASE_REQUIRED_CHECKS)
    if len(declared_sources) > 1:
        checks.append(BROWSER_SOURCE_SWITCH_CHECK)
    return tuple(checks)


def evaluate_browser_lane(
    doc: EvidenceDocumentLike | None,
    *,
    declared_sources: tuple[str, ...],
    strict_identities: Mapping[str, Mapping[str, Any]],
    evidence_run_id: str,
    helpers: BrowserLaneEvaluationHelpers[LaneResultT],
) -> LaneResultT:
    return _BrowserLaneEvaluator(helpers).evaluate(
        doc,
        declared_sources=declared_sources,
        strict_identities=strict_identities,
        evidence_run_id=evidence_run_id,
    )


@dataclass(frozen=True)
class _BrowserLaneEvaluator(Generic[LaneResultT]):
    helpers: BrowserLaneEvaluationHelpers[LaneResultT]

    def evaluate(
        self,
        doc: EvidenceDocumentLike | None,
        *,
        declared_sources: tuple[str, ...],
        strict_identities: Mapping[str, Mapping[str, Any]],
        evidence_run_id: str,
    ) -> LaneResultT:
        name = "browser"
        if doc is None:
            return self.helpers.missing_lane(name, "TWO_NODE_E2E_BROWSER_EVIDENCE_MISSING")

        payload = doc.payload
        required_checks = browser_required_checks(declared_sources)
        status = self.helpers.normalized_status(payload.get("status"))
        blockers = list(self.helpers.stale_lane_blockers(payload))
        findings: list[dict[str, Any]] = []
        partial_sources = False

        if self.helpers.has_mock_or_fixture(payload):
            findings.append(
                self.helpers.finding(
                    "TWO_NODE_E2E_BROWSER_MOCK_EVIDENCE",
                    "browser evidence uses mock or deterministic fixture data.",
                )
            )
        if self.helpers.has_historical_latest(payload):
            findings.append(
                self.helpers.finding(
                    "TWO_NODE_E2E_BROWSER_HISTORICAL_LATEST",
                    "browser evidence uses historical latest or source-only fallback.",
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
                        "TWO_NODE_E2E_BROWSER_LIVE_EVIDENCE_MISSING",
                        "browser PASS requires live evidence.",
                    )
                )
            if not self.helpers.has_producer_backed_lane_evidence(payload):
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_BROWSER_PRODUCER_EVIDENCE_MISSING",
                        "browser PASS requires producer-backed command, artifact, request/response, browser, "
                        "network, or per-check evidence.",
                    )
                )
            blockers.extend(
                self.helpers.source_lane_check_producer_blockers(
                    name,
                    payload,
                    declared_sources=declared_sources,
                    required_checks=required_checks,
                    strict_identities=strict_identities,
                    evidence_run_id=evidence_run_id,
                )
            )

        records = self.helpers.source_records(payload)
        missing_sources = [source for source in declared_sources if source not in records]
        for source in missing_sources:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_BROWSER_SOURCE_MISSING",
                    f"browser evidence is missing declared source {source}.",
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
                        "TWO_NODE_E2E_BROWSER_SOURCE_FAILED",
                        "browser source evidence failed.",
                        source=source,
                    )
                )
            elif source_status == STATUS_BLOCKED:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_BROWSER_SOURCE_BLOCKED",
                        "browser source evidence is blocked.",
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
            for check in required_checks:
                check_result = check_results.get(check)
                if check_result is None:
                    blockers.append(
                        self.helpers.blocker(
                            "TWO_NODE_E2E_BROWSER_CHECK_MISSING",
                            f"browser source evidence is missing required check {check}.",
                            source=source,
                            check=check,
                        )
                    )
                    continue
                check_status = self.helpers.normalized_status(check_result.get("status"))
                if self.helpers.has_mock_or_fixture(check_result):
                    findings.append(
                        self.helpers.finding(
                            "TWO_NODE_E2E_BROWSER_MOCK_CHECK",
                            "browser check uses mock or fixture data.",
                            source=source,
                            check=check,
                        )
                    )
                if self.helpers.has_historical_latest(check_result):
                    findings.append(
                        self.helpers.finding(
                            "TWO_NODE_E2E_BROWSER_HISTORICAL_CHECK",
                            "browser check uses historical latest or source-only fallback.",
                            source=source,
                            check=check,
                        )
                    )
                if check_status == STATUS_FAIL:
                    findings.append(
                        self.helpers.finding(
                            "TWO_NODE_E2E_BROWSER_CHECK_FAILED",
                            "browser required check failed.",
                            source=source,
                            check=check,
                        )
                    )
                elif check_status != STATUS_PASS:
                    blockers.append(
                        self.helpers.blocker(
                            "TWO_NODE_E2E_BROWSER_CHECK_BLOCKED",
                            "browser required check is not PASS.",
                            source=source,
                            check=check,
                            check_status=check_status,
                        )
                    )

                _, check_findings, check_blockers = self.helpers.identity_match_status(
                    source,
                    check_result,
                    strict_identities,
                    require_job_id=check in BROWSER_JOB_ID_REQUIRED_CHECKS,
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
