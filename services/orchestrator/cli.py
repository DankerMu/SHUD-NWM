from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Sequence

from services.tile_publisher import PublishError, TilePublisher
from services.tile_publisher.publisher import failure_payload
from workers.data_adapters.base import parse_cycle_time

from .chain import AnalysisOrchestrator, OrchestratorError
from .file_orchestration_journal import FileOrchestrationJournalError
from .file_orchestration_migration import (
    complete_file_journal_rollforward,
    export_scheduler_state_from_postgres,
    launch_file_journal_rollback_writer,
    prepare_file_journal_rollback,
    write_migration_receipt,
)
from .retention import RetentionConfig, run_retention
from .scheduler import MAX_CONTINUOUS_JSON_PASSES, ProductionScheduler, ProductionSchedulerConfig


def _trigger_analysis(*, model_id: str, date_range: str) -> dict[str, object]:
    orchestrator = AnalysisOrchestrator.from_env()
    result = orchestrator.trigger_analysis(model_id=model_id, date_range=date_range)
    return {
        "run_id": result.run_id,
        "cycle_id": result.cycle_id,
        "status": result.status,
        "stages": [
            {
                "stage": stage.stage,
                "job_type": stage.job_type,
                "pipeline_job_id": stage.pipeline_job_id,
                "slurm_job_id": stage.slurm_job_id,
                "status": stage.status,
                "exit_code": stage.exit_code,
                "error_code": stage.error_code,
                "error_message": stage.error_message,
            }
            for stage in result.stages
        ],
    }


def _publish_tiles(*, cycle_id: str) -> dict[str, object]:
    try:
        publisher = TilePublisher.from_env()
        return publisher.publish_cycle(cycle_id).to_dict()
    except PublishError as error:
        if _publish_tiles_should_defer(error):
            return _deferred_compute_control_publish(cycle_id=cycle_id)
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise PublishError("PUBLISH_TILES_FAILED", f"Tile publication failed: {error}") from error


def _publish_tiles_should_defer(error: PublishError) -> bool:
    return error.error_code == "DATABASE_URL_MISSING"


def _deferred_compute_control_publish(*, cycle_id: str) -> dict[str, object]:
    return {
        "artifacts": [],
        "cycle_id": cycle_id,
        "layers": [],
        "lineage": {
            "database_url_configured": False,
            "deferred_to": "node27_autopipeline",
            "reason_code": "NODE22_DB_FREE_PUBLISH_DEFERRED",
            "service_role": "compute_control",
        },
        "status": "deferred_to_node27_ingest",
    }


def _publish_qdown(*, cycle_id: str) -> dict[str, object]:
    try:
        publisher = TilePublisher.from_env()
        return publisher.publish_qdown_cycle(cycle_id).to_dict()
    except PublishError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise PublishError("PUBLISH_QDOWN_FAILED", f"q_down publication failed: {error}") from error


def _run_cleanup(*, retention_days: int | None, dry_run: bool) -> dict[str, object]:
    base = RetentionConfig.from_env()
    config = RetentionConfig(
        enabled=True,
        dry_run=dry_run,
        retention_days=retention_days if retention_days is not None else base.retention_days,
    )
    result = run_retention(
        object_store_root=os.getenv("OBJECT_STORE_ROOT"),
        now=datetime.now(UTC),
        config=config,
        published_artifact_root=os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT"),
    )
    return result.to_dict()


def _prepare_file_journal_rollback(
    *,
    journal_root: str,
    workspace_root: str,
    lock_path: str | None,
    scheduler_lock_backend: str,
    lock_ttl_seconds: int,
    scheduler_state: str,
    active_scheduler_processes: int,
    checked_at: str,
    checked_by: str,
    target_writer_generation: str,
) -> dict[str, object]:
    normalized_checked_at = checked_at[:-1] + "+00:00" if checked_at.endswith("Z") else checked_at
    try:
        checked_at_value = datetime.fromisoformat(normalized_checked_at)
    except ValueError as error:
        raise ValueError("prepare-file-journal-rollback --checked-at must be ISO-8601") from error
    if checked_at_value.tzinfo is None or checked_at_value.utcoffset() is None:
        raise ValueError("prepare-file-journal-rollback --checked-at must include a timezone")
    return prepare_file_journal_rollback(
        journal_root=journal_root,
        workspace_root=workspace_root,
        lock_path=lock_path,
        scheduler_lock_backend=scheduler_lock_backend,
        lock_ttl_seconds=lock_ttl_seconds,
        scheduler_state=scheduler_state,
        active_scheduler_processes=active_scheduler_processes,
        checked_at=checked_at_value,
        checked_by=checked_by,
        target_writer_generation=target_writer_generation,
    )


def _complete_file_journal_rollforward(
    *,
    journal_root: str,
    workspace_root: str,
    preparation_receipt_id: str,
    lock_path: str | None,
    scheduler_lock_backend: str,
    lock_ttl_seconds: int,
) -> dict[str, object]:
    return complete_file_journal_rollforward(
        journal_root=journal_root,
        workspace_root=workspace_root,
        preparation_receipt_id=preparation_receipt_id,
        lock_path=lock_path,
        scheduler_lock_backend=scheduler_lock_backend,
        lock_ttl_seconds=lock_ttl_seconds,
    )


def _launch_file_journal_rollback_writer(
    *,
    journal_root: str,
    workspace_root: str,
    receipt_id: str,
    writer_repository_root: str,
    writer_args: Sequence[str],
    lock_path: str | None,
    scheduler_lock_backend: str,
    lock_ttl_seconds: int,
) -> dict[str, object]:
    return launch_file_journal_rollback_writer(
        journal_root=journal_root,
        workspace_root=workspace_root,
        receipt_id=receipt_id,
        writer_repository_root=writer_repository_root,
        writer_args=writer_args,
        lock_path=lock_path,
        scheduler_lock_backend=scheduler_lock_backend,
        lock_ttl_seconds=lock_ttl_seconds,
    )


def _split_csv(values: Sequence[str] | None) -> tuple[str, ...]:
    result: list[str] = []
    for value in values or ():
        for item in str(value).split(","):
            item = item.strip()
            if item:
                result.append(item)
    return tuple(result)


def _split_env_csv(name: str) -> tuple[str, ...]:
    value = os.getenv(name)
    if value in (None, ""):
        return ()
    return _split_csv((value,))


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return int(str(value))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _env_optional_int(name: str) -> int | None:
    value = os.getenv(name)
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return float(str(value))
    except ValueError as error:
        raise ValueError(f"{name} must be a float") from error


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _non_blank_path(value: str | None, option_name: str) -> str | None:
    if value is not None and value.strip() == "":
        raise ValueError(f"plan-production {option_name} must not be blank")
    return value


def _plan_production(
    *,
    sources: Sequence[str],
    lookback_hours: int | None,
    cycle_lag_hours: int | None,
    max_cycles_per_source: int | None,
    model_ids: Sequence[str],
    basin_ids: Sequence[str],
    dry_run: bool,
    continuous: bool,
    interval_seconds: float | None,
    max_passes: int | None,
    workspace_root: str | None,
    lock_path: str | None,
    evidence_dir: str | None,
    cycle_time: str | None = None,
    disable_backfill: bool = False,
) -> dict[str, object]:
    workspace_root = _non_blank_path(workspace_root, "--workspace-root")
    lock_path = _non_blank_path(lock_path, "--lock-path")
    evidence_dir = _non_blank_path(evidence_dir, "--evidence-dir")
    resolved_interval_seconds = (
        interval_seconds
        if interval_seconds is not None
        else _env_float("NHMS_SCHEDULER_INTERVAL_SECONDS", 300.0)
    )
    resolved_max_passes = max_passes
    if continuous and resolved_max_passes is None:
        resolved_max_passes = _env_optional_int("NHMS_SCHEDULER_MAX_PASSES")
    if continuous:
        if resolved_max_passes is None:
            raise ValueError(
                "plan-production --continuous JSON output requires --max-passes "
                "or NHMS_SCHEDULER_MAX_PASSES"
            )
        if resolved_max_passes < 1:
            raise ValueError("plan-production --continuous max_passes must be at least 1")
        if resolved_max_passes > MAX_CONTINUOUS_JSON_PASSES:
            raise ValueError(
                "plan-production --continuous JSON output max_passes exceeds limit "
                f"{MAX_CONTINUOUS_JSON_PASSES}"
            )
    resolved_workspace_root = workspace_root or os.getenv("WORKSPACE_ROOT")
    if resolved_workspace_root in (None, ""):
        raise ValueError(
            "plan-production requires WORKSPACE_ROOT when --workspace-root is omitted"
        )
    require_runtime_roots = workspace_root is None or not dry_run
    resolved_sources = tuple(sources) if sources else _split_env_csv("NHMS_SCHEDULER_SOURCES") or ("gfs", "IFS")
    resolved_model_ids = tuple(model_ids) if model_ids else _split_env_csv("NHMS_SCHEDULER_MODEL_IDS")
    resolved_basin_ids = tuple(basin_ids) if basin_ids else _split_env_csv("NHMS_SCHEDULER_BASIN_IDS")
    resolved_max_cycles = (
        max_cycles_per_source
        if max_cycles_per_source is not None
        else _env_int("NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE", 1)
    )
    if resolved_max_cycles < 1:
        raise ValueError("plan-production max_cycles_per_source must be at least 1")
    resolved_lookback = (
        lookback_hours
        if lookback_hours is not None
        else _env_int("NHMS_SCHEDULER_LOOKBACK_HOURS", 24)
    )
    resolved_cycle_lag = (
        cycle_lag_hours
        if cycle_lag_hours is not None
        else _env_int("NHMS_SCHEDULER_CYCLE_LAG_HOURS", 0)
    )
    if cycle_time not in (None, ""):
        if lookback_hours is not None or cycle_lag_hours is not None or max_cycles_per_source is not None:
            raise ValueError(
                "plan-production --cycle-time cannot be combined with "
                "--lookback-hours, --cycle-lag-hours, or --max-cycles-per-source"
            )
        target_cycle = parse_cycle_time(str(cycle_time))
        lag_seconds = (datetime.now(UTC) - target_cycle).total_seconds()
        if lag_seconds < 0:
            raise ValueError("plan-production --cycle-time must not be in the future")
        resolved_lookback = 0
        resolved_cycle_lag = int(lag_seconds // 3600)
        resolved_max_cycles = 1
        disable_backfill = True
    config_kwargs: dict[str, object] = {
        "workspace_root": resolved_workspace_root,
        "sources": resolved_sources,
        "lookback_hours": resolved_lookback,
        "cycle_lag_hours": resolved_cycle_lag,
        "max_cycles_per_source": resolved_max_cycles,
        "model_ids": resolved_model_ids,
        "basin_ids": resolved_basin_ids,
        "dry_run": dry_run,
        "continuous": continuous,
        "interval_seconds": resolved_interval_seconds,
        "lock_path": lock_path,
        "evidence_dir": evidence_dir,
        "require_runtime_roots": require_runtime_roots,
    }
    if disable_backfill:
        config_kwargs["backfill_enabled"] = False
    if workspace_root is not None and lock_path is None:
        config_kwargs["scheduler_lock_root"] = None
    if workspace_root is not None and evidence_dir is None:
        config_kwargs["scheduler_evidence_root"] = None
    config = ProductionSchedulerConfig(**config_kwargs)
    scheduler = ProductionScheduler.from_env(config)
    if continuous:
        results = scheduler.run_continuous(max_passes=resolved_max_passes)
        return {
            "status": results[-1].status if results else "not_run",
            "passes": [result.to_dict() for result in results],
        }
    result = scheduler.run_once()
    return result.to_dict()


_SCHEDULER_STDOUT_SUMMARY_SCALAR_KEYS = (
    "pass_id",
    "status",
    "artifact_path",
    "started_at",
    "finished_at",
    "dry_run",
    "continuous",
    "readiness_interpretation",
    "execution_boundary",
    "scheduler_state_backend",
    "scheduler_registry_backend",
    "scheduler_state_index_backend",
    "scheduler_journal_backend",
)


def _scheduler_stdout_payload(payload: Mapping[str, object]) -> dict[str, object]:
    if not _env_bool("NHMS_SCHEDULER_STDOUT_SUMMARY_ONLY"):
        return dict(payload)
    return _scheduler_stdout_summary(payload)


def _scheduler_stdout_summary(payload: Mapping[str, object]) -> dict[str, object]:
    passes = payload.get("passes")
    if isinstance(passes, list):
        return {
            "status": payload.get("status", "unknown"),
            "passes": [
                _scheduler_pass_stdout_summary(item)
                for item in passes
                if isinstance(item, Mapping)
            ],
        }
    return _scheduler_pass_stdout_summary(payload)


def _scheduler_pass_stdout_summary(payload: Mapping[str, object]) -> dict[str, object]:
    summary: dict[str, object] = {
        key: payload[key]
        for key in _SCHEDULER_STDOUT_SUMMARY_SCALAR_KEYS
        if key in payload and _is_json_scalar(payload[key])
    }
    for key, value in payload.items():
        if key.endswith("_count") and _is_json_scalar(value):
            summary[key] = value
    for key in ("sources", "model_ids", "basin_ids", "selected_cycle_ids"):
        value = payload.get(key)
        if _is_small_scalar_list(value):
            summary[key] = value
    if "status" not in summary:
        summary["status"] = "unknown"
    return summary


def _is_json_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _is_small_scalar_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= 32
        and all(_is_json_scalar(item) for item in value)
    )


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("trigger-analysis")
    @click.option("--model-id", required=True)
    @click.option("--date-range", required=True)
    def trigger_analysis(model_id: str, date_range: str) -> None:
        try:
            click.echo(json.dumps(_trigger_analysis(model_id=model_id, date_range=date_range), sort_keys=True))
        except OrchestratorError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error

    @cli.command("publish-tiles")
    @click.option("--cycle-id", required=True)
    def publish_tiles(cycle_id: str) -> None:
        try:
            click.echo(json.dumps(_publish_tiles(cycle_id=cycle_id), sort_keys=True))
        except PublishError as error:
            click.echo(json.dumps(failure_payload(cycle_id, error), sort_keys=True))
            raise SystemExit(1) from error

    @cli.command("publish-qdown")
    @click.option("--cycle-id", required=True)
    def publish_qdown(cycle_id: str) -> None:
        try:
            click.echo(json.dumps(_publish_qdown(cycle_id=cycle_id), sort_keys=True))
        except PublishError as error:
            click.echo(json.dumps(failure_payload(cycle_id, error), sort_keys=True))
            raise SystemExit(1) from error

    @cli.command("cleanup")
    @click.option("--retention-days", default=None, type=int)
    @click.option("--dry-run", "dry_run", flag_value=True, default=True, show_default=True)
    @click.option("--execute", "dry_run", flag_value=False, help="Actually delete aged artifacts.")
    def cleanup(retention_days: int | None, dry_run: bool) -> None:
        try:
            click.echo(
                json.dumps(_run_cleanup(retention_days=retention_days, dry_run=dry_run), sort_keys=True)
            )
        except ValueError as error:
            click.echo(str(error), err=True)
            raise SystemExit(2) from error

    @cli.command("migrate-scheduler-state")
    @click.option("--database-url", required=True)
    @click.option("--journal-root", required=True)
    @click.option("--receipt-path")
    @click.option("--allow-historical-node22", is_flag=True)
    def migrate_scheduler_state(
        database_url: str,
        journal_root: str,
        receipt_path: str | None,
        allow_historical_node22: bool,
    ) -> None:
        try:
            receipt = export_scheduler_state_from_postgres(
                database_url=database_url,
                journal_root=journal_root,
                allow_historical_node22=allow_historical_node22,
            )
            if receipt_path:
                write_migration_receipt(receipt, receipt_path, containment_root=journal_root)
            click.echo(json.dumps(receipt, sort_keys=True))
        except (RuntimeError, ValueError) as error:
            click.echo(str(error), err=True)
            raise SystemExit(2) from error

    @cli.command("prepare-file-journal-rollback")
    @click.option("--journal-root", required=True)
    @click.option("--workspace-root", required=True)
    @click.option("--lock-path")
    @click.option("--scheduler-lock-backend", default="file", show_default=True)
    @click.option("--lock-ttl-seconds", default=60, type=int, show_default=True)
    @click.option("--scheduler-state", required=True, type=click.Choice(["stopped"]))
    @click.option("--active-scheduler-processes", required=True, type=int)
    @click.option("--checked-at", required=True)
    @click.option("--checked-by", required=True)
    @click.option("--target-writer-generation", required=True)
    def prepare_file_journal_rollback_command(
        journal_root: str,
        workspace_root: str,
        lock_path: str | None,
        scheduler_lock_backend: str,
        lock_ttl_seconds: int,
        scheduler_state: str,
        active_scheduler_processes: int,
        checked_at: str,
        checked_by: str,
        target_writer_generation: str,
    ) -> None:
        try:
            receipt = _prepare_file_journal_rollback(
                journal_root=journal_root,
                workspace_root=workspace_root,
                lock_path=lock_path,
                scheduler_lock_backend=scheduler_lock_backend,
                lock_ttl_seconds=lock_ttl_seconds,
                scheduler_state=scheduler_state,
                active_scheduler_processes=active_scheduler_processes,
                checked_at=checked_at,
                checked_by=checked_by,
                target_writer_generation=target_writer_generation,
            )
            click.echo(json.dumps(receipt, sort_keys=True))
        except (FileOrchestrationJournalError, ValueError) as error:
            click.echo(str(error), err=True)
            raise SystemExit(2) from error

    @cli.command("launch-file-journal-rollback-writer")
    @click.option("--journal-root", required=True)
    @click.option("--workspace-root", required=True)
    @click.option("--receipt-id", required=True)
    @click.option("--writer-repository-root", required=True)
    @click.option("--lock-path")
    @click.option("--scheduler-lock-backend", default="file", show_default=True)
    @click.option("--lock-ttl-seconds", default=60, type=int, show_default=True)
    @click.argument("writer_args", nargs=-1, required=True)
    def launch_file_journal_rollback_writer_command(
        journal_root: str,
        workspace_root: str,
        receipt_id: str,
        writer_repository_root: str,
        lock_path: str | None,
        scheduler_lock_backend: str,
        lock_ttl_seconds: int,
        writer_args: tuple[str, ...],
    ) -> None:
        try:
            result = _launch_file_journal_rollback_writer(
                journal_root=journal_root,
                workspace_root=workspace_root,
                receipt_id=receipt_id,
                writer_repository_root=writer_repository_root,
                writer_args=writer_args,
                lock_path=lock_path,
                scheduler_lock_backend=scheduler_lock_backend,
                lock_ttl_seconds=lock_ttl_seconds,
            )
            click.echo(json.dumps(result, sort_keys=True))
            if result["writer_exit_code"] != 0:
                raise SystemExit(int(result["writer_exit_code"]))
        except (FileOrchestrationJournalError, ValueError) as error:
            click.echo(str(error), err=True)
            raise SystemExit(2) from error

    @cli.command("complete-file-journal-rollforward")
    @click.option("--journal-root", required=True)
    @click.option("--workspace-root", required=True)
    @click.option("--preparation-receipt-id", required=True)
    @click.option("--lock-path")
    @click.option("--scheduler-lock-backend", default="file", show_default=True)
    @click.option("--lock-ttl-seconds", default=60, type=int, show_default=True)
    def complete_file_journal_rollforward_command(
        journal_root: str,
        workspace_root: str,
        preparation_receipt_id: str,
        lock_path: str | None,
        scheduler_lock_backend: str,
        lock_ttl_seconds: int,
    ) -> None:
        try:
            receipt = _complete_file_journal_rollforward(
                journal_root=journal_root,
                workspace_root=workspace_root,
                preparation_receipt_id=preparation_receipt_id,
                lock_path=lock_path,
                scheduler_lock_backend=scheduler_lock_backend,
                lock_ttl_seconds=lock_ttl_seconds,
            )
            click.echo(json.dumps(receipt, sort_keys=True))
        except (FileOrchestrationJournalError, ValueError) as error:
            click.echo(str(error), err=True)
            raise SystemExit(2) from error

    @cli.command("plan-production")
    @click.option(
        "--source",
        "sources",
        multiple=True,
        help="Forecast source id. Repeat or pass comma-separated values.",
    )
    @click.option("--lookback-hours", default=None, type=int)
    @click.option("--cycle-lag-hours", default=None, type=int)
    @click.option("--max-cycles-per-source", default=None, type=int)
    @click.option("--cycle-time", default=None, help="Run exactly one UTC source cycle.")
    @click.option("--disable-backfill", is_flag=True, help="Select latest window cycles instead of oldest gaps.")
    @click.option(
        "--model-id",
        "model_ids",
        multiple=True,
        help="Model id filter. Repeat or pass comma-separated values.",
    )
    @click.option(
        "--basin-id",
        "basin_ids",
        multiple=True,
        help="Basin id filter. Repeat or pass comma-separated values.",
    )
    @click.option("--dry-run", "dry_run", flag_value=True, default=True, show_default=True)
    @click.option("--plan", "dry_run", flag_value=True, help="Planning-only alias for --dry-run.")
    @click.option("--submit", "dry_run", flag_value=False, help="Allow production orchestration after preflight.")
    @click.option("--continuous", is_flag=True)
    @click.option("--interval-seconds", default=None, type=float)
    @click.option("--max-passes", type=int)
    @click.option("--workspace-root")
    @click.option("--lock-path")
    @click.option("--evidence-dir")
    def plan_production(
        sources: Sequence[str],
        lookback_hours: int,
        cycle_lag_hours: int | None,
        max_cycles_per_source: int | None,
        cycle_time: str | None,
        disable_backfill: bool,
        model_ids: Sequence[str],
        basin_ids: Sequence[str],
        dry_run: bool,
        continuous: bool,
        interval_seconds: float | None,
        max_passes: int | None,
        workspace_root: str | None,
        lock_path: str | None,
        evidence_dir: str | None,
    ) -> None:
        try:
            payload = _plan_production(
                sources=_split_csv(sources),
                lookback_hours=lookback_hours,
                cycle_lag_hours=cycle_lag_hours,
                max_cycles_per_source=max_cycles_per_source,
                cycle_time=cycle_time,
                disable_backfill=disable_backfill,
                model_ids=_split_csv(model_ids),
                basin_ids=_split_csv(basin_ids),
                dry_run=dry_run,
                continuous=continuous,
                interval_seconds=interval_seconds,
                max_passes=max_passes,
                workspace_root=workspace_root,
                lock_path=lock_path,
                evidence_dir=evidence_dir,
            )
            click.echo(
                json.dumps(
                    _scheduler_stdout_payload(payload),
                    sort_keys=True,
                )
            )
        except ValueError as error:
            click.echo(str(error), err=True)
            raise SystemExit(2) from error
        except OrchestratorError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=argv is None)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    trigger_parser = subparsers.add_parser("trigger-analysis")
    trigger_parser.add_argument("--model-id", required=True)
    trigger_parser.add_argument("--date-range", required=True)
    publish_tiles_parser = subparsers.add_parser("publish-tiles")
    publish_tiles_parser.add_argument("--cycle-id", required=True)
    publish_qdown_parser = subparsers.add_parser("publish-qdown")
    publish_qdown_parser.add_argument("--cycle-id", required=True)
    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--retention-days", type=int, default=None)
    cleanup_parser.add_argument("--dry-run", action="store_true", default=True)
    cleanup_parser.add_argument("--execute", action="store_false", dest="dry_run")
    migrate_parser = subparsers.add_parser("migrate-scheduler-state")
    migrate_parser.add_argument("--database-url", required=True)
    migrate_parser.add_argument("--journal-root", required=True)
    migrate_parser.add_argument("--receipt-path")
    migrate_parser.add_argument("--allow-historical-node22", action="store_true")
    rollback_parser = subparsers.add_parser("prepare-file-journal-rollback")
    rollback_parser.add_argument("--journal-root", required=True)
    rollback_parser.add_argument("--workspace-root", required=True)
    rollback_parser.add_argument("--lock-path")
    rollback_parser.add_argument("--scheduler-lock-backend", default="file")
    rollback_parser.add_argument("--lock-ttl-seconds", default=60, type=int)
    rollback_parser.add_argument("--scheduler-state", required=True, choices=("stopped",))
    rollback_parser.add_argument("--active-scheduler-processes", required=True, type=int)
    rollback_parser.add_argument("--checked-at", required=True)
    rollback_parser.add_argument("--checked-by", required=True)
    rollback_parser.add_argument("--target-writer-generation", required=True)
    launch_rollback_parser = subparsers.add_parser("launch-file-journal-rollback-writer")
    launch_rollback_parser.add_argument("--journal-root", required=True)
    launch_rollback_parser.add_argument("--workspace-root", required=True)
    launch_rollback_parser.add_argument("--receipt-id", required=True)
    launch_rollback_parser.add_argument("--writer-repository-root", required=True)
    launch_rollback_parser.add_argument("--lock-path")
    launch_rollback_parser.add_argument("--scheduler-lock-backend", default="file")
    launch_rollback_parser.add_argument("--lock-ttl-seconds", default=60, type=int)
    launch_rollback_parser.add_argument("writer_args", nargs=argparse.REMAINDER)
    rollforward_parser = subparsers.add_parser("complete-file-journal-rollforward")
    rollforward_parser.add_argument("--journal-root", required=True)
    rollforward_parser.add_argument("--workspace-root", required=True)
    rollforward_parser.add_argument("--preparation-receipt-id", required=True)
    rollforward_parser.add_argument("--lock-path")
    rollforward_parser.add_argument("--scheduler-lock-backend", default="file")
    rollforward_parser.add_argument("--lock-ttl-seconds", default=60, type=int)
    plan_parser = subparsers.add_parser("plan-production")
    plan_parser.add_argument("--source", action="append", default=[])
    plan_parser.add_argument("--lookback-hours", type=int, default=None)
    plan_parser.add_argument("--cycle-lag-hours", type=int, default=None)
    plan_parser.add_argument("--max-cycles-per-source", type=int, default=None)
    plan_parser.add_argument("--cycle-time", default=None)
    plan_parser.add_argument("--disable-backfill", action="store_true")
    plan_parser.add_argument("--model-id", action="append", default=[])
    plan_parser.add_argument("--basin-id", action="append", default=[])
    plan_parser.add_argument("--dry-run", action="store_true", default=True)
    plan_parser.add_argument("--plan", action="store_true", dest="dry_run")
    plan_parser.add_argument("--submit", action="store_false", dest="dry_run")
    plan_parser.add_argument("--continuous", action="store_true")
    plan_parser.add_argument("--interval-seconds", type=float, default=None)
    plan_parser.add_argument("--max-passes", type=int)
    plan_parser.add_argument("--workspace-root")
    plan_parser.add_argument("--lock-path")
    plan_parser.add_argument("--evidence-dir")
    args = parser.parse_args(argv)

    if args.command == "trigger-analysis":
        try:
            print(json.dumps(_trigger_analysis(model_id=args.model_id, date_range=args.date_range), sort_keys=True))
        except OrchestratorError as error:
            print(f"{error.error_code}: {error.message}", file=sys.stderr)
            return 1
        return 0
    if args.command == "publish-tiles":
        try:
            print(json.dumps(_publish_tiles(cycle_id=args.cycle_id), sort_keys=True))
            return 0
        except PublishError as error:
            print(json.dumps(failure_payload(args.cycle_id, error), sort_keys=True))
            return 1
    if args.command == "publish-qdown":
        try:
            print(json.dumps(_publish_qdown(cycle_id=args.cycle_id), sort_keys=True))
            return 0
        except PublishError as error:
            print(json.dumps(failure_payload(args.cycle_id, error), sort_keys=True))
            return 1
    if args.command == "cleanup":
        try:
            print(json.dumps(_run_cleanup(retention_days=args.retention_days, dry_run=args.dry_run), sort_keys=True))
            return 0
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
    if args.command == "migrate-scheduler-state":
        try:
            receipt = export_scheduler_state_from_postgres(
                database_url=args.database_url,
                journal_root=args.journal_root,
                allow_historical_node22=args.allow_historical_node22,
            )
            if args.receipt_path:
                write_migration_receipt(receipt, args.receipt_path, containment_root=args.journal_root)
            print(json.dumps(receipt, sort_keys=True))
            return 0
        except (RuntimeError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
    if args.command == "prepare-file-journal-rollback":
        try:
            receipt = _prepare_file_journal_rollback(
                journal_root=args.journal_root,
                workspace_root=args.workspace_root,
                lock_path=args.lock_path,
                scheduler_lock_backend=args.scheduler_lock_backend,
                lock_ttl_seconds=args.lock_ttl_seconds,
                scheduler_state=args.scheduler_state,
                active_scheduler_processes=args.active_scheduler_processes,
                checked_at=args.checked_at,
                checked_by=args.checked_by,
                target_writer_generation=args.target_writer_generation,
            )
            print(json.dumps(receipt, sort_keys=True))
            return 0
        except (FileOrchestrationJournalError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
    if args.command == "launch-file-journal-rollback-writer":
        try:
            result = _launch_file_journal_rollback_writer(
                journal_root=args.journal_root,
                workspace_root=args.workspace_root,
                receipt_id=args.receipt_id,
                writer_repository_root=args.writer_repository_root,
                writer_args=args.writer_args,
                lock_path=args.lock_path,
                scheduler_lock_backend=args.scheduler_lock_backend,
                lock_ttl_seconds=args.lock_ttl_seconds,
            )
            print(json.dumps(result, sort_keys=True))
            return int(result["writer_exit_code"])
        except (FileOrchestrationJournalError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
    if args.command == "complete-file-journal-rollforward":
        try:
            receipt = _complete_file_journal_rollforward(
                journal_root=args.journal_root,
                workspace_root=args.workspace_root,
                preparation_receipt_id=args.preparation_receipt_id,
                lock_path=args.lock_path,
                scheduler_lock_backend=args.scheduler_lock_backend,
                lock_ttl_seconds=args.lock_ttl_seconds,
            )
            print(json.dumps(receipt, sort_keys=True))
            return 0
        except (FileOrchestrationJournalError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
    if args.command == "plan-production":
        try:
            payload = _plan_production(
                sources=_split_csv(args.source),
                lookback_hours=args.lookback_hours,
                cycle_lag_hours=args.cycle_lag_hours,
                max_cycles_per_source=args.max_cycles_per_source,
                cycle_time=args.cycle_time,
                disable_backfill=args.disable_backfill,
                model_ids=_split_csv(args.model_id),
                basin_ids=_split_csv(args.basin_id),
                dry_run=args.dry_run,
                continuous=args.continuous,
                interval_seconds=args.interval_seconds,
                max_passes=args.max_passes,
                workspace_root=args.workspace_root,
                lock_path=args.lock_path,
                evidence_dir=args.evidence_dir,
            )
            print(
                json.dumps(
                    _scheduler_stdout_payload(payload),
                    sort_keys=True,
                )
            )
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        except OrchestratorError as error:
            print(f"{error.error_code}: {error.message}", file=sys.stderr)
            return 1
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
