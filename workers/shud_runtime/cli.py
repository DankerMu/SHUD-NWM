from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .runtime import SHUDRuntime, SHUDRuntimeError


def _execute(manifest: str, *, dry_run: bool = False) -> dict[str, object]:
    runtime = SHUDRuntime.from_env(dry_run=dry_run)
    result = runtime.execute_manifest_path(manifest)
    return {
        "run_id": result.run_id,
        "status": result.status,
        "output_uri": result.output_uri,
        "log_uri": result.log_uri,
        "rivqdown_file": result.rivqdown_file,
    }


def _run(run_id: str, *, run_type: str | None = None, dry_run: bool = False) -> dict[str, object]:
    manifest_path = Path(os.getenv("WORKSPACE_ROOT", ".")) / "runs" / run_id / "input" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if run_type is not None and manifest.get("run_type") != run_type:
        raise SHUDRuntimeError(
            "RUN_TYPE_MISMATCH",
            f"Manifest run_type is {manifest.get('run_type')!r}, not {run_type!r}.",
        )
    return _execute(str(manifest_path), dry_run=dry_run)


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("execute")
    @click.option("--manifest", required=True)
    @click.option("--dry-run", is_flag=True, default=False)
    def execute(manifest: str, dry_run: bool) -> None:
        try:
            click.echo(json.dumps(_execute(manifest, dry_run=dry_run), sort_keys=True))
        except SHUDRuntimeError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error

    @cli.command("run")
    @click.option("--run-type", required=False, default=None)
    @click.option("--run-id", required=True)
    @click.option("--dry-run", is_flag=True, default=False)
    def run(run_type: str | None, run_id: str, dry_run: bool) -> None:
        try:
            click.echo(json.dumps(_run(run_id, run_type=run_type, dry_run=dry_run), sort_keys=True))
        except (SHUDRuntimeError, OSError, json.JSONDecodeError) as error:
            if isinstance(error, SHUDRuntimeError):
                click.echo(f"{error.error_code}: {error.message}", err=True)
            else:
                click.echo(str(error), err=True)
            raise SystemExit(1) from error

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-shud-runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("--manifest", required=True)
    execute_parser.add_argument("--dry-run", action="store_true")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--run-type", default=None)
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "execute":
        try:
            print(json.dumps(_execute(args.manifest, dry_run=args.dry_run), sort_keys=True))
        except SHUDRuntimeError as error:
            print(f"{error.error_code}: {error.message}", file=sys.stderr)
            return 1
        return 0
    if args.command == "run":
        try:
            print(json.dumps(_run(args.run_id, run_type=args.run_type, dry_run=args.dry_run), sort_keys=True))
        except SHUDRuntimeError as error:
            print(f"{error.error_code}: {error.message}", file=sys.stderr)
            return 1
        except (OSError, json.JSONDecodeError) as error:
            print(str(error), file=sys.stderr)
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
