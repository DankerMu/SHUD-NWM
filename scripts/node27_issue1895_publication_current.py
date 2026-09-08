#!/usr/bin/env python3
"""Produce one private C3 current publication-and-display PASS receipt."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from packages.common.node27_issue1895_publication_current import (
    CANONICAL_SCHEDULER_REGISTRY_MANIFEST,
    observe_current_publication,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display-env", required=True, type=Path)
    parser.add_argument("--basin-id", required=True)
    parser.add_argument("--baseline-valid-times", required=True, type=Path)
    parser.add_argument("--c4-receipt", required=True, type=Path)
    parser.add_argument("--receipt-path", required=True, type=Path)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--reviewed-sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dsn = str(os.environ.get("NHMS_DISPLAY_READONLY_DATABASE_URL") or "").strip()
    if not dsn:
        print("C3_DSN_MISSING: private readonly DSN is required only for this owner", file=sys.stderr)
        return 1
    try:
        observe_current_publication(
            display_env=args.display_env,
            registry=CANONICAL_SCHEDULER_REGISTRY_MANIFEST,
            basin_id=args.basin_id,
            baseline_valid_times=args.baseline_valid_times,
            c4_receipt=args.c4_receipt,
            receipt_path=args.receipt_path,
            head_sha=args.head_sha,
            reviewed_sha=args.reviewed_sha,
            dsn=dsn,
        )
    except Issue1895ReadinessError as error:
        print(redact_text(f"{error.code}: {error}"), file=sys.stderr)
        return 1
    print("C3 current publication PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
