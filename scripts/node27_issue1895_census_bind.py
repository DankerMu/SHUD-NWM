#!/usr/bin/env python3
"""Bind the G5 pre-movement census against the original six-group preimage."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packages.common.node27_issue1895_census_bind import bind_pre_movement_census
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", required=True, type=Path)
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--bracket", required=True, type=Path)
    parser.add_argument("--reviewed-sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bind_pre_movement_census(
            current_path=args.current,
            original_path=args.original,
            expected_digest=args.digest,
            bracket_path=args.bracket,
            reviewed_sha=args.reviewed_sha,
        )
    except Issue1895ReadinessError as error:
        print(redact_text(f"{error.code}: {error}"), file=sys.stderr)
        return 1
    print("pre-movement census OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
