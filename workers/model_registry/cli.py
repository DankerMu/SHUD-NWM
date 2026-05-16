from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .basins_discovery import BasinsDiscoveryError, discover_basins_inventory, resolve_basins_root, write_inventory
from .basins_package import BasinsPackageError, publish_basins_package, write_basins_migration_report
from .validator import ModelPackageValidationError, validate_model_package_path

DEFAULT_BASINS_MIGRATION_SOURCE_URI = "/volume/data/nwm/Basins"


def _validate_package(package_path: str) -> dict[str, object]:
    result = validate_model_package_path(package_path)
    return {
        "status": "valid",
        "package_path": result.package_path,
        "matched_files": list(result.matched_files),
    }


def _discover_basins(basins_root: str | None, output: str) -> dict[str, object]:
    root = resolve_basins_root(basins_root)
    inventory = discover_basins_inventory(root)
    write_inventory(inventory, output)
    return inventory


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("validate-package")
    @click.argument("package_path")
    def validate_package(package_path: str) -> None:
        try:
            result = _validate_package(package_path)
        except ModelPackageValidationError as error:
            click.echo(str(error), err=True)
            raise SystemExit(1) from error
        click.echo(
            "All required model package files are present: " + ", ".join(str(file) for file in result["matched_files"])
        )

    @cli.command("discover-basins")
    @click.option("--basins-root", default=None, help="Basins root path. Overrides NHMS_BASINS_ROOT.")
    @click.option("--output", required=True, help="Path to write inventory JSON.")
    def discover_basins(basins_root: str | None, output: str) -> None:
        try:
            inventory = _discover_basins(basins_root, output)
        except BasinsDiscoveryError as error:
            click.echo(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), err=True)
            raise SystemExit(1) from error
        click.echo(
            json.dumps(
                {
                    "status": "ok",
                    "root": inventory["root"],
                    "resolved_root": inventory["resolved_root"],
                    "model_count": inventory["model_count"],
                    "output": output,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    @cli.command("publish-basins")
    @click.option("--inventory", required=True, help="Path to Basins discovery inventory JSON.")
    @click.option("--model-id", required=True, help="Basins model_id to publish.")
    @click.option("--version", required=True, help="Immutable package version to publish.")
    @click.option("--output", required=True, help="Path to write package manifest JSON.")
    @click.option("--copy-forcing", is_flag=True, help="Copy historical forcing CSV payloads explicitly.")
    def publish_basins(
        inventory: str,
        model_id: str,
        version: str,
        output: str,
        copy_forcing: bool,
    ) -> None:
        try:
            result = publish_basins_package(
                inventory_path=inventory,
                model_id=model_id,
                version=version,
                output_path=output,
                copy_forcing=copy_forcing,
            )
        except BasinsPackageError as error:
            click.echo(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), err=True)
            raise SystemExit(1) from error
        click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))

    @cli.command("basins-migration-report")
    @click.option("--basins-root", required=True, help="Copied Basins root path to inspect.")
    @click.option(
        "--source-uri",
        default=DEFAULT_BASINS_MIGRATION_SOURCE_URI,
        show_default=True,
        help="Original production source URI/path, e.g. /volume/data/nwm/Basins.",
    )
    @click.option("--output", required=True, help="Path to write migration report JSON.")
    def basins_migration_report(basins_root: str, source_uri: str, output: str) -> None:
        try:
            report = write_basins_migration_report(basins_root=basins_root, source_uri=source_uri, output_path=output)
        except BasinsPackageError as error:
            click.echo(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), err=True)
            raise SystemExit(1) from error
        click.echo(
            json.dumps(
                {
                    "status": "ok",
                    "path": report["target_path"],
                    "output": output,
                    "production_ready": report["production_ready"],
                    "inventory_checksum": report["inventory_checksum"],
                    "file_count": report["file_count"],
                    "byte_count": report["byte_count"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-model")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate-package")
    validate_parser.add_argument("package_path")
    discover_parser = subparsers.add_parser("discover-basins")
    discover_parser.add_argument("--basins-root", default=None)
    discover_parser.add_argument("--output", required=True)
    publish_parser = subparsers.add_parser("publish-basins")
    publish_parser.add_argument("--inventory", required=True)
    publish_parser.add_argument("--model-id", required=True)
    publish_parser.add_argument("--version", required=True)
    publish_parser.add_argument("--output", required=True)
    publish_parser.add_argument("--copy-forcing", action="store_true")
    migration_parser = subparsers.add_parser("basins-migration-report")
    migration_parser.add_argument("--basins-root", required=True)
    migration_parser.add_argument("--source-uri", default=DEFAULT_BASINS_MIGRATION_SOURCE_URI)
    migration_parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    if args.command == "validate-package":
        try:
            result = _validate_package(args.package_path)
        except ModelPackageValidationError as error:
            print(str(error), file=sys.stderr)
            return 1
        print("All required model package files are present: " + ", ".join(result["matched_files"]))
        return 0
    if args.command == "discover-basins":
        try:
            inventory = _discover_basins(args.basins_root, args.output)
        except BasinsDiscoveryError as error:
            print(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "status": "ok",
                    "root": inventory["root"],
                    "resolved_root": inventory["resolved_root"],
                    "model_count": inventory["model_count"],
                    "output": args.output,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "publish-basins":
        try:
            result = publish_basins_package(
                inventory_path=args.inventory,
                model_id=args.model_id,
                version=args.version,
                output_path=args.output,
                copy_forcing=args.copy_forcing,
            )
        except BasinsPackageError as error:
            print(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "basins-migration-report":
        try:
            report = write_basins_migration_report(
                basins_root=args.basins_root,
                source_uri=args.source_uri,
                output_path=args.output,
            )
        except BasinsPackageError as error:
            print(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "status": "ok",
                    "path": report["target_path"],
                    "output": args.output,
                    "production_ready": report["production_ready"],
                    "inventory_checksum": report["inventory_checksum"],
                    "file_count": report["file_count"],
                    "byte_count": report["byte_count"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
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
