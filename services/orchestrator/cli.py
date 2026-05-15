from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from services.tile_publisher import PublishError, TilePublisher
from services.tile_publisher.publisher import failure_payload

from .chain import AnalysisOrchestrator, OrchestratorError


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
    publisher = TilePublisher.from_env()
    return publisher.publish_cycle(cycle_id).to_dict()


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

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    trigger_parser = subparsers.add_parser("trigger-analysis")
    trigger_parser.add_argument("--model-id", required=True)
    trigger_parser.add_argument("--date-range", required=True)
    publish_tiles_parser = subparsers.add_parser("publish-tiles")
    publish_tiles_parser.add_argument("--cycle-id", required=True)
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
