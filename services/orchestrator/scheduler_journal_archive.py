"""Immutable archive manifest verification and publication."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    list_directory_no_follow_limited,
    open_file_no_follow,
    read_bytes_durable_no_follow,
    stat_no_follow,
)
from packages.common.safe_fs_publication import (
    make_directory_no_follow_exclusive,
    move_regular_file_no_follow_exclusive,
    write_bytes_no_follow_exclusive,
)
from services.orchestrator.file_orchestration_journal import (
    MAX_FILE_JOURNAL_CYCLE_SEGMENTS,
    FileJournalRetentionMember,
)
from services.orchestrator.retention_frontier import FrontierReadResult
from services.orchestrator.scheduler_journal_retention_types import (
    ARCHIVE_NAME,
    HOT_SURFACES,
    MANIFEST_NAME,
    MANIFEST_SCHEMA_VERSION,
    MAX_ARCHIVE_MEMBER_BYTES,
    PUBLICATION_SCHEMA_VERSION,
    ArchiveIdentity,
    RetentionFailure,
    SchedulerJournalRetentionConfig,
    _cycle_stamp,
    _iso,
)

_ARCHIVE_TIMEOUT_SECONDS = 300
_BUNDLE_DIRECTORY = "bundle"
_PUBLICATION_MARKER = "publication.json"


def _sha256_path(path: Path, *, root: Path, limit: int) -> str:
    """Hash a regular no-follow file incrementally, enforcing its byte ceiling."""

    digest = hashlib.sha256()
    total = 0
    try:
        fd = open_file_no_follow(path, containment_root=root)
    except (OSError, SafeFilesystemError) as error:
        raise RetentionFailure("archive_input_unreadable") from error
    try:
        while True:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise RetentionFailure("archive_input_too_large")
            digest.update(chunk)
    except OSError as error:
        raise RetentionFailure("archive_input_unreadable") from error
    finally:
        os.close(fd)
    return digest.hexdigest()


def _member_metadata(
    root: Path,
    members: Sequence[FileJournalRetentionMember],
    *,
    source_id: str,
    cycle_time: datetime,
    max_members: int,
    max_bytes: int,
) -> list[dict[str, Any]]:
    """Bind every hot byte candidate to the requested canonical cycle before tar."""

    if len(members) > max_members:
        raise RetentionFailure("archive_member_limit_exceeded")
    total = 0
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for member in members:
        relative = _validated_cycle_member_path(
            relative=member.relative_path,
            source_id=source_id,
            cycle_time=cycle_time,
        )
        if relative in seen:
            raise RetentionFailure("archive_member_invalid")
        seen.add(relative)
        path = root / relative
        try:
            actual_size = int(stat_no_follow(path, containment_root=root).st_size)
        except (OSError, SafeFilesystemError) as error:
            raise RetentionFailure("archive_input_unreadable") from error
        if actual_size != member.size_bytes:
            raise RetentionFailure("archive_input_changed")
        if actual_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise RetentionFailure("archive_input_too_large")
        total += actual_size
        if total > max_bytes:
            raise RetentionFailure("archive_cycle_byte_limit_exceeded")
        digest = _sha256_path(path, root=root, limit=actual_size)
        out.append({"path": relative, "size_bytes": actual_size, "sha256": digest})
    return out


def _safe_member_relative_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise RetentionFailure("archive_member_path_unsafe")
    if path.parts[0] not in HOT_SURFACES:
        raise RetentionFailure("archive_member_path_unsafe")
    return path.as_posix()


def _validated_cycle_member_path(*, relative: str, source_id: str, cycle_time: datetime) -> str:
    """Prove an archive entry names exactly one requested hot cycle member."""

    safe = _safe_member_relative_path(relative)
    parts = Path(safe).parts
    stamp = _cycle_stamp(cycle_time)
    if parts[0] == "latest":
        valid = len(parts) == 4 and parts[1] == source_id and parts[2] == stamp and parts[3].endswith(".json")
    else:
        valid = len(parts) == 3 and parts[1] == source_id and _valid_segment_name(parts[2], stamp)
    if not valid:
        raise RetentionFailure("archive_manifest_identity_mismatch")
    return safe


def _valid_segment_name(name: str, stamp: str) -> bool:
    if name == f"{stamp}.jsonl":
        return True
    prefix = f"{stamp}."
    if not name.startswith(prefix) or not name.endswith(".jsonl"):
        return False
    number = name[len(prefix) : -len(".jsonl")]
    return (
        bool(number) and number.isascii() and number.isdecimal() and 0 < int(number) < MAX_FILE_JOURNAL_CYCLE_SEGMENTS
    )


def _archive_paths(
    config: SchedulerJournalRetentionConfig,
    *,
    source_id: str,
    cycle_time: datetime,
) -> tuple[Path, Path]:
    target = config.archive_root / source_id / _cycle_stamp(cycle_time) / _BUNDLE_DIRECTORY
    return target / ARCHIVE_NAME, target / MANIFEST_NAME


def _legacy_archive_paths(
    archive_root: Path,
    *,
    source_id: str,
    cycle_time: datetime,
) -> tuple[Path, Path]:
    target = archive_root / source_id / _cycle_stamp(cycle_time)
    return target / ARCHIVE_NAME, target / MANIFEST_NAME


def _archive_commands(
    *,
    journal_root: Path,
    output: Path,
    members: Sequence[dict[str, Any]],
) -> list[list[str]]:
    names = [member["path"] for member in members]
    # node-22 has GNU tar and zstd.  Keeping argv data-only prevents a path from
    # becoming shell syntax; names were already safe-relative-path validated.
    if os.environ.get("NHMS_JOURNAL_RETENTION_TEST_ALLOW_BSD_TAR") == "true":
        # Local macOS fixtures lack GNU tar. Production never sets this test-only
        # escape hatch and therefore requires the fixed GNU tar argv below.
        tar_argv = ["tar", "--format=ustar", "-C", str(journal_root), "-cf", str(output), *names]
    else:
        tar_argv = [
            "tar",
            "--format=posix",
            "--sort=name",
            "--mtime=@0",
            "--owner=0",
            "--group=0",
            "--numeric-owner",
            "-C",
            str(journal_root),
            "-cf",
            str(output),
            *names,
        ]
    return [tar_argv, ["zstd", "-q", "-T1", "-19", "-f", str(output)]]


def _run_archive_toolchain(
    *,
    journal_root: Path,
    output: Path,
    members: Sequence[dict[str, Any]],
    max_archive_bytes: int,
) -> None:
    tar_argv, zstd_argv = _archive_commands(journal_root=journal_root, output=output, members=members)
    try:
        tar_result = subprocess.run(
            tar_argv,
            capture_output=True,
            timeout=_ARCHIVE_TIMEOUT_SECONDS,
            check=False,
        )
        zstd_result = subprocess.run(
            zstd_argv,
            capture_output=True,
            timeout=_ARCHIVE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RetentionFailure("archive_tool_failed") from error
    if tar_result.returncode != 0 or zstd_result.returncode != 0:
        raise RetentionFailure("archive_tool_failed")
    compressed = output.with_suffix(output.suffix + ".zst")
    if not compressed.exists():
        raise RetentionFailure("archive_tool_failed")
    try:
        compressed_size = int(stat_no_follow(compressed, containment_root=output.parent).st_size)
    except (OSError, SafeFilesystemError) as error:
        raise RetentionFailure("archive_tool_failed") from error
    if compressed_size > max_archive_bytes:
        raise RetentionFailure("archive_byte_limit_exceeded")
    compressed.replace(output)
    try:
        final_size = int(stat_no_follow(output, containment_root=output.parent).st_size)
    except (OSError, SafeFilesystemError) as error:
        raise RetentionFailure("archive_tool_failed") from error
    if final_size > max_archive_bytes:
        raise RetentionFailure("archive_byte_limit_exceeded")


@contextmanager
def _archive_tar_reader(path: Path) -> Iterable[subprocess.Popen[bytes]]:
    """Stream a compressed archive through tar without materialising its tar bytes."""

    try:
        archive_fd = open_file_no_follow(path, containment_root=path.parent)
        zstd = subprocess.Popen(
            ["zstd", "-q", "-d", "-c"],
            stdin=archive_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert zstd.stdout is not None
        tar = subprocess.Popen(
            ["tar", "-tf", "-"],
            stdin=zstd.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        zstd.stdout.close()
    except (OSError, subprocess.SubprocessError) as error:
        try:
            os.close(archive_fd)
        except (NameError, OSError):
            pass
        raise RetentionFailure("archive_verification_failed") from error
    try:
        yield tar
        if tar.wait(timeout=_ARCHIVE_TIMEOUT_SECONDS) != 0 or zstd.wait(timeout=_ARCHIVE_TIMEOUT_SECONDS) != 0:
            raise RetentionFailure("archive_verification_failed")
    except subprocess.TimeoutExpired as error:
        tar.kill()
        zstd.kill()
        raise RetentionFailure("archive_verification_failed") from error
    finally:
        if tar.poll() is None:
            tar.kill()
            tar.wait()
        if zstd.poll() is None:
            zstd.kill()
            zstd.wait()
        os.close(archive_fd)


def _archive_listing(path: Path, *, max_members: int) -> list[str]:
    listed: list[str] = []
    with _archive_tar_reader(path) as tar:
        assert tar.stdout is not None
        for raw in tar.stdout:
            if len(raw) > 4096:
                raise RetentionFailure("archive_verification_failed")
            try:
                name = raw.decode("utf-8").rstrip("\n")
            except UnicodeDecodeError as error:
                raise RetentionFailure("archive_verification_failed") from error
            if not name:
                continue
            listed.append(name)
            if len(listed) > max_members:
                raise RetentionFailure("archive_member_limit_exceeded")
    return listed


def _stream_archive_member(
    path: Path,
    member_path: str,
    *,
    max_bytes: int,
    collect_content: bool = False,
) -> tuple[int, str, bytes]:
    """Stream one member; callers opt in to bounded staging bytes only."""

    try:
        archive_fd = open_file_no_follow(path, containment_root=path.parent)
        zstd = subprocess.Popen(
            ["zstd", "-q", "-d", "-c"],
            stdin=archive_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert zstd.stdout is not None
        tar = subprocess.Popen(
            ["tar", "-xOf", "-", member_path],
            stdin=zstd.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        zstd.stdout.close()
    except (OSError, subprocess.SubprocessError) as error:
        try:
            os.close(archive_fd)
        except (NameError, OSError):
            pass
        raise RetentionFailure("archive_verification_failed") from error
    content = bytearray()
    digest = hashlib.sha256()
    total = 0
    try:
        assert tar.stdout is not None
        while True:
            chunk = tar.stdout.read(min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise RetentionFailure("archive_verification_failed")
            digest.update(chunk)
            if collect_content:
                content.extend(chunk)
        if tar.wait(timeout=_ARCHIVE_TIMEOUT_SECONDS) != 0 or zstd.wait(timeout=_ARCHIVE_TIMEOUT_SECONDS) != 0:
            raise RetentionFailure("archive_verification_failed")
    except subprocess.TimeoutExpired as error:
        tar.kill()
        zstd.kill()
        raise RetentionFailure("archive_verification_failed") from error
    finally:
        if tar.poll() is None:
            tar.kill()
            tar.wait()
        if zstd.poll() is None:
            zstd.kill()
            zstd.wait()
        os.close(archive_fd)
    return total, digest.hexdigest(), bytes(content)


def _verify_archive(
    path: Path,
    *,
    manifest: Mapping[str, Any],
    source_id: str,
    cycle_time: datetime,
    max_members: int,
    max_archive_bytes: int,
) -> str:
    try:
        info = stat_no_follow(path, containment_root=path.parent)
    except (OSError, SafeFilesystemError) as error:
        raise RetentionFailure("archive_verification_failed") from error
    if not stat.S_ISREG(info.st_mode):
        raise RetentionFailure("archive_verification_failed")
    if info.st_size > max_archive_bytes:
        raise RetentionFailure("archive_byte_limit_exceeded")
    expected_members = manifest.get("members")
    if not isinstance(expected_members, list) or len(expected_members) > max_members:
        raise RetentionFailure("archive_manifest_invalid")
    expected_paths: list[str] = []
    for member in expected_members:
        if not isinstance(member, Mapping):
            raise RetentionFailure("archive_manifest_invalid")
        try:
            expected_paths.append(
                _validated_cycle_member_path(
                    relative=str(member.get("path") or ""),
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
            )
        except RetentionFailure as error:
            # Restore/adopt seams already have an archive. Keep the established
            # verification classification while using the same identity parser.
            if error.reason in {"archive_manifest_identity_mismatch", "archive_member_path_unsafe"}:
                raise RetentionFailure("archive_verification_failed") from error
            raise
    if len(set(expected_paths)) != len(expected_paths):
        raise RetentionFailure("archive_manifest_invalid")
    actual_paths = _archive_listing(path, max_members=max_members)
    if actual_paths != expected_paths:
        raise RetentionFailure("archive_verification_failed")
    for entry, relative in zip(expected_members, expected_paths, strict=True):
        size = entry.get("size_bytes")
        digest = entry.get("sha256")
        if type(size) is not int or size < 0 or size > MAX_ARCHIVE_MEMBER_BYTES or not isinstance(digest, str):
            raise RetentionFailure("archive_manifest_invalid")
        actual_size, actual_digest, _content = _stream_archive_member(
            path,
            relative,
            max_bytes=size,
        )
        if actual_size != size or actual_digest != digest:
            raise RetentionFailure("archive_verification_failed")
    return _sha256_path(path, root=path.parent, limit=max_archive_bytes)


def _require_gnu_tar() -> None:
    """Fail closed unless the operational GNU tar dependency is present."""

    if os.environ.get("NHMS_JOURNAL_RETENTION_TEST_ALLOW_BSD_TAR") == "true":
        return
    try:
        completed = subprocess.run(
            ["tar", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RetentionFailure("archive_tool_unavailable") from error
    if completed.returncode != 0 or b"GNU tar" not in completed.stdout:
        raise RetentionFailure("archive_tool_unavailable")


def _manifest_payload(
    *,
    source_id: str,
    cycle_time: datetime,
    now: datetime,
    frontier: FrontierReadResult,
    members: list[dict[str, Any]],
    archive_sha256: str | None,
    mode: str,
) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source_id": source_id,
        "cycle_time": _iso(cycle_time),
        "created_at": _iso(now),
        "mode": mode,
        "frontier": {
            "source": frontier.source,
            "active_lower_bound": None if frontier.active_lower_bound is None else _iso(frontier.active_lower_bound),
            "receipt_path": frontier.receipt_path,
            "receipt_started_at": None if frontier.receipt_started_at is None else _iso(frontier.receipt_started_at),
        },
        "archive_sha256": archive_sha256,
        "members": members,
    }


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        content = read_bytes_durable_no_follow(
            path,
            max_bytes=MAX_ARCHIVE_MEMBER_BYTES,
            containment_root=path.parent,
        )
        payload = json.loads(content.decode("utf-8"))
    except (OSError, SafeFilesystemError, UnicodeDecodeError, ValueError) as error:
        raise RetentionFailure("archive_manifest_invalid") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise RetentionFailure("archive_manifest_invalid")
    return payload


def _manifest_identity_matches(
    manifest: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
) -> bool:
    return manifest.get("source_id") == source_id and manifest.get("cycle_time") == _iso(cycle_time)


def _validated_manifest_members(
    manifest: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    max_members: int,
) -> tuple[dict[str, Any], ...]:
    """Validate every immutable manifest member against one canonical cycle.

    The owner only receives these records after the archive has proven the same
    paths and bytes.  Keeping identity validation here means an adopted archive
    cannot turn a merely hot-shaped member from another source or cycle into a
    deletion target.
    """

    members = manifest.get("members")
    if not isinstance(members, list) or not members or len(members) > max_members:
        raise RetentionFailure("archive_manifest_invalid")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for member in members:
        if not isinstance(member, Mapping):
            raise RetentionFailure("archive_manifest_invalid")
        relative = _validated_cycle_member_path(
            relative=str(member.get("path") or ""),
            source_id=source_id,
            cycle_time=cycle_time,
        )
        size = member.get("size_bytes")
        digest = member.get("sha256")
        if (
            type(size) is not int
            or size < 0
            or size > MAX_ARCHIVE_MEMBER_BYTES
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RetentionFailure("archive_manifest_invalid")
        if relative in seen:
            raise RetentionFailure("archive_manifest_invalid")
        seen.add(relative)
        validated.append({"path": relative, "size_bytes": size, "sha256": digest})
    return tuple(validated)


def _archive_matches(
    archive_path: Path,
    manifest_path: Path,
    *,
    source_id: str,
    cycle_time: datetime,
    config: SchedulerJournalRetentionConfig,
) -> ArchiveIdentity:
    """Verify an existing immutable bundle independently of a new frontier receipt.

    The archive member set is the immutable identity.  A later fresh frontier is
    operational evidence for the retry, not archive provenance to compare.
    """

    manifest = _read_manifest(manifest_path)
    if not _manifest_identity_matches(manifest, source_id=source_id, cycle_time=cycle_time):
        raise RetentionFailure("archive_conflict")
    try:
        _validated_manifest_members(
            manifest,
            source_id=source_id,
            cycle_time=cycle_time,
            max_members=config.max_cycle_members,
        )
    except RetentionFailure as error:
        raise RetentionFailure("archive_conflict") from error
    archive_sha256 = _verify_archive(
        archive_path,
        manifest=manifest,
        source_id=source_id,
        cycle_time=cycle_time,
        max_members=config.max_cycle_members,
        max_archive_bytes=config.max_archive_bytes,
    )
    if manifest.get("archive_sha256") != archive_sha256:
        raise RetentionFailure("archive_conflict")
    return ArchiveIdentity(archive_sha256=archive_sha256, manifest=manifest)


def _bundle_root(config: SchedulerJournalRetentionConfig, *, source_id: str, cycle_time: datetime) -> Path:
    return config.archive_root / source_id / _cycle_stamp(cycle_time)


def _entry_state(path: Path, *, root: Path) -> str:
    try:
        info = stat_no_follow(path, containment_root=root)
    except FileNotFoundError:
        return "absent"
    except (OSError, SafeFilesystemError):
        return "unsafe"
    return "regular" if stat.S_ISREG(info.st_mode) else "unsafe"


def _directory_state(path: Path, *, root: Path) -> str:
    try:
        info = stat_no_follow(path, containment_root=root)
    except FileNotFoundError:
        return "absent"
    except (OSError, SafeFilesystemError):
        return "unsafe"
    return "directory" if stat.S_ISDIR(info.st_mode) else "unsafe"


def _recoverable_publication_manifest(
    config: SchedulerJournalRetentionConfig,
    *,
    source_id: str,
    cycle_time: datetime,
) -> dict[str, Any] | None:
    """Return a durable pre-publish manifest only for this command's own state.

    The root marker is written before the final bundle directory exists.  It
    closes the crash window between an empty directory claim and a member move:
    a marker-free incomplete bundle is always an external conflict, while a
    marker-bound one is safe to complete only from bytes that re-verify against
    that exact immutable manifest.
    """

    bundle_root = _bundle_root(config, source_id=source_id, cycle_time=cycle_time)
    marker = bundle_root / _PUBLICATION_MARKER
    marker_manifest = _read_publication_manifest(
        marker,
        source_id=source_id,
        cycle_time=cycle_time,
    )
    archive_path, manifest_path = _archive_paths(config, source_id=source_id, cycle_time=cycle_time)
    bundle = archive_path.parent
    bundle_state = _directory_state(bundle, root=config.archive_root)
    archive_state = _entry_state(archive_path, root=config.archive_root)
    manifest_state = _entry_state(manifest_path, root=config.archive_root)
    if marker_manifest is None:
        if bundle_state != "absent" or archive_state != "absent" or manifest_state != "absent":
            raise RetentionFailure("archive_conflict")
        return None
    if bundle_state == "absent":
        if archive_state == manifest_state == "absent":
            return marker_manifest
        raise RetentionFailure("archive_conflict")
    if bundle_state != "directory" or archive_state == "unsafe" or manifest_state == "unsafe":
        raise RetentionFailure("archive_conflict")
    try:
        names = list_directory_no_follow_limited(
            bundle,
            containment_root=config.archive_root,
            max_entries=2,
        )
    except (OSError, SafeFilesystemError) as error:
        raise RetentionFailure("archive_conflict") from error
    if set(names) - {ARCHIVE_NAME, MANIFEST_NAME}:
        raise RetentionFailure("archive_conflict")
    if archive_state == "regular":
        _verify_archive(
            archive_path,
            manifest=marker_manifest,
            source_id=source_id,
            cycle_time=cycle_time,
            max_members=config.max_cycle_members,
            max_archive_bytes=config.max_archive_bytes,
        )
    if manifest_state == "regular":
        try:
            final_manifest_bytes = read_bytes_durable_no_follow(
                manifest_path,
                max_bytes=MAX_ARCHIVE_MEMBER_BYTES,
                containment_root=config.archive_root,
            )
        except (OSError, SafeFilesystemError) as error:
            raise RetentionFailure("archive_conflict") from error
        if final_manifest_bytes != _canonical_json(marker_manifest):
            raise RetentionFailure("archive_conflict")
    return marker_manifest


def _existing_archive_identity(
    config: SchedulerJournalRetentionConfig,
    *,
    source_id: str,
    cycle_time: datetime,
) -> ArchiveIdentity | None:
    archive_path, manifest_path = _archive_paths(config, source_id=source_id, cycle_time=cycle_time)
    archive_state = _entry_state(archive_path, root=config.archive_root)
    manifest_state = _entry_state(manifest_path, root=config.archive_root)
    if archive_state == manifest_state == "regular":
        identity = _archive_matches(
            archive_path,
            manifest_path,
            source_id=source_id,
            cycle_time=cycle_time,
            config=config,
        )
        marker_manifest = _read_publication_manifest(
            _bundle_root(config, source_id=source_id, cycle_time=cycle_time) / _PUBLICATION_MARKER,
            source_id=source_id,
            cycle_time=cycle_time,
        )
        if marker_manifest is not None and _canonical_json(marker_manifest) != _canonical_json(identity.manifest):
            raise RetentionFailure("archive_conflict")
        return identity
    if archive_state == "unsafe" or manifest_state == "unsafe":
        raise RetentionFailure("archive_conflict")
    if (
        _recoverable_publication_manifest(
            config,
            source_id=source_id,
            cycle_time=cycle_time,
        )
        is not None
    ):
        return None
    if archive_state != "absent" or manifest_state != "absent":
        raise RetentionFailure("archive_conflict")
    # Legacy final pairs are not written anymore, but an incomplete or
    # externally supplied legacy-looking pair must still refuse rather than be
    # silently ignored and overwritten by the new bundle directory.
    legacy_archive, legacy_manifest = _legacy_archive_paths(
        config.archive_root,
        source_id=source_id,
        cycle_time=cycle_time,
    )
    legacy_states = (
        _entry_state(legacy_archive, root=config.archive_root),
        _entry_state(legacy_manifest, root=config.archive_root),
    )
    if legacy_states == ("absent", "absent"):
        return None
    raise RetentionFailure("archive_conflict")


def _publication_payload(*, manifest: Mapping[str, Any]) -> bytes:
    return _canonical_json(
        {
            "schema_version": PUBLICATION_SCHEMA_VERSION,
            "manifest": dict(manifest),
        }
    )


def _read_publication_manifest(
    path: Path,
    *,
    source_id: str,
    cycle_time: datetime,
) -> dict[str, Any] | None:
    try:
        payload = json.loads(
            read_bytes_durable_no_follow(
                path,
                max_bytes=MAX_ARCHIVE_MEMBER_BYTES,
                containment_root=path.parent,
            ).decode("utf-8")
        )
    except (FileNotFoundError, OSError, SafeFilesystemError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, Mapping) or payload.get("schema_version") != PUBLICATION_SCHEMA_VERSION:
        return None
    manifest = payload.get("manifest")
    if not isinstance(manifest, Mapping) or not _manifest_identity_matches(
        manifest,
        source_id=source_id,
        cycle_time=cycle_time,
    ):
        return None
    return dict(manifest)


def _publish_bundle_directory(
    *,
    temporary_bundle: Path,
    final_bundle: Path,
    bundle_root: Path,
    archive_root: Path,
    manifest: Mapping[str, Any],
) -> None:
    """No-clobber publish an archive pair behind a durable identity marker.

    The marker makes an empty reserved directory distinguishable from a foreign
    directory after a crash.  The archive remains inaccessible to normal
    restore until both immutable member files are present and verified.
    """

    marker = bundle_root / _PUBLICATION_MARKER
    try:
        write_bytes_no_follow_exclusive(
            marker,
            _publication_payload(manifest=manifest),
            containment_root=archive_root,
            require_durable_create=True,
        )
    except FileExistsError:
        raise RetentionFailure("archive_publish_raced") from None
    except (OSError, SafeFilesystemError) as error:
        raise RetentionFailure("archive_publish_failed") from error
    try:
        make_directory_no_follow_exclusive(final_bundle, containment_root=archive_root)
    except FileExistsError:
        raise RetentionFailure("archive_publish_recoverable") from None
    except (OSError, SafeFilesystemError) as error:
        raise RetentionFailure("archive_publish_failed") from error
    try:
        move_regular_file_no_follow_exclusive(
            temporary_bundle,
            ARCHIVE_NAME,
            final_bundle,
            ARCHIVE_NAME,
            containment_root=archive_root,
        )
        move_regular_file_no_follow_exclusive(
            temporary_bundle,
            MANIFEST_NAME,
            final_bundle,
            MANIFEST_NAME,
            containment_root=archive_root,
        )
    except (OSError, SafeFilesystemError) as error:
        # The durable marker is a self-produced reservation.  No hot unlink is
        # reachable until `_existing_archive_identity` proves both final bytes.
        raise RetentionFailure("archive_publish_failed") from error


def _publish_archive(
    config: SchedulerJournalRetentionConfig,
    *,
    source_id: str,
    cycle_time: datetime,
    now: datetime,
    frontier: FrontierReadResult,
    members: Sequence[FileJournalRetentionMember],
) -> ArchiveIdentity:
    existing = _existing_archive_identity(config, source_id=source_id, cycle_time=cycle_time)
    if existing is not None:
        return existing
    _require_gnu_tar()
    member_data = _member_metadata(
        config.journal_root,
        members,
        source_id=source_id,
        cycle_time=cycle_time,
        max_members=config.max_cycle_members,
        max_bytes=config.max_cycle_bytes,
    )
    expected = _manifest_payload(
        source_id=source_id,
        cycle_time=cycle_time,
        now=now,
        frontier=frontier,
        members=member_data,
        archive_sha256=None,
        mode="enforce",
    )
    bundle_root = _bundle_root(config, source_id=source_id, cycle_time=cycle_time)
    final_bundle = bundle_root / _BUNDLE_DIRECTORY
    try:
        ensure_directory_no_follow(config.archive_root)
        ensure_directory_no_follow(bundle_root, containment_root=config.archive_root)
    except (OSError, SafeFilesystemError) as error:
        raise RetentionFailure("archive_publish_failed") from error
    temporary_parent = Path(tempfile.mkdtemp(prefix=".journal-cycle-", dir=bundle_root))
    temporary_bundle = temporary_parent / _BUNDLE_DIRECTORY
    try:
        ensure_directory_no_follow(temporary_bundle, containment_root=config.archive_root)
        temporary_archive = temporary_bundle / ARCHIVE_NAME
        _run_archive_toolchain(
            journal_root=config.journal_root,
            output=temporary_archive,
            members=member_data,
            max_archive_bytes=config.max_archive_bytes,
        )
        archive_sha256 = _verify_archive(
            temporary_archive,
            manifest={**expected, "archive_sha256": None},
            source_id=source_id,
            cycle_time=cycle_time,
            max_members=config.max_cycle_members,
            max_archive_bytes=config.max_archive_bytes,
        )
        manifest = {**expected, "archive_sha256": archive_sha256}
        atomic_write_bytes_no_follow(
            temporary_bundle / MANIFEST_NAME,
            _canonical_json(manifest),
            containment_root=config.archive_root,
            require_durable_replace=True,
        )
        # Bind final manifest bytes and final archive bytes before publication.
        _verify_archive(
            temporary_archive,
            manifest=manifest,
            source_id=source_id,
            cycle_time=cycle_time,
            max_members=config.max_cycle_members,
            max_archive_bytes=config.max_archive_bytes,
        )
        try:
            _publish_bundle_directory(
                temporary_bundle=temporary_bundle,
                final_bundle=final_bundle,
                bundle_root=bundle_root,
                archive_root=config.archive_root,
                manifest=manifest,
            )
        except RetentionFailure as error:
            if error.reason not in {"archive_publish_raced", "archive_publish_recoverable"}:
                raise
            existing = _existing_archive_identity(config, source_id=source_id, cycle_time=cycle_time)
            if existing is not None:
                return existing
            reserved_manifest = _recoverable_publication_manifest(
                config,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if reserved_manifest is None or _canonical_json(reserved_manifest) != _canonical_json(manifest):
                raise RetentionFailure("archive_conflict") from error
            # The marker binds this recovery to the exact already verified
            # archive digest and member bytes, so the temporary pair may safely
            # fill absent slots without ever clobbering a present one.
            try:
                if _directory_state(final_bundle, root=config.archive_root) == "absent":
                    make_directory_no_follow_exclusive(final_bundle, containment_root=config.archive_root)
                if _entry_state(final_bundle / ARCHIVE_NAME, root=config.archive_root) == "absent":
                    move_regular_file_no_follow_exclusive(
                        temporary_bundle,
                        ARCHIVE_NAME,
                        final_bundle,
                        ARCHIVE_NAME,
                        containment_root=config.archive_root,
                    )
                if _entry_state(final_bundle / MANIFEST_NAME, root=config.archive_root) == "absent":
                    move_regular_file_no_follow_exclusive(
                        temporary_bundle,
                        MANIFEST_NAME,
                        final_bundle,
                        MANIFEST_NAME,
                        containment_root=config.archive_root,
                    )
            except (OSError, SafeFilesystemError) as retry_error:
                raise RetentionFailure("archive_conflict") from retry_error
        return _archive_matches(
            final_bundle / ARCHIVE_NAME,
            final_bundle / MANIFEST_NAME,
            source_id=source_id,
            cycle_time=cycle_time,
            config=config,
        )
    finally:
        shutil.rmtree(temporary_parent, ignore_errors=True)



# Public owner seams used by retention orchestration.
publish_archive = _publish_archive
validated_manifest_members = _validated_manifest_members
