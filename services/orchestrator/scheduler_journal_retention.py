"""Scheduler-journal retention orchestration and durable receipts."""

from __future__ import annotations

import argparse
import json
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_durable_no_follow,
)
from packages.common.safe_fs_publication import write_bytes_no_follow_exclusive
from services.orchestrator import scheduler_journal_archive as archive
from services.orchestrator.file_orchestration_journal import (
    MAX_FILE_JOURNAL_DISCOVERED_FILES,
    MAX_FILE_JOURNAL_SCAN_DEPTH,
    FileJournalRetentionMember,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.retention_frontier import (
    FrontierReadResult,
    max_age_from_env,
    read_latest_pass_frontier,
)
from services.orchestrator.scheduler_journal_retention_types import (
    _RECEIPT_STATUS_FINAL,
    _RECEIPT_STATUS_INTENT,
    DEFAULT_RETENTION_DAYS,
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_CYCLE_BYTES,
    MAX_ARCHIVE_CYCLE_MEMBERS,
    MAX_ARCHIVE_MEMBER_BYTES,
    MAX_RECEIPT_CYCLES,
    MAX_RECEIPT_MEMBERS,
    RECEIPT_DIRECTORY,
    SCHEMA_VERSION,
    ReceiptReservation,
    RetentionFailure,
    SchedulerJournalRetentionConfig,
    _iso,
    _utc,
)

# Compatibility seams stay here while implementations are owned by the archive module.
_publish_archive = archive.publish_archive
_validated_manifest_members = archive.validated_manifest_members
_canonical_json = archive._canonical_json


def _valid_archive_limit(value: int) -> bool:
    return type(value) is int and 0 < value <= MAX_ARCHIVE_BYTES



def _bool_env(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RetentionFailure(f"{name.lower()}_invalid")


def _required_positive_int(name: str, value: str | None, *, default: int | None = None) -> int:
    raw = value if value is not None else os.getenv(name)
    if raw is None or not raw.strip():
        if default is not None:
            return default
        raise RetentionFailure(f"{name.lower()}_missing")
    try:
        parsed = int(raw.strip())
    except ValueError as error:
        raise RetentionFailure(f"{name.lower()}_invalid") from error
    if parsed <= 0:
        raise RetentionFailure(f"{name.lower()}_invalid")
    return parsed


def _parse_allowed_cycle_hours(raw: str | None) -> tuple[int, ...]:
    if raw is None or not raw.strip():
        raise RetentionFailure("nhms_scheduler_allowed_cycle_hours_utc_missing")
    parts = [part.strip() for part in raw.split(",")]
    if not parts or any(not part for part in parts):
        raise RetentionFailure("nhms_scheduler_allowed_cycle_hours_utc_invalid")
    try:
        hours = tuple(sorted({int(part) for part in parts}))
    except ValueError as error:
        raise RetentionFailure("nhms_scheduler_allowed_cycle_hours_utc_invalid") from error
    if not hours or any(hour < 0 or hour > 23 for hour in hours):
        raise RetentionFailure("nhms_scheduler_allowed_cycle_hours_utc_invalid")
    return hours


def _largest_cycle_gap(hours: Sequence[int]) -> int:
    if len(hours) == 1:
        return 24
    return max((hours[(index + 1) % len(hours)] - hour) % 24 for index, hour in enumerate(hours))


def _path_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_existing_directory(value: str | Path, *, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RetentionFailure(f"{field}_not_absolute")
    try:
        info = path.lstat()
    except OSError as error:
        raise RetentionFailure(f"{field}_unavailable") from error
    if stat.S_ISLNK(info.st_mode):
        raise RetentionFailure(f"{field}_symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise RetentionFailure(f"{field}_not_directory")
    try:
        ensure_directory_no_follow(path)
    except (OSError, SafeFilesystemError) as error:
        raise RetentionFailure(f"{field}_unsafe") from error
    return path


def _safe_archive_root(value: str | Path, *, journal_root: Path, allowed_roots: Sequence[Path]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RetentionFailure("archive_root_not_absolute")
    if path == Path("/"):
        raise RetentionFailure("archive_root_is_root")
    if _path_under(path, journal_root) or _path_under(journal_root, path):
        raise RetentionFailure("archive_root_overlaps_journal")
    if not any(_path_under(path, root) for root in allowed_roots):
        raise RetentionFailure("archive_root_outside_allowed_roots")
    try:
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise RetentionFailure("archive_root_symlink")
            if not stat.S_ISDIR(info.st_mode):
                raise RetentionFailure("archive_root_not_directory")
        else:
            parent = path.parent
            parent_info = parent.lstat()
            if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
                raise RetentionFailure("archive_root_parent_unsafe")
        ensure_directory_no_follow(path)
    except RetentionFailure:
        raise
    except (OSError, SafeFilesystemError) as error:
        raise RetentionFailure("archive_root_unsafe") from error
    return path


def _allowed_roots(raw: str | None) -> tuple[Path, ...]:
    if raw is None or not raw.strip():
        raise RetentionFailure("nhms_scheduler_allowed_roots_missing")
    roots: list[Path] = []
    for item in raw.split(":"):
        text = item.strip()
        if not text:
            raise RetentionFailure("nhms_scheduler_allowed_roots_invalid")
        root = _safe_existing_directory(text, field="allowed_root")
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def config_from_env(args: argparse.Namespace) -> tuple[SchedulerJournalRetentionConfig | None, list[str]]:
    """Resolve all destructive inputs before a journal or archive write occurs."""

    try:
        allowed_roots = _allowed_roots(os.getenv("NHMS_SCHEDULER_ALLOWED_ROOTS"))
        journal_raw = args.journal_root or os.getenv("NHMS_SCHEDULER_JOURNAL_ROOT")
        if journal_raw is None or not journal_raw.strip():
            raise RetentionFailure("journal_root_missing")
        journal_root = _safe_existing_directory(journal_raw, field="journal_root")
        if not any(_path_under(journal_root, root) for root in allowed_roots):
            raise RetentionFailure("journal_root_outside_allowed_roots")
        archive_raw = args.archive_root or os.getenv("NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT")
        if archive_raw is None or not archive_raw.strip():
            raise RetentionFailure("archive_root_missing")
        archive_root = _safe_archive_root(archive_raw, journal_root=journal_root, allowed_roots=allowed_roots)
        evidence_raw = args.evidence_root or os.getenv("NHMS_SCHEDULER_EVIDENCE_ROOT")
        if evidence_raw is None or not evidence_raw.strip():
            raise RetentionFailure("evidence_root_missing")
        evidence_root = _safe_existing_directory(evidence_raw, field="evidence_root")
        if not any(_path_under(evidence_root, root) for root in allowed_roots):
            raise RetentionFailure("evidence_root_outside_allowed_roots")
        retention_days = _required_positive_int(
            "NHMS_SCHEDULER_JOURNAL_RETENTION_DAYS",
            str(args.retention_days) if args.retention_days is not None else None,
            default=DEFAULT_RETENTION_DAYS,
        )
        enabled = _bool_env("NHMS_SCHEDULER_JOURNAL_RETENTION_ENABLED", default=False)
        dry_run = _bool_env("NHMS_SCHEDULER_JOURNAL_RETENTION_DRY_RUN", default=True)
        lookback_hours = _required_positive_int("NHMS_SCHEDULER_LOOKBACK_HOURS", None)
        cycle_lag_hours = _required_positive_int("NHMS_SCHEDULER_CYCLE_LAG_HOURS", None)
        allowed_cycle_hours = _parse_allowed_cycle_hours(os.getenv("NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC"))
        max_files = _required_positive_int(
            "MAX_FILE_JOURNAL_DISCOVERED_FILES",
            str(args.max_files) if args.max_files is not None else None,
            default=MAX_FILE_JOURNAL_DISCOVERED_FILES,
        )
        max_depth = _required_positive_int(
            "MAX_FILE_JOURNAL_SCAN_DEPTH",
            str(args.max_depth) if args.max_depth is not None else None,
            default=MAX_FILE_JOURNAL_SCAN_DEPTH,
        )
        max_cycle_members = _required_positive_int(
            "NHMS_SCHEDULER_JOURNAL_RETENTION_MAX_CYCLE_MEMBERS",
            str(args.max_cycle_members) if args.max_cycle_members is not None else None,
            default=MAX_ARCHIVE_CYCLE_MEMBERS,
        )
        max_cycle_bytes = _required_positive_int(
            "NHMS_SCHEDULER_JOURNAL_RETENTION_MAX_CYCLE_BYTES",
            str(args.max_cycle_bytes) if args.max_cycle_bytes is not None else None,
            default=MAX_ARCHIVE_CYCLE_BYTES,
        )
        max_archive_bytes = _required_positive_int(
            "NHMS_SCHEDULER_JOURNAL_RETENTION_MAX_ARCHIVE_BYTES",
            str(args.max_archive_bytes) if args.max_archive_bytes is not None else None,
            default=MAX_ARCHIVE_BYTES,
        )
        if not _valid_archive_limit(max_cycle_bytes) or not _valid_archive_limit(max_archive_bytes):
            raise RetentionFailure("nhms_scheduler_journal_retention_archive_limit_invalid")
        return (
            SchedulerJournalRetentionConfig(
                journal_root=journal_root,
                archive_root=archive_root,
                evidence_root=evidence_root,
                allowed_roots=allowed_roots,
                retention_days=retention_days,
                enabled=enabled,
                dry_run=dry_run,
                lookback_hours=lookback_hours,
                cycle_lag_hours=cycle_lag_hours,
                allowed_cycle_hours=allowed_cycle_hours,
                max_files=max_files,
                max_depth=max_depth,
                max_cycle_members=max_cycle_members,
                max_cycle_bytes=max_cycle_bytes,
                max_archive_bytes=max_archive_bytes,
            ),
            [],
        )
    except RetentionFailure as error:
        return None, [error.reason]


def _safety_window_valid(config: SchedulerJournalRetentionConfig) -> bool:
    gap = _largest_cycle_gap(config.allowed_cycle_hours)
    return config.retention_days * 24 > config.lookback_hours + config.cycle_lag_hours + 2 * gap


def _cycle_row(
    *,
    source_id: str,
    cycle_time: datetime,
    status: str,
    reason: str | None = None,
    members: Sequence[FileJournalRetentionMember] = (),
    archive_sha256: str | None = None,
    removed_paths: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "cycle_time": _iso(cycle_time),
        "status": status,
        "reason": reason,
        "member_count": len(members),
        "member_bytes": sum(member.size_bytes for member in members),
        "members": [member.relative_path for member in members[:MAX_RECEIPT_MEMBERS]],
        "members_truncated": len(members) > MAX_RECEIPT_MEMBERS,
        "archive_sha256": archive_sha256,
        "removed_paths": list(removed_paths[:MAX_RECEIPT_MEMBERS]),
        "removed_paths_truncated": len(removed_paths) > MAX_RECEIPT_MEMBERS,
    }


def run_retention(
    config: SchedulerJournalRetentionConfig,
    *,
    now: datetime,
    frontier: FrontierReadResult | None = None,
    receipt_reservation: ReceiptReservation | None = None,
) -> dict[str, Any]:
    """Plan or enforce bounded journal-cycle retention at its public seam.

    Destructive execution requires a durable ``receipt_reservation`` created
    before this call.  Dry-run and disabled invocations remain deterministic
    planning seams; they never create an archive or remove a hot member.
    """

    now = _utc(now)
    cutoff = now - timedelta(days=config.retention_days)
    preflight_blockers: list[str] = []
    if not _safety_window_valid(config):
        preflight_blockers.append("scheduler_window_invalid")
    if (
        config.max_cycle_members < 1
        or not _valid_archive_limit(config.max_cycle_bytes)
        or not _valid_archive_limit(config.max_archive_bytes)
    ):
        preflight_blockers.append("archive_limit_invalid")
    frontier = frontier or read_latest_pass_frontier(
        config.evidence_root,
        now=now,
        max_age=max_age_from_env(),
    )
    if frontier.status != "ok":
        preflight_blockers.append(frontier.reason or "frontier_unavailable")
    mutation_requested = config.enabled and not config.dry_run
    if mutation_requested:
        if receipt_reservation is None:
            preflight_blockers.append("receipt_reservation_required")
        else:
            try:
                _validate_receipt_reservation(config, receipt_reservation)
            except RetentionFailure as error:
                preflight_blockers.append(error.reason)
    mutation_enabled = mutation_requested and not preflight_blockers
    repository = FileOrchestrationJournalRepository(
        config.journal_root,
        max_files=config.max_files,
        max_depth=config.max_depth,
    )
    discovery = repository.discover_retention_cycles(max_files=config.max_files, max_depth=config.max_depth)
    rows: list[dict[str, Any]] = []
    if discovery.status != "ok":
        preflight_blockers.append(discovery.reason or "discovery_blocked")
    elif preflight_blockers:
        # Discovery is still reported, but no cycle is inspected while an
        # invocation-level safety proof is absent.
        for source_id, cycle_time in discovery.cycles:
            rows.append(
                _cycle_row(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    status="blocked",
                    reason=preflight_blockers[0],
                )
            )
    else:
        for source_id, cycle_time in discovery.cycles:
            if cycle_time >= cutoff:
                rows.append(
                    _cycle_row(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        status="skipped",
                        reason="within_retention_window",
                    )
                )
                continue
            if frontier.active_lower_bound is not None and cycle_time >= frontier.active_lower_bound:
                rows.append(
                    _cycle_row(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        status="skipped",
                        reason="pipeline_frontier_exempt",
                    )
                )
                continue
            with repository.open_retention_cycle(source_id=source_id, cycle_time=cycle_time) as window:
                inspection = window.inspect()
                if inspection.status == "busy":
                    rows.append(
                        _cycle_row(
                            source_id=source_id,
                            cycle_time=cycle_time,
                            status="skipped",
                            reason="in_flight",
                        )
                    )
                    continue
                if inspection.status != "eligible":
                    rows.append(
                        _cycle_row(
                            source_id=source_id,
                            cycle_time=cycle_time,
                            status="skipped" if inspection.status == "live" else "blocked",
                            reason=inspection.reason,
                            members=inspection.members,
                        )
                    )
                    continue
                if config.dry_run or not config.enabled:
                    rows.append(
                        _cycle_row(
                            source_id=source_id,
                            cycle_time=cycle_time,
                            status="planned",
                            reason="dry_run" if config.dry_run else "retention_disabled",
                            members=inspection.members,
                        )
                    )
                    continue
                try:
                    identity = _publish_archive(
                        config,
                        source_id=source_id,
                        cycle_time=cycle_time,
                        now=now,
                        frontier=frontier,
                        members=inspection.members,
                    )
                    manifest_members = _validated_manifest_members(
                        identity.manifest,
                        source_id=source_id,
                        cycle_time=cycle_time,
                        max_members=config.max_cycle_members,
                    )
                    removal = window.remove_members(
                        tuple(
                            FileJournalRetentionMember(
                                relative_path=str(member["path"]),
                                size_bytes=int(member["size_bytes"]),
                                sha256=str(member["sha256"]),
                            )
                            for member in manifest_members
                        )
                    )
                    rows.append(
                        _cycle_row(
                            source_id=source_id,
                            cycle_time=cycle_time,
                            status="archived" if removal.status == "removed" else removal.status,
                            reason=removal.reason,
                            members=inspection.members,
                            archive_sha256=identity.archive_sha256,
                            removed_paths=removal.removed_paths,
                        )
                    )
                except RetentionFailure as error:
                    rows.append(
                        _cycle_row(
                            source_id=source_id,
                            cycle_time=cycle_time,
                            status="blocked",
                            reason=error.reason,
                            members=inspection.members,
                        )
                    )
    rows.sort(key=lambda row: (row["source_id"], row["cycle_time"]))
    listed_rows = rows[:MAX_RECEIPT_CYCLES]
    return {
        "schema_version": SCHEMA_VERSION,
        "started_at": _iso(now),
        "finished_at": _iso(now),
        "journal_root": str(config.journal_root),
        "archive_root": str(config.archive_root),
        "enabled": config.enabled,
        "dry_run": config.dry_run,
        "enforcement_performed": mutation_enabled,
        "retention_days": config.retention_days,
        "cutoff": _iso(cutoff),
        "frontier": {
            "status": frontier.status,
            "reason": frontier.reason,
            "source": frontier.source,
            "active_lower_bound": None if frontier.active_lower_bound is None else _iso(frontier.active_lower_bound),
            "receipt_path": frontier.receipt_path,
            "receipt_started_at": None if frontier.receipt_started_at is None else _iso(frontier.receipt_started_at),
        },
        "preflight_blockers": preflight_blockers,
        "discovery": {"status": discovery.status, "reason": discovery.reason, "candidate_count": len(discovery.cycles)},
        "cycles": listed_rows,
        "cycles_truncated": len(rows) > len(listed_rows),
        "counts": {
            "archived": sum(row["status"] == "archived" for row in rows),
            "planned": sum(row["status"] == "planned" for row in rows),
            "skipped": sum(row["status"] == "skipped" for row in rows),
            "blocked": sum(row["status"] == "blocked" for row in rows),
            "partial": sum(row["status"] == "partial" for row in rows),
            "members": sum(int(row["member_count"]) for row in rows),
            "member_bytes": sum(int(row["member_bytes"]) for row in rows),
        },
    }


def _receipt_path(config: SchedulerJournalRetentionConfig, now: datetime, *, suffix: str) -> Path:
    receipt_dir = config.archive_root / RECEIPT_DIRECTORY
    ensure_directory_no_follow(config.archive_root)
    ensure_directory_no_follow(receipt_dir, containment_root=config.archive_root)
    return receipt_dir / f"retention-{_utc(now).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}-{suffix}.json"


def _reserve_receipt_path(config: SchedulerJournalRetentionConfig, now: datetime) -> Path:
    """Claim one no-clobber intent pathname despite same-second invocations."""

    receipt_dir = config.archive_root / RECEIPT_DIRECTORY
    try:
        ensure_directory_no_follow(config.archive_root)
        ensure_directory_no_follow(receipt_dir, containment_root=config.archive_root)
    except (OSError, SafeFilesystemError) as error:
        raise RetentionFailure("receipt_reservation_failed") from error
    stamp = _utc(now).strftime("%Y%m%dT%H%M%SZ")
    for sequence in range(MAX_RECEIPT_CYCLES):
        candidate = receipt_dir / f"retention-{stamp}-{os.getpid()}-{sequence}-intent.json"
        try:
            write_bytes_no_follow_exclusive(
                candidate,
                b"",
                containment_root=config.archive_root,
                require_durable_create=True,
            )
        except FileExistsError:
            continue
        except (OSError, SafeFilesystemError) as error:
            raise RetentionFailure("receipt_reservation_failed") from error
        return candidate
    raise RetentionFailure("receipt_reservation_failed")


def reserve_receipt(config: SchedulerJournalRetentionConfig, *, now: datetime) -> ReceiptReservation:
    """Durably reserve an invocation transaction before destructive work."""

    path = _reserve_receipt_path(config, now)
    invocation_id = path.stem.removesuffix("-intent")
    intent = {
        "schema_version": SCHEMA_VERSION,
        "receipt_status": _RECEIPT_STATUS_INTENT,
        "invocation_id": invocation_id,
        "started_at": _iso(now),
        "journal_root": str(config.journal_root),
        "archive_root": str(config.archive_root),
    }
    try:
        atomic_write_bytes_no_follow(
            path,
            _canonical_json(intent),
            containment_root=config.archive_root,
            require_durable_replace=True,
        )
    except (OSError, SafeFilesystemError) as error:
        raise RetentionFailure("receipt_reservation_failed") from error
    return ReceiptReservation(path=path, invocation_id=invocation_id)


def _validate_receipt_reservation(
    config: SchedulerJournalRetentionConfig,
    reservation: ReceiptReservation,
) -> None:
    """Prove the destructive call still owns its durable intent record."""

    try:
        payload = json.loads(
            read_bytes_durable_no_follow(
                reservation.path,
                max_bytes=MAX_ARCHIVE_MEMBER_BYTES,
                containment_root=config.archive_root,
            ).decode("utf-8")
        )
    except (OSError, SafeFilesystemError, UnicodeDecodeError, ValueError) as error:
        raise RetentionFailure("receipt_reservation_invalid") from error
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("receipt_status") != _RECEIPT_STATUS_INTENT
        or payload.get("invocation_id") != reservation.invocation_id
        or payload.get("journal_root") != str(config.journal_root)
        or payload.get("archive_root") != str(config.archive_root)
    ):
        raise RetentionFailure("receipt_reservation_invalid")


def finalize_receipt(
    config: SchedulerJournalRetentionConfig,
    reservation: ReceiptReservation,
    payload: Mapping[str, Any],
    *,
    now: datetime,
) -> Path:
    """Atomically replace the intent record with its bounded final outcome."""

    final = dict(payload)
    final.update(
        {
            "receipt_status": _RECEIPT_STATUS_FINAL,
            "invocation_id": reservation.invocation_id,
            "receipt_intent_path": str(reservation.path),
            "finished_at": _iso(now),
        }
    )
    try:
        atomic_write_bytes_no_follow(
            reservation.path,
            _canonical_json(final),
            containment_root=config.archive_root,
            require_durable_replace=True,
        )
    except (OSError, SafeFilesystemError) as error:
        raise RetentionFailure("receipt_finalization_failed") from error
    return reservation.path


def _write_receipt(config: SchedulerJournalRetentionConfig, payload: Mapping[str, Any], now: datetime) -> Path:
    """Write a standalone final receipt for non-destructive invocations."""

    path = _receipt_path(config, now, suffix="final")
    final = dict(payload)
    final["receipt_status"] = _RECEIPT_STATUS_FINAL
    try:
        write_bytes_no_follow_exclusive(
            path,
            _canonical_json(final),
            containment_root=config.archive_root,
            require_durable_create=True,
        )
    except (OSError, SafeFilesystemError, FileExistsError) as error:
        raise RetentionFailure("receipt_write_failed") from error
    return path
