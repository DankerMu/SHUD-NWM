"""Durable retry-attempt derivation from pipeline job identity.

The journal's clean-reservation invariant forces ``retry_count`` back to 0 on
master rows, so the only durable per-stage attempt record is the ``_retry_<n>``
suffix carried by the job id.  Production ids stack suffixes
(``..._retry_1_retry_2``), so the LAST suffix is authoritative.

This module is the single owner of that parsing; it deliberately has no
dependencies so both the DB-free journal and the scheduler read side can use it.
"""

from __future__ import annotations

from typing import Any

RETRY_JOB_ID_MARKER = "_retry_"


def split_retry_job_identity(job_id: str | None) -> tuple[str, int]:
    """Split ``job_id`` into its retry base and the last ``_retry_<n>`` attempt.

    Ids without a parsable trailing suffix are returned unchanged with attempt 0.
    """

    text = str(job_id or "")
    if RETRY_JOB_ID_MARKER not in text:
        return text, 0
    base, attempt = text.rsplit(RETRY_JOB_ID_MARKER, maxsplit=1)
    try:
        return base, max(int(attempt), 0)
    except ValueError:
        return text, 0


def retry_suffix_attempt(job_id: str | None) -> int:
    """Return the attempt number encoded in the last ``_retry_<n>`` suffix."""

    return split_retry_job_identity(job_id)[1]


def effective_retry_attempt(job_id: str | None, recorded_count: Any = None) -> int:
    """Return the effective attempt for a job: recorded count or id suffix, whichever is higher."""

    return max(_coerce_attempt(recorded_count), retry_suffix_attempt(job_id))


def _coerce_attempt(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
