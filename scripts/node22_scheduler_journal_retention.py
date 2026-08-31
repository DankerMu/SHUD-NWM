#!/usr/bin/env python
"""Cold-archive expired node-22 file-journal cycles.

The checked-in executable path is retained for the systemd unit.  Archive,
restore, configuration, retention orchestration, and receipt ownership live in
the scheduler-journal owner modules.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from services.orchestrator import scheduler_journal_archive as archive
from services.orchestrator import scheduler_journal_restore as restore
from services.orchestrator import scheduler_journal_retention as retention
from services.orchestrator.retention_frontier import FrontierReadResult
from services.orchestrator.scheduler_journal_retention_types import (
    SCHEMA_VERSION,
    ReceiptReservation,
    RetentionFailure,
    _iso,
)

# Public command compatibility surface.  The thin entrypoint deliberately
# exposes concrete owner seams instead of reimplementing policy.
config_from_env = retention.config_from_env
run_retention = retention.run_retention
reserve_receipt = retention.reserve_receipt
finalize_receipt = retention.finalize_receipt
verify_and_restore = restore.verify_and_restore

# Test and operator compatibility for stable archive constants/helpers.
# ``FrontierReadResult`` is deliberately an exported input type for callers.
FrontierReadResult = FrontierReadResult
ArchiveIdentity = archive.ArchiveIdentity
SchedulerJournalRetentionConfig = retention.SchedulerJournalRetentionConfig
ARCHIVE_NAME = archive.ARCHIVE_NAME
MANIFEST_NAME = archive.MANIFEST_NAME
MANIFEST_SCHEMA_VERSION = archive.MANIFEST_SCHEMA_VERSION
MAX_ARCHIVE_MEMBER_BYTES = archive.MAX_ARCHIVE_MEMBER_BYTES
MAX_ARCHIVE_CYCLE_MEMBERS = retention.MAX_ARCHIVE_CYCLE_MEMBERS
MAX_ARCHIVE_CYCLE_BYTES = retention.MAX_ARCHIVE_CYCLE_BYTES
MAX_ARCHIVE_BYTES = retention.MAX_ARCHIVE_BYTES
_archive_path = archive._archive_paths
_archive_paths = archive._archive_paths
_archive_commands = archive._archive_commands
_run_archive_toolchain = archive._run_archive_toolchain
_publish_archive = archive._publish_archive
_publish_bundle_directory = archive._publish_bundle_directory
_publication_payload = archive._publication_payload
_validated_manifest_members = archive._validated_manifest_members
_PUBLICATION_MARKER = archive._PUBLICATION_MARKER


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-root")
    parser.add_argument("--archive-root")
    parser.add_argument("--evidence-root")
    parser.add_argument("--retention-days", type=int)
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--max-depth", type=int)
    parser.add_argument("--max-cycle-members", type=int)
    parser.add_argument("--max-cycle-bytes", type=int)
    parser.add_argument("--max-archive-bytes", type=int)
    subparsers = parser.add_subparsers(dest="command")
    restore_parser = subparsers.add_parser("verify-restore", help="verify, stage, and no-clobber restore one archive")
    restore_parser.add_argument("--journal-root", required=True)
    restore_parser.add_argument("--archive-root", required=True)
    restore_parser.add_argument("--source-id", required=True)
    restore_parser.add_argument("--cycle", required=True)
    restore_parser.add_argument("--stage-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "verify-restore":
        try:
            payload = verify_and_restore(
                journal_root=args.journal_root,
                archive_root=args.archive_root,
                source_id=args.source_id,
                cycle=args.cycle,
                stage_root=args.stage_root,
            )
        except (RetentionFailure, ValueError) as error:
            print(
                json.dumps(
                    {"schema_version": SCHEMA_VERSION, "status": "blocked", "reason": str(error)},
                    sort_keys=True,
                )
            )
            return 2
        print(json.dumps(payload, sort_keys=True))
        return 0
    config, blockers = config_from_env(args)
    now = datetime.now(UTC)
    if config is None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "started_at": _iso(now),
            "finished_at": _iso(now),
            "status": "preflight_blocked",
            "preflight_blockers": blockers,
        }
        print(json.dumps(payload, sort_keys=True))
        return 2
    reservation: ReceiptReservation | None = None
    if config.enabled and not config.dry_run:
        try:
            reservation = reserve_receipt(config, now=now)
        except RetentionFailure as error:
            print(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "status": "preflight_blocked",
                        "preflight_blockers": [error.reason],
                    },
                    sort_keys=True,
                )
            )
            return 2
    payload = run_retention(config, now=now, receipt_reservation=reservation)
    try:
        receipt_path = (
            finalize_receipt(config, reservation, payload, now=now)
            if reservation is not None
            else retention._write_receipt(config, payload, now)
        )
    except RetentionFailure as error:
        payload["receipt_write_error"] = error.reason
        print(json.dumps(payload, sort_keys=True))
        return 2
    payload["receipt_path"] = str(receipt_path)
    print(json.dumps(payload, sort_keys=True))
    failed_cycles = payload["counts"]["blocked"] + payload["counts"]["partial"]
    return 2 if payload["preflight_blockers"] or failed_cycles else 0


if __name__ == "__main__":
    raise SystemExit(main())
