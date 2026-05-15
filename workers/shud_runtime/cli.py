from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from packages.common.manifest_index import ManifestValidationError, load_manifest_entry, resolve_task_id

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


def _resolve_execute_manifest(manifest: str | None, manifest_index: str | None, task_id: int | None) -> str:
    if manifest_index is not None:
        resolved_task_id = resolve_task_id(task_id)
        entry = load_manifest_entry(manifest_index, resolved_task_id)
        if entry.get("manifest_path"):
            return str(entry["manifest_path"])
        workspace = os.getenv("WORKSPACE_ROOT") or str(entry["workspace_dir"])
        return str(Path(workspace) / "runs" / str(entry["run_id"]) / "input" / "manifest.json")
    if not manifest:
        raise ManifestValidationError(
            "Explicit runtime execution requires --manifest.",
            {"missing_fields": ["manifest"]},
        )
    return manifest


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("execute")
    @click.option("--manifest")
    @click.option("--manifest-index")
    @click.option("--task-id", type=int, default=None)
    @click.option("--dry-run", is_flag=True, default=False)
    def execute(manifest: str | None, manifest_index: str | None, task_id: int | None, dry_run: bool) -> None:
        try:
            resolved_manifest = _resolve_execute_manifest(manifest, manifest_index, task_id)
            click.echo(json.dumps(_execute(resolved_manifest, dry_run=dry_run), sort_keys=True))
        except (ManifestValidationError, SHUDRuntimeError, OSError, json.JSONDecodeError) as error:
            error_code, message = _cli_error(error)
            click.echo(f"{error_code}: {message}", err=True)
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
    execute_parser.add_argument("--manifest")
    execute_parser.add_argument("--manifest-index")
    execute_parser.add_argument("--task-id", type=int, default=None)
    execute_parser.add_argument("--dry-run", action="store_true")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--run-type", default=None)
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "execute":
        try:
            resolved_manifest = _resolve_execute_manifest(args.manifest, args.manifest_index, args.task_id)
            print(json.dumps(_execute(resolved_manifest, dry_run=args.dry_run), sort_keys=True))
        except (ManifestValidationError, SHUDRuntimeError, OSError, json.JSONDecodeError) as error:
            error_code, message = _cli_error(error)
            print(f"{error_code}: {message}", file=sys.stderr)
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


def _cli_error(error: Exception) -> tuple[str, str]:
    if isinstance(error, (ManifestValidationError, SHUDRuntimeError)):
        return error.error_code, error.message
    if isinstance(error, FileNotFoundError):
        return "RUNTIME_MANIFEST_MISSING", str(error)
    if isinstance(error, json.JSONDecodeError):
        return "RUNTIME_MANIFEST_INVALID_JSON", str(error)
    return "RUNTIME_MANIFEST_READ_FAILED", str(error)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
