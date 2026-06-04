from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Sequence

from services.tile_publisher import PublishError, TilePublisher
from services.tile_publisher.publisher import failure_payload

from .chain import AnalysisOrchestrator, OrchestratorError
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
    except PublishError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise PublishError("PUBLISH_TILES_FAILED", f"Tile publication failed: {error}") from error


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


def _non_blank_path(value: str | None, option_name: str) -> str | None:
    if value is not None and value.strip() == "":
        raise ValueError(f"plan-production {option_name} must not be blank")
    return value


def _plan_production(
    *,
    sources: Sequence[str],
    lookback_hours: int | None,
    cycle_lag_hours: int,
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
    config_kwargs: dict[str, object] = {
        "workspace_root": resolved_workspace_root,
        "sources": resolved_sources,
        "lookback_hours": resolved_lookback,
        "cycle_lag_hours": cycle_lag_hours,
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

    @cli.command("plan-production")
    @click.option(
        "--source",
        "sources",
        multiple=True,
        help="Forecast source id. Repeat or pass comma-separated values.",
    )
    @click.option("--lookback-hours", default=None, type=int)
    @click.option("--cycle-lag-hours", default=0, show_default=True, type=int)
    @click.option("--max-cycles-per-source", default=None, type=int)
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
        cycle_lag_hours: int,
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
    ) -> None:
        try:
            click.echo(
                json.dumps(
                    _plan_production(
                        sources=_split_csv(sources),
                        lookback_hours=lookback_hours,
                        cycle_lag_hours=cycle_lag_hours,
                        max_cycles_per_source=max_cycles_per_source,
                        model_ids=_split_csv(model_ids),
                        basin_ids=_split_csv(basin_ids),
                        dry_run=dry_run,
                        continuous=continuous,
                        interval_seconds=interval_seconds,
                        max_passes=max_passes,
                        workspace_root=workspace_root,
                        lock_path=lock_path,
                        evidence_dir=evidence_dir,
                    ),
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
    plan_parser = subparsers.add_parser("plan-production")
    plan_parser.add_argument("--source", action="append", default=[])
    plan_parser.add_argument("--lookback-hours", type=int, default=None)
    plan_parser.add_argument("--cycle-lag-hours", type=int, default=0)
    plan_parser.add_argument("--max-cycles-per-source", type=int, default=None)
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
    if args.command == "plan-production":
        try:
            print(
                json.dumps(
                    _plan_production(
                        sources=_split_csv(args.source),
                        lookback_hours=args.lookback_hours,
                        cycle_lag_hours=args.cycle_lag_hours,
                        max_cycles_per_source=args.max_cycles_per_source,
                        model_ids=_split_csv(args.model_id),
                        basin_ids=_split_csv(args.basin_id),
                        dry_run=args.dry_run,
                        continuous=args.continuous,
                        interval_seconds=args.interval_seconds,
                        max_passes=args.max_passes,
                        workspace_root=args.workspace_root,
                        lock_path=args.lock_path,
                        evidence_dir=args.evidence_dir,
                    ),
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
