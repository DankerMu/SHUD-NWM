"""Forcing submit ambiguity: persisted identity, commentless controller resume and reclaim.

#3177 moved the member-reservation and retry cases to
``tests/test_forcing_submit_ambiguity_reservation.py``; the cycle constants and
forcing fixtures both modules use are in ``tests/forcing_submit_ambiguity_helpers.py``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.orchestrator import reconcile as reconcile_module
from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
from services.orchestrator.chain import M3_STAGES, HttpSlurmGatewayClient, OrchestratorError
from services.orchestrator.file_orchestration_journal import (
    FileOrchestrationJournalRepository,
)
from services.orchestrator.reconcile import CommentAccountingResult, reconcile_reserved_unbound_jobs
from services.orchestrator.scheduler_candidate_execution_evidence import (
    _pipeline_result_slurm_submit_called,
)
from services.orchestrator.scheduler_evidence import UNKNOWN_AFTER_ATTEMPT
from tests.forcing_submit_ambiguity_helpers import (
    _CYCLE,
    _CYCLE_ID,
    _CYCLE_TIME,
    _expected_forcing_attempt_comment,
    _forcing_basins,
    _forcing_context,
    _reserve_forcing,
)
from tests.test_orchestration_chain import FakeCycleSlurmClient, _orchestrator


def _controller_forcing_rows(
    *,
    master_id: str,
    task_count: int,
    comment: str,
    submitted_at: Any,
    user: str = "scheduler",
    account: str = "account",
    array_task_id: str | None = None,
) -> str:
    submit_time = submitted_at.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    task_ids = (array_task_id,) if array_task_id is not None else tuple(
        str(task_id) for task_id in range(task_count)
    )
    return "\n".join(
        (
            f"JobId={int(master_id) + row_index + 1} ArrayJobId={master_id} "
            f"ArrayTaskId={task_id} JobName=nhms_forcing UserId={user}(1000) "
            f"Account={account} JobState=RUNNING ExitCode=0:0 "
            f"SubmitTime={submit_time} Comment={comment}"
        )
        for row_index, task_id in enumerate(task_ids)
    ) + "\n"


def _commentless_forcing_query(
    monkeypatch: pytest.MonkeyPatch,
    *,
    controller_stdout: str,
    query_end: Any,
) -> tuple[Any, list[list[str]]]:
    commands: list[list[str]] = []

    def controller(command: list[str]) -> str:
        commands.append(list(command))
        if command == ["/opt/slurm/bin/scontrol", "show", "config"]:
            return "AccountingStoreFlags = (null)\n"
        return controller_stdout

    monkeypatch.setattr("services.orchestrator.reconcile_sacct_probe._bounded_visibility_stdout", controller)
    monkeypatch.setattr(
        "services.orchestrator.reconcile_comment_sacct._bounded_sacct_stdout",
        lambda _command: pytest.fail("comment-less forcing controller proof must not query sacct"),
    )
    return (
        reconcile_module.default_comment_sacct_querier(
            "/opt/slurm/bin",
            now=lambda: query_end,
        ),
        commands,
    )


@pytest.mark.parametrize(
    ("failure", "expected_error_code"),
    [
        pytest.param("http_502", "SLURM_PARSE_ERROR", id="http-502"),
        pytest.param("transport", "SLURM_GATEWAY_UNAVAILABLE", id="transport"),
        pytest.param("invalid_success", "SLURM_GATEWAY_INVALID_RESPONSE", id="invalid-success"),
    ],
)
def test_real_http_forcing_ambiguity_persists_identity_and_blocks_renamed_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_error_code: str,
) -> None:
    requests: list[tuple[str, str, dict[str, Any]]] = []

    class _HttpClient:
        def __enter__(self) -> _HttpClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def request(self, method: str, path: str, *, json: Any = None) -> httpx.Response:
            requests.append((method, path, dict(json or {})))
            if failure == "transport":
                raise httpx.ConnectError("gateway connection reset")
            if failure == "http_502":
                return httpx.Response(502, json={"detail": {"error": {"code": "SLURM_PARSE_ERROR"}}})
            return httpx.Response(200, json={"status": "submitted"})

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: _HttpClient())
    monkeypatch.setattr(
        "services.orchestrator.reconcile_sacct_probe._bounded_visibility_stdout",
        lambda command: (
            "AccountingStoreFlags = (null)\n"
            if list(command)[-2:] == ["show", "config"]
            else ""
        ),
    )
    root = tmp_path / "journal"
    initial_run_id = f"cycle_gfs_{_CYCLE}_convert_cohort_a"
    repository = FileOrchestrationJournalRepository(root)
    initial = _orchestrator(
        tmp_path / "initial",
        repository,
        HttpSlurmGatewayClient("http://gateway.test"),
        terminal_stage="forecast",
    )
    context = _forcing_context(
        initial,
        _forcing_basins(2, orchestration_run_id=initial_run_id),
        run_id=initial_run_id,
    )

    result, aggregation = initial._submit_and_wait_cycle_stage(M3_STAGES[1], context)

    assert aggregation is None
    assert result.status == "submit_result_ambiguous"
    assert result.error_code == expected_error_code
    durable = repository.get_pipeline_job(result.pipeline_job_id)
    assert durable is not None
    assert durable["status"] == "reserved"
    assert durable["submit_outcome"] == "submit_result_ambiguous"
    assert durable["slurm_job_id"] is None
    assert durable["error_code"] == expected_error_code
    assert durable["error_message"]
    assert durable.get("accepted_submit_contract_version") is None
    assert durable.get("cohort_digest") is None
    assert durable["restart_stage"] == "forcing"
    assert [member["array_task_id"] for member in durable["cohort_members"]] == [0, 1]
    assert [member["model_id"] for member in durable["cohort_members"]] == ["model_0", "model_1"]
    assert all(member["restart_stage"] == "forcing" for member in durable["cohort_members"])
    assert durable["slurm_comment"] == _expected_forcing_attempt_comment(
        durable["idempotency_key"],
        durable["submission_attempt"],
    )
    assert repository.reclaim_pipeline_job_reservation(dict(durable)) is None

    renamed_run_id = f"cycle_gfs_{_CYCLE}_forcing_cohort_b"
    restarted = _orchestrator(
        tmp_path / "restarted",
        FileOrchestrationJournalRepository(root),
        HttpSlurmGatewayClient("http://gateway.test"),
        terminal_stage="forecast",
    )
    restarted_result = restarted.orchestrate_cycle(
        "gfs",
        _CYCLE,
        list(reversed(_forcing_basins(2, orchestration_run_id=renamed_run_id))),
    )

    assert restarted_result.status == "reconciling"
    assert [stage.stage for stage in restarted_result.stages] == ["forcing"]
    assert restarted_result.stages[0].pipeline_job_id == durable["job_id"]
    assert restarted_result.stages[0].slurm_job_id == ""
    assert _pipeline_result_slurm_submit_called(restarted_result) == UNKNOWN_AFTER_ATTEMPT
    assert [(method, path) for method, path, _payload in requests] == [
        ("POST", "/api/v1/slurm/job-arrays")
    ]
    assert requests[0][2]["manifest"]["comment"] == durable["slurm_comment"]


def test_commentless_controller_identity_resumes_original_forcing_through_normal_aggregation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _AcceptedThenLostForcingClient(FakeCycleSlurmClient):
        def __init__(self) -> None:
            super().__init__(array_results_by_stage={"forcing": ["succeeded", "succeeded"]})
            self.array_task_queries: list[str] = []
            self.forcing_submission_comments: list[str] = []

        def submit_job_array(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            accepted = super().submit_job_array(*args, **kwargs)
            if kwargs["stage_name"] == "forcing":
                self.forcing_submission_comments.append(str(kwargs["manifest"]["comment"]))
                raise OrchestratorError(
                    "SLURM_GATEWAY_UNAVAILABLE",
                    "forcing response was lost after Gateway acceptance",
                )
            return accepted

        def get_array_task_results(self, job_id: str) -> list[dict[str, Any]]:
            self.array_task_queries.append(job_id)
            return super().get_array_task_results(job_id)

    root = tmp_path / "journal"
    initial_run_id = f"cycle_gfs_{_CYCLE}_convert_cohort_a"
    repository = FileOrchestrationJournalRepository(root)
    client = _AcceptedThenLostForcingClient()
    initial = _orchestrator(tmp_path / "initial", repository, client, terminal_stage="forecast")
    initial.config = replace(
        initial.config,
        reconcile_slurm_user="scheduler",
        reconcile_slurm_account="account",
    )
    monkeypatch.setattr(
        "services.orchestrator.reconcile_sacct_probe._bounded_visibility_stdout",
        lambda command: (
            "AccountingStoreFlags = (null)\n"
            if list(command)[-2:] == ["show", "config"]
            else ""
        ),
    )

    initial_result = initial.orchestrate_cycle(
        "gfs",
        _CYCLE,
        _forcing_basins(2, orchestration_run_id=initial_run_id),
    )

    assert initial_result.status == "reconciling"
    forcing = next(
        job for job in repository.query_pipeline_jobs_by_cycle(_CYCLE_ID) if job["stage"] == "forcing"
    )
    assert forcing["submit_outcome"] == "submit_result_ambiguous"
    assert forcing["slurm_job_id"] is None
    assert client.forcing_submission_comments == [forcing["slurm_comment"]]
    held = repository.query_reserved_unbound_jobs()[0]
    anchor = held.submission_attempt_started_at
    original_job_id = next(
        job_id for job_id, job in client.jobs.items() if job["stage"] == "forcing"
    )
    query, commands = _commentless_forcing_query(
        monkeypatch,
        controller_stdout=_controller_forcing_rows(
            master_id=original_job_id,
            task_count=2,
            comment=client.forcing_submission_comments[0],
            submitted_at=anchor.replace(microsecond=0),
        ),
        query_end=anchor + timedelta(minutes=1),
    )

    resolution = reconcile_reserved_unbound_jobs(repository, comment_query=query)[0]

    assert resolution.action == "bound"
    assert resolution.reconciliation_source == "slurm_controller_exact_comment"
    assert commands == [
        ["/opt/slurm/bin/scontrol", "show", "config"],
        ["/opt/slurm/bin/scontrol", "show", "job", "-o"],
    ]
    bound = repository.get_pipeline_job(forcing["job_id"])
    assert bound is not None
    assert bound["status"] == "submitted"
    assert bound["slurm_job_id"] == original_job_id
    assert bound["reconciliation_source"] == "slurm_controller_exact_comment"
    assert bound["reconciliation_decision"] == "matched_bound"
    inflight_commands: list[list[str]] = []
    monkeypatch.setattr(
        "services.orchestrator.reconcile_comment_sacct._bounded_sacct_stdout",
        lambda command: inflight_commands.append(list(command))
        or f"{original_job_id}|nhms_forcing|RUNNING|0:0||scheduler|account\n",
    )
    inflight = reconcile_module.reconcile_inflight_jobs(
        repository,
        sacct_query=reconcile_module.default_sacct_querier("/opt/slurm/bin"),
    )[0]
    assert inflight.action == "still_running"
    assert inflight_commands == [
        [
            "/opt/slurm/bin/sacct",
            "--parsable2",
            "--noheader",
            "--format=JobID,JobName,State,ExitCode,Comment,User,Account",
            f"--jobs={original_job_id}",
        ]
    ]
    refreshed = repository.get_pipeline_job(forcing["job_id"])
    assert refreshed is not None
    assert refreshed["status"] == "running"
    assert refreshed["reconciliation_source"] == "slurm_controller_exact_comment"

    resumed = _orchestrator(
        tmp_path / "resumed",
        FileOrchestrationJournalRepository(root),
        client,
        terminal_stage="forecast",
    )
    resumed.config = replace(
        resumed.config,
        reconcile_slurm_user="scheduler",
        reconcile_slurm_account="account",
    )
    renamed_run_id = f"cycle_gfs_{_CYCLE}_forcing_cohort_b"
    resumed_result = resumed.orchestrate_cycle(
        "gfs",
        _CYCLE,
        list(reversed(_forcing_basins(2, orchestration_run_id=renamed_run_id))),
    )

    assert resumed_result.status == "succeeded"
    assert [stage.stage for stage in resumed_result.stages] == ["forcing", "forecast"]
    assert resumed_result.stages[0].pipeline_job_id == forcing["job_id"]
    assert resumed_result.stages[0].slurm_job_id == original_job_id
    assert [
        (task["array_task_id"], task["model_id"], task["status"])
        for task in resumed_result.stages[0].task_results
    ] == [(0, "model_0", "succeeded"), (1, "model_1", "succeeded")]
    assert original_job_id in client.array_task_queries
    assert [submission["stage"] for submission in client.submissions] == ["forcing", "forecast"]

def test_boundary_controller_identity_continues_before_next_global_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BoundaryRepository(FileOrchestrationJournalRepository):
        def query_reserved_unbound_jobs(self) -> list[Any]:
            raise AssertionError("failure-boundary reconcile must select only its current row")

    class _HttpGatewayWithFakeRuntime(HttpSlurmGatewayClient):
        def __init__(self, runtime: FakeCycleSlurmClient) -> None:
            super().__init__("http://gateway.test")
            self.runtime = runtime
            self.array_task_queries: list[str] = []

        def get_job_status(self, job_id: str) -> dict[str, Any]:
            return self.runtime.get_job_status(job_id)

        def get_array_task_results(self, job_id: str) -> list[dict[str, Any]]:
            self.array_task_queries.append(job_id)
            return self.runtime.get_array_task_results(job_id)

        def fetch_logs(self, job_id: str) -> dict[str, Any]:
            return self.runtime.fetch_logs(job_id)

    runtime = FakeCycleSlurmClient(array_results_by_stage={"forcing": ["succeeded", "succeeded"]})
    client = _HttpGatewayWithFakeRuntime(runtime)
    requests: list[tuple[str, str, dict[str, Any]]] = []
    controller_job_queries: list[list[str]] = []
    durable_at_discovery: list[dict[str, Any]] = []

    class _HttpClient:
        def __enter__(self) -> _HttpClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def request(
            self,
            method: str,
            path: str,
            *,
            json: Any = None,
            headers: Any = None,
        ) -> httpx.Response:
            del headers
            payload = dict(json or {})
            requests.append((method, path, payload))
            assert (method, path) == ("POST", "/api/v1/slurm/job-arrays")
            accepted = runtime.submit_job_array(
                str(payload["job_type"]),
                cycle_id=str(payload["cycle_id"]),
                stage_name=str(payload["stage_name"]),
                tasks=[dict(task) for task in payload["tasks"]],
                manifest=dict(payload["manifest"]),
            )
            if payload["stage_name"] == "forcing":
                return httpx.Response(
                    502,
                    json={"detail": {"error": {"code": "SLURM_PARSE_ERROR"}}},
                )
            return httpx.Response(200, json=accepted)

    repository = _BoundaryRepository(tmp_path / "journal")

    def controller(command: list[str]) -> str:
        if command[-2:] == ["show", "config"]:
            return "AccountingStoreFlags = (null)\n"
        if command[-3:] == ["show", "job", "-o"]:
            controller_job_queries.append(list(command))
            if len(controller_job_queries) == 1:
                forcing = next(
                    job
                    for job in repository.query_pipeline_jobs_by_cycle(_CYCLE_ID)
                    if job["stage"] == "forcing"
                )
                durable_at_discovery.append(
                    {
                        "status": forcing["status"],
                        "slurm_job_id": forcing["slurm_job_id"],
                        "submit_outcome": forcing["submit_outcome"],
                        "error_code": forcing["error_code"],
                        "slurm_comment": forcing["slurm_comment"],
                    }
                )
                forcing_job_id = next(
                    job_id for job_id, job in runtime.jobs.items() if job["stage"] == "forcing"
                )
                return _controller_forcing_rows(
                    master_id=forcing_job_id,
                    task_count=2,
                    comment=forcing["slurm_comment"],
                    submitted_at=_CYCLE_TIME,
                ).replace("JobState=RUNNING", "JobState=COMPLETED", 1)
            return ""
        return ""

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: _HttpClient())
    monkeypatch.setenv("SLURM_GATEWAY_SLURM_BIN_PATH", "/opt/slurm/bin")
    monkeypatch.setattr("services.orchestrator.reconcile_sacct_probe._bounded_visibility_stdout", controller)
    monkeypatch.setattr(
        "services.orchestrator.reconcile_comment_sacct._bounded_sacct_stdout",
        lambda _command: pytest.fail("comment-less boundary discovery must not query sacct"),
    )
    orchestrator = _orchestrator(tmp_path / "orchestrator", repository, client, terminal_stage="forecast")
    orchestrator.config = replace(
        orchestrator.config,
        reconcile_slurm_user="scheduler",
        reconcile_slurm_account="account",
    )
    run_id = f"cycle_gfs_{_CYCLE}_boundary_reconcile"

    result = orchestrator.orchestrate_cycle(
        "gfs",
        _CYCLE,
        _forcing_basins(2, orchestration_run_id=run_id),
    )

    forcing = next(
        job for job in repository.query_pipeline_jobs_by_cycle(_CYCLE_ID) if job["stage"] == "forcing"
    )
    original_forcing_job_id = next(
        job_id for job_id, job in runtime.jobs.items() if job["stage"] == "forcing"
    )
    forcing_posts = [
        payload
        for method, path, payload in requests
        if (method, path) == ("POST", "/api/v1/slurm/job-arrays")
        and payload["stage_name"] == "forcing"
    ]
    assert result.status == "succeeded"
    assert [stage.stage for stage in result.stages] == ["forcing", "forecast"]
    assert len(forcing_posts) == 1
    assert forcing_posts[0]["manifest"]["comment"] == forcing["slurm_comment"]
    assert durable_at_discovery == [
        {
            "status": "reserved",
            "slurm_job_id": None,
            "submit_outcome": "submit_result_ambiguous",
            "error_code": "SLURM_PARSE_ERROR",
            "slurm_comment": forcing["slurm_comment"],
        }
    ]
    assert controller_job_queries == [["/opt/slurm/bin/scontrol", "show", "job", "-o"]]
    assert result.stages[0].slurm_job_id == original_forcing_job_id
    assert result.stages[0].slurm_job_id == forcing["slurm_job_id"]
    assert [
        (task["array_task_id"], task["model_id"], task["status"])
        for task in result.stages[0].task_results
    ] == [(0, "model_0", "succeeded"), (1, "model_1", "succeeded")]
    assert forcing["slurm_job_id"] in client.array_task_queries
    assert [payload["stage_name"] for _method, _path, payload in requests] == ["forcing", "forecast"]
    assert forcing["status"] == "succeeded"
    assert forcing["reconciliation_source"] == "slurm_controller_exact_comment"


def test_commentless_controller_binds_a_pending_array_range_for_the_original_master(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    reservation = _reserve_forcing(
        repository,
        suffix="pending_range",
        model_ids=("model_0", "model_1"),
        expected_user="scheduler",
        expected_account="account",
    )
    transition = repository.transition_pipeline_job_submit_evidence(
        reservation.job_id,
        AcceptedSubmitTransition.timeout(),
        expected_submission_attempt=reservation.submission_attempt,
        expected_statuses=("reserved",),
        require_unbound=True,
        error_code="SLURM_PARSE_ERROR",
        error_message="Gateway response was empty after submission.",
    )
    assert transition.committed
    held = repository.query_reserved_unbound_jobs()[0]
    anchor = held.submission_attempt_started_at
    submit_time = (anchor + timedelta(seconds=24)).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    query, commands = _commentless_forcing_query(
        monkeypatch,
        controller_stdout=(
            "JobId=2001_[0-1] ArrayJobId=2001 ArrayTaskId=0-1%16 "
            "JobName=nhms_forcing UserId=scheduler(1000) Account=account "
            f"JobState=PENDING ExitCode=0:0 SubmitTime={submit_time} "
            f"Comment={held.slurm_comment}\n"
        ),
        query_end=anchor + timedelta(minutes=1),
    )

    outcome = reconcile_reserved_unbound_jobs(repository, comment_query=query)[0]

    assert outcome.action == "bound"
    assert outcome.slurm_job_id == "2001"
    assert outcome.reconciliation_source == "slurm_controller_exact_comment"
    assert commands == [
        ["/opt/slurm/bin/scontrol", "show", "config"],
        ["/opt/slurm/bin/scontrol", "show", "job", "-o"],
    ]

def test_comment_storing_accounting_binds_current_forcing_attempt_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    reservation = _reserve_forcing(
        repository,
        suffix="accounting_token",
        model_ids=("model_0",),
        expected_user="scheduler",
        expected_account="account",
    )
    transition = repository.transition_pipeline_job_submit_evidence(
        reservation.job_id,
        AcceptedSubmitTransition.timeout(),
        expected_submission_attempt=reservation.submission_attempt,
        expected_statuses=("reserved",),
        require_unbound=True,
        error_code="SLURM_PARSE_ERROR",
        error_message="Gateway response was empty after submission.",
    )
    assert transition.committed
    held = repository.query_reserved_unbound_jobs()[0]
    assert held.slurm_comment == _expected_forcing_attempt_comment(
        reservation.idempotency_key,
        reservation.submission_attempt,
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "services.orchestrator.reconcile_comment_sacct._bounded_sacct_stdout",
        lambda command: commands.append(list(command))
        or f"2001_0|nhms_forcing|RUNNING|0:0|{held.slurm_comment}|scheduler|account\n",
    )
    query = reconcile_module.default_comment_sacct_querier(
        "/opt/slurm/bin",
        comment_storage_probe=lambda: True,
        global_visibility_probe=lambda: True,
        now=lambda: held.submission_attempt_started_at + timedelta(minutes=1),
    )

    outcome = reconcile_reserved_unbound_jobs(repository, comment_query=query)[0]

    assert outcome.action == "bound"
    assert outcome.slurm_job_id == "2001"
    assert outcome.reconciliation_source == "slurm_exact_comment"
    assert commands
    bound = repository.get_reconcile_pipeline_job(reservation.job_id)
    assert bound is not None
    assert bound.slurm_comment == held.slurm_comment


@pytest.mark.parametrize(
    ("identity_case", "expected_action"),
    [
        pytest.param("missing", "query_unavailable", id="missing-controller-identity"),
        pytest.param("wrong_owner", "query_unavailable", id="wrong-owner"),
        pytest.param("wrong_comment", "query_unavailable", id="wrong-comment"),
        pytest.param("multiple_masters", "multiple_matches_blocked", id="multiple-masters"),
        pytest.param("duplicate_comment", "query_unavailable", id="duplicate-identity-field"),
    ],
)
def test_commentless_controller_identity_fails_closed_without_one_current_owned_master(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_case: str,
    expected_action: str,
) -> None:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    attempt_anchor = _CYCLE_TIME
    reservation = _reserve_forcing(
        repository,
        suffix=identity_case,
        model_ids=("model_0",),
        expected_user="scheduler",
        expected_account="account",
        submission_attempt_started_at=attempt_anchor,
    )
    transition = repository.transition_pipeline_job_submit_evidence(
        reservation.job_id,
        AcceptedSubmitTransition.timeout(),
        expected_submission_attempt=reservation.submission_attempt,
        expected_statuses=("reserved",),
        require_unbound=True,
        error_code="SLURM_PARSE_ERROR",
        error_message="Gateway response was empty after submission.",
    )
    assert transition.committed
    held = repository.query_reserved_unbound_jobs()[0]
    anchor = held.submission_attempt_started_at
    expected_comment = held.slurm_comment
    if identity_case == "missing":
        controller_stdout = ""
    elif identity_case == "wrong_owner":
        controller_stdout = _controller_forcing_rows(
            master_id="2001",
            task_count=1,
            comment=expected_comment,
            submitted_at=anchor + timedelta(seconds=1),
            user="other",
        )
    elif identity_case == "wrong_comment":
        controller_stdout = _controller_forcing_rows(
            master_id="2001",
            task_count=1,
            comment=_expected_forcing_attempt_comment("unrelated:forcing", 1),
            submitted_at=anchor + timedelta(seconds=1),
        )
    elif identity_case == "duplicate_comment":
        controller_stdout = _controller_forcing_rows(
            master_id="2001",
            task_count=1,
            comment=expected_comment,
            submitted_at=anchor + timedelta(seconds=1),
        ).replace(
            f"Comment={expected_comment}",
            f"Comment={expected_comment} Comment={expected_comment}",
        )
    else:
        controller_stdout = (
            _controller_forcing_rows(
                master_id="2001",
                task_count=1,
                comment=expected_comment,
                submitted_at=anchor + timedelta(seconds=1),
            )
            + _controller_forcing_rows(
                master_id="2002",
                task_count=1,
                comment=expected_comment,
                submitted_at=anchor + timedelta(seconds=1),
            )
        )
    query, commands = _commentless_forcing_query(
        monkeypatch,
        controller_stdout=controller_stdout,
        query_end=anchor + timedelta(minutes=1),
    )

    outcome = reconcile_reserved_unbound_jobs(repository, comment_query=query)[0]

    assert outcome.action == expected_action
    assert commands == [
        ["/opt/slurm/bin/scontrol", "show", "config"],
        ["/opt/slurm/bin/scontrol", "show", "job", "-o"],
    ]
    persisted = repository.get_pipeline_job(reservation.job_id)
    assert persisted is not None
    assert persisted["status"] == "reserved"
    assert persisted["slurm_job_id"] is None
    assert repository.reclaim_pipeline_job_reservation(dict(persisted)) is None

def test_controller_rejects_a_stale_forcing_attempt_comment_after_reclaim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    first = _reserve_forcing(
        repository,
        suffix="stale_token",
        model_ids=("model_0",),
        expected_user="scheduler",
        expected_account="account",
    )
    first_comment = _expected_forcing_attempt_comment(
        first.idempotency_key,
        first.submission_attempt,
    )
    first_transition = repository.transition_pipeline_job_submit_evidence(
        first.job_id,
        AcceptedSubmitTransition.timeout(),
        expected_submission_attempt=first.submission_attempt,
        expected_statuses=("reserved",),
        require_unbound=True,
        error_code="SLURM_PARSE_ERROR",
        error_message="Gateway response was empty after submission.",
    )
    assert first_transition.committed
    first_held = repository.query_reserved_unbound_jobs()[0]
    assert repository.permit_forcing_submit_retry(
        first.job_id,
        expected_submission_attempt=first.submission_attempt,
        expected_submission_attempt_started_at=first_held.submission_attempt_started_at,
    ) is not None

    current = _reserve_forcing(
        repository,
        suffix="stale_token",
        model_ids=("model_0",),
        expected_user="scheduler",
        expected_account="account",
    )
    assert current.created
    assert current.submission_attempt == 2
    current_comment = _expected_forcing_attempt_comment(
        current.idempotency_key,
        current.submission_attempt,
    )
    reopened = repository.get_reconcile_pipeline_job(current.job_id)
    assert reopened is not None
    assert reopened.slurm_comment == current_comment
    current_transition = repository.transition_pipeline_job_submit_evidence(
        current.job_id,
        AcceptedSubmitTransition.timeout(),
        expected_submission_attempt=current.submission_attempt,
        expected_statuses=("reserved",),
        require_unbound=True,
        error_code="SLURM_PARSE_ERROR",
        error_message="Gateway response was empty after submission.",
    )
    assert current_transition.committed
    held = repository.query_reserved_unbound_jobs()[0]
    query, _commands = _commentless_forcing_query(
        monkeypatch,
        controller_stdout=_controller_forcing_rows(
            master_id="2001",
            task_count=1,
            comment=first_comment,
            submitted_at=held.submission_attempt_started_at.replace(microsecond=0),
        ),
        query_end=held.submission_attempt_started_at + timedelta(minutes=1),
    )

    stale_outcome = reconcile_reserved_unbound_jobs(repository, comment_query=query)[0]

    assert stale_outcome.action == "query_unavailable"
    persisted = repository.get_pipeline_job(current.job_id)
    assert persisted is not None
    assert persisted["status"] == "reserved"

    current_query, _commands = _commentless_forcing_query(
        monkeypatch,
        controller_stdout=_controller_forcing_rows(
            master_id="2002",
            task_count=1,
            comment=current_comment,
            submitted_at=held.submission_attempt_started_at.replace(microsecond=0),
        ),
        query_end=held.submission_attempt_started_at + timedelta(minutes=1),
    )
    current_outcome = reconcile_reserved_unbound_jobs(repository, comment_query=current_query)[0]

    assert current_outcome.action == "bound"
    assert current_outcome.slurm_job_id == "2002"
    assert current_outcome.reconciliation_source == "slurm_controller_exact_comment"
    bound = repository.get_pipeline_job(current.job_id)
    assert bound is not None
    assert bound["status"] == "submitted"


def test_permitted_reclaim_persists_current_attempt_task_mapping_before_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    class _HttpClient:
        def __enter__(self) -> _HttpClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def request(self, method: str, path: str, *, json: Any = None) -> httpx.Response:
            del method, path
            requests.append(dict(json or {}))
            return httpx.Response(
                502, json={"detail": {"error": {"code": "SLURM_PARSE_ERROR"}}}
            )

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: _HttpClient())
    monkeypatch.setattr(
        "services.orchestrator.reconcile_sacct_probe._bounded_visibility_stdout",
        lambda command: (
            "AccountingStoreFlags = (null)\n"
            if list(command)[-2:] == ["show", "config"]
            else ""
        ),
    )
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    run_id = f"cycle_gfs_{_CYCLE}_forcing_cohort_order"
    orchestrator = _orchestrator(
        tmp_path / "orch",
        repository,
        HttpSlurmGatewayClient("http://gateway.test"),
        terminal_stage="forecast",
    )
    basins = _forcing_basins(2, orchestration_run_id=run_id)
    first, _aggregation = orchestrator._submit_and_wait_cycle_stage(
        M3_STAGES[1],
        _forcing_context(orchestrator, basins, run_id=run_id),
    )
    held = repository.query_reserved_unbound_jobs()[0]
    anchor = held.submission_attempt_started_at
    outcome = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=lambda _key, **_kwargs: CommentAccountingResult(
            (),
            scope="global",
            coverage_start=anchor - timedelta(seconds=1),
            coverage_end=anchor + timedelta(minutes=3),
            coverage_complete=True,
        ),
        now=lambda: anchor + timedelta(minutes=3),
    )[0]
    assert outcome.action == "absence_retry_permitted"

    second, _aggregation = orchestrator._submit_and_wait_cycle_stage(
        M3_STAGES[1],
        _forcing_context(orchestrator, list(reversed(basins)), run_id=run_id),
    )
    current = repository.get_reconcile_pipeline_job(second.pipeline_job_id)
    assert current is not None
    sent_task_models = [entry["model_id"] for entry in requests[-1]["tasks"]]
    durable_task_models = [entry["model_id"] for entry in current.cohort_members]
    assert first.pipeline_job_id == second.pipeline_job_id
    assert current.submission_attempt == 2
    assert len(requests) == 2
    assert sent_task_models == ["model_1", "model_0"]
    assert durable_task_models == sent_task_models
