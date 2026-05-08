from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .base import parse_cycle_time
from .era5_adapter import ERA5Adapter, parse_area
from .gfs_adapter import GFSAdapter


def _download(source_id: str, cycle_time: str) -> dict[str, object]:
    adapter = GFSAdapter.from_env()
    if source_id != adapter.config.source_id:
        raise SystemExit(
            f"Unsupported source_id {source_id!r}; this worker is configured for {adapter.config.source_id!r}."
        )

    manifest = adapter.build_manifest(parse_cycle_time(cycle_time))
    result = adapter.download_plan(manifest)
    if result.status == "failed_download":
        failure = next((file for file in result.files if file.status == "failed"), None)
        detail = ""
        if failure is not None:
            detail = f": {failure.error_code or 'UNKNOWN'} {failure.error_message or ''}".rstrip()
        print(f"Download failed for {source_id} {cycle_time}{detail}", file=sys.stderr)
        raise SystemExit(1)
    return {
        "status": result.status,
        "total_bytes_written": result.total_bytes_written,
        "retry_count": result.retry_count,
        "files": len(result.files),
    }


def _download_era5(cycle_date: str, area: str | None = None) -> dict[str, object]:
    adapter = ERA5Adapter.from_env(area=parse_area(area) if area else None)
    manifest = adapter.build_manifest(cycle_date)
    result = adapter.download_plan(manifest)
    if result.status == "failed_download":
        failure = next((file for file in result.files if file.status == "failed"), None)
        detail = ""
        if failure is not None:
            detail = f": {failure.error_code or 'UNKNOWN'} {failure.error_message or ''}".rstrip()
        print(f"Download failed for ERA5 {cycle_date}{detail}", file=sys.stderr)
        raise SystemExit(1)
    return {
        "status": result.status,
        "total_bytes_written": result.total_bytes_written,
        "retry_count": result.retry_count,
        "files": len(result.files),
    }


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command()
    @click.option("--source-id", default="gfs", show_default=True)
    @click.option("--cycle-time", required=True)
    def download(source_id: str, cycle_time: str) -> None:
        click.echo(json.dumps(_download(source_id, cycle_time), sort_keys=True))

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _click_era5_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command()
    @click.option("--date", "cycle_date", required=True)
    @click.option("--area", default=None)
    def download(cycle_date: str, area: str | None) -> None:
        click.echo(json.dumps(_download_era5(cycle_date, area), sort_keys=True))

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-gfs")
    subparsers = parser.add_subparsers(dest="command", required=True)
    download_parser = subparsers.add_parser("download")
    download_parser.add_argument("--source-id", default="gfs")
    download_parser.add_argument("--cycle-time", required=True)
    args = parser.parse_args(argv)

    if args.command == "download":
        print(json.dumps(_download(args.source_id, args.cycle_time), sort_keys=True))
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


def _argparse_era5_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-era5")
    subparsers = parser.add_subparsers(dest="command", required=True)
    download_parser = subparsers.add_parser("download")
    download_parser.add_argument("--date", required=True)
    download_parser.add_argument("--area", default=None)
    args = parser.parse_args(argv)

    if args.command == "download":
        print(json.dumps(_download_era5(args.date, args.area), sort_keys=True))
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


def era5_main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_era5_main(argv)
    return _click_era5_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
