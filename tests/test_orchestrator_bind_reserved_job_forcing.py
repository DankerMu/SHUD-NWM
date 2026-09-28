"""#2675 bind-reserved-job, forcing lane: the typed journal CAS and both CLI entrypoints.

Seam: ``FileOrchestrationJournalRepository.bind_operator_verified_reserved_job``
(and the shipped ``cli._click_main`` / ``cli._argparse_main``) against a real
file journal whose held forcing master was written by the real forcing writers:
``reserve_candidate`` with the forcing reservation evidence plus the restart
reconcile producer's ``_transition_forcing_submit_ambiguity`` (see
``tests/orchestrator_bind_reserved_job_helpers.py``).

The success oracle is independent of the code under test: an identical held
row bound by the lane's own automatic writer, ``bind_forcing_submit_attempt``.
The operator bind must leave exactly that durable row -- no
``slurm_binding_source``, no ``slurm_accounting_submitted_at``, no new token --
plus one ``operator_verified_bind`` event in the same durable append.  Every
refusal is named and leaves every durable byte identical.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli
from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
from services.orchestrator.file_orchestration_journal import (
    OPERATOR_BIND_REFUSALS,
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
)
from tests.orchestrator_bind_reserved_job_helpers import (
    CHECKED_AT,
    FORCING_ANCHOR,
    FORCING_COMMENT,
    FORCING_JOB_ID,
    FORCING_KEY,
    FORCING_MASTER_ID,
    FORCING_SUBMIT,
    FORCING_SUBMIT_LINE,
    JOB_ID,
    after,
    bind_events,
    bind_kwargs,
    forcing_bind_kwargs,
    forcing_comment,
    forcing_held_repository,
    forcing_row,
    held_repository,
    held_row,
    journal_bytes,
    journal_records,
)
from tests.test_real_slurm_gateway import _pinned_local_timezone

pytestmark = pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")

#: Every field of the automatic forcing bind's post-state (``bind_forcing_submit_attempt``).
FORCING_BIND_TUPLE = {
    "status": "submitted",
    "slurm_job_id": FORCING_MASTER_ID,
    "matched_slurm_job_id": FORCING_MASTER_ID,
    "submit_outcome": "accepted",
    "reconciliation_source": "slurm_exact_comment",
    "reconciliation_decision": "matched_bound",
    "reconciliation_reason_class": None,
    "slurm_binding_source": None,
    "slurm_accounting_submitted_at": None,
    "submission_attempt": 1,
    "submission_attempt_started_at": "2026-07-12T00:00:05Z",
    "slurm_comment": FORCING_COMMENT,
}
_FROZEN_NOW = datetime(2026, 7, 12, 2, 31, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _utc_local_time() -> Iterator[None]:
    """The forecast fixtures parse sacct ``Submit`` as local time; the fixture cluster runs in UTC."""

    with _pinned_local_timezone("UTC"):
        yield


def _refused(repository: Any, token: str, **overrides: Any) -> None:
    """The bind refuses by ``token`` and every durable byte stays identical."""

    assert token in OPERATOR_BIND_REFUSALS
    before = journal_bytes(repository.root)
    result = repository.bind_operator_verified_reserved_job(
        FORCING_JOB_ID, **forcing_bind_kwargs(forcing_row(repository), **overrides)
    )
    assert (result.refusal, result.receipt) == (token, None)
    assert journal_bytes(repository.root) == before
    assert bind_events(repository.root) == []


def _bind(repository: Any, **overrides: Any) -> Any:
    result = repository.bind_operator_verified_reserved_job(
        FORCING_JOB_ID, **forcing_bind_kwargs(forcing_row(repository), **overrides)
    )
    assert result.refusal is None, result.refusal
    return result


# --- F2: the exact automatic forcing bind row, one audit event, one append -----------------


def test_bind_writes_exactly_the_automatic_forcing_bind_row_and_one_audit_event_in_one_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(journal_module, "_utcnow", lambda: _FROZEN_NOW)
    oracle_repository = forcing_held_repository(tmp_path / "automatic")
    assert (
        oracle_repository.bind_forcing_submit_attempt(
            FORCING_KEY,
            expected_submission_attempt=1,
            expected_submission_attempt_started_at=FORCING_ANCHOR,
            slurm_job_id=FORCING_MASTER_ID,
            expected_slurm_comment=FORCING_COMMENT,
        )
        is not None
    )
    oracle = forcing_row(oracle_repository)
    repository = forcing_held_repository(tmp_path / "operator")
    records_before = journal_records(repository.root)

    result = _bind(repository)

    bound = forcing_row(repository)
    # The whole durable row, not a chosen subset: same fields, same values.
    assert bound == oracle
    assert {field: bound.get(field) for field in FORCING_BIND_TUPLE} == FORCING_BIND_TUPLE
    records = journal_records(repository.root)
    added = records[len(records_before) :]
    assert [record["record_type"] for record in added] == ["pipeline_job", "pipeline_event"]
    assert added[1]["sequence"] == added[0]["sequence"] + 1
    (event,) = bind_events(repository.root)
    assert event == added[1]["payload"]
    assert (event["entity_id"], event["status_from"], event["status_to"]) == (FORCING_JOB_ID, "reserved", "submitted")
    assert event["details"] == {
        "lane": "forcing",
        "checked_by": "operator-alice",
        "checked_at": CHECKED_AT,
        "verification_note": forcing_bind_kwargs(bound)["verification_note"],
        "submitline_key": FORCING_COMMENT,
        "array_spec": "0-2%15",
        "slurm_job_id": FORCING_MASTER_ID,
        "slurm_submit_time": FORCING_SUBMIT,
        "slurm_user": "scheduler",
        "slurm_account": "account",
        "expected_submission_attempt": 1,
        "expected_submission_attempt_started_at": "2026-07-12T00:00:05Z",
        "prior_status": "reserved",
        "prior_submit_outcome": "submit_result_ambiguous",
        "prior_reconciliation_source": "",
        "prior_reconciliation_decision": "",
        "prior_reconciliation_reason_class": "",
    }
    receipt = result.receipt
    assert (receipt.lane, receipt.array_spec, receipt.written_record_count, receipt.warnings) == (
        "forcing",
        "0-2%15",
        2,
        (),
    )
    assert (receipt.reconciliation_source, receipt.submitline_key) == ("slurm_exact_comment", FORCING_COMMENT)
    # The automatic writer wrote no event; the operator bind wrote exactly one.
    assert bind_events(oracle_repository.root) == []
    # A fresh reader replays the same row (the append is the authority).
    assert FileOrchestrationJournalRepository(repository.root).get_pipeline_job(FORCING_JOB_ID) == bound
    # A repeated request is a zero-write ``not_held``.
    before = journal_bytes(repository.root)
    repeated = repository.bind_operator_verified_reserved_job(FORCING_JOB_ID, **forcing_bind_kwargs(bound))
    assert (repeated.refusal, journal_bytes(repository.root)) == ("not_held", before)


@pytest.mark.parametrize(
    "submit_line",
    [
        f"sbatch --array=0-2 --comment={FORCING_COMMENT} /tmp/x.sbatch",
        f"sbatch --comment={FORCING_COMMENT} --array=0-2%1 --comment={FORCING_COMMENT} /tmp/x.sbatch",
    ],
    ids=["unthrottled", "repeated-identical-comment"],
)
def test_other_valid_submitline_spellings_bind(tmp_path: Path, submit_line: str) -> None:
    repository = forcing_held_repository(tmp_path)
    assert _bind(repository, submit_line=submit_line).receipt.array_spec == submit_line.split("--array=")[1].split()[0]


def test_submit_time_equal_to_the_anchor_or_the_check_time_is_inside_the_window(tmp_path: Path) -> None:
    anchor = forcing_held_repository(tmp_path / "anchor")
    assert _bind(anchor, slurm_submit_time="2026-07-12T00:00:05Z").receipt is not None
    checked = forcing_held_repository(tmp_path / "checked")
    assert _bind(checked, slurm_submit_time=CHECKED_AT).receipt is not None


# --- F2b: owner evidence, the inflight ``_forcing_accounting_identity_matches`` rule ---------


@pytest.mark.parametrize(
    "overrides",
    [
        {"slurm_user": None},
        {"slurm_account": None},
        {"slurm_user": "", "slurm_account": ""},
        {"slurm_user": "intruder"},
        {"slurm_account": "other_account"},
    ],
    ids=["no-user", "no-account", "both-blank", "foreign-user", "foreign-account"],
)
def test_ownership_required_refuses_a_missing_or_foreign_owner(tmp_path: Path, overrides: dict[str, Any]) -> None:
    _refused(forcing_held_repository(tmp_path), "slurm_owner_mismatch", **overrides)


@pytest.mark.parametrize(
    ("owner", "overrides"),
    [
        (("scheduler", None, False), {"slurm_user": "intruder"}),
        (("scheduler", None, False), {"slurm_user": None}),
        ((None, "account", False), {"slurm_account": "other_account"}),
    ],
    ids=["expected-user-differs", "expected-user-missing", "expected-account-differs"],
)
def test_a_recorded_owner_must_match_even_without_the_ownership_flag(
    tmp_path: Path, owner: tuple[str | None, str | None, bool], overrides: dict[str, Any]
) -> None:
    _refused(forcing_held_repository(tmp_path, owner=owner), "slurm_owner_mismatch", **overrides)


def test_a_row_with_no_expected_owner_and_no_requirement_ignores_the_owner_arguments(tmp_path: Path) -> None:
    repository = forcing_held_repository(tmp_path, owner=(None, None, False))
    result = _bind(repository, slurm_user="anyone", slurm_account=None)
    assert result.receipt.status_to == "submitted"
    (event,) = bind_events(repository.root)
    assert (event["details"]["slurm_user"], event["details"]["slurm_account"]) == ("anyone", "")


def test_a_non_string_owner_raises_typed_before_any_read(tmp_path: Path) -> None:
    repository = forcing_held_repository(tmp_path)
    before = journal_bytes(repository.root)
    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.bind_operator_verified_reserved_job(
            FORCING_JOB_ID, **forcing_bind_kwargs(forcing_row(repository), slurm_account=7)
        )
    assert error.value.field == "slurm_account"
    assert journal_bytes(repository.root) == before


# --- F3 / F4: attempt, anchor, window -----------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_submission_attempt": 2},
        {"expected_submission_attempt_started_at": "2026-07-12T00:00:06Z"},
    ],
    ids=["attempt", "anchor"],
)
def test_attempt_or_anchor_mismatch_is_stale_attempt(tmp_path: Path, overrides: dict[str, Any]) -> None:
    _refused(forcing_held_repository(tmp_path), "stale_attempt", **overrides)


@pytest.mark.parametrize(
    "submit",
    ["2026-07-12T00:00:04Z", after(CHECKED_AT, seconds=1)],
    ids=["before-anchor", "after-checked-at"],
)
def test_submit_time_outside_the_attempt_window_is_refused(tmp_path: Path, submit: str) -> None:
    _refused(forcing_held_repository(tmp_path), "submit_time_outside_attempt_window", slurm_submit_time=submit)


# --- F5: the SubmitLine comment is this attempt's own ----------------------------------------


def _attempt_two(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    """Attempt 1 released through the absence exit, the same key re-reserved and held again."""

    repository = forcing_held_repository(tmp_path, attempt=2)
    row = forcing_row(repository)
    anchor = datetime.fromisoformat(str(row["submission_attempt_started_at"]).replace("Z", "+00:00"))
    window = {
        "slurm_submit_time": (anchor + timedelta(seconds=30)).isoformat(),
        "checked_at": (anchor + timedelta(hours=1)).isoformat(),
    }
    return repository, window


def test_attempt_two_binds_on_its_own_comment(tmp_path: Path) -> None:
    repository, window = _attempt_two(tmp_path)
    own = f"sbatch --array=0-2%15 --comment={forcing_comment(2)} /tmp/x.sbatch"
    assert _bind(repository, submit_line=own, **window).receipt.submitline_key == forcing_comment(2)


@pytest.mark.parametrize(
    "comment_tokens",
    [
        f"--comment={forcing_comment(1)}",
        f"--comment={forcing_comment(2, key='cycle_gfs_2026071200_convert_cohort_000000000000:forcing')}",
        f"--comment=nhms_idem:{FORCING_KEY}",
        "",
        f"--comment={forcing_comment(2)} --comment={forcing_comment(1)}",
    ],
    ids=["previous-attempt", "other-key", "forecast-idem-comment", "no-comment", "two-distinct"],
)
def test_a_submitline_comment_that_is_not_this_attempts_is_refused(tmp_path: Path, comment_tokens: str) -> None:
    repository, window = _attempt_two(tmp_path)
    line = f"/usr/bin/sbatch --array=0-2%15 {comment_tokens} /tmp/x.sbatch"
    _refused(repository, "submitline_key_mismatch", submit_line=line, **window)


# --- F6: exactly one ``--array=0-<n-1>[%k]`` for the n cohort members ------------------------


@pytest.mark.parametrize(
    "array_tokens",
    [
        "",
        "--array=0-1%15",
        "--array=0-3",
        "--array=1-3",
        "--array=0,1,2",
        "--array=0-2,5",
        "--array=0-02",
        "--array=0-2%0",
        "--array=",
        "--array=0-2 --array=0-2",
        "--array 0-2",
        "-a 0-2 --array=0-2",
        "-a0-2",
    ],
    ids=[
        "missing",
        "too-small",
        "too-large",
        "not-from-zero",
        "list",
        "range-plus-list",
        "leading-zero",
        "zero-throttle",
        "empty",
        "duplicated",
        "space-form",
        "short-plus-long",
        "short-form",
    ],
)
def test_an_array_spec_that_is_not_one_exact_range_for_the_members_is_refused(
    tmp_path: Path, array_tokens: str
) -> None:
    line = f"/usr/bin/sbatch {array_tokens} --comment={FORCING_COMMENT} /tmp/x.sbatch"
    _refused(forcing_held_repository(tmp_path), "array_spec_mismatch", submit_line=line)


def test_a_single_member_array_is_zero_to_zero(tmp_path: Path) -> None:
    repository = forcing_held_repository(tmp_path, members=1)
    _refused(repository, "array_spec_mismatch")  # the fixture line is 0-2 for three members
    one = f"sbatch --array=0-0%15 --comment={FORCING_COMMENT} /tmp/x.sbatch"
    assert _bind(repository, submit_line=one).receipt.array_spec == "0-0%15"


# --- F7: canonical, unclaimed Slurm id -----------------------------------------------------


@pytest.mark.parametrize(
    "slurm_job_id", [f"0{FORCING_MASTER_ID}", f"{FORCING_MASTER_ID}_0", f"{FORCING_MASTER_ID}.batch"]
)
def test_a_non_canonical_slurm_id_is_invalid(tmp_path: Path, slurm_job_id: str) -> None:
    _refused(forcing_held_repository(tmp_path), "slurm_id_invalid", slurm_job_id=slurm_job_id)


def _same_cycle_forcing_bound(repository: Any, slurm_job_id: str) -> None:
    key = "cycle_gfs_2026071200_forcing_fixture:forcing"
    repository.reserve_pipeline_job(
        {
            "job_id": "job_cycle_gfs_2026071200_forcing_fixture_forcing",
            "run_id": "cycle_gfs_2026071200_forcing_fixture",
            "cycle_id": "gfs_2026071200",
            "job_type": "produce_forcing_array",
            "stage": "forcing",
            "idempotency_key": key,
            "slurm_comment": f"nhms_forcing_attempt:{key}:a1",
        }
    )
    assert repository.bind_pipeline_job_reservation(key, slurm_job_id=slurm_job_id) is not None


def _same_cycle_member_task(repository: Any, slurm_job_id: str) -> None:
    repository.append_historical_pipeline_job(
        {
            "job_id": "job_fcst_gfs_2026071200_model_5",
            "run_id": "fcst_gfs_2026071200_model_5",
            "cycle_id": "gfs_2026071200",
            "job_type": "run_shud_forecast_array",
            "stage": "forecast",
            "model_id": "model_5",
            "array_task_id": 5,
            "status": "running",
            "slurm_job_id": slurm_job_id,
        }
    )


def _same_cycle_forecast_master_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slurm_job_id: str) -> Any:
    from tests.gateway_reconcile_helpers import _bind_current_file_cohort

    repository = held_repository(tmp_path, monkeypatch)
    held = held_row(repository)
    _bind_current_file_cohort(repository, str(held["idempotency_key"]), slurm_job_id=slurm_job_id)
    return forcing_held_repository(tmp_path, repository=repository)


def _other_cycle_forecast_master_bound(tmp_path: Path, slurm_job_id: str) -> Any:
    from tests.gateway_reconcile_helpers import _bind_current_file_cohort, _file_cohort_repository

    repository = _file_cohort_repository(
        tmp_path / "ifs", member_count=1, expected_user="scheduler", expected_account="account", source_id="ifs"
    )
    (held,) = [row for row in repository.query_pipeline_jobs_by_cycle("ifs_2026071200") if row.get("cohort_members")]
    _bind_current_file_cohort(repository, str(held["idempotency_key"]), slurm_job_id=slurm_job_id)
    return forcing_held_repository(tmp_path, repository=repository)


@pytest.mark.parametrize(
    "claimant",
    ["same-cycle-forcing", "same-cycle-member-task", "same-cycle-forecast-master", "other-cycle-forecast-master"],
)
def test_a_slurm_id_claimed_by_another_row_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claimant: str
) -> None:
    if claimant == "same-cycle-forcing":
        repository = forcing_held_repository(tmp_path)
        _same_cycle_forcing_bound(repository, FORCING_MASTER_ID)
    elif claimant == "same-cycle-member-task":
        repository = forcing_held_repository(tmp_path)
        _same_cycle_member_task(repository, f"{FORCING_MASTER_ID}_5")
    elif claimant == "same-cycle-forecast-master":
        repository = _same_cycle_forecast_master_bound(tmp_path, monkeypatch, FORCING_MASTER_ID)
    else:
        repository = _other_cycle_forecast_master_bound(tmp_path, FORCING_MASTER_ID)
    _refused(repository, "slurm_id_claimed")


def test_an_unrelated_claimed_id_does_not_block_the_bind(tmp_path: Path) -> None:
    repository = _other_cycle_forecast_master_bound(tmp_path, "70001")
    _same_cycle_forcing_bound(repository, "70002")
    assert _bind(repository).receipt.matched_slurm_job_id == FORCING_MASTER_ID


# --- F7b: a held forecast master is never a window claimant of a forcing bind ----------------


def test_a_held_forecast_master_whose_window_contains_the_submit_time_does_not_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same source, same cycle, same owner, window open at the submit time (design Decision 2)."""

    repository = held_repository(tmp_path, monkeypatch)
    forecast = held_row(repository)
    assert (forecast["status"], forecast["slurm_job_id"], forecast["expected_slurm_user"]) == (
        "reserved",
        None,
        "scheduler",
    )
    assert forecast["submission_attempt_started_at"] <= FORCING_SUBMIT
    forcing_held_repository(tmp_path, repository=repository)

    assert _bind(repository).receipt.status_to == "submitted"
    # The held forecast master is untouched.
    assert held_row(repository) == forecast


# --- F8: not held --------------------------------------------------------------------------


def _already_bound(tmp_path: Path) -> Any:
    repository = forcing_held_repository(tmp_path)
    assert (
        repository.bind_forcing_submit_attempt(
            FORCING_KEY,
            expected_submission_attempt=1,
            expected_submission_attempt_started_at=FORCING_ANCHOR,
            slurm_job_id=FORCING_MASTER_ID,
        )
        is not None
    )
    return repository


def _released(tmp_path: Path) -> Any:
    repository = forcing_held_repository(tmp_path)
    assert repository.permit_forcing_submit_retry(
        FORCING_JOB_ID, expected_submission_attempt=1, expected_submission_attempt_started_at=FORCING_ANCHOR
    )
    return repository


def _identity_incomplete(tmp_path: Path) -> Any:
    """A forcing reservation without the member map (a sparse historical row shape)."""

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.reserve_pipeline_job(
        {
            "job_id": FORCING_JOB_ID,
            "run_id": "cycle_gfs_2026071200_convert_cohort_36e4f7b9bf80",
            "cycle_id": "gfs_2026071200",
            "job_type": "produce_forcing_array",
            "stage": "forcing",
            "idempotency_key": FORCING_KEY,
            "slurm_comment": FORCING_COMMENT,
            "submission_attempt": 1,
            "submission_attempt_started_at": FORCING_ANCHOR,
        }
    )
    return repository


def _outcome_not_ambiguous(tmp_path: Path) -> Any:
    """Reserved, never reached the gateway: no submit outcome yet."""

    from services.orchestrator.reservation import reserve_candidate
    from tests.orchestrator_bind_reserved_job_helpers import _forcing_members

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    assert reserve_candidate(
        repository,
        idempotency_key=FORCING_KEY,
        job_id=FORCING_JOB_ID,
        run_id="cycle_gfs_2026071200_convert_cohort_36e4f7b9bf80",
        cycle_id="gfs_2026071200",
        job_type="produce_forcing_array",
        model_id=None,
        stage="forcing",
        candidate_id="cycle_gfs_2026071200_convert_cohort_36e4f7b9bf80",
        reservation_evidence={
            "slurm_comment": FORCING_COMMENT,
            "cohort_members": _forcing_members(3),
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
    result = repository.transition_pipeline_job_submit_evidence(
        FORCING_JOB_ID,
        AcceptedSubmitTransition.accounting(
            "accounting_unavailable",
            submit_outcome="submit_result_ambiguous",
            reconciliation_reason_class="comment_accounting_unproven",
        ),
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    )
    assert result.committed
    assert forcing_row(repository)["reconciliation_decision"] == "accounting_unavailable"
    return repository


@pytest.mark.parametrize(
    "build",
    [_already_bound, _released, _identity_incomplete, _outcome_not_ambiguous, _reconciliation_decision_present],
    ids=["already-bound", "not-reserved", "identity-incomplete", "outcome-not-ambiguous", "decision-present"],
)
def test_a_forcing_row_outside_the_held_shape_is_not_held(tmp_path: Path, build: Any) -> None:
    _refused(build(tmp_path), "not_held")


# --- F12: a concurrent automatic forcing bind wins the lock first --------------------------


def test_a_concurrent_automatic_forcing_bind_before_the_lock_makes_the_locked_reread_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = forcing_held_repository(tmp_path)
    held = forcing_row(repository)
    original = repository._locked_cycle_write
    raced: dict[str, dict[str, bytes]] = {}

    @contextmanager
    def _racing(**kwargs: Any) -> Iterator[None]:
        rival = FileOrchestrationJournalRepository(repository.root)
        assert (
            rival.bind_forcing_submit_attempt(
                FORCING_KEY,
                expected_submission_attempt=1,
                expected_submission_attempt_started_at=FORCING_ANCHOR,
                slurm_job_id=FORCING_MASTER_ID,
                reconciliation_source="slurm_controller_exact_comment",
            )
            is not None
        )
        raced["bytes"] = journal_bytes(repository.root)
        with original(**kwargs):
            yield

    monkeypatch.setattr(repository, "_locked_cycle_write", _racing)
    result = repository.bind_operator_verified_reserved_job(FORCING_JOB_ID, **forcing_bind_kwargs(held))

    assert (result.refusal, result.receipt) == ("not_held", None)
    assert journal_bytes(repository.root) == raced["bytes"]
    assert bind_events(repository.root) == []
    assert forcing_row(repository)["reconciliation_source"] == "slurm_controller_exact_comment"


# --- forecast rows ignore the forcing owner arguments --------------------------------------


def test_a_forecast_bind_ignores_the_owner_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plain = held_repository(tmp_path / "plain", monkeypatch)
    with_owner = held_repository(tmp_path / "owner", monkeypatch)
    assert plain.bind_operator_verified_reserved_job(JOB_ID, **bind_kwargs(held_row(plain))).refusal is None
    result = with_owner.bind_operator_verified_reserved_job(
        JOB_ID, **bind_kwargs(held_row(with_owner), slurm_user="intruder", slurm_account=None)
    )
    assert (result.refusal, result.receipt.lane, result.receipt.array_spec) == (None, "forecast", None)
    drop = ("updated_at", "submitted_at")
    assert {k: v for k, v in held_row(with_owner).items() if k not in drop} == {
        k: v for k, v in held_row(plain).items() if k not in drop
    }
    (event,) = bind_events(with_owner.root)
    assert "lane" not in event["details"] and "slurm_user" not in event["details"]


# --- F10: both CLI entrypoints -------------------------------------------------------------


def _invoke(entrypoint: str, args: list[str]) -> int:
    if entrypoint == "click":
        try:
            return int(cli._click_main(args) or 0)
        except SystemExit as error:
            return int(error.code or 0)
    return cli._argparse_main(args)


def _forcing_cli_args(root: Path, row: dict[str, Any], **overrides: str | None) -> list[str]:
    values: dict[str, str | None] = {
        "--journal-root": str(root),
        "--job-id": FORCING_JOB_ID,
        "--slurm-job-id": FORCING_MASTER_ID,
        "--slurm-submit-time": FORCING_SUBMIT,
        "--submit-line": FORCING_SUBMIT_LINE,
        "--expected-attempt": str(row["submission_attempt"]),
        "--expected-attempt-started-at": str(row["submission_attempt_started_at"]),
        "--checked-by": "operator-alice",
        "--checked-at": CHECKED_AT,
        "--verification-note": "sacct --jobs=56838 re-read: nhms_forcing scheduler/account",
        "--slurm-user": "scheduler",
        "--slurm-account": "account",
    }
    values.update(overrides)
    args = ["bind-reserved-job"]
    for option, value in values.items():
        if value is not None:
            args.extend([option, value])
    return [*args, "--confirm"]


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_both_entrypoints_bind_a_held_forcing_master(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], entrypoint: str
) -> None:
    repository = forcing_held_repository(tmp_path)

    assert _invoke(entrypoint, _forcing_cli_args(repository.root, forcing_row(repository))) == 0

    payload = json.loads(capsys.readouterr().out)
    assert {key: payload[key] for key in ("status", "job_id", "status_to", "lane", "array_spec")} == {
        "status": "bound",
        "job_id": FORCING_JOB_ID,
        "status_to": "submitted",
        "lane": "forcing",
        "array_spec": "0-2%15",
    }
    assert (payload["matched_slurm_job_id"], payload["submitline_key"]) == (FORCING_MASTER_ID, FORCING_COMMENT)
    assert FORCING_SUBMIT_LINE not in json.dumps(payload)
    assert {key: forcing_row(repository)[key] for key in FORCING_BIND_TUPLE} == FORCING_BIND_TUPLE
    assert len(bind_events(repository.root)) == 1


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
@pytest.mark.parametrize(
    ("overrides", "refusal"),
    [
        ({"--slurm-user": None}, "slurm_owner_mismatch"),
        ({"--slurm-account": "other_account"}, "slurm_owner_mismatch"),
        ({"--submit-line": f"sbatch --array=0-1%15 --comment={FORCING_COMMENT} x.sbatch"}, "array_spec_mismatch"),
        ({"--submit-line": f"sbatch --array=0-2 --comment=nhms_idem:{FORCING_KEY} x"}, "submitline_key_mismatch"),
    ],
)
def test_a_forcing_refusal_exits_2_on_stderr_with_zero_bytes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
    overrides: dict[str, str | None],
    refusal: str,
) -> None:
    repository = forcing_held_repository(tmp_path)
    before = journal_bytes(repository.root)

    assert _invoke(entrypoint, _forcing_cli_args(repository.root, forcing_row(repository), **overrides)) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == f"bind-reserved-job: refused: {refusal}; no journal bytes were written"
    assert journal_bytes(repository.root) == before
