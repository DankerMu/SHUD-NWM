#!/usr/bin/env python3
"""G6 filesystem reconciliation for one moved group's member bytes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from packages.common.node27_issue1895_fs import reconcile_moved_group_filesystem
from packages.common.node27_issue1895_private_receipt import read_held_private_json
from packages.common.node27_issue1895_receipt import unique_migrated_observation
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--hot-avail-before", required=True, type=int)
    parser.add_argument("--hot-avail-after", required=True, type=int)
    parser.add_argument("--cold-avail-before", required=True, type=int)
    parser.add_argument("--cold-avail-after", required=True, type=int)
    parser.add_argument("--allocation-granularity-bytes", type=int, default=4096)
    parser.add_argument("--concurrent-noise-bytes", type=int, default=64 * 1024 * 1024)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        try:
            _raw, receipt, _facts = read_held_private_json(
                args.receipt,
                label="filesystem receipt",
                stage="filesystem",
                unreadable_code="RECEIPT_SELECTED_INVALID",
                identity_code="RECEIPT_SELECTED_INVALID",
                toctou_code="RECEIPT_SELECTED_INVALID",
                json_code="RECEIPT_SELECTED_INVALID",
            )
        except Issue1895ReadinessError as error:
            if error.code.startswith("READINESS_INPUT_"):
                raise Issue1895ReadinessError(
                    "receipt selected is missing",
                    code="RECEIPT_SELECTED_INVALID",
                    stage="filesystem",
                ) from error
            raise
        observation = unique_migrated_observation(receipt)
        after = observation.get("after") if isinstance(observation.get("after"), dict) else {}
        members = after.get("members") or observation.get("members") or []
        result = reconcile_moved_group_filesystem(
            members=members,
            hot_avail_before=args.hot_avail_before,
            hot_avail_after=args.hot_avail_after,
            cold_avail_before=args.cold_avail_before,
            cold_avail_after=args.cold_avail_after,
            allocation_granularity_bytes=args.allocation_granularity_bytes,
            concurrent_noise_bytes=args.concurrent_noise_bytes,
        )
    except Issue1895ReadinessError as error:
        print(redact_text(f"{error.code}: {error}"), file=sys.stderr)
        return 1
    print(json.dumps({"approved": result["approved"], "moved_member_bytes": result["moved_member_bytes"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
