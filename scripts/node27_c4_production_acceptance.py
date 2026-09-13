#!/usr/bin/env python3
"""Bringup-C4 production-acceptance owner CLI.

Public freeze / bind / verify stages around the unchanged Node C4 binder.
This CLI never self-approves a source and never substitutes a fake PASS.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from services.production_closure.c4_production_acceptance import bind, freeze, verify
from services.production_closure.c4_production_acceptance_io import C4AcceptanceError


class _ArgumentParser(argparse.ArgumentParser):
    """Keep command-line errors static and free of supplied input text."""

    def error(self, _message: str) -> None:
        self.exit(2, "C4_USAGE: invalid arguments\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="node27_c4_production_acceptance.py",
        description=__doc__,
        add_help=False,
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_ArgumentParser)

    freeze_cmd = sub.add_parser("freeze", add_help=False, allow_abbrev=False)
    freeze_cmd.add_argument("--approved-record", required=True)
    freeze_cmd.add_argument("--reviewed-sha", required=True)
    freeze_cmd.add_argument("--receipt", required=True)
    freeze_cmd.add_argument("--frontend-origin", required=True)
    freeze_cmd.add_argument("--api-origin", required=True)
    freeze_cmd.add_argument("--basin-id", required=True)
    freeze_cmd.add_argument("--segment-id", required=True)
    freeze_cmd.add_argument("--output", required=True)

    bind_cmd = sub.add_parser("bind", add_help=False, allow_abbrev=False)
    bind_cmd.add_argument("--freeze", required=True)
    bind_cmd.add_argument("--reviewed-sha", required=True)
    bind_cmd.add_argument("--cmd-start", required=True)
    bind_cmd.add_argument("--cmd-end", required=True)
    bind_cmd.add_argument("--output", required=True)

    verify_cmd = sub.add_parser("verify", add_help=False, allow_abbrev=False)
    verify_cmd.add_argument("--freeze", required=True)
    verify_cmd.add_argument("--binding", required=True)
    verify_cmd.add_argument("--reviewed-sha", required=True)
    verify_cmd.add_argument("--output", required=True)
    return parser


def _report(error: C4AcceptanceError) -> int:
    print(f"{error.code}: {error}", file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.command == "freeze":
            freeze(
                approved_record=args.approved_record,
                reviewed_sha=args.reviewed_sha,
                receipt=args.receipt,
                frontend_origin=args.frontend_origin,
                api_origin=args.api_origin,
                basin_id=args.basin_id,
                segment_id=args.segment_id,
                output=args.output,
            )
            print("C4 production acceptance freeze PASS")
            return 0
        if args.command == "bind":
            bind(
                freeze_path=args.freeze,
                reviewed_sha=args.reviewed_sha,
                cmd_start=args.cmd_start,
                cmd_end=args.cmd_end,
                output=args.output,
            )
            print("C4 production acceptance bind PASS")
            return 0
        verify(
            freeze_path=args.freeze,
            binding_path=args.binding,
            reviewed_sha=args.reviewed_sha,
            output=args.output,
        )
        print("C4 production acceptance verify PASS")
        return 0
    except C4AcceptanceError as error:
        return _report(error)


if __name__ == "__main__":
    raise SystemExit(main())
