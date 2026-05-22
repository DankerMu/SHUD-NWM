from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from services.tile_publisher import PublishError, TilePublisher
from services.tile_publisher.publisher import failure_payload

from .chain import AnalysisOrchestrator, OrchestratorError
from .scheduler import ProductionScheduler, ProductionSchedulerConfig


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


def _split_csv(values: Sequence[str] | None) -> tuple[str, ...]:
    result: list[str] = []
    for value in values or ():
        for item in str(value).split(","):
            item = item.strip()
            if item:
                result.append(item)
    return tuple(result)


def _plan_production(
    *,
    sources: Sequence[str],
    lookback_hours: int,
    cycle_lag_hours: int,
    max_cycles_per_source: int,
    model_ids: Sequence[str],
    basin_ids: Sequence[str],
    dry_run: bool,
    continuous: bool,
    interval_seconds: float,
    max_passes: int | None,
    workspace_root: str | None,
    lock_path: str | None,
    evidence_dir: str | None,
) -> dict[str, object]:
    config = ProductionSchedulerConfig(
        workspace_root=workspace_root or ".nhms-workspace",
        sources=tuple(sources) if sources else ("gfs", "IFS"),
        lookback_hours=lookback_hours,
        cycle_lag_hours=cycle_lag_hours,
        max_cycles_per_source=max_cycles_per_source,
        model_ids=tuple(model_ids),
        basin_ids=tuple(basin_ids),
        dry_run=dry_run,
        continuous=continuous,
        interval_seconds=interval_seconds,
        lock_path=lock_path,
        evidence_dir=evidence_dir,
    )
    scheduler = ProductionScheduler(config)
    if continuous:
        results = scheduler.run_continuous(max_passes=max_passes)
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

    @cli.command("plan-production")
    @click.option(
        "--source",
        "sources",
        multiple=True,
        help="Forecast source id. Repeat or pass comma-separated values.",
    )
    @click.option("--lookback-hours", default=24, show_default=True, type=int)
    @click.option("--cycle-lag-hours", default=0, show_default=True, type=int)
    @click.option("--max-cycles-per-source", default=1, show_default=True, type=int)
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
    @click.option("--dry-run/--plan", default=True, show_default=True)
    @click.option("--continuous", is_flag=True)
    @click.option("--interval-seconds", default=300.0, show_default=True, type=float)
    @click.option("--max-passes", type=int)
    @click.option("--workspace-root")
    @click.option("--lock-path")
    @click.option("--evidence-dir")
    def plan_production(
        sources: Sequence[str],
        lookback_hours: int,
        cycle_lag_hours: int,
        max_cycles_per_source: int,
        model_ids: Sequence[str],
        basin_ids: Sequence[str],
        dry_run: bool,
        continuous: bool,
        interval_seconds: float,
        max_passes: int | None,
        workspace_root: str | None,
        lock_path: str | None,
        evidence_dir: str | None,
    ) -> None:
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
    plan_parser = subparsers.add_parser("plan-production")
    plan_parser.add_argument("--source", action="append", default=[])
    plan_parser.add_argument("--lookback-hours", type=int, default=24)
    plan_parser.add_argument("--cycle-lag-hours", type=int, default=0)
    plan_parser.add_argument("--max-cycles-per-source", type=int, default=1)
    plan_parser.add_argument("--model-id", action="append", default=[])
    plan_parser.add_argument("--basin-id", action="append", default=[])
    plan_parser.add_argument("--dry-run", action="store_true", default=True)
    plan_parser.add_argument("--plan", action="store_false", dest="dry_run")
    plan_parser.add_argument("--continuous", action="store_true")
    plan_parser.add_argument("--interval-seconds", type=float, default=300.0)
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
    if args.command == "plan-production":
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
