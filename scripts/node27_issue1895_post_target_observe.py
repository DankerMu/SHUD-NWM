#!/usr/bin/env python3
"""Post-target catalog observer for G8. Never reuses the pre-target census CLI."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from packages.common.display_watermark import DisplayWatermarkError
from packages.common.node27_issue1895_dsn import resolve_readonly_dsn
from packages.common.node27_issue1895_env import validate_canonical_positive_decimal
from packages.common.node27_issue1895_post_target import run_post_target_observation
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--reviewed-sha", required=True)
    parser.add_argument("--lag-seconds", required=True)
    parser.add_argument("--display-env", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        lag = int(validate_canonical_positive_decimal(args.lag_seconds, label="lag_seconds"))
        display_text = None
        if args.display_env is not None:
            display_text = args.display_env.read_text(encoding="utf-8")
        dsn = resolve_readonly_dsn(display_env_text=display_text, environ=dict(os.environ))
        run_post_target_observation(
            baseline_path=args.baseline,
            output_path=args.output,
            reviewed_sha=args.reviewed_sha,
            lag_seconds=lag,
            dsn=dsn,
        )
    except DisplayWatermarkError:
        print("WATERMARK_UNAVAILABLE", file=sys.stderr)
        return 1
    except Issue1895ReadinessError as error:
        print(redact_text(f"{error.code}: {error}"), file=sys.stderr)
        return 1
    print("post-target observe OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
