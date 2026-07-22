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

import inspect
import logging
import os
import selectors
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from services.orchestrator.accepted_submit_identity import (
    FORECAST_COHORT_STAGE_ALIASES,
    forecast_cohort_identity_is_valid,
    is_forecast_cohort_stage_name,
    ordered_cohort_members,
)
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
MAX_EXACT_COMMENT_MATCHES = 2
MAX_COMMENT_SACCT_BYTES = 2 * 1024 * 1024
MAX_COMMENT_SACCT_ROWS = 20_000
COMMENT_SACCT_TIMEOUT_SECONDS = 30.0


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


# A sacct querier maps a slurm_job_id to its accounting record (or None when the
# job is unknown to accounting). Injectable so tests can supply a fake without a
# real cluster.
SacctQuerier = Callable[[str], "SacctRecord | None"]


def default_sacct_querier(slurm_bin_path: str = "") -> SacctQuerier:
    """Build a sacct querier that shells out to the real ``sacct`` binary.

    Uses ``--parsable2 --noheader`` with the same field shape the gateway uses
    elsewhere (``JobID|JobName|State|ExitCode``). It returns the exact target
    row when present; a bare array master with no parent row is reconstructed
    from its exact ``<master>_<task>`` rows.
    """

    sacct = f"{slurm_bin_path.rstrip('/')}/sacct" if slurm_bin_path else "sacct"

    def _query(slurm_job_id: str) -> SacctRecord | None:
        command = [
            sacct,
            "--parsable2",
            "--noheader",
            "--format=JobID,JobName,State,ExitCode,Comment,User,Account",
            f"--jobs={slurm_job_id}",
        ]
        try:
            stdout = _bounded_sacct_stdout(command)
        except ReconcileQueryUnavailable as error:
            LOGGER.warning("sacct query failed for %s: %s", slurm_job_id, error)
            raise
        # Query succeeded: a None here means accounting has no such row
        # (confirmed-absent), not that we failed to ask.
        return _parse_master_sacct_row(stdout, slurm_job_id)

    return _query


def default_comment_sacct_querier(slurm_bin_path: str = "") -> CommentSacctQuerier:
    """Build a comment querier: idempotency_key -> the job sbatch recorded.

    Queries ``sacct`` for the master row whose ``Comment`` is
    ``nhms_idem:<idempotency_key>`` so a crashed reservation can be reconciled
    back to the real ``slurm_job_id`` even though the durable bind never ran.
    """

    from services.orchestrator.reservation import slurm_comment_for

    sacct = f"{slurm_bin_path.rstrip('/')}/sacct" if slurm_bin_path else "sacct"

    indexed_matches_by_scope: dict[tuple[str, str], dict[str, tuple[SacctRecord, ...]]] = {}

    def _query(
        idempotency_key: str,
        *,
        expected_user: str | None = None,
        expected_account: str | None = None,
    ) -> tuple[SacctRecord, ...]:
        target_comment = slurm_comment_for(idempotency_key)
        owner_scope = (str(expected_user or ""), str(expected_account or ""))
        # --starttime: without it sacct only returns jobs inside its default
        # (often same-day) window, so an in-flight master submitted just before a
        # crash can be invisible and misjudged reservation_lost. Mirror
        # real_backend.list_jobs by passing an explicit conservative floor.
        start_time = (datetime.now(UTC) - timedelta(days=COMMENT_SACCT_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        command = [
            sacct,
            "--parsable2",
            "--noheader",
            "--format=JobID,JobName,State,ExitCode,Comment,User,Account",
            f"--starttime={start_time}",
        ]
        command.append(f"--user={owner_scope[0]}" if owner_scope[0] else "--allusers")
        if owner_scope[1]:
            command.append(f"--accounts={owner_scope[1]}")
        if owner_scope not in indexed_matches_by_scope:
            stdout = _bounded_sacct_stdout(command)
            indexed_matches_by_scope[owner_scope] = _index_comment_sacct_matches(
                stdout,
                expected_user=owner_scope[0] or None,
                expected_account=owner_scope[1] or None,
            )
        # Below: the query succeeded. A None here is the authoritative
        # "accounting has no job for this comment" answer (confirmed-absent),
        # which is the ONLY case allowed to mark reservation_lost. A transient
        # failure never reaches here.
        return CommentAccountingResult(
            indexed_matches_by_scope[owner_scope].get(target_comment, ()),
            scope="owner" if any(owner_scope) else "global",
        )

    return _query


def _bounded_sacct_stdout(command: Sequence[str]) -> str:
    """Read sacct output with byte/row/time bounds and always reap the child."""

    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell.
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise ReconcileQueryUnavailable("sacct query could not start") from error
    stdout = process.stdout
    if stdout is None:
        _terminate_and_reap(process)
        raise ReconcileQueryUnavailable("sacct query has no stdout")
    output = bytearray()
    row_count = 0
    started = time.monotonic()
    selector = selectors.DefaultSelector()
    try:
        selector.register(stdout, selectors.EVENT_READ)
        while True:
            remaining = COMMENT_SACCT_TIMEOUT_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise ReconcileQueryUnavailable("sacct query timed out")
            events = selector.select(timeout=min(remaining, 0.25))
            if not events:
                if process.poll() is not None:
                    break
                continue
            chunk = os.read(stdout.fileno(), 64 * 1024)
            if not chunk:
                break
            output.extend(chunk)
            row_count += chunk.count(b"\n")
            if len(output) > MAX_COMMENT_SACCT_BYTES or row_count > MAX_COMMENT_SACCT_ROWS:
                raise ReconcileQueryUnavailable("sacct query exceeded bounded output")
        remaining = COMMENT_SACCT_TIMEOUT_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise ReconcileQueryUnavailable("sacct query timed out")
        return_code = process.wait(timeout=remaining)
        if return_code != 0:
            raise ReconcileQueryUnavailable(f"sacct query returned {return_code}")
        return bytes(output).decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as error:
        raise ReconcileQueryUnavailable("sacct query failed") from error
    finally:
        selector.close()
        if process.poll() is None:
            _terminate_and_reap(process)
        else:
            process.wait()


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    try:
        process.terminate()
        process.wait(timeout=1)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.SubprocessError):
            pass


def _index_comment_sacct_matches(
    stdout: str,
    *,
    expected_user: str | None = None,
    expected_account: str | None = None,
) -> dict[str, tuple[SacctRecord, ...]]:
    comments: dict[str, list[SacctRecord]] = {}
    seen: dict[str, set[str]] = {}
    for line in stdout.splitlines()[:MAX_COMMENT_SACCT_ROWS]:
        fields = line.strip().split("|")
        if len(fields) < 5 or "." in fields[0]:
            continue
        comment = fields[4].strip()
        if not comment.startswith("nhms_idem:"):
            continue
        user = fields[5].strip() if len(fields) > 5 and fields[5].strip() else None
        account = fields[6].strip() if len(fields) > 6 and fields[6].strip() else None
        if expected_user and user != expected_user:
            continue
        if expected_account and account != expected_account:
            continue
        master_id = fields[0].split("_", 1)[0]
        if not SLURM_JOB_ID_RE.fullmatch(master_id):
            continue
        comment_seen = seen.setdefault(comment, set())
        if master_id in comment_seen:
            continue
        comment_seen.add(master_id)
        matches = comments.setdefault(comment, [])
        if len(matches) >= MAX_EXACT_COMMENT_MATCHES + 1:
            continue
        matches.append(
            SacctRecord(
                slurm_job_id=master_id,
                job_name=fields[1].strip(),
                raw_state=fields[2].strip(),
                exit_code=fields[3].strip() or None,
                comment=comment,
                user=user,
                account=account,
            )
        )
    return {comment: tuple(records) for comment, records in comments.items()}


def _parse_comment_sacct_rows(stdout: str, target_comment: str) -> SacctRecord | None:
    """Find the sacct row whose Comment matches target_comment and return its
    master job id. Array element/pending rows ("<master>_<task>",
    "<master>_[<range>]") normalize down to the bare master id; step sub-rows
    ("<id>.batch"/"<id>.extern") are skipped. Returns None when no row matches
    (the authoritative confirmed-absent answer)."""
    matches = _parse_comment_sacct_matches(stdout, target_comment, limit=1)
    return matches[0] if matches else None


def _parse_comment_sacct_matches(
    stdout: str,
    target_comment: str,
    *,
    limit: int,
) -> tuple[SacctRecord, ...]:
    matches: list[SacctRecord] = []
    seen_master_ids: set[str] = set()
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
        if job_id in seen_master_ids:
            continue
        seen_master_ids.add(job_id)
        matches.append(
            SacctRecord(
                slurm_job_id=job_id,
                job_name=fields[1],
                raw_state=fields[2],
                exit_code=fields[3] or None,
                comment=fields[4].strip(),
                user=fields[5].strip() if len(fields) > 5 and fields[5].strip() else None,
                account=fields[6].strip() if len(fields) > 6 and fields[6].strip() else None,
            )
        )
        if len(matches) >= max(int(limit), 1):
            break
    return tuple(matches)


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


@dataclass(frozen=True)
class ReconcileOutcome:
    job_id: str
    slurm_job_id: str
    # "terminal" | "still_running" | "unverified" | "query_unavailable"
    action: str
    status: str
    durable_write_kind: str | None = None
    durable_write_count: int = 0
    pipeline_status_write_count: int = 0
    pipeline_event_write_count: int = 0


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
    if _array_master_record_matches_job(record, job):
        return True
    for identity in _record_identity_mappings(record):
        if _identity_mapping_matches_job(identity, job):
            return True
    return False


def _array_master_record_matches_job(record: SacctRecord, job: Any) -> bool:
    """Prove a synthesized array master against its durable master binding."""

    if not record.array_member_job_ids:
        return False
    expected_master = str(getattr(job, "slurm_job_id", "") or "")
    if (
        not expected_master
        or "_" in expected_master
        or record.slurm_job_id != expected_master
        or getattr(job, "array_task_id", None) not in (None, "")
    ):
        return False
    member_prefix = f"{expected_master}_"
    return all(
        member_id.startswith(member_prefix) and SLURM_JOB_ID_RE.fullmatch(member_id)
        for member_id in record.array_member_job_ids
    )


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
        cohort_members = getattr(job, "cohort_members", None)
        file_cohort = bool(
            getattr(store, "supports_accepted_submit_reconcile", False)
            and _is_forecast_cohort_job(job)
            and isinstance(cohort_members, Sequence)
            and not isinstance(cohort_members, str | bytes)
            and cohort_members
        )
        requires_durable_identity = slurm_status in TERMINAL_STATUSES and not file_cohort

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
                error_message=(f"sacct could not verify the candidate identity for slurm_job_id={slurm_job_id}."),
            )
            outcomes.append(
                ReconcileOutcome(
                    job_id=job.job_id,
                    slurm_job_id=str(slurm_job_id),
                    action="unverified",
                    status=RECONCILE_UNVERIFIED_STATUS,
                    durable_write_kind="pipeline_job_status",
                    durable_write_count=1,
                )
            )
            continue

        if file_cohort and not _terminal_file_cohort_identity_matches(store, record, job):
            write_count = int(
                bool(
                    getattr(store, "record_pipeline_job_reconciliation")(
                        job.job_id,
                        reconciliation_decision="identity_mismatch_blocked",
                        matched_slurm_job_id=None,
                        status=RECONCILE_UNVERIFIED_STATUS,
                    )
                )
            )
            outcomes.append(
                ReconcileOutcome(
                    job_id=job.job_id,
                    slurm_job_id=str(slurm_job_id),
                    action="identity_mismatch_blocked",
                    status=RECONCILE_UNVERIFIED_STATUS,
                    durable_write_kind="pipeline_job_reconciliation",
                    durable_write_count=write_count,
                )
            )
            continue

        slurm_status = slurm_status or SlurmJobStatus.FAILED

        if slurm_status in TERMINAL_STATUSES:
            if file_cohort:
                projections, accounting_complete = _file_cohort_task_projections(job, record)
                error_code = map_slurm_error_code(record.raw_state) if slurm_status == SlurmJobStatus.FAILED else None
                write_result = store.project_forecast_cohort_tasks(
                    job.job_id,
                    master_slurm_job_id=str(record.slurm_job_id),
                    projections=projections,
                    complete=accounting_complete,
                    master_status=slurm_status.value,
                    master_error_code=error_code,
                    reconciliation_decision="matched_bound",
                )
                write_count = int(write_result.get("total") or 0)
                outcomes.append(
                    ReconcileOutcome(
                        job_id=job.job_id,
                        slurm_job_id=str(slurm_job_id),
                        action="terminal" if accounting_complete else "task_accounting_incomplete",
                        status=slurm_status.value if accounting_complete else RECONCILE_UNVERIFIED_STATUS,
                        durable_write_kind="forecast_cohort_projection",
                        durable_write_count=write_count,
                        pipeline_status_write_count=int(write_result.get("pipeline_status") or 0),
                        pipeline_event_write_count=int(write_result.get("pipeline_event") or 0),
                    )
                )
                continue
            error_code = map_slurm_error_code(record.raw_state) if slurm_status == SlurmJobStatus.FAILED else None
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
                    durable_write_kind="pipeline_job_status",
                    durable_write_count=1,
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
                durable_write_kind="pipeline_job_status",
                durable_write_count=1,
            )
        )

    return outcomes


def _is_forecast_cohort_job(job: Any) -> bool:
    stage = str(getattr(job, "stage", None) or "")
    if stage:
        return stage in FORECAST_COHORT_STAGE_ALIASES
    return str(getattr(job, "job_type", None) or "") in FORECAST_COHORT_STAGE_ALIASES


def _file_cohort_runtime_identity_matches(store: Any, identity: Mapping[str, Any]) -> bool:
    validator = getattr(store, "forecast_cohort_runtime_identity_matches", None)
    return callable(validator) and bool(validator(identity))


def _terminal_file_cohort_identity_matches(store: Any, record: SacctRecord, job: Any) -> bool:
    identity = vars(job) if hasattr(job, "__dict__") else {}
    if not forecast_cohort_identity_is_valid(identity) or not _file_cohort_runtime_identity_matches(store, identity):
        return False
    expected_master = str(getattr(job, "slurm_job_id", None) or "")
    if not expected_master or record.slurm_job_id != expected_master:
        return False
    if record.comment != str(getattr(job, "slurm_comment", None) or ""):
        return False
    if record.stage not in (None, "") and not is_forecast_cohort_stage_name(record.stage):
        return False
    expected_user = str(getattr(job, "expected_slurm_user", None) or "")
    expected_account = str(getattr(job, "expected_slurm_account", None) or "")
    if bool(getattr(job, "slurm_ownership_required", False)) and (
        not expected_user or not expected_account or not record.user or not record.account
    ):
        return False
    if expected_user and record.user != expected_user:
        return False
    if expected_account and record.account != expected_account:
        return False
    prefix = f"{expected_master}_"
    for task in record.array_task_records:
        if not str(task.slurm_job_id or "").startswith(prefix):
            return False
        if task.job_name.strip() not in _GENERIC_ARRAY_JOB_NAMES and not is_forecast_cohort_stage_name(
            task.job_name.removeprefix("nhms_")
        ):
            return False
        if task.comment not in (None, "", getattr(job, "slurm_comment", None)):
            return False
    return True


def _file_cohort_task_projections(job: Any, record: SacctRecord) -> tuple[list[dict[str, Any]], bool]:
    members = {
        int(member["array_task_id"]): dict(member)
        for member in ordered_cohort_members(getattr(job, "cohort_members", ()))
    }
    tasks: dict[int, list[SacctRecord]] = {}
    accounting_complete = True
    for task in record.array_task_records:
        raw_task_id = task.array_task_id if task.array_task_id is not None else task.task_id
        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError):
            accounting_complete = False
            continue
        if task_id not in members:
            accounting_complete = False
            continue
        tasks.setdefault(task_id, []).append(task)
    if set(tasks) != set(members) or len(record.array_task_records) != len(members):
        accounting_complete = False

    projections: list[dict[str, Any]] = []
    for task_id, member in sorted(members.items()):
        task_records = tasks.get(task_id, [])
        task = task_records[0] if len(task_records) == 1 else None
        outcome = "unverified"
        if len(task_records) > 1:
            accounting_complete = False
        elif task is not None:
            normalized = _normalize_slurm_state(task.raw_state)
            status = SLURM_STATE_MAP.get(normalized)
            if status == SlurmJobStatus.SUCCEEDED:
                outcome = "succeeded"
            elif status in TERMINAL_STATUSES:
                outcome = "failed"
            else:
                accounting_complete = False
        if outcome == "unverified":
            accounting_complete = False
        projections.append(
            {
                "candidate_id": member.get("candidate_id"),
                "run_id": member.get("run_id"),
                "model_id": member.get("model_id"),
                "array_task_id": task_id,
                "array_task_outcome": outcome,
                "task_slurm_job_id": task.slurm_job_id if task is not None else None,
                "error_code": (None if outcome != "failed" or task is None else map_slurm_error_code(task.raw_state)),
                "restart_stage": "state_save_qc" if outcome == "succeeded" else member.get("restart_stage"),
                "native_shud_resubmitted": False,
            }
        )
    return projections, accounting_complete


# A comment querier maps an idempotency_key to the accounting record of the job
# sbatch accepted under that ``--comment`` (or None when accounting has no such
# job). Injectable so tests need no real cluster.
@dataclass(frozen=True)
class CommentAccountingResult(Sequence[SacctRecord]):
    """Explicit scope carried by the default exact-comment adapter."""

    records: tuple[SacctRecord, ...]
    scope: str  # ``owner`` or authoritative ``global``.

    def __getitem__(self, index: int) -> SacctRecord:
        return self.records[index]

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[SacctRecord]:
        return iter(self.records)


@dataclass(frozen=True)
class _CommentAccountingProof:
    kind: str
    records: tuple[SacctRecord, ...] = ()


CommentSacctQuerier = Callable[..., "CommentAccountingResult | SacctRecord | Sequence[SacctRecord] | None"]


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
    reconciliation_source: str | None = None
    reconciliation_decision: str | None = None
    matched_slurm_job_id: str | None = None
    match_count: int | None = None
    durable_write_kind: str | None = None
    durable_write_count: int = 0


def reconcile_reserved_unbound_jobs(
    store,
    *,
    comment_query: CommentSacctQuerier,
    grace: timedelta = RESERVATION_ABSENCE_GRACE,
    accepted_submit_grace: timedelta | None = None,
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
        accepted_submit_reconcile = _accepted_submit_reconcile_job(store, job)
        file_forecast_cohort = bool(
            getattr(store, "supports_accepted_submit_reconcile", False)
            and _is_forecast_cohort_job(job)
            and isinstance(getattr(job, "cohort_members", None), Sequence)
            and not isinstance(getattr(job, "cohort_members", None), str | bytes)
            and getattr(job, "cohort_members", None)
        )
        if file_forecast_cohort and not accepted_submit_reconcile:
            write_count = _record_file_reconciliation(store, job.job_id, "identity_mismatch_blocked")
            outcomes.append(
                ReservationReconcileOutcome(
                    job_id=job.job_id,
                    idempotency_key=str(idempotency_key),
                    action="identity_mismatch_blocked",
                    status=str(job.status),
                    reconciliation_source="slurm_exact_comment",
                    reconciliation_decision="identity_mismatch_blocked",
                    durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                    durable_write_count=write_count,
                )
            )
            continue

        if accepted_submit_reconcile and getattr(job, "submit_outcome", None) is None:
            recorder = getattr(store, "record_pipeline_job_reconciliation", None)
            if callable(recorder):
                recorder(
                    job.job_id,
                    submit_outcome="submit_result_ambiguous",
                    status=str(job.status),
                )

        try:
            expected_user = str(getattr(job, "expected_slurm_user", None) or "")
            expected_account = str(getattr(job, "expected_slurm_account", None) or "")
            proof = _query_comment_accounting_proof(
                comment_query,
                str(idempotency_key),
                expected_user=expected_user if accepted_submit_reconcile else "",
                expected_account=expected_account if accepted_submit_reconcile else "",
            )
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
            write_count = (
                _record_file_reconciliation(store, job.job_id, "accounting_unavailable")
                if accepted_submit_reconcile
                else 0
            )
            outcomes.append(
                ReservationReconcileOutcome(
                    job_id=job.job_id,
                    idempotency_key=str(idempotency_key),
                    action="query_unavailable",
                    status=str(job.status),
                    reconciliation_source="slurm_exact_comment" if accepted_submit_reconcile else None,
                    reconciliation_decision="accounting_unavailable" if accepted_submit_reconcile else None,
                    durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                    durable_write_count=write_count,
                )
            )
            continue
        if accepted_submit_reconcile and proof.kind == "ambiguous":
            write_count = _record_file_reconciliation(store, job.job_id, "multiple_matches_blocked")
            outcomes.append(
                ReservationReconcileOutcome(
                    job_id=job.job_id,
                    idempotency_key=str(idempotency_key),
                    action="multiple_matches_blocked",
                    status=str(job.status),
                    reconciliation_source="slurm_exact_comment",
                    reconciliation_decision="multiple_matches_blocked",
                    match_count=min(len(proof.records), MAX_EXACT_COMMENT_MATCHES + 1),
                    durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                    durable_write_count=write_count,
                )
            )
            continue
        if accepted_submit_reconcile and proof.kind == "foreign_collision":
            write_count = _record_file_reconciliation(store, job.job_id, "identity_mismatch_blocked")
            outcomes.append(
                ReservationReconcileOutcome(
                    job_id=job.job_id,
                    idempotency_key=str(idempotency_key),
                    action="identity_mismatch_blocked",
                    status=str(job.status),
                    reconciliation_source="slurm_exact_comment",
                    reconciliation_decision="identity_mismatch_blocked",
                    match_count=min(len(proof.records), MAX_EXACT_COMMENT_MATCHES + 1),
                    durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                    durable_write_count=write_count,
                )
            )
            continue
        record = (
            proof.records[0]
            if proof.records and (proof.kind == "owned_match" or not accepted_submit_reconcile)
            else None
        )
        if (
            accepted_submit_reconcile
            and record is not None
            and not _reserved_record_identity_matches(store, record, job, str(idempotency_key))
        ):
            write_count = _record_file_reconciliation(store, job.job_id, "identity_mismatch_blocked")
            outcomes.append(
                ReservationReconcileOutcome(
                    job_id=job.job_id,
                    idempotency_key=str(idempotency_key),
                    action="identity_mismatch_blocked",
                    status=str(job.status),
                    reconciliation_source="slurm_exact_comment",
                    reconciliation_decision="identity_mismatch_blocked",
                    match_count=1,
                    durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                    durable_write_count=write_count,
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
            # reservation's immutable current-attempt anchor is younger than
            # `grace`, defer
            # demotion to a later pass rather than risk demoting an in-flight job
            # into a reclaim+re-sbatch double-submit. Accepted-submit cohorts use
            # submission_attempt_started_at: reconciliation evidence may refresh
            # updated_at, but cannot extend this attempt's grace. Legacy rows keep
            # updated_at with created_at as their compatibility fallback.
            anchor = (
                getattr(job, "submission_attempt_started_at", None)
                if accepted_submit_reconcile
                else getattr(job, "updated_at", None)
            )
            if anchor is None:
                anchor = getattr(job, "created_at", None)
            if anchor is not None:
                if anchor.tzinfo is None:
                    anchor = anchor.replace(tzinfo=UTC)
                absence_grace = (
                    accepted_submit_grace
                    if accepted_submit_reconcile and accepted_submit_grace is not None
                    else grace
                )
                if now() - anchor < absence_grace:
                    write_count = (
                        _record_file_reconciliation(store, job.job_id, "absence_deferred")
                        if accepted_submit_reconcile
                        else 0
                    )
                    outcomes.append(
                        ReservationReconcileOutcome(
                            job_id=job.job_id,
                            idempotency_key=str(idempotency_key),
                            action=ABSENCE_UNCONFIRMED_ACTION,
                            status=str(job.status),
                            reconciliation_source="slurm_exact_comment" if accepted_submit_reconcile else None,
                            reconciliation_decision="absence_deferred" if accepted_submit_reconcile else None,
                            durable_write_kind="pipeline_job_reconciliation" if write_count else None,
                            durable_write_count=write_count,
                        )
                    )
                    continue
            retry_permitted = False
            permit_retry = getattr(store, "permit_pipeline_job_retry", None)
            if accepted_submit_reconcile and callable(permit_retry):
                retry_write_count = int(permit_retry(job.job_id))
                retry_permitted = retry_write_count > 0
            else:
                store.update_job_status(
                    job.job_id,
                    RESERVATION_LOST_STATUS,
                    error_code="SLURM_RESERVATION_LOST",
                    error_message=(
                        "sbatch acceptance for reservation "
                        f"idempotency_key={idempotency_key} could not be confirmed in accounting."
                    ),
                )
                retry_permitted = True
                retry_write_count = 1
            outcomes.append(
                ReservationReconcileOutcome(
                    job_id=job.job_id,
                    idempotency_key=str(idempotency_key),
                    action="absence_retry_permitted" if accepted_submit_reconcile else "reservation_lost",
                    status=RESERVATION_LOST_STATUS if retry_permitted else str(job.status),
                    reconciliation_source="slurm_exact_comment" if accepted_submit_reconcile else None,
                    reconciliation_decision="absence_retry_permitted" if accepted_submit_reconcile else None,
                    durable_write_kind=(
                        "forecast_retry_permission" if accepted_submit_reconcile else "pipeline_job_status"
                    ),
                    durable_write_count=retry_write_count,
                )
            )
            continue

        bound = store.bind_reservation(
            str(idempotency_key),
            slurm_job_id=record.slurm_job_id,
            status="submitted",
        )
        write_count = 1 if bound is not None else 0
        if accepted_submit_reconcile and bound is not None:
            recorder = getattr(store, "record_pipeline_job_reconciliation", None)
            if callable(recorder):
                recorded = recorder(
                    job.job_id,
                    submit_outcome="accepted",
                    reconciliation_decision="matched_bound",
                    matched_slurm_job_id=record.slurm_job_id,
                )
                write_count += int(recorded is not None)
        bound_status = "submitted" if bound is not None else str(job.status)
        outcomes.append(
            ReservationReconcileOutcome(
                job_id=job.job_id,
                idempotency_key=str(idempotency_key),
                action="bound",
                status=bound_status,
                slurm_job_id=record.slurm_job_id,
                reconciliation_source="slurm_exact_comment" if accepted_submit_reconcile else None,
                reconciliation_decision="matched_bound" if accepted_submit_reconcile else None,
                matched_slurm_job_id=record.slurm_job_id if accepted_submit_reconcile else None,
                match_count=1 if accepted_submit_reconcile else None,
                durable_write_kind="reservation_bind" if write_count else None,
                durable_write_count=write_count,
            )
        )

    return outcomes


def _comment_query_records(
    value: CommentAccountingResult | SacctRecord | Sequence[SacctRecord] | None,
    *,
    expected_user: str = "",
    expected_account: str = "",
) -> tuple[SacctRecord, ...]:
    if value is None:
        return ()
    values = (value,) if isinstance(value, SacctRecord) else value
    owned = (
        item
        for item in values
        if isinstance(item, SacctRecord)
        and (not expected_user or item.user == expected_user)
        and (not expected_account or item.account == expected_account)
    )
    return tuple(item for _, item in zip(range(MAX_EXACT_COMMENT_MATCHES + 1), owned, strict=False))


def _bounded_comment_records(
    value: CommentAccountingResult | SacctRecord | Sequence[SacctRecord] | None,
) -> tuple[SacctRecord, ...]:
    if value is None:
        return ()
    values = (value,) if isinstance(value, SacctRecord) else value
    return tuple(
        item
        for _, item in zip(
            range(MAX_EXACT_COMMENT_MATCHES + 1),
            (entry for entry in values if isinstance(entry, SacctRecord)),
            strict=False,
        )
    )


def _classify_global_comment_records(
    records: tuple[SacctRecord, ...],
    *,
    expected_user: str,
    expected_account: str,
) -> _CommentAccountingProof:
    if not records:
        return _CommentAccountingProof("global_absence")
    owned = tuple(
        record
        for record in records
        if (not expected_user or record.user == expected_user)
        and (not expected_account or record.account == expected_account)
    )
    foreign = tuple(record for record in records if record not in owned)
    if foreign:
        return _CommentAccountingProof("foreign_collision", records)
    if len(owned) > 1:
        return _CommentAccountingProof("ambiguous", owned)
    if owned:
        return _CommentAccountingProof("owned_match", owned)
    return _CommentAccountingProof("global_absence")


def _query_comment_accounting_proof(
    comment_query: CommentSacctQuerier,
    idempotency_key: str,
    *,
    expected_user: str,
    expected_account: str,
) -> _CommentAccountingProof:
    """Obtain an owner match or an authoritative global exact-comment proof."""

    supports_scope = _comment_query_accepts_scope(comment_query)
    if not supports_scope:
        # A legacy one-argument fake represents a global collection. Never
        # owner-filter it into a false zero-match proof.
        global_value = comment_query(idempotency_key)
        return _classify_global_comment_records(
            _bounded_comment_records(global_value),
            expected_user=expected_user,
            expected_account=expected_account,
        )

    owner_value = comment_query(
        idempotency_key,
        expected_user=expected_user or None,
        expected_account=expected_account or None,
    )
    owner_records = _bounded_comment_records(owner_value)
    if isinstance(owner_value, CommentAccountingResult) and owner_value.scope == "global":
        return _classify_global_comment_records(
            owner_records,
            expected_user=expected_user,
            expected_account=expected_account,
        )
    owned = _comment_query_records(
        owner_records,
        expected_user=expected_user,
        expected_account=expected_account,
    )
    if len(owned) > 1:
        return _CommentAccountingProof("ambiguous", owned)
    if owner_records:
        if not owned:
            return _CommentAccountingProof("foreign_collision", owner_records)
        owner_candidate = owned[0]
    else:
        owner_candidate = None

    # Neither owner-scoped zero nor one owner candidate proves global identity.
    # A bounded all-ownership view must prove either one identical owned master
    # or zero exact-comment rows before bind/retry can proceed.
    global_value = comment_query(
        idempotency_key,
        expected_user=None,
        expected_account=None,
    )
    if isinstance(global_value, CommentAccountingResult) and global_value.scope != "global":
        raise ReconcileQueryUnavailable("exact-comment adapter did not provide global proof")
    global_proof = _classify_global_comment_records(
        _bounded_comment_records(global_value),
        expected_user=expected_user,
        expected_account=expected_account,
    )
    if owner_candidate is not None and global_proof.kind == "owned_match":
        global_candidate = global_proof.records[0]
        if (
            global_candidate.slurm_job_id != owner_candidate.slurm_job_id
            or global_candidate.user != owner_candidate.user
            or global_candidate.account != owner_candidate.account
            or global_candidate.comment != owner_candidate.comment
        ):
            return _CommentAccountingProof(
                "foreign_collision", (owner_candidate, global_candidate)
            )
    elif owner_candidate is not None and global_proof.kind == "global_absence":
        return _CommentAccountingProof("foreign_collision", (owner_candidate,))
    return global_proof


def _comment_query_accepts_scope(comment_query: CommentSacctQuerier) -> bool:
    try:
        parameters = tuple(inspect.signature(comment_query).parameters.values())
    except (TypeError, ValueError):
        return False
    parameter_names = {parameter.name for parameter in parameters}
    return any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters) or {
        "expected_user",
        "expected_account",
    }.issubset(parameter_names)


def _query_comment_accounting(
    comment_query: CommentSacctQuerier,
    idempotency_key: str,
    *,
    expected_user: str,
    expected_account: str,
) -> SacctRecord | Sequence[SacctRecord] | None:
    """Compatibility wrapper for direct adapter callers."""

    if _comment_query_accepts_scope(comment_query):
        return comment_query(
            idempotency_key,
            expected_user=expected_user or None,
            expected_account=expected_account or None,
        )
    return comment_query(idempotency_key)


def _record_file_reconciliation(store: Any, job_id: str, decision: str) -> int:
    recorder = getattr(store, "record_pipeline_job_reconciliation", None)
    if callable(recorder):
        return int(
            recorder(
                job_id,
                reconciliation_decision=decision,
                matched_slurm_job_id=None,
            )
            is not None
        )
    return 0


def _reserved_record_identity_matches(store: Any, record: SacctRecord, job: Any, idempotency_key: str) -> bool:
    identity = vars(job) if hasattr(job, "__dict__") else {}
    if not forecast_cohort_identity_is_valid(identity) or not _file_cohort_runtime_identity_matches(store, identity):
        return False
    if idempotency_key_from_comment(record.comment) != idempotency_key:
        return False
    if not SLURM_JOB_ID_RE.fullmatch(str(record.slurm_job_id)):
        return False
    expected_token = _expected_job_name_token(getattr(job, "stage", None), getattr(job, "job_type", None))
    if not _identity_matches(record, expected_token, job, require_durable_identity=False):
        return False
    for record_value, job_field in (
        (record.run_id, "run_id"),
        (record.stage, "stage"),
        (record.pipeline_job_id, "job_id"),
    ):
        expected = getattr(job, job_field, None)
        if record_value not in (None, "") and expected not in (None, "") and str(record_value) != str(expected):
            return False
    expected_user = str(getattr(job, "expected_slurm_user", None) or "")
    expected_account = str(getattr(job, "expected_slurm_account", None) or "")
    if bool(getattr(job, "slurm_ownership_required", False)) and (
        not expected_user or not expected_account or not record.user or not record.account
    ):
        return False
    if expected_user and record.user != expected_user:
        return False
    if expected_account and record.account != expected_account:
        return False
    return True


def _accepted_submit_reconcile_job(store: Any, job: Any) -> bool:
    identity = vars(job) if hasattr(job, "__dict__") else {}
    pre_outcome_reservation = identity.get("submit_outcome") is None
    return bool(
        getattr(store, "supports_accepted_submit_reconcile", False)
        and _is_forecast_cohort_job(job)
        and forecast_cohort_identity_is_valid(identity)
        and (pre_outcome_reservation or _file_cohort_runtime_identity_matches(store, identity))
    )
