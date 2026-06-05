"""Shape-validation tests for comment-based sacct row parsing (issue #300 LOW-2)
and the dead-session engine-dispose path of the reconcile store reset (LOW-1).

LOW-2: a malformed sacct JobID (e.g. leading ``_``) normalizes to ``""`` or a
non-numeric string. ``_parse_comment_sacct_rows`` MUST skip such rows and keep
scanning rather than short-circuit-returning a bogus empty-id ``SacctRecord``,
which would otherwise manufacture a false confirmed-absent / mis-bind.
"""

from __future__ import annotations

from services.orchestrator.reconcile import (
    SacctRecord,
    _parse_comment_sacct_rows,
)

_COMMENT = "nwm:abc123"


def _row(job_id: str, comment: str = _COMMENT) -> str:
    # JobID|JobName|State|ExitCode|Comment
    return f"{job_id}|stage.sh|RUNNING|0:0|{comment}"


def test_malformed_jobid_skipped_then_valid_array_row_returned() -> None:
    """(a) A malformed leading-``_`` row whose Comment matches is SKIPPED, and a
    SUBSEQUENT well-formed array row with the same Comment is the one returned —
    proving no short-circuit / no false-absent."""
    stdout = "\n".join(
        [
            _row("_4"),  # malformed: normalizes to "" -> must be skipped
            _row("99001_4"),  # real array element -> master id 99001
        ]
    )
    record = _parse_comment_sacct_rows(stdout, _COMMENT)
    assert record is not None
    assert record.slurm_job_id == "99001"
    assert record.comment == _COMMENT


def test_malformed_jobid_no_valid_followup_returns_none() -> None:
    """(b) A malformed matching row with NO valid follow-up returns None
    (confirmed-absent), NOT a bogus empty-id record."""
    stdout = "\n".join(
        [
            _row("_4"),  # normalizes to "" -> skipped
            _row("abc_4"),  # non-numeric master -> skipped
            _row("12345_0", comment="other:comment"),  # comment mismatch
        ]
    )
    assert _parse_comment_sacct_rows(stdout, _COMMENT) is None


def test_normal_array_row_normalizes_to_master_id() -> None:
    """(c) Regression: a normal ``<master>_<task>`` array row still normalizes
    to the bare master id and returns correctly."""
    record = _parse_comment_sacct_rows(_row("778899_12"), _COMMENT)
    assert record is not None
    assert record.slurm_job_id == "778899"


def test_single_job_id_passes_through() -> None:
    """Regression: a single-job id (no ``_``) is well-formed and returned."""
    record = _parse_comment_sacct_rows(_row("55555"), _COMMENT)
    assert isinstance(record, SacctRecord)
    assert record.slurm_job_id == "55555"


# --- LOW-1: dead-session engine dispose on reconcile-store reset ------------


class _RecordingBind:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class _DeadSession:
    """Session whose rollback raises (connection truly dead)."""

    def __init__(self, bind: _RecordingBind) -> None:
        self._bind = bind
        self.closed = False

    def rollback(self) -> None:
        raise RuntimeError("connection dead")

    def get_bind(self) -> _RecordingBind:
        return self._bind

    def close(self) -> None:
        self.closed = True


class _FakeStore:
    def __init__(self, session: _DeadSession) -> None:
        self.session = session


def test_reset_reconcile_store_disposes_dead_engine() -> None:
    from services.orchestrator.scheduler import ProductionScheduler

    bind = _RecordingBind()
    session = _DeadSession(bind)
    store = _FakeStore(session)

    scheduler = ProductionScheduler.__new__(ProductionScheduler)
    scheduler._reconcile_store = store  # type: ignore[attr-defined]

    scheduler._reset_reconcile_store_after_error()

    assert bind.disposed is True
    assert session.closed is True
    assert scheduler._reconcile_store is None  # type: ignore[attr-defined]
