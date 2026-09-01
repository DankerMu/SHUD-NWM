"""Shared contracts for scheduler-journal retention owners."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "nhms.node22_scheduler_journal_retention.v1"
MANIFEST_SCHEMA_VERSION = "nhms.node22_scheduler_journal_archive_manifest.v1"
PUBLICATION_SCHEMA_VERSION = "nhms.node22_scheduler_journal_archive_publication.v1"
DEFAULT_RETENTION_DAYS = 90
ARCHIVE_NAME = "journal-cycle.tar.zst"
MANIFEST_NAME = "manifest.json"
RECEIPT_DIRECTORY = "retention"
MAX_RECEIPT_CYCLES = 256
MAX_RECEIPT_MEMBERS = 1024
MAX_ARCHIVE_MEMBER_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_CYCLE_MEMBERS = 1024
MAX_ARCHIVE_CYCLE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
ARCHIVE_TIMEOUT_SECONDS = 300
HOT_SURFACES = ("latest", "journal", "pipeline-events")
_BUNDLE_DIRECTORY = "bundle"
_PUBLICATION_MARKER = "publication.json"
_RECEIPT_STATUS_INTENT = "intent"
_RECEIPT_STATUS_FINAL = "final"


@dataclass(frozen=True)
class SchedulerJournalRetentionConfig:
    journal_root: Path
    archive_root: Path
    evidence_root: Path
    allowed_roots: tuple[Path, ...]
    retention_days: int
    enabled: bool
    dry_run: bool
    lookback_hours: int
    cycle_lag_hours: int
    allowed_cycle_hours: tuple[int, ...]
    max_files: int
    max_depth: int
    max_cycle_members: int = MAX_ARCHIVE_CYCLE_MEMBERS
    max_cycle_bytes: int = MAX_ARCHIVE_CYCLE_BYTES
    max_archive_bytes: int = MAX_ARCHIVE_BYTES


@dataclass(frozen=True)
class ArchiveIdentity:
    archive_sha256: str
    manifest: dict[str, Any]


@dataclass(frozen=True)
class ReceiptReservation:
    path: Path
    invocation_id: str


class RetentionFailure(RuntimeError):
    """A stable per-cycle or preflight refusal token."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cycle_stamp(value: datetime) -> str:
    return _utc(value).strftime("%Y%m%d%H")


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _stable_archive_identity(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Marker-bound completion identity: bytes/members, not retry provenance."""

    return {
        "schema_version": manifest.get("schema_version"),
        "source_id": manifest.get("source_id"),
        "cycle_time": manifest.get("cycle_time"),
        "mode": manifest.get("mode"),
        "archive_sha256": manifest.get("archive_sha256"),
        "members": manifest.get("members"),
    }


def _stable_archive_identity_matches(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return _canonical_json(_stable_archive_identity(left)) == _canonical_json(_stable_archive_identity(right))
