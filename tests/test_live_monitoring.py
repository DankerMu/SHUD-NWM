from __future__ import annotations

import builtins
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator.monitoring import MonitorConfig, _alerts, _scheduler_summary, run_monitor

REPO_ROOT = Path(__file__).resolve().parents[1]

# The requirement "The live monitor raises a fixed set of alerts with a runbook
# each" (production-ops-readiness): one row per alert, values copied from its
# table rather than from the module so a drift in the code is caught here.
# (alert, severity, threshold present, runbook)
_ALERT_TABLE = {
    "failed_cycles": ("critical", True, "docs/runbooks/failed-basin-retry.md"),
    "slurm_monitor_unavailable": ("warning", False, "docs/runbooks/slurm-backlog.md"),
    "slurm_queue_backlog": ("warning", True, "docs/runbooks/slurm-backlog.md"),
    "scheduler_evidence_missing": ("critical", False, "docs/runbooks/source-latency.md"),
    "scheduler_stale": ("warning", True, "docs/runbooks/source-latency.md"),
}

_MAX_FAILED_CYCLES = 1
_MAX_ACTIVE_SLURM_JOBS = 4
_MAX_STALE_MINUTES = 20


def _limits_config(tmp_path: Path) -> MonitorConfig:
    return MonitorConfig(
        workspace_root=tmp_path,
        database_url="postgresql://example.invalid/nhms",
        lookback_hours=168,
        max_active_slurm_jobs=_MAX_ACTIVE_SLURM_JOBS,
        max_failed_cycles=_MAX_FAILED_CYCLES,
        max_stale_minutes=_MAX_STALE_MINUTES,
        output_dir=tmp_path / "monitoring",
    )


def _quiet_summaries() -> dict[str, dict[str, Any]]:
    # Every value sits exactly on its limit: "exceed" is strict, so none fires.
    return {
        "cycles": {"failed_cycles": [{"cycle_id": f"gfs_20260601{hour:02d}"} for hour in range(_MAX_FAILED_CYCLES)]},
        "slurm": {"status": "ok", "active_jobs": [], "active_count": _MAX_ACTIVE_SLURM_JOBS},
        "scheduler": {"status": "ok", "latest_status": "submitted", "latest_age_minutes": float(_MAX_STALE_MINUTES)},
    }


def _summaries_for(alert: str) -> dict[str, dict[str, Any]]:
    summaries = _quiet_summaries()
    if alert == "failed_cycles":
        summaries["cycles"] = {
            "failed_cycles": [{"cycle_id": f"gfs_20260601{hour:02d}"} for hour in range(_MAX_FAILED_CYCLES + 1)]
        }
    elif alert == "slurm_monitor_unavailable":
        summaries["slurm"] = {
            "status": "unavailable",
            "error": "squeue: command not found",
            "active_jobs": [],
            "active_count": 0,
        }
    elif alert == "slurm_queue_backlog":
        summaries["slurm"] = {"status": "ok", "active_jobs": [], "active_count": _MAX_ACTIVE_SLURM_JOBS + 1}
    elif alert == "scheduler_evidence_missing":
        summaries["scheduler"] = {"status": "missing", "latest_artifact": None}
    elif alert == "scheduler_stale":
        summaries["scheduler"] = {
            "status": "ok",
            "latest_status": "submitted",
            "latest_age_minutes": _MAX_STALE_MINUTES + 0.5,
        }
    else:  # pragma: no cover - the parametrisation only passes table rows
        raise AssertionError(alert)
    return summaries


def test_quiet_system_raises_no_alert(tmp_path: Path) -> None:
    assert _alerts(_limits_config(tmp_path), **_quiet_summaries()) == []


@pytest.mark.parametrize("alert_name", list(_ALERT_TABLE))
def test_each_condition_raises_exactly_its_alert_as_the_table_says(tmp_path: Path, alert_name: str) -> None:
    severity, has_threshold, runbook = _ALERT_TABLE[alert_name]

    alerts = _alerts(_limits_config(tmp_path), **_summaries_for(alert_name))

    assert [alert["alert"] for alert in alerts] == [alert_name]
    (alert,) = alerts
    expected_keys = {"alert", "severity", "observed", "runbook"} | ({"threshold"} if has_threshold else set())
    assert set(alert) == expected_keys
    assert alert["severity"] == severity
    assert alert["runbook"] == runbook


def test_unreadable_queue_is_not_reported_as_backlog(tmp_path: Path) -> None:
    summaries = _quiet_summaries()
    summaries["slurm"] = {
        "status": "unavailable",
        "error": "squeue exited 1",
        "active_jobs": [],
        "active_count": _MAX_ACTIVE_SLURM_JOBS * 100,
    }

    alerts = _alerts(_limits_config(tmp_path), **summaries)

    assert [alert["alert"] for alert in alerts] == ["slurm_monitor_unavailable"]


def test_scheduler_summary_not_ok_is_missing_evidence_even_with_an_old_age(tmp_path: Path) -> None:
    summaries = _quiet_summaries()
    summaries["scheduler"] = {"status": "missing", "latest_artifact": None, "latest_age_minutes": 10_000.0}

    alerts = _alerts(_limits_config(tmp_path), **summaries)

    assert [alert["alert"] for alert in alerts] == ["scheduler_evidence_missing"]


def test_every_linked_runbook_exists(tmp_path: Path) -> None:
    runbooks: dict[str, str] = {}
    for alert_name in _ALERT_TABLE:
        for alert in _alerts(_limits_config(tmp_path), **_summaries_for(alert_name)):
            runbooks[alert["alert"]] = alert["runbook"]

    assert set(runbooks) == set(_ALERT_TABLE)
    for alert_name, runbook in runbooks.items():
        assert (REPO_ROOT / runbook).is_file(), (alert_name, runbook)


def test_only_scheduler_json_files_count_as_scheduler_evidence(tmp_path: Path) -> None:
    config = _limits_config(tmp_path)
    evidence_dir = tmp_path / "scheduler" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "monitor_2026060700.json").write_text('{"status":"submitted"}', encoding="utf-8")
    (evidence_dir / "scheduler_2026060700.txt").write_text('{"status":"submitted"}', encoding="utf-8")
    (tmp_path / "scheduler" / "scheduler_2026060700.json").write_text('{"status":"submitted"}', encoding="utf-8")
    now = datetime.now(UTC)

    scheduler = _scheduler_summary(config, now)

    assert scheduler["status"] == "missing"
    summaries = _quiet_summaries()
    summaries["scheduler"] = scheduler
    assert [alert["alert"] for alert in _alerts(config, **summaries)] == ["scheduler_evidence_missing"]

    (evidence_dir / "scheduler_2026060700.json").write_text('{"status":"submitted"}', encoding="utf-8")
    summaries["scheduler"] = _scheduler_summary(config, now)
    assert summaries["scheduler"]["status"] == "ok"
    assert _alerts(config, **summaries) == []


@pytest.mark.parametrize("content", ['{"status":"ok"}', '{"status":"failed"}', "not json"])
def test_old_scheduler_evidence_is_stale_whatever_it_says(tmp_path: Path, content: str) -> None:
    config = _limits_config(tmp_path)
    evidence_dir = tmp_path / "scheduler" / "evidence"
    evidence_dir.mkdir(parents=True)
    evidence = evidence_dir / "scheduler_2026060700.json"
    evidence.write_text(content, encoding="utf-8")
    written = datetime(2026, 6, 7, 0, 0, tzinfo=UTC).timestamp()
    os.utime(evidence, (written, written))
    now = datetime(2026, 6, 7, 0, _MAX_STALE_MINUTES + 1, tzinfo=UTC)

    summaries = _quiet_summaries()
    summaries["scheduler"] = _scheduler_summary(config, now)

    alerts = _alerts(config, **summaries)
    assert [alert["alert"] for alert in alerts] == ["scheduler_stale"]
    assert alerts[0]["observed"] == pytest.approx(_MAX_STALE_MINUTES + 1)
    assert alerts[0]["threshold"] == _MAX_STALE_MINUTES


def test_live_monitoring_alerts_on_failed_cycle_and_stale_scheduler(tmp_path: Path) -> None:
    config = MonitorConfig(
        workspace_root=tmp_path,
        database_url="postgresql://example.invalid/nhms",
        lookback_hours=168,
        max_active_slurm_jobs=1,
        max_failed_cycles=0,
        max_stale_minutes=20,
        output_dir=tmp_path / "monitoring",
    )

    alerts = _alerts(
        config,
        cycles={"failed_cycles": [{"cycle_id": "gfs_2026053100"}]},
        slurm={"status": "ok", "active_count": 2},
        scheduler={"status": "ok", "latest_age_minutes": 21.5},
    )

    assert [alert["alert"] for alert in alerts] == [
        "failed_cycles",
        "slurm_queue_backlog",
        "scheduler_stale",
    ]


def test_run_monitor_writes_status_files(monkeypatch, tmp_path: Path) -> None:
    config = MonitorConfig(
        workspace_root=tmp_path,
        database_url="postgresql://example.invalid/nhms",
        lookback_hours=168,
        max_active_slurm_jobs=32,
        max_failed_cycles=0,
        max_stale_minutes=20,
        output_dir=tmp_path / "monitoring",
    )
    evidence_dir = tmp_path / "scheduler" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "scheduler_2026060700_test.json").write_text('{"status":"submitted"}', encoding="utf-8")
    monkeypatch.setattr(
        "services.orchestrator.monitoring._cycle_summary",
        lambda _config, _now: {"counts": {"complete": 1, "incomplete": 0, "total": 1}, "failed_cycles": []},
    )
    monkeypatch.setattr(
        "services.orchestrator.monitoring._slurm_summary",
        lambda: {"status": "ok", "active_count": 0, "active_jobs": []},
    )
    monkeypatch.setattr("services.orchestrator.monitoring.datetime", _FixedDatetime)

    payload = run_monitor(config)

    assert payload["status"] == "ok"
    status_path = config.output_dir / "monitoring_status.json"
    alerts_path = config.output_dir / "monitoring_alerts.json"
    assert json.loads(status_path.read_text(encoding="utf-8"))["schema"] == "nhms.live_monitoring.v1"
    assert json.loads(alerts_path.read_text(encoding="utf-8"))["alerts"] == []


def test_scheduler_summary_selects_latest_without_sorting(monkeypatch, tmp_path: Path) -> None:
    config = MonitorConfig(
        workspace_root=tmp_path,
        database_url="postgresql://example.invalid/nhms",
        lookback_hours=168,
        max_active_slurm_jobs=32,
        max_failed_cycles=0,
        max_stale_minutes=20,
        output_dir=tmp_path / "monitoring",
    )
    evidence_dir = tmp_path / "scheduler" / "evidence"
    evidence_dir.mkdir(parents=True)
    old = evidence_dir / "scheduler_2026060700_old.json"
    latest = evidence_dir / "scheduler_2026060700_latest.json"
    old.write_text('{"status":"old"}', encoding="utf-8")
    latest.write_text('{"status":"submitted"}', encoding="utf-8")
    os.utime(old, (1, 1))
    os.utime(latest, (2, 2))

    def fail_sorted(*_args, **_kwargs):
        raise AssertionError("scheduler summary must not sort every evidence file")

    monkeypatch.setattr(builtins, "sorted", fail_sorted)

    summary = _scheduler_summary(config, datetime(2026, 6, 7, tzinfo=UTC))

    assert summary["latest_artifact"] == str(latest)
    assert summary["latest_status"] == "submitted"


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return datetime(2026, 6, 7, 0, 0, tzinfo=tz or UTC)
