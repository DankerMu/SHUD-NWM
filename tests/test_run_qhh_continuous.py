from __future__ import annotations

import signal
import subprocess
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts import run_qhh_continuous as runner


def _candidate() -> runner.CandidateCycle:
    return runner.CandidateCycle("gfs", datetime(2026, 5, 21, 6, tzinfo=UTC))


def test_slurm_preflight_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit, match="DATABASE_URL"):
        runner._require_slurm_reachable_database()


def test_slurm_preflight_rejects_localhost_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@localhost:55432/nhms")

    with pytest.raises(SystemExit, match="reachable from compute nodes"):
        runner._require_slurm_reachable_database()


def test_slurm_preflight_accepts_cluster_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@10.0.2.100:55432/nhms")

    runner._require_slurm_reachable_database()


def test_slurm_exports_filters_continuous_and_comma_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("QHH_CONTINUOUS_SOURCES", "gfs,IFS")
    monkeypatch.setenv("QHH_MAX_LEAD_HOURS", "144")
    monkeypatch.setenv("QHH_UNSAFE_LIST", "a,b")
    monkeypatch.setenv("QHH_SECRET_TOKEN", "do-not-export")
    monkeypatch.setenv("QHH-BAD-NAME", "do-not-export")
    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@10.0.2.100:55432/nhms")

    exports = runner._slurm_exports(_candidate(), tmp_path)
    formatted = runner._format_slurm_export(exports)

    assert "QHH_CONTINUOUS_SOURCES" not in exports
    assert "QHH_UNSAFE_LIST" not in exports
    assert "QHH_SECRET_TOKEN" not in exports
    assert "QHH-BAD-NAME" not in exports
    assert exports["QHH_MAX_LEAD_HOURS"] == "144"
    assert exports["QHH_SOURCE_ID"] == "gfs"
    assert exports["QHH_CYCLE_TIME"] == "2026052106"
    assert formatted.startswith("QHH_MAX_LEAD_HOURS=144,") or ",QHH_MAX_LEAD_HOURS=144," in formatted
    assert "ALL" not in formatted
    assert ",QHH_CONTINUOUS_SOURCES=" not in formatted


def test_slurm_exports_excludes_extra_database_and_credential_urls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:required@10.0.2.100:55432/nhms")
    monkeypatch.setenv("NHMS_INTEGRATION_DATABASE_URL", "postgresql://nhms:other-secret@db.example/integration")
    monkeypatch.setenv("PIPELINE_DATABASE_URL", "postgresql://nhms:pipeline-secret@db.example/pipeline")
    monkeypatch.setenv("QHH_AUX_DATABASE_URL", "postgresql://nhms:qhh-secret@db.example/qhh")
    monkeypatch.setenv("OBJECT_STORE_SECRET_ACCESS_KEY", "do-not-export")
    monkeypatch.setenv("OBJECT_STORE_SESSION_TOKEN", "do-not-export")
    monkeypatch.setenv("SHUD_LICENSE_TOKEN", "do-not-export")
    monkeypatch.setenv("SHUD_EXECUTABLE", "/opt/shud/bin/shud")
    monkeypatch.setenv("NHMS_BASINS_ROOT", "/data/Basins")
    monkeypatch.setenv("QHH_PACKAGE_VERSION", "v0.0.1-qhh-smoke-lake2")

    exports = runner._slurm_exports(_candidate(), tmp_path)

    assert exports["DATABASE_URL"] == "postgresql://nhms:required@10.0.2.100:55432/nhms"
    assert exports["SHUD_EXECUTABLE"] == "/opt/shud/bin/shud"
    assert exports["NHMS_BASINS_ROOT"] == "/data/Basins"
    assert exports["QHH_PACKAGE_VERSION"] == "v0.0.1-qhh-smoke-lake2"
    assert "NHMS_INTEGRATION_DATABASE_URL" not in exports
    assert "PIPELINE_DATABASE_URL" not in exports
    assert "QHH_AUX_DATABASE_URL" not in exports
    assert "OBJECT_STORE_SECRET_ACCESS_KEY" not in exports
    assert "OBJECT_STORE_SESSION_TOKEN" not in exports
    assert "SHUD_LICENSE_TOKEN" not in exports


def test_slurm_submit_uses_env_file_not_full_submit_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="5743\n", stderr="")

    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@10.0.2.100:55432/nhms")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    args = Namespace(
        slurm_partition="CPU",
        slurm_cpus=2,
        slurm_mem="4G",
        slurm_time="00:30:00",
        slurm_wait=False,
    )

    result = runner._submit_slurm_cycle(_candidate(), run_root=tmp_path, state_file=tmp_path / "state.json", args=args)

    submit_command = commands[0]
    export_arg = submit_command[submit_command.index("--export") + 1]
    assert result["status"] == "submitted"
    assert export_arg.startswith("QHH_SLURM_ENV_FILE=")
    assert "ALL" not in export_arg
    assert "DATABASE_URL" not in export_arg
    env_file = tmp_path / "slurm-logs" / "gfs" / "2026052106" / "qhh-cycle.env"
    assert env_file.exists()
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert "DATABASE_URL=" in env_file.read_text(encoding="utf-8")


def test_slurm_wait_returns_unknown_when_accounting_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "squeue":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="sacct disabled")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    status = runner._wait_for_slurm_job("5743", wait_timeout_seconds=1, accounting_timeout_seconds=1)

    assert status == runner.SLURM_ACCOUNTING_UNKNOWN


def test_slurm_wait_is_bounded_when_sacct_has_no_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "squeue":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: sleeps.append(seconds))

    status = runner._wait_for_slurm_job(
        "5743",
        wait_timeout_seconds=5,
        accounting_timeout_seconds=1,
        accounting_poll_seconds=1,
    )

    assert status == runner.SLURM_ACCOUNTING_UNKNOWN
    assert sleeps


def test_slurm_wait_timeout_while_job_active_returns_nonterminal_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monotonic_values = iter([0.0, 0.0, 0.0, 2.0])

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert command[0] == "squeue"
        return subprocess.CompletedProcess(command, 0, stdout="RUNNING\n", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    status = runner._wait_for_slurm_job("5743", wait_timeout_seconds=1, accounting_timeout_seconds=1)

    assert status == runner.SLURM_WAIT_TIMEOUT
    assert calls == [["squeue", "-h", "-j", "5743", "-o", "%T"]]


def test_slurm_submit_wait_timeout_keeps_submitted_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="5743\n", stderr="")

    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@10.0.2.100:55432/nhms")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "_wait_for_slurm_job", lambda *_args, **_kwargs: runner.SLURM_WAIT_TIMEOUT)
    args = Namespace(
        slurm_partition="CPU",
        slurm_cpus=2,
        slurm_mem="4G",
        slurm_time="00:30:00",
        slurm_wait=True,
        slurm_wait_timeout_seconds=1,
        slurm_accounting_timeout_seconds=1,
    )
    state_file = tmp_path / "state.json"

    result = runner._submit_slurm_cycle(_candidate(), run_root=tmp_path, state_file=state_file, args=args)
    state = runner._read_json(state_file)

    assert result["status"] == "submitted"
    assert result["slurm_status"] == runner.SLURM_WAIT_TIMEOUT
    assert state["status"] == "submitted"
    assert state["slurm_job_id"] == "5743"
    assert "finished_at" not in state


def test_slurm_wait_signal_cancels_job(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers: dict[int, Any] = {}
    calls: list[list[str]] = []

    def fake_signal(signum: int, handler: Any) -> Any:
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        return previous

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0] == "squeue":
            return subprocess.CompletedProcess(command, 0, stdout="RUNNING\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def fake_sleep(_seconds: float) -> None:
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(runner.signal, "signal", fake_signal)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner.time, "sleep", fake_sleep)

    with pytest.raises(SystemExit):
        runner._wait_for_slurm_job("5743", wait_timeout_seconds=1, accounting_timeout_seconds=1)

    assert ["scancel", "5743"] in calls


def test_active_submitted_slurm_state_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0] == "squeue":
            return subprocess.CompletedProcess(command, 0, stdout="RUNNING\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    state = {"status": "submitted", "source_id": "gfs", "cycle_time": "2026052106", "slurm_job_id": "5743"}

    reason = runner._skip_reason(_candidate(), state, executor="slurm")

    assert reason == "slurm job 5743 is still active"
    assert calls == [["squeue", "-h", "-j", "5743", "-o", "%T"]]


def test_finished_submitted_slurm_state_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "squeue":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="COMPLETED|\n", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    state = {"status": "submitted", "source_id": "gfs", "cycle_time": "2026052106", "slurm_job_id": "5743"}

    assert runner._skip_reason(_candidate(), state, executor="slurm") is None


def test_run_pass_does_not_resubmit_active_slurm_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_root = tmp_path / "state"
    state_file = runner._state_file(state_root, _candidate())
    runner._write_state(
        state_file,
        {"status": "submitted", "source_id": "gfs", "cycle_time": "2026052106", "slurm_job_id": "5743"},
    )
    monkeypatch.setattr(
        runner,
        "_candidate_cycles",
        lambda **_: [_candidate()],
    )
    monkeypatch.setattr(runner, "_slurm_job_is_active", lambda job_id: job_id == "5743")
    monkeypatch.setattr(
        runner,
        "_submit_slurm_cycle",
        lambda *_args, **_kwargs: pytest.fail("active job should not be resubmitted"),
    )
    args = Namespace(
        sources="gfs",
        lookback_hours=24,
        max_cycles_per_source=1,
        cycle_lag_hours=6,
        dry_run=False,
        executor="slurm",
    )

    summary = runner.run_pass(args=args, run_root=tmp_path, state_root=state_root)

    assert summary["status"] == "completed"
    assert summary["results"][0]["reason"] == "slurm job 5743 is still active"


def test_run_pass_does_not_resubmit_wait_timeout_slurm_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_root = tmp_path / "state"
    state_file = runner._state_file(state_root, _candidate())
    runner._write_state(
        state_file,
        {
            "status": "submitted",
            "source_id": "gfs",
            "cycle_time": "2026052106",
            "slurm_job_id": "5743",
            "slurm_status": runner.SLURM_WAIT_TIMEOUT,
        },
    )
    monkeypatch.setattr(runner, "_candidate_cycles", lambda **_: [_candidate()])
    monkeypatch.setattr(runner, "_slurm_job_is_active", lambda job_id: job_id == "5743")
    monkeypatch.setattr(
        runner,
        "_submit_slurm_cycle",
        lambda *_args, **_kwargs: pytest.fail("wait-timeout job should not be resubmitted while active"),
    )
    args = Namespace(
        sources="gfs",
        lookback_hours=24,
        max_cycles_per_source=1,
        cycle_lag_hours=6,
        dry_run=False,
        executor="slurm",
    )

    summary = runner.run_pass(args=args, run_root=tmp_path, state_root=state_root)

    assert summary["status"] == "completed"
    assert summary["results"][0]["status"] == "submitted"
    assert summary["results"][0]["reason"] == "slurm job 5743 is still active"
