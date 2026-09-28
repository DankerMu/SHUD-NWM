"""#2668 bind-reserved-job: the typed journal CAS (``bind_operator_verified_reserved_job``).

Seam: ``FileOrchestrationJournalRepository.bind_operator_verified_reserved_job``
against a real file journal whose held row was written by the real restart
reconcile producer (see ``tests/orchestrator_bind_reserved_job_helpers.py``).

The success oracle is independent of the code under test: a SECOND journal in
which the #2655 automatic name-window fallback itself bound the same master
(``submitline_exact``, one master carrying the row's key).  The operator bind
must leave exactly that durable tuple -- no new token -- plus one
``operator_verified_bind`` audit event in the same durable append.  Every
refusal is named and leaves every durable byte identical.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    AcceptedSubmitTransition,
)
from services.orchestrator.file_orchestration_journal import (
    OPERATOR_BIND_REFUSALS,
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
)
from tests.gateway_reconcile_helpers import (
    _append_cohort_placeholders,
    _commit_name_window_fallback_bind,
    _file_cohort_repository,
)
from tests.orchestrator_bind_reserved_job_helpers import (
    ANCHOR,
    BIND_TUPLE_FIELDS,
    CHECKED_AT,
    FOREIGN_KEY,
    JOB_ID,
    KEY,
    MASTER_ID,
    MASTER_SUBMIT,
    MASTER_SUBMIT_LOCAL,
    OWN_COMMENT,
    QUERY_END,
    SUBMIT_LINE,
    _fallback_array,
    after,
    bind_events,
    bind_kwargs,
    held_repository,
    held_row,
    journal_bytes,
    journal_records,
)
from tests.test_gateway_reconcile_claimant_exclusivity import (
    _fallback_querier,
    _second_master_reservation_record,
)
from tests.test_real_slurm_gateway import _pinned_local_timezone

pytestmark = pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")

IFS_JOB = "job_cycle_ifs_2026071200_forecast_fixture_forecast"


@pytest.fixture(autouse=True)
def _utc_local_time() -> Iterator[None]:
    """sacct prints ``Submit`` in local time; the fixture cluster runs in UTC."""

    with _pinned_local_timezone("UTC"):
        yield


def _refused(repository: Any, token: str, **overrides: Any) -> None:
    """The bind refuses by ``token`` and every durable byte stays identical."""

    assert token in OPERATOR_BIND_REFUSALS
    before = journal_bytes(repository.root)
    job_id = overrides.pop("job_id", JOB_ID)
    result = repository.bind_operator_verified_reserved_job(job_id, **bind_kwargs(held_row(repository), **overrides))
    assert (result.refusal, result.receipt) == (token, None)
    assert journal_bytes(repository.root) == before


def _automatic_bind_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The oracle: the #2655 automatic fallback binds the same master itself."""

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    repository = _file_cohort_repository(
        tmp_path / "automatic",
        created_at=ANCHOR,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
    )
    rows = _fallback_array(MASTER_ID, submit=MASTER_SUBMIT_LOCAL, submit_line=SUBMIT_LINE)
    query, _commands = _fallback_querier(monkeypatch, rows=rows, query_end=QUERY_END)
    (outcome,) = reconcile_reserved_unbound_jobs(repository, comment_query=query, now=lambda: QUERY_END)
    assert (outcome.action, outcome.slurm_job_id, outcome.fallback_match_basis) == (
        "bound",
        MASTER_ID,
        "submitline_exact",
    )
    return repository


# --- success: the exact #2655 tuple, one audit event, one append ---------------------------


@pytest.mark.parametrize("shape", ["a", "b"])
def test_bind_writes_the_automatic_fallback_bind_tuple_and_one_audit_event_in_one_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    oracle = held_row(_automatic_bind_repository(tmp_path, monkeypatch))
    repository = held_repository(tmp_path, monkeypatch, shape)
    held = held_row(repository)
    records_before = journal_records(repository.root)

    result = repository.bind_operator_verified_reserved_job(JOB_ID, **bind_kwargs(held))

    assert result.refusal is None
    bound = held_row(repository)
    assert {field: bound.get(field) for field in BIND_TUPLE_FIELDS} == {
        field: oracle.get(field) for field in BIND_TUPLE_FIELDS
    }
    assert (bound["status"], bound["slurm_job_id"], bound["slurm_binding_source"]) == (
        "submitted",
        MASTER_ID,
        "slurm_name_window_unique",
    )
    # One durable append: the bind row and its audit event are the two newest
    # records, with consecutive sequence numbers, and nothing else was written.
    records = journal_records(repository.root)
    added = records[len(records_before) :]
    assert [record["record_type"] for record in added] == ["pipeline_job", "pipeline_event"]
    assert added[1]["sequence"] == added[0]["sequence"] + 1
    (event,) = bind_events(repository.root)
    assert event == added[1]["payload"]
    assert (event["entity_id"], event["status_from"], event["status_to"]) == (JOB_ID, "reserved", "submitted")
    assert event["details"] == {
        "checked_by": "operator-alice",
        "checked_at": CHECKED_AT,
        "verification_note": bind_kwargs(held)["verification_note"],
        "submitline_key": OWN_COMMENT,
        "slurm_job_id": MASTER_ID,
        "slurm_accounting_submitted_at": MASTER_SUBMIT,
        "expected_submission_attempt": 1,
        "expected_submission_attempt_started_at": "2026-07-12T00:00:13Z",
        "prior_status": "reserved",
        "prior_submit_outcome": "submit_result_ambiguous",
        "prior_reconciliation_source": "slurm_exact_comment",
        "prior_reconciliation_decision": "accounting_unavailable",
        "prior_reconciliation_reason_class": "comment_accounting_unproven",
    }
    receipt = result.receipt
    assert receipt is not None
    assert (receipt.written_record_count, receipt.warnings, receipt.status_to) == (2, (), "submitted")
    # A fresh reader replays the same row (the append is the authority).
    assert FileOrchestrationJournalRepository(repository.root).get_accepted_submit_pipeline_job(JOB_ID) == bound
    # A repeated request is a zero-write ``not_held``.
    _refused(repository, "not_held")


def test_submit_time_equal_to_the_anchor_or_the_check_time_is_inside_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = held_repository(tmp_path, monkeypatch)
    result = repository.bind_operator_verified_reserved_job(
        JOB_ID,
        **bind_kwargs(
            held_row(repository), slurm_submit_time="2026-07-12T00:00:13Z", checked_at="2026-07-12T00:00:13Z"
        ),
    )
    assert result.refusal is None
    assert held_row(repository)["slurm_accounting_submitted_at"] == "2026-07-12T00:00:13Z"


# --- the negative matrix: named refusal, zero bytes ----------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"slurm_submit_time": "2026-07-12T00:00:12Z"},  # one second before the attempt anchor
        {"slurm_submit_time": after(CHECKED_AT, seconds=1)},  # after the operator's check
        {"slurm_submit_time": "2026-07-11T23:59:00Z"},  # a prior attempt's master under the same key
    ],
)
def test_submit_time_outside_the_attempt_window_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str]
) -> None:
    _refused(held_repository(tmp_path, monkeypatch), "submit_time_outside_attempt_window", **overrides)


@pytest.mark.parametrize(
    "submit_line",
    [
        f"/usr/bin/sbatch --array=0-0%15 --comment=nhms_idem:{FOREIGN_KEY} /tmp/x.sbatch",  # another job
        "/usr/bin/sbatch --array=0-0%15 /tmp/x.sbatch",  # no --comment at all
        f"/usr/bin/sbatch --comment={OWN_COMMENT} --comment=nhms_idem:{FOREIGN_KEY} /tmp/x.sbatch",  # ambiguous
        f"/usr/bin/sbatch --comment={KEY} /tmp/x.sbatch",  # bare key without the nhms_idem: prefix
        "",
    ],
)
def test_submitline_key_that_is_not_the_rows_own_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, submit_line: str
) -> None:
    _refused(held_repository(tmp_path, monkeypatch), "submitline_key_mismatch", submit_line=submit_line)


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_submission_attempt": 2},
        {"expected_submission_attempt_started_at": "2026-07-12T00:00:14Z"},
    ],
)
def test_attempt_or_anchor_mismatch_is_stale_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    _refused(held_repository(tmp_path, monkeypatch), "stale_attempt", **overrides)


def _not_reserved(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> Any:
    repository = _file_cohort_repository(
        tmp_path / "rejected", created_at=ANCHOR, member_count=1, expected_user="scheduler", expected_account="account"
    )
    assert repository.reject_pipeline_job_submit_attempt(
        KEY,
        pipeline_job_id=JOB_ID,
        expected_submission_attempt=1,
        finished_at=QUERY_END,
        error_code="SBATCH_REJECTED",
        error_message="queue policy",
        stage="forecast",
        job_type="run_shud_forecast_array",
    ).committed
    assert held_row(repository)["status"] == "submission_failed"
    return repository


def _already_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    repository = held_repository(tmp_path, monkeypatch)
    assert _commit_name_window_fallback_bind(
        repository, KEY, slurm_job_id=MASTER_ID, canonical_submit=MASTER_SUBMIT, pipeline_job_id=JOB_ID
    ).committed
    return repository


def _outcome_not_ambiguous(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> Any:
    repository = _file_cohort_repository(
        tmp_path / "pre-outcome",
        created_at=ANCHOR,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
        submit_outcome=None,
    )
    row = held_row(repository)
    assert (row["status"], row["submit_outcome"]) == ("reserved", None)
    return repository


def _exact_comment_coverage_incomplete(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> Any:
    """A comment-storing cluster's held reason keeps its own exits (design Non-goals)."""

    repository = _file_cohort_repository(
        tmp_path / "coverage", created_at=ANCHOR, member_count=1, expected_user="scheduler", expected_account="account"
    )
    assert repository.transition_pipeline_job_submit_evidence(
        JOB_ID,
        AcceptedSubmitTransition.accounting(
            "accounting_unavailable",
            submit_outcome="submit_result_ambiguous",
            reconciliation_reason_class="coverage_incomplete",
        ),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    ).wrote
    return repository


def _multiple_matches_blocked(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> Any:
    repository = _file_cohort_repository(
        tmp_path / "multiple", created_at=ANCHOR, member_count=1, expected_user="scheduler", expected_account="account"
    )
    assert repository.transition_pipeline_job_submit_evidence(
        JOB_ID,
        AcceptedSubmitTransition.accounting("multiple_matches_blocked", submit_outcome="submit_result_ambiguous"),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    ).wrote
    return repository


@pytest.mark.parametrize(
    "factory",
    [
        _not_reserved,
        _already_bound,
        _outcome_not_ambiguous,
        _exact_comment_coverage_incomplete,
        _multiple_matches_blocked,
    ],
)
def test_a_row_outside_the_exact_held_shape_is_not_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, factory: Any
) -> None:
    _refused(factory(tmp_path, monkeypatch), "not_held")


@pytest.mark.parametrize("slurm_job_id", ["123_4", "abc", "56839.batch", " 56839", "56839_[0-3]", "", "٣٤"])
def test_a_slurm_id_that_is_not_a_bare_numeric_master_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slurm_job_id: str
) -> None:
    _refused(held_repository(tmp_path, monkeypatch), "slurm_id_invalid", slurm_job_id=slurm_job_id)


def test_a_naive_or_malformed_submit_time_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = held_repository(tmp_path, monkeypatch)
    _refused(repository, "slurm_submit_time_invalid", slurm_submit_time=MASTER_SUBMIT_LOCAL)
    _refused(repository, "slurm_submit_time_invalid", slurm_submit_time="yesterday")


@pytest.mark.parametrize("job_id", ["job_cycle_gfs_2026071200_absent_forecast", "not-a-job-id"])
def test_an_unknown_job_is_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job_id: str) -> None:
    _refused(held_repository(tmp_path, monkeypatch), "not_found", job_id=job_id)


def test_a_legacy_unversioned_master_is_refused_by_name(tmp_path: Path) -> None:
    """Shape (c): reconcile reports it ``legacy_unversioned_read_only``; the bind refuses it by name."""

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    repository = _file_cohort_repository(tmp_path / "legacy", created_at=ANCHOR, member_count=1, versioned=False)
    row = held_row(repository)
    assert row.get("accepted_submit_contract_version") is None
    assert (row["status"], row["slurm_job_id"]) == ("reserved", None)
    (outcome,) = reconcile_reserved_unbound_jobs(
        repository, comment_query=lambda _key, **_kw: pytest.fail("never queried"), now=lambda: QUERY_END
    )
    assert outcome.action == "legacy_unversioned_read_only"
    _refused(repository, "legacy_unversioned_unsupported")


# --- claimant exclusivity (stricter than #1850) ----------------------------------------------


def _name_window_bind() -> AcceptedSubmitTransition:
    return AcceptedSubmitTransition.accounting(
        "matched_bound",
        submit_outcome="accepted",
        matched_slurm_job_id=MASTER_ID,
        status="submitted",
        reconciliation_source="slurm_name_window_unique",
    )


def _with_ifs_sibling(repository: Any) -> None:
    record = _second_master_reservation_record(
        source_id="ifs", created_at=datetime(2026, 7, 12, tzinfo=UTC), member_count=1
    )
    repository.reserve_pipeline_job(record)
    _append_cohort_placeholders(repository, 1, source_id="ifs")


def test_a_slurm_id_bound_to_another_active_master_is_claimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = held_repository(tmp_path, monkeypatch)
    _with_ifs_sibling(repository)
    assert repository.commit_pipeline_job_submit_attempt(
        FOREIGN_KEY,
        pipeline_job_id=IFS_JOB,
        expected_submission_attempt=1,
        slurm_job_id=MASTER_ID,
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    ).committed
    _refused(repository, "slurm_id_claimed")


def test_a_recycled_slurm_id_settled_on_another_master_is_claimed_even_though_1850_would_allow_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design Decision 3: the operator path accepts no recycle ambiguity.

    The IFS sibling ran Slurm id 56839 with a DIFFERENT canonical Submit and
    settled.  The automatic #1850 fallback reads that as a recycled id and
    binds (proved on a byte copy of the same journal); the operator bind
    refuses it.
    """

    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    repository = held_repository(tmp_path, monkeypatch)
    _with_ifs_sibling(repository)
    # The IFS array's own SubmitLine proved its key, so the held GFS row is
    # not a claimant of that bind (#2655 key narrowing).
    assert repository.commit_pipeline_job_submit_attempt(
        FOREIGN_KEY,
        pipeline_job_id=IFS_JOB,
        expected_submission_attempt=1,
        slurm_job_id=MASTER_ID,
        transition=_name_window_bind(),
        slurm_accounting_submitted_at="2026-07-12T00:02:17Z",
        fallback_submitline_key=f"nhms_idem:{FOREIGN_KEY}",
    ).committed
    task = SacctRecord(
        f"{MASTER_ID}_0",
        "COMPLETED",
        "nhms_forecast",
        exit_code="0:0",
        user="scheduler",
        account="account",
        array_task_id=0,
    )
    completed = SacctRecord(
        slurm_job_id=MASTER_ID,
        raw_state="COMPLETED",
        job_name="nhms_forecast",
        exit_code="0:0",
        user="scheduler",
        account="account",
        array_member_job_ids=(task.slurm_job_id,),
        array_task_records=(task,),
    )
    inflight = reconcile_inflight_jobs(repository, sacct_query=lambda job_id: completed)
    assert [(outcome.job_id, outcome.status) for outcome in inflight] == [(IFS_JOB, "succeeded")]

    control_root = tmp_path / "control-journal"
    shutil.copytree(repository.root, control_root)
    automatic = FileOrchestrationJournalRepository(control_root).commit_pipeline_job_submit_attempt(
        KEY,
        pipeline_job_id=JOB_ID,
        expected_submission_attempt=1,
        slurm_job_id=MASTER_ID,
        transition=_name_window_bind(),
        slurm_accounting_submitted_at=MASTER_SUBMIT,
        fallback_submitline_key=OWN_COMMENT,
    )
    assert automatic.outcome == "applied"

    _refused(repository, "slurm_id_claimed")


# --- concurrency, input validation ------------------------------------------------------------


def test_a_concurrent_reconcile_bind_before_the_lock_is_taken_makes_the_locked_reread_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = held_repository(tmp_path, monkeypatch)
    held = held_row(repository)
    original = repository._locked_cycle_write
    raced: dict[str, dict[str, bytes]] = {}

    @contextmanager
    def _racing(**kwargs: Any) -> Iterator[None]:
        # Another process (a separate repository instance) wins the cycle lock
        # first and commits the automatic fallback bind of the same row.
        rival = FileOrchestrationJournalRepository(repository.root)
        assert _commit_name_window_fallback_bind(
            rival, KEY, slurm_job_id=MASTER_ID, canonical_submit=MASTER_SUBMIT, pipeline_job_id=JOB_ID
        ).committed
        raced["bytes"] = journal_bytes(repository.root)
        with original(**kwargs):
            yield

    monkeypatch.setattr(repository, "_locked_cycle_write", _racing)
    result = repository.bind_operator_verified_reserved_job(JOB_ID, **bind_kwargs(held))

    assert (result.refusal, result.receipt) == ("not_held", None)
    assert journal_bytes(repository.root) == raced["bytes"]
    assert bind_events(repository.root) == []
    assert held_row(repository)["reconciliation_source"] == "slurm_name_window_unique"


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"checked_at": "2026-07-12T02:30:00"}, "submission_attempt_started_at"),
        ({"checked_by": "   "}, "checked_by"),
        ({"verification_note": ""}, "verification_note"),
        ({"expected_submission_attempt": 0}, "expected_submission_attempt"),
        ({"accepted_submit_contract_version": None}, "accepted_submit_contract_version"),
        ({"submit_line": None}, "submit_line"),
    ],
)
def test_invalid_operator_input_raises_typed_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], field: str
) -> None:
    repository = held_repository(tmp_path, monkeypatch)
    before = journal_bytes(repository.root)
    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.bind_operator_verified_reserved_job(JOB_ID, **bind_kwargs(held_row(repository), **overrides))
    assert error.value.field == field
    assert journal_bytes(repository.root) == before


def test_submit_line_script_paths_never_reach_durable_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = held_repository(tmp_path, monkeypatch)
    assert repository.bind_operator_verified_reserved_job(JOB_ID, **bind_kwargs(held_row(repository))).refusal is None
    durable = b"".join(journal_bytes(repository.root).values())
    assert SUBMIT_LINE.encode() not in durable
    assert b"nhms_6y2bx17b.sbatch" not in durable
