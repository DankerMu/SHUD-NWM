"""Restart reconcile for in-flight Slurm jobs by durable candidate identity.

On scheduler/gateway restart, in-flight job ids MUST be recovered from the
durable ``pipeline_job`` table (NOT the gateway's in-memory ``_jobs``), then each
is reconciled against authoritative Slurm accounting via ``sacct``. Reconcile is
read-only with respect to submission: it queries ``sacct`` and updates the
durable status; it NEVER resubmits a still-running or already-terminal
candidate.

Identity verification: ``pipeline_job`` durably binds ``slurm_job_id`` to
``run_id``/``model_id``/``cycle_id``/``stage``. ``sacct`` is queried for that
``slurm_job_id`` and the returned record must (a) exist in accounting and (b)
carry the expected ``JobName`` stage token (``nhms_<stage>``), confirming the
accounting row is the job we recorded and guarding against job-id reuse or
mismatch. If accounting has no matching/verifiable row, the candidate is marked
``reconcile_unverified`` rather than blindly resubmitted.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from services.orchestrator.reservation import idempotency_key_from_comment
from services.slurm_gateway.models import TERMINAL_STATUSES, SlurmJobStatus
from services.slurm_gateway.real_backend import (
    SLURM_JOB_ID_RE,
    SLURM_STATE_MAP,
    _normalize_slurm_state,
    map_slurm_error_code,
)

LOGGER = logging.getLogger(__name__)

# Conservative lookback for comment-based sacct recovery. The crash window
# between sbatch acceptance and durable bind can outlive sacct's default
# accounting horizon; a fixed 7-day floor keeps a just-submitted master job
# visible to reconcile-by-comment instead of being misjudged reservation_lost.
COMMENT_SACCT_LOOKBACK_DAYS = 7

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


@dataclass(frozen=True)
class SacctRecord:
    """Minimal authoritative accounting view of a Slurm job for reconcile."""

    slurm_job_id: str
    raw_state: str
    job_name: str
    exit_code: str | None = None
    comment: str | None = None
    run_id: str | None = None
    model_id: str | None = None
    stage: str | None = None
    pipeline_job_id: str | None = None
    array_task_id: int | str | None = None
    task_id: int | str | None = None
    submitted_manifest: Mapping[str, Any] | None = None
    stdout_identity: Mapping[str, Any] | None = None
    file_journal_identity: Mapping[str, Any] | None = None


# A sacct querier maps a slurm_job_id to its accounting record (or None when the
# job is unknown to accounting). Injectable so tests can supply a fake without a
# real cluster.
SacctQuerier = Callable[[str], "SacctRecord | None"]


def default_sacct_querier(slurm_bin_path: str = "") -> SacctQuerier:
    """Build a sacct querier that shells out to the real ``sacct`` binary.

    Uses ``--parsable2 --noheader`` with the same field shape the gateway uses
    elsewhere (``JobID|JobName|State|ExitCode``), and returns the exact target
    row. For master jobs this is the master row; for array tasks this is the
    ``<master>_<task>`` row.
    """

    sacct = f"{slurm_bin_path.rstrip('/')}/sacct" if slurm_bin_path else "sacct"

    def _query(slurm_job_id: str) -> SacctRecord | None:
        command = [
            sacct,
            "--parsable2",
            "--noheader",
            "--format=JobID,JobName,State,ExitCode,Comment",
            f"--jobs={slurm_job_id}",
        ]
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell.
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            LOGGER.warning("sacct query failed for %s: %s", slurm_job_id, error)
            raise ReconcileQueryUnavailable(
                f"sacct query failed for {slurm_job_id}: {error}"
            ) from error
        if result.returncode != 0:
            LOGGER.warning(
                "sacct returned %s for %s", result.returncode, slurm_job_id
            )
            raise ReconcileQueryUnavailable(
                f"sacct returned {result.returncode} for {slurm_job_id}"
            )
        # Query succeeded: a None here means accounting has no such row
        # (confirmed-absent), not that we failed to ask.
        return _parse_master_sacct_row(result.stdout, slurm_job_id)

    return _query


def default_comment_sacct_querier(slurm_bin_path: str = "") -> CommentSacctQuerier:
    """Build a comment querier: idempotency_key -> the job sbatch recorded.

    Queries ``sacct`` for the master row whose ``Comment`` is
    ``nhms_idem:<idempotency_key>`` so a crashed reservation can be reconciled
    back to the real ``slurm_job_id`` even though the durable bind never ran.
    """

    from services.orchestrator.reservation import slurm_comment_for

    sacct = f"{slurm_bin_path.rstrip('/')}/sacct" if slurm_bin_path else "sacct"

    def _query(idempotency_key: str) -> SacctRecord | None:
        target_comment = slurm_comment_for(idempotency_key)
        # --starttime: without it sacct only returns jobs inside its default
        # (often same-day) window, so an in-flight master submitted just before a
        # crash can be invisible and misjudged reservation_lost. Mirror
        # real_backend.list_jobs by passing an explicit conservative floor.
        start_time = (
            datetime.now(UTC) - timedelta(days=COMMENT_SACCT_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        command = [
            sacct,
            "--parsable2",
            "--noheader",
            "--format=JobID,JobName,State,ExitCode,Comment",
            "--allusers",
            f"--starttime={start_time}",
        ]
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell.
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            LOGGER.warning("sacct comment query failed for %s: %s", idempotency_key, error)
            raise ReconcileQueryUnavailable(
                f"sacct comment query failed for {idempotency_key}: {error}"
            ) from error
        if result.returncode != 0:
            LOGGER.warning("sacct comment query returned %s", result.returncode)
            raise ReconcileQueryUnavailable(
                f"sacct comment query returned {result.returncode}"
            )
        # Below: the query succeeded. A None here is the authoritative
        # "accounting has no job for this comment" answer (confirmed-absent),
        # which is the ONLY case allowed to mark reservation_lost. A transient
        # failure never reaches here.
        return _parse_comment_sacct_rows(result.stdout, target_comment)

    return _query


def _parse_comment_sacct_rows(stdout: str, target_comment: str) -> SacctRecord | None:
    """Find the sacct row whose Comment matches target_comment and return its
    master job id. Array element/pending rows ("<master>_<task>",
    "<master>_[<range>]") normalize down to the bare master id; step sub-rows
    ("<id>.batch"/"<id>.extern") are skipped. Returns None when no row matches
    (the authoritative confirmed-absent answer)."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split("|")
        if len(fields) < 5:
            continue
        raw_job_id = fields[0]
        if "." in raw_job_id:
            continue  # skip .batch/.extern step sub-rows
        if fields[4].strip() != target_comment:
            continue
        # Normalize array element/pending rows to the bare master id so an
        # array stage stamped with the idempotency --comment reconciles back
        # to its master job id (single-job ids have no "_" and pass through).
        job_id = raw_job_id.split("_", 1)[0]
        # Shape-validate the normalized master id (bare digits). A malformed
        # JobID (e.g. leading "_") normalizes to "" or non-numeric; skip it
        # and keep scanning so it can't short-circuit past a real array row
        # into a bogus empty-id record / false confirmed-absent.
        if not SLURM_JOB_ID_RE.fullmatch(job_id):
            continue
        return SacctRecord(
            slurm_job_id=job_id,
            job_name=fields[1],
            raw_state=fields[2],
            exit_code=fields[3] or None,
            comment=fields[4].strip(),
        )
    return None


def _parse_master_sacct_row(stdout: str, slurm_job_id: str) -> SacctRecord | None:
    """Parse the exact master or array-task job row from sacct."""

    target_job_id = str(slurm_job_id)
    target_is_array_task = "_" in target_job_id
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
        # Master queries keep the historical master-row behavior. Array-task
        # queries must return the exact task row instead of being skipped.
        if not target_is_array_task and "_" in job_id:
            continue
        if job_id != target_job_id:
            continue
        task_id = job_id.split("_", 1)[1] if "_" in job_id else None
        return SacctRecord(
            slurm_job_id=job_id,
            job_name=fields[1],
            raw_state=fields[2],
            exit_code=fields[3] if len(fields) > 3 else None,
            comment=fields[4].strip() if len(fields) > 4 else None,
            task_id=task_id,
            array_task_id=task_id,
        )
    return None


@dataclass(frozen=True)
class ReconcileOutcome:
    job_id: str
    slurm_job_id: str
    # "terminal" | "still_running" | "unverified" | "query_unavailable"
    action: str
    status: str


def _expected_job_name_token(stage: str | None, job_type: str | None) -> str | None:
    """The ``nhms_<stage>`` token sbatch templates set as ``--job-name``."""

    token = (stage or job_type or "").strip()
    if not token:
        return None
    return f"nhms_{token}"


_GENERIC_ARRAY_JOB_NAMES = frozenset({"nhms_forecast", "nhms_forcing"})
_FORECAST_STAGE_ALIASES = frozenset({"forecast", "run_shud_forecast_array", "run_shud_forecast"})
_FORCING_STAGE_ALIASES = frozenset({"forcing", "produce_forcing", "forcing_package"})


def _identity_matches(
    record: SacctRecord,
    expected_token: str | None,
    job: Any | None = None,
    *,
    require_durable_identity: bool = True,
) -> bool:
    """Confirm the accounting row belongs to the recorded candidate.

    When the durable row gives a stage/job_type, the sacct ``JobName`` must carry
    the matching ``nhms_<stage>`` token.
    """

    # The durable 1:1 slurm_job_id↔candidate binding is the primary identity; the
    # JobName stage token is a secondary anti-reuse guard against a recycled
    # slurm_job_id. With no token available we cannot run that guard, so be
    # conservative and judge unverified (do not accept, do not resubmit).
    if expected_token is None:
        return False
    job_name = record.job_name.strip()
    generic_job_name = job_name in _GENERIC_ARRAY_JOB_NAMES
    expected_stage_token = expected_token.removeprefix("nhms_")
    job_stage_token = job_name.removeprefix("nhms_")
    if job_name != expected_token and not (generic_job_name and _stages_match(job_stage_token, expected_stage_token)):
        return False
    if generic_job_name and require_durable_identity:
        return job is not None and _record_has_durable_identity_proof(record, job)
    return True


def _record_has_durable_identity_proof(record: SacctRecord, job: Any) -> bool:
    for identity in _record_identity_mappings(record):
        if _identity_mapping_matches_job(identity, job):
            return True
    return False


def _record_identity_mappings(record: SacctRecord) -> list[Mapping[str, Any]]:
    mappings: list[Mapping[str, Any]] = []
    direct = {
        "run_id": record.run_id,
        "model_id": record.model_id,
        "stage": record.stage,
        "pipeline_job_id": record.pipeline_job_id,
        "array_task_id": record.array_task_id,
        "task_id": record.task_id,
    }
    if any(value not in (None, "") for value in direct.values()):
        mappings.append(direct)
    for value in (record.submitted_manifest, record.stdout_identity, record.file_journal_identity):
        if isinstance(value, Mapping):
            mappings.append(value)
    return mappings


def _identity_mapping_matches_job(identity: Mapping[str, Any], job: Any) -> bool:
    proof = False
    expected_job_id = str(getattr(job, "job_id", "") or "")
    expected_run_id = str(getattr(job, "run_id", "") or "")
    expected_model_id = str(getattr(job, "model_id", "") or "")
    expected_stage = str(getattr(job, "stage", None) or getattr(job, "job_type", "") or "")
    expected_task_id = getattr(job, "array_task_id", None)

    actual_job_id = _identity_text(identity, "pipeline_job_id", "job_id")
    if actual_job_id:
        if actual_job_id != expected_job_id:
            return False
        proof = True

    actual_run_id = _identity_text(identity, "run_id")
    if actual_run_id:
        if expected_run_id and actual_run_id != expected_run_id:
            return False
        proof = True

    actual_model_id = _identity_text(identity, "model_id")
    if actual_model_id and expected_model_id and actual_model_id != expected_model_id:
        return False

    actual_stage = _identity_text(identity, "stage", "job_type", "stage_name")
    if actual_stage and not _stages_match(actual_stage, expected_stage):
        return False

    if expected_task_id not in (None, ""):
        actual_task_id = _identity_text(identity, "array_task_id", "task_id", "original_task_id")
        if actual_task_id != str(expected_task_id):
            return False
        proof = True

    return proof


def _identity_text(identity: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = identity.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _stages_match(actual: str | None, expected: str | None) -> bool:
    if not actual or not expected:
        return False
    actual_value = actual.strip()
    expected_value = expected.strip()
    if actual_value == expected_value:
        return True
    return _stage_family(actual_value) is not None and _stage_family(actual_value) == _stage_family(expected_value)


def _stage_family(value: str) -> str | None:
    if value in _FORECAST_STAGE_ALIASES:
        return "forecast"
    if value in _FORCING_STAGE_ALIASES:
        return "forcing"
    return None


def _sacct_query_job_id(job: Any) -> str:
    slurm_job_id = str(getattr(job, "slurm_job_id", "") or "")
    array_task_id = getattr(job, "array_task_id", None)
    if not slurm_job_id or "_" in slurm_job_id or array_task_id in (None, ""):
        return slurm_job_id
    return f"{slurm_job_id}_{array_task_id}"


def reconcile_inflight_jobs(
    store,
    *,
    sacct_query: SacctQuerier,
) -> list[ReconcileOutcome]:
    """Reconcile durable in-flight jobs against ``sacct``; never resubmit.

    For each in-flight ``pipeline_job`` (read from durable storage), query
    ``sacct`` for its ``slurm_job_id``, verify candidate identity, and update the
    durable status to the terminal/running state reported by accounting. Returns
    a per-job outcome. Submission is never triggered here.
    """

    outcomes: list[ReconcileOutcome] = []
    for job in store.query_inflight_jobs():
        slurm_job_id = job.slurm_job_id
        if not slurm_job_id:
            continue

        query_slurm_job_id = _sacct_query_job_id(job)
        try:
            record = sacct_query(query_slurm_job_id)
        except ReconcileQueryUnavailable as error:
            LOGGER.warning(
                "reconcile inflight query unavailable for %s: %s",
                query_slurm_job_id,
                error,
            )
            outcomes.append(
                ReconcileOutcome(
                    job_id=job.job_id,
                    slurm_job_id=str(slurm_job_id),
                    action="query_unavailable",
                    status=str(job.status),
                )
            )
            continue
        expected_token = _expected_job_name_token(job.stage, job.job_type)
        normalized = _normalize_slurm_state(record.raw_state) if record is not None else None
        slurm_status = SLURM_STATE_MAP.get(normalized, SlurmJobStatus.FAILED) if normalized is not None else None
        requires_durable_identity = slurm_status in TERMINAL_STATUSES

        if record is None or not _identity_matches(
            record,
            expected_token,
            job,
            require_durable_identity=requires_durable_identity,
        ):
            # Cannot prove this is our candidate: mark typed, do NOT resubmit.
            store.update_job_status(
                job.job_id,
                RECONCILE_UNVERIFIED_STATUS,
                error_code="SLURM_RECONCILE_UNVERIFIED",
                error_message=(
                    "sacct could not verify the candidate identity for "
                    f"slurm_job_id={slurm_job_id}."
                ),
            )
            outcomes.append(
                ReconcileOutcome(
                    job_id=job.job_id,
                    slurm_job_id=str(slurm_job_id),
                    action="unverified",
                    status=RECONCILE_UNVERIFIED_STATUS,
                )
            )
            continue

        slurm_status = slurm_status or SlurmJobStatus.FAILED

        if slurm_status in TERMINAL_STATUSES:
            error_code = (
                map_slurm_error_code(record.raw_state)
                if slurm_status == SlurmJobStatus.FAILED
                else None
            )
            store.update_job_status(
                job.job_id,
                slurm_status.value,
                error_code=error_code,
            )
            outcomes.append(
                ReconcileOutcome(
                    job_id=job.job_id,
                    slurm_job_id=str(slurm_job_id),
                    action="terminal",
                    status=slurm_status.value,
                )
            )
            continue

        # Still running/queued: keep current status, never resubmit.
        store.update_job_status(job.job_id, slurm_status.value)
        outcomes.append(
            ReconcileOutcome(
                job_id=job.job_id,
                slurm_job_id=str(slurm_job_id),
                action="still_running",
                status=slurm_status.value,
            )
        )

    return outcomes


# A comment querier maps an idempotency_key to the accounting record of the job
# sbatch accepted under that ``--comment`` (or None when accounting has no such
# job). Injectable so tests need no real cluster.
CommentSacctQuerier = Callable[[str], "SacctRecord | None"]


@dataclass(frozen=True)
class ReservationReconcileOutcome:
    job_id: str
    idempotency_key: str
    # "bound" | "reservation_lost" | "query_unavailable" | "absence_unconfirmed".
    # "absence_unconfirmed": accounting answered absent (query succeeded, no
    # matching comment row) but the reservation is younger than
    # RESERVATION_ABSENCE_GRACE, so the absence may merely be slurmdbd
    # propagation lag for a just-accepted sbatch. We keep the row reserved and
    # defer demotion to a later pass — NEVER mark reservation_lost here, which
    # would let the reserve gate reclaim+re-sbatch an in-flight job.
    action: str
    status: str
    slurm_job_id: str | None = None


def reconcile_reserved_unbound_jobs(
    store,
    *,
    comment_query: CommentSacctQuerier,
    grace: timedelta = RESERVATION_ABSENCE_GRACE,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[ReservationReconcileOutcome]:
    """Reconcile the submit-crash window: reserved rows with no slurm_job_id.

    A crash after ``sbatch`` accepted a job but before the durable bind leaves a
    ``status='reserved'`` row whose ``slurm_job_id`` is NULL. We recover the real
    ``slurm_job_id`` by querying accounting for the job sbatch recorded under
    ``--comment=nhms_idem:<idempotency_key>`` and bind it — never blindly
    re-submitting, so at most one job per idempotency_key enters submitted.

    If accounting *confirms* there is no job for that comment, sbatch did not
    actually take; we mark the reservation ``reservation_lost`` (typed), leaving
    a later pass free to reserve+submit again under the same idempotency_key.

    Tri-state, NOT binary: a transient query failure (``sacct`` timed out /
    exited non-zero, surfaced as ``ReconcileQueryUnavailable``) is NOT an
    absence. We must not read it as ``reservation_lost`` — that would free a
    later pass to re-reserve+re-sbatch a job that is in fact in flight (double
    submission). On a transient failure we keep the row ``reserved`` and let a
    later reconcile pass retry; only a *confirmed* absence marks
    ``reservation_lost``.

    Even a *confirmed* absence is gated by a minimum survival window
    (``grace``): sacct/slurmdbd accounting propagation lags sbatch acceptance,
    so a job just accepted can be momentarily invisible to the comment query
    (returncode 0, no matching row). Demoting such a young reservation to
    ``reservation_lost`` would let the reserve gate reclaim it and re-sbatch the
    very job that is in fact in flight — the same double-submit failure mode as
    the transient case. So a reserved-unbound row younger than ``grace`` whose
    comment query confirms absence is emitted ``absence_unconfirmed`` and left
    ``reserved`` for a later pass (where accounting will have caught up and
    either binds it or, past grace, demotes it). A row with no ``created_at`` to
    prove youth, or one already older than ``grace``, keeps the old behavior and
    demotes to ``reservation_lost`` so liveness never regresses. ``now`` is
    injectable for deterministic tests.
    """

    outcomes: list[ReservationReconcileOutcome] = []
    for job in store.query_reserved_unbound_jobs():
        idempotency_key = job.idempotency_key
        if not idempotency_key:
            continue

        try:
            record = comment_query(str(idempotency_key))
        except ReconcileQueryUnavailable as error:
            # Transient failure: we did NOT confirm absence. Keep the row
            # reserved (do not touch status) and retry on a later pass. Never
            # mark reservation_lost here — that is what would let a double
            # submit slip through.
            LOGGER.warning(
                "reconcile reservation query unavailable for idempotency_key=%s: %s",
                idempotency_key,
                error,
            )
            outcomes.append(
                ReservationReconcileOutcome(
                    job_id=job.job_id,
                    idempotency_key=str(idempotency_key),
                    action="query_unavailable",
                    status=str(job.status),
                )
            )
            continue
        # Confirm the accounting row truly carries our idempotency comment AND
        # that its slurm_job_id has a valid Slurm shape (``\d+`` or ``\d+_\d+``)
        # before binding — symmetric with the identity guard in
        # reconcile_inflight_jobs, guarding against a malformed/garbage JobID
        # being durably bound onto the reservation.
        if (
            record is None
            or idempotency_key_from_comment(record.comment) != idempotency_key
            or not SLURM_JOB_ID_RE.fullmatch(str(record.slurm_job_id))
        ):
            # Confirmed-absent BUT possibly just slurmdbd propagation lag: if the
            # reservation's last sbatch attempt is younger than `grace`, defer
            # demotion to a later pass rather than risk demoting an in-flight job
            # into a reclaim+re-sbatch double-submit. Anchor on updated_at —
            # refreshed by the reserve INSERT, the reclaim takeover, AND bind — so
            # a reclaimed+re-submitted reservation keeps grace coverage; created_at
            # is left stale by reclaim and would silently drop that protection.
            # (updated_at is NOT NULL by DB default; created_at is a legacy fallback.)
            anchor = getattr(job, "updated_at", None)
            if anchor is None:
                anchor = getattr(job, "created_at", None)
            if anchor is not None:
                if anchor.tzinfo is None:
                    anchor = anchor.replace(tzinfo=UTC)
                if now() - anchor < grace:
                    outcomes.append(
                        ReservationReconcileOutcome(
                            job_id=job.job_id,
                            idempotency_key=str(idempotency_key),
                            action=ABSENCE_UNCONFIRMED_ACTION,
                            status=str(job.status),
                        )
                    )
                    continue
            store.update_job_status(
                job.job_id,
                RESERVATION_LOST_STATUS,
                error_code="SLURM_RESERVATION_LOST",
                error_message=(
                    "sbatch acceptance for reservation "
                    f"idempotency_key={idempotency_key} could not be confirmed in accounting."
                ),
            )
            outcomes.append(
                ReservationReconcileOutcome(
                    job_id=job.job_id,
                    idempotency_key=str(idempotency_key),
                    action="reservation_lost",
                    status=RESERVATION_LOST_STATUS,
                )
            )
            continue

        bound = store.bind_reservation(
            str(idempotency_key),
            slurm_job_id=record.slurm_job_id,
            status="submitted",
        )
        bound_status = "submitted" if bound is not None else str(job.status)
        outcomes.append(
            ReservationReconcileOutcome(
                job_id=job.job_id,
                idempotency_key=str(idempotency_key),
                action="bound",
                status=bound_status,
                slurm_job_id=record.slurm_job_id,
            )
        )

    return outcomes
