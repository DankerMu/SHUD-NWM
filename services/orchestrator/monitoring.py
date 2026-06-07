from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

DEFAULT_LOOKBACK_HOURS = 168
DEFAULT_MAX_ACTIVE_SLURM_JOBS = 32
DEFAULT_MAX_FAILED_CYCLES = 0
DEFAULT_MAX_STALE_MINUTES = 20


@dataclass(frozen=True)
class MonitorConfig:
    workspace_root: Path
    database_url: str
    lookback_hours: int
    max_active_slurm_jobs: int
    max_failed_cycles: int
    max_stale_minutes: int
    output_dir: Path

    @classmethod
    def from_env(cls, *, output_dir: Path | None = None) -> MonitorConfig:
        workspace_root = Path(os.getenv("WORKSPACE_ROOT", ".nhms-workspace")).expanduser()
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise ValueError("DATABASE_URL is required for live monitoring.")
        resolved_output_dir = output_dir or Path(
            os.getenv("NHMS_MONITORING_OUTPUT_DIR", str(workspace_root / "monitoring"))
        )
        return cls(
            workspace_root=workspace_root,
            database_url=database_url,
            lookback_hours=_env_int("NHMS_MONITORING_LOOKBACK_HOURS", DEFAULT_LOOKBACK_HOURS),
            max_active_slurm_jobs=_env_int("NHMS_MONITORING_MAX_ACTIVE_SLURM_JOBS", DEFAULT_MAX_ACTIVE_SLURM_JOBS),
            max_failed_cycles=_env_int("NHMS_MONITORING_MAX_FAILED_CYCLES", DEFAULT_MAX_FAILED_CYCLES),
            max_stale_minutes=_env_int("NHMS_MONITORING_MAX_STALE_MINUTES", DEFAULT_MAX_STALE_MINUTES),
            output_dir=resolved_output_dir.expanduser(),
        )


def run_monitor(config: MonitorConfig) -> dict[str, Any]:
    now = datetime.now(UTC)
    cycles = _cycle_summary(config, now)
    slurm = _slurm_summary()
    scheduler = _scheduler_summary(config, now)
    alerts = _alerts(config, cycles=cycles, slurm=slurm, scheduler=scheduler)
    status = "ok" if not alerts else "warning"
    payload = {
        "schema": "nhms.live_monitoring.v1",
        "generated_at": now.isoformat(),
        "status": status,
        "workspace_root": str(config.workspace_root),
        "lookback_hours": config.lookback_hours,
        "scheduler": scheduler,
        "slurm": slurm,
        "cycles": cycles,
        "alerts": alerts,
    }
    config.output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(config.output_dir / "monitoring_status.json", payload)
    _write_json(
        config.output_dir / "monitoring_alerts.json",
        {
            "schema": "nhms.live_monitoring.alerts.v1",
            "generated_at": payload["generated_at"],
            "status": status,
            "alerts": alerts,
        },
    )
    return payload


def _cycle_summary(config: MonitorConfig, now: datetime) -> dict[str, Any]:
    cutoff = now - timedelta(hours=config.lookback_hours)
    with psycopg2.connect(config.database_url) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
              cycle_id,
              source_id,
              cycle_time,
              status,
              error_code,
              left(coalesce(error_message, ''), 240) AS error_message
            FROM met.forecast_cycle
            WHERE cycle_time >= %s
              AND status::text LIKE 'failed%%'
            ORDER BY cycle_time, source_id
            LIMIT 50
            """,
            (cutoff,),
        )
        failed_cycles = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT
              count(*) FILTER (WHERE status = 'complete') AS complete,
              count(*) FILTER (WHERE status <> 'complete') AS incomplete,
              count(*) AS total
            FROM met.forecast_cycle
            WHERE cycle_time >= %s
            """,
            (cutoff,),
        )
        counts = dict(cur.fetchone() or {})
    return {
        "cutoff": cutoff.isoformat(),
        "counts": {key: int(value or 0) for key, value in counts.items()},
        "failed_cycles": [_json_ready(row) for row in failed_cycles],
    }


def _slurm_summary() -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["squeue", "-u", os.getenv("USER", ""), "-h", "-o", "%i|%j|%T|%M|%R"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
    except Exception as exc:
        return {"status": "unavailable", "error": str(exc), "active_jobs": [], "active_count": 0}
    if completed.returncode != 0:
        return {
            "status": "unavailable",
            "error": completed.stderr.strip() or f"squeue exited {completed.returncode}",
            "active_jobs": [],
            "active_count": 0,
        }
    jobs = []
    for line in completed.stdout.splitlines():
        parts = line.split("|", maxsplit=4)
        if len(parts) != 5:
            continue
        jobs.append(
            {
                "job_id": parts[0],
                "name": parts[1],
                "state": parts[2],
                "elapsed": parts[3],
                "reason": parts[4],
            }
        )
    return {"status": "ok", "active_jobs": jobs, "active_count": len(jobs)}


def _scheduler_summary(config: MonitorConfig, now: datetime) -> dict[str, Any]:
    evidence_dir = config.workspace_root / "scheduler" / "evidence"
    files = sorted(evidence_dir.glob("scheduler_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        return {"status": "missing", "latest_artifact": None}
    latest = files[0]
    mtime = datetime.fromtimestamp(latest.stat().st_mtime, tz=UTC)
    age_minutes = max((now - mtime).total_seconds() / 60.0, 0.0)
    latest_status = None
    try:
        with latest.open("r", encoding="utf-8") as fh:
            latest_status = json.load(fh).get("status")
    except Exception:
        latest_status = "unreadable"
    return {
        "status": "ok",
        "latest_artifact": str(latest),
        "latest_status": latest_status,
        "latest_mtime": mtime.isoformat(),
        "latest_age_minutes": round(age_minutes, 3),
    }


def _alerts(
    config: MonitorConfig,
    *,
    cycles: dict[str, Any],
    slurm: dict[str, Any],
    scheduler: dict[str, Any],
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    failed_count = len(cycles.get("failed_cycles", []))
    if failed_count > config.max_failed_cycles:
        alerts.append(
            {
                "alert": "failed_cycles",
                "severity": "critical",
                "observed": failed_count,
                "threshold": config.max_failed_cycles,
                "runbook": "docs/runbooks/failed-basin-retry.md",
            }
        )
    active_count = int(slurm.get("active_count") or 0)
    if slurm.get("status") != "ok":
        alerts.append(
            {
                "alert": "slurm_monitor_unavailable",
                "severity": "warning",
                "observed": slurm.get("error"),
                "runbook": "docs/runbooks/slurm-backlog.md",
            }
        )
    elif active_count > config.max_active_slurm_jobs:
        alerts.append(
            {
                "alert": "slurm_queue_backlog",
                "severity": "warning",
                "observed": active_count,
                "threshold": config.max_active_slurm_jobs,
                "runbook": "docs/runbooks/slurm-backlog.md",
            }
        )
    age = scheduler.get("latest_age_minutes")
    if scheduler.get("status") != "ok":
        alerts.append(
            {
                "alert": "scheduler_evidence_missing",
                "severity": "critical",
                "observed": scheduler.get("status"),
                "runbook": "docs/runbooks/source-latency.md",
            }
        )
    elif isinstance(age, int | float) and age > config.max_stale_minutes:
        alerts.append(
            {
                "alert": "scheduler_stale",
                "severity": "warning",
                "observed": age,
                "threshold": config.max_stale_minutes,
                "runbook": "docs/runbooks/source-latency.md",
            }
        )
    return alerts


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return int(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-monitor")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    config = MonitorConfig.from_env(output_dir=args.output_dir)
    print(json.dumps(run_monitor(config), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
