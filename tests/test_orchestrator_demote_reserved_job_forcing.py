"""#2682 demote-reserved-job, forcing lane: the operator-verified absence exit.

Production shape: a forcing master stays ``reserved`` / unbound /
``submit_result_ambiguous``, restart reconcile answers ``query_unavailable``
pass after pass, and no master carries this attempt's comment in sacct/squeue.
The bind has nothing to bind and the automatic ``permit_forcing_submit_retry``
never gets its reconcile-computed ``credible_absence``.

Seams: ``FileOrchestrationJournalRepository.demote_operator_verified_reserved_job``
and the shipped ``cli._click_main`` / ``cli._argparse_main`` against a real file
journal whose held forcing master was written by the real forcing writers (see
``tests/orchestrator_bind_reserved_job_helpers.py``), plus one end-to-end lane
case through the real stage loop (``orchestrate_cycle``), restart reconcile, the
scheduler's own ``_candidate_state_decision`` and the real reserve/reclaim path.

The write oracle is independent of the code under test: the lane's own
automatic writer, ``permit_forcing_submit_retry``, on a byte copy of the same
held journal.  The exit must leave exactly that row plus one
``operator_verified_absence`` event; every refusal is named and leaves every
durable byte identical.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli
from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator import reconcile as reconcile_module
from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    AcceptedSubmitTransition,
)
from services.orchestrator.file_orchestration_journal import (
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
    OperatorDemoteReceipt,
)
from services.orchestrator.forcing_submit_identity import is_unresolved_forcing_attempt
from services.orchestrator.reservation import reserve_candidate
from tests.orchestrator_bind_reserved_job_helpers import (
    FORCING_ANCHOR,
    FORCING_JOB_ID,
    FORCING_KEY,
    FORCING_MASTER_ID,
    FORCING_MEMBERS,
    FORCING_RUN_ID,
    JOB_ID,
    _forcing_members,
    forcing_comment,
    forcing_held_repository,
    forcing_row,
    held_repository,
    held_row,
    journal_bytes,
    journal_records,
)
from tests.test_orchestration_chain import FakeCycleSlurmClient, _orchestrator
from tests.test_orchestrator_bind_reserved_job_forcing_lane import (
    _assert_members_held,
    _held_forcing_shape,
    _restart_reconcile,
)
from tests.test_real_slurm_gateway import _pinned_local_timezone
from tests.test_scheduler_held_reservation_block import (
    _CYCLE_ID,
    _CYCLE_TIME,
    _HELD,
    _cohort_basins,
    _decision,
    _master,
    _plan,
    _scheduler,
)

pytestmark = pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")

#: The cycle of the unit fixture's held forcing master (``gfs_2026071200``).
_FIXTURE_CYCLE_TIME = datetime(2026, 7, 12, tzinfo=UTC)
#: Independent literal of ``reconcile.RESERVATION_ABSENCE_GRACE`` (pinned below).
_GRACE = timedelta(seconds=120)
CHECKED_AT = "2026-07-12T02:30:00Z"
NOTE = (
    "sacct -a --name nhms_forcing --starttime 2026-07-12T00:00:05 -o JobID,State,Submit,SubmitLine and "
    "squeue -a --name nhms_forcing: no master carries nhms_forcing_attempt:<key>:a1"
)
#: The exact row state ``permit_forcing_submit_retry`` writes.
_RETRY_PERMITTED = {
    "status": "reservation_lost",
    "reconciliation_source": "slurm_exact_comment",
    "reconciliation_decision": "absence_retry_permitted",
    "reconciliation_reason_class": None,
    "matched_slurm_job_id": None,
    "slurm_job_id": None,
    "submit_outcome": "submit_result_ambiguous",
    "submission_attempt": 1,
    "submission_attempt_started_at": "2026-07-12T00:00:05Z",
}
_AUDIT_KEYS = ("checked_by", "checked_at", "verification_note")
_REFUSED_LINE = "demote-reserved-job: refused: {}; no journal bytes were written"


@pytest.fixture(autouse=True)
def _planner_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")
    monkeypatch.delenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", raising=False)
    with _pinned_local_timezone("UTC"):
        yield


def _kwargs(row: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
        "expected_submission_attempt": int(row["submission_attempt"]),
        "expected_submission_attempt_started_at": row["submission_attempt_started_at"],
        "checked_by": "operator-alice",
        "checked_at": CHECKED_AT,
        "verification_note": NOTE,
    }
    kwargs.update(overrides)
    return kwargs


def _absence_events(root: Path) -> list[dict[str, Any]]:
    return [
        record["payload"]
        for record in journal_records(root)
        if record.get("record_type") == "pipeline_event"
        and record["payload"].get("event_type") == "operator_verified_absence"
    ]


def _exit(repository: Any, **overrides: Any) -> Any:
    result = repository.demote_operator_verified_reserved_job(
        FORCING_JOB_ID, **_kwargs(forcing_row(repository), **overrides)
    )
    assert getattr(result, "refusal", "no result") is None, result
    return result


def _refused(repository: Any, name: str, **overrides: Any) -> None:
    """The exit refuses by ``name`` and every durable byte stays identical."""

    before = journal_bytes(repository.root)
    result = repository.demote_operator_verified_reserved_job(
        FORCING_JOB_ID, **_kwargs(forcing_row(repository), **overrides)
    )
    assert (getattr(result, "refusal", None), getattr(result, "receipt", None)) == (name, None)
    assert journal_bytes(repository.root) == before
    assert _absence_events(repository.root) == []
    assert name in journal_module.OPERATOR_DEMOTE_REFUSALS


def _held_with(tmp_path: Path, **fields: Any) -> Any:
    """The held forcing master with exactly ``fields`` changed.

    One leg of the held tuple cannot be isolated through a public writer (a
    real bind sets the id, the matched id and the outcome together), so the
    mutated row is appended by the journal's lowest row writer,
    ``_write_pipeline_job_unlocked``, under the real cycle lock.
    """

    repository = forcing_held_repository(tmp_path)
    held = forcing_row(repository)
    with repository._locked_cycle_write(source_id="gfs", cycle_time=_FIXTURE_CYCLE_TIME):
        assert repository._write_pipeline_job_unlocked({**held, **fields}, exclusive_direct=False, model_id=None)
    row = forcing_row(repository)
    assert {key: row.get(key) for key in held if key != "updated_at"} == {
        **{key: value for key, value in held.items() if key != "updated_at"},
        **fields,
    }
    return repository


# --- the write: exactly the automatic permit row, one audit event, one append ----------------


def test_the_exit_writes_exactly_the_automatic_permit_row_and_one_audit_event_in_one_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = forcing_held_repository(tmp_path)
    held = forcing_row(repository)
    shutil.copytree(repository.root, tmp_path / "oracle")
    oracle_repository = FileOrchestrationJournalRepository(tmp_path / "oracle")
    monkeypatch.setattr(journal_module, "_utcnow", lambda: datetime(2026, 7, 12, 2, 31, tzinfo=UTC))
    assert oracle_repository.permit_forcing_submit_retry(
        FORCING_JOB_ID, expected_submission_attempt=1, expected_submission_attempt_started_at=FORCING_ANCHOR
    )
    records_before = journal_records(repository.root)

    result = _exit(repository)

    row = forcing_row(repository)
    assert row == forcing_row(oracle_repository)
    assert {key: row[key] for key in _RETRY_PERMITTED} == _RETRY_PERMITTED
    assert (row["slurm_comment"], len(row["cohort_members"])) == (held["slurm_comment"], FORCING_MEMBERS)
    # One durable append: the automatic permit's own row record, then the event.
    records = journal_records(repository.root)
    assert records[: len(records_before)] == records_before
    row_record, event_record = records[len(records_before) :]
    assert journal_records(oracle_repository.root) == [*records_before, row_record]
    assert (row_record["record_type"], event_record["record_type"]) == ("pipeline_job", "pipeline_event")
    assert event_record["sequence"] == row_record["sequence"] + 1
    # Every derived file (direct projection, reconcile inventory) is the permit's too.
    jsonl = {name for name in journal_bytes(repository.root) if name.endswith(".jsonl")}
    derived = {name: data for name, data in journal_bytes(repository.root).items() if name not in jsonl}
    assert derived == {
        name: data for name, data in journal_bytes(oracle_repository.root).items() if not name.endswith(".jsonl")
    }
    # The audit trail lives only in the event.
    (event,) = _absence_events(repository.root)
    assert event == event_record["payload"]
    assert {key: event[key] for key in ("entity_type", "entity_id", "event_type", "status_from", "status_to")} == {
        "entity_type": "pipeline_job",
        "entity_id": FORCING_JOB_ID,
        "event_type": "operator_verified_absence",
        "status_from": "reserved",
        "status_to": "reservation_lost",
    }
    assert {
        key: event["details"][key]
        for key in (*_AUDIT_KEYS, "expected_submission_attempt", "expected_submission_attempt_started_at")
    } == {
        "checked_by": "operator-alice",
        "checked_at": CHECKED_AT,
        "verification_note": NOTE,
        "expected_submission_attempt": 1,
        "expected_submission_attempt_started_at": "2026-07-12T00:00:05Z",
    }
    assert not set(_AUDIT_KEYS) & set(row_record["payload"])
    assert "operator-alice" not in json.dumps(row_record) and NOTE not in json.dumps(row_record)
    receipt = result.receipt
    assert isinstance(receipt, OperatorDemoteReceipt)
    assert (receipt.job_id, receipt.status_from, receipt.status_to, receipt.reconciliation_decision) == (
        FORCING_JOB_ID,
        "reserved",
        "reservation_lost",
        "absence_retry_permitted",
    )
    assert (receipt.submission_attempt, receipt.submission_attempt_started_at) == (1, "2026-07-12T00:00:05Z")
    assert (receipt.checked_by, receipt.checked_at, receipt.verification_note) == ("operator-alice", CHECKED_AT, NOTE)
    assert (receipt.written_record_count, receipt.warnings, receipt.lane) == (2, (), "forcing")


def test_the_real_reserve_path_reclaims_the_released_master_as_attempt_two_without_the_audit_fields(
    tmp_path: Path,
) -> None:
    repository = forcing_held_repository(tmp_path)

    def _reserve() -> Any:
        return reserve_candidate(
            repository,
            idempotency_key=FORCING_KEY,
            job_id=FORCING_JOB_ID,
            run_id=FORCING_RUN_ID,
            cycle_id="gfs_2026071200",
            job_type="produce_forcing_array",
            model_id=None,
            stage="forcing",
            candidate_id=FORCING_RUN_ID,
            reservation_evidence={
                "slurm_comment": forcing_comment(1),
                "cohort_members": _forcing_members(FORCING_MEMBERS),
                "restart_stage": "forcing",
                "slurm_ownership_required": True,
                "expected_slurm_user": "scheduler",
                "expected_slurm_account": "account",
            },
        )

    # Held: the real reserve path may not take the master over.
    assert _reserve().created is False
    assert is_unresolved_forcing_attempt(forcing_row(repository))

    _exit(repository)

    assert not is_unresolved_forcing_attempt(forcing_row(repository))
    reservation = _reserve()
    assert (reservation.created, reservation.submission_attempt) == (True, 2)
    row = forcing_row(repository)
    assert (row["status"], row["submission_attempt"], row["slurm_comment"]) == ("reserved", 2, forcing_comment(2))
    assert row["submission_attempt_started_at"] != "2026-07-12T00:00:05Z"
    assert (row["slurm_job_id"], row["submit_outcome"], row["reconciliation_decision"]) == (None, None, None)
    assert not set(_AUDIT_KEYS) & set(row)
    assert "operator-alice" not in json.dumps(row) and NOTE not in json.dumps(row)
    # The attestation stays on attempt 1's event; the reclaim wrote none.
    (event,) = _absence_events(repository.root)
    assert event["details"]["expected_submission_attempt"] == 1


def test_attempt_two_exits_on_its_own_attempt_and_anchor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The reclaim mints attempt 2's anchor from the journal clock inside the lock.
    reclaimed_at = datetime(2026, 7, 12, 0, 10, 5, tzinfo=UTC)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: reclaimed_at)
    repository = forcing_held_repository(tmp_path, attempt=2)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: datetime(2026, 7, 12, 3, 0, tzinfo=UTC))
    held = forcing_row(repository)
    assert (held["submission_attempt"], held["slurm_comment"]) == (2, forcing_comment(2))
    assert held["submission_attempt_started_at"] == "2026-07-12T00:10:05Z"
    # Attempt 1's expectation is stale, whatever anchor rides with it.
    _refused(repository, "stale_attempt", expected_submission_attempt=1)
    _refused(
        repository,
        "stale_attempt",
        expected_submission_attempt=1,
        expected_submission_attempt_started_at=FORCING_ANCHOR,
    )

    _exit(repository)

    assert (forcing_row(repository)["status"], _absence_events(repository.root)[0]["details"]) == (
        "reservation_lost",
        {
            **_absence_events(repository.root)[0]["details"],
            "expected_submission_attempt": 2,
            "expected_submission_attempt_started_at": held["submission_attempt_started_at"],
        },
    )


def test_a_repeated_exit_is_not_held_and_appends_nothing(tmp_path: Path) -> None:
    repository = forcing_held_repository(tmp_path)
    _exit(repository)
    before = journal_bytes(repository.root)

    result = repository.demote_operator_verified_reserved_job(FORCING_JOB_ID, **_kwargs(forcing_row(repository)))

    assert (result.refusal, result.receipt) == ("not_held", None)
    assert journal_bytes(repository.root) == before
    assert len(_absence_events(repository.root)) == 1


def test_a_projection_fault_after_the_append_is_a_warning_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = forcing_held_repository(tmp_path)

    def _fail_direct(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("direct projection write failed")

    monkeypatch.setattr(repository, "_write_pipeline_job_direct_unlocked", _fail_direct)

    result = _exit(repository)

    assert [warning.projection for warning in result.receipt.warnings] == ["pipeline_job_direct"]
    # The journal replay is the authority: the exit is committed.
    replayed = FileOrchestrationJournalRepository(repository.root).get_accepted_submit_pipeline_job(FORCING_JOB_ID)
    assert (replayed["status"], replayed["reconciliation_decision"]) == ("reservation_lost", "absence_retry_permitted")
    assert len(_absence_events(repository.root)) == 1


# --- refusals: attempt, anchor ---------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_submission_attempt": 2},
        {"expected_submission_attempt_started_at": FORCING_ANCHOR + timedelta(seconds=1)},
        {"expected_submission_attempt_started_at": FORCING_ANCHOR - timedelta(microseconds=1)},
    ],
    ids=["attempt", "anchor-later", "anchor-earlier"],
)
def test_attempt_or_anchor_mismatch_is_stale_attempt(tmp_path: Path, overrides: dict[str, Any]) -> None:
    _refused(forcing_held_repository(tmp_path), "stale_attempt", **overrides)


# --- refusals: each leg of the held tuple ----------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "submission_failed"},
        {"slurm_job_id": FORCING_MASTER_ID},
        {"matched_slurm_job_id": FORCING_MASTER_ID},
        {"submit_outcome": "accepted"},
        {"submit_outcome": None},
        {"reconciliation_decision": "accounting_unavailable"},
    ],
    ids=["status", "slurm-job-id", "matched-slurm-job-id", "outcome-accepted", "outcome-absent", "decision"],
)
def test_each_leg_of_the_held_tuple_alone_is_not_held(tmp_path: Path, fields: dict[str, Any]) -> None:
    _refused(_held_with(tmp_path, **fields), "not_held")


def _already_bound(tmp_path: Path) -> Any:
    repository = forcing_held_repository(tmp_path)
    assert repository.bind_forcing_submit_attempt(
        FORCING_KEY,
        expected_submission_attempt=1,
        expected_submission_attempt_started_at=FORCING_ANCHOR,
        slurm_job_id=FORCING_MASTER_ID,
    )
    return repository


def _released(tmp_path: Path) -> Any:
    repository = forcing_held_repository(tmp_path)
    assert repository.permit_forcing_submit_retry(
        FORCING_JOB_ID, expected_submission_attempt=1, expected_submission_attempt_started_at=FORCING_ANCHOR
    )
    return repository


def _reserved_before_the_gateway(tmp_path: Path) -> Any:
    """Reserved by the real reserve path, never sent: no submit outcome yet."""

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    assert reserve_candidate(
        repository,
        idempotency_key=FORCING_KEY,
        job_id=FORCING_JOB_ID,
        run_id=FORCING_RUN_ID,
        cycle_id="gfs_2026071200",
        job_type="produce_forcing_array",
        model_id=None,
        stage="forcing",
        candidate_id=FORCING_RUN_ID,
        reservation_evidence={
            "slurm_comment": forcing_comment(1),
            "cohort_members": _forcing_members(FORCING_MEMBERS),
            "restart_stage": "forcing",
            "submission_attempt": 1,
            "submission_attempt_started_at": FORCING_ANCHOR,
            "slurm_ownership_required": True,
            "expected_slurm_user": "scheduler",
            "expected_slurm_account": "account",
        },
    ).created
    return repository


def _reconciliation_decision_present(tmp_path: Path) -> Any:
    repository = forcing_held_repository(tmp_path)
    assert repository.transition_pipeline_job_submit_evidence(
        FORCING_JOB_ID,
        AcceptedSubmitTransition.accounting(
            "accounting_unavailable",
            submit_outcome="submit_result_ambiguous",
            reconciliation_reason_class="comment_accounting_unproven",
        ),
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    ).committed
    return repository


@pytest.mark.parametrize(
    "build",
    [_already_bound, _released, _reserved_before_the_gateway, _reconciliation_decision_present],
    ids=["already-bound", "already-released", "reserved-before-the-gateway", "decision-present"],
)
def test_a_forcing_row_the_real_writers_moved_out_of_the_held_shape_is_not_held(tmp_path: Path, build: Any) -> None:
    _refused(build(tmp_path), "not_held")


def test_an_incomplete_forcing_identity_is_refused_by_its_own_name(tmp_path: Path) -> None:
    # The complete held tuple, but ownership required with no recorded user.
    repository = forcing_held_repository(tmp_path, owner=(None, "account", True))
    row = forcing_row(repository)
    assert (row["status"], row["submit_outcome"], row["expected_slurm_user"]) == (
        "reserved",
        "submit_result_ambiguous",
        None,
    )
    _refused(repository, "identity_incomplete")


# --- refusals: the attestation and the verification instant ----------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"checked_by": ""},
        {"checked_by": "  \t "},
        {"checked_by": None},
        {"verification_note": ""},
        {"verification_note": " \n "},
        {"verification_note": None},
    ],
    ids=["by-empty", "by-blank", "by-none", "note-empty", "note-blank", "note-none"],
)
def test_a_blank_verifier_or_evidence_is_attestation_missing(tmp_path: Path, overrides: dict[str, Any]) -> None:
    _refused(forcing_held_repository(tmp_path), "attestation_missing", **overrides)


def test_a_blank_attestation_is_refused_before_the_cas_is_evaluated(tmp_path: Path) -> None:
    # Stale attempt AND blank verifier: the attestation is checked first.
    _refused(forcing_held_repository(tmp_path), "attestation_missing", checked_by=" ", expected_submission_attempt=9)


@pytest.mark.parametrize(
    "checked_at",
    ["not-a-time", "2026-07-12T02:30:00", "", None, 1783823400],
    ids=["garbage", "no-timezone", "empty", "none", "epoch-number"],
)
def test_a_malformed_checked_at_is_verification_before_grace(tmp_path: Path, checked_at: Any) -> None:
    _refused(forcing_held_repository(tmp_path), "verification_before_grace", checked_at=checked_at)


def test_a_future_checked_at_is_refused_and_now_itself_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 7, 12, 3, 0, tzinfo=UTC)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: now)
    repository = forcing_held_repository(tmp_path)

    _refused(repository, "verification_before_grace", checked_at=now + timedelta(seconds=1))
    _refused(repository, "verification_before_grace", checked_at="2026-07-12T11:00:01+08:00")

    _exit(repository, checked_at=now)


def test_checked_at_before_the_anchor_plus_the_absence_grace_is_refused(tmp_path: Path) -> None:
    assert reconcile_module.RESERVATION_ABSENCE_GRACE == _GRACE
    repository = forcing_held_repository(tmp_path)

    _refused(repository, "verification_before_grace", checked_at=FORCING_ANCHOR)
    _refused(repository, "verification_before_grace", checked_at=FORCING_ANCHOR - timedelta(hours=1))
    _refused(repository, "verification_before_grace", checked_at=FORCING_ANCHOR + _GRACE - timedelta(microseconds=1))

    _exit(repository, checked_at=FORCING_ANCHOR + _GRACE)


def test_the_grace_is_the_one_reconcile_uses_for_forcing_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reconcile_module, "RESERVATION_ABSENCE_GRACE", timedelta(hours=3))
    repository = forcing_held_repository(tmp_path)

    # 02:30 is two and a half hours after the anchor: inside the moved grace.
    _refused(repository, "verification_before_grace")
    _exit(repository, checked_at=FORCING_ANCHOR + timedelta(hours=3))


def test_an_oversized_attestation_still_raises_typed_with_zero_bytes(tmp_path: Path) -> None:
    repository = forcing_held_repository(tmp_path)
    before = journal_bytes(repository.root)

    for overrides in ({"checked_by": "x" * 257}, {"verification_note": "x" * 2049}):
        with pytest.raises(FileOrchestrationJournalError) as error:
            repository.demote_operator_verified_reserved_job(
                FORCING_JOB_ID, **_kwargs(forcing_row(repository), **overrides)
            )
        assert error.value.reason == "file_journal_evidence_limit_exceeded"
    assert journal_bytes(repository.root) == before


def test_operator_evidence_is_redacted_in_the_event_and_the_receipt(tmp_path: Path) -> None:
    repository = forcing_held_repository(tmp_path)

    result = _exit(repository, verification_note="sacct clean; proof at /home/frd/private/proof password=hunter2")

    (event,) = _absence_events(repository.root)
    durable = json.dumps(journal_records(repository.root))
    assert "hunter2" not in durable and "/home/frd/private/proof" not in durable
    assert result.receipt.verification_note == event["details"]["verification_note"]
    assert "hunter2" not in result.receipt.verification_note


# --- the locked re-read is the authority -----------------------------------------------------


def test_a_concurrent_automatic_bind_before_the_lock_makes_the_locked_reread_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = forcing_held_repository(tmp_path)
    held = forcing_row(repository)
    original = repository._locked_cycle_write
    raced: dict[str, dict[str, bytes]] = {}

    @contextmanager
    def _racing(**kwargs: Any) -> Iterator[None]:
        if "bytes" not in raced:
            rival = FileOrchestrationJournalRepository(repository.root)
            assert rival.bind_forcing_submit_attempt(
                FORCING_KEY,
                expected_submission_attempt=1,
                expected_submission_attempt_started_at=FORCING_ANCHOR,
                slurm_job_id=FORCING_MASTER_ID,
            )
            raced["bytes"] = journal_bytes(repository.root)
        with original(**kwargs):
            yield

    monkeypatch.setattr(repository, "_locked_cycle_write", _racing)
    result = repository.demote_operator_verified_reserved_job(FORCING_JOB_ID, **_kwargs(held))

    assert (getattr(result, "refusal", None), getattr(result, "receipt", None)) == ("not_held", None)
    assert journal_bytes(repository.root) == raced["bytes"]
    assert _absence_events(repository.root) == []
    assert forcing_row(repository)["slurm_job_id"] == FORCING_MASTER_ID


# --- rows the forcing branch does not own ----------------------------------------------------


def test_an_unknown_job_id_keeps_the_none_contract_with_zero_bytes(tmp_path: Path) -> None:
    repository = forcing_held_repository(tmp_path)
    before = journal_bytes(repository.root)

    result = repository.demote_operator_verified_reserved_job(
        "job_cycle_gfs_2026071200_convert_cohort_000000000000_forcing", **_kwargs(forcing_row(repository))
    )

    # No row, so no lane: the pre-#2682 miss contract of the method.
    assert result is None
    assert journal_bytes(repository.root) == before


def test_a_forecast_row_keeps_the_none_and_bare_receipt_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = held_repository(tmp_path, monkeypatch)
    held = held_row(repository)
    before = journal_bytes(repository.root)

    def _forecast_kwargs(**overrides: Any) -> dict[str, Any]:
        return _kwargs(held, checked_at="2026-07-12T02:30:00Z", **overrides)

    assert (
        repository.demote_operator_verified_reserved_job(JOB_ID, **_forecast_kwargs(expected_submission_attempt=2))
        is None
    )
    for overrides in ({"checked_by": " "}, {"verification_note": ""}, {"checked_at": "2026-07-12T02:00:00"}):
        with pytest.raises(FileOrchestrationJournalError):
            repository.demote_operator_verified_reserved_job(JOB_ID, **{**_forecast_kwargs(), **overrides})
    assert journal_bytes(repository.root) == before

    # Neither the absence grace nor the future check belongs to the forecast lane.
    receipt = repository.demote_operator_verified_reserved_job(
        JOB_ID, **{**_forecast_kwargs(), "checked_at": "2999-01-01T00:00:00Z"}
    )

    assert isinstance(receipt, OperatorDemoteReceipt) and receipt.lane == "forecast"
    assert receipt.reconciliation_decision == "operator_verified_absence"
    assert held_row(repository)["reconciliation_decision"] == "operator_verified_absence"


# --- both CLI entrypoints --------------------------------------------------------------------


def _invoke(entrypoint: str, args: list[str]) -> int:
    if entrypoint == "click":
        try:
            return int(cli._click_main(args) or 0)
        except SystemExit as error:
            return int(error.code or 0)
    return cli._argparse_main(args)


def _cli_args(root: Path, row: dict[str, Any], *, confirm: bool = True, **overrides: str) -> list[str]:
    values = {
        "--journal-root": str(root),
        "--job-id": FORCING_JOB_ID,
        "--expected-attempt": str(row["submission_attempt"]),
        "--expected-attempt-started-at": str(row["submission_attempt_started_at"]),
        "--checked-by": "operator-alice",
        "--checked-at": CHECKED_AT,
        "--verification-note": NOTE,
    }
    values.update(overrides)
    args = ["demote-reserved-job"]
    for option, value in values.items():
        args.extend([option, value])
    return [*args, "--confirm"] if confirm else args


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_both_entrypoints_release_a_held_forcing_master(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], entrypoint: str
) -> None:
    repository = forcing_held_repository(tmp_path)

    assert _invoke(entrypoint, _cli_args(repository.root, forcing_row(repository))) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.out.strip() == json.dumps(payload, sort_keys=True)
    assert payload == {
        "command": "demote-reserved-job",
        "status": "demoted",
        "committed": True,
        "lane": "forcing",
        "journal_root": str(repository.root.resolve()),
        "job_id": FORCING_JOB_ID,
        "status_from": "reserved",
        "status_to": "reservation_lost",
        "reconciliation_decision": "absence_retry_permitted",
        "submission_attempt": 1,
        "submission_attempt_started_at": "2026-07-12T00:00:05Z",
        "checked_by": "operator-alice",
        "checked_at": CHECKED_AT,
        "verification_note": NOTE,
        "written_record_count": 2,
        "warnings": [],
    }
    row = forcing_row(repository)
    assert {key: row[key] for key in _RETRY_PERMITTED} == _RETRY_PERMITTED
    assert len(_absence_events(repository.root)) == 1


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
@pytest.mark.parametrize(
    ("overrides", "refusal"),
    [
        ({"--expected-attempt": "2"}, "stale_attempt"),
        ({"--expected-attempt-started-at": "2026-07-12T00:00:06Z"}, "stale_attempt"),
        ({"--checked-at": "2026-07-12T00:02:04Z"}, "verification_before_grace"),
        ({"--checked-at": "2999-01-01T00:00:00Z"}, "verification_before_grace"),
    ],
    ids=["attempt", "anchor", "before-grace", "future"],
)
def test_a_forcing_refusal_exits_2_naming_the_refusal_with_zero_bytes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
    overrides: dict[str, str],
    refusal: str,
) -> None:
    repository = forcing_held_repository(tmp_path)
    before = journal_bytes(repository.root)

    assert _invoke(entrypoint, _cli_args(repository.root, forcing_row(repository), **overrides)) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == _REFUSED_LINE.format(refusal)
    assert journal_bytes(repository.root) == before


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_a_not_held_forcing_row_is_refused_by_name_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], entrypoint: str
) -> None:
    repository = _already_bound(tmp_path)
    before = journal_bytes(repository.root)

    assert _invoke(entrypoint, _cli_args(repository.root, forcing_row(repository))) == 2

    assert capsys.readouterr().err.strip() == _REFUSED_LINE.format("not_held")
    assert journal_bytes(repository.root) == before


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
@pytest.mark.parametrize("option", ["--checked-by", "--verification-note"])
def test_a_blank_attestation_exits_2_naming_the_option_before_the_journal_is_opened(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    option: str,
) -> None:
    repository = forcing_held_repository(tmp_path)
    row = forcing_row(repository)
    before = journal_bytes(repository.root)

    def _never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the repository must not be constructed for a blank attestation")

    monkeypatch.setattr("services.orchestrator.operator_reserved_demotion.FileOrchestrationJournalRepository", _never)

    assert _invoke(entrypoint, _cli_args(repository.root, row, **{option: "   "})) == 2

    assert capsys.readouterr().err.strip() == f"demote-reserved-job {option} must not be blank"
    assert journal_bytes(repository.root) == before


def test_confirm_is_still_required_on_both_entrypoints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import click

    repository = forcing_held_repository(tmp_path)
    args = _cli_args(repository.root, forcing_row(repository), confirm=False)
    before = journal_bytes(repository.root)

    def _never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the repository must not be constructed without --confirm")

    monkeypatch.setattr("services.orchestrator.operator_reserved_demotion.FileOrchestrationJournalRepository", _never)

    with pytest.raises(click.MissingParameter):
        cli._click_main(args)
    assert cli._argparse_main(args) == 2
    assert "--confirm" in capsys.readouterr().err
    assert journal_bytes(repository.root) == before


# --- the reproduction: held forcing master, no job in sacct, members frozen -------------------


def test_a_held_forcing_master_with_no_job_is_released_and_reclaimed_as_attempt_two_by_the_next_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, raw_models, candidates, client = _held_forcing_shape(tmp_path, monkeypatch)
    held = _master(repository, "forcing")
    anchor = datetime.fromisoformat(str(held["submission_attempt_started_at"]).replace("Z", "+00:00"))
    # #2682: the comment-less cluster shows no master for this attempt, pass after pass.
    outcome = _restart_reconcile(repository, monkeypatch, "", anchor + timedelta(hours=7))
    assert outcome.action == "query_unavailable"
    assert _master(repository, "forcing") == held
    _assert_members_held(repository, candidates)
    submissions_before = len(client.submissions)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: anchor + timedelta(hours=8))
    # Oracle: the lane's own automatic permit on a byte copy of the held journal.
    shutil.copytree(repository.root, tmp_path / "oracle")
    oracle_repository = FileOrchestrationJournalRepository(tmp_path / "oracle")
    assert oracle_repository.permit_forcing_submit_retry(
        str(held["job_id"]),
        expected_submission_attempt=int(held["submission_attempt"]),
        expected_submission_attempt_started_at=anchor,
    )

    code = cli.main(
        [
            "demote-reserved-job",
            "--journal-root",
            str(repository.root),
            "--job-id",
            str(held["job_id"]),
            "--expected-attempt",
            str(held["submission_attempt"]),
            "--expected-attempt-started-at",
            str(held["submission_attempt_started_at"]),
            "--checked-by",
            "operator-alice",
            "--checked-at",
            (anchor + timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "--verification-note",
            NOTE,
            "--confirm",
        ]
    )

    assert code in (0, None)
    receipt = json.loads(capsys.readouterr().out)
    assert (receipt["status"], receipt["lane"], receipt["reconciliation_decision"]) == (
        "demoted",
        "forcing",
        "absence_retry_permitted",
    )
    released = _master(repository, "forcing")
    assert (released["status"], released["submission_attempt"]) == ("reservation_lost", held["submission_attempt"])
    assert not is_unresolved_forcing_attempt(released)
    # The scheduler's own held-skip predicate no longer matches the members: they
    # get the decision the automatic permit leaves them (same journal, byte copy).
    for candidate in candidates:
        decision = _decision(repository, candidate)
        assert (decision.action, decision.reason) != _HELD
        assert "held_reservations" not in decision.evidence
        oracle = _decision(oracle_repository, candidate)
        assert (decision.action, decision.reason, decision.evidence.get("restart_stage")) == (
            oracle.action,
            oracle.reason,
            oracle.evidence.get("restart_stage"),
        )
        assert (decision.action, decision.evidence.get("restart_stage")) == ("retry", "forcing")
    selected, blocked, skipped = _plan(_scheduler(tmp_path, repository, raw_models), raw_models)
    assert (blocked, skipped) == ([], [])
    assert sorted(candidate.candidate_id for candidate in selected) == sorted(c.candidate_id for c in candidates)
    # The exit, the decision and the plan reached the gateway zero times.
    assert len(client.submissions) == submissions_before
    assert client.cancelled_jobs == []

    # The real stage submit of the same forcing run reclaims the master as attempt + 1.
    # Function-local, as in the held-block suite's ``_Runtime`` double.
    from services.orchestrator.chain import M3_STAGES, CycleOrchestrationContext

    next_client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path / "next-pass", repository, next_client, terminal_stage="forecast")
    basins = orchestrator._normalize_cycle_basins(_cohort_basins(candidates), "gfs", _CYCLE_TIME)
    result, _aggregation = orchestrator._submit_and_wait_cycle_stage(
        M3_STAGES[1],
        CycleOrchestrationContext(
            source_id="gfs",
            cycle_time=_CYCLE_TIME,
            cycle_id=_CYCLE_ID,
            run_id=str(held["run_id"]),
            all_basins=basins,
            active_basins=list(basins),
            restart_stage="forcing",
        ),
    )

    reclaimed = _master(repository, "forcing")
    assert (result.pipeline_job_id, reclaimed["job_id"]) == (held["job_id"], held["job_id"])
    assert reclaimed["submission_attempt"] == held["submission_attempt"] + 1
    assert reclaimed["submission_attempt_started_at"] != held["submission_attempt_started_at"]
    assert str(reclaimed["slurm_comment"]).endswith(f":a{held['submission_attempt'] + 1}")
    assert reclaimed["slurm_comment"] != held["slurm_comment"]
    forcing_submissions = [item for item in next_client.submissions if item["stage"] == "forcing"]
    assert [item["manifest"]["comment"] for item in forcing_submissions] == [reclaimed["slurm_comment"]]
    assert (result.status, reclaimed["status"]) == ("succeeded", "succeeded")
    assert not set(_AUDIT_KEYS) & set(reclaimed)
    assert "operator-alice" not in json.dumps(reclaimed) and NOTE not in json.dumps(reclaimed)
    assert next_client.cancelled_jobs == [] and client.cancelled_jobs == []
    (event,) = _absence_events(repository.root)
    assert event["details"]["expected_submission_attempt"] == held["submission_attempt"]
