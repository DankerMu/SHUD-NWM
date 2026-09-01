"""Requirement tests for node-22 journal-cycle cold retention.

The fixtures are intentionally small while the discovery bound is injectable;
this exercises boundary semantics without synthesising a production-sized tree.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from services.orchestrator import scheduler_journal_archive as archive
from services.orchestrator import scheduler_journal_retention as retention
from services.orchestrator.file_orchestration_journal import (
    FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
    FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
)
from workers.data_adapters.base import cycle_id_for

FrontierReadResult = retention.FrontierReadResult
SchedulerJournalRetentionConfig = retention.SchedulerJournalRetentionConfig
ReceiptReservation = retention.ReceiptReservation
reserve_receipt = retention.reserve_receipt

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
OLD_CYCLE = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
NEW_CYCLE = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)

# node-22 production requires GNU tar; macOS CI fixtures use BSD tar only to
# exercise archive publication and verification without claiming node-22 proof.
os.environ.setdefault("NHMS_JOURNAL_RETENTION_TEST_ALLOW_BSD_TAR", "true")


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records), encoding="utf-8")
    return path


def _job(
    source_id: str,
    cycle: datetime,
    *,
    status: str = "succeeded",
    model_id: str = "model_a",
    accepted: bool = False,
    projections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    stamp = cycle.strftime("%Y%m%d%H")
    job = {
        "job_id": f"job_fcst_{source_id.lower()}_{stamp}_{model_id}_forecast",
        "run_id": f"fcst_{source_id.lower()}_{stamp}_{model_id}",
        "cycle_id": cycle_id_for(source_id, cycle),
        "source_id": source_id,
        "cycle_time": cycle.isoformat(),
        "model_id": model_id,
        "job_type": "run_shud_forecast_array",
        "stage": "forecast",
        "status": status,
        "slurm_job_id": None if status in {"reserved", "reservation_lost"} else "4001",
        "idempotency_key": f"{source_id}:{stamp}:{model_id}:forecast",
        "created_at": "2026-05-01T00:00:00Z",
    }
    if accepted:
        # The production accepted-submit schema is much stricter than the
        # retention predicate.  The fixtures intentionally omit its version
        # marker; the owner still classifies their structural master shape and
        # retention must preserve that old authority instead of deleting it.
        job.update(
            {
                "cohort_members": [{"array_task_id": 0, "model_id": model_id}],
                "candidate_projections": projections or [],
                "reconciliation_decision": "matched_bound",
                "reconciliation_source": "slurm_exact_comment",
                "matched_slurm_job_id": "4001",
                "slurm_comment": "nhms-test",
            }
        )
    return job


def _record(
    source_id: str,
    cycle: datetime,
    payload: dict[str, Any],
    *,
    record_type: str = "pipeline_job",
) -> dict[str, Any]:
    return {
        "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
        "sequence": 1,
        "record_type": record_type,
        "source_id": source_id,
        "cycle_time": cycle.isoformat(),
        "model_id": payload.get("model_id"),
        "payload": payload,
    }


def _latest(source_id: str, cycle: datetime, job: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
        "generated_at": "2026-05-01T00:00:00Z",
        "source_id": source_id,
        "cycle_time": cycle.isoformat(),
        "model_id": job["model_id"],
        "pipeline_jobs": [job],
        "pipeline_events": [],
        "replay": {"latest_sequence": 1},
    }


def _seed_cycle(
    journal_root: Path,
    *,
    source_id: str = "gfs",
    cycle: datetime = OLD_CYCLE,
    job: dict[str, Any] | None = None,
    continuation: bool = False,
    pipeline_event: bool = False,
) -> list[Path]:
    job = job or _job(source_id, cycle)
    source_segment = source_id
    stamp = cycle.strftime("%Y%m%d%H")
    paths = [
        _write_json(
            journal_root / "latest" / source_segment / stamp / f"{job['model_id']}.json",
            _latest(source_id, cycle, job),
        ),
        _write_jsonl(
            journal_root / "journal" / source_segment / f"{stamp}.jsonl",
            [_record(source_id, cycle, job)],
        ),
    ]
    if continuation:
        paths.append(_write_jsonl(journal_root / "journal" / source_segment / f"{stamp}.1.jsonl", []))
    if pipeline_event:
        event = {
            "event_id": 1,
            "entity_type": "pipeline_job",
            "entity_id": job["job_id"],
            "event_type": "terminal",
            "created_at": "2026-05-01T00:01:00Z",
        }
        paths.append(
            _write_jsonl(
                journal_root / "pipeline-events" / source_segment / f"{stamp}.jsonl",
                [_record(source_id, cycle, event, record_type="pipeline_event")],
            )
        )
    return paths


def _frontier(bound: datetime | None = datetime(2026, 6, 1, tzinfo=UTC)) -> FrontierReadResult:
    return FrontierReadResult(
        status="ok",
        active_lower_bound=bound,
        source="receipt:scheduler_pass",
        receipt_path="/receipts/pass.json",
        receipt_started_at=NOW,
    )


def _config(
    root: Path,
    *,
    enabled: bool = False,
    dry_run: bool = True,
    max_files: int = 100,
) -> SchedulerJournalRetentionConfig:
    # safe_fs deliberately rejects macOS /var -> /private symlink ancestry;
    # use a symlink-free fixture root to exercise the same no-follow contract.
    if str(root).startswith("/var/"):
        root = Path("/private" + str(root))
    journal = root / "journal"
    archive = root / "archive"
    evidence = root / "evidence"
    journal.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    return SchedulerJournalRetentionConfig(
        journal_root=journal,
        archive_root=archive,
        evidence_root=evidence,
        allowed_roots=(root,),
        retention_days=90,
        enabled=enabled,
        dry_run=dry_run,
        lookback_hours=96,
        cycle_lag_hours=16,
        allowed_cycle_hours=(0, 12),
        max_files=max_files,
        max_depth=32,
    )


def _bytes(path: Path) -> bytes:
    return path.read_bytes()


def _fixture_path(config: SchedulerJournalRetentionConfig, path: Path) -> Path:
    """Map pytest's /private tmp_path spelling to the physical fixture root."""

    return config.journal_root.parent / path.relative_to(config.journal_root.parent)


def _reservation(config: SchedulerJournalRetentionConfig) -> ReceiptReservation:
    """Create the durable intent capability required at the destructive seam."""

    return reserve_receipt(config, now=NOW)


def _manifest_path(config: SchedulerJournalRetentionConfig, *, source_id: str = "gfs") -> Path:
    return config.archive_root / source_id / "2026050100" / "bundle" / archive.MANIFEST_NAME


def _archive_path(config: SchedulerJournalRetentionConfig, *, source_id: str = "gfs") -> Path:
    return _manifest_path(config, source_id=source_id).with_name(archive.ARCHIVE_NAME)


def _direct_record(job: dict[str, Any]) -> dict[str, Any]:
    return _record(str(job["source_id"]), datetime.fromisoformat(str(job["cycle_time"])), job)


def _later_frontier() -> FrontierReadResult:
    return FrontierReadResult(
        status="ok",
        active_lower_bound=datetime(2026, 7, 1, tzinfo=UTC),
        source="receipt:later_pass",
        receipt_path="/receipts/later-pass.json",
        receipt_started_at=NOW + timedelta(hours=1),
    )


def _cycle_lock_path(journal_root: Path, *, source_id: str = "gfs", stamp: str = "2026050100") -> Path:
    return journal_root / ".locks" / source_id / f"{stamp}.lock"


def _install_cli_env(
    monkeypatch: Any,
    config: SchedulerJournalRetentionConfig,
    *,
    enabled: bool,
    dry_run: bool,
) -> None:
    for name, value in {
        "NHMS_SCHEDULER_ALLOWED_ROOTS": str(config.journal_root.parent),
        "NHMS_SCHEDULER_JOURNAL_ROOT": str(config.journal_root),
        "NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT": str(config.archive_root),
        "NHMS_SCHEDULER_EVIDENCE_ROOT": str(config.evidence_root),
        "NHMS_SCHEDULER_LOOKBACK_HOURS": "96",
        "NHMS_SCHEDULER_CYCLE_LAG_HOURS": "16",
        "NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC": "0,12",
        "NHMS_SCHEDULER_JOURNAL_RETENTION_ENABLED": "true" if enabled else "false",
        "NHMS_SCHEDULER_JOURNAL_RETENTION_DRY_RUN": "true" if dry_run else "false",
    }.items():
        monkeypatch.setenv(name, value)
