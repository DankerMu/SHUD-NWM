"""Comment accounting coverage: versioned mutations never enumerate
unrelated history, adapter coverage recomputation, and sacct row parsing.
"""

from __future__ import annotations

import copy
import os
import threading
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from typing import Any

import pytest

from services.orchestrator.reconcile import SacctRecord
from tests.gateway_reconcile_helpers import (
    _authoritative_absence_query,
    _file_cohort_repository,
    _seed_unrelated_history,
)

# --- FINDING-2: real comment-row parsing + array-master normalization ----------


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
@pytest.mark.parametrize("operation", ["reserve", "commit", "accounting_bind", "reject"])
def test_versioned_accepted_submit_mutations_never_enumerate_unrelated_history(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    source_id: str,
    operation: str,
) -> None:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    if operation == "reserve":
        template = _file_cohort_repository(
            tmp_path / "template",
            member_count=1,
            with_runtime_rows=False,
            source_id=source_id,
        )
        pipeline_job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
        template_row = template.get_accepted_submit_pipeline_job(pipeline_job_id)
        repository = FileOrchestrationJournalRepository(tmp_path / "target")
        assert repository.query_reserved_unbound_jobs() == []
        _seed_unrelated_history(repository)
    else:
        repository = _file_cohort_repository(
            tmp_path / "target",
            member_count=1,
            source_id=source_id,
        )
        pipeline_job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
        template_row = None
        assert len(repository.query_reserved_unbound_jobs()) == 1
        _seed_unrelated_history(repository)
    repository.max_records = 8

    def global_iteration_forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("versioned accepted-submit mutation called the global history iterator")

    monkeypatch.setattr(repository, "_iter_pipeline_job_records", global_iteration_forbidden)

    if operation == "reserve":
        assert template_row is not None
        clean_template = {
            **template_row,
            "status": "reserved",
            "slurm_job_id": None,
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "reconciliation_reason_class": None,
            "matched_slurm_job_id": None,
            "candidate_projections": [],
        }
        created = repository.reserve_pipeline_job(dict(clean_template))
        assert created is not None
        assert repository.reserve_pipeline_job(dict(clean_template)) is None
        return

    current = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert current is not None
    idempotency_key = str(current["idempotency_key"])
    if operation == "commit":
        before = dict(current)
        stale = repository.commit_pipeline_job_submit_attempt(
            idempotency_key,
            pipeline_job_id=pipeline_job_id,
            expected_submission_attempt=2,
            slurm_job_id="71001",
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert stale.outcome == "stale"
        assert repository.get_accepted_submit_pipeline_job(pipeline_job_id) == before
        applied = repository.commit_pipeline_job_submit_attempt(
            idempotency_key,
            pipeline_job_id=pipeline_job_id,
            expected_submission_attempt=1,
            slurm_job_id="71001",
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert applied.outcome == "applied"
        assert applied.row["slurm_job_id"] == "71001"
        return

    if operation == "accounting_bind":
        exact = SacctRecord(
            slurm_job_id="71002",
            raw_state="RUNNING",
            job_name="nhms_forecast",
            comment=str(current["slurm_comment"]),
        )
        outcome = reconcile_reserved_unbound_jobs(repository, comment_query=lambda _key: exact)[0]
        assert outcome.action == "bound"
        assert repository.get_accepted_submit_pipeline_job(pipeline_job_id)["slurm_job_id"] == "71002"
        return

    rejected = repository.reject_pipeline_job_submit_attempt(
        idempotency_key,
        pipeline_job_id=pipeline_job_id,
        expected_submission_attempt=1,
        finished_at=datetime(2026, 7, 12, 0, 1, tzinfo=UTC),
        error_code="VALIDATION_ERROR",
        error_message="request rejected before acceptance",
        stage="forecast",
        job_type="run_shud_forecast_array",
    )
    assert rejected.outcome == "applied"
    reopened = FileOrchestrationJournalRepository(repository.root, max_records=8)
    row = reopened.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert row["submit_outcome"] == "rejected"
    with reopened._locked_cycle_write(
        source_id=source_id,
        cycle_time=datetime(2026, 7, 12, tzinfo=UTC),
    ):
        rows = reopened._cycle_rows_by_model_unlocked(
            source_id=source_id,
            cycle_time=datetime(2026, 7, 12, tzinfo=UTC),
            model_ids=("model_0",),
            include_direct_jobs=False,
        )
    assert rows["model_0"].hydro_run["status"] == "failed"


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
def test_default_comment_accounting_requires_full_attempt_coverage_but_still_binds_match(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    source_id: str,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    query_end = datetime(2026, 7, 22, 12, tzinfo=UTC)
    old_anchor = query_end - timedelta(days=8)
    repository = _file_cohort_repository(
        tmp_path / "absent",
        created_at=old_anchor,
        member_count=1,
        source_id=source_id,
    )
    commands: list[list[str]] = []

    def empty_page(command: list[str]) -> str:
        commands.append(command)
        return ""

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", empty_page)
    outcome = reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            comment_storage_probe=lambda: True,
            now=lambda: query_end,
        ),
        grace=timedelta(0),
        now=lambda: query_end,
    )[0]
    pipeline_job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert outcome.action == "query_unavailable"
    assert outcome.reconciliation_decision == "accounting_unavailable"
    assert outcome.reconciliation_reason_class == "coverage_incomplete"
    assert persisted["status"] == "reserved"
    assert persisted["slurm_job_id"] is None
    assert persisted["reconciliation_reason_class"] == "coverage_incomplete"
    assert commands
    assert all(any(arg.startswith("--starttime=") for arg in command) for command in commands)
    assert all(any(arg.startswith("--endtime=") for arg in command) for command in commands)

    covered_repository = _file_cohort_repository(
        tmp_path / "covered",
        created_at=query_end - timedelta(minutes=1),
        member_count=1,
        source_id=source_id,
    )
    covered = reconcile_module.reconcile_reserved_unbound_jobs(
        covered_repository,
        comment_query=reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            comment_storage_probe=lambda: True,
            now=lambda: query_end,
        ),
        grace=timedelta(0),
        now=lambda: query_end,
    )[0]
    assert covered.action == "absence_retry_permitted"

    matched_repository = _file_cohort_repository(
        tmp_path / "matched",
        created_at=old_anchor,
        member_count=1,
        source_id=source_id,
    )
    matched = matched_repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    comment = str(matched["slurm_comment"])
    row = f"72001|nhms_forecast|RUNNING|0:0|{comment}|||\n"
    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", lambda _command: row)
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


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
@pytest.mark.parametrize(
    "coverage_case",
    [
        "declared_false",
        "missing_bounds",
        "reversed_bounds",
        "outside_anchor",
        "malformed_bounds",
        "naive_bounds",
    ],
)
def test_versioned_zero_recomputes_adapter_coverage_at_consumer_boundary(
    tmp_path: Any,
    source_id: str,
    coverage_case: str,
) -> None:
    from services.orchestrator.reconcile import CommentAccountingResult, reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(
        tmp_path,
        created_at=anchor,
        member_count=1,
        source_id=source_id,
    )
    hydro_before = copy.deepcopy(repository._hydro_run_for(f"fcst_{source_id}_2026071200_model_0"))

    def declared_zero(_key: str, **_kwargs: Any) -> CommentAccountingResult:
        complete = coverage_case != "declared_false"
        start: Any = anchor - timedelta(seconds=1)
        end: Any = anchor + timedelta(seconds=1)
        if coverage_case == "missing_bounds":
            start = end = None
        elif coverage_case == "reversed_bounds":
            start, end = end, start
        elif coverage_case == "outside_anchor":
            start, end = anchor + timedelta(seconds=1), anchor + timedelta(seconds=2)
        elif coverage_case == "malformed_bounds":
            start, end = "2026-07-12T00:00:00Z", {"not": "a datetime"}
        elif coverage_case == "naive_bounds":
            start, end = datetime(2026, 7, 11, 23, 59), datetime(2026, 7, 12, 0, 1)
        return CommentAccountingResult(
            (),
            scope="global",
            coverage_start=start,
            coverage_end=end,
            coverage_complete=complete,
        )

    outcome = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=declared_zero,
        grace=timedelta(0),
        now=lambda: anchor + timedelta(minutes=1),
    )[0]
    pipeline_job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    hydro = repository._hydro_run_for(f"fcst_{source_id}_2026071200_model_0")
    assert outcome.action == "query_unavailable"
    assert outcome.reconciliation_reason_class == "coverage_incomplete"
    assert persisted["status"] == "reserved"
    assert persisted["slurm_job_id"] is None
    assert persisted["reconciliation_reason_class"] == "coverage_incomplete"
    assert hydro == hydro_before


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
def test_missing_durable_attempt_anchor_blocks_zero_but_not_exact_match(
    tmp_path: Any,
    source_id: str,
) -> None:
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)

    def hide_durable_anchor(repository: Any) -> None:
        original = repository.query_reserved_unbound_jobs

        def missing_anchor_rows() -> list[Any]:
            rows = original()
            for row in rows:
                row.submission_attempt_started_at = None
            return rows

        repository.query_reserved_unbound_jobs = missing_anchor_rows

    absent = _file_cohort_repository(
        tmp_path / "absent",
        created_at=anchor,
        member_count=1,
        source_id=source_id,
    )
    hide_durable_anchor(absent)
    unavailable = reconcile_reserved_unbound_jobs(
        absent,
        comment_query=_authoritative_absence_query,
        grace=timedelta(0),
        now=lambda: anchor + timedelta(minutes=1),
    )[0]
    pipeline_job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    persisted = absent.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert unavailable.action == "query_unavailable"
    assert unavailable.reconciliation_reason_class == "coverage_incomplete"
    assert persisted["status"] == "reserved"
    assert persisted["slurm_job_id"] is None

    matched = _file_cohort_repository(
        tmp_path / "matched",
        created_at=anchor,
        member_count=1,
        source_id=source_id,
    )
    identity = matched.get_accepted_submit_pipeline_job(pipeline_job_id)
    hide_durable_anchor(matched)
    exact = SacctRecord(
        "72501",
        "RUNNING",
        "nhms_forecast",
        comment=str(identity["slurm_comment"]),
    )
    bound = reconcile_reserved_unbound_jobs(matched, comment_query=lambda _key: exact)[0]
    assert bound.action == "bound"
    assert matched.get_accepted_submit_pipeline_job(pipeline_job_id)["slurm_job_id"] == "72501"


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
def test_valid_custom_coverage_permits_exactly_one_retry_and_marker_free_is_unchanged(
    tmp_path: Any,
    source_id: str,
) -> None:
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import CommentAccountingResult, reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)

    def valid_zero(_key: str, **_kwargs: Any) -> CommentAccountingResult:
        return CommentAccountingResult(
            (),
            scope="global",
            coverage_start=anchor - timedelta(seconds=1),
            coverage_end=anchor + timedelta(seconds=1),
            coverage_complete=True,
        )

    repository = _file_cohort_repository(
        tmp_path / "versioned",
        created_at=anchor,
        member_count=1,
        source_id=source_id,
    )
    pipeline_job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    assert (
        repository.permit_pipeline_job_retry(
            pipeline_job_id,
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_submission_attempt_started_at=anchor + timedelta(seconds=1),
        )
        == 0
    )
    assert repository.get_accepted_submit_pipeline_job(pipeline_job_id)["status"] == "reserved"
    first = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=valid_zero,
        grace=timedelta(0),
        now=lambda: anchor + timedelta(minutes=1),
    )
    second = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=valid_zero,
        grace=timedelta(0),
        now=lambda: anchor + timedelta(minutes=1),
    )
    assert [outcome.action for outcome in first] == ["absence_retry_permitted"]
    assert second == []

    marker_template = _file_cohort_repository(
        tmp_path / "marker-template",
        created_at=anchor,
        member_count=1,
        source_id=source_id,
        versioned=False,
    )
    marker_row = marker_template.get_pipeline_job(pipeline_job_id)
    marker_row.pop("submission_attempt_started_at")
    marker_free = FileOrchestrationJournalRepository(tmp_path / "marker-free")
    assert marker_free.reserve_pipeline_job(marker_row) is not None
    legacy = reconcile_reserved_unbound_jobs(marker_free, comment_query=lambda _key: None)
    assert [outcome.action for outcome in legacy] == ["legacy_unversioned_read_only"]


@pytest.mark.parametrize(
    ("boundary", "reason_class", "payload", "max_rows", "max_bytes"),
    [
        ("rows", "bounded_output_rows_saturated", "\n\n", 1, 1024),
        ("bytes", "bounded_output_bytes_saturated", "123456789", 100, 8),
    ],
)
def test_versioned_accounting_saturation_is_public_bounded_unavailable_evidence(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    reason_class: str,
    payload: str,
    max_rows: int,
    max_bytes: int,
) -> None:
    from services.orchestrator import reconcile as reconcile_module
    from services.orchestrator import scheduler_runtime

    repository = _file_cohort_repository(tmp_path, member_count=1)
    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_ROWS", max_rows)
    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_BYTES", max_bytes)
    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", lambda _command: payload)
    query_end = datetime(2026, 7, 12, 0, 1, tzinfo=UTC)
    outcome = reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            comment_storage_probe=lambda: True,
            now=lambda: query_end,
        ),
        now=lambda: query_end,
    )[0]
    pipeline_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    public = scheduler_runtime._restart_reconcile_attempt_evidence(repository, pipeline_job_id)
    assert outcome.action == "query_unavailable"
    assert outcome.match_count is None
    assert outcome.reconciliation_decision == "accounting_unavailable"
    assert outcome.reconciliation_reason_class == reason_class
    assert persisted["status"] == "reserved"
    assert persisted["slurm_job_id"] is None
    assert persisted["reconciliation_reason_class"] == reason_class
    assert public["reconciliation_reason_class"] == reason_class
    assert boundary in reason_class
    assert "nhms_idem:" not in str(public)
    assert str(repository.root) not in str(public)

    exact = reconcile_module.SacctRecord(
        "73001",
        "RUNNING",
        "nhms_forecast",
        comment=str(persisted["slurm_comment"]),
    )
    recovered = reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=lambda _key: exact,
    )[0]
    rebound = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert recovered.action == "bound"
    assert rebound["slurm_job_id"] == "73001"
    assert rebound["reconciliation_reason_class"] is None


def test_legacy_custom_comment_adapter_remains_callable_but_cannot_prove_versioned_zero(
    tmp_path: Any,
) -> None:
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    repository = _file_cohort_repository(tmp_path / "versioned", member_count=1)
    outcome = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=lambda _key: None,
        grace=timedelta(0),
    )[0]
    assert outcome.action == "query_unavailable"
    assert outcome.reconciliation_reason_class == "coverage_incomplete"

    marker_free = _file_cohort_repository(
        tmp_path / "legacy",
        member_count=1,
        versioned=False,
    )
    legacy = reconcile_reserved_unbound_jobs(marker_free, comment_query=lambda _key: None)[0]
    assert legacy.action == "legacy_unversioned_read_only"


def test_parse_comment_sacct_rows_resolves_array_master() -> None:
    """Real (non-mock) parse of multi-row sacct output: an array stage stamped
    with the idempotency --comment reconciles back to its BARE master id. Array
    element rows (``<master>_<task>``) normalize to ``<master>``, ``.batch`` step
    sub-rows are skipped, and an unrelated Comment never false-matches.
    """

    from services.orchestrator.reconcile import _parse_comment_sacct_rows
    from services.slurm_gateway.real_backend import SLURM_JOB_ID_RE

    stdout = (
        "77042_0|stageA|RUNNING|0:0|nhms_idem:K\n"
        "77042_1|stageA|RUNNING|0:0|nhms_idem:K\n"
        "77042.batch|batch|RUNNING|0:0|nhms_idem:K\n"
        "99999|other|RUNNING|0:0|nhms_idem:OTHER\n"
    )

    record = _parse_comment_sacct_rows(stdout, "nhms_idem:K")

    assert record is not None
    assert isinstance(record, SacctRecord)
    assert record.slurm_job_id == "77042"  # array element → bare master id.
    # The normalized id must pass the master/single-job id shape guard.
    assert SLURM_JOB_ID_RE.fullmatch("77042")


def test_parse_comment_sacct_rows_single_job() -> None:
    """A single (non-array) job with a matching Comment passes through unchanged;
    its ``.batch`` step sub-row is skipped.
    """

    from services.orchestrator.reconcile import _parse_comment_sacct_rows

    stdout = (
        "88001|stage|RUNNING|0:0|nhms_idem:K\n"
        "88001.batch|batch|RUNNING|0:0|nhms_idem:K\n"
    )

    record = _parse_comment_sacct_rows(stdout, "nhms_idem:K")

    assert record is not None
    assert record.slurm_job_id == "88001"  # no "_" → original id, untouched.


def test_parse_comment_sacct_rows_no_match_returns_none() -> None:
    """No row's Comment equals the target → None, the authoritative
    confirmed-absent answer that crash-recovery reconcile relies on.
    """

    from services.orchestrator.reconcile import _parse_comment_sacct_rows

    stdout = (
        "12345|stage|RUNNING|0:0|nhms_idem:OTHER\n"
        "12345.batch|batch|RUNNING|0:0|nhms_idem:OTHER\n"
        "67890_0|stage|RUNNING|0:0|nhms_idem:DIFFERENT\n"
    )

    assert _parse_comment_sacct_rows(stdout, "nhms_idem:K") is None


def test_comment_sacct_querier_scans_once_and_reaps_oversized_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from services.orchestrator import reconcile as reconcile_module

    scans = 0
    original_bounded = reconcile_module._bounded_sacct_stdout

    def bounded(_command: Any) -> str:
        nonlocal scans
        scans += 1
        return (
            "17667|nhms_forecast|RUNNING|0:0|nhms_idem:key-a|scheduler|account\n"
            "17668|nhms_forecast|PENDING|0:0|nhms_idem:key-b|scheduler|account\n"
        )

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=lambda: True,
    )
    assert query("key-a")[0].slurm_job_id == "17667"
    assert query("key-b")[0].slurm_job_id == "17668"
    assert scans == (reconcile_module.COMMENT_SACCT_LOOKBACK_DAYS * 24) // reconcile_module.COMMENT_SACCT_PAGE_HOURS

    class FakeProcess:
        def __init__(self) -> None:
            read_fd, self.write_fd = os.pipe()
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            self.terminated = threading.Event()
            self.reaped = False
            self.thread = threading.Thread(target=self._write, daemon=True)
            self.thread.start()

        def _write(self) -> None:
            try:
                while not self.terminated.is_set():
                    os.write(self.write_fd, b"x" * 64)
            except OSError:
                pass
            finally:
                os.close(self.write_fd)

        def poll(self) -> int | None:
            return -15 if self.terminated.is_set() else None

        def terminate(self) -> None:
            self.terminated.set()

        kill = terminate

        def wait(self, timeout: float | None = None) -> int:
            self.thread.join(timeout)
            self.reaped = not self.thread.is_alive()
            return -15

    processes: list[FakeProcess] = []

    def popen(*_args: Any, **_kwargs: Any) -> FakeProcess:
        processes.append(FakeProcess())
        return processes[-1]

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", original_bounded)
    monkeypatch.setattr(reconcile_module.subprocess, "Popen", popen)
    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_BYTES", 128)
    with pytest.raises(reconcile_module.ReconcileQueryUnavailable):
        reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            comment_storage_probe=lambda: True,
        )("secret-key")
    assert len(processes) == 1
    assert processes[0].reaped is True


def test_file_submit_attempt_commit_is_cas_bound_idempotent_and_reopen_safe(tmp_path: Any) -> None:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = _file_cohort_repository(tmp_path, member_count=18)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    applied = repository.commit_pipeline_job_submit_attempt(
        key,
        expected_submission_attempt=1,
        slurm_job_id="17667",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    idempotent = repository.commit_pipeline_job_submit_attempt(
        key,
        expected_submission_attempt=1,
        slurm_job_id="17667",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    collision = repository.commit_pipeline_job_submit_attempt(
        key,
        expected_submission_attempt=1,
        slurm_job_id="17668",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    stale = repository.commit_pipeline_job_submit_attempt(
        key,
        expected_submission_attempt=2,
        slurm_job_id="17667",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )

    assert (applied.outcome, idempotent.outcome, collision.outcome, stale.outcome) == (
        "applied",
        "idempotent",
        "collision",
        "stale",
    )
    reopened = FileOrchestrationJournalRepository(tmp_path / "journal")
    row = reopened.get_pipeline_job(job_id)
    assert row is not None
    assert row["slurm_job_id"] == "17667"
    assert row["submit_outcome"] == "accepted"
    assert [job.job_id for job in reopened.query_inflight_jobs()] == [job_id]
    assert reopened.query_reserved_unbound_jobs() == []
