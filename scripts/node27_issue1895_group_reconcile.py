#!/usr/bin/env python3
"""G8 exact six-group reconciliation plus natural-tick receipt identity.

Binds subsequent-tick selected/moved/deferred semantics to the shipping
receipt, never a catalog row count.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packages.common.node27_issue1895_private_receipt import read_held_private_json
from packages.common.node27_issue1895_timer import (
    COMPRESSION_SERVICE,
    assert_exact_cold_groups,
    assert_natural_receipt_identity,
    assert_natural_tick_selection,
    persist_baseline_groups,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--observed", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--reviewed-sha", required=True)
    parser.add_argument("--expected-cutoff", required=True)
    parser.add_argument("--expected-watermark", required=True)
    parser.add_argument("--invoked-unit", default=COMPRESSION_SERVICE)
    parser.add_argument("--remaining-all-source-keys", default="")
    parser.add_argument("--newly-terminal-key", default="")
    return parser


def _load(path: Path) -> dict:
    try:
        _raw, document, _facts = read_held_private_json(
            path,
            label="group artifact",
            stage="census",
            unreadable_code="GROUP_ARTIFACT_INVALID",
            identity_code="GROUP_ARTIFACT_INVALID",
            toctou_code="GROUP_ARTIFACT_INVALID",
            json_code="GROUP_ARTIFACT_INVALID",
        )
    except Issue1895ReadinessError as error:
        if error.code.startswith("READINESS_INPUT_"):
            raise Issue1895ReadinessError(
                "artifact is not a private identity-bound JSON object",
                code="GROUP_ARTIFACT_INVALID",
                stage="census",
            ) from error
        raise
    if not isinstance(document, dict):
        raise Issue1895ReadinessError(
            "artifact is not an object",
            code="GROUP_ARTIFACT_INVALID",
            stage="census",
        )
    return document


def _groups(document: dict) -> list:
    groups = document.get("groups")
    if not isinstance(groups, list):
        raise Issue1895ReadinessError(
            "artifact groups are missing",
            code="GROUP_ARTIFACT_INVALID",
            stage="census",
        )
    return groups


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        baseline_doc = _load(args.baseline)
        observed_doc = _load(args.observed)
        receipt = _load(args.receipt)
        persist_baseline_groups(_groups(baseline_doc))
        assert_exact_cold_groups(_groups(observed_doc), baseline=_groups(baseline_doc))
        assert_natural_receipt_identity(
            receipt,
            reviewed_sha=args.reviewed_sha,
            expected_cutoff=args.expected_cutoff,
            expected_watermark=args.expected_watermark,
            invoked_unit=args.invoked_unit,
        )
        remaining = [item for item in args.remaining_all_source_keys.split("\n") if item]
        newly = [item for item in args.newly_terminal_key.split("\n") if item]
        baseline_keys = [str(group.get("key") or "") for group in _groups(baseline_doc)]
        assert_natural_tick_selection(
            receipt,
            remaining_complete_source_keys=remaining,
            newly_terminal_keys=newly,
            baseline_keys=baseline_keys,
        )
    except Issue1895ReadinessError as error:
        print(error.code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
