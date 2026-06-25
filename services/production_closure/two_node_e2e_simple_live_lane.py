from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

SLURM_DOCUMENT_CANDIDATES = (
    "slurm/summary.json",
    "slurm/evidence.json",
)
COMPUTE_SUMMARY_DOCUMENT_CANDIDATES = (
    "22-compute/summary.json",
    "compute/summary.json",
    "compute-summary.json",
)
DISPLAY_SUMMARY_DOCUMENT_CANDIDATES = (
    "27-display/summary.json",
    "display/summary.json",
    "display-summary.json",
)
SIMPLE_LIVE_LANE_OWNER = "services.production_closure.two_node_e2e_simple_live_lane"
SLURM_LANE_VERIFICATION = 'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or slurm"'
COMPUTE_SUMMARY_LANE_VERIFICATION = (
    'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or compute_summary"'
)
DISPLAY_SUMMARY_LANE_VERIFICATION = (
    'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "simple_lane or display_summary"'
)
SIMPLE_LIVE_LANE_GUARD_SYMBOLS = (
    "SimpleLiveLaneConfig",
    "SimpleLiveLaneEvaluationHelpers",
    "SLURM_DOCUMENT_CANDIDATES",
    "COMPUTE_SUMMARY_DOCUMENT_CANDIDATES",
    "DISPLAY_SUMMARY_DOCUMENT_CANDIDATES",
    "SLURM_LANE_CONFIG",
    "COMPUTE_SUMMARY_LANE_CONFIG",
    "DISPLAY_SUMMARY_LANE_CONFIG",
    "SIMPLE_LIVE_LANE_CONFIGS",
    "evaluate_simple_live_lane",
)
SIMPLE_LIVE_LANE_BLOCKER_NAMESPACES = (
    "TWO_NODE_E2E_SLURM_",
    "TWO_NODE_E2E_COMPUTE_SUMMARY_",
    "TWO_NODE_E2E_DISPLAY_SUMMARY_",
    "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_",
    "TWO_NODE_E2E_NESTED_CURRENT_EVIDENCE_RUN_ID_MISMATCH",
    "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
    "TWO_NODE_E2E_PRODUCER_SOURCE_ARTIFACT_",
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


class HasLiveLaneEvidence(Protocol):
    def __call__(self, payload: Mapping[str, Any], *, live_flag: str) -> bool: ...


@dataclass(frozen=True)
class SimpleLiveLaneConfig:
    name: str
    document_candidates: tuple[str, ...]
    live_flag: str
    pass_aliases: tuple[str, ...] = (STATUS_PASS,)


SLURM_LANE_CONFIG = SimpleLiveLaneConfig(
    name="slurm",
    document_candidates=SLURM_DOCUMENT_CANDIDATES,
    live_flag="live_slurm_evidence",
)
COMPUTE_SUMMARY_LANE_CONFIG = SimpleLiveLaneConfig(
    name="compute_summary",
    document_candidates=COMPUTE_SUMMARY_DOCUMENT_CANDIDATES,
    live_flag="live_compute_evidence",
    pass_aliases=(STATUS_PASS, "ready", "submitted"),
)
DISPLAY_SUMMARY_LANE_CONFIG = SimpleLiveLaneConfig(
    name="display_summary",
    document_candidates=DISPLAY_SUMMARY_DOCUMENT_CANDIDATES,
    live_flag="live_display_evidence",
    pass_aliases=(STATUS_PASS, "ready"),
)
SIMPLE_LIVE_LANE_CONFIGS: Mapping[str, SimpleLiveLaneConfig] = {
    lane_config.name: lane_config
    for lane_config in (
        SLURM_LANE_CONFIG,
        COMPUTE_SUMMARY_LANE_CONFIG,
        DISPLAY_SUMMARY_LANE_CONFIG,
    )
}


@dataclass(frozen=True)
class SimpleLiveLaneEvaluationHelpers(Generic[LaneResultT]):
    missing_lane: MissingLaneAdapter[LaneResultT]
    lane_from_status: LaneFromStatusAdapter[LaneResultT]
    normalized_status: NormalizedStatus
    blocker: BlockerFactory
    finding: FindingFactory
    stale_lane_blockers: Callable[[Mapping[str, Any]], list[dict[str, Any]]]
    current_run_blockers: CurrentRunBlockers
    recursive_current_run_blockers: RecursiveCurrentRunBlockers
    producer_source_artifact_blockers: ProducerSourceArtifactBlockers
    has_live_lane_evidence: HasLiveLaneEvidence
    has_producer_backed_lane_evidence: Callable[[Mapping[str, Any]], bool]
    has_mock_or_fixture: Callable[[Mapping[str, Any]], bool]


def evaluate_simple_live_lane(
    lane_config: SimpleLiveLaneConfig,
    doc: EvidenceDocumentLike | None,
    *,
    evidence_run_id: str,
    run_dir: Path,
    helpers: SimpleLiveLaneEvaluationHelpers[LaneResultT],
) -> LaneResultT:
    name = lane_config.name
    if doc is None:
        return helpers.missing_lane(name, f"TWO_NODE_E2E_{name.upper()}_EVIDENCE_MISSING")
    payload = doc.payload
    status = helpers.normalized_status(payload.get("status"), pass_aliases=lane_config.pass_aliases)
    blockers = list(helpers.stale_lane_blockers(payload))
    findings: list[dict[str, Any]] = []
    if status == STATUS_PASS:
        producer_scope_dir = doc.path.parents[1] if len(doc.path.parents) > 1 else run_dir
        blockers.extend(helpers.current_run_blockers(payload, evidence_run_id, lane_name=name))
        blockers.extend(helpers.recursive_current_run_blockers(payload, evidence_run_id, lane_name=name))
        blockers.extend(
            helpers.producer_source_artifact_blockers(
                payload,
                evidence_run_id=evidence_run_id,
                lane_name=name,
                run_dir=producer_scope_dir,
            )
        )
        if not helpers.has_live_lane_evidence(payload, live_flag=lane_config.live_flag):
            blockers.append(
                helpers.blocker(
                    f"TWO_NODE_E2E_{name.upper()}_LIVE_EVIDENCE_MISSING",
                    f"{name} PASS requires live evidence.",
                )
            )
        if not helpers.has_producer_backed_lane_evidence(payload):
            blockers.append(
                helpers.blocker(
                    f"TWO_NODE_E2E_{name.upper()}_PRODUCER_EVIDENCE_MISSING",
                    f"{name} PASS requires producer-backed command, artifact, request/response, browser, "
                    "network, or per-check evidence.",
                )
            )
    if status == STATUS_PASS and helpers.has_mock_or_fixture(payload):
        findings.append(
            helpers.finding(
                f"TWO_NODE_E2E_{name.upper()}_MOCK_EVIDENCE",
                f"{name} evidence uses mock or deterministic fixture data.",
            )
        )
    if status == STATUS_PASS:
        if findings:
            status = STATUS_FAIL
        elif blockers:
            status = STATUS_BLOCKED
    return helpers.lane_from_status(
        name,
        doc,
        status=status,
        summary_status=str(payload.get("status", "unknown")),
        blockers=blockers,
        findings=findings,
    )
