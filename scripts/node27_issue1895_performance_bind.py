#!/usr/bin/env python3
"""PASS-only current-run binder for the #1895 performance receipt+commit pair."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packages.common.node27_issue1895_commit import bind_performance_artifacts
from packages.common.node27_issue1895_performance import format_refusal
from packages.common.node27_issue1895_private_receipt import read_held_private_text
from packages.common.node27_issue1895_types import Issue1895ReadinessError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--commit", required=True, type=Path)
    parser.add_argument("--reviewed-sha", required=True)
    parser.add_argument("--basin-id", required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--cmd-start")
    parser.add_argument("--cmd-end")
    parser.add_argument("--bracket", type=Path)
    return parser


def _bracket_instants(args: argparse.Namespace) -> tuple[str | None, str | None]:
    if args.bracket is not None:
        try:
            text = read_held_private_text(
                args.bracket,
                label="performance bracket",
                stage="performance",
                unreadable_code="COMMIT_BRACKET",
                identity_code="COMMIT_BRACKET",
                toctou_code="COMMIT_BRACKET",
            )
        except Issue1895ReadinessError as error:
            if error.code.startswith("READINESS_INPUT_"):
                raise Issue1895ReadinessError(
                    "command bracket is incomplete",
                    code="COMMIT_BRACKET",
                    stage="performance",
                ) from error
            raise
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < 2:
            raise Issue1895ReadinessError(
                "command bracket is incomplete",
                code="COMMIT_BRACKET",
                stage="performance",
            )
        return lines[0], lines[1]
    return args.cmd_start, args.cmd_end


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        start, end = _bracket_instants(args)
        bind_performance_artifacts(
            args.receipt,
            args.commit,
            expected_sha=args.reviewed_sha,
            expected_basin=args.basin_id,
            expected_segment=args.segment_id,
            cmd_start=start,
            cmd_end=end,
        )
    except Issue1895ReadinessError as error:
        print(format_refusal(error), file=sys.stderr)
        return 1
    print("performance PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
