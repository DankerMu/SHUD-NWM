#!/usr/bin/env python
"""Manually advanced offline PGDATA relocation. Default action is plan-only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg2

from packages.common.node27_pgdata_migrate import Migration, MigrationError
from packages.common.node27_timeseries_lifecycle_lock import LifecycleLockContended, LifecycleLockError
from packages.common.safe_fs import SafeFilesystemError


def _path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("must be an absolute path")
    return str(path)


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _smart(value: str) -> tuple[str, str]:
    device, separator, path = value.partition("=")
    if not separator or not device.startswith("/dev/") or not path.startswith("/"):
        raise argparse.ArgumentTypeError("must be DEVICE=/ABSOLUTE_PATH")
    return device, path


def parser() -> argparse.ArgumentParser:
    tool = argparse.ArgumentParser(description=__doc__)
    tool.add_argument(
        "--action", default="plan", choices=("plan", "prepare", "copy", "activate", "rollback", "release")
    )
    tool.add_argument("--workspace", type=_path, required=True)
    tool.add_argument("--enforce", action="store_true")
    tool.add_argument("--source-container")
    tool.add_argument("--source-pgdata", type=_path)
    tool.add_argument("--target-pgdata", type=_path)
    tool.add_argument("--reserve-bytes", type=_positive)
    tool.add_argument("--mdadm-evidence", type=_path)
    tool.add_argument("--smart-evidence", type=_smart, action="append", default=[])
    tool.add_argument("--disposable-root", type=_path)
    tool.add_argument("--database")
    tool.add_argument("--admin-role")
    tool.add_argument("--reader-dsn-file", type=_path)
    tool.add_argument("--writer-dsn-file", type=_path)
    tool.add_argument("--drain-timeout", type=_positive)
    return tool


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    overrides = {
        "source_container": args.source_container,
        "source_pgdata": args.source_pgdata,
        "target_pgdata": args.target_pgdata,
        "reserve_bytes": args.reserve_bytes,
        "mdadm_evidence": args.mdadm_evidence,
        "smart_evidence": dict(args.smart_evidence),
        "disposable_root": args.disposable_root,
        "database": args.database,
        "admin_role": args.admin_role,
        "reader_dsn_file": args.reader_dsn_file,
        "writer_dsn_file": args.writer_dsn_file,
        "drain_timeout": args.drain_timeout,
    }
    try:
        result = Migration(Path(args.workspace)).run(args.action, enforce=args.enforce, overrides=overrides)
    except MigrationError as error:
        print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    except (OSError, ValueError, psycopg2.Error, SafeFilesystemError, LifecycleLockError, LifecycleLockContended):
        print(json.dumps({"ok": False, "error": "migration input or IO unavailable; state retained"}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
