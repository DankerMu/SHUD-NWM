"""Verified no-clobber restoration for scheduler-journal archive bundles."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
)
from packages.common.safe_fs_publication import write_bytes_no_follow_exclusive
from packages.common.source_identity import normalize_source_id
from services.orchestrator import scheduler_journal_archive as archive_owner
from services.orchestrator.chain_types import OrchestratorError
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from services.orchestrator.scheduler_journal_retention import _path_under, _safe_existing_directory
from services.orchestrator.scheduler_journal_retention_types import (
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_CYCLE_BYTES,
    MAX_ARCHIVE_CYCLE_MEMBERS,
    SCHEMA_VERSION,
    RetentionFailure,
    _cycle_stamp,
    _iso,
)


def _safe_stage_root(stage_root: Path | str, *, journal: Path, archive_root: Path) -> Path:
    stage = Path(stage_root).expanduser()
    if not stage.is_absolute():
        raise RetentionFailure("stage_root_not_absolute")
    if stage == Path("/"):
        raise RetentionFailure("stage_root_is_root")
    # Neither direction may overlap: staging under authority risks accidental
    # selection, while staging an authority ancestor gives restoration writes a
    # destructive namespace parent.
    if (
        _path_under(stage, journal)
        or _path_under(journal, stage)
        or _path_under(stage, archive_root)
        or _path_under(archive_root, stage)
    ):
        raise RetentionFailure("stage_root_overlaps_authority")
    try:
        ensure_directory_no_follow(stage)
    except (OSError, SafeFilesystemError) as error:
        raise RetentionFailure("stage_root_unsafe") from error
    return stage


def _restore_archive_paths(
    archive_root: Path,
    *,
    source_id: str,
    cycle_time: datetime,
) -> tuple[Path, Path]:
    bundle_archive = (
        archive_root
        / source_id
        / _cycle_stamp(cycle_time)
        / archive_owner._BUNDLE_DIRECTORY
        / archive_owner.ARCHIVE_NAME
    )
    bundle_manifest = bundle_archive.with_name(archive_owner.MANIFEST_NAME)
    if (
        archive_owner._entry_state(bundle_archive, root=archive_root) == "regular"
        or archive_owner._entry_state(bundle_manifest, root=archive_root) == "regular"
    ):
        return bundle_archive, bundle_manifest
    # Accept only a complete legacy pair written by the initial release. This
    # is recovery compatibility, not a second publication protocol.
    legacy_archive, legacy_manifest = archive_owner._legacy_archive_paths(
        archive_root,
        source_id=source_id,
        cycle_time=cycle_time,
    )
    return legacy_archive, legacy_manifest


def verify_and_restore(
    *,
    journal_root: Path | str,
    archive_root: Path | str,
    source_id: str,
    cycle: str,
    stage_root: Path | str,
) -> dict[str, Any]:
    """Verify one archive, stage it, and restore under the existing cycle flock."""

    journal = _safe_existing_directory(journal_root, field="journal_root")
    archive_root = _safe_existing_directory(archive_root, field="archive_root")
    try:
        canonical_source = normalize_source_id(source_id)
    except (TypeError, ValueError) as error:
        raise RetentionFailure("source_id_invalid") from error
    if source_id != canonical_source:
        raise RetentionFailure("source_id_not_normalized")
    try:
        cycle_time = datetime.strptime(cycle, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError as error:
        raise RetentionFailure("cycle_invalid") from error
    stage = Path(stage_root).expanduser()
    if not stage.is_absolute():
        raise RetentionFailure("stage_root_not_absolute")
    if stage == Path("/"):
        raise RetentionFailure("stage_root_is_root")
    if (
        _path_under(stage, journal)
        or _path_under(journal, stage)
        or _path_under(stage, archive_root)
        or _path_under(archive_root, stage)
    ):
        raise RetentionFailure("stage_root_overlaps_authority")
    archive_path, manifest_path = _restore_archive_paths(
        archive_root,
        source_id=canonical_source,
        cycle_time=cycle_time,
    )
    repository = FileOrchestrationJournalRepository(journal)
    try:
        with repository.open_retention_cycle(source_id=canonical_source, cycle_time=cycle_time) as window:
            if window.status == "busy":
                raise RetentionFailure("in_flight")
            stage = _safe_stage_root(stage, journal=journal, archive_root=archive_root)
            manifest = archive_owner._read_manifest(manifest_path)
            if not archive_owner._manifest_identity_matches(
                manifest,
                source_id=canonical_source,
                cycle_time=cycle_time,
            ):
                raise RetentionFailure("archive_manifest_identity_mismatch")
            archive_sha = archive_owner._verify_archive(
                archive_path,
                manifest=manifest,
                source_id=canonical_source,
                cycle_time=cycle_time,
                max_members=MAX_ARCHIVE_CYCLE_MEMBERS,
                max_archive_bytes=MAX_ARCHIVE_BYTES,
            )
            if archive_sha != manifest.get("archive_sha256"):
                raise RetentionFailure("archive_digest_mismatch")
            members = archive_owner._validated_manifest_members(
                manifest,
                source_id=canonical_source,
                cycle_time=cycle_time,
                max_members=MAX_ARCHIVE_CYCLE_MEMBERS,
            )
            staged_content: list[tuple[str, bytes]] = []
            total = 0
            for member in members:
                relative = str(member["path"])
                size = int(member["size_bytes"])
                digest = str(member["sha256"])
                total += size
                if total > MAX_ARCHIVE_CYCLE_BYTES:
                    raise RetentionFailure("archive_cycle_byte_limit_exceeded")
                actual_size, actual_digest, content = archive_owner._stream_archive_member(
                    archive_path,
                    relative,
                    max_bytes=size,
                    collect_content=True,
                )
                if actual_size != size or actual_digest != digest:
                    raise RetentionFailure("archive_verification_failed")
                staged_content.append((relative, content))
            if len({relative for relative, _content in staged_content}) != len(staged_content):
                raise RetentionFailure("archive_manifest_invalid")
            # All destination and stage checks happen under the flock.  Do them all
            # before the first no-clobber write so ordinary refusals make zero writes.
            for relative, _content in staged_content:
                stage_target = stage / relative
                destination = journal / relative
                if archive_owner._entry_state(stage_target, root=stage) != "absent":
                    raise RetentionFailure("stage_clobber")
                if archive_owner._entry_state(destination, root=journal) != "absent":
                    raise RetentionFailure("restore_clobber")
            for relative, content in staged_content:
                atomic_write_bytes_no_follow(
                    stage / relative,
                    content,
                    containment_root=stage,
                    require_durable_replace=True,
                )
            for relative, content in staged_content:
                write_bytes_no_follow_exclusive(journal / relative, content, containment_root=journal)
    except OrchestratorError as error:
        if error.error_code != "FILE_JOURNAL_WRITE_FAILED" or error.details.get("surface") not in {
            "cycle_lock",
            "journal_root",
        }:
            raise
        reason = (
            "journal_root_unavailable"
            if error.details.get("surface") == "journal_root"
            else "cycle_lock_unavailable"
        )
        raise RetentionFailure(reason) from error
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "restored",
        "source_id": canonical_source,
        "cycle_time": _iso(cycle_time),
        "archive_sha256": archive_sha,
        "restored_paths": [relative for relative, _content in staged_content],
    }
