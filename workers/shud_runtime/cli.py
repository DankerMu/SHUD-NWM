from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .runtime import SHUDRuntime, SHUDRuntimeError


def _execute(manifest: str) -> dict[str, object]:
    runtime = SHUDRuntime.from_env()
    result = runtime.execute_manifest_path(manifest)
    return {
        "run_id": result.run_id,
        "status": result.status,
        "output_uri": result.output_uri,
        "log_uri": result.log_uri,
        "rivqdown_file": result.rivqdown_file,
    }


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("execute")
    @click.option("--manifest", required=True)
    def execute(manifest: str) -> None:
        try:
            click.echo(json.dumps(_execute(manifest), sort_keys=True))
        except SHUDRuntimeError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-shud-runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)

    if args.command == "execute":
        try:
            print(json.dumps(_execute(args.manifest), sort_keys=True))
        except SHUDRuntimeError as error:
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
