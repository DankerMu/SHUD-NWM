#!/usr/bin/env python3
"""Independently persist W8/C8 from the display watermark and cold lag."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from packages.common.display_watermark import DisplayWatermarkError
from packages.common.node27_issue1895_dsn import read_display_env_text, resolve_readonly_dsn
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.node27_issue1895_watermark import observe_current_cutoff
from packages.common.redaction import redact_text
from packages.common.safe_fs import SafeFilesystemError
from packages.common.safe_fs_publication import write_bytes_no_follow_exclusive


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--lag-seconds", required=True)
    parser.add_argument("--display-env", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        display_text = None
        if args.display_env is not None:
            display_text = read_display_env_text(args.display_env)
        dsn = resolve_readonly_dsn(display_env_text=display_text, environ=dict(os.environ))
        document = observe_current_cutoff(dsn=dsn, lag_seconds=args.lag_seconds)
        write_bytes_no_follow_exclusive(
            args.output,
            (json.dumps(document, sort_keys=True) + "\n").encode("utf-8"),
            containment_root=args.output.parent,
            require_durable_create=True,
            mode=0o600,
        )
    except (FileExistsError, SafeFilesystemError):
        print("WATERMARK_UNAVAILABLE", file=sys.stderr)
        return 1
    except DisplayWatermarkError:
        print("WATERMARK_UNAVAILABLE", file=sys.stderr)
        return 1
    except Issue1895ReadinessError as error:
        print(redact_text(f"{error.code}: {error}"), file=sys.stderr)
        return 1
    print("watermark OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
