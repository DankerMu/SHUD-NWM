#!/usr/bin/env python3
"""Bind one G6 sequential tick receipt to the ordered baseline keys."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packages.common.node27_issue1895_private_receipt import read_held_private_json
from packages.common.node27_issue1895_receipt import assert_sequential_tick_receipt
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_text


def _load_object(path: Path, *, label: str) -> dict:
    try:
        _raw, payload, _facts = read_held_private_json(
            path,
            label=label,
            stage="receipt",
            unreadable_code="RECEIPT_KEYS_INVALID" if label == "census" else "RECEIPT_JSON_INVALID",
            identity_code="RECEIPT_KEYS_INVALID" if label == "census" else "RECEIPT_JSON_INVALID",
            toctou_code="RECEIPT_KEYS_INVALID" if label == "census" else "RECEIPT_JSON_INVALID",
            json_code="RECEIPT_KEYS_INVALID" if label == "census" else "RECEIPT_JSON_INVALID",
        )
    except Issue1895ReadinessError as error:
        if error.code.startswith("READINESS_INPUT_"):
            raise Issue1895ReadinessError(
                f"{label} is not a private identity-bound JSON object",
                code="RECEIPT_KEYS_INVALID" if label == "census" else "RECEIPT_JSON_INVALID",
                stage="receipt",
            ) from error
        raise
    if not isinstance(payload, dict):
        raise Issue1895ReadinessError(
            f"{label} is not an object",
            code="RECEIPT_KEYS_INVALID" if label == "census" else "RECEIPT_JSON_INVALID",
            stage="receipt",
        )
    return payload


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
        census = _load_object(args.census, label="census")
        receipt = _load_object(args.receipt, label="receipt")
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
