from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .validator import ModelPackageValidationError, validate_model_package_path


def _validate_package(package_path: str) -> dict[str, object]:
    result = validate_model_package_path(package_path)
    return {
        "status": "valid",
        "package_path": result.package_path,
        "matched_files": list(result.matched_files),
    }


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

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-model")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate-package")
    validate_parser.add_argument("package_path")
    args = parser.parse_args(argv)

    if args.command == "validate-package":
        try:
            result = _validate_package(args.package_path)
        except ModelPackageValidationError as error:
            print(str(error), file=sys.stderr)
            return 1
        print("All required model package files are present: " + ", ".join(result["matched_files"]))
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
