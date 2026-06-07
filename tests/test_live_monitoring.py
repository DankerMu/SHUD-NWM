from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from services.orchestrator.monitoring import MonitorConfig, _alerts, run_monitor


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


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return datetime(2026, 6, 7, 0, 0, tzinfo=tz or UTC)
