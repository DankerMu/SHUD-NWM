from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
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

    assert "DATABASE_URL" not in exports
    assert exports["SHUD_EXECUTABLE"] == "/opt/shud/bin/shud"
    assert exports["NHMS_BASINS_ROOT"] == "/data/Basins"
    assert exports["QHH_PACKAGE_VERSION"] == "v0.0.1-qhh-smoke-lake2"
    assert "NHMS_INTEGRATION_DATABASE_URL" not in exports
    assert "PIPELINE_DATABASE_URL" not in exports
    assert "QHH_AUX_DATABASE_URL" not in exports
    assert "OBJECT_STORE_SECRET_ACCESS_KEY" not in exports
    assert "OBJECT_STORE_SESSION_TOKEN" not in exports
    assert "SHUD_LICENSE_TOKEN" not in exports


def test_slurm_exports_passes_through_force_upstream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@10.0.2.100:55432/nhms")
    monkeypatch.setenv("QHH_FORCE_UPSTREAM", "1")

    exports = runner._slurm_exports(_candidate(), tmp_path)

    assert exports["QHH_FORCE_UPSTREAM"] == "1"


def test_slurm_exports_omits_force_upstream_when_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@10.0.2.100:55432/nhms")
    monkeypatch.delenv("QHH_FORCE_UPSTREAM", raising=False)

    exports = runner._slurm_exports(_candidate(), tmp_path)

    assert "QHH_FORCE_UPSTREAM" not in exports


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
    assert ",DATABASE_URL" in export_arg
    assert "ALL" not in export_arg
    assert "postgresql://nhms:secret" not in export_arg
    env_file = tmp_path / "slurm-logs" / "gfs" / "2026052106" / "qhh-cycle.env"
    assert env_file.exists()
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert env_file.parent.stat().st_mode & 0o777 == 0o700
    assert env_file.parent.parent.stat().st_mode & 0o777 == 0o700
    env_content = env_file.read_text(encoding="utf-8")
    assert "DATABASE_URL=" not in env_content
    assert "postgresql://nhms:secret" not in env_content


def test_slurm_submit_rejects_non_numeric_sbatch_job_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="warning: retrying\nSubmitted batch job abc\n", stderr="")

    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@10.0.2.100:55432/nhms")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    args = Namespace(
        slurm_partition="CPU",
        slurm_cpus=2,
        slurm_mem="4G",
        slurm_time="00:30:00",
        slurm_wait=False,
    )
    state_file = tmp_path / "state.json"

    result = runner._submit_slurm_cycle(_candidate(), run_root=tmp_path, state_file=state_file, args=args)

    assert result["status"] == "failed"
    assert result["reason"] == "invalid sbatch job id"
    assert runner._read_json(state_file)["finished_at"]


def test_slurm_env_file_rejects_symlink_target_without_modifying_it(tmp_path: Path) -> None:
    target = tmp_path / "target.env"
    target.write_text("unchanged\n", encoding="utf-8")
    env_file = tmp_path / "slurm-logs" / "gfs" / "2026052106" / "qhh-cycle.env"
    env_file.parent.mkdir(parents=True)
    env_file.symlink_to(target)

    with pytest.raises(RuntimeError, match="symlink"):
        runner._write_slurm_env_file(env_file, {"QHH_RUN_ID": "fcst_gfs_2026052106_basins_qhh_shud"})

    assert target.read_text(encoding="utf-8") == "unchanged\n"
    assert env_file.is_symlink()


def test_slurm_env_file_helper_does_not_use_write_text_chmod_secret_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden_write_text(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Path.write_text must not be used for Slurm credential files")

    def forbidden_chmod(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Path.chmod must not be used after writing Slurm credential files")

    monkeypatch.setattr(Path, "write_text", forbidden_write_text)
    monkeypatch.setattr(Path, "chmod", forbidden_chmod)
    env_file = tmp_path / "slurm-logs" / "gfs" / "2026052106" / "qhh-cycle.env"

    runner._write_slurm_env_file(env_file, {"QHH_RUN_ID": "fcst_gfs_2026052106_basins_qhh_shud"})

    assert env_file.read_text(encoding="utf-8").startswith("export QHH_RUN_ID=")
    assert env_file.stat().st_mode & 0o777 == 0o600


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

    assert status == "RUNNING"
    assert calls == [["squeue", "-h", "-j", "5743", "-o", "%T"]]


@pytest.mark.parametrize("slurm_state", ["RUNNING", "PENDING"])
def test_slurm_wait_preserves_sacct_nonterminal_state(
    monkeypatch: pytest.MonkeyPatch,
    slurm_state: str,
) -> None:
    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "squeue":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout=f"{slurm_state}|\n", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    status = runner._wait_for_slurm_job("5743", wait_timeout_seconds=1, accounting_timeout_seconds=1)

    assert status == slurm_state


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


def test_slurm_submit_accounting_unknown_keeps_submitted_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="5743\n", stderr="")

    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@10.0.2.100:55432/nhms")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "_wait_for_slurm_job", lambda *_args, **_kwargs: runner.SLURM_ACCOUNTING_UNKNOWN)
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
    assert result["slurm_status"] == runner.SLURM_ACCOUNTING_UNKNOWN
    assert state["status"] == "submitted"
    assert "finished_at" not in state


def test_slurm_submit_deadline_marks_failed_with_finished_at(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="5743\n", stderr="")

    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@10.0.2.100:55432/nhms")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "_wait_for_slurm_job", lambda *_args, **_kwargs: "DEADLINE")
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

    assert result["status"] == "failed"
    assert result["slurm_status"] == "DEADLINE"
    assert state["status"] == "failed"
    assert state["slurm_status"] == "DEADLINE"
    assert state["finished_at"]


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

    assert reason == "slurm job 5743 status is active (RUNNING)"
    assert calls == [["squeue", "-h", "-j", "5743", "-o", "%T"]]


def test_finished_submitted_slurm_state_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "squeue":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="COMPLETED|\n", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    state = {"status": "submitted", "source_id": "gfs", "cycle_time": "2026052106", "slurm_job_id": "5743"}

    assert runner._skip_reason(_candidate(), state, executor="slurm") is None


def test_deadline_submitted_slurm_state_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "_slurm_job_status", lambda _job_id: "DEADLINE")
    state = {"status": "submitted", "source_id": "gfs", "cycle_time": "2026052106", "slurm_job_id": "5743"}

    assert runner._skip_reason(_candidate(), state, executor="slurm") is None


def test_terminal_failed_slurm_state_is_not_skipped_after_unknown_active_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "_slurm_job_status", lambda _job_id: "FAILED")
    state = {
        "status": "submitted",
        "source_id": "gfs",
        "cycle_time": "2026052106",
        "slurm_job_id": "5743",
        "slurm_status": runner.SLURM_ACCOUNTING_UNKNOWN,
    }

    assert runner._skip_reason(_candidate(), state, executor="slurm") is None


def test_unknown_submitted_slurm_state_is_retried_without_controller_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "_slurm_job_status", lambda _job_id: runner.SLURM_ACCOUNTING_UNKNOWN)
    state = {
        "status": "submitted",
        "source_id": "gfs",
        "cycle_time": "2026052106",
        "slurm_job_id": "5743",
        "slurm_status": runner.SLURM_ACCOUNTING_UNKNOWN,
    }

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
    monkeypatch.setattr(runner, "_slurm_job_status", lambda job_id: "RUNNING" if job_id == "5743" else "COMPLETED")
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
    assert summary["results"][0]["reason"] == "slurm job 5743 status is active (RUNNING)"


def test_run_pass_resubmits_unknown_slurm_job_after_controller_loses_visibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_file = runner._state_file(state_root, _candidate())
    runner._write_state(
        state_file,
        {
            "status": "submitted",
            "source_id": "gfs",
            "cycle_time": "2026052106",
            "slurm_job_id": "5743",
            "slurm_status": runner.SLURM_ACCOUNTING_UNKNOWN,
        },
    )
    monkeypatch.setattr(runner, "_candidate_cycles", lambda **_: [_candidate()])
    monkeypatch.setattr(runner, "_slurm_job_status", lambda _job_id: runner.SLURM_ACCOUNTING_UNKNOWN)
    submissions: list[str] = []

    def fake_submit(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        submissions.append("submitted")
        return {
            "source_id": "gfs",
            "cycle_time": "2026052106",
            "run_id": _candidate().run_id,
            "status": "submitted",
            "slurm_job_id": "6000",
        }

    monkeypatch.setattr(runner, "_submit_slurm_cycle", fake_submit)
    args = Namespace(
        sources="gfs",
        lookback_hours=24,
        max_cycles_per_source=1,
        cycle_lag_hours=6,
        dry_run=False,
        executor="slurm",
    )

    summary = runner.run_pass(args=args, run_root=tmp_path, state_root=state_root)

    assert submissions == ["submitted"]
    assert summary["status"] == "completed"
    assert summary["results"][0]["status"] == "submitted"
    assert summary["results"][0]["slurm_job_id"] == "6000"


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
    monkeypatch.setattr(runner, "_slurm_job_status", lambda job_id: "RUNNING" if job_id == "5743" else "COMPLETED")
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
    assert summary["results"][0]["reason"] == "slurm job 5743 status is active (RUNNING)"


def test_run_pass_resubmits_deadline_slurm_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_root = tmp_path / "state"
    state_file = runner._state_file(state_root, _candidate())
    runner._write_state(
        state_file,
        {
            "status": "submitted",
            "source_id": "gfs",
            "cycle_time": "2026052106",
            "slurm_job_id": "5743",
            "slurm_status": runner.SLURM_ACCOUNTING_UNKNOWN,
        },
    )
    submissions: list[str] = []
    monkeypatch.setattr(runner, "_candidate_cycles", lambda **_: [_candidate()])
    monkeypatch.setattr(runner, "_slurm_job_status", lambda _job_id: "DEADLINE")

    def fake_submit(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        submissions.append("submitted")
        return {
            "source_id": "gfs",
            "cycle_time": "2026052106",
            "run_id": _candidate().run_id,
            "status": "submitted",
            "slurm_job_id": "6000",
        }

    monkeypatch.setattr(runner, "_submit_slurm_cycle", fake_submit)
    args = Namespace(
        sources="gfs",
        lookback_hours=24,
        max_cycles_per_source=1,
        cycle_lag_hours=6,
        dry_run=False,
        executor="slurm",
    )

    summary = runner.run_pass(args=args, run_root=tmp_path, state_root=state_root)

    assert submissions == ["submitted"]
    assert summary["results"][0]["slurm_job_id"] == "6000"


def test_local_runner_preserves_typed_probe_failed_state_on_nonzero_cycle_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = runner.CandidateCycle("IFS", datetime(2026, 6, 8, 0, tzinfo=UTC))
    state_file = runner._state_file(tmp_path / "state", candidate)

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert command[-1].endswith("run_qhh_cycle.sh")
        runner._write_state(
            state_file,
            {
                "status": "probe_failed",
                "reason": "source_cycle_probe_failed",
                "classifier": "network_error",
                "retryable": True,
                "source_id": "IFS",
                "cycle_time": "2026060800",
                "run_id": candidate.run_id,
            },
        )
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner._run_cycle(candidate, run_root=tmp_path, state_file=state_file)
    state = runner._read_json(state_file)

    assert result["status"] == "probe_failed"
    assert result["reason"] == "source_cycle_probe_failed"
    assert result["classifier"] == "network_error"
    assert result["retryable"] is True
    assert result["returncode"] == 1
    assert state["status"] == "probe_failed"
    assert state["reason"] == "source_cycle_probe_failed"
    assert state["classifier"] == "network_error"
    assert state["retryable"] is True
    assert state["finished_at"]


@pytest.mark.parametrize(
    ("status", "reason", "classifier"),
    [
        ("probe_failed", "source_cycle_probe_failed", "network_error"),
        ("rate_limited", "source_cycle_rate_limited", "rate_limited"),
    ],
)
def test_run_qhh_cycle_exits_zero_and_preserves_typed_ifs_download_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    reason: str,
    classifier: str,
) -> None:
    run_root = tmp_path / "run-root"
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    uv_stub = stub_bin / "uv"
    command_log = tmp_path / "uv-commands.jsonl"
    cycle_token = "2026060800"
    run_id = "fcst_ifs_2026060800_basins_qhh_shud"
    uv_stub.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "args = sys.argv[1:]\n"
        "log_path = Path(os.environ['UV_STUB_COMMAND_LOG'])\n"
        "with log_path.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(args) + '\\n')\n"
        "if args[:2] != ['run', 'python']:\n"
        "    if args[:3] == ['run', 'nhms-model', 'discover-basins']:\n"
        "        output = Path(args[args.index('--output') + 1])\n"
        "        output.parent.mkdir(parents=True, exist_ok=True)\n"
        "        output.write_text('{\"basins\": []}\\n', encoding='utf-8')\n"
        "        print('{\"status\":\"ok\",\"command\":\"discover-basins\"}')\n"
        "        raise SystemExit(0)\n"
        "    if args[:3] == ['run', 'nhms-model', 'publish-basins']:\n"
        "        output = Path(args[args.index('--output') + 1])\n"
        "        output.parent.mkdir(parents=True, exist_ok=True)\n"
        "        output.write_text(\n"
        "            '{\"model_package_uri\":\"s3://nhms/qhh/package\",\"package_checksum\":\"stub\"}\\n',\n"
        "            encoding='utf-8',\n"
        "        )\n"
        "        print('{\"status\":\"ok\",\"command\":\"publish-basins\"}')\n"
        "        raise SystemExit(0)\n"
        "    if args[:3] == ['run', 'nhms-model', 'import-basins-registry']:\n"
        "        output = Path(args[args.index('--output') + 1])\n"
        "        output.parent.mkdir(parents=True, exist_ok=True)\n"
        "        output.write_text('{\"status\":\"ok\"}\\n', encoding='utf-8')\n"
        "        print('{\"status\":\"ok\",\"command\":\"import-basins-registry\"}')\n"
        "        raise SystemExit(0)\n"
        "    if args[:3] == ['run', 'nhms-ifs', 'download']:\n"
        "        print(json.dumps({\n"
        "            'status': os.environ['IFS_DOWNLOAD_STATUS'],\n"
        "            'reason': os.environ['IFS_DOWNLOAD_REASON'],\n"
        "            'classifier': os.environ['IFS_DOWNLOAD_CLASSIFIER'],\n"
        "            'retryable': True,\n"
        "            'source_id': 'IFS',\n"
        "            'cycle_time': os.environ['QHH_CYCLE_TIME'],\n"
        "            'files': 0,\n"
        "            'total_bytes_written': 0,\n"
        "        }, sort_keys=True))\n"
        "        raise SystemExit(9)\n"
        "    print(json.dumps({'status': 'unexpected_command', 'args': args}), file=sys.stderr)\n"
        "    raise SystemExit(17)\n"
        "\n"
        "script_index = 2\n"
        "script_args = args[script_index:]\n"
        "if script_args and script_args[0] == '-':\n"
        "    code = sys.stdin.read()\n"
        "    if 'normalize_source_id' in code:\n"
        "        source = script_args[1]\n"
        "        print('IFS' if source.lower() == 'ifs' else source.lower())\n"
        "        raise SystemExit(0)\n"
        "    if 'print(sys.argv[1].lower())' in code:\n"
        "        print(script_args[1].lower())\n"
        "        raise SystemExit(0)\n"
        "    if 'psycopg2.connect' in code:\n"
        "        if 'SELECT status FROM hydro.hydro_run' in code:\n"
        "            print('')\n"
        "        elif 'SELECT model_package_uri, resource_profile' in code:\n"
        "            print('1')\n"
        "        elif 'SELECT 1 FROM met.canonical_met_product' in code:\n"
        "            print('0')\n"
        "        elif 'SELECT 1 FROM met.forcing_version' in code:\n"
        "            print('0')\n"
        "        raise SystemExit(0)\n"
        "    raise SystemExit(subprocess.run([sys.executable, *script_args], input=code, text=True).returncode)\n"
        "script = script_args[0]\n"
        "if script.endswith('apply_smoke_migrations.py'):\n"
        "    print('{\"status\":\"ok\",\"command\":\"apply_smoke_migrations\"}')\n"
        "elif script.endswith('seed_qhh_forcing_stations.py'):\n"
        "    print('{\"status\":\"ok\",\"command\":\"seed_qhh_forcing_stations\"}')\n"
        "elif script.endswith('seed_qhh_shud_output_segments.py'):\n"
        "    print('{\"status\":\"ok\",\"command\":\"seed_qhh_shud_output_segments\"}')\n"
        "else:\n"
        "    print(json.dumps({'status': 'unexpected_python_script', 'args': args}), file=sys.stderr)\n"
        "    raise SystemExit(18)\n"
        "\n",
        encoding="utf-8",
    )
    uv_stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("UV_STUB_COMMAND_LOG", str(command_log))
    monkeypatch.setenv("IFS_DOWNLOAD_STATUS", status)
    monkeypatch.setenv("IFS_DOWNLOAD_REASON", reason)
    monkeypatch.setenv("IFS_DOWNLOAD_CLASSIFIER", classifier)

    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": "postgresql://nhms:secret@db.example/nhms",
            "NHMS_BASINS_ROOT": str(tmp_path / "Basins"),
            "OBJECT_STORE_PREFIX": "s3://nhms",
            "OBJECT_STORE_ROOT": str(run_root / "object-store"),
            "QHH_AUTO_START_PG": "0",
            "QHH_CYCLE_TIME": cycle_token,
            "QHH_IFS_FORECAST_END_HOUR": "9",
            "QHH_IFS_FORECAST_START_HOUR": "3",
            "QHH_MODEL_OUTPUT_INTERVAL": "10",
            "QHH_RUN_ID": run_id,
            "QHH_RUN_ROOT": str(run_root),
            "QHH_SKIP_COMPLETED": "1",
            "QHH_SOURCE_ID": "IFS",
            "QHH_USE_SMOKE_MIGRATIONS": "1",
        }
    )

    completed = subprocess.run(
        [str(runner.ROOT / "scripts" / "run_qhh_cycle.sh")],
        cwd=runner.ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    state_file = run_root / "state" / "cycles" / "ifs" / f"{cycle_token}.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    commands = [
        json.loads(line)
        for line in command_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert completed.returncode == 0, completed.stderr
    assert state["status"] == status
    assert state["reason"] == reason
    assert state["classifier"] == classifier
    assert state["retryable"] == "true"
    assert state["source_id"] == "IFS"
    assert state["cycle_time"] == cycle_token
    assert state["run_id"] == run_id
    assert any(command[:3] == ["run", "nhms-ifs", "download"] for command in commands)
    assert not any(command[:2] == ["run", "nhms-canonical"] for command in commands)
    assert not any(command[:2] == ["run", "nhms-forcing"] for command in commands)
    assert not any(command[:2] == ["run", "nhms-shud-runtime"] for command in commands)
    assert not any(command[:2] == ["run", "nhms-parse"] for command in commands)
    assert "downstream stages skipped" in completed.stdout


def test_probe_failed_state_is_known_retryable_and_not_skipped_when_retry_enabled() -> None:
    state = {
        "status": "probe_failed",
        "reason": "source_cycle_probe_failed",
        "classifier": "network_error",
        "retryable": True,
        "source_id": "gfs",
        "cycle_time": "2026052106",
    }

    assert runner._skip_reason(_candidate(), state, executor="local") is None
