"""M24 §3A durable two-phase reservation for concurrent submit-and-return.

The scheduler submits independent candidates (different basin/source/cycle)
concurrently. To stay within the configured bound with no duplicate submission
across overlapping passes or across the submit-crash window, every candidate
goes through a durable two-phase protocol on ``ops.pipeline_job``:

1. ``reserve`` (inside the pass lock, BEFORE ``sbatch``): write a durable row
   keyed by a stable ``idempotency_key`` with ``status='reserved'`` and no
   ``slurm_job_id``. The partial unique index on ``idempotency_key`` makes this
   at-most-once even under a race; ``reserve_pipeline_job`` is
   ``ON CONFLICT DO NOTHING`` and returns the existing row.

2. ``bind`` (after ``sbatch`` accepts): atomically set ``slurm_job_id`` and
   ``status='submitted'`` ``WHERE idempotency_key=... AND slurm_job_id IS NULL``.

An overlapping pass queries ``candidate_state(idempotency_key)`` and skips any
candidate already ``reserved``/``submitted``/``running`` — even before the job
appears in ``squeue``/``sacct``.

The idempotency_key is derived from stable candidate identity so it is constant
for the same candidate+stage across passes:

    f"{source_id}:{cycle_id}:{basin_id}:{stage}"
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from services.orchestrator.persistence import (
    RESERVATION_ACTIVE_STATUSES,
    RESERVED_STATUS,
)

# Slurm ``--comment`` (and sacct ``Comment``) carries the idempotency_key so the
# crash-window reconcile can match an unbound reservation back to the job that
# sbatch actually accepted, by idempotency rather than a never-recorded job id.
SLURM_COMMENT_PREFIX = "nhms_idem:"


def candidate_idempotency_key(
    *,
    source_id: str,
    cycle_id: str,
    basin_id: str,
    stage: str,
) -> str:
    """Stable per-candidate+stage idempotency key (constant across passes)."""

    return f"{source_id}:{cycle_id}:{basin_id}:{stage}"


def slurm_comment_for(idempotency_key: str) -> str:
    """The ``--comment`` value sbatch records so reconcile can match by key."""

    return f"{SLURM_COMMENT_PREFIX}{idempotency_key}"


def idempotency_key_from_comment(comment: str | None) -> str | None:
    """Recover an idempotency_key from a sacct ``Comment`` field, if present."""

    if not comment:
        return None
    comment = comment.strip()
    if comment.startswith(SLURM_COMMENT_PREFIX):
        return comment[len(SLURM_COMMENT_PREFIX) :]
    return None


def reservation_is_active(status: str | None) -> bool:
    """True when an existing reservation must NOT be re-submitted."""

    return status in RESERVATION_ACTIVE_STATUSES


@dataclass(frozen=True)
class ReservationResult:
    """Outcome of phase 1 ``reserve``."""

    idempotency_key: str
    job_id: str
    status: str
    created: bool  # True => this pass wrote the reservation; False => reused.

    @property
    def already_inflight(self) -> bool:
        return (not self.created) and reservation_is_active(self.status)


@dataclass
class SubmitOverlapReceipt:
    """Evidence that ≥2 candidate submits overlapped in time.

    The Evidence Floor requires proof concurrency happened, not a scope note.
    Each entry records a candidate's submit start/end wall-clock window; the
    receipt computes whether any two windows overlap.
    """

    submissions: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        idempotency_key: str,
        started_at: float,
        finished_at: float,
        candidate_id: str | None = None,
    ) -> None:
        self.submissions.append(
            {
                "idempotency_key": idempotency_key,
                "candidate_id": candidate_id,
                "submit_started_at": started_at,
                "submit_finished_at": finished_at,
            }
        )

    @property
    def overlapping(self) -> bool:
        windows = sorted(
            (s["submit_started_at"], s["submit_finished_at"]) for s in self.submissions
        )
        for previous, current in zip(windows, windows[1:], strict=False):
            if current[0] < previous[1]:  # next starts before previous finishes.
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "concurrent_submit_count": len(self.submissions),
            "overlapping": self.overlapping,
            "submissions": list(self.submissions),
        }


def reserve_candidate(
    repository: Any,
    *,
    idempotency_key: str,
    job_id: str,
    run_id: str | None,
    cycle_id: str | None,
    job_type: str,
    model_id: str | None,
    stage: str | None,
    candidate_id: str | None = None,
) -> ReservationResult:
    """Phase 1: durably reserve a candidate before ``sbatch``.

    Idempotent: if the idempotency_key already maps to a row, that row is reused
    (no second row, no re-submit when it is already active).
    """

    existing = repository.query_candidate_state(idempotency_key)
    if existing is not None:
        return ReservationResult(
            idempotency_key=idempotency_key,
            job_id=str(existing["job_id"]),
            status=str(existing["status"]),
            created=False,
        )

    record = repository.reserve_pipeline_job(
        {
            "job_id": job_id,
            "run_id": run_id,
            "cycle_id": cycle_id,
            "job_type": job_type,
            "model_id": model_id,
            "stage": stage,
            "status": RESERVED_STATUS,
            "idempotency_key": idempotency_key,
            "candidate_id": candidate_id,
        }
    )
    return ReservationResult(
        idempotency_key=idempotency_key,
        job_id=str(record["job_id"]),
        status=str(record["status"]),
        # DO NOTHING under a race returns the pre-existing row: created only when
        # the returned row is still ``reserved`` and carries our job_id.
        created=str(record["job_id"]) == job_id and str(record["status"]) == RESERVED_STATUS,
    )


def bind_reservation(
    repository: Any,
    *,
    idempotency_key: str,
    slurm_job_id: str,
    status: str = "submitted",
    array_task_id: int | None = None,
) -> dict[str, Any] | None:
    """Phase 2: atomically bind ``slurm_job_id`` onto the reservation.

    The bind is conditional on ``slurm_job_id IS NULL`` so it is a no-op if a
    concurrent pass already bound the same reservation.
    """

    return repository.bind_pipeline_job_reservation(
        idempotency_key,
        slurm_job_id=slurm_job_id,
        status=status,
        array_task_id=array_task_id,
    )


def run_concurrent_submissions(
    submitters: list[Callable[[], Any]],
    *,
    max_workers: int,
    receipt: SubmitOverlapReceipt | None = None,
) -> list[Any]:
    """Run candidate submitters concurrently within ``max_workers``.

    Submit-and-return: a candidate is submitted without waiting for any prior
    candidate's terminal state. The reservation unique index is the correctness
    backstop; this only governs concurrency. Errors are returned in-place (not
    raised) so one candidate's failure does not abort siblings.
    """

    from concurrent.futures import ThreadPoolExecutor

    if not submitters:
        return []

    del receipt  # the submitter records its own window via ``timed_submission``.
    workers = max(1, min(int(max_workers), len(submitters)))
    results: list[Any] = [None] * len(submitters)

    def _run(index: int) -> None:
        try:
            results[index] = submitters[index]()
        except Exception as error:  # noqa: BLE001 - isolate per-candidate failure.
            results[index] = error

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_run, range(len(submitters))))
    return results


def timed_submission(
    submitter: Callable[[], Any],
    *,
    receipt: SubmitOverlapReceipt,
    idempotency_key: str,
    candidate_id: str | None = None,
) -> Callable[[], Any]:
    """Wrap a submitter so its submit window is recorded on ``receipt``."""

    def _wrapped() -> Any:
        started = time.monotonic()
        try:
            return submitter()
        finally:
            receipt.record(
                idempotency_key=idempotency_key,
                started_at=started,
                finished_at=time.monotonic(),
                candidate_id=candidate_id,
            )

    return _wrapped
