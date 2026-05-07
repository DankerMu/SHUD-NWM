from __future__ import annotations

import argparse
import json
from typing import Sequence

from .base import parse_cycle_time
from .gfs_adapter import GFSAdapter


def _download(source_id: str, cycle_time: str) -> dict[str, object]:
    adapter = GFSAdapter.from_env()
    if source_id != adapter.config.source_id:
        raise SystemExit(
            f"Unsupported source_id {source_id!r}; this worker is configured for {adapter.config.source_id!r}."
        )

    manifest = adapter.build_manifest(parse_cycle_time(cycle_time))
    result = adapter.download_plan(manifest)
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


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
