"""#3176: behaviour pins for the reserved-unbound reconcile decomposition.

``reconcile_reserved_unbound_jobs`` is the submit-crash-window state machine
(double-submit safety). These pins were written against the monolithic body and
must stay green, unedited, after it is split into ordered phase helpers. They
drive the public facade over a synthetic store and a scripted comment querier:

* The store records every call it receives in one ordered trace. Its methods
  have exact signatures (no ``**kwargs``, no ``__getattr__``), because the
  reconcile body picks branches from the store's real surface
  (``getattr`` probes and ``_callable_accepts_keyword``). An optional API is
  removed by a subclass that sets it to ``None``, which is what the
  ``callable(...)`` probes see for a store that lacks it.
* The querier's ``capability()`` counts and does NOT cache, so a per-row
  re-probe is visible (the default querier caches per instance).
* ``now`` is a clock that advances on every call, so a snapshot is visible.

Expected values are the outcome vocabulary and write order of the frozen
reconcile contract (proposal "Must preserve"), not values read back from the
code under test.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import MISSING, fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from services.orchestrator import reconcile as reconcile_module
from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION
from tests.gateway_reconcile_helpers import _versioned_master_reservation_record

ANCHOR = datetime(2026, 7, 12, tzinfo=UTC)
GRACE = timedelta(minutes=2)
YOUNG = ANCHOR + timedelta(seconds=30)
OLD = ANCHOR + timedelta(hours=1)
COVERAGE = (ANCHOR - timedelta(hours=1), ANCHOR + timedelta(hours=2))

GFS_KEY = "cycle_gfs_2026071200_forecast_fixture:forecast"
GFS_JOB = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
IFS_KEY = "cycle_ifs_2026071200_forecast_fixture:forecast"
IFS_JOB = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
FORCING_RUN = "cycle_gfs_2026071200_convert_cohort_36e4f7b9bf80"
FORCING_JOB = f"job_{FORCING_RUN}_forcing"
FORCING_KEY = f"{FORCING_RUN}:forcing"
#: Independent literal of ``forcing_attempt_comment_for(FORCING_KEY, 1)``.
FORCING_COMMENT = f"nhms_forcing_attempt:{FORCING_KEY}:a1"
LEGACY_A_KEY = "cycle_legacy_2026071200_a:hydro"
LEGACY_B_KEY = "cycle_legacy_2026071200_b:hydro"


# --- synthetic rows -------------------------------------------------------------------------


def _accepted_job(source_id: str = "gfs", *, submit_outcome: str | None = "submit_result_ambiguous") -> Any:
    record = _versioned_master_reservation_record(
        created_at=ANCHOR,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
        source_id=source_id,
    )
    record["submit_outcome"] = submit_outcome
    return SimpleNamespace(**record, status="reserved", slurm_job_id=None)


def _forcing_job(*, submit_outcome: str | None = "submit_result_ambiguous") -> Any:
    return SimpleNamespace(
        job_id=FORCING_JOB,
        run_id=FORCING_RUN,
        source_id="GFS",
        cycle_id="gfs_2026071200",
        job_type="produce_forcing_array",
        stage="forcing",
        idempotency_key=FORCING_KEY,
        slurm_comment=FORCING_COMMENT,
        submission_attempt=1,
        submission_attempt_started_at=ANCHOR,
        cohort_members=[
            {
                "array_task_id": 0,
                "candidate_id": "gfs:2026-07-12T00:00:00Z:model_0:forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026071200_model_0",
                "model_id": "model_0",
                "basin_id": "basin_0",
                "scenario_id": "forecast_gfs_deterministic",
                "restart_stage": "forcing",
            }
        ],
        slurm_ownership_required=True,
        expected_slurm_user="scheduler",
        expected_slurm_account="account",
        submit_outcome=submit_outcome,
        status="reserved",
        slurm_job_id=None,
        created_at=ANCHOR,
        updated_at=ANCHOR,
    )


def _legacy_job(key: str | None, job_id: str) -> Any:
    return SimpleNamespace(
        job_id=job_id,
        run_id=f"run_{job_id}",
        stage="hydro",
        job_type="run_shud",
        idempotency_key=key,
        status="reserved",
        slurm_job_id=None,
        created_at=ANCHOR,
        updated_at=ANCHOR,
    )


def _record(slurm_job_id: str, *, job_name: str, comment: str | None, **extra: Any) -> Any:
    return reconcile_module.SacctRecord(
        slurm_job_id=slurm_job_id,
        raw_state="RUNNING",
        job_name=job_name,
        comment=comment,
        user="scheduler",
        account="account",
        **extra,
    )


def _global(*records: Any) -> Any:
    return reconcile_module.CommentAccountingResult(
        tuple(records),
        scope="global",
        coverage_start=COVERAGE[0],
        coverage_end=COVERAGE[1],
        coverage_complete=True,
    )


def _fallback(*records: Any, basis: str | None) -> Any:
    return reconcile_module.CommentAccountingResult(
        tuple(records),
        scope="owner",
        coverage_start=COVERAGE[0],
        coverage_end=COVERAGE[1],
        fallback_name_window=True,
        fallback_match_basis=basis,
    )


def _controller(*records: Any) -> Any:
    return reconcile_module.CommentAccountingResult(tuple(records), scope="owner", controller_exact_comment=True)


# --- recording collaborators ------------------------------------------------------------------


def _transition(value: Any) -> tuple[Any, ...]:
    return (
        value.submit_outcome,
        value.reconciliation_source,
        value.reconciliation_decision,
        value.matched_slurm_job_id,
        value.reconciliation_reason_class,
        value.status,
        value.identity_blocked_streak,
    )


class _Store:
    """A reserved-unbound store whose every call lands in one ordered trace."""

    supports_accepted_submit_reconcile = True

    def __init__(
        self,
        jobs: list[Any],
        trace: list[tuple[Any, ...]],
        *,
        journal_faults: frozenset[tuple[str, str | None]] = frozenset(),
    ) -> None:
        self.jobs = jobs
        self.trace = trace
        self.journal_faults = journal_faults

    def query_reserved_unbound_jobs(self) -> list[Any]:
        self.trace.append(("query_reserved_unbound_jobs",))
        return list(self.jobs)

    def get_reserved_unbound_job(self, job_id: str) -> Any:
        self.trace.append(("get_reserved_unbound_job", job_id))
        return next((job for job in self.jobs if job.job_id == job_id), None)

    def forecast_cohort_runtime_identity_matches(self, identity: Any) -> bool:
        self.trace.append(("forecast_cohort_runtime_identity_matches", identity["job_id"]))
        return True

    def transition_pipeline_job_submit_evidence(
        self,
        job_id: str,
        transition: Any,
        *,
        expected_submission_attempt: int,
        expected_statuses: tuple[str, ...],
        require_unbound: bool,
        accepted_submit_contract_version: str | None = None,
    ) -> Any:
        self.trace.append(
            (
                "transition_pipeline_job_submit_evidence",
                job_id,
                _transition(transition),
                expected_submission_attempt,
                expected_statuses,
                require_unbound,
                accepted_submit_contract_version,
            )
        )
        if (job_id, transition.reconciliation_decision) in self.journal_faults:
            from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

            raise FileOrchestrationJournalError("file_journal_fixture_fault", field="pipeline_jobs")
        return SimpleNamespace(wrote=True)

    def permit_pipeline_job_retry(
        self,
        job_id: str,
        *,
        expected_submission_attempt: int,
        expected_status: str,
        expected_submission_attempt_started_at: datetime | None,
        accepted_submit_contract_version: str,
    ) -> int:
        self.trace.append(
            (
                "permit_pipeline_job_retry",
                job_id,
                expected_submission_attempt,
                expected_status,
                expected_submission_attempt_started_at,
                accepted_submit_contract_version,
            )
        )
        return 1

    def update_job_status(self, job_id: str, status: str, *, error_code: str, error_message: str) -> None:
        self.trace.append(("update_job_status", job_id, status, error_code, error_message))

    def commit_pipeline_job_submit_attempt(
        self,
        idempotency_key: str,
        *,
        expected_submission_attempt: int,
        slurm_job_id: str,
        transition: Any,
        pipeline_job_id: str,
        slurm_accounting_submitted_at: datetime | None = None,
        fallback_submitline_key: str | None = None,
    ) -> Any:
        self.trace.append(
            (
                "commit_pipeline_job_submit_attempt",
                idempotency_key,
                expected_submission_attempt,
                slurm_job_id,
                _transition(transition),
                pipeline_job_id,
                slurm_accounting_submitted_at,
                fallback_submitline_key,
            )
        )
        return SimpleNamespace(committed=True, outcome="committed", row={"slurm_job_id": slurm_job_id}, wrote=True)

    def bind_forcing_submit_attempt(
        self,
        idempotency_key: str,
        *,
        expected_submission_attempt: int,
        expected_submission_attempt_started_at: Any,
        slurm_job_id: str,
        reconciliation_source: str,
        expected_slurm_comment: str | None = None,
    ) -> Any:
        self.trace.append(
            (
                "bind_forcing_submit_attempt",
                idempotency_key,
                expected_submission_attempt,
                expected_submission_attempt_started_at,
                slurm_job_id,
                reconciliation_source,
                expected_slurm_comment,
            )
        )
        return {"slurm_job_id": slurm_job_id}

    def permit_forcing_submit_retry(
        self,
        job_id: str,
        *,
        expected_submission_attempt: int,
        expected_submission_attempt_started_at: Any,
    ) -> Any:
        self.trace.append(
            ("permit_forcing_submit_retry", job_id, expected_submission_attempt, expected_submission_attempt_started_at)
        )
        return {"status": "reservation_lost"}

    def bind_reservation(self, idempotency_key: str, *, slurm_job_id: str, status: str) -> Any:
        self.trace.append(("bind_reservation", idempotency_key, slurm_job_id, status))
        return {"slurm_job_id": slurm_job_id}


class _StoreWithoutRetryPermit(_Store):
    permit_pipeline_job_retry = None  # type: ignore[assignment]


class _StoreWithAnchorlessRetryPermit(_Store):
    def permit_pipeline_job_retry(  # type: ignore[override]
        self, job_id: str, *, expected_submission_attempt: int, expected_status: str
    ) -> int:
        self.trace.append(("permit_pipeline_job_retry", job_id, expected_submission_attempt, expected_status))
        return 1


class _StoreWithoutCommitter(_Store):
    commit_pipeline_job_submit_attempt = None  # type: ignore[assignment]


class _StoreWithoutForcingBinder(_Store):
    bind_forcing_submit_attempt = None  # type: ignore[assignment]


class _StoreWithCommentlessForcingBinder(_Store):
    def bind_forcing_submit_attempt(  # type: ignore[override]
        self,
        idempotency_key: str,
        *,
        expected_submission_attempt: int,
        expected_submission_attempt_started_at: Any,
        slurm_job_id: str,
        reconciliation_source: str,
    ) -> Any:
        self.trace.append(("bind_forcing_submit_attempt", idempotency_key, slurm_job_id))
        return {"slurm_job_id": slurm_job_id}


class _StoreWithoutForcingRetryPermit(_Store):
    permit_forcing_submit_retry = None  # type: ignore[assignment]


class _StoreWithUnversionedRetryPermit(_Store):
    def permit_pipeline_job_retry(  # type: ignore[override]
        self,
        job_id: str,
        *,
        expected_submission_attempt: int,
        expected_status: str,
        expected_submission_attempt_started_at: datetime | None,
    ) -> int:
        self.trace.append(
            (
                "permit_pipeline_job_retry",
                job_id,
                expected_submission_attempt,
                expected_status,
                expected_submission_attempt_started_at,
            )
        )
        return 1


class _StoreWithJoblessCommitter(_Store):
    def commit_pipeline_job_submit_attempt(  # type: ignore[override]
        self,
        idempotency_key: str,
        *,
        expected_submission_attempt: int,
        slurm_job_id: str,
        transition: Any,
    ) -> Any:
        self.trace.append(("commit_pipeline_job_submit_attempt", idempotency_key, slurm_job_id))
        return SimpleNamespace(committed=True, outcome="committed", row={"slurm_job_id": slurm_job_id}, wrote=True)


class _Querier:
    """Exact-keyword comment querier with a counting, non-caching ``capability()``."""

    def __init__(
        self,
        answers: dict[str, Any],
        trace: list[tuple[Any, ...]],
        *,
        capability: bool | None = None,
    ) -> None:
        self.answers = answers
        self.trace = trace
        self.verdict = capability
        self.capability_calls = 0

    def capability(self) -> bool | None:
        self.capability_calls += 1
        self.trace.append(("capability",))
        return self.verdict

    def __call__(
        self,
        idempotency_key: str,
        *,
        expected_user: str | None = None,
        expected_account: str | None = None,
        accepted_submit_contract_version: str | None = None,
        submission_attempt_started_at: datetime | None = None,
        forcing_exact_comment: str | None = None,
        forcing_current_attempt_comment: bool = False,
        forcing_controller_identity: bool = False,
        forcing_stage: str | None = None,
        forcing_task_count: int | None = None,
    ) -> Any:
        self.trace.append(("query", idempotency_key))
        answer = self.answers[idempotency_key]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _Clock:
    """``now`` that advances on every call; the call count is part of the contract."""

    def __init__(self, *instants: datetime) -> None:
        self._instants: Iterator[datetime] = iter(instants)
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return next(self._instants)


def _reconcile(store: _Store, querier: _Querier, **kwargs: Any) -> list[Any]:
    kwargs.setdefault("grace", GRACE)
    kwargs.setdefault("now", lambda: OLD)
    return reconcile_module.reconcile_reserved_unbound_jobs(store, comment_query=querier, **kwargs)


def _expected(**values: Any) -> dict[str, Any]:
    """The FULL field set of one outcome: every unnamed field keeps its default."""

    defaults = {
        field.name: field.default
        for field in fields(reconcile_module.ReservationReconcileOutcome)
        if field.default is not MISSING
    }
    return {**defaults, **values}


def _gfs_comment_record(slurm_job_id: str = "70001", **extra: Any) -> Any:
    return _record(slurm_job_id, job_name="nhms_forecast", comment=f"nhms_idem:{GFS_KEY}", **extra)


def _forcing_record(slurm_job_id: str = "70003") -> Any:
    return _record(slurm_job_id, job_name="nhms_forcing", comment=FORCING_COMMENT)


TIMEOUT = ("submit_result_ambiguous", None, None, None, None, "reserved", 0)
UNPROVEN = (
    "submit_result_ambiguous",
    "slurm_exact_comment",
    "accounting_unavailable",
    None,
    "comment_accounting_unproven",
    None,
    0,
)


# --- write order ---------------------------------------------------------------------------------


def test_a_multi_row_pass_keeps_the_outcome_sequence_and_the_ordered_store_and_query_calls() -> None:
    trace: list[tuple[Any, ...]] = []
    jobs = [
        _legacy_job(None, "job_unkeyed"),
        _accepted_job("gfs", submit_outcome=None),
        _forcing_job(submit_outcome=None),
        _accepted_job("ifs"),
        _legacy_job(LEGACY_A_KEY, "job_legacy_a"),
    ]
    ifs_master = _record(
        "70002",
        job_name="nhms_forecast",
        comment=None,
        submitted_at=ANCHOR + timedelta(minutes=2),
        submitline_key=f"nhms_idem:{IFS_KEY}",
    )
    querier = _Querier(
        {
            GFS_KEY: _global(),
            FORCING_KEY: _global(),
            IFS_KEY: _fallback(ifs_master, basis="submitline_exact"),
            LEGACY_A_KEY: _global(_record("70004", job_name="nhms_hydro", comment=f"nhms_idem:{LEGACY_A_KEY}")),
        },
        trace,
    )

    outcomes = _reconcile(_Store(jobs, trace), querier)

    # One outcome per keyed row, in row order; the unkeyed row yields none.
    assert [vars(outcome) for outcome in outcomes] == [
        _expected(
            job_id=GFS_JOB,
            idempotency_key=GFS_KEY,
            action="absence_retry_permitted",
            status="reservation_lost",
            reconciliation_source="slurm_exact_comment",
            reconciliation_decision="absence_retry_permitted",
            durable_write_kind="forecast_retry_permission",
            durable_write_count=1,
        ),
        _expected(
            job_id=FORCING_JOB,
            idempotency_key=FORCING_KEY,
            action="absence_retry_permitted",
            status="reservation_lost",
            reconciliation_source="slurm_exact_comment",
            reconciliation_decision="absence_retry_permitted",
            durable_write_kind="forcing_retry_permission",
            durable_write_count=1,
        ),
        _expected(
            job_id=IFS_JOB,
            idempotency_key=IFS_KEY,
            action="bound",
            status="submitted",
            slurm_job_id="70002",
            reconciliation_source="slurm_name_window_unique",
            reconciliation_decision="matched_bound",
            matched_slurm_job_id="70002",
            match_count=1,
            durable_write_kind="reservation_bind",
            durable_write_count=1,
            fallback_match_basis="submitline_exact",
        ),
        _expected(
            job_id="job_legacy_a",
            idempotency_key=LEGACY_A_KEY,
            action="bound",
            status="submitted",
            slurm_job_id="70004",
            durable_write_kind="reservation_bind",
            durable_write_count=1,
        ),
    ]
    version = ACCEPTED_SUBMIT_CONTRACT_VERSION
    assert trace == [
        ("query_reserved_unbound_jobs",),
        # gfs: lane selection probes the pass capability once, then the
        # pre-outcome timeout transition precedes the accounting query.
        ("capability",),
        ("forecast_cohort_runtime_identity_matches", GFS_JOB),
        ("transition_pipeline_job_submit_evidence", GFS_JOB, TIMEOUT, 1, ("reserved",), True, version),
        ("query", GFS_KEY),
        ("permit_pipeline_job_retry", GFS_JOB, 1, "reserved", ANCHOR, version),
        # forcing: the forcing ambiguity transition (no contract version keyword
        # is passed by that writer) precedes its query.
        ("transition_pipeline_job_submit_evidence", FORCING_JOB, TIMEOUT, 1, ("reserved",), True, None),
        ("query", FORCING_KEY),
        ("permit_forcing_submit_retry", FORCING_JOB, 1, ANCHOR),
        # ifs: the capability verdict is reused (no second probe); the identity
        # gate re-reads the runtime identity before the typed commit.
        ("forecast_cohort_runtime_identity_matches", IFS_JOB),
        ("query", IFS_KEY),
        ("forecast_cohort_runtime_identity_matches", IFS_JOB),
        (
            "commit_pipeline_job_submit_attempt",
            IFS_KEY,
            1,
            "70002",
            ("accepted", "slurm_name_window_unique", "matched_bound", "70002", None, "submitted", 0),
            IFS_JOB,
            ANCHOR + timedelta(minutes=2),
            f"nhms_idem:{IFS_KEY}",
        ),
        ("query", LEGACY_A_KEY),
        ("bind_reservation", LEGACY_A_KEY, "70004", "submitted"),
    ]


# --- capability probe ---------------------------------------------------------------------------


def test_the_capability_probe_never_runs_for_an_empty_selection_or_rows_that_exit_before_lane_selection() -> None:
    trace: list[tuple[Any, ...]] = []
    empty = _Querier({}, trace)
    assert _reconcile(_Store([], trace), empty) == []
    assert empty.capability_calls == 0

    # bind_only exits a non-forcing row before lane selection.
    early = _Querier({}, trace)
    outcomes = _reconcile(_Store([_accepted_job("gfs"), _accepted_job("ifs")], trace), early, bind_only=True)
    assert [(outcome.action, outcome.reconciliation_reason_class) for outcome in outcomes] == [
        ("query_unavailable", "comment_accounting_unproven"),
        ("query_unavailable", "comment_accounting_unproven"),
    ]
    assert early.capability_calls == 0
    assert ("capability",) not in trace


def test_the_capability_probe_runs_exactly_once_across_several_eligible_rows() -> None:
    trace: list[tuple[Any, ...]] = []
    querier = _Querier({GFS_KEY: _global(), IFS_KEY: _global()}, trace)

    outcomes = _reconcile(_Store([_accepted_job("gfs"), _accepted_job("ifs")], trace), querier)

    assert [outcome.action for outcome in outcomes] == ["absence_retry_permitted", "absence_retry_permitted"]
    assert querier.capability_calls == 1
    assert trace.index(("capability",)) < trace.index(("query", GFS_KEY))


# --- now() call points ----------------------------------------------------------------------------


def test_now_is_read_at_every_deferral_check_never_snapshotted_for_the_pass() -> None:
    trace: list[tuple[Any, ...]] = []
    jobs = [_forcing_job(), _legacy_job(LEGACY_A_KEY, "job_legacy_a"), _accepted_job("gfs")]
    querier = _Querier({FORCING_KEY: _global(), LEGACY_A_KEY: _global(), GFS_KEY: _global()}, trace)
    # Young, old, young: the clock crosses the grace boundary between rows.
    clock = _Clock(YOUNG, OLD, YOUNG)

    outcomes = _reconcile(_Store(jobs, trace), querier, now=clock)

    assert [(outcome.action, outcome.status, outcome.reconciliation_decision) for outcome in outcomes] == [
        ("absence_unconfirmed", "reserved", None),
        ("reservation_lost", "reservation_lost", None),
        ("absence_unconfirmed", "reserved", "absence_deferred"),
    ]
    assert clock.calls == 3
    write_names = {"update_job_status", "transition_pipeline_job_submit_evidence"}
    writes = [entry[:3] for entry in trace if entry[0] in write_names]
    assert writes == [
        ("update_job_status", "job_legacy_a", "reservation_lost"),
        (
            "transition_pipeline_job_submit_evidence",
            GFS_JOB,
            ("submit_result_ambiguous", "slurm_exact_comment", "absence_deferred", None, None, None, 0),
        ),
    ]


# --- exception scope -------------------------------------------------------------------------------


def test_an_unavailable_commit_api_escapes_the_whole_pass_before_later_rows() -> None:
    trace: list[tuple[Any, ...]] = []
    jobs = [_accepted_job("gfs"), _legacy_job(LEGACY_A_KEY, "job_legacy_a")]
    querier = _Querier({GFS_KEY: _global(_gfs_comment_record()), LEGACY_A_KEY: _global()}, trace)

    with pytest.raises(reconcile_module.ReconcileQueryUnavailable) as raised:
        _reconcile(_StoreWithoutCommitter(jobs, trace), querier)

    assert str(raised.value) == "accepted-submit commit API unavailable"
    assert raised.value.reason_class == "process_unavailable"
    assert ("query", LEGACY_A_KEY) not in trace


def test_an_unavailable_forcing_bind_api_escapes_the_whole_pass_before_later_rows() -> None:
    trace: list[tuple[Any, ...]] = []
    jobs = [_forcing_job(), _legacy_job(LEGACY_A_KEY, "job_legacy_a")]
    querier = _Querier({FORCING_KEY: _controller(_forcing_record()), LEGACY_A_KEY: _global()}, trace)

    with pytest.raises(reconcile_module.ReconcileQueryUnavailable) as raised:
        _reconcile(_StoreWithoutForcingBinder(jobs, trace), querier)

    assert str(raised.value) == "forcing submit bind API unavailable"
    assert raised.value.reason_class == "process_unavailable"
    assert ("query", LEGACY_A_KEY) not in trace


def test_a_query_unavailable_inside_the_accounting_query_is_held_per_row() -> None:
    trace: list[tuple[Any, ...]] = []
    jobs = [_accepted_job("gfs"), _legacy_job(LEGACY_A_KEY, "job_legacy_a")]
    unavailable = reconcile_module.ReconcileQueryUnavailable(
        "sacct timed out", reason_class="accounting_authority_unproven"
    )
    querier = _Querier({GFS_KEY: unavailable, LEGACY_A_KEY: _global()}, trace)

    held, demoted = _reconcile(_Store(jobs, trace), querier)

    assert vars(held) == _expected(
        job_id=GFS_JOB,
        idempotency_key=GFS_KEY,
        action="query_unavailable",
        status="reserved",
        reconciliation_source="slurm_exact_comment",
        reconciliation_decision="accounting_unavailable",
        reconciliation_reason_class="accounting_authority_unproven",
        durable_write_kind="pipeline_job_reconciliation",
        durable_write_count=1,
    )
    assert demoted.action == "reservation_lost"


# --- journal quarantine ----------------------------------------------------------------------------


def test_a_journal_fault_after_fallback_classification_quarantines_with_the_basis_and_the_pass_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    trace: list[tuple[Any, ...]] = []
    jobs = [_accepted_job("gfs"), _legacy_job(LEGACY_A_KEY, "job_legacy_a")]
    masters = (
        _record("70001", job_name="nhms_forecast", comment=None),
        _record("70002", job_name="nhms_forecast", comment=None),
    )
    querier = _Querier(
        {
            GFS_KEY: _fallback(*masters, basis="submitline_exact"),
            LEGACY_A_KEY: _global(_record("70004", job_name="nhms_hydro", comment=f"nhms_idem:{LEGACY_A_KEY}")),
        },
        trace,
    )
    store = _Store(jobs, trace, journal_faults=frozenset({(GFS_JOB, "accounting_unavailable")}))

    with caplog.at_level(logging.WARNING, logger="services.orchestrator.reconcile"):
        quarantined, bound = _reconcile(store, querier)

    assert vars(quarantined) == _expected(
        job_id=GFS_JOB,
        idempotency_key=GFS_KEY,
        action="journal_quarantined",
        status="reserved",
        quarantine_reason="file_journal_fixture_fault",
        quarantine_field="pipeline_jobs",
        fallback_match_basis="submitline_exact",
    )
    assert bound.action == "bound"
    assert ("transition_pipeline_job_submit_evidence", GFS_JOB, UNPROVEN) == trace[4][:3]
    assert [record.getMessage() for record in caplog.records] == [
        f"reconcile reservation quarantined for idempotency_key={GFS_KEY}: file_journal_fixture_fault (pipeline_jobs)"
    ]


def test_a_journal_fault_before_classification_quarantines_without_a_basis() -> None:
    trace: list[tuple[Any, ...]] = []
    jobs = [_accepted_job("gfs", submit_outcome=None), _legacy_job(LEGACY_A_KEY, "job_legacy_a")]
    querier = _Querier({GFS_KEY: _global(), LEGACY_A_KEY: _global()}, trace)
    store = _Store(jobs, trace, journal_faults=frozenset({(GFS_JOB, None)}))

    quarantined, demoted = _reconcile(store, querier)

    assert (quarantined.action, quarantined.fallback_match_basis) == ("journal_quarantined", None)
    assert ("query", GFS_KEY) not in trace
    assert demoted.action == "reservation_lost"


# --- branches no other suite reaches ---------------------------------------------------------------


def test_an_accepted_absence_on_a_store_without_the_retry_permit_demotes_through_update_job_status() -> None:
    trace: list[tuple[Any, ...]] = []
    querier = _Querier({GFS_KEY: _global()}, trace)

    (outcome,) = _reconcile(_StoreWithoutRetryPermit([_accepted_job("gfs")], trace), querier)

    assert vars(outcome) == _expected(
        job_id=GFS_JOB,
        idempotency_key=GFS_KEY,
        action="absence_retry_permitted",
        status="reservation_lost",
        reconciliation_source="slurm_exact_comment",
        reconciliation_decision="absence_retry_permitted",
        durable_write_kind="forecast_retry_permission",
        durable_write_count=1,
    )
    assert trace[-1] == (
        "update_job_status",
        GFS_JOB,
        "reservation_lost",
        "SLURM_RESERVATION_LOST",
        f"sbatch acceptance for reservation idempotency_key={GFS_KEY} could not be confirmed in accounting.",
    )


def test_an_accepted_absence_on_an_anchorless_retry_permit_stays_reserved_without_calling_it() -> None:
    trace: list[tuple[Any, ...]] = []
    querier = _Querier({GFS_KEY: _global()}, trace)

    (outcome,) = _reconcile(_StoreWithAnchorlessRetryPermit([_accepted_job("gfs")], trace), querier)

    assert vars(outcome) == _expected(
        job_id=GFS_JOB,
        idempotency_key=GFS_KEY,
        action="stale_attempt_blocked",
        status="reserved",
        reconciliation_source="slurm_exact_comment",
    )
    assert trace[-1] == ("query", GFS_KEY)


def test_a_targeted_comment_pin_on_a_binder_without_the_comment_keyword_never_binds() -> None:
    trace: list[tuple[Any, ...]] = []
    querier = _Querier({FORCING_KEY: _controller(_forcing_record())}, trace)

    (outcome,) = _reconcile(
        _StoreWithCommentlessForcingBinder([_forcing_job()], trace),
        querier,
        target_job_id=FORCING_JOB,
        target_slurm_comment=FORCING_COMMENT,
    )

    assert vars(outcome) == _expected(
        job_id=FORCING_JOB,
        idempotency_key=FORCING_KEY,
        action="stale_attempt_blocked",
        status="reserved",
        slurm_job_id="70003",
        reconciliation_source="slurm_controller_exact_comment",
        reconciliation_decision="matched_bound",
        matched_slurm_job_id="70003",
        match_count=1,
    )
    assert trace == [("get_reserved_unbound_job", FORCING_JOB), ("query", FORCING_KEY)]


def test_a_targeted_comment_pin_reaches_a_binder_that_accepts_it() -> None:
    trace: list[tuple[Any, ...]] = []
    querier = _Querier({FORCING_KEY: _controller(_forcing_record())}, trace)

    (outcome,) = _reconcile(
        _Store([_forcing_job()], trace),
        querier,
        target_job_id=FORCING_JOB,
        target_submission_attempt=1,
        target_slurm_comment=FORCING_COMMENT,
    )

    assert (outcome.action, outcome.durable_write_kind, outcome.durable_write_count) == (
        "bound",
        "forcing_submit_bind",
        1,
    )
    assert trace[-1] == (
        "bind_forcing_submit_attempt",
        FORCING_KEY,
        1,
        ANCHOR,
        "70003",
        "slurm_controller_exact_comment",
        FORCING_COMMENT,
    )


def test_a_pre_outcome_forcing_row_under_bind_only_exits_before_any_write_or_query() -> None:
    trace: list[tuple[Any, ...]] = []
    querier = _Querier({}, trace)

    (outcome,) = _reconcile(_Store([_forcing_job(submit_outcome=None)], trace), querier, bind_only=True)

    assert vars(outcome) == _expected(
        job_id=FORCING_JOB,
        idempotency_key=FORCING_KEY,
        action="query_unavailable",
        status="reserved",
        reconciliation_source="slurm_exact_comment",
        reconciliation_reason_class="comment_accounting_unproven",
    )
    assert trace == [("query_reserved_unbound_jobs",)]


def test_a_forcing_controller_match_with_a_foreign_job_name_is_identity_blocked_without_a_write() -> None:
    trace: list[tuple[Any, ...]] = []
    foreign = _record("70003", job_name="nhms_forecast", comment=FORCING_COMMENT)
    querier = _Querier({FORCING_KEY: _controller(foreign)}, trace)

    (outcome,) = _reconcile(_Store([_forcing_job()], trace), querier)

    assert vars(outcome) == _expected(
        job_id=FORCING_JOB,
        idempotency_key=FORCING_KEY,
        action="identity_mismatch_blocked",
        status="reserved",
        reconciliation_source="slurm_controller_exact_comment",
        match_count=1,
    )
    assert trace == [("query_reserved_unbound_jobs",), ("query", FORCING_KEY)]


def test_a_credible_forcing_absence_on_a_store_without_the_retry_permit_stays_unconfirmed() -> None:
    trace: list[tuple[Any, ...]] = []
    querier = _Querier({FORCING_KEY: _global()}, trace)

    (outcome,) = _reconcile(_StoreWithoutForcingRetryPermit([_forcing_job()], trace), querier)

    assert vars(outcome) == _expected(
        job_id=FORCING_JOB,
        idempotency_key=FORCING_KEY,
        action="absence_unconfirmed",
        status="reserved",
        reconciliation_source="slurm_exact_comment",
    )


def test_a_fallback_unique_master_with_an_invalid_slurm_id_past_the_identity_seam_is_held_as_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The real identity gate already refuses an invalid id; this pins the
    # independent check behind it (reached only when the seam is bypassed).
    monkeypatch.setattr(
        "services.orchestrator.reconcile_reserved._reserved_record_identity_matches",
        lambda *_args, **_kwargs: True,
    )
    trace: list[tuple[Any, ...]] = []
    master = _record("not-a-slurm-id", job_name="nhms_forecast", comment=None)
    querier = _Querier({GFS_KEY: _fallback(master, basis="name_window_count")}, trace)

    (outcome,) = _reconcile(_Store([_accepted_job("gfs")], trace), querier)

    assert vars(outcome) == _expected(
        job_id=GFS_JOB,
        idempotency_key=GFS_KEY,
        action="fallback_no_match",
        status="reserved",
        reconciliation_source="slurm_exact_comment",
        reconciliation_decision="accounting_unavailable",
        reconciliation_reason_class="comment_accounting_unproven",
        match_count=0,
        durable_write_kind="pipeline_job_reconciliation",
        durable_write_count=1,
    )
    assert trace[-1][:3] == ("transition_pipeline_job_submit_evidence", GFS_JOB, UNPROVEN)


def test_an_accepted_absence_on_an_unversioned_retry_permit_passes_the_anchor_only() -> None:
    trace: list[tuple[Any, ...]] = []
    querier = _Querier({GFS_KEY: _global()}, trace)

    (outcome,) = _reconcile(_StoreWithUnversionedRetryPermit([_accepted_job("gfs")], trace), querier)

    assert (outcome.action, outcome.status, outcome.durable_write_kind) == (
        "absence_retry_permitted",
        "reservation_lost",
        "forecast_retry_permission",
    )
    assert trace[-1] == ("permit_pipeline_job_retry", GFS_JOB, 1, "reserved", ANCHOR)


def test_an_accepted_commit_on_a_committer_without_the_job_keyword_still_binds() -> None:
    trace: list[tuple[Any, ...]] = []
    querier = _Querier({GFS_KEY: _global(_gfs_comment_record())}, trace)

    (outcome,) = _reconcile(_StoreWithJoblessCommitter([_accepted_job("gfs")], trace), querier)

    assert vars(outcome) == _expected(
        job_id=GFS_JOB,
        idempotency_key=GFS_KEY,
        action="bound",
        status="submitted",
        slurm_job_id="70001",
        reconciliation_source="slurm_exact_comment",
        reconciliation_decision="matched_bound",
        matched_slurm_job_id="70001",
        match_count=1,
        durable_write_kind="reservation_bind",
        durable_write_count=1,
    )
    assert trace[-1] == ("commit_pipeline_job_submit_attempt", GFS_KEY, "70001")
