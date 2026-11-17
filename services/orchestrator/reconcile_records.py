"""Shared records, query errors, constants and sacct row parsing for restart reconcile."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from services.slurm_gateway.models import TERMINAL_STATUSES, SlurmJobStatus
from services.slurm_gateway.real_backend import SLURM_JOB_ID_RE, SLURM_STATE_MAP, _normalize_slurm_state

MAX_FALLBACK_MASTERS = 2

# Pass-evidence-only match basis of a classified name-window fallback outcome.
FALLBACK_BASIS_SUBMITLINE_EXACT = "submitline_exact"
FALLBACK_BASIS_NAME_WINDOW_COUNT = "name_window_count"
# A timezone-less Slurm ``Submit`` value is rendered in host-local wall clock;
# ``%Y-%m-%dT%H:%M:%S`` is Slurm's own field format for ``Submit``.
_FALLBACK_SUBMIT_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S.%f")

RECONCILE_UNVERIFIED_STATUS = "reconcile_unverified"
# A reservation whose sbatch was never confirmed by accounting: sbatch did not
# take, so the candidate may be safely re-submitted on a later pass. We mark it
# typed rather than blindly re-submitting inside reconcile.
RESERVATION_LOST_STATUS = "reservation_lost"

# Minimum survival window a reserved-unbound row must clear before a *confirmed
# absence* from accounting may demote it to reservation_lost. sacct/slurmdbd
# accounting propagation lags sbatch acceptance: a job sbatch just accepted can
# be momentarily invisible to sacct (returncode 0, no matching comment row).
# Within this grace we treat that empty answer as "not yet propagated", keep the
# row reserved, and defer to a later pass — preventing the misjudge→reclaim→
# re-sbatch double-submit. 120s comfortably covers observed slurmdbd lag.
RESERVATION_ABSENCE_GRACE = timedelta(seconds=120)

# Typed action for a reserved-unbound row that queried absent but is younger
# than RESERVATION_ABSENCE_GRACE: deliberately NOT demoted this pass.
ABSENCE_UNCONFIRMED_ACTION = "absence_unconfirmed"

# Typed action/decision for a reserved-unbound row abandoned after the
# configured number of consecutive identity-mismatch passes.
IDENTITY_MISMATCH_RELEASED_ACTION = "identity_mismatch_released"

# Typed action for a reserved-unbound row whose journal surface raised a
# capacity or integrity fault: the row is left untouched (fail-closed locally)
# and its reason/offending file are recorded, while the remaining rows and the
# scheduler pass keep going.
JOURNAL_QUARANTINED_ACTION = "journal_quarantined"
MAX_EXACT_COMMENT_MATCHES = 2


def _as_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _strict_utc_datetime(value: Any) -> datetime | None:
    """Accept only aware datetimes at evidence-authority boundaries."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except (OverflowError, TypeError, ValueError):
        return None


class ReconcileQueryUnavailable(Exception):
    """sacct could not be reached / did not answer authoritatively.

    Distinguishes a *transient* query failure (sacct timed out, crashed, or
    exited non-zero) from an authoritative "accounting has no such job" answer.
    A transient failure MUST NOT be read as "job is gone": doing so would let a
    crash-recovery reconcile mark an in-flight reservation ``reservation_lost``
    and free a later pass to re-reserve+re-sbatch the very same candidate
    (double submission). Callers must keep the row in its current state and let
    a later reconcile pass retry, rather than acting on an absence we never
    actually confirmed.
    """

    reason_class = "process_unavailable"

    def __init__(self, message: str, *, reason_class: str | None = None) -> None:
        super().__init__(message)
        if reason_class is not None:
            self.reason_class = reason_class


class ReconcileQuerySaturated(ReconcileQueryUnavailable):
    """A bounded accounting page exceeded a byte or logical-row ceiling."""

    def __init__(self, boundary: str) -> None:
        if boundary not in {"bytes", "rows"}:
            raise ValueError("bounded accounting saturation boundary must be bytes or rows")
        self.boundary = boundary
        super().__init__(
            f"sacct query exceeded bounded output ({boundary})",
            reason_class=f"bounded_output_{boundary}_saturated",
        )


class _ReconcileFallbackQueryUnavailable(ReconcileQueryUnavailable):
    """A transient query failure raised by the name-window fallback (#1565).

    The reason class on the exception stays the applicable existing transient
    reason (``process_unavailable`` / ``bounded_output_*``) for PASS evidence,
    but its origin is marked so the reconcile writer persists the durable held
    tuple reason ``comment_accounting_unproven`` instead of the transient one.
    Exact-comment query failures are unmarked and keep their historical durable
    reason behavior.
    """


class _ReconcileFallbackQuerySaturated(ReconcileQuerySaturated, _ReconcileFallbackQueryUnavailable):
    """A bounded-query saturation raised by the name-window fallback.

    ``ReconcileQuerySaturated.__init__`` already sets ``reason_class`` to the
    applicable ``bounded_output_*_saturated`` value; the multiple inheritance
    only marks the fallback origin.
    """

    pass


@dataclass(frozen=True)
class SacctRecord:
    """Minimal authoritative accounting view of a Slurm job for reconcile."""

    slurm_job_id: str
    raw_state: str
    job_name: str
    exit_code: str | None = None
    comment: str | None = None
    user: str | None = None
    account: str | None = None
    run_id: str | None = None
    model_id: str | None = None
    stage: str | None = None
    pipeline_job_id: str | None = None
    array_task_id: int | str | None = None
    task_id: int | str | None = None
    submitted_manifest: Mapping[str, Any] | None = None
    stdout_identity: Mapping[str, Any] | None = None
    file_journal_identity: Mapping[str, Any] | None = None
    array_member_job_ids: tuple[str, ...] = ()
    array_task_records: tuple["SacctRecord", ...] = ()
    # #1850: the parsed host-local-to-UTC Slurm ``Submit`` instant carried by a
    # name-window fallback candidate so the typed commit can prove the attempt
    # window is closed at both endpoints and the candidate is not claimed by a
    # sibling reserved attempt. ``None`` when no Submit was requested/parsed.
    submitted_at: datetime | None = None
    # #2655: the name-window fallback master's proven ``SubmitLine`` comment
    # key (``nhms_idem:<idempotency_key>``), known only when every eligible row
    # of that master carries the same single ``--comment=`` value.
    submitline_key: str | None = None


# A sacct querier maps a slurm_job_id to its accounting record (or None when the
# job is unknown to accounting). Injectable so tests can supply a fake without a
# real cluster.
SacctQuerier = Callable[[str], "SacctRecord | None"]


def _parse_fallback_submit_instant(value: str | None) -> datetime | None:
    """Parse a timezone-less Slurm ``Submit`` value as host-local wall clock.

    Slurm renders ``Submit`` without a timezone; sacct already interpreted the
    raw timestamp in the host's local timezone, so the value is attached to the
    same local zone before converting to UTC. ``None`` (unparseable) is the
    caller's transient-denial signal.
    """

    text = (value or "").strip()
    if not text:
        return None
    # Slurm may render fractional seconds; only the whole-second form is
    # guaranteed, so try the longest match last.
    for fmt in _FALLBACK_SUBMIT_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        try:
            local = parsed.astimezone()
        except (OverflowError, OSError, ValueError):
            return None
        if local.utcoffset() is None:
            return None
        return local.astimezone(UTC)
    return None


def _fallback_master_id(raw_job_id: str) -> str | None:
    """Normalize an array/step row to its bare numeric master id, or None.

    Strips the ``.<step>`` suffix first, then the ``_<task>`` array suffix,
    and validates the remaining bare numeric master id. A malformed JobID
    (non-numeric after normalization) yields None.
    """

    without_step = raw_job_id.split(".", 1)[0]
    master_id = without_step.split("_", 1)[0]
    if not SLURM_JOB_ID_RE.fullmatch(master_id):
        return None
    return master_id


def _parse_master_sacct_row(stdout: str, slurm_job_id: str) -> SacctRecord | None:
    """Parse one job row, or aggregate array members for a bare master id.

    Some Slurm accounting configurations omit the bare array-master row and
    return only ``<master>_<task>`` rows.  A durable pipeline row is bound to
    that bare master id, so restart reconcile must fold its exact members using
    the same parent-status semantics as the gateway rather than treating the
    job as absent.
    """

    target_job_id = str(slurm_job_id)
    target_is_array_task = "_" in target_job_id
    exact_fields: list[str] | None = None
    member_fields: list[list[str]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split("|")
        if len(fields) < 3:
            continue
        job_id = fields[0]
        # Skip job step rows (12345.batch / 12345_3.batch).
        if "." in job_id:
            continue
        if job_id != target_job_id:
            if (
                not target_is_array_task
                and job_id.startswith(f"{target_job_id}_")
                and SLURM_JOB_ID_RE.fullmatch(job_id)
            ):
                member_fields.append(fields)
            continue
        exact_fields = fields

    if exact_fields is not None:
        exact = _sacct_record_from_fields(exact_fields)
        if member_fields:
            return SacctRecord(
                **{
                    **exact.__dict__,
                    "array_member_job_ids": tuple(fields[0] for fields in member_fields),
                    "array_task_records": tuple(_sacct_record_from_fields(fields) for fields in member_fields),
                }
            )
        return exact
    if not target_is_array_task and member_fields:
        return _aggregate_array_member_rows(member_fields, target_job_id)
    return None


def _sacct_record_from_fields(fields: list[str]) -> SacctRecord:
    job_id = fields[0]
    task_id = job_id.split("_", 1)[1] if "_" in job_id else None
    return SacctRecord(
        slurm_job_id=job_id,
        job_name=fields[1],
        raw_state=fields[2],
        exit_code=fields[3] if len(fields) > 3 else None,
        comment=fields[4].strip() if len(fields) > 4 else None,
        user=fields[5].strip() if len(fields) > 5 and fields[5].strip() else None,
        account=fields[6].strip() if len(fields) > 6 and fields[6].strip() else None,
        task_id=task_id,
        array_task_id=task_id,
    )


def _aggregate_array_member_rows(
    member_fields: list[list[str]],
    master_job_id: str,
) -> SacctRecord:
    """Fold exact array members into one durable master accounting record."""

    member_statuses: list[SlurmJobStatus] = []
    for fields in member_fields:
        normalized = _normalize_slurm_state(fields[2])
        member_statuses.append(SLURM_STATE_MAP.get(normalized, SlurmJobStatus.FAILED))

    non_terminal = [status for status in member_statuses if status not in TERMINAL_STATUSES]
    selected_fields: list[str] | None = None
    if non_terminal:
        raw_state = "PENDING" if all(status == SlurmJobStatus.SUBMITTED for status in member_statuses) else "RUNNING"
    elif SlurmJobStatus.FAILED in member_statuses:
        failed_index = member_statuses.index(SlurmJobStatus.FAILED)
        selected_fields = member_fields[failed_index]
        raw_state = selected_fields[2]
    elif SlurmJobStatus.CANCELLED in member_statuses:
        cancelled_index = member_statuses.index(SlurmJobStatus.CANCELLED)
        selected_fields = member_fields[cancelled_index]
        raw_state = "CANCELLED"
    else:
        selected_fields = member_fields[0]
        raw_state = "COMPLETED"

    job_names = {fields[1].strip() for fields in member_fields}
    comments = {fields[4].strip() for fields in member_fields if len(fields) > 4 and fields[4].strip()}
    users = {fields[5].strip() for fields in member_fields if len(fields) > 5 and fields[5].strip()}
    accounts = {fields[6].strip() for fields in member_fields if len(fields) > 6 and fields[6].strip()}
    return SacctRecord(
        slurm_job_id=master_job_id,
        job_name=job_names.pop() if len(job_names) == 1 else "",
        raw_state=raw_state,
        exit_code=(selected_fields[3] if selected_fields is not None and len(selected_fields) > 3 else None),
        comment=comments.pop() if len(comments) == 1 else None,
        user=users.pop() if len(users) == 1 else None,
        account=accounts.pop() if len(accounts) == 1 else None,
        array_member_job_ids=tuple(fields[0] for fields in member_fields),
        array_task_records=tuple(_sacct_record_from_fields(fields) for fields in member_fields),
    )


# A comment querier maps an idempotency_key to the accounting record of the job
# sbatch accepted under that ``--comment`` (or None when accounting has no such
# job). Injectable so tests need no real cluster.
@dataclass(frozen=True)
class CommentAccountingResult(Sequence[SacctRecord]):
    """Explicit scope carried by the default exact-comment adapter."""

    records: tuple[SacctRecord, ...]
    scope: str  # ``owner`` or authoritative ``global``.
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    coverage_complete: bool = False
    # #1565: pass-evidence-only markers set by the name-window fallback. The
    # fallback result is a complete owner-exact proof by itself — it must never
    # run through the exact-comment global-visibility round. Never durable.
    fallback_name_window: bool = False
    fallback_submit_unparsable: bool = False
    # Controller retention is positive-only evidence: it identifies a unique
    # forcing master but never authorizes absence or a global accounting scan.
    controller_exact_comment: bool = False
    # #2655: ``submitline_exact`` | ``name_window_count`` when at least one
    # master remained after classification; pass evidence only.
    fallback_match_basis: str | None = None

    def __getitem__(self, index: int) -> SacctRecord:
        return self.records[index]

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[SacctRecord]:
        return iter(self.records)


CommentSacctQuerier = Callable[..., "CommentAccountingResult | SacctRecord | Sequence[SacctRecord] | None"]


def _callable_accepts_keyword(callable_value: Callable[..., Any], name: str) -> bool:
    try:
        parameters = tuple(inspect.signature(callable_value).parameters.values())
    except (TypeError, ValueError):
        return False
    return any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters) or any(
        parameter.name == name for parameter in parameters
    )
