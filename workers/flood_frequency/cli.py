from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from workers.flood_frequency.hindcast import (
    HindcastError,
    hindcast_status,
    hindcast_year,
    submit_hindcast,
)


def _session_from_env() -> Session:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise HindcastError("DATABASE_URL_MISSING", "DATABASE_URL is required for nhms-flood commands.")
    return Session(create_engine(database_url, future=True))


def _hindcast_submit(model_id: str, source_id: str, start_time: str, end_time: str, purpose: str) -> dict[str, object]:
    with _session_from_env() as session:
        result = submit_hindcast(model_id, source_id, start_time, end_time, purpose, session)
        return {
            "total_runs": result.total_runs,
            "run_ids": result.run_ids,
            "skipped_years": result.skipped_years,
        }


def _hindcast_year(model_id: str, source_id: str, year: int) -> dict[str, object]:
    with _session_from_env() as session:
        result = hindcast_year(model_id, source_id, year, session)
        return {
            "run_id": result.run_id,
            "forcing_version_id": result.forcing_version_id,
            "status": result.status,
            "shud_result": result.shud_result,
            "parse_result": result.parse_result,
        }


def _hindcast_status(model_id: str) -> dict[str, object]:
    with _session_from_env() as session:
        return {"items": hindcast_status(model_id, session)}


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("hindcast-submit")
    @click.option("--model-id", required=True)
    @click.option("--source-id", default="ERA5", show_default=True)
    @click.option("--start-time", required=True)
    @click.option("--end-time", required=True)
    @click.option("--purpose", default="flood_frequency_sample", show_default=True)
    def hindcast_submit_command(model_id: str, source_id: str, start_time: str, end_time: str, purpose: str) -> None:
        try:
            click.echo(json.dumps(_hindcast_submit(model_id, source_id, start_time, end_time, purpose), sort_keys=True))
        except HindcastError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error

    @cli.command("hindcast-year")
    @click.option("--model-id", required=True)
    @click.option("--source-id", default="ERA5", show_default=True)
    @click.option("--year", required=True, type=int)
    def hindcast_year_command(model_id: str, source_id: str, year: int) -> None:
        try:
            click.echo(json.dumps(_hindcast_year(model_id, source_id, year), sort_keys=True))
        except HindcastError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error

    @cli.command("hindcast-status")
    @click.option("--model-id", required=True)
    def hindcast_status_command(model_id: str) -> None:
        try:
            click.echo(json.dumps(_hindcast_status(model_id), sort_keys=True, default=str))
        except HindcastError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-flood")
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit_parser = subparsers.add_parser("hindcast-submit")
    submit_parser.add_argument("--model-id", required=True)
    submit_parser.add_argument("--source-id", default="ERA5")
    submit_parser.add_argument("--start-time", required=True)
    submit_parser.add_argument("--end-time", required=True)
    submit_parser.add_argument("--purpose", default="flood_frequency_sample")

    year_parser = subparsers.add_parser("hindcast-year")
    year_parser.add_argument("--model-id", required=True)
    year_parser.add_argument("--source-id", default="ERA5")
    year_parser.add_argument("--year", required=True, type=int)

    status_parser = subparsers.add_parser("hindcast-status")
    status_parser.add_argument("--model-id", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "hindcast-submit":
            result = _hindcast_submit(args.model_id, args.source_id, args.start_time, args.end_time, args.purpose)
            print(json.dumps(result))
            return 0
        if args.command == "hindcast-year":
            print(json.dumps(_hindcast_year(args.model_id, args.source_id, args.year)))
            return 0
        if args.command == "hindcast-status":
            print(json.dumps(_hindcast_status(args.model_id), default=str))
            return 0
    except HindcastError as error:
        print(f"{error.error_code}: {error.message}", file=sys.stderr)
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
