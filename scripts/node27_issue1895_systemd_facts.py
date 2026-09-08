#!/usr/bin/env python3
"""Validate captured systemd timer/service facts for the natural tick."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.node27_issue1895_watermark import assert_systemd_invocation_facts, parse_systemctl_show
from packages.common.redaction import redact_text
from packages.common.safe_fs import SafeFilesystemError
from packages.common.safe_fs_publication import write_bytes_no_follow_exclusive


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timer-show", required=True, type=Path)
    parser.add_argument("--service-show", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        timer_text = args.timer_show.read_text(encoding="utf-8")
        service_text = args.service_show.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        print("SYSTEMD_FACTS_UNAVAILABLE", file=sys.stderr)
        return 1
    try:
        proven = assert_systemd_invocation_facts(
            timer=parse_systemctl_show(timer_text),
            service=parse_systemctl_show(service_text),
        )
        write_bytes_no_follow_exclusive(
            args.output,
            (json.dumps(proven, sort_keys=True) + "\n").encode("utf-8"),
            containment_root=args.output.parent,
            require_durable_create=True,
            mode=0o600,
        )
    except (FileExistsError, SafeFilesystemError):
        print("SYSTEMD_FACTS_REFUSED", file=sys.stderr)
        return 1
    except Issue1895ReadinessError as error:
        print(redact_text(f"{error.code}: {error}"), file=sys.stderr)
        return 1
    print("systemd facts OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
