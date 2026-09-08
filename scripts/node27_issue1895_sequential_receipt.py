#!/usr/bin/env python3
"""Bind one G6 sequential tick receipt to the ordered baseline keys."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from packages.common.node27_issue1895_receipt import assert_sequential_tick_receipt
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--census", required=True, type=Path)
    parser.add_argument("--call-index", required=True, type=int)
    parser.add_argument("--migrate-outcome", default="migrated")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        census = json.loads(args.census.read_text(encoding="utf-8"))
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
        keys = census.get("group_keys")
        if not isinstance(keys, list):
            raise Issue1895ReadinessError(
                "census group_keys are missing",
                code="RECEIPT_KEYS_INVALID",
                stage="receipt",
            )
        assert_sequential_tick_receipt(
            receipt,
            ordered_keys=[str(key) for key in keys],
            call_index=args.call_index,
            migrate_outcome=args.migrate_outcome,
        )
    except Issue1895ReadinessError as error:
        print(redact_text(f"{error.code}: {error}"), file=sys.stderr)
        return 1
    print("sequential receipt OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
