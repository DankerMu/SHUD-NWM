#!/usr/bin/env python3
"""Produce one private C1 exact-SHA display-runtime PASS receipt."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packages.common.node27_issue1895_display_runtime import observe_display_runtime
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display-env", required=True, type=Path)
    parser.add_argument("--receipt-path", required=True, type=Path)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--reviewed-sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        observe_display_runtime(
            display_env=args.display_env,
            receipt_path=args.receipt_path,
            head_sha=args.head_sha,
            reviewed_sha=args.reviewed_sha,
        )
    except Issue1895ReadinessError as error:
        print(redact_text(f"{error.code}: {error}"), file=sys.stderr)
        return 1
    print("C1 display runtime PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
