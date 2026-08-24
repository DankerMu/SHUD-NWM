"""Bounded sacct stream reading: page bounds by host timezone, collision
detection across pages, per-page limits, and real-process reaping.
"""

from __future__ import annotations

import os
import time
from datetime import (
    UTC,
    datetime,
)
from typing import Any

import pytest

from services.orchestrator.reconcile import (
    RECONCILE_UNVERIFIED_STATUS,
    SacctRecord,
    reconcile_inflight_jobs,
)
from tests.gateway_reconcile_helpers import (
    _bind_current_file_cohort,
    _file_cohort_repository,
)
from tests.test_real_slurm_gateway import _pinned_local_timezone

# sacct reads bare timestamps in the host's local timezone, so the pinned instant below
# is rendered differently per host TZ; expectations are literal, never recomputed.
_PINNED_COMMENT_SCAN_NOW = datetime(2026, 7, 12, 4, 0, 0, tzinfo=UTC)


def _rendered_page_bounds(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], int]:
    """Return every sacct page command of one global-scope scan, newest page first."""
    from services.orchestrator import reconcile as reconcile_module

    page_count = (reconcile_module.COMMENT_SACCT_LOOKBACK_DAYS * 24) // reconcile_module.COMMENT_SACCT_PAGE_HOURS
    commands: list[list[str]] = []

    def bounded(command: Any) -> str:
        commands.append(list(command))
        return ""

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)
    reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=lambda: True,
        now=lambda: _PINNED_COMMENT_SCAN_NOW,
    )("gfs:tz:forecast")

    assert len(commands) == page_count
    return commands, page_count


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset() is POSIX-only")
def test_comment_sacct_page_bounds_are_local_wall_clock_east_of_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _pinned_local_timezone("Asia/Shanghai"):
        commands, _ = _rendered_page_bounds(monkeypatch)

    assert "--endtime=2026-07-12T12:00:00" in commands[0]
    assert "--starttime=2026-07-05T12:00:00" in commands[-1]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset() is POSIX-only")
def test_comment_sacct_page_bounds_are_local_wall_clock_west_of_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _pinned_local_timezone("America/New_York"):
        commands, _ = _rendered_page_bounds(monkeypatch)

    assert "--endtime=2026-07-12T00:00:00" in commands[0]
    assert "--starttime=2026-07-05T00:00:00" in commands[-1]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset() is POSIX-only")
def test_comment_sacct_page_bounds_on_utc_host_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _pinned_local_timezone("UTC"):
        commands, page_count = _rendered_page_bounds(monkeypatch)

    assert "--endtime=2026-07-12T04:00:00" in commands[0]
    assert "--starttime=2026-07-05T04:00:00" in commands[-1]
    # Page identity is the rendered pair, so a UTC host must still see one command per page.
    rendered_bounds = {
        tuple(item for item in command if item.startswith(("--starttime=", "--endtime=")))
        for command in commands
    }
    assert len(rendered_bounds) == page_count


def test_comment_sacct_global_collision_is_detected_across_separate_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    page_count = (reconcile_module.COMMENT_SACCT_LOOKBACK_DAYS * 24) // reconcile_module.COMMENT_SACCT_PAGE_HOURS
    scope_pages = {"owner": 0, "global": 0}
    target = "gfs:collision:forecast"

    def bounded(command: Any) -> str:
        scope = "global" if "--allusers" in command else "owner"
        page_index = scope_pages[scope]
        scope_pages[scope] += 1
        if page_index == 0:
            return f"17667_0.batch|batch|RUNNING|0:0|nhms_idem:{target}|scheduler|account\n"
        if scope == "global" and page_index == page_count - 1:
            return f"17668_0|nhms_forecast|RUNNING|0:0|nhms_idem:{target}|foreign|other\n"
        return ""

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)
    proof = reconcile_module._query_comment_accounting_proof(
        reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            comment_storage_probe=lambda: True,
        ),
        target,
        expected_user="scheduler",
        expected_account="account",
    )

    assert proof.kind == "foreign_collision"
    assert [record.slurm_job_id for record in proof.records] == ["17667", "17668"]
    assert scope_pages == {"owner": page_count, "global": page_count}


@pytest.mark.parametrize("boundary", ["row", "byte"])
def test_comment_sacct_rejects_any_single_page_over_its_bound(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_ROWS", 2)
    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_BYTES", 8)
    payload = "\n\n\n" if boundary == "row" else "123456789"
    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", lambda _command: payload)

    with pytest.raises(reconcile_module.ReconcileQuerySaturated, match="bounded output") as error:
        reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            comment_storage_probe=lambda: True,
        )("key")
    expected = "rows" if boundary == "row" else "bytes"
    assert error.value.boundary == expected
    assert error.value.reason_class == f"bounded_output_{expected}_saturated"


def test_bounded_sacct_rejects_max_newlines_plus_unterminated_row(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    executable = tmp_path / "sacct"
    executable.write_text(
        "#!/bin/sh\ni=0\nwhile [ $i -lt 20000 ]; do printf '\\n'; i=$((i+1)); done\nprintf 'unterminated'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_ROWS", 20_000)

    with pytest.raises(reconcile_module.ReconcileQueryUnavailable, match="bounded output"):
        reconcile_module._bounded_sacct_stdout([str(executable)])


def test_comment_sacct_querier_proves_owner_candidate_against_global_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []

    def bounded(command: Any) -> str:
        commands.append(list(command))
        foreign = "".join(
            f"{17000 + index}|nhms_forecast|RUNNING|0:0|nhms_idem:key|foreign|other\n"
            for index in range(100)
        )
        owned = "17667|nhms_forecast|RUNNING|0:0|nhms_idem:key|scheduler|account\n"
        return owned + foreign if "--allusers" in command else foreign + owned

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)
    proof = reconcile_module._query_comment_accounting_proof(
        reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            comment_storage_probe=lambda: True,
        ),
        "key",
        expected_user="scheduler",
        expected_account="account",
    )

    assert proof.kind == "foreign_collision"
    page_count = (reconcile_module.COMMENT_SACCT_LOOKBACK_DAYS * 24) // reconcile_module.COMMENT_SACCT_PAGE_HOURS
    assert len(commands) == page_count + 1
    assert "--user=scheduler" in commands[0]
    assert "--accounts=account" in commands[0]
    assert "--allusers" not in commands[0]
    assert "--allusers" in commands[page_count]


def test_comment_sacct_global_overlimit_after_owner_candidate_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []

    def bounded(command: Any) -> str:
        commands.append(list(command))
        if "--allusers" in command:
            raise reconcile_module.ReconcileQueryUnavailable("sacct query exceeded bounded output")
        return "17667|nhms_forecast|RUNNING|0:0|nhms_idem:key|scheduler|account\n"

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)

    with pytest.raises(reconcile_module.ReconcileQueryUnavailable, match="bounded output"):
        reconcile_module._query_comment_accounting_proof(
            reconcile_module.default_comment_sacct_querier(
                global_visibility_probe=lambda: True,
                comment_storage_probe=lambda: True,
            ),
            "key",
            expected_user="scheduler",
            expected_account="account",
        )

    page_count = (reconcile_module.COMMENT_SACCT_LOOKBACK_DAYS * 24) // reconcile_module.COMMENT_SACCT_PAGE_HOURS
    assert len(commands) == page_count + 1
    assert "--user=scheduler" in commands[0]
    assert "--allusers" in commands[-1]


def test_inflight_sacct_querier_uses_shared_bounded_stream_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []

    def bounded(command: Any) -> str:
        commands.append(list(command))
        return "17667|nhms_forecast|RUNNING|0:0|nhms_idem:key|scheduler|account\n"

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)

    record = reconcile_module.default_sacct_querier()("17667")

    assert record is not None
    assert record.slurm_job_id == "17667"
    assert len(commands) == 1
    assert "--jobs=17667" in commands[0]


@pytest.mark.parametrize("boundary", ["byte", "row", "wall_time"])
def test_real_sacct_process_bounds_reap_and_leave_inflight_cohort_unchanged(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    executable_root = tmp_path / f"fake-sacct-{boundary}"
    executable_root.mkdir()
    executable = executable_root / "sacct"
    pid_path = tmp_path / f"{boundary}.pid"
    terminated_path = tmp_path / f"{boundary}.terminated"
    executable.write_text(
        """#!/bin/sh
printf '%s' "$$" > "$FAKE_SACCT_PID_PATH"
terminated() {
    : > "$FAKE_SACCT_TERMINATED_PATH"
    exit 0
}
trap terminated TERM INT
case "$FAKE_SACCT_BOUNDARY" in
    byte)
        while :; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'; done
        ;;
    row)
        while :; do printf '17667|nhms_forecast|RUNNING|0:0||scheduler|account\\n'; done
        ;;
    wall_time)
        exec sleep 60
        ;;
esac
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("FAKE_SACCT_BOUNDARY", boundary)
    monkeypatch.setenv("FAKE_SACCT_PID_PATH", str(pid_path))
    monkeypatch.setenv("FAKE_SACCT_TERMINATED_PATH", str(terminated_path))
    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_BYTES", 128 if boundary == "byte" else 1_000_000)
    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_ROWS", 2 if boundary == "row" else 10_000)
    monkeypatch.setattr(
        reconcile_module,
        "COMMENT_SACCT_TIMEOUT_SECONDS",
        1.0 if boundary == "wall_time" else 2.0,
    )

    repository = _file_cohort_repository(tmp_path / "state", member_count=1)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    before = repository.get_pipeline_job(job_id)

    outcomes = reconcile_inflight_jobs(
        repository,
        sacct_query=reconcile_module.default_sacct_querier(str(executable_root)),
    )

    assert len(outcomes) == 1
    assert outcomes[0].action == "query_unavailable"
    assert outcomes[0].durable_write_count == 0
    assert len(repr(outcomes[0])) < 1_000
    assert repository.get_pipeline_job(job_id) == before
    assert not before.get("candidate_projections")
    if boundary != "wall_time":
        assert terminated_path.exists()
    child_pid = int(pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ChildProcessError):
        os.waitpid(child_pid, os.WNOHANG)


def test_parse_master_sacct_row_returns_exact_array_task_row() -> None:
    from services.orchestrator.reconcile import _parse_master_sacct_row

    stdout = (
        "12345|nhms_run_shud_forecast_array|COMPLETED|0:0|master-comment\n"
        "12345_2|nhms_run_shud_forecast_array|FAILED|1:0|task-2\n"
        "12345_3|nhms_run_shud_forecast_array|COMPLETED|0:0|task-3\n"
        "12345_3.batch|batch|COMPLETED|0:0|task-3\n"
    )

    record = _parse_master_sacct_row(stdout, "12345_3")

    assert record is not None
    assert record.slurm_job_id == "12345_3"
    assert record.task_id == "3"
    assert record.array_task_id == "3"
    assert record.raw_state == "COMPLETED"


@pytest.mark.parametrize(
    ("member_rows", "expected_state", "expected_exit_code"),
    [
        (
            "15144_0|nhms_forecast|PENDING|0:0|\n"
            "15144_1|nhms_forecast|PENDING|0:0|\n",
            "PENDING",
            None,
        ),
        (
            "15144_0|nhms_forecast|COMPLETED|0:0|\n"
            "15144_1|nhms_forecast|RUNNING|0:0|\n",
            "RUNNING",
            None,
        ),
        (
            "15144_0|nhms_forecast|FAILED|1:0|\n"
            "15144_1|nhms_forecast|RUNNING|0:0|\n",
            "RUNNING",
            None,
        ),
        (
            "15144_0|nhms_forecast|COMPLETED|0:0|\n"
            "15144_1|nhms_forecast|TIMEOUT|1:0|\n",
            "TIMEOUT",
            "1:0",
        ),
        (
            "15144_0|nhms_forecast|COMPLETED|0:0|\n"
            "15144_1|nhms_forecast|CANCELLED|0:15|\n",
            "CANCELLED",
            "0:15",
        ),
    ],
)
def test_parse_master_sacct_row_aggregates_array_member_statuses(
    member_rows: str,
    expected_state: str,
    expected_exit_code: str | None,
) -> None:
    from services.orchestrator.reconcile import _parse_master_sacct_row

    record = _parse_master_sacct_row(member_rows, "15144")

    assert record is not None
    assert record.slurm_job_id == "15144"
    assert record.job_name == "nhms_forecast"
    assert record.raw_state == expected_state
    assert record.exit_code == expected_exit_code
    assert record.array_member_job_ids == ("15144_0", "15144_1")


def test_file_restart_reconcile_retries_unverified_array_master_without_resubmit(
    tmp_path: Any,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import _parse_master_sacct_row

    cycle_time = datetime(2026, 7, 18, 1, tzinfo=UTC)
    cycle_id = "gfs_2026071801"
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.upsert_pipeline_job(
        {
            "job_id": "job_gfs_2026071801_model_a_forecast",
            "run_id": "fcst_gfs_2026071801_model_a",
            "cycle_id": cycle_id,
            "job_type": "run_shud_forecast_array",
            "slurm_job_id": "15144",
            "array_task_id": None,
            "model_id": "model_a",
            "status": "submitted",
            "stage": "forecast",
            "idempotency_key": "gfs:gfs_2026071801:model_a:forecast",
            "candidate_id": (
                "gfs:2026-07-18T01:00:00Z:model_a:forecast_gfs_deterministic"
            ),
        }
    )

    query_count = 0

    def _sacct_query(_slurm_job_id: str) -> SacctRecord | None:
        nonlocal query_count
        query_count += 1
        if query_count == 1:
            return None
        return _parse_master_sacct_row(
            "15144_0|nhms_forecast|COMPLETED|0:0|\n"
            "15144_0.batch|batch|COMPLETED|0:0|\n",
            "15144",
        )

    first = reconcile_inflight_jobs(repository, sacct_query=_sacct_query)
    assert first[0].action == "unverified"
    assert repository.get_pipeline_job(first[0].job_id)["status"] == (
        RECONCILE_UNVERIFIED_STATUS
    )
    assert [job.job_id for job in repository.query_inflight_jobs()] == [first[0].job_id]

    second = reconcile_inflight_jobs(repository, sacct_query=_sacct_query)

    assert query_count == 2
    assert second[0].action == "terminal"
    assert second[0].status == "succeeded"
    recovered = repository.get_pipeline_job(second[0].job_id)
    assert recovered is not None
    assert recovered["status"] == "succeeded"
    assert recovered["error_code"] is None
    assert repository.has_active_pipeline(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_a",
    ) is False
    # Restart reconcile only updates the existing durable row.  The inactive
    # gate now permits the scheduler to advance to state_save_qc without a
    # duplicate sbatch for the forecast stage.
    jobs = repository.query_pipeline_jobs_by_cycle(cycle_id)
    assert [job["job_id"] for job in jobs] == [second[0].job_id]
