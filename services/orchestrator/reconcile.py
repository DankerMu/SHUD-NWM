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

from services.slurm_gateway.models import TERMINAL_STATUSES, SlurmJobStatus
from services.slurm_gateway.real_backend import (
    SLURM_STATE_MAP,
    _normalize_slurm_state,
    map_slurm_error_code,
)

LOGGER = logging.getLogger(__name__)

RECONCILE_UNVERIFIED_STATUS = "reconcile_unverified"


@dataclass(frozen=True)
class SacctRecord:
    """Minimal authoritative accounting view of a Slurm job for reconcile."""

    slurm_job_id: str
    raw_state: str
    job_name: str
    exit_code: str | None = None


# A sacct querier maps a slurm_job_id to its accounting record (or None when the
# job is unknown to accounting). Injectable so tests can supply a fake without a
# real cluster.
SacctQuerier = Callable[[str], "SacctRecord | None"]


def default_sacct_querier(slurm_bin_path: str = "") -> SacctQuerier:
    """Build a sacct querier that shells out to the real ``sacct`` binary.

    Uses ``--parsable2 --noheader`` with the same field shape the gateway uses
    elsewhere (``JobID|JobName|State|ExitCode``), and returns the master job's
    record (the row whose ``JobID`` has no ``_<task>``/``.<step>`` suffix).
    """

    sacct = f"{slurm_bin_path.rstrip('/')}/sacct" if slurm_bin_path else "sacct"

    def _query(slurm_job_id: str) -> SacctRecord | None:
        command = [
            sacct,
            "--parsable2",
            "--noheader",
            "--format=JobID,JobName,State,ExitCode",
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
            return None
        if result.returncode != 0:
            LOGGER.warning(
                "sacct returned %s for %s", result.returncode, slurm_job_id
            )
            return None
        return _parse_master_sacct_row(result.stdout, slurm_job_id)

    return _query


def _parse_master_sacct_row(stdout: str, slurm_job_id: str) -> SacctRecord | None:
    """Parse the master job row (JobID without ``_``/``.`` suffix) from sacct."""

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split("|")
        if len(fields) < 3:
            continue
        job_id = fields[0]
        # Skip array task rows (12345_0) and job step rows (12345.batch).
        if "_" in job_id or "." in job_id:
            continue
        if job_id != str(slurm_job_id):
            continue
        return SacctRecord(
            slurm_job_id=job_id,
            job_name=fields[1],
            raw_state=fields[2],
            exit_code=fields[3] if len(fields) > 3 else None,
        )
    return None


@dataclass(frozen=True)
class ReconcileOutcome:
    job_id: str
    slurm_job_id: str
    action: str  # "terminal" | "still_running" | "unverified"
    status: str


def _expected_job_name_token(stage: str | None, job_type: str | None) -> str | None:
    """The ``nhms_<stage>`` token sbatch templates set as ``--job-name``."""

    token = (stage or job_type or "").strip()
    if not token:
        return None
    return f"nhms_{token}"


def _identity_matches(record: SacctRecord, expected_token: str | None) -> bool:
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
    return record.job_name.strip() == expected_token


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

        record = sacct_query(str(slurm_job_id))
        expected_token = _expected_job_name_token(job.stage, job.job_type)

        if record is None or not _identity_matches(record, expected_token):
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

        normalized = _normalize_slurm_state(record.raw_state)
        slurm_status = SLURM_STATE_MAP.get(normalized, SlurmJobStatus.FAILED)

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
