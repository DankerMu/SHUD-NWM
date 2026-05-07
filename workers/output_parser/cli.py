from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .parser import OutputParser, OutputParsingError


def _parse(run_id: str) -> dict[str, object]:
    parser = OutputParser.from_env()
    result = parser.parse_run(run_id)
    return {
        "run_id": result.run_id,
        "status": result.status,
        "source_file": result.source_file,
        "rows_written": result.rows_written,
        "qc_passed": result.qc_passed,
        "max_value_m3s": result.max_value_m3s,
    }


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("shud-output")
    @click.option("--run-id", required=True)
    def shud_output(run_id: str) -> None:
        try:
            click.echo(json.dumps(_parse(run_id), sort_keys=True))
        except OutputParsingError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-parse")
    subparsers = parser.add_subparsers(dest="command", required=True)
    shud_parser = subparsers.add_parser("shud-output")
    shud_parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)

    if args.command == "shud-output":
        try:
            print(json.dumps(_parse(args.run_id), sort_keys=True))
        except OutputParsingError as error:
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
