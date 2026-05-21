from __future__ import annotations

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
    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@10.0.2.100:55432/nhms")

    exports = runner._slurm_exports(_candidate(), tmp_path)
    formatted = runner._format_slurm_export(exports)

    assert "QHH_CONTINUOUS_SOURCES" not in exports
    assert "QHH_UNSAFE_LIST" not in exports
    assert exports["QHH_MAX_LEAD_HOURS"] == "144"
    assert exports["QHH_SOURCE_ID"] == "gfs"
    assert exports["QHH_CYCLE_TIME"] == "2026052106"
    assert ",QHH_CONTINUOUS_SOURCES=" not in formatted


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
