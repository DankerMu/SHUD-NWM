#!/usr/bin/env python3
"""Parse and pin the live PG 15.2 / Timescale 2.10.2 engine row."""

from __future__ import annotations

import argparse
import sys

from packages.common.node27_issue1895_engine import assert_engine_sql_row
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--row", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        server, timescale = assert_engine_sql_row(args.row)
    except Issue1895ReadinessError as error:
        print(redact_text(f"{error.code}: {error}"), file=sys.stderr)
        return 1
    print(f"engine OK: {server.split()[0]}|{timescale}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
