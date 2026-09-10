#!/usr/bin/env python3
"""PASS-only binder for one private C1 display runtime receipt."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packages.common.node27_issue1895_display_runtime import bind_c1_receipt
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--reviewed-sha", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--cmd-start", required=True)
    parser.add_argument("--cmd-end", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bind_c1_receipt(
            args.receipt,
            reviewed_sha=args.reviewed_sha,
            expected_port=args.port,
            cmd_start=args.cmd_start,
            cmd_end=args.cmd_end,
        )
    except Issue1895ReadinessError as error:
        print(redact_text(f"{error.code}: {error}"), file=sys.stderr)
        return 1
    print("C1 binder PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
