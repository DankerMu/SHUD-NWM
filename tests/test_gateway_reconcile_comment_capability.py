"""Comment-sacct capability gates: visibility probes, comment-storage
probes, scope refusal, and production cadence paging.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from typing import Any

import pytest

from tests.gateway_reconcile_helpers import (
    _file_cohort_repository,
    _versioned_master_reservation_record,
)
from tests.test_real_slurm_gateway import _pinned_local_timezone


def test_comment_sacct_global_zero_is_unavailable_without_visibility_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    calls: list[list[str]] = []
    monkeypatch.setattr(
        reconcile_module,
        "_bounded_sacct_stdout",
        lambda command: calls.append(list(command)) or "",
    )
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: False,
        comment_storage_probe=lambda: True,
    )

    with pytest.raises(reconcile_module.ReconcileQueryUnavailable, match="visibility is unproven"):
        query("key", accepted_submit_contract_version="nhms.accepted_submit.v1")
    assert calls == []


def test_comment_sacct_legacy_global_query_does_not_require_visibility_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    calls: list[list[str]] = []
    monkeypatch.setattr(
        reconcile_module,
        "_bounded_sacct_stdout",
        lambda command: calls.append(list(command)) or "",
    )
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: False,
        comment_storage_probe=lambda: True,
    )

    assert tuple(query("legacy-key")) == ()
    assert calls
    assert all("--allusers" in command for command in calls)


@pytest.mark.parametrize("member_count", [1, 18, 256])
def test_rejected_submit_batch_write_failure_reopens_as_unbound_recoverable_reservation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    member_count: int,
) -> None:
    from services.orchestrator.chain_types import OrchestratorError
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = _file_cohort_repository(
        tmp_path / str(member_count),
        member_count=member_count,
        submit_outcome=None,
    )
    for index in range(member_count):
        repository.update_hydro_run_status(
            f"fcst_gfs_2026071200_model_{index}",
            "created",
            error_code=None,
            error_message=None,
        )
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    def fail_batch(**_kwargs: Any) -> None:
        raise OrchestratorError("FILE_JOURNAL_WRITE_FAILED", "injected batch failure")

    monkeypatch.setattr(repository, "_append_journal_records_unlocked", fail_batch)
    with pytest.raises(OrchestratorError, match="injected batch failure"):
        repository.reject_pipeline_job_submit_attempt(
            "cycle_gfs_2026071200_forecast_fixture:forecast",
            expected_submission_attempt=1,
            finished_at=datetime(2026, 7, 12, 0, 1, tzinfo=UTC),
            error_code="VALIDATION_ERROR",
            error_message="pre-submit rejected",
            stage="forecast",
            job_type="run_shud_forecast_array",
        )

    reopened = FileOrchestrationJournalRepository(repository.root)
    master = reopened.get_pipeline_job(job_id)
    assert master["status"] == "reserved"
    assert master["submit_outcome"] is None
    assert master["slurm_job_id"] is None
    assert len(reopened.query_reserved_unbound_jobs()) == 1
    assert all(
        (reopened._hydro_run_for(f"fcst_gfs_2026071200_model_{index}") or {})["status"] == "created"
        for index in range(member_count)
    )


def test_marker_free_historical_candidate_remains_readable_but_unversioned(tmp_path: Any) -> None:
    from services.orchestrator.accepted_submit_identity import accepted_submit_contract_is_current
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    legacy = {
        "job_id": "job_fcst_gfs_2026071200_model_legacy_forecast_candidate_0",
        "run_id": "fcst_gfs_2026071200_model_legacy",
        "cycle_id": "gfs_2026071200",
        "job_type": "run_shud_forecast_array",
        "slurm_job_id": "17667_0",
        "array_task_id": 0,
        "model_id": "model_legacy",
        "status": "succeeded",
        "stage": "forecast",
        "candidate_id": "gfs:2026-07-12T00:00:00Z:model_legacy:forecast_gfs_deterministic",
        "submit_outcome": "historical_pre_1112_value",
        "restart_stage": "forecast",
    }
    repository.append_historical_pipeline_job(legacy)

    reopened = FileOrchestrationJournalRepository(repository.root)
    direct = reopened.get_pipeline_job(legacy["job_id"])
    queried = next(
        job for job in reopened.query_pipeline_jobs_by_cycle("gfs_2026071200")
        if job["job_id"] == legacy["job_id"]
    )
    latest = json.loads(
        (repository.root / "latest" / "gfs" / "2026071200" / "model_legacy.json").read_text()
    )
    latest_row = next(job for job in latest["pipeline_jobs"] if job["job_id"] == legacy["job_id"])
    assert direct == queried == latest_row
    assert accepted_submit_contract_is_current(direct) is False


def test_marker_free_historical_master_is_read_only_to_accepted_submit_reconcile(tmp_path: Any) -> None:
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    repository = _file_cohort_repository(
        tmp_path,
        member_count=1,
        with_runtime_rows=False,
        submit_outcome=None,
        versioned=False,
    )
    before = repository.get_pipeline_job("job_cycle_gfs_2026071200_forecast_fixture_forecast")

    outcomes = reconcile_reserved_unbound_jobs(repository, comment_query=lambda _key: None)

    assert outcomes[0].action == "legacy_unversioned_read_only"
    assert repository.get_pipeline_job(before["job_id"]) == before


@pytest.mark.parametrize("version", ["nhms.accepted_submit.v2", None, 1])
def test_explicit_unknown_or_malformed_accepted_submit_version_fails_closed(
    tmp_path: Any,
    version: Any,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    row = {
        "job_id": "job_fcst_gfs_2026071200_model_legacy_forecast_candidate_0",
        "run_id": "fcst_gfs_2026071200_model_legacy",
        "cycle_id": "gfs_2026071200",
        "job_type": "run_shud_forecast_array",
        "slurm_job_id": "17667_0",
        "array_task_id": 0,
        "model_id": "model_legacy",
        "status": "succeeded",
        "stage": "forecast",
        "candidate_id": "gfs:2026-07-12T00:00:00Z:model_legacy:forecast_gfs_deterministic",
        "submit_outcome": "accepted",
        "restart_stage": "forecast",
        "accepted_submit_contract_version": version,
    }

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.upsert_pipeline_job(row)
    assert error.value.field == "accepted_submit_contract_version"


@pytest.mark.parametrize(
    ("controller_private_data", "slurmdbd_private_data", "expected"),
    [
        ("none", "none", True),
        ("accounts,events", "users", True),
        ("jobs", "none", False),
        ("none", "all", False),
        (None, "none", False),
        ("none", None, False),
    ],
)
def test_global_accounting_visibility_probe_requires_controller_and_slurmdbd_private_data(
    monkeypatch: pytest.MonkeyPatch,
    controller_private_data: str | None,
    slurmdbd_private_data: str | None,
    expected: bool,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []

    def run(command: Any) -> str:
        commands.append(list(command))
        value = controller_private_data if str(command[0]).endswith("scontrol") else slurmdbd_private_data
        return f"PrivateData = {value}\n" if value is not None else ""

    monkeypatch.setattr(reconcile_module, "_bounded_visibility_stdout", run)
    assert reconcile_module.default_global_accounting_visibility_probe("/opt/slurm/bin")() is expected
    assert commands == [
        ["/opt/slurm/bin/scontrol", "show", "config"],
        ["/opt/slurm/bin/sacctmgr", "show", "config"],
    ]


def test_global_accounting_visibility_probe_fails_closed_but_checks_both_when_one_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []

    def run(command: Any) -> str:
        commands.append(list(command))
        if str(command[0]).endswith("scontrol"):
            raise reconcile_module.ReconcileQueryUnavailable("controller config unavailable")
        return "PrivateData = none\n"

    monkeypatch.setattr(reconcile_module, "_bounded_visibility_stdout", run)
    assert reconcile_module.default_global_accounting_visibility_probe()() is False
    assert commands == [["scontrol", "show", "config"], ["sacctmgr", "show", "config"]]


_RECONCILE_MODULE_LOGGER = "services.orchestrator.reconcile"


def _reconcile_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _RECONCILE_MODULE_LOGGER and record.levelno == logging.WARNING
    ]


@pytest.mark.parametrize(
    ("flags_line", "expected", "warning_token"),
    [
        # node-22 renders the production value with padded spaces around "=".
        # Tri-state (#1565): a present line lacking job_comment is explicit
        # False; a missing line is unknown None; a probe failure is None.
        ("AccountingStoreFlags    = (null)", False, "does not store job comments"),
        ("AccountingStoreFlags    = job_comment", True, None),
        ("AccountingStoreFlags    = job_comment,job_extra", True, None),
        ("AccountingStoreFlags    = job_extra", False, "does not store job comments"),
        ("AccountingStoreFlags    = ", False, "does not store job comments"),
        (None, None, "capability unknown"),
    ],
)
def test_comment_storage_probe_requires_job_comment_in_accounting_store_flags(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    flags_line: str | None,
    expected: bool | None,
    warning_token: str | None,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []

    def run(command: Any) -> str:
        commands.append(list(command))
        config = "PrivateData = none\nClusterName = qhh\n"
        return config if flags_line is None else f"{config}{flags_line}\n"

    monkeypatch.setattr(reconcile_module, "_bounded_visibility_stdout", run)
    with caplog.at_level(logging.WARNING, logger=_RECONCILE_MODULE_LOGGER):
        assert reconcile_module.default_comment_storage_probe("/opt/slurm/bin")() is expected
    assert commands == [["/opt/slurm/bin/scontrol", "show", "config"]]
    warnings = _reconcile_warnings(caplog)
    if warning_token is None:
        assert warnings == []
    else:
        assert any(warning_token in message for message in warnings)
        assert not any("could not execute" in message for message in warnings)


def test_comment_storage_probe_swallows_an_unrunnable_probe_with_a_distinct_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []

    def run(command: Any) -> str:
        commands.append(list(command))
        raise reconcile_module.ReconcileQueryUnavailable("controller config unavailable")

    monkeypatch.setattr(reconcile_module, "_bounded_visibility_stdout", run)
    with caplog.at_level(logging.WARNING, logger=_RECONCILE_MODULE_LOGGER):
        assert reconcile_module.default_comment_storage_probe()() is None
    assert commands == [["scontrol", "show", "config"]]
    warnings = _reconcile_warnings(caplog)
    assert any("comment storage probe could not execute" in message for message in warnings)
    assert not any("accounting does not store job comments" in message for message in warnings)
    assert not any("capability unknown" in message for message in warnings)


@pytest.mark.parametrize(
    "query_kwargs",
    [
        {},
        {"accepted_submit_contract_version": "nhms.accepted_submit.v1"},
        {
            "expected_user": "scheduler",
            "expected_account": "account",
            "accepted_submit_contract_version": "nhms.accepted_submit.v1",
        },
    ],
    ids=["legacy", "global", "owner"],
)
def test_comment_sacct_refuses_every_scope_when_comment_storage_is_unproven(
    monkeypatch: pytest.MonkeyPatch,
    query_kwargs: dict[str, Any],
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    calls: list[list[str]] = []
    monkeypatch.setattr(
        reconcile_module,
        "_bounded_sacct_stdout",
        lambda command: calls.append(list(command)) or "",
    )
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=lambda: False,
    )

    with pytest.raises(
        reconcile_module.ReconcileQueryUnavailable,
        match="accounting does not store job comments",
    ) as error:
        query("key", **query_kwargs)
    assert error.value.reason_class == "comment_accounting_unproven"
    assert calls == []


@pytest.mark.parametrize("proven", [True, False, None])
def test_comment_storage_probe_runs_once_per_querier_instance(
    monkeypatch: pytest.MonkeyPatch,
    proven: bool | None,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    calls: list[list[str]] = []
    monkeypatch.setattr(
        reconcile_module,
        "_bounded_sacct_stdout",
        lambda command: calls.append(list(command)) or "",
    )
    probes = 0

    def storage_probe() -> bool | None:
        nonlocal probes
        probes += 1
        return proven

    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=storage_probe,
    )
    page_count = (reconcile_module.COMMENT_SACCT_LOOKBACK_DAYS * 24) // reconcile_module.COMMENT_SACCT_PAGE_HOURS

    for key in ("key-a", "key-b"):
        if proven is True:
            assert tuple(query(key)) == ()
        else:
            # Explicit False and unknown None both refuse query-free; the probe
            # still runs exactly once for the querier instance (#1565 D1).
            with pytest.raises(reconcile_module.ReconcileQueryUnavailable):
                query(key)
    assert probes == 1
    assert len(calls) == (page_count if proven is True else 0)


def test_comment_storage_gate_outranks_visibility_but_not_the_contract_version_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", lambda _command: "")
    probes = 0

    def storage_probe() -> bool:
        nonlocal probes
        probes += 1
        return False

    both_unproven = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: False,
        comment_storage_probe=storage_probe,
    )
    with pytest.raises(reconcile_module.ReconcileQueryUnavailable) as error:
        both_unproven("key", accepted_submit_contract_version="nhms.accepted_submit.v1")
    assert error.value.reason_class == "comment_accounting_unproven"

    unsupported = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: False,
        comment_storage_probe=storage_probe,
    )
    with pytest.raises(
        reconcile_module.ReconcileQueryUnavailable,
        match="contract version is unsupported",
    ):
        unsupported("key", accepted_submit_contract_version="nhms.accepted_submit.v0")
    assert probes == 1


def test_reserved_row_stays_reserved_on_a_cluster_that_does_not_store_comments(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    query_end = datetime(2026, 7, 22, 12, tzinfo=UTC)
    repository = _file_cohort_repository(
        tmp_path,
        created_at=query_end - timedelta(days=1),
        member_count=1,
    )
    # The genuinely in-flight cohort job, as a comment-less cluster reports it:
    # accounting stores the row but drops the sbatch --comment, so the comment
    # index can never see it.
    monkeypatch.setattr(
        reconcile_module,
        "_bounded_sacct_stdout",
        lambda _command: "72001|nhms_forecast|RUNNING|0:0||scheduler|account\n",
    )
    monkeypatch.setattr(
        reconcile_module,
        "_bounded_visibility_stdout",
        lambda _command: "PrivateData = none\nAccountingStoreFlags    = (null)\n",
    )

    outcome = reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            now=lambda: query_end,
        ),
        grace=timedelta(0),
        now=lambda: query_end,
    )[0]

    pipeline_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert persisted["status"] == "reserved"
    assert persisted["slurm_job_id"] is None
    assert outcome.action == "query_unavailable"
    assert outcome.status == "reserved"
    assert outcome.reconciliation_decision == "accounting_unavailable"
    assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
    assert persisted["reconciliation_reason_class"] == "comment_accounting_unproven"


def test_comment_storing_cluster_still_binds_and_still_demotes_past_grace(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    query_end = datetime(2026, 7, 22, 12, tzinfo=UTC)
    pipeline_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    matched_repository = _file_cohort_repository(
        tmp_path / "matched",
        created_at=query_end - timedelta(days=1),
        member_count=1,
    )
    comment = str(matched_repository.get_accepted_submit_pipeline_job(pipeline_job_id)["slurm_comment"])
    monkeypatch.setattr(
        reconcile_module,
        "_bounded_sacct_stdout",
        lambda _command: f"72001|nhms_forecast|RUNNING|0:0|{comment}|||\n",
    )
    bound = reconcile_module.reconcile_reserved_unbound_jobs(
        matched_repository,
        comment_query=reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            comment_storage_probe=lambda: True,
            now=lambda: query_end,
        ),
        now=lambda: query_end,
    )[0]
    assert bound.action == "bound"
    assert matched_repository.get_accepted_submit_pipeline_job(pipeline_job_id)["slurm_job_id"] == "72001"

    absent_repository = _file_cohort_repository(
        tmp_path / "absent",
        created_at=query_end - timedelta(days=1),
        member_count=1,
    )
    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", lambda _command: "")
    absent = reconcile_module.reconcile_reserved_unbound_jobs(
        absent_repository,
        comment_query=reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            comment_storage_probe=lambda: True,
            now=lambda: query_end,
        ),
        grace=timedelta(0),
        now=lambda: query_end,
    )[0]
    assert absent.action == "absence_retry_permitted"
    assert absent_repository.get_accepted_submit_pipeline_job(pipeline_job_id)["status"] == "reservation_lost"


@pytest.mark.parametrize("stream_fd", [1, 2])
def test_global_accounting_visibility_process_bounds_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
    stream_fd: int,
) -> None:
    import sys

    from services.orchestrator import reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "MAX_VISIBILITY_PROBE_BYTES", 64)
    command = [
        sys.executable,
        "-c",
        f"import os; os.write({stream_fd}, b'x' * 4096)",
    ]
    with pytest.raises(reconcile_module.ReconcileQuerySaturated) as error:
        reconcile_module._bounded_visibility_stdout(command)
    assert error.value.boundary == "bytes"


def test_global_accounting_visibility_process_timeout_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from services.orchestrator import reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "COMMENT_SACCT_VISIBILITY_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(reconcile_module.ReconcileQueryUnavailable, match="timed out"):
        reconcile_module._bounded_visibility_stdout(
            [sys.executable, "-c", "import time; time.sleep(10)"],
        )


@pytest.mark.parametrize("boundary", ["bytes", "rows", "timeout"])
@pytest.mark.parametrize("stream_fd", [1, 2])
def test_round8_visibility_probe_saturation_and_timeout_reap_actual_child_pid(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    stream_fd: int,
) -> None:
    import sys

    from services.orchestrator import reconcile as reconcile_module

    pid_path = tmp_path / f"{boundary}-{stream_fd}.pid"
    if boundary == "bytes":
        monkeypatch.setattr(reconcile_module, "MAX_VISIBILITY_PROBE_BYTES", 64)
        body = f"os.write({stream_fd}, b'x' * 4096)"
        expected_error = reconcile_module.ReconcileQuerySaturated
    elif boundary == "rows":
        monkeypatch.setattr(reconcile_module, "MAX_VISIBILITY_PROBE_BYTES", 1024 * 1024)
        monkeypatch.setattr(reconcile_module, "MAX_VISIBILITY_PROBE_ROWS", 2)
        body = f"os.write({stream_fd}, b'x\\n' * 64)"
        expected_error = reconcile_module.ReconcileQuerySaturated
    else:
        monkeypatch.setattr(reconcile_module, "COMMENT_SACCT_VISIBILITY_TIMEOUT_SECONDS", 0.05)
        body = "time.sleep(10)"
        expected_error = reconcile_module.ReconcileQueryUnavailable
    script = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        + body
    )
    with pytest.raises(expected_error) as error:
        reconcile_module._bounded_visibility_stdout([sys.executable, "-c", script, str(pid_path)])
    if boundary in {"bytes", "rows"}:
        assert error.value.boundary == boundary
    child_pid = int(pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("PrivateData = none\nPrivateData = none\n", True),
        ("PrivateData = none\nPrivateData = jobs\n", False),
        ("PrivateData = all\nPrivateData = none\n", False),
        ("unrelated = none\n", False),
    ],
)
def test_private_data_visibility_requires_every_occurrence_to_allow_jobs(
    stdout: str,
    expected: bool,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    assert reconcile_module._private_data_allows_global_jobs(stdout) is expected


def test_comment_sacct_production_cadence_pages_are_independently_bounded_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    page_count = (reconcile_module.COMMENT_SACCT_LOOKBACK_DAYS * 24) // reconcile_module.COMMENT_SACCT_PAGE_HOURS
    stages = ("forcing", "forecast", "state_save_qc")
    expected_ids: dict[str, str] = {}

    def page_rows(page_index: int) -> str:
        rows: list[str] = []
        for source_index, source in enumerate(("gfs", "ifs")):
            for stage_index, stage in enumerate(stages):
                key = f"{source}:day{page_index:02d}:{stage}"
                master_id = str(17000 + page_index * 10 + source_index * len(stages) + stage_index)
                expected_ids[key] = master_id
                for task_id in range(256):
                    rows.append(
                        f"{master_id}_{task_id}|nhms_{stage}|RUNNING|0:0|nhms_idem:{key}|scheduler|account\n"
                    )
                    rows.append(
                        f"{master_id}_{task_id}.batch|batch|RUNNING|0:0|nhms_idem:{key}|scheduler|account\n"
                    )
        assert len(rows) == 2 * 3 * 256 * 2
        return "".join(rows)

    commands: list[list[str]] = []
    scope_pages = {"owner": 0, "global": 0}

    def bounded(command: Any) -> str:
        commands.append(list(command))
        scope = "global" if "--allusers" in command else "owner"
        page_index = scope_pages[scope]
        scope_pages[scope] += 1
        return page_rows(page_index)

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=lambda: True,
        now=lambda: datetime(2026, 7, 22, 12, tzinfo=UTC),
    )

    target = f"ifs:day{page_count - 1:02d}:state_save_qc"
    proof = reconcile_module._query_comment_accounting_proof(
        query,
        target,
        expected_user="scheduler",
        expected_account="account",
    )
    assert proof.kind == "owned_match"
    assert [record.slurm_job_id for record in proof.records] == [expected_ids[target]]
    assert scope_pages == {"owner": page_count, "global": page_count}
    assert len(commands) == page_count * 2

    cached_target = "gfs:day00:forcing"
    cached_proof = reconcile_module._query_comment_accounting_proof(
        query,
        cached_target,
        expected_user="scheduler",
        expected_account="account",
    )
    assert cached_proof.kind == "owned_match"
    assert [record.slurm_job_id for record in cached_proof.records] == [expected_ids[cached_target]]
    assert len(commands) == page_count * 2
    assert all(any(item.startswith("--endtime=") for item in command) for command in commands)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset() is POSIX-only")
def test_comment_sacct_session_freezes_advancing_clock_window_for_all_keys_and_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    page_count = (reconcile_module.COMMENT_SACCT_LOOKBACK_DAYS * 24) // reconcile_module.COMMENT_SACCT_PAGE_HOURS
    base_now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    now_calls: list[datetime] = []

    def advancing_now() -> datetime:
        value = base_now + timedelta(seconds=len(now_calls))
        now_calls.append(value)
        return value

    commands: list[list[str]] = []
    scope_pages = {"owner": 0, "global": 0}
    late_key = "gfs:late:forecast"
    early_key = "ifs:early:state_save_qc"

    def bounded(command: Any) -> str:
        commands.append(list(command))
        scope = "global" if "--allusers" in command else "owner"
        page_index = scope_pages[scope]
        scope_pages[scope] += 1
        if page_index == 0:
            return f"17668_0.batch|batch|RUNNING|0:0|nhms_idem:{early_key}|scheduler|account\n"
        if page_index == page_count - 1:
            return f"17667_0|nhms_forecast|RUNNING|0:0|nhms_idem:{late_key}|scheduler|account\n"
        return ""

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)
    with _pinned_local_timezone("Asia/Shanghai"):
        query = reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            comment_storage_probe=lambda: True,
            now=advancing_now,
        )

        late_proof = reconcile_module._query_comment_accounting_proof(
            query,
            late_key,
            expected_user="scheduler",
            expected_account="account",
        )
        assert late_proof.kind == "owned_match"
        assert [record.slurm_job_id for record in late_proof.records] == ["17667"]
        assert len(commands) == page_count * 2
        assert scope_pages == {"owner": page_count, "global": page_count}

        early_proof = reconcile_module._query_comment_accounting_proof(
            query,
            early_key,
            expected_user="scheduler",
            expected_account="account",
        )
    assert early_proof.kind == "owned_match"
    assert [record.slurm_job_id for record in early_proof.records] == ["17668"]
    assert len(commands) == page_count * 2
    assert now_calls == [base_now]
    # base_now is 2026-07-22T12:00Z; UTC+8 renders it as the host's local wall clock.
    assert "--endtime=2026-07-22T20:00:00" in commands[0]


# --- #1850 Phase 6b (Fix D): capability probing is lazy — an empty or
# unversioned-only reserved-unbound inventory must never trigger the probe.


def _counting_storage_probe(counter: list[int], proven: bool | None = False):
    def _probe() -> bool | None:
        counter[0] += 1
        return proven

    return _probe


def test_capability_probe_not_called_with_zero_reserved_rows(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix D: an empty reserved-unbound inventory must not probe capability.

    ``reconcile_reserved_unbound_jobs`` on a journal with no reserved-unbound
    rows performs zero storage probes and zero accounting queries."""
    from services.orchestrator import reconcile as reconcile_module
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    assert repository.query_reserved_unbound_jobs() == []
    probes: list[int] = [0]
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=_counting_storage_probe(probes),
        now=lambda: datetime(2026, 7, 12, 2, tzinfo=UTC),
    )
    reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=query,
        now=lambda: datetime(2026, 7, 12, 2, tzinfo=UTC),
    )
    assert probes[0] == 0


def test_capability_probe_not_called_with_unversioned_only_rows(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix D: unversioned-only reserved rows must not trigger the probe.

    A legacy/unversioned reserved-unbound row never needs the current-contract
    lane selection, so capability stays unprobed and no fallback/accounting
    query issues."""
    from services.orchestrator import reconcile as reconcile_module
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _versioned_master_reservation_record(
        created_at=datetime(2026, 7, 12, tzinfo=UTC),
        member_count=1,
        versioned=False,
    )
    repository.reserve_pipeline_job(record)
    assert len(repository.query_reserved_unbound_jobs()) == 1
    probes: list[int] = [0]
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=_counting_storage_probe(probes),
        now=lambda: datetime(2026, 7, 12, 2, tzinfo=UTC),
    )
    reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=query,
        now=lambda: datetime(2026, 7, 12, 2, tzinfo=UTC),
    )
    assert probes[0] == 0


def test_capability_probe_runs_once_for_two_current_rows(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix D: two current-contract rows probe capability exactly once.

    The verdict is computed once for the pass and reused for both rows, even
    though each row needs lane selection."""
    from services.orchestrator import reconcile as reconcile_module
    from tests.gateway_reconcile_helpers import (
    _file_cohort_repository,
    _versioned_master_reservation_record,
)

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(
        tmp_path / "gfs",
        created_at=anchor,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
    )
    record = _versioned_master_reservation_record(
        created_at=anchor,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
        source_id="ifs",
    )
    repository.reserve_pipeline_job(record)
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )

    repository.transition_pipeline_job_submit_evidence(
        record["job_id"],
        AcceptedSubmitTransition.timeout(),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    )
    probes: list[int] = [0]
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=_counting_storage_probe(probes),
        now=lambda: datetime(2026, 7, 12, 2, tzinfo=UTC),
    )
    reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=query,
        now=lambda: datetime(2026, 7, 12, 2, tzinfo=UTC),
    )
    assert probes[0] == 1
