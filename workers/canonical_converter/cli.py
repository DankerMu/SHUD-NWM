from __future__ import annotations

import argparse
import json
from typing import Sequence

from .converter import (
    CanonicalConverter,
    ERA5CanonicalConverter,
    IFSCanonicalConverter,
    format_cycle_time,
    parse_cycle_time,
)


def _convert(source_id: str, cycle_time: str) -> dict[str, object]:
    normalized_source_id = source_id.upper() if source_id.upper() in {"ERA5", "IFS"} else source_id
    if normalized_source_id == "ERA5":
        converter = ERA5CanonicalConverter.from_env()
    elif normalized_source_id == "IFS":
        converter = IFSCanonicalConverter.from_env()
    else:
        converter = CanonicalConverter.from_env()
    if normalized_source_id != converter.config.source_id:
        raise SystemExit(
            f"Unsupported source_id {source_id!r}; this worker is configured for {converter.config.source_id!r}."
        )

    parsed_cycle_time = parse_cycle_time(cycle_time)
    compact_cycle = format_cycle_time(parsed_cycle_time)
    manifest_cycle_key = parsed_cycle_time.strftime("%Y-%m-%d") if normalized_source_id == "ERA5" else compact_cycle
    manifest_uri = converter.object_store.uri_for_key(f"raw/{normalized_source_id}/{manifest_cycle_key}/manifest.json")
    result = converter.convert_manifest_uri(manifest_uri)
    return {"status": result.status, "products": len(result.products)}


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command()
    @click.option("--source-id", default="gfs", show_default=True)
    @click.option("--cycle-time", required=True)
    def convert(source_id: str, cycle_time: str) -> None:
        click.echo(json.dumps(_convert(source_id, cycle_time), sort_keys=True))

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-canonical")
    subparsers = parser.add_subparsers(dest="command", required=True)
    convert_parser = subparsers.add_parser("convert")
    convert_parser.add_argument("--source-id", default="gfs")
    convert_parser.add_argument("--cycle-time", required=True)
    args = parser.parse_args(argv)

    if args.command == "convert":
        print(json.dumps(_convert(args.source_id, args.cycle_time), sort_keys=True))
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
