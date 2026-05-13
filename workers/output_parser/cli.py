from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from packages.common.manifest_index import ManifestValidationError, load_manifest_entry, resolve_task_id

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


def _resolve_run_id(run_id: str | None, manifest_index: str | None, task_id: int | None) -> str:
    if manifest_index is not None:
        resolved_task_id = resolve_task_id(task_id)
        entry = load_manifest_entry(manifest_index, resolved_task_id)
        return str(entry["run_id"])
    if not run_id:
        raise ManifestValidationError(
            "Explicit output parsing requires --run-id.",
            {"missing_fields": ["run_id"]},
        )
    return run_id


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("shud-output")
    @click.option("--run-id")
    @click.option("--manifest-index")
    @click.option("--task-id", type=int, default=None)
    def shud_output(run_id: str | None, manifest_index: str | None, task_id: int | None) -> None:
        try:
            click.echo(json.dumps(_parse(_resolve_run_id(run_id, manifest_index, task_id)), sort_keys=True))
        except (ManifestValidationError, OutputParsingError) as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error

    @cli.command("parse")
    @click.option("--run-id")
    @click.option("--manifest-index")
    @click.option("--task-id", type=int, default=None)
    def parse(run_id: str | None, manifest_index: str | None, task_id: int | None) -> None:
        try:
            click.echo(json.dumps(_parse(_resolve_run_id(run_id, manifest_index, task_id)), sort_keys=True))
        except (ManifestValidationError, OutputParsingError) as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-parse")
    subparsers = parser.add_subparsers(dest="command", required=True)
    shud_parser = subparsers.add_parser("shud-output")
    shud_parser.add_argument("--run-id")
    shud_parser.add_argument("--manifest-index")
    shud_parser.add_argument("--task-id", type=int, default=None)
    parse_parser = subparsers.add_parser("parse")
    parse_parser.add_argument("--run-id")
    parse_parser.add_argument("--manifest-index")
    parse_parser.add_argument("--task-id", type=int, default=None)
    args = parser.parse_args(argv)

    if args.command == "shud-output":
        try:
            print(json.dumps(_parse(_resolve_run_id(args.run_id, args.manifest_index, args.task_id)), sort_keys=True))
        except (ManifestValidationError, OutputParsingError) as error:
            print(f"{error.error_code}: {error.message}", file=sys.stderr)
            return 1
        return 0
    if args.command == "parse":
        try:
            print(json.dumps(_parse(_resolve_run_id(args.run_id, args.manifest_index, args.task_id)), sort_keys=True))
        except (ManifestValidationError, OutputParsingError) as error:
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
