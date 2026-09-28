"""#2675: a held forcing master bound by ``bind-reserved-job`` releases its members.

Production shape: the forcing array submit was accepted by Slurm but the gateway
answer was lost, so the forcing master stays ``reserved`` /
``submit_result_ambiguous`` with its complete forcing submit identity.  Restart
reconcile on the comment-less cluster then reports ``multiple_matches_blocked``,
``identity_mismatch_blocked`` or ``query_unavailable`` pass after pass and never
binds, and #2667 skips every member ``active_duplicate_pipeline``.

Seams, all real except the Slurm runtime and ``scontrol`` / ``sacct`` stdout: the
``orchestrate_cycle`` stage loop writes the journal through a real
``FileOrchestrationJournalRepository``; ``reconcile_reserved_unbound_jobs`` is the
restart reconcile; the shipped CLI ``bind-reserved-job`` binds;
``reconcile_inflight_jobs`` projects accounting; the scheduler's
``_candidate_state_decision`` and the planner's ``_build_candidates`` judge the
members.  The tuple oracle is the lane's own automatic writer,
``bind_forcing_submit_attempt``, on a byte copy of the same held journal.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli
from services.orchestrator import reconcile as reconcile_module
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from services.orchestrator.operator_action_listing_held import (
    _held_job_is_forcing_master,
    _held_job_is_forecast_master,
)
from tests.test_orchestration_chain import FakeCycleSlurmClient, _orchestrator
from tests.test_real_slurm_gateway import _pinned_local_timezone
from tests.test_scheduler_held_reservation_block import (
    _CYCLE,
    _HELD,
    _candidates,
    _cohort_basins,
    _decision,
    _forcing_witness,
    _master,
    _plan,
    _raw_models,
    _scheduler,
    _state,
)

pytestmark = pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")

#: Every field of the automatic forcing bind's post-state.
_TUPLE = (
    "status",
    "slurm_job_id",
    "matched_slurm_job_id",
    "submit_outcome",
    "reconciliation_source",
    "reconciliation_decision",
    "reconciliation_reason_class",
    "slurm_binding_source",
    "slurm_accounting_submitted_at",
    "submission_attempt",
    "submission_attempt_started_at",
    "slurm_comment",
)


@pytest.fixture(autouse=True)
def _planner_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")
    monkeypatch.delenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", raising=False)
    with _pinned_local_timezone("UTC"):
        yield


class _ForcingAnswerLost(FakeCycleSlurmClient):
    """Slurm accepts the forcing array, the gateway answer never arrives (the #2675 shape)."""

    def __init__(self) -> None:
        super().__init__()
        self.forcing_master_id: str | None = None
        self.forcing_comment: str | None = None
        self.forcing_task_count = 0

    def submit_job_array(self, job_type: str, **kwargs: Any) -> dict[str, Any]:
        accepted = super().submit_job_array(job_type, **kwargs)
        if kwargs["stage_name"] == "forcing":
            # Function-local, as in the held-block suite's ``_Runtime`` double.
            from services.orchestrator.chain_types import OrchestratorError

            self.forcing_master_id = next(job_id for job_id, job in self.jobs.items() if job["stage"] == "forcing")
            self.forcing_comment = str(kwargs["manifest"]["comment"])
            self.forcing_task_count = len(kwargs["tasks"])
            raise OrchestratorError("SLURM_TIMEOUT", "gateway read timed out after Slurm accepted the array")
        return accepted


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _controller_rows(master_id: str, *, tasks: int, comment: str, submit: datetime, user: str = "scheduler") -> str:
    submit_text = submit.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return "".join(
        f"JobId={int(master_id) + task + 1} ArrayJobId={master_id} ArrayTaskId={task} JobName=nhms_forcing "
        f"UserId={user}(1000) Account=account JobState=RUNNING ExitCode=0:0 SubmitTime={submit_text} "
        f"Comment={comment}\n"
        for task in range(tasks)
    )


def _held_forcing_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, members: int = 2
) -> tuple[FileOrchestrationJournalRepository, list[dict[str, Any]], list[Any], _ForcingAnswerLost]:
    raw_models = _raw_models(range(members))
    candidates = _candidates(raw_models)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = _ForcingAnswerLost()
    orchestrator = _orchestrator(tmp_path / "first-pass", repository, client, terminal_stage="forecast")
    orchestrator.config = dataclasses.replace(
        orchestrator.config, reconcile_slurm_user="scheduler", reconcile_slurm_account="account"
    )
    # The pass itself only records the ambiguity; nothing in it may prove absence.
    monkeypatch.setattr(
        reconcile_module,
        "_bounded_visibility_stdout",
        lambda command: "AccountingStoreFlags = (null)\n" if list(command)[-2:] == ["show", "config"] else "",
    )
    orchestrator.orchestrate_cycle("gfs", _CYCLE, _cohort_basins(candidates))
    held = _master(repository, "forcing")
    assert (held["status"], held["slurm_job_id"], held["submit_outcome"]) == (
        "reserved",
        None,
        "submit_result_ambiguous",
    )
    assert (held["slurm_comment"], len(held["cohort_members"])) == (client.forcing_comment, members)
    assert client.forcing_master_id is not None and client.forcing_task_count == members
    # The listing reads this orchestrator-minted id as a forcing master.
    assert _held_job_is_forcing_master(held["job_id"]) and not _held_job_is_forecast_master(held["job_id"])
    _forcing_witness(monkeypatch, tmp_path, candidates)
    return repository, raw_models, candidates, client


def _assert_members_held(repository: Any, candidates: list[Any]) -> None:
    for candidate in candidates:
        decision = _decision(repository, candidate)
        assert (decision.action, decision.reason) == _HELD
        (held,) = decision.evidence["held_reservations"]
        assert (held["stage"], held["status"], held.get("slurm_job_id")) == ("forcing", "reserved", None)


def _restart_reconcile(
    repository: Any, monkeypatch: pytest.MonkeyPatch, controller_stdout: str, query_end: datetime
) -> Any:
    monkeypatch.setattr(
        reconcile_module,
        "_bounded_visibility_stdout",
        lambda command: (
            "AccountingStoreFlags = (null)\n" if list(command)[-2:] == ["show", "config"] else controller_stdout
        ),
    )
    monkeypatch.setattr(
        reconcile_module,
        "_bounded_sacct_stdout",
        lambda _command: pytest.fail("comment-less forcing reconcile must not query sacct"),
    )
    query = reconcile_module.default_comment_sacct_querier("/opt/slurm/bin", now=lambda: query_end)
    (outcome,) = reconcile_module.reconcile_reserved_unbound_jobs(repository, comment_query=query)
    return outcome


def _single_record_foreign_owner(repository: Any, monkeypatch: pytest.MonkeyPatch, submit: datetime) -> Any:
    """Comment-storing accounting returns one record with this attempt's comment and a foreign owner."""

    comment = _master(repository, "forcing")["slurm_comment"]
    monkeypatch.setattr(
        reconcile_module,
        "_bounded_sacct_stdout",
        lambda _command: f"9001_0|nhms_forcing|RUNNING|0:0|{comment}|intruder|account\n",
    )
    query = reconcile_module.default_comment_sacct_querier(
        "/opt/slurm/bin",
        comment_storage_probe=lambda: True,
        global_visibility_probe=lambda: True,
        now=lambda: submit + timedelta(minutes=1),
    )
    (outcome,) = reconcile_module.reconcile_reserved_unbound_jobs(repository, comment_query=query)
    return outcome


# --- F1: the reproduction -----------------------------------------------------------------


def test_every_unresolvable_restart_reconcile_keeps_the_forcing_master_held_and_its_members_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _raw, candidates, client = _held_forcing_shape(tmp_path, monkeypatch)
    anchor = datetime.fromisoformat(
        str(_master(repository, "forcing")["submission_attempt_started_at"]).replace("Z", "+00:00")
    )
    submit = anchor + timedelta(seconds=30)
    comment = str(client.forcing_comment)
    _assert_members_held(repository, candidates)

    outcomes = [
        _restart_reconcile(
            repository,
            monkeypatch,
            _controller_rows(str(client.forcing_master_id), tasks=2, comment=comment, submit=submit)
            + _controller_rows("9100", tasks=2, comment=comment, submit=submit),
            anchor + timedelta(hours=7),
        ),
        _single_record_foreign_owner(repository, monkeypatch, submit),
        _restart_reconcile(repository, monkeypatch, "", anchor + timedelta(hours=7)),
    ]

    assert [outcome.action for outcome in outcomes] == [
        "multiple_matches_blocked",
        "identity_mismatch_blocked",
        "query_unavailable",
    ]
    held = _master(repository, "forcing")
    assert (held["status"], held["slurm_job_id"], held["matched_slurm_job_id"], held["reconciliation_decision"]) == (
        "reserved",
        None,
        None,
        None,
    )
    _assert_members_held(repository, candidates)


# --- F2: bind -> inflight reconcile -> the members' candidate decision ------------------------


def _cli_bind(repository: Any, client: _ForcingAnswerLost, submit: datetime, capsys: Any) -> dict[str, Any]:
    held = _master(repository, "forcing")
    array = f"--array=0-{client.forcing_task_count - 1}%15"
    args = [
        "bind-reserved-job",
        "--journal-root",
        str(repository.root),
        "--job-id",
        str(held["job_id"]),
        "--slurm-job-id",
        str(client.forcing_master_id),
        "--slurm-submit-time",
        _iso(submit),
        "--submit-line",
        f"/usr/bin/sbatch {array} --comment={client.forcing_comment} /tmp/nhms_forcing_x.sbatch",
        "--expected-attempt",
        str(held["submission_attempt"]),
        "--expected-attempt-started-at",
        str(held["submission_attempt_started_at"]),
        "--checked-by",
        "operator-alice",
        "--checked-at",
        _iso(submit + timedelta(hours=7)),
        "--verification-note",
        "sacct --jobs=<id> --parsable2 -o JobID,JobName,User,Account,Submit,SubmitLine re-read",
        "--slurm-user",
        "scheduler",
        "--slurm-account",
        "account",
        "--confirm",
    ]
    assert cli.main(args) in (0, None)
    return json.loads(capsys.readouterr().out)


def _completed_forcing_accounting(master_id: str, members: int) -> Any:
    from services.orchestrator.reconcile import SacctRecord

    tasks = tuple(
        SacctRecord(
            f"{master_id}_{index}",
            "COMPLETED",
            "nhms_forcing",
            exit_code="0:0",
            user="scheduler",
            account="account",
            array_task_id=index,
        )
        for index in range(members)
    )
    return SacctRecord(
        slurm_job_id=master_id,
        raw_state="COMPLETED",
        job_name="nhms_forcing",
        exit_code="0:0",
        user="scheduler",
        account="account",
        array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
        array_task_records=tasks,
    )


def test_a_bound_held_forcing_master_projects_terminal_and_its_members_move_on_to_forecast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, raw_models, candidates, client = _held_forcing_shape(tmp_path, monkeypatch)
    held = _master(repository, "forcing")
    anchor = datetime.fromisoformat(str(held["submission_attempt_started_at"]).replace("Z", "+00:00"))
    submit = anchor + timedelta(seconds=30)
    master_id = str(client.forcing_master_id)
    # The same row held by the restart reconcile a pass would run (multiple masters).
    outcome = _restart_reconcile(
        repository,
        monkeypatch,
        _controller_rows(master_id, tasks=2, comment=str(client.forcing_comment), submit=submit)
        + _controller_rows("9100", tasks=2, comment=str(client.forcing_comment), submit=submit),
        anchor + timedelta(hours=7),
    )
    assert outcome.action == "multiple_matches_blocked"
    _assert_members_held(repository, candidates)
    # Oracle: the lane's own automatic bind on a byte copy of the held journal.
    shutil.copytree(repository.root, tmp_path / "oracle")
    oracle_repository = FileOrchestrationJournalRepository(tmp_path / "oracle")
    assert (
        oracle_repository.bind_forcing_submit_attempt(
            str(held["idempotency_key"]),
            expected_submission_attempt=int(held["submission_attempt"]),
            expected_submission_attempt_started_at=anchor,
            slurm_job_id=master_id,
            expected_slurm_comment=str(held["slurm_comment"]),
        )
        is not None
    )
    submissions_before = len(client.submissions)

    receipt = _cli_bind(repository, client, submit, capsys)

    assert (receipt["status"], receipt["lane"], receipt["matched_slurm_job_id"]) == ("bound", "forcing", master_id)
    bound = _master(repository, "forcing")
    oracle = _master(oracle_repository, "forcing")
    assert {field: bound.get(field) for field in _TUPLE} == {field: oracle.get(field) for field in _TUPLE}
    assert (bound["status"], bound["slurm_binding_source"], bound["slurm_accounting_submitted_at"]) == (
        "submitted",
        None,
        None,
    )

    inflight = reconcile_module.reconcile_inflight_jobs(
        repository,
        sacct_query=lambda job_id: (
            _completed_forcing_accounting(master_id, len(candidates)) if str(job_id) == master_id else None
        ),
    )

    assert [(item.job_id, item.status) for item in inflight] == [(bound["job_id"], "succeeded")]
    assert _master(repository, "forcing")["status"] == "succeeded"
    for candidate in candidates:
        assert not [row for row in _state(repository, candidate)["pipeline_jobs"] if row.get("status") == "reserved"]
        decision = _decision(repository, candidate)
        assert (decision.action, decision.reason) != _HELD
        assert "held_reservations" not in decision.evidence
        assert (decision.action, decision.reason, decision.evidence.get("restart_stage")) == (
            "retry",
            "resume_after_completed_stage",
            "forecast",
        )
    selected, blocked, skipped = _plan(_scheduler(tmp_path, repository, raw_models), raw_models)
    assert (blocked, skipped) == ([], [])
    assert sorted(candidate.candidate_id for candidate in selected) == sorted(c.candidate_id for c in candidates)
    # Nothing in bind, reconcile or the decision reached the gateway.
    assert len(client.submissions) == submissions_before
