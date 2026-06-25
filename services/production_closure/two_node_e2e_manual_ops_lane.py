from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

STATUS_PASS = "PASS"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

MANUAL_OPS_SCHEMA = "nhms.two_node_e2e.manual_ops.v1"
MANUAL_OPS_DOCUMENT_CANDIDATES = (
    "manual-ops/summary.json",
    "manual-ops/evidence.json",
)
MANUAL_OPS_REQUIRED_DISPLAY_ACTIONS = frozenset({"retry", "cancel"})
MANUAL_OPS_MANUAL_ACTION_ERROR_CODE = "CONTROL_PLANE_MANUAL_ACTION_REQUIRED"
MANUAL_OPS_RESPONSE_REDACTION_KEYS = ("body_redacted", "redacted", "sensitive_values_redacted")
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
MANUAL_OPS_LANE_OWNER = "services.production_closure.two_node_e2e_manual_ops_lane"
MANUAL_OPS_LANE_VERIFICATION = 'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "manual_ops"'
MANUAL_OPS_LANE_GUARD_SYMBOLS = (
    "MANUAL_OPS_SCHEMA",
    "MANUAL_OPS_DOCUMENT_CANDIDATES",
    "MANUAL_OPS_REQUIRED_DISPLAY_ACTIONS",
    "MANUAL_OPS_MANUAL_ACTION_ERROR_CODE",
    "MANUAL_OPS_RESPONSE_REDACTION_KEYS",
    "MANUAL_OPS_SIDE_EFFECT_CATEGORIES",
    "MANUAL_OPS_LANE_OWNER",
    "MANUAL_OPS_LANE_VERIFICATION",
    "MANUAL_OPS_LANE_BLOCKER_NAMESPACES",
    "ManualOpsLaneEvaluationHelpers",
    "evaluate_manual_ops_lane",
    "manual_action_name",
    "manual_action_outcome_status",
    "_ManualOpsLaneEvaluator",
)
MANUAL_OPS_LANE_BLOCKER_NAMESPACES = (
    "TWO_NODE_E2E_MANUAL_OPS_",
    "TWO_NODE_E2E_STRICT_IDENTITY_",
    "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_",
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


class IdentityMatchStatus(Protocol):
    def __call__(
        self,
        source: str,
        record: Mapping[str, Any],
        strict_identities: Mapping[str, Mapping[str, Any]],
        *,
        require_job_id: bool = False,
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]: ...


class ReadBytesLimitedNoFollow(Protocol):
    def __call__(self, path: Path, *, max_bytes: int, containment_root: Path) -> bytes: ...


class EnsureBoundedEvidenceValue(Protocol):
    def __call__(self, value: Any, *, path: Path) -> None: ...


@dataclass(frozen=True)
class ManualOpsLaneEvaluationHelpers(Generic[LaneResultT]):
    missing_lane: MissingLaneAdapter[LaneResultT]
    lane_from_status: LaneFromStatusAdapter[LaneResultT]
    normalized_status: NormalizedStatus
    blocker: BlockerFactory
    finding: FindingFactory
    stale_lane_blockers: Callable[[Mapping[str, Any]], list[dict[str, Any]]]
    current_run_blockers: CurrentRunBlockers
    identity_match_status: IdentityMatchStatus
    with_context: Callable[..., dict[str, Any]]
    record_identity: Callable[[Mapping[str, Any]], dict[str, Any]]
    source_name: Callable[[Any], str | None]
    identity_value: Callable[[Mapping[str, Any], str], str | None]
    strict_identity_value_matches: Callable[[str, Any, Any], bool]
    explicit_bundle_run_ids: Callable[[Mapping[str, Any]], list[tuple[str, Any]]]
    explicit_bundle_run_ids_from_value: Callable[[Any], list[tuple[str, Any]]]
    approved_artifact_path: Callable[[str], Path]
    approved_artifact_containment_root: Callable[[Path], Path]
    path_is_relative_to: Callable[[Path, Path], bool]
    public_path: Callable[[Path], str]
    read_bytes_limited_no_follow: ReadBytesLimitedNoFollow
    ensure_bounded_evidence_value: EnsureBoundedEvidenceValue
    evidence_error_type: type[Exception]
    safe_filesystem_error_type: type[Exception]
    current_evidence_run_id_keys: tuple[str, ...]
    strict_identity_fields: tuple[str, ...]
    max_evidence_payload_bytes: int


def evaluate_manual_ops_lane(
    doc: EvidenceDocumentLike | None,
    *,
    declared_sources: tuple[str, ...],
    strict_identities: Mapping[str, Mapping[str, Any]],
    evidence_run_id: str,
    helpers: ManualOpsLaneEvaluationHelpers[LaneResultT],
) -> LaneResultT:
    evaluator = _ManualOpsLaneEvaluator(
        doc,
        declared_sources=declared_sources,
        strict_identities=strict_identities,
        evidence_run_id=evidence_run_id,
        helpers=helpers,
    )
    return evaluator.evaluate()


def manual_action_name(action: Mapping[str, Any]) -> str:
    raw = str(action.get("action") or action.get("name") or action.get("path") or "").lower()
    if "retry" in raw:
        return "retry"
    if "cancel" in raw:
        return "cancel"
    return raw.strip()


def manual_action_outcome_status(action: Mapping[str, Any]) -> str:
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


class _ManualOpsLaneEvaluator(Generic[LaneResultT]):
    def __init__(
        self,
        doc: EvidenceDocumentLike | None,
        *,
        declared_sources: tuple[str, ...],
        strict_identities: Mapping[str, Mapping[str, Any]],
        evidence_run_id: str,
        helpers: ManualOpsLaneEvaluationHelpers[LaneResultT],
    ) -> None:
        self.doc = doc
        self.declared_sources = declared_sources
        self.strict_identities = strict_identities
        self.evidence_run_id = evidence_run_id
        self.helpers = helpers

    def evaluate(self) -> LaneResultT:
        if self.doc is None:
            return self.helpers.missing_lane("manual_ops", "TWO_NODE_E2E_MANUAL_OPS_EVIDENCE_MISSING")
        payload = self.doc.payload
        status = self.helpers.normalized_status(payload.get("status"))
        blockers = list(self.helpers.stale_lane_blockers(payload))
        findings: list[dict[str, Any]] = []
        display_actions = _first_mapping_value(
            payload,
            ("display_actions", "display_action_probes", "readonly_actions"),
        )
        stable_27_actions: set[str] = set()
        observed_27_actions: set[str] = set()
        self._evaluate_display_actions(
            display_actions,
            blockers=blockers,
            findings=findings,
            stable_27_actions=stable_27_actions,
            observed_27_actions=observed_27_actions,
        )
        receipts = _first_mapping_value(payload, ("control_receipts", "retry_cancel_receipts", "receipts"))
        actual_22_receipt_count, actual_22_receipt_sources = self._evaluate_receipts(
            receipts,
            blockers=blockers,
            findings=findings,
        )
        if status == STATUS_PASS:
            blockers.extend(
                self.helpers.current_run_blockers(payload, self.evidence_run_id, lane_name="manual_ops")
            )
            blockers.extend(
                self._manual_ops_contract_blockers(
                    payload,
                    display_actions,
                    receipts,
                )
            )
            blockers.extend(self._manual_ops_operator_auth_blockers(payload))
            if not isinstance(display_actions, list) or not display_actions:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_ACTIONS_MISSING",
                        "Manual ops evidence must include 27 display retry/cancel fail-closed probes.",
                    )
                )
            missing_display_actions = sorted(MANUAL_OPS_REQUIRED_DISPLAY_ACTIONS - stable_27_actions)
            if missing_display_actions:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_MANUAL_OPS_27_RETRY_CANCEL_MISSING",
                        "Manual ops PASS requires stable 27 retry and cancel manual-action probes.",
                        missing_actions=missing_display_actions,
                        observed_27_actions=sorted(observed_27_actions),
                    )
                )
            if isinstance(receipts, list):
                if actual_22_receipt_count == 0:
                    blockers.append(
                        self.helpers.blocker(
                            "TWO_NODE_E2E_MANUAL_OPS_22_RECEIPT_MISSING",
                            "Manual ops PASS requires actual retry/cancel receipt evidence produced by node 22.",
                        )
                    )
                else:
                    missing_receipt_sources = sorted(
                        source for source in self.declared_sources if source not in actual_22_receipt_sources
                    )
                    if missing_receipt_sources:
                        blockers.append(
                            self.helpers.blocker(
                                "TWO_NODE_E2E_MANUAL_OPS_22_RECEIPT_SOURCE_COVERAGE_MISSING",
                                "Manual ops PASS requires actual node 22 receipt strict identity coverage for every "
                                "declared source.",
                                missing_sources=missing_receipt_sources,
                                observed_sources=sorted(actual_22_receipt_sources),
                            )
                        )
            elif receipts is None:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_MANUAL_OPS_RECEIPTS_MISSING",
                        "Manual ops PASS requires explicit receipt evidence or an empty receipt list.",
                    )
                )
        if findings:
            status = STATUS_FAIL
        elif status == STATUS_PASS and blockers:
            status = STATUS_BLOCKED
        return self.helpers.lane_from_status(
            "manual_ops",
            self.doc,
            status=status,
            summary_status=str(payload.get("status", "unknown")),
            blockers=blockers,
            findings=findings,
        )

    def _evaluate_display_actions(
        self,
        display_actions: Any,
        *,
        blockers: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        stable_27_actions: set[str],
        observed_27_actions: set[str],
    ) -> None:
        if not isinstance(display_actions, list):
            return
        for action in display_actions:
            if not isinstance(action, Mapping):
                continue
            if _node_number(action) != "27":
                continue
            action_name = manual_action_name(action)
            if action_name in MANUAL_OPS_REQUIRED_DISPLAY_ACTIONS:
                observed_27_actions.add(action_name)
            side_effect_findings, side_effect_blockers = self._manual_action_side_effect_issues(action)
            findings.extend(side_effect_findings)
            blockers.extend(side_effect_blockers)
            outcome_status = manual_action_outcome_status(action)
            if outcome_status == STATUS_PASS and action_name in MANUAL_OPS_REQUIRED_DISPLAY_ACTIONS:
                stable_27_actions.add(action_name)
            elif outcome_status == STATUS_BLOCKED:
                blockers.append(
                    self.helpers.blocker(
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
                    self.helpers.finding(
                        "TWO_NODE_E2E_MANUAL_OPS_27_NOT_FAIL_CLOSED",
                        "27 display retry/cancel evidence did not fail closed as manual action.",
                        action=action.get("action") or action.get("name"),
                        outcome=_manual_action_outcome_text(action),
                    )
                )

    def _evaluate_receipts(
        self,
        receipts: Any,
        *,
        blockers: list[dict[str, Any]],
        findings: list[dict[str, Any]],
    ) -> tuple[int, set[str]]:
        actual_22_receipt_count = 0
        actual_22_receipt_sources: set[str] = set()
        if not isinstance(receipts, list):
            return actual_22_receipt_count, actual_22_receipt_sources
        assert self.doc is not None
        run_dir = self.doc.path.parent.parent
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            producer = _node_number(receipt) or str(receipt.get("producer") or receipt.get("producer_role") or "")
            if _is_actual_control_receipt(receipt) and producer != "22":
                findings.append(
                    self.helpers.finding(
                        "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PRODUCED_BY_27",
                        "Actual retry/cancel receipts must be produced by node 22.",
                        producer=producer,
                        action=receipt.get("action"),
                    )
                )
            elif _is_actual_control_receipt(receipt) and producer == "22":
                actual_22_receipt_count += 1
            if _is_actual_control_receipt(receipt) and producer == "22":
                receipt_identity = self.helpers.record_identity(receipt)
                source = self.helpers.source_name(
                    receipt_identity.get("source") or receipt_identity.get("source_id")
                )
                if not source:
                    blockers.append(
                        self.helpers.blocker(
                            "TWO_NODE_E2E_MANUAL_OPS_22_RECEIPT_IDENTITY_MISSING",
                            "Actual node 22 retry/cancel receipt must include strict source identity.",
                            action=receipt.get("action"),
                        )
                    )
                    continue
                if source not in self.strict_identities:
                    blockers.append(
                        self.helpers.blocker(
                            "TWO_NODE_E2E_MANUAL_OPS_22_RECEIPT_SOURCE_UNDECLARED",
                            "Actual node 22 retry/cancel receipt source is not in strict identity scope.",
                            source=source,
                            action=receipt.get("action"),
                        )
                    )
                    continue
                actual_22_receipt_sources.add(source)
                _, identity_findings, identity_blockers = self.helpers.identity_match_status(
                    source,
                    receipt,
                    self.strict_identities,
                    require_job_id=False,
                )
                findings.extend(
                    self.helpers.with_context(item, lane="manual_ops", source=source)
                    for item in identity_findings
                )
                blockers.extend(
                    self.helpers.with_context(item, lane="manual_ops", source=source)
                    for item in identity_blockers
                )
                provenance_blockers = self._manual_ops_receipt_provenance_blockers(
                    receipt,
                    source=source,
                    run_dir=run_dir,
                    receipt_record=receipt,
                )
                blockers.extend(
                    self.helpers.with_context(item, lane="manual_ops", source=source)
                    for item in provenance_blockers
                )
        return actual_22_receipt_count, actual_22_receipt_sources

    def _manual_action_side_effect_issues(
        self,
        action: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        findings: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        action_name = manual_action_name(action)
        for category, keys in MANUAL_OPS_SIDE_EFFECT_CATEGORIES.items():
            observed = [(key, action.get(key)) for key in keys if isinstance(action.get(key), bool)]
            if not observed:
                blockers.append(
                    self.helpers.blocker(
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
                    self.helpers.finding(
                        "TWO_NODE_E2E_MANUAL_OPS_27_MUTATION",
                        "27 display retry/cancel evidence executed or wrote a control action.",
                        action=action_name,
                        side_effect=category,
                        fields=true_fields,
                    )
                )
        return findings, blockers

    def _manual_ops_contract_blockers(
        self,
        payload: Mapping[str, Any],
        display_actions: Any,
        receipts: Any,
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        if payload.get("schema") != MANUAL_OPS_SCHEMA:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_SCHEMA_MISSING",
                    "Manual ops PASS requires the accepted manual ops evidence schema, not boolean assertions.",
                    expected_schema=MANUAL_OPS_SCHEMA,
                    schema=payload.get("schema"),
                )
            )
        if not isinstance(display_actions, list):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_EVIDENCE_MISSING",
                    "Manual ops PASS requires 27 display retry/cancel response evidence.",
                )
            )
        elif any(not isinstance(action, Mapping) for action in display_actions):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_EVIDENCE_MISSING",
                    "Manual ops 27 actions must include metadata-only response evidence.",
                )
            )
        else:
            blockers.extend(self._manual_ops_display_response_evidence_blockers(display_actions))
        no_side_effect = payload.get("no_side_effect_proof")
        if not isinstance(no_side_effect, Mapping) or no_side_effect.get("node") not in {"27", 27}:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_NO_SIDE_EFFECT_PROOF_MISSING",
                    "Manual ops PASS requires node 27 no-side-effect proof.",
                )
            )
        else:
            for key in ("db_writes", "gateway_calls", "control_receipts_created"):
                if no_side_effect.get(key) is not False:
                    blockers.append(
                        self.helpers.blocker(
                            "TWO_NODE_E2E_MANUAL_OPS_NO_SIDE_EFFECT_PROOF_MISSING",
                            "Manual ops node 27 no-side-effect proof must explicitly record false side effects.",
                            side_effect=key,
                        )
                    )
        if not isinstance(receipts, list):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_MISSING",
                    "Manual ops PASS requires node 22 receipt provenance for declared sources.",
                )
            )
        return blockers

    def _manual_ops_display_response_evidence_blockers(
        self,
        display_actions: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        for action in display_actions:
            if _node_number(action) != "27":
                continue
            action_name = manual_action_name(action)
            if action_name not in MANUAL_OPS_REQUIRED_DISPLAY_ACTIONS:
                continue
            if manual_action_outcome_status(action) != STATUS_PASS:
                continue
            blockers.extend(
                self._manual_ops_single_response_evidence_blockers(
                    action,
                    action_name=action_name,
                )
            )
        return blockers

    def _manual_ops_single_response_evidence_blockers(
        self,
        action: Mapping[str, Any],
        *,
        action_name: str,
    ) -> list[dict[str, Any]]:
        if "response_evidence" not in action:
            return [
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_EVIDENCE_MISSING",
                    "Manual ops 27 retry/cancel actions must include response_evidence.",
                    action=action_name,
                )
            ]

        response_evidence = action.get("response_evidence")
        if not isinstance(response_evidence, Mapping) or not response_evidence:
            return [
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_EVIDENCE_INVALID",
                    "Manual ops response_evidence must be a non-empty metadata mapping.",
                    action=action_name,
                    observed_type=type(response_evidence).__name__,
                )
            ]

        blockers: list[dict[str, Any]] = []
        status_values = [
            (key, response_evidence.get(key))
            for key in ("http_status", "status_code")
            if key in response_evidence
        ]
        if not status_values or any(str(value).strip() != "409" for _, value in status_values):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_STATUS_INVALID",
                    "Manual ops response_evidence must prove a 409 manual-action response.",
                    action=action_name,
                    http_status=response_evidence.get("http_status"),
                    status_code=response_evidence.get("status_code"),
                )
            )
        if response_evidence.get("error_code") != MANUAL_OPS_MANUAL_ACTION_ERROR_CODE:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_ERROR_CODE_INVALID",
                    "Manual ops response_evidence must prove CONTROL_PLANE_MANUAL_ACTION_REQUIRED.",
                    action=action_name,
                    observed_error_code=response_evidence.get("error_code"),
                )
            )
        if not any(response_evidence.get(key) is True for key in MANUAL_OPS_RESPONSE_REDACTION_KEYS):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_REDACTION_MISSING",
                    "Manual ops response_evidence must be redacted metadata only.",
                    action=action_name,
                )
            )
        response_action = response_evidence.get("action")
        if response_action is None:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_BINDING_MISSING",
                    "Manual ops response_evidence must include action binding.",
                    action=action_name,
                )
            )
        elif manual_action_name({"action": response_action}) != action_name:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_BINDING_MISMATCH",
                    "Manual ops response_evidence action binding must match the display action.",
                    action=action_name,
                    response_action=response_action,
                )
            )
        response_source = (
            response_evidence.get("source") if "source" in response_evidence else response_evidence.get("source_id")
        )
        action_source = self.helpers.source_name(action.get("source") or action.get("source_id"))
        declared_source_set = set(self.declared_sources)
        if action_source is None:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_BINDING_MISSING",
                    "Manual ops 27 retry/cancel actions must include source binding.",
                    action=action_name,
                )
            )
        elif action_source not in declared_source_set:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_SOURCE_UNDECLARED",
                    "Manual ops 27 retry/cancel action source is not in strict identity scope.",
                    action=action_name,
                    source=action_source,
                    declared_sources=list(self.declared_sources),
                )
            )
        if response_source is None:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_BINDING_MISSING",
                    "Manual ops response_evidence must include source binding for source-scoped display actions.",
                    action=action_name,
                    expected_source=action_source,
                )
            )
        else:
            bound_source = self.helpers.source_name(response_source)
            if action_source is None or bound_source != action_source:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_BINDING_MISMATCH",
                        "Manual ops response_evidence source binding must match the display action source.",
                        action=action_name,
                        source=bound_source,
                        expected_source=action_source,
                    )
                )
            elif bound_source not in declared_source_set:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_SOURCE_UNDECLARED",
                        "Manual ops response_evidence source is not in strict identity scope.",
                        action=action_name,
                        source=bound_source,
                        declared_sources=list(self.declared_sources),
                    )
                )
        run_bindings = [
            (key, response_evidence.get(key))
            for key in self.helpers.current_evidence_run_id_keys
            if key in response_evidence
        ]
        if not run_bindings:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_RUN_ID_MISSING",
                    "Manual ops response_evidence must include current evidence run binding.",
                    action=action_name,
                    accepted_fields=list(self.helpers.current_evidence_run_id_keys),
                )
            )
        for key, value in run_bindings:
            if str(value or "").strip() != self.evidence_run_id:
                blockers.append(
                    self.helpers.blocker(
                        "TWO_NODE_E2E_MANUAL_OPS_DISPLAY_RESPONSE_RUN_ID_MISMATCH",
                        "Manual ops response_evidence belongs to a different evidence run.",
                        action=action_name,
                        key=key,
                        evidence_run_id=value,
                        expected_evidence_run_id=self.evidence_run_id,
                    )
                )
        return blockers

    def _manual_ops_receipt_provenance_blockers(
        self,
        receipt: Mapping[str, Any],
        *,
        source: str,
        run_dir: Path,
        receipt_record: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        provenance = receipt.get("provenance")
        if not isinstance(provenance, Mapping) or not provenance:
            return [
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_MISSING",
                    "Actual node 22 manual ops receipts must include producer provenance.",
                    action=receipt.get("action"),
                )
            ]
        blockers: list[dict[str, Any]] = []
        raw_node = provenance.get("producer_node") or provenance.get("node") or provenance.get("host_node")
        if raw_node is None or _node_number(provenance) != "22":
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_PRODUCER_INVALID",
                    "Manual ops receipt provenance must identify node 22 as the producer.",
                    producer_node=raw_node,
                )
            )
        producer_role = str(provenance.get("producer_role") or provenance.get("service_role") or "").strip()
        if producer_role != "compute_control":
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_PRODUCER_INVALID",
                    "Manual ops receipt provenance must identify the compute_control producer role.",
                    producer_role=producer_role,
                )
            )
        if not any(str(provenance.get(key) or "").strip() for key in ("receipt_id", "command_id")):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_ID_MISSING",
                    "Manual ops receipt provenance must include a receipt_id or command_id.",
                )
            )
        provenance_source = self.helpers.source_name(provenance.get("source") or provenance.get("source_id"))
        if not provenance_source:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_SOURCE_MISSING",
                    "Manual ops receipt provenance must include the strict source.",
                )
            )
        elif provenance_source != source:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_SOURCE_MISMATCH",
                    "Manual ops receipt provenance source must match the receipt strict source.",
                    source=provenance_source,
                    expected_source=source,
                )
            )
        if provenance.get("redacted") is not True:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_UNREDACTED",
                    "Manual ops receipt provenance must be redacted metadata only.",
                )
            )
        explicit_ids = self.helpers.explicit_bundle_run_ids(provenance)
        if not explicit_ids:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_RUN_ID_MISSING",
                    "Manual ops receipt provenance must bind to the current evidence run.",
                    expected_evidence_run_id=self.evidence_run_id,
                )
            )
        else:
            for key, value in explicit_ids:
                if str(value) != self.evidence_run_id:
                    blockers.append(
                        self.helpers.blocker(
                            "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_PROVENANCE_RUN_ID_MISMATCH",
                            "Manual ops receipt provenance belongs to a different evidence run.",
                            key=key,
                            evidence_run_id=value,
                            expected_evidence_run_id=self.evidence_run_id,
                        )
                    )
        blockers.extend(
            self._manual_ops_receipt_artifact_blockers(
                provenance,
                run_dir=run_dir,
                source=source,
                action=manual_action_name(receipt),
                receipt_id=str(provenance.get("receipt_id") or provenance.get("command_id") or "").strip() or None,
                receipt_record=receipt_record or receipt,
            )
        )
        return blockers

    def _manual_ops_receipt_artifact_blockers(
        self,
        provenance: Mapping[str, Any],
        *,
        run_dir: Path,
        source: str,
        action: str,
        receipt_id: str | None,
        receipt_record: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        raw_path = provenance.get("artifact_path") or provenance.get("path")
        raw_sha256 = provenance.get("sha256") or provenance.get("artifact_sha256")
        if raw_path is None and raw_sha256 is None:
            return []
        if not isinstance(raw_path, str) or not raw_path.strip():
            return [
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_PATH_MISSING",
                    "Manual ops receipt artifact provenance must include a path.",
                )
            ]
        if not isinstance(raw_sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", raw_sha256.strip()):
            return [
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_SHA_MISSING",
                    "Manual ops receipt artifact provenance must include a sha256 digest.",
                    path=raw_path,
                )
            ]
        evidence_error_type = self.helpers.evidence_error_type
        try:
            path = self.helpers.approved_artifact_path(raw_path)
        except evidence_error_type:
            return [
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_OUTSIDE_APPROVED_ROOT",
                    "Manual ops receipt artifact path must stay under approved evidence roots.",
                    path=raw_path,
                )
            ]
        blockers: list[dict[str, Any]] = []
        explicit_ids = self.helpers.explicit_bundle_run_ids(provenance)
        if not self.helpers.path_is_relative_to(path, run_dir) and not (
            explicit_ids and all(str(value) == self.evidence_run_id for _, value in explicit_ids)
        ):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_STALE_OR_UNSCOPED",
                    "Manual ops receipt artifact must be in the current run or explicitly bind to it.",
                    path=self.helpers.public_path(path),
                    expected_evidence_run_id=self.evidence_run_id,
                )
            )
        containment_root = self.helpers.approved_artifact_containment_root(path)
        safe_filesystem_error_type = self.helpers.safe_filesystem_error_type
        try:
            content = self.helpers.read_bytes_limited_no_follow(
                path,
                max_bytes=self.helpers.max_evidence_payload_bytes,
                containment_root=containment_root,
            )
        except FileNotFoundError:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_MISSING",
                    "Manual ops receipt artifact file is missing.",
                    path=self.helpers.public_path(path),
                )
            )
            return blockers
        except safe_filesystem_error_type:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_PATH_UNSAFE",
                    "Manual ops receipt artifact path is unsafe.",
                    path=self.helpers.public_path(path),
                )
            )
            return blockers
        if len(content) > self.helpers.max_evidence_payload_bytes:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_TOO_LARGE",
                    "Manual ops receipt artifact file is too large.",
                    path=self.helpers.public_path(path),
                )
            )
            return blockers
        if hashlib.sha256(content).hexdigest() != raw_sha256.lower():
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_HASH_MISMATCH",
                    "Manual ops receipt artifact sha256 does not match file content.",
                    path=self.helpers.public_path(path),
                )
            )
        try:
            payload = json.loads(content.decode("utf-8"))
            self.helpers.ensure_bounded_evidence_value(payload, path=path)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, evidence_error_type):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_JSON_INVALID",
                    "Manual ops receipt artifact must be bounded valid JSON.",
                    path=self.helpers.public_path(path),
                )
            )
            return blockers
        if not isinstance(payload, Mapping):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_JSON_INVALID",
                    "Manual ops receipt artifact JSON must be an object.",
                    path=self.helpers.public_path(path),
                )
            )
            return blockers
        blockers.extend(
            self._manual_ops_receipt_artifact_payload_blockers(
                payload,
                provenance=provenance,
                source=source,
                action=action,
                receipt_id=receipt_id,
                receipt_record=receipt_record,
                path=path,
            )
        )
        return blockers

    def _manual_ops_receipt_artifact_payload_blockers(
        self,
        payload: Mapping[str, Any],
        *,
        provenance: Mapping[str, Any],
        source: str,
        action: str,
        receipt_id: str | None,
        receipt_record: Mapping[str, Any],
        path: Path,
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        schema = payload.get("schema") or payload.get("schema_version")
        if not isinstance(schema, str) or "manual_ops.receipt" not in schema:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_SCHEMA_INVALID",
                    "Manual ops receipt artifact must use a receipt evidence schema.",
                    path=self.helpers.public_path(path),
                    schema=schema,
                )
            )
        status = payload.get("status")
        if status is not None and self.helpers.normalized_status(status) != STATUS_PASS:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_STATUS_INVALID",
                    "Manual ops receipt artifact status must be PASS when present.",
                    path=self.helpers.public_path(path),
                    status=status,
                )
            )
        payload_source = self.helpers.source_name(payload.get("source") or payload.get("source_id"))
        if not payload_source:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_SOURCE_MISSING",
                    "Manual ops receipt artifact must include strict source.",
                    path=self.helpers.public_path(path),
                )
            )
        elif payload_source != source:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_SOURCE_MISMATCH",
                    "Manual ops receipt artifact source must match receipt provenance.",
                    path=self.helpers.public_path(path),
                    source=payload_source,
                    expected_source=source,
                )
            )
        payload_action = manual_action_name(payload)
        if action and payload_action and payload_action != action:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_ACTION_MISMATCH",
                    "Manual ops receipt artifact action must match receipt provenance.",
                    path=self.helpers.public_path(path),
                    action=payload_action,
                    expected_action=action,
                )
            )
        if action and not payload_action:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_ACTION_MISSING",
                    "Manual ops receipt artifact must include action binding.",
                    path=self.helpers.public_path(path),
                    expected_action=action,
                )
            )
        payload_receipt_id = str(payload.get("receipt_id") or payload.get("command_id") or "").strip()
        if receipt_id and payload_receipt_id and payload_receipt_id != receipt_id:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_ID_MISMATCH",
                    "Manual ops receipt artifact id must match receipt provenance.",
                    path=self.helpers.public_path(path),
                    receipt_id=payload_receipt_id,
                    expected_receipt_id=receipt_id,
                )
            )
        if receipt_id and not payload_receipt_id:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_ID_MISSING",
                    "Manual ops receipt artifact must include receipt_id or command_id.",
                    path=self.helpers.public_path(path),
                    expected_receipt_id=receipt_id,
                )
            )
        producer_node = _node_number(payload)
        if producer_node != "22":
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_PRODUCER_INVALID",
                    "Manual ops receipt artifact must identify node 22 as producer.",
                    path=self.helpers.public_path(path),
                    producer_node=payload.get("producer_node") or payload.get("node"),
                )
            )
        producer_role = str(payload.get("producer_role") or payload.get("service_role") or "").strip()
        if producer_role != "compute_control":
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_PRODUCER_INVALID",
                    "Manual ops receipt artifact must identify compute_control producer role.",
                    path=self.helpers.public_path(path),
                    producer_role=producer_role,
                )
            )
        explicit_ids = self.helpers.explicit_bundle_run_ids_from_value(payload)
        if not explicit_ids:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_RUN_ID_MISSING",
                    "Manual ops receipt artifact must bind to the current evidence run.",
                    path=self.helpers.public_path(path),
                    expected_evidence_run_id=self.evidence_run_id,
                )
            )
        else:
            for key, value in explicit_ids:
                if str(value) != self.evidence_run_id:
                    blockers.append(
                        self.helpers.blocker(
                            "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_RUN_ID_MISMATCH",
                            "Manual ops receipt artifact belongs to a different evidence run.",
                            path=self.helpers.public_path(path),
                            key=key,
                            evidence_run_id=value,
                            expected_evidence_run_id=self.evidence_run_id,
                        )
                    )
        receipt_identity = self.helpers.record_identity(receipt_record)
        payload_identity = self.helpers.record_identity(payload)
        for identity_field in self.helpers.strict_identity_fields:
            receipt_value = self.helpers.identity_value(receipt_identity, identity_field)
            payload_value = self.helpers.identity_value(payload_identity, identity_field)
            if identity_field == "source":
                if payload_source and not payload_value:
                    payload_value = payload_source
                if source and not receipt_value:
                    receipt_value = source
            if not receipt_value or not payload_value:
                if identity_field in {"source", "run_id"}:
                    blockers.append(
                        self.helpers.blocker(
                            "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_IDENTITY_INCOMPLETE",
                            "Manual ops receipt artifact must include current strict identity.",
                            path=self.helpers.public_path(path),
                            field=identity_field,
                        )
                    )
                continue
            if identity_field == "source":
                if self.helpers.strict_identity_value_matches(identity_field, payload_value, receipt_value):
                    continue
            elif self.helpers.strict_identity_value_matches(identity_field, payload_value, receipt_value):
                continue
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_IDENTITY_MISMATCH",
                    "Manual ops receipt artifact identity must match receipt/provenance identity.",
                    path=self.helpers.public_path(path),
                    field=identity_field,
                    observed=payload_value,
                    expected=receipt_value,
                )
            )
        if provenance.get("redacted") is True and payload.get("redacted") is False:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_RECEIPT_ARTIFACT_UNREDACTED",
                    "Manual ops receipt artifact must not contradict redacted provenance.",
                    path=self.helpers.public_path(path),
                )
            )
        return blockers

    def _manual_ops_operator_auth_blockers(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        auth = payload.get("production_operator_auth")
        if not isinstance(auth, Mapping):
            return [
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_PRODUCTION_AUTH_MISSING",
                    "Manual ops PASS requires metadata-only production operator auth evidence.",
                )
            ]
        blockers: list[dict[str, Any]] = []
        if auth.get("status") != STATUS_PASS:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_PRODUCTION_AUTH_MISSING",
                    "Production operator auth evidence must be PASS.",
                    auth_status=auth.get("status"),
                )
            )
        if auth.get("redacted") is not True or auth.get("secret_material_written") is not False:
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_PRODUCTION_AUTH_UNREDACTED",
                    "Production operator auth evidence must be redacted metadata only.",
                )
            )
        if not any(auth.get(key) for key in ("auth_source", "header_source", "token_source", "principal")):
            blockers.append(
                self.helpers.blocker(
                    "TWO_NODE_E2E_MANUAL_OPS_PRODUCTION_AUTH_MISSING",
                    "Production operator auth evidence must include redacted source metadata.",
                )
            )
        return blockers


def _first_mapping_value(payload: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


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
