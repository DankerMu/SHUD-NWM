from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from packages.common.source_identity import normalize_source_id
from services.orchestrator.chain_config import scenario_for_source
from services.orchestrator.reservation import slurm_comment_for
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

FORECAST_COHORT_STAGE_ALIASES = frozenset({"forecast", "run_shud_forecast", "run_shud_forecast_array"})
MAX_FORECAST_COHORT_MEMBERS = 256
MAX_ACCEPTED_SUBMIT_TEXT_LENGTH = 256
ACCEPTED_SUBMIT_CONTRACT_VERSION = "nhms.accepted_submit.v1"
ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD = "accepted_submit_contract_version"

ACCEPTED_SUBMIT_OUTCOMES = frozenset({"accepted", "submit_result_ambiguous", "rejected"})
ACCEPTED_RECONCILIATION_DECISIONS = frozenset(
    {
        "accounting_unavailable",
        "absence_deferred",
        "absence_retry_permitted",
        "identity_mismatch_blocked",
        "matched_bound",
        "multiple_matches_blocked",
    }
)
ACCEPTED_RECONCILIATION_REASON_CLASSES = frozenset(
    {
        "accounting_authority_unproven",
        "bounded_output_bytes_saturated",
        "bounded_output_rows_saturated",
        "coverage_incomplete",
        "process_unavailable",
    }
)
ACCEPTED_RESTART_STAGES = frozenset({"forecast", "state_save_qc"})
ACCEPTED_PROJECTION_OUTCOMES = frozenset({"succeeded", "failed", "unverified"})
ACCEPTED_PROJECTION_FIELDS = frozenset(
    {
        "array_task_id",
        "array_task_outcome",
        "candidate_id",
        "model_id",
        "native_shud_resubmitted",
        "restart_stage",
        "run_id",
    }
)

_MEMBER_FIELDS = (
    "array_task_id",
    "candidate_id",
    "run_id",
    "model_id",
    "basin_id",
    "scenario_id",
    "restart_stage",
)


class AcceptedSubmitEvidenceError(ValueError):
    """Typed canonical accepted-submit evidence validation failure."""

    def __init__(self, reason: str, *, field: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.field = field


@dataclass(frozen=True)
class AcceptedSubmitCommitResult:
    """Typed result for one attempt-scoped accepted-submit journal mutation."""

    outcome: str
    row: Mapping[str, Any] | None = None

    @property
    def committed(self) -> bool:
        return self.outcome in {"applied", "idempotent"}

    @property
    def wrote(self) -> bool:
        return self.outcome == "applied"


@dataclass(frozen=True)
class AcceptedSubmitTransition:
    """One complete accepted-submit evidence transition.

    Every transition replaces the accounting tuple as a unit. A ``None`` tuple
    therefore means CLEAR, never "leave the previous attempt unchanged".
    """

    submit_outcome: str | None
    reconciliation_source: str | None = None
    reconciliation_decision: str | None = None
    matched_slurm_job_id: str | None = None
    reconciliation_reason_class: str | None = None
    status: str | None = None

    def __post_init__(self) -> None:
        if self.submit_outcome is None:
            if (
                self.reconciliation_source is not None
                or self.reconciliation_decision is not None
                or self.matched_slurm_job_id is not None
                or self.reconciliation_reason_class is not None
                or self.status != "reserved"
            ):
                raise ValueError("pre-outcome transition must begin one reserved attempt")
            return
        if self.submit_outcome not in ACCEPTED_SUBMIT_OUTCOMES:
            raise ValueError("invalid accepted-submit outcome transition")
        decision = self.reconciliation_decision
        if decision is None:
            if (
                self.reconciliation_source is not None
                or self.matched_slurm_job_id is not None
                or self.reconciliation_reason_class is not None
            ):
                raise ValueError("accounting tuple must be cleared together")
            return
        if decision not in ACCEPTED_RECONCILIATION_DECISIONS:
            raise ValueError("invalid accepted-submit accounting decision")
        if self.reconciliation_source != "slurm_exact_comment":
            raise ValueError("accounting transition requires exact-comment source")
        if decision == "matched_bound":
            if not isinstance(self.matched_slurm_job_id, str) or not self.matched_slurm_job_id.isdigit():
                raise ValueError("matched accounting transition requires a Slurm job id")
        elif self.matched_slurm_job_id is not None:
            raise ValueError("blocked accounting transition cannot carry a matched Slurm job id")
        reason_class = self.reconciliation_reason_class
        if reason_class is not None:
            if decision != "accounting_unavailable":
                raise ValueError("accounting reason class requires accounting_unavailable")
            if reason_class not in ACCEPTED_RECONCILIATION_REASON_CLASSES:
                raise ValueError("invalid accepted-submit accounting reason class")

    @classmethod
    def begin_attempt(cls) -> AcceptedSubmitTransition:
        return cls(None, status="reserved")

    @classmethod
    def timeout(cls, *, status: str = "reserved") -> AcceptedSubmitTransition:
        return cls("submit_result_ambiguous", status=status)

    @classmethod
    def accepted(cls, *, status: str | None = None) -> AcceptedSubmitTransition:
        return cls("accepted", status=status)

    @classmethod
    def rejected(cls, *, status: str = "submission_failed") -> AcceptedSubmitTransition:
        return cls("rejected", status=status)

    @classmethod
    def accounting(
        cls,
        decision: str,
        *,
        submit_outcome: str,
        matched_slurm_job_id: str | None = None,
        reconciliation_reason_class: str | None = None,
        status: str | None = None,
    ) -> AcceptedSubmitTransition:
        return cls(
            submit_outcome=submit_outcome,
            reconciliation_source="slurm_exact_comment",
            reconciliation_decision=decision,
            matched_slurm_job_id=matched_slurm_job_id,
            reconciliation_reason_class=reconciliation_reason_class,
            status=status,
        )


def apply_accepted_submit_transition(
    row: Mapping[str, Any], transition: AcceptedSubmitTransition
) -> dict[str, Any]:
    """Apply a typed transition, replacing the accounting tuple atomically."""

    transitioned = dict(row)
    transitioned.update(
        {
            "submit_outcome": transition.submit_outcome,
            "reconciliation_source": transition.reconciliation_source,
            "reconciliation_decision": transition.reconciliation_decision,
            "matched_slurm_job_id": transition.matched_slurm_job_id,
            "reconciliation_reason_class": transition.reconciliation_reason_class,
        }
    )
    if transition.status is not None:
        transitioned["status"] = transition.status
    return transitioned


def accepted_submit_pipeline_job_model_id(
    *,
    supports_accepted_submit_reconcile: bool,
    stage: Any,
    job_type: Any,
    model_id: str | None,
) -> str | None:
    """Keep canonical forecast cohort masters model-less on every write path."""

    if supports_accepted_submit_reconcile and is_forecast_cohort_stage_name(stage, job_type):
        return None
    return model_id


def accepted_submit_row_kind(row: Mapping[str, Any]) -> str | None:
    """Classify forecast master evidence separately from candidate task rows."""

    if not is_forecast_cohort_stage_name(row.get("stage"), row.get("job_type")):
        return None
    master_markers = (
        "cohort_digest",
        "cohort_members",
        "expected_slurm_account",
        "expected_slurm_user",
        "matched_slurm_job_id",
        "reconciliation_decision",
        "reconciliation_source",
        "slurm_comment",
    )
    if any(row.get(key) not in (None, "", (), []) for key in master_markers):
        return "master"
    run_id = str(row.get("run_id") or "")
    job_id = str(row.get("job_id") or "")
    candidate_id = str(row.get("candidate_id") or "")
    if (
        run_id.startswith("cycle_")
        and job_id.startswith(f"job_{run_id}_forecast")
        and candidate_id == run_id
    ):
        return "master"
    if row.get("model_id") not in (None, "") and (
        type(row.get("array_task_id")) is int or row.get("candidate_id") not in (None, "")
    ):
        return "candidate"
    return None


def accepted_submit_contract_is_current(row: Mapping[str, Any]) -> bool:
    """Return whether a row explicitly opts into the current authority contract.

    Marker-free rows are historical compatibility data. An explicit but
    unknown/malformed marker is corruption and therefore fails closed.
    """

    if ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD not in row:
        return False
    value = row.get(ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD)
    if value != ACCEPTED_SUBMIT_CONTRACT_VERSION:
        raise AcceptedSubmitEvidenceError(
            "file_journal_evidence_enum_invalid",
            field=ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD,
        )
    return True


def normalize_accepted_submit_attempt_anchor(value: Any) -> str:
    """Return one canonical aware-UTC anchor for the current submission attempt."""

    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as error:
            raise AcceptedSubmitEvidenceError(
                "file_journal_evidence_invariant_invalid",
                field="submission_attempt_started_at",
            ) from error
    else:
        raise AcceptedSubmitEvidenceError(
            "file_journal_evidence_invariant_invalid",
            field="submission_attempt_started_at",
        )
    try:
        offset = parsed.utcoffset() if parsed.tzinfo is not None else None
    except (OverflowError, TypeError, ValueError) as error:
        raise AcceptedSubmitEvidenceError(
            "file_journal_evidence_invariant_invalid",
            field="submission_attempt_started_at",
        ) from error
    if offset is None:
        raise AcceptedSubmitEvidenceError(
            "file_journal_evidence_invariant_invalid",
            field="submission_attempt_started_at",
        )
    try:
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, TypeError, ValueError) as error:
        raise AcceptedSubmitEvidenceError(
            "file_journal_evidence_invariant_invalid",
            field="submission_attempt_started_at",
        ) from error


def normalize_accepted_submit_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and validate the one durable accepted-submit master contract.

    Candidate task rows are deliberately outside the cohort evidence contract:
    they may carry ``submit_outcome=accepted`` but never own the master member
    map or reconciliation proof.
    """

    normalized = dict(row)
    if not accepted_submit_contract_is_current(normalized):
        return normalized
    outcome = normalized.get("submit_outcome")
    if outcome is not None and outcome not in ACCEPTED_SUBMIT_OUTCOMES:
        raise AcceptedSubmitEvidenceError("file_journal_evidence_enum_invalid", field="submit_outcome")
    if accepted_submit_row_kind(normalized) != "master":
        return normalized
    if normalized.get("model_id") not in (None, ""):
        raise AcceptedSubmitEvidenceError(
            "file_journal_evidence_invariant_invalid", field="model_id"
        )
    ownership_required = normalized.get("slurm_ownership_required")
    if type(ownership_required) is not bool:
        raise AcceptedSubmitEvidenceError(
            "file_journal_evidence_type_invalid", field="slurm_ownership_required"
        )
    restart_stage = normalized.get("restart_stage")
    if restart_stage not in ACCEPTED_RESTART_STAGES:
        raise AcceptedSubmitEvidenceError("file_journal_evidence_enum_invalid", field="restart_stage")
    native_resubmitted = normalized.get("native_shud_resubmitted")
    if native_resubmitted is not None and type(native_resubmitted) is not bool:
        raise AcceptedSubmitEvidenceError(
            "file_journal_evidence_type_invalid", field="native_shud_resubmitted"
        )
    normalized["submission_attempt_started_at"] = normalize_accepted_submit_attempt_anchor(
        normalized.get("submission_attempt_started_at")
    )

    normalized["candidate_projections"] = normalize_candidate_projections(
        normalized.get("candidate_projections"),
        cohort_members=normalized.get("cohort_members"),
    )
    decision = normalized.get("reconciliation_decision")
    source = normalized.get("reconciliation_source")
    matched_id = normalized.get("matched_slurm_job_id")
    reason_class = normalized.get("reconciliation_reason_class")
    if decision is None:
        if source is not None or matched_id is not None or reason_class is not None:
            raise AcceptedSubmitEvidenceError(
                "file_journal_evidence_invariant_invalid", field="reconciliation_decision"
            )
    else:
        if outcome is None:
            raise AcceptedSubmitEvidenceError(
                "file_journal_evidence_invariant_invalid", field="submit_outcome"
            )
        if decision not in ACCEPTED_RECONCILIATION_DECISIONS:
            raise AcceptedSubmitEvidenceError(
                "file_journal_evidence_enum_invalid", field="reconciliation_decision"
            )
        if source != "slurm_exact_comment":
            raise AcceptedSubmitEvidenceError(
                "file_journal_evidence_enum_invalid", field="reconciliation_source"
            )
        if decision == "matched_bound":
            if not isinstance(matched_id, str) or not matched_id.isdigit():
                raise AcceptedSubmitEvidenceError(
                    "file_journal_evidence_invariant_invalid", field="matched_slurm_job_id"
                )
        elif matched_id is not None:
            raise AcceptedSubmitEvidenceError(
                "file_journal_evidence_invariant_invalid", field="matched_slurm_job_id"
            )
        if reason_class is not None:
            if decision != "accounting_unavailable":
                raise AcceptedSubmitEvidenceError(
                    "file_journal_evidence_invariant_invalid",
                    field="reconciliation_reason_class",
                )
            if reason_class not in ACCEPTED_RECONCILIATION_REASON_CLASSES:
                raise AcceptedSubmitEvidenceError(
                    "file_journal_evidence_enum_invalid",
                    field="reconciliation_reason_class",
                )

    # The pre-Gateway durable reservation is the sole state allowed to omit an
    # outcome. It cannot already contain a decision or task projections.
    if outcome is None and (decision is not None or normalized["candidate_projections"]):
        raise AcceptedSubmitEvidenceError(
            "file_journal_evidence_invariant_invalid", field="submit_outcome"
        )
    if not forecast_cohort_identity_is_valid(normalized):
        raise AcceptedSubmitEvidenceError(
            "file_journal_evidence_invariant_invalid", field="cohort_digest"
        )
    return normalized


def normalize_candidate_projections(
    value: Any,
    *,
    cohort_members: Any,
) -> list[dict[str, Any]]:
    """Return the bounded public projection schema with exact member identity."""

    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise AcceptedSubmitEvidenceError(
            "file_journal_evidence_type_invalid", field="candidate_projections"
        )
    if len(value) > MAX_FORECAST_COHORT_MEMBERS:
        raise AcceptedSubmitEvidenceError(
            "file_journal_evidence_limit_exceeded", field="candidate_projections"
        )
    members = ordered_cohort_members(cohort_members)
    members_by_task = {member.get("array_task_id"): member for member in members}
    normalized: list[dict[str, Any]] = []
    seen_task_ids: set[int] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise AcceptedSubmitEvidenceError(
                "file_journal_evidence_type_invalid", field="candidate_projections"
            )
        extras = set(item) - ACCEPTED_PROJECTION_FIELDS
        if extras:
            raise AcceptedSubmitEvidenceError(
                "file_journal_evidence_field_not_allowed",
                field=f"candidate_projections.{sorted(extras)[0]}",
            )
        task_id = item.get("array_task_id")
        if type(task_id) is not int:
            raise AcceptedSubmitEvidenceError(
                "file_journal_evidence_type_invalid", field="candidate_projections.array_task_id"
            )
        if task_id in seen_task_ids:
            raise AcceptedSubmitEvidenceError(
                "file_journal_evidence_invariant_invalid", field="candidate_projections.array_task_id"
            )
        seen_task_ids.add(task_id)
        projection: dict[str, Any] = {}
        for field_name in ("candidate_id", "run_id", "model_id"):
            field_value = item.get(field_name)
            if not isinstance(field_value, str) or not field_value:
                raise AcceptedSubmitEvidenceError(
                    "file_journal_evidence_required", field=f"candidate_projections.{field_name}"
                )
            if len(field_value) > MAX_ACCEPTED_SUBMIT_TEXT_LENGTH:
                raise AcceptedSubmitEvidenceError(
                    "file_journal_evidence_limit_exceeded", field=f"candidate_projections.{field_name}"
                )
            projection[field_name] = field_value
        projection["array_task_id"] = task_id
        projection["array_task_outcome"] = item.get("array_task_outcome")
        projection["restart_stage"] = item.get("restart_stage")
        projection["native_shud_resubmitted"] = item.get("native_shud_resubmitted")
        if projection["array_task_outcome"] not in ACCEPTED_PROJECTION_OUTCOMES:
            raise AcceptedSubmitEvidenceError(
                "file_journal_evidence_enum_invalid", field="candidate_projections.array_task_outcome"
            )
        if projection["restart_stage"] not in ACCEPTED_RESTART_STAGES:
            raise AcceptedSubmitEvidenceError(
                "file_journal_evidence_enum_invalid", field="candidate_projections.restart_stage"
            )
        if type(projection["native_shud_resubmitted"]) is not bool:
            raise AcceptedSubmitEvidenceError(
                "file_journal_evidence_type_invalid",
                field="candidate_projections.native_shud_resubmitted",
            )
        member = members_by_task.get(task_id)
        if member is None or any(
            projection[field_name] != member.get(field_name)
            for field_name in ("candidate_id", "run_id", "model_id")
        ):
            raise AcceptedSubmitEvidenceError(
                "file_journal_evidence_invariant_invalid", field="candidate_projections.array_task_id"
            )
        normalized.append(projection)
    return normalized


def canonical_forecast_stage(value: Any) -> str | None:
    return "forecast" if str(value or "").strip() in FORECAST_COHORT_STAGE_ALIASES else None


def is_forecast_cohort_stage_name(stage: Any, job_type: Any = None) -> bool:
    stage_text = str(stage or "").strip()
    if stage_text:
        return canonical_forecast_stage(stage_text) is not None
    return canonical_forecast_stage(job_type) is not None


def ordered_cohort_members(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return ()
    return tuple(
        {key: item.get(key) for key in _MEMBER_FIELDS}
        for item in value[:MAX_FORECAST_COHORT_MEMBERS]
        if isinstance(item, Mapping)
    )


def canonical_forecast_cohort_members(
    *, source_id: str, cycle_time: Any, basins: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """Build the one canonical accepted-submit member identity projection."""
    source = normalize_source_id(source_id)
    parsed_cycle = parse_cycle_time(cycle_time)
    compact_cycle = format_cycle_time(parsed_cycle)
    cycle_iso = parsed_cycle.astimezone(UTC).isoformat().replace("+00:00", "Z")
    scenario_id = scenario_for_source(source)
    members: list[dict[str, Any]] = []
    for index, basin in enumerate(basins):
        model_id = str(basin.get("model_id") or "")
        members.append(
            {
                "array_task_id": int(basin.get("task_id", index)),
                "candidate_id": f"{source}:{cycle_iso}:{model_id}:{scenario_id}",
                "run_id": f"fcst_{source.lower()}_{compact_cycle}_{model_id}",
                "model_id": model_id,
                "basin_id": str(basin.get("basin_id") or ""),
                "scenario_id": scenario_id,
                "restart_stage": "forecast",
            }
        )
    return tuple(members)


def forecast_cohort_digest(identity: Mapping[str, Any]) -> str:
    payload = {
        "job_id": str(identity.get("job_id") or ""),
        "run_id": str(identity.get("run_id") or ""),
        "source_id": str(identity.get("source_id") or ""),
        "cycle_id": str(identity.get("cycle_id") or ""),
        "stage": canonical_forecast_stage(identity.get("stage") or identity.get("job_type")),
        "idempotency_key": str(identity.get("idempotency_key") or ""),
        "slurm_comment": str(identity.get("slurm_comment") or ""),
        "cohort_members": ordered_cohort_members(identity.get("cohort_members")),
        "slurm_ownership_required": bool(identity.get("slurm_ownership_required", False)),
        "expected_slurm_user": str(identity.get("expected_slurm_user") or ""),
        "expected_slurm_account": str(identity.get("expected_slurm_account") or ""),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def forecast_cohort_identity_is_valid(identity: Mapping[str, Any]) -> bool:
    if not is_forecast_cohort_stage_name(identity.get("stage"), identity.get("job_type")):
        return False
    try:
        source_id = normalize_source_id(str(identity.get("source_id") or ""))
        cycle_id = str(identity.get("cycle_id") or "")
        cycle_time = parse_cycle_time(cycle_id.split("_", maxsplit=1)[1])
    except (IndexError, TypeError, ValueError):
        return False
    if cycle_id != cycle_id_for(source_id, cycle_time):
        return False

    run_id = str(identity.get("run_id") or "")
    expected_run_prefix = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
    if run_id != expected_run_prefix and not run_id.startswith(f"{expected_run_prefix}_"):
        return False
    job_id = str(identity.get("job_id") or "")
    expected_job_id = f"job_{run_id}_forecast"
    if job_id == expected_job_id:
        expected_key = f"{run_id}:forecast"
    elif job_id.startswith(f"{expected_job_id}_"):
        retry_suffix = job_id.removeprefix(f"{expected_job_id}_")
        if not retry_suffix:
            return False
        expected_key = f"{run_id}:forecast:{retry_suffix}"
    else:
        return False
    key = str(identity.get("idempotency_key") or "")
    if key != expected_key or str(identity.get("slurm_comment") or "") != slurm_comment_for(key):
        return False

    raw_members = identity.get("cohort_members")
    members = ordered_cohort_members(raw_members)
    if (
        not isinstance(raw_members, Sequence)
        or isinstance(raw_members, str | bytes | bytearray)
        or not members
        or len(members) != len(raw_members)
    ):
        return False

    scenario_id = scenario_for_source(source_id)
    cycle_iso = cycle_time.astimezone(UTC).isoformat().replace("+00:00", "Z")
    compact_cycle = format_cycle_time(cycle_time)
    unique_fields = {field: set() for field in ("candidate_id", "run_id", "model_id", "basin_id")}
    for index, member in enumerate(members):
        model_id = str(member.get("model_id") or "")
        basin_id = str(member.get("basin_id") or "")
        expected_member = {
            "array_task_id": index,
            "candidate_id": f"{source_id}:{cycle_iso}:{model_id}:{scenario_id}",
            "run_id": f"fcst_{source_id.lower()}_{compact_cycle}_{model_id}",
            "model_id": model_id,
            "basin_id": basin_id,
            "scenario_id": scenario_id,
            "restart_stage": "forecast",
        }
        if not model_id or not basin_id or member != expected_member:
            return False
        for field, seen in unique_fields.items():
            value = str(member.get(field) or "")
            if value in seen:
                return False
            seen.add(value)

    if bool(identity.get("slurm_ownership_required", False)) and (
        not str(identity.get("expected_slurm_user") or "").strip()
        or not str(identity.get("expected_slurm_account") or "").strip()
    ):
        return False
    digest = str(identity.get("cohort_digest") or "")
    return bool(digest and digest == forecast_cohort_digest(identity))


__all__ = (
    "ACCEPTED_SUBMIT_CONTRACT_VERSION",
    "ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD",
    "ACCEPTED_PROJECTION_FIELDS",
    "ACCEPTED_RECONCILIATION_DECISIONS",
    "ACCEPTED_SUBMIT_OUTCOMES",
    "AcceptedSubmitTransition",
    "AcceptedSubmitCommitResult",
    "AcceptedSubmitEvidenceError",
    "FORECAST_COHORT_STAGE_ALIASES",
    "MAX_FORECAST_COHORT_MEMBERS",
    "accepted_submit_pipeline_job_model_id",
    "accepted_submit_contract_is_current",
    "accepted_submit_row_kind",
    "apply_accepted_submit_transition",
    "canonical_forecast_cohort_members",
    "canonical_forecast_stage",
    "forecast_cohort_digest",
    "forecast_cohort_identity_is_valid",
    "is_forecast_cohort_stage_name",
    "normalize_accepted_submit_attempt_anchor",
    "normalize_accepted_submit_evidence",
    "normalize_candidate_projections",
    "ordered_cohort_members",
)
