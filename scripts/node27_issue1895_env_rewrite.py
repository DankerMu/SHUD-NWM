#!/usr/bin/env python3
"""Rewrite governed keys in the private node-27 cold-residency env.

The owner preserves comments, blanks, commented/unassigned optional keys, and
unrelated assignments. ``DATABASE_URL`` is copied byte-for-byte and never
printed. Publication is a mode-0600 no-clobber atomic replace.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from packages.common.node27_issue1895_env import (
    G4_GOVERNED_KEYS,
    G5_GOVERNED_KEYS,
    rewrite_cold_env_file,
    validate_canonical_positive_decimal,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, type=Path)
    parser.add_argument("--cold-reserve-bytes")
    parser.add_argument("--wal-reserve-bytes")
    parser.add_argument("--per-tick-bound")
    parser.add_argument("--container-exec-uid")
    parser.add_argument("--container-exec-gid")
    parser.add_argument("--device-identity")
    parser.add_argument("--lag-seconds", default=None)
    parser.add_argument("--stage", choices=("g4", "g5"), default="g4")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.stage == "g5":
        if not args.device_identity or not args.lag_seconds:
            raise SystemExit("G5 rewrite requires --device-identity and --lag-seconds")
        updates = {
            "NODE27_COLD_RESIDENCY_DEVICE_IDENTITY": args.device_identity,
            "NODE27_COLD_RESIDENCY_LAG_SECONDS": validate_canonical_positive_decimal(
                args.lag_seconds, label="NODE27_COLD_RESIDENCY_LAG_SECONDS"
            ),
        }
        governed = G5_GOVERNED_KEYS
        require_unassigned = False
    else:
        missing = [
            name
            for name, value in (
                ("--cold-reserve-bytes", args.cold_reserve_bytes),
                ("--wal-reserve-bytes", args.wal_reserve_bytes),
                ("--per-tick-bound", args.per_tick_bound),
                ("--container-exec-uid", args.container_exec_uid),
                ("--container-exec-gid", args.container_exec_gid),
            )
            if not value
        ]
        if missing:
            raise SystemExit(f"G4 rewrite requires {', '.join(missing)}")
        updates = {
            "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": args.cold_reserve_bytes,
            "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": args.wal_reserve_bytes,
            "NODE27_COLD_RESIDENCY_PER_TICK_BOUND": args.per_tick_bound,
            "NODE27_COLD_RESIDENCY_CONTAINER_EXEC_UID": args.container_exec_uid,
            "NODE27_COLD_RESIDENCY_CONTAINER_EXEC_GID": args.container_exec_gid,
        }
        governed = G4_GOVERNED_KEYS
        require_unassigned = True
    try:
        rewrite_cold_env_file(
            args.path,
            updates=updates,
            governed_keys=governed,
            require_device_identity_unassigned=require_unassigned,
        )
    except Issue1895ReadinessError as error:
        print(redact_text(f"{error.code}: {error}"), file=sys.stderr)
        return 1
    os.chmod(args.path, 0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
