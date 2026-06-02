from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Sequence

from apps.api.auth import PolicyDecision, cli_policy_decision_from_evidence

from .basins_discovery import BasinsDiscoveryError, discover_basins_inventory, resolve_basins_root, write_inventory
from .basins_package import BasinsPackageError, publish_basins_package, write_basins_migration_report
from .basins_registry_import import BasinsRegistryImportError, import_basins_registry
from .qhh_production_bootstrap import QhhProductionBootstrapError, bootstrap_qhh_production
from .validator import ModelPackageValidationError, validate_model_package_path

DEFAULT_BASINS_MIGRATION_SOURCE_URI = "/volume/data/nwm/Basins"
PUBLIC_REGISTRY_IMPORT_UNKNOWN_TARGET_ID = "unknown"


@dataclass(frozen=True)
class RegistryImportPolicyDecisions:
    preflight: PolicyDecision | None
    manifest: PolicyDecision | None


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


def _import_policy_decision(
    model_id: str,
    *,
    auth_actor_id: str | None,
    auth_roles: Sequence[str] | None,
) -> PolicyDecision | None:
    return cli_policy_decision_from_evidence(
        "models.switch_version",
        target_type="model_registry",
        target_id=model_id,
        actor_id=auth_actor_id,
        roles=auth_roles,
    )


def _model_id_from_manifest(package_manifest_path: str) -> str:
    try:
        with open(package_manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return ""
    return str(manifest.get("model_id") or "")


def _registry_import_policy_decision(
    package_manifest_path: str,
    *,
    auth_actor_id: str | None,
    auth_roles: Sequence[str] | None,
) -> RegistryImportPolicyDecisions:
    decision = _import_policy_decision(
        PUBLIC_REGISTRY_IMPORT_UNKNOWN_TARGET_ID,
        auth_actor_id=auth_actor_id,
        auth_roles=auth_roles,
    )
    if decision is None or decision.decision != "allow":
        return RegistryImportPolicyDecisions(preflight=decision, manifest=decision)
    return RegistryImportPolicyDecisions(
        preflight=decision,
        manifest=_import_policy_decision(
            _model_id_from_manifest(package_manifest_path),
            auth_actor_id=auth_actor_id,
            auth_roles=auth_roles,
        ),
    )


def _add_argparse_auth_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--auth-actor-id")
    parser.add_argument("--auth-role", action="append", default=[])


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

    @cli.command("import-basins-registry")
    @click.option("--inventory", required=True, help="Path to Basins discovery inventory JSON.")
    @click.option("--package-manifest", required=True, help="Path to Basins package manifest JSON.")
    @click.option("--database-url", default=None, help="PostgreSQL/PostGIS URL. Defaults to DATABASE_URL.")
    @click.option("--output", default=None, help="Optional path to write import report JSON.")
    @click.option("--auth-actor-id", default=None, help="Dev/test CLI auth actor id.")
    @click.option("--auth-role", multiple=True, help="Dev/test CLI auth role. May be repeated.")
    def import_basins_registry_command(
        inventory: str,
        package_manifest: str,
        database_url: str | None,
        output: str | None,
        auth_actor_id: str | None,
        auth_role: tuple[str, ...],
    ) -> None:
        try:
            policy_decisions = _registry_import_policy_decision(
                package_manifest,
                auth_actor_id=auth_actor_id,
                auth_roles=auth_role,
            )
            result = import_basins_registry(
                inventory_path=inventory,
                package_manifest_path=package_manifest,
                database_url=database_url,
                output_path=output,
                policy_decision=policy_decisions.manifest,
                preflight_policy_decision=policy_decisions.preflight,
            )
        except BasinsRegistryImportError as error:
            click.echo(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), err=True)
            raise SystemExit(1) from error
        click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))

    @cli.command("bootstrap-qhh-production")
    @click.option("--database-url", default=None, help="PostgreSQL/PostGIS URL. Defaults to DATABASE_URL.")
    @click.option("--basins-root", default=None, help="Basins root path. Overrides NHMS_BASINS_ROOT.")
    @click.option("--project-name", default="qhh", show_default=True, help="QHH SHUD project/input name.")
    @click.option("--basin-slug", default="qhh", show_default=True, help="QHH Basins source slug under root.")
    @click.option("--model-id", default="basins_qhh_shud", show_default=True, help="QHH model_id to bootstrap.")
    @click.option(
        "--package-version",
        default="vbasins-qhh-production",
        show_default=True,
        help="Package version to publish when --package-manifest is omitted.",
    )
    @click.option("--inventory", default=None, help="Optional precomputed Basins discovery inventory JSON.")
    @click.option("--package-manifest", default=None, help="Optional precomputed Basins package manifest JSON.")
    @click.option("--work-dir", default=None, help="Bootstrap work dir for generated inventory/manifest.")
    @click.option("--evidence-dir", default=None, help="Approved evidence root for --evidence-path.")
    @click.option("--evidence-path", default=None, help="No-clobber bootstrap evidence JSON path.")
    @click.option(
        "--shud-code-version",
        default="basins-shud",
        show_default=True,
        help="SHUD code/runtime version recorded on the active model.",
    )
    def bootstrap_qhh_production_command(
        database_url: str | None,
        basins_root: str | None,
        project_name: str,
        basin_slug: str,
        model_id: str,
        package_version: str,
        inventory: str | None,
        package_manifest: str | None,
        work_dir: str | None,
        evidence_dir: str | None,
        evidence_path: str | None,
        shud_code_version: str,
    ) -> None:
        try:
            result = bootstrap_qhh_production(
                database_url=database_url,
                basins_root=basins_root,
                qhh_project_name=project_name,
                qhh_basin_slug=basin_slug,
                model_id=model_id,
                package_version=package_version,
                inventory_path=inventory,
                package_manifest_path=package_manifest,
                work_dir=work_dir,
                evidence_dir=evidence_dir,
                evidence_path=evidence_path,
                shud_code_version=shud_code_version,
            )
        except QhhProductionBootstrapError as error:
            click.echo(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), err=True)
            raise SystemExit(1) from error
        click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))

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
    import_parser = subparsers.add_parser("import-basins-registry")
    import_parser.add_argument("--inventory", required=True)
    import_parser.add_argument("--package-manifest", required=True)
    import_parser.add_argument("--database-url", default=None)
    import_parser.add_argument("--output", default=None)
    _add_argparse_auth_options(import_parser)
    qhh_parser = subparsers.add_parser("bootstrap-qhh-production")
    qhh_parser.add_argument("--database-url", default=None)
    qhh_parser.add_argument("--basins-root", default=None)
    qhh_parser.add_argument("--project-name", default="qhh")
    qhh_parser.add_argument("--basin-slug", default="qhh")
    qhh_parser.add_argument("--model-id", default="basins_qhh_shud")
    qhh_parser.add_argument("--package-version", default="vbasins-qhh-production")
    qhh_parser.add_argument("--inventory", default=None)
    qhh_parser.add_argument("--package-manifest", default=None)
    qhh_parser.add_argument("--work-dir", default=None)
    qhh_parser.add_argument("--evidence-dir", default=None)
    qhh_parser.add_argument("--evidence-path", default=None)
    qhh_parser.add_argument("--shud-code-version", default="basins-shud")
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
    if args.command == "import-basins-registry":
        try:
            policy_decisions = _registry_import_policy_decision(
                args.package_manifest,
                auth_actor_id=args.auth_actor_id,
                auth_roles=args.auth_role,
            )
            result = import_basins_registry(
                inventory_path=args.inventory,
                package_manifest_path=args.package_manifest,
                database_url=args.database_url,
                output_path=args.output,
                policy_decision=policy_decisions.manifest,
                preflight_policy_decision=policy_decisions.preflight,
            )
        except BasinsRegistryImportError as error:
            print(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "bootstrap-qhh-production":
        try:
            result = bootstrap_qhh_production(
                database_url=args.database_url,
                basins_root=args.basins_root,
                qhh_project_name=args.project_name,
                qhh_basin_slug=args.basin_slug,
                model_id=args.model_id,
                package_version=args.package_version,
                inventory_path=args.inventory,
                package_manifest_path=args.package_manifest,
                work_dir=args.work_dir,
                evidence_dir=args.evidence_dir,
                evidence_path=args.evidence_path,
                shud_code_version=args.shud_code_version,
            )
        except QhhProductionBootstrapError as error:
            print(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
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
