#!/usr/bin/env python3
"""Bind RC_DSN_FILE from the private display.env nhms_display_ro DSN.

Never prints the DSN. Refuses if the target already exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packages.common.node27_issue1895_dsn import bind_rc_dsn_file, read_display_env_text, resolve_readonly_dsn
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display-env", required=True, type=Path)
    parser.add_argument("--rc-dsn-file", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        text = read_display_env_text(args.display_env)
        # Explicit display.env provenance wins over every ambient readonly key.
        # The caller deliberately passes an empty mapping: inherited writer or
        # stale readonly DSNs must never redirect this private bind.
        dsn = resolve_readonly_dsn(display_env_text=text, environ={})
        bind_rc_dsn_file(args.rc_dsn_file, dsn=dsn)
    except Issue1895ReadinessError as error:
        print(redact_text(f"{error.code}: {error}"), file=sys.stderr)
        return 1
    except OSError:
        print("DSN_IO: display.env is unreadable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
