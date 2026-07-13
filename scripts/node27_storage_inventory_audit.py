#!/usr/bin/env python3
"""Read-only node-27 hot/cold inventory and completeness-receipt publisher."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import unquote, urlparse

import jsonschema

from packages.common.redaction import redact_database_dsn, redact_text
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    list_directory_no_follow_limited,
    open_directory_no_follow,
    open_file_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
    verify_directory_no_follow,
)
from packages.common.source_identity import normalize_source_id
from packages.common.storage import (
    DEFAULT_DB_RETENTION_DAYS,
    ArchiveConfigurationError,
    ArchiveIdentity,
    archive_identity_for_state_reference,
    archive_provenance_paths,
    validate_product_archive_manifest_binding,
)
from scripts import node27_product_archive as product_archive

MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_SALVAGE_MANIFESTS = 10_000
MAX_SALVAGE_ENTRIES = 100_000
MAX_SALVAGE_DEPTH = 8
MAX_SUBJECTS = 100_000
MAX_RUN_OUTPUT_ENTRIES = 10_000
MAX_RUN_OUTPUT_DEPTH = 8
STATEMENT_TIMEOUT_MS = 20_000
SCHEMA_VERSION = "1.1"
_ROOT = Path(__file__).resolve().parents[1]
COMPLETENESS_SCHEMA_PATH = _ROOT / "schemas/archive_completeness_receipt.schema.json"
PRODUCT_SCHEMA_PATH = _ROOT / "schemas/product_archive_manifest.schema.json"
SALVAGE_SCHEMA_PATH = _ROOT / "schemas/salvage_manifest.schema.json"


class AuditBlocked(RuntimeError):
    """Raised when evidence is unsafe or the gate receipt cannot be proved."""


class PublicationIndeterminate(AuditBlocked):
    """Raised after replacement when receipt durability or namespace identity is unknown."""


BLOCKED_REASONS = frozenset(
    {
        "CONFIG_INVALID",
        "EMPTY_INVENTORY",
        "OBJECT_URI_PREFIX_MISMATCH",
        "EVIDENCE_BLOCKED",
        "RESOURCE_BOUND_EXCEEDED",
        "RECEIPT_INVALID",
    }
)
UNEXPECTED_ERROR_REASON = "UNEXPECTED_AUDIT_ERROR"
PUBLICATION_FAILED_CODE = "RECEIPT_PUBLICATION_FAILED"
PUBLICATION_INDETERMINATE_CODE = "RECEIPT_PUBLICATION_INDETERMINATE"


@dataclass(frozen=True)
class InventorySubject:
    lane: str
    subject_id: str
    source_id: str | None
    cycle_time: datetime
    start: datetime
    end: datetime
    model_id: str
    basin_version_id: str | None = None
    hot_uri: str = ""
    checksum: str | None = None
    state_id: str | None = None
    cloned_from_state_id: str | None = None
    cloned_from_model_id: str | None = None
    clone_gate_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.lane not in {"forcing", "runs", "states"}:
            raise AuditBlocked(f"unknown subject lane: {self.lane}")
        if not self.subject_id or not self.model_id:
            raise AuditBlocked("subject identity fields must be non-empty")
        for value in (self.cycle_time, self.start, self.end):
            _require_aware(value)
        cycle = _require_aware(self.cycle_time)
        if self.lane in {"forcing", "runs"} and (cycle.minute or cycle.second or cycle.microsecond):
            raise AuditBlocked(f"{self.lane} cycle_time must be an exact UTC hour: {self.cycle_time!r}")
        if self.start > self.end:
            raise AuditBlocked(f"inverted subject window: {self.stable_key}")

    @property
    def stable_key(self) -> tuple[str, str]:
        return self.lane, self.subject_id

    @property
    def window(self) -> dict[str, str]:
        return {"start": _time(self.start), "end": _time(self.end)}

    @property
    def selector(self) -> dict[str, Any] | None:
        if self.lane == "states":
            return None
        identity_key = "forcing_version_id" if self.lane == "forcing" else "run_id"
        table = "met.forcing_station_timeseries" if self.lane == "forcing" else "hydro.river_timeseries"
        return {"table": table, "identity": {identity_key: self.subject_id}, "window": self.window}

    @property
    def archive_identity(self) -> ArchiveIdentity:
        if self.lane == "states":
            physical_model = self.cloned_from_model_id or self.model_id
            return archive_identity_for_state_reference(
                source_id=self.source_id, model_id=physical_model, valid_time=self.cycle_time
            )
        source = normalize_source_id(self.source_id or "")
        cycle = self.cycle_time.astimezone(UTC)
        return ArchiveIdentity(
            lane=self.lane,
            source=source,
            cycle_identity=cycle.strftime("%Y%m%d%H"),
            cycle_time=cycle.strftime("%Y-%m-%dT%H:00:00Z"),
            basin_version_id=self.basin_version_id if self.lane == "forcing" else None,
            model_id=self.model_id if self.lane == "forcing" else None,
            run_id=self.subject_id if self.lane == "runs" else None,
        )


@dataclass(frozen=True)
class AuditConfig:
    database_url: str
    object_store_root: Path
    object_store_prefix: str
    archive_root: Path
    archive_min_age_days: int
    receipt_path: Path
    zstd_path: Path = Path("/usr/bin/zstd")


@dataclass(frozen=True)
class Coverage:
    mechanism: str
    evidence: tuple[str, ...] = ()


class ConnectionFactory(Protocol):
    def __call__(self, dsn: str) -> Any: ...


FORCING_INVENTORY_SQL = """
SELECT fv.forcing_version_id, fv.model_id, fv.source_id, fv.cycle_time,
       fv.start_time, fv.end_time, fv.forcing_package_uri, fv.checksum,
       mi.basin_version_id
FROM met.forcing_version fv
JOIN core.model_instance mi ON mi.model_id = fv.model_id
CROSS JOIN LATERAL (
  SELECT 1 AS detail_present
  FROM met.forcing_station_timeseries x
  WHERE x.forcing_version_id = fv.forcing_version_id
  LIMIT 1
) fst_presence
ORDER BY fv.forcing_version_id
LIMIT 100001
"""

RUN_INVENTORY_SQL = """
SELECT r.run_id, r.model_id, r.basin_version_id, r.source_id, r.cycle_time,
       r.start_time, r.end_time, r.run_manifest_uri, r.output_uri,
       rt_presence.detail_present
FROM hydro.hydro_run r
CROSS JOIN LATERAL (
  SELECT 1 AS detail_present
  FROM hydro.river_timeseries x
  WHERE x.run_id = r.run_id
  LIMIT 1
) rt_presence
ORDER BY r.run_id
LIMIT 100001
"""

STATE_INVENTORY_SQL = """
SELECT ss.state_id, ss.model_id, ss.run_id, ss.source_id, ss.valid_time, ss.state_uri, ss.checksum,
       ss.cloned_from_state_id, ss.cloned_from_model_id, ss.clone_gate_fingerprint,
       origin.state_id AS origin_state_id, origin.model_id AS origin_model_id,
       origin.source_id AS origin_source_id, origin.valid_time AS origin_valid_time,
       origin.state_uri AS origin_state_uri, origin.checksum AS origin_checksum
FROM hydro.state_snapshot ss
LEFT JOIN hydro.state_snapshot origin ON origin.state_id = ss.cloned_from_state_id
ORDER BY ss.state_id
LIMIT 100001
"""


def load_inventory(connection: Any) -> tuple[datetime, list[InventorySubject]]:
    """Capture all subjects in one bounded, read-only repeatable-read snapshot."""
    with connection.cursor() as cursor:
        cursor.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")
        cursor.execute("SELECT statement_timestamp() AS audit_time")
        audit_time = _row_mapping(cursor, cursor.fetchone())["audit_time"]
        cursor.execute(FORCING_INVENTORY_SQL)
        forcing_rows = [_row_mapping(cursor, row) for row in cursor.fetchall()]
        cursor.execute(RUN_INVENTORY_SQL)
        run_rows = [_row_mapping(cursor, row) for row in cursor.fetchall()]
        cursor.execute(STATE_INVENTORY_SQL)
        state_rows = [_row_mapping(cursor, row) for row in cursor.fetchall()]
    connection.rollback()
    if len(forcing_rows) + len(run_rows) + len(state_rows) > MAX_SUBJECTS:
        raise AuditBlocked(f"inventory exceeds {MAX_SUBJECTS} subjects")
    subjects: list[InventorySubject] = []
    for row in forcing_rows:
        if not row["cycle_time"]:
            raise AuditBlocked(f"forcing version {row['forcing_version_id']} has no cycle_time")
        subjects.append(
            InventorySubject(
                lane="forcing",
                subject_id=str(row["forcing_version_id"]),
                source_id=str(row["source_id"]),
                cycle_time=row["cycle_time"],
                start=row["start_time"],
                end=row["end_time"],
                model_id=str(row["model_id"]),
                basin_version_id=str(row["basin_version_id"]),
                hot_uri=str(row["forcing_package_uri"]),
                checksum=str(row["checksum"] or ""),
            )
        )
    for row in run_rows:
        if not row["cycle_time"]:
            raise AuditBlocked(f"run {row['run_id']} has no cycle_time")
        subjects.append(
            InventorySubject(
                lane="runs",
                subject_id=str(row["run_id"]),
                source_id=str(row["source_id"] or ""),
                cycle_time=row["cycle_time"],
                start=row["start_time"],
                end=row["end_time"],
                model_id=str(row["model_id"]),
                basin_version_id=str(row["basin_version_id"]),
                hot_uri=json.dumps({"manifest": row["run_manifest_uri"], "output": row["output_uri"]}),
            )
        )
    for row in state_rows:
        clone_values = [
            row.get(name) for name in ("cloned_from_state_id", "cloned_from_model_id", "clone_gate_fingerprint")
        ]
        clone_presence = [value is not None for value in clone_values]
        if any(clone_presence) and not all(clone_presence):
            raise AuditBlocked(f"state {row['state_id']} has incomplete clone provenance")
        if all(clone_presence):
            _validate_clone_provenance(row)
        subjects.append(
            InventorySubject(
                lane="states",
                subject_id=str(row["state_id"]),
                state_id=str(row["state_id"]),
                source_id=str(row["source_id"]) if row["source_id"] not in (None, "") else None,
                cycle_time=row["valid_time"],
                start=row["valid_time"],
                end=row["valid_time"],
                model_id=str(row["model_id"]),
                hot_uri=str(row["state_uri"]),
                checksum=str(row["checksum"]),
                cloned_from_state_id=row.get("cloned_from_state_id"),
                cloned_from_model_id=row.get("cloned_from_model_id"),
                clone_gate_fingerprint=row.get("clone_gate_fingerprint"),
            )
        )
    if not subjects:
        raise AuditBlocked("inventory is empty")
    if len(subjects) > MAX_SUBJECTS:
        raise AuditBlocked(f"inventory exceeds {MAX_SUBJECTS} subjects")
    if len({subject.stable_key for subject in subjects}) != len(subjects):
        raise AuditBlocked("inventory contains duplicate stable subjects")
    return _require_aware(audit_time), sorted(subjects, key=lambda value: value.stable_key)


def discover_salvage(
    archive_root: Path, *, mismatch_evidence: dict[str, str] | None = None
) -> tuple[dict[str, Any], ...]:
    """Return verified exact selectors from a bounded, symlink-safe namespace scan."""
    base = archive_root / "db-export"
    schema = _load_schema(SALVAGE_SCHEMA_PATH)
    try:
        base_fd = open_directory_no_follow(base, containment_root=archive_root)
    except FileNotFoundError:
        return ()
    except (OSError, SafeFilesystemError) as error:
        raise AuditBlocked(f"cannot open salvage namespace safely {base}: {error}") from error
    found: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    scanned_entries = 0
    manifest_count = 0

    def walk(directory_fd: int, directory: Path, depth: int) -> None:
        nonlocal manifest_count, scanned_entries
        names = _list_salvage_directory_fd(
            directory_fd, directory, MAX_SALVAGE_ENTRIES - scanned_entries
        )
        if scanned_entries + len(names) > MAX_SALVAGE_ENTRIES:
            raise AuditBlocked(f"salvage scan exceeds {MAX_SALVAGE_ENTRIES} total entries")
        scanned_entries += len(names)
        for name in names:
            entry = directory / name
            info = _stat_salvage_entry(directory_fd, name, entry)
            if stat.S_ISLNK(info.st_mode):
                raise AuditBlocked(f"unsafe salvage symlink: {entry}")
            if stat.S_ISDIR(info.st_mode):
                if depth >= MAX_SALVAGE_DEPTH:
                    raise AuditBlocked(f"salvage scan exceeds depth {MAX_SALVAGE_DEPTH}: {entry}")
                child_fd = _open_salvage_child_dir(directory_fd, name, entry, info)
                try:
                    walk(child_fd, entry, depth + 1)
                finally:
                    os.close(child_fd)
            elif name == "manifest.json" and stat.S_ISREG(info.st_mode):
                manifest_count += 1
                if manifest_count > MAX_SALVAGE_MANIFESTS:
                    raise AuditBlocked(f"salvage scan exceeds {MAX_SALVAGE_MANIFESTS} manifests")
                manifest_fd = _open_salvage_regular_file(directory_fd, name, entry, info)
                try:
                    manifest = _read_json_fd(manifest_fd, entry)
                finally:
                    os.close(manifest_fd)
                _validate_schema(manifest, schema, str(entry))
                for export in manifest["exports"]:
                    selector = export["selector"]
                    key = _canonical(selector)
                    if key in seen:
                        raise AuditBlocked(f"duplicate/conflicting salvage selector: {key}")
                    seen.add(key)
                    obj = export["object"]
                    object_fd = _open_salvage_object(base_fd, obj["path"], base)
                    try:
                        object_label = base / Path(obj["path"]).relative_to("db-export")
                        size, digest = _size_sha256_fd(object_fd, object_label)
                    finally:
                        os.close(object_fd)
                    if size != obj["size_bytes"] or digest != obj["sha256"]:
                        if mismatch_evidence is not None:
                            mismatch_evidence[key] = "db-export object size/sha256 mismatch"
                        continue
                    found[key] = selector

    try:
        walk(base_fd, base, 0)
    finally:
        os.close(base_fd)
    return tuple(found[key] for key in sorted(found))


def _list_salvage_directory_fd(directory_fd: int, label: Path, max_entries: int) -> list[str]:
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > max_entries:
                    break
    except OSError as error:
        raise AuditBlocked(f"cannot list salvage directory safely {label}: {error}") from error
    return sorted(names)


def _stat_salvage_entry(directory_fd: int, name: str, label: Path) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise AuditBlocked(f"salvage namespace changed during traversal: {label}") from error
    except OSError as error:
        raise AuditBlocked(f"cannot stat salvage entry safely {label}: {error}") from error


def _open_salvage_child_dir(
    directory_fd: int, name: str, label: Path, expected: os.stat_result
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        child_fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        raise AuditBlocked(f"cannot open salvage directory safely {label}: {error}") from error
    try:
        opened = os.fstat(child_fd)
    except OSError as error:
        os.close(child_fd)
        raise AuditBlocked(f"cannot verify salvage directory safely {label}: {error}") from error
    if opened.st_dev != expected.st_dev or opened.st_ino != expected.st_ino:
        os.close(child_fd)
        raise AuditBlocked(f"salvage directory changed while being opened: {label}")
    return child_fd


def _open_salvage_regular_file(
    directory_fd: int, name: str, label: Path, expected: os.stat_result
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        file_fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        raise AuditBlocked(f"cannot open salvage file safely {label}: {error}") from error
    try:
        opened = os.fstat(file_fd)
    except OSError as error:
        os.close(file_fd)
        raise AuditBlocked(f"cannot verify salvage file safely {label}: {error}") from error
    if not stat.S_ISREG(opened.st_mode):
        os.close(file_fd)
        raise AuditBlocked(f"salvage object is not a regular file: {label}")
    if opened.st_dev != expected.st_dev or opened.st_ino != expected.st_ino:
        os.close(file_fd)
        raise AuditBlocked(f"salvage file changed while being opened: {label}")
    return file_fd


def _open_salvage_object(base_fd: int, relative_path: str, base_label: Path) -> int:
    path = Path(relative_path)
    parts = path.parts
    if not parts or parts[0] != "db-export" or len(parts) < 2:
        raise AuditBlocked(f"salvage object is outside db-export namespace: {relative_path}")
    current_fd = os.dup(base_fd)
    try:
        current_label = base_label
        for part in parts[1:-1]:
            current_label /= part
            expected = _stat_salvage_entry(current_fd, part, current_label)
            if not stat.S_ISDIR(expected.st_mode):
                raise AuditBlocked(f"salvage object parent is not a directory: {current_label}")
            child_fd = _open_salvage_child_dir(current_fd, part, current_label, expected)
            os.close(current_fd)
            current_fd = child_fd
        label = current_label / parts[-1]
        expected = _stat_salvage_entry(current_fd, parts[-1], label)
        if not stat.S_ISREG(expected.st_mode):
            raise AuditBlocked(f"salvage object is not a regular file: {label}")
        return _open_salvage_regular_file(current_fd, parts[-1], label, expected)
    finally:
        os.close(current_fd)


def _read_json_fd(file_fd: int, label: Path) -> dict[str, Any]:
    content = bytearray()
    try:
        while len(content) <= MAX_MANIFEST_BYTES:
            chunk = os.read(file_fd, MAX_MANIFEST_BYTES + 1 - len(content))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > MAX_MANIFEST_BYTES:
            raise AuditBlocked(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes: {label}")
        value = json.loads(content)
    except AuditBlocked:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuditBlocked(f"cannot read JSON evidence {label}: {error}") from error
    if not isinstance(value, dict):
        raise AuditBlocked(f"JSON evidence must be an object: {label}")
    return value


def _size_sha256_fd(file_fd: int, label: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        while chunk := os.read(file_fd, 1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    except OSError as error:
        raise AuditBlocked(f"cannot checksum evidence {label}: {error}") from error
    return size, digest.hexdigest()


def verify_product_archive(
    subject: InventorySubject,
    archive_root: Path,
    object_store_prefix: str = "",
    zstd_path: Path = Path("/usr/bin/zstd"),
) -> Coverage | None:
    paths = archive_provenance_paths(archive_root, identity=subject.archive_identity)
    manifest_result = _read_json_with_digest_optional(paths.manifest, archive_root)
    archive_result = _size_sha256_optional(paths.archive, archive_root)
    if manifest_result is None and archive_result is None:
        return None
    if manifest_result is None or archive_result is None:
        return None
    manifest = manifest_result[0]
    _validate_schema(manifest, _load_schema(PRODUCT_SCHEMA_PATH), str(paths.manifest))
    try:
        expected = validate_product_archive_manifest_binding(archive_root, manifest)
    except ArchiveConfigurationError as error:
        raise AuditBlocked(str(error)) from error
    if expected != paths:
        raise AuditBlocked(f"archive path binding differs for {subject.stable_key}")
    if subject.lane in {"forcing", "runs"}:
        _verify_product_producer_provenance(subject, manifest)
    if subject.lane == "states":
        member_path = _state_archive_member_path(subject, object_store_prefix)
        members = [entry for entry in manifest["files"] if entry["path"] == member_path]
        if len(members) != 1:
            raise AuditBlocked(
                f"state archive manifest must contain exactly one bound member {member_path!r}: {subject.subject_id}"
            )
        if members[0]["sha256"] != subject.checksum:
            raise AuditBlocked(f"state archive member checksum differs from state provenance: {subject.subject_id}")
    size, digest = archive_result
    declared = manifest["archive"]
    if size != declared["size_bytes"] or digest != declared["sha256"]:
        return Coverage("none", ("product archive size/sha256 mismatch",))
    try:
        validated_zstd = product_archive._validate_zstd(zstd_path)
        verified_manifest = product_archive.verify_archive_pair(
            paths.archive.parent,
            archive_root,
            zstd_path=validated_zstd,
            object_store_prefix=object_store_prefix,
            mount_id_provider=_archive_mount_id,
        )
    except product_archive.ArchiveMoverError as error:
        raise AuditBlocked(f"product archive content verification failed: {error}") from error
    if verified_manifest != manifest:
        raise AuditBlocked(f"product archive manifest changed during verification: {subject.subject_id}")
    return Coverage("product-archive", ("member-verified product archive present",))


def _archive_mount_id(fd: int) -> int:
    """Use Linux mount IDs in production and a device proof for local portable tests."""
    if Path(f"/proc/self/fdinfo/{fd}").exists():
        return product_archive.fd_mount_id(fd)
    return os.fstat(fd).st_dev


def _verify_product_producer_provenance(subject: InventorySubject, manifest: Mapping[str, Any]) -> None:
    producer = manifest.get("producer")
    if not isinstance(producer, Mapping):
        raise AuditBlocked(f"product archive lacks producer provenance: {subject.subject_id}")
    expected_kind = "forcing-package" if subject.lane == "forcing" else "run-manifest"
    expected_path = "forcing_package.json" if subject.lane == "forcing" else "input/manifest.json"
    expected = {
        "kind": expected_kind,
        "subject_id": subject.subject_id,
        "manifest_path": expected_path,
        "start_time": _time(subject.start),
        "end_time": _time(subject.end),
        "model_id": subject.model_id,
        "basin_version_id": subject.basin_version_id,
    }
    for field, expected_value in expected.items():
        if producer.get(field) != expected_value:
            raise AuditBlocked(
                f"product archive producer {field} differs from DB inventory for {subject.subject_id}"
            )
    digest = producer.get("manifest_sha256")
    if not isinstance(digest, str):
        raise AuditBlocked(f"product archive producer manifest digest is missing: {subject.subject_id}")
    if subject.lane == "forcing" and digest != subject.checksum:
        raise AuditBlocked(f"forcing producer manifest digest differs from DB provenance: {subject.subject_id}")
    members = [entry for entry in manifest["files"] if entry["path"] == expected_path]
    if len(members) != 1 or members[0]["sha256"] != digest:
        raise AuditBlocked(
            f"product archive producer manifest member does not bind its declared digest: {subject.subject_id}"
        )


def _state_archive_member_path(subject: InventorySubject, object_store_prefix: str) -> str:
    key = _object_key(subject.hot_uri, object_store_prefix)
    physical_model = subject.cloned_from_model_id or subject.model_id
    cycle = subject.cycle_time.astimezone(UTC).strftime("%Y%m%d%H")
    if subject.source_id:
        state_root = f"states/{normalize_source_id(subject.source_id)}/{physical_model}/{cycle}/"
    else:
        state_root = f"states/{physical_model}/{cycle}/"
    if not key.startswith(state_root):
        raise AuditBlocked(f"state URI does not bind to archive physical identity: {subject.subject_id}")
    member_path = key[len(state_root) :]
    if not member_path or any(part in {"", ".", ".."} for part in member_path.split("/")):
        raise AuditBlocked(f"state URI has no safe archive member path: {subject.subject_id}")
    return member_path


def verify_hot(subject: InventorySubject, config: AuditConfig) -> Coverage | None:
    if subject.lane == "forcing":
        return _verify_forcing_hot(subject, config)
    if subject.lane == "runs":
        return _verify_run_hot(subject, config)
    return _verify_state_hot(subject, config)


def _verify_forcing_hot(subject: InventorySubject, config: AuditConfig) -> Coverage | None:
    cycle = subject.cycle_time.astimezone(UTC).strftime("%Y%m%d%H")
    source = normalize_source_id(subject.source_id or "").lower()
    expected = f"forcing/{source}/{cycle}/{subject.basin_version_id}/{subject.model_id}"
    key = _object_key(subject.hot_uri, config.object_store_prefix)
    if key != expected:
        raise AuditBlocked(f"forcing URI identity mismatch for {subject.subject_id}: {key}")
    manifest_path = config.object_store_root / key / "forcing_package.json"
    manifest_result = _read_json_with_digest_optional(manifest_path, config.object_store_root)
    if manifest_result is None:
        return None
    manifest, _size, manifest_digest = manifest_result
    mismatch_evidence: list[str] = []
    if not subject.checksum or manifest_digest != subject.checksum:
        mismatch_evidence.append("hot forcing manifest checksum mismatch")
    identity = {
        "forcing_version_id": subject.subject_id,
        "source_id": subject.source_id,
        "cycle_time": _time(subject.cycle_time),
        "model_id": subject.model_id,
        "basin_version_id": subject.basin_version_id,
    }
    for name, expected_value in identity.items():
        actual = manifest.get(name)
        if name == "source_id":
            try:
                actual, expected_value = normalize_source_id(str(actual)), normalize_source_id(str(expected_value))
            except ValueError as error:
                raise AuditBlocked(f"forcing manifest source invalid: {error}") from error
        elif name == "cycle_time":
            actual = _time(_parse_time(actual))
        if actual != expected_value:
            raise AuditBlocked(f"forcing manifest {name} mismatch for {subject.subject_id}")
    manifest_start = _parse_time(manifest.get("start_time"))
    manifest_end = _parse_time(manifest.get("end_time"))
    if manifest_start > subject.start or manifest_end < subject.end:
        raise AuditBlocked(f"forcing manifest range does not contain DB window: {subject.subject_id}")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise AuditBlocked(f"forcing manifest has no files: {subject.subject_id}")
    for entry in files:
        if (
            not isinstance(entry, Mapping)
            or not isinstance(entry.get("uri"), str)
            or not isinstance(entry.get("checksum"), str)
        ):
            raise AuditBlocked(f"malformed forcing file entry: {subject.subject_id}")
        file_key = _object_key(entry["uri"], config.object_store_prefix)
        if not file_key.startswith(expected + "/"):
            raise AuditBlocked(f"forcing file escapes package: {file_key}")
        path = config.object_store_root / file_key
        try:
            digest = _sha256(path, config.object_store_root)
        except FileNotFoundError:
            mismatch_evidence.append(f"hot forcing member missing: {file_key}")
            continue
        if digest != entry["checksum"]:
            mismatch_evidence.append(f"hot forcing member checksum mismatch: {file_key}")
    if mismatch_evidence:
        return Coverage("none", tuple(mismatch_evidence))
    return Coverage("hot-object-store", ("row-bound forcing package and files checksum-verified",))


def _verify_run_hot(subject: InventorySubject, config: AuditConfig) -> Coverage | None:
    try:
        refs = json.loads(subject.hot_uri)
    except json.JSONDecodeError as error:
        raise AuditBlocked(f"malformed internal run refs: {subject.subject_id}") from error
    manifest_key = _object_key(str(refs.get("manifest") or ""), config.object_store_prefix)
    output_key = _object_key(str(refs.get("output") or ""), config.object_store_prefix).rstrip("/")
    expected_manifest = f"runs/{subject.subject_id}/input/manifest.json"
    expected_output = f"runs/{subject.subject_id}/output"
    if manifest_key != expected_manifest or output_key != expected_output:
        raise AuditBlocked(f"run URI identity mismatch for {subject.subject_id}")
    manifest_path = config.object_store_root / manifest_key
    output_path = config.object_store_root / output_key
    manifest_result = _read_json_with_digest_optional(manifest_path, config.object_store_root)
    output_has_regular = _directory_has_regular_file_optional(output_path, config.object_store_root)
    if manifest_result is None and output_has_regular is None:
        return None
    if manifest_result is None:
        raise AuditBlocked(f"run output exists without input manifest: {subject.subject_id}")
    manifest = manifest_result[0]
    expected = {
        "run_id": subject.subject_id,
        "source_id": normalize_source_id(subject.source_id or ""),
        "cycle_time": _time(subject.cycle_time),
        "start_time": _time(subject.start),
        "end_time": _time(subject.end),
    }
    actual_model = manifest.get("model")
    if not isinstance(actual_model, Mapping):
        raise AuditBlocked(f"run manifest missing model identity: {subject.subject_id}")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        raise AuditBlocked(f"run manifest missing output identity: {subject.subject_id}")
    actual = {
        "run_id": manifest.get("run_id"),
        "source_id": normalize_source_id(str(manifest.get("source_id") or "")),
        "cycle_time": _time(_parse_time(manifest.get("cycle_time"))),
        "start_time": _time(_parse_time(manifest.get("start_time"))),
        "end_time": _time(_parse_time(manifest.get("end_time"))),
    }
    if (
        actual != expected
        or actual_model.get("model_id") != subject.model_id
        or actual_model.get("basin_version_id") != subject.basin_version_id
        or _object_key(str(outputs.get("run_manifest_uri") or ""), config.object_store_prefix) != expected_manifest
        or _object_key(str(outputs.get("output_uri") or ""), config.object_store_prefix).rstrip("/") != expected_output
    ):
        raise AuditBlocked(f"run manifest row identity mismatch: {subject.subject_id}")
    if output_has_regular is None:
        raise AuditBlocked(f"run input manifest exists without output directory: {subject.subject_id}")
    if not output_has_regular:
        raise AuditBlocked(f"run output has no regular product: {subject.subject_id}")
    return Coverage("hot-object-store", ("row-bound input manifest and run output present",))


def _verify_state_hot(subject: InventorySubject, config: AuditConfig) -> Coverage | None:
    key = _object_key(subject.hot_uri, config.object_store_prefix)
    physical_model = subject.cloned_from_model_id or subject.model_id
    cycle = subject.cycle_time.astimezone(UTC).strftime("%Y%m%d%H")
    if subject.source_id:
        expected_prefix = f"states/{normalize_source_id(subject.source_id)}/{physical_model}/{cycle}/"
    else:
        expected_prefix = f"states/{physical_model}/{cycle}/"
    if not key.startswith(expected_prefix):
        raise AuditBlocked(f"state URI row/provenance identity mismatch: {subject.subject_id}")
    path = config.object_store_root / key
    digest = _sha256_optional(path, config.object_store_root)
    if digest is None:
        return None
    if digest != subject.checksum:
        return Coverage("none", (f"hot state checksum mismatch: {subject.subject_id}",))
    return Coverage("hot-object-store", ("state artifact checksum-verified",))


def build_receipt(
    subjects: Sequence[InventorySubject],
    *,
    audit_time: datetime,
    archive_min_age_days: int,
    product_coverage: Mapping[tuple[str, str], Coverage | None],
    salvage_selectors: Sequence[Mapping[str, Any]],
    hot_coverage: Mapping[tuple[str, str], Coverage | None],
    salvage_mismatches: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not subjects:
        raise AuditBlocked("inventory is empty")
    audit_time = _require_aware(audit_time)
    keys = [subject.stable_key for subject in subjects]
    if len(set(keys)) != len(keys):
        raise AuditBlocked("duplicate inventory subject")
    salvage = {_canonical(selector): selector for selector in salvage_selectors}
    windows: list[dict[str, Any]] = []
    required_selectors: dict[str, dict[str, Any]] = {}
    for subject in sorted(subjects, key=lambda value: value.stable_key):
        evidence: list[str] = []
        product = product_coverage.get(subject.stable_key)
        hot = hot_coverage.get(subject.stable_key)
        selector_key = _canonical(subject.selector) if subject.selector is not None else None
        if product and product.mechanism != "product-archive":
            evidence.extend(product.evidence)
        if selector_key is not None and salvage_mismatches and selector_key in salvage_mismatches:
            evidence.append(salvage_mismatches[selector_key])
        if hot and hot.mechanism != "hot-object-store":
            evidence.extend(hot.evidence)
        if product and product.mechanism == "product-archive":
            coverage, verdict = "product-archive", "complete"
            evidence.extend(product.evidence)
        elif selector_key is not None and selector_key in salvage:
            coverage, verdict = "db-export", "complete"
            evidence.append("checksum-verified exact db-export selector present")
        else:
            if hot and hot.mechanism == "hot-object-store":
                coverage = "hot-object-store"
                if subject.end > audit_time - timedelta(days=archive_min_age_days):
                    verdict = "complete"
                else:
                    verdict = "pending-archive"
                evidence.extend(hot.evidence)
            else:
                coverage, verdict = "none", "gap"
                evidence.append("no verified archive, db-export, or hot product")
                if subject.selector is not None:
                    required_selectors[selector_key] = subject.selector
        identity_key = {"forcing": "forcing_version_id", "runs": "run_id", "states": "state_id"}[subject.lane]
        windows.append(
            {
                "lane": subject.lane,
                "subject": {identity_key: subject.subject_id},
                "window": subject.window,
                "coverage": coverage,
                "verdict": verdict,
                "evidence": evidence,
            }
        )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _time(audit_time),
        "outcome": "complete" if all(item["verdict"] == "complete" for item in windows) else "incomplete",
        "coverage_bounds": {
            "start": min(item["window"]["start"] for item in windows),
            "end": max(item["window"]["end"] for item in windows),
        },
        "windows": windows,
        "salvage_selectors": [required_selectors[key] for key in sorted(required_selectors)],
    }
    validate_receipt_semantics(receipt, subjects)
    _validate_schema(receipt, _load_schema(COMPLETENESS_SCHEMA_PATH), "archive completeness receipt")
    return receipt


def validate_receipt_semantics(receipt: Mapping[str, Any], subjects: Sequence[InventorySubject] | None = None) -> None:
    outcome = receipt.get("outcome")
    if outcome in {"blocked", "indeterminate"}:
        _validate_schema(receipt, _load_schema(COMPLETENESS_SCHEMA_PATH), "archive completeness receipt")
        return
    if outcome not in {"complete", "incomplete"}:
        raise AuditBlocked("receipt outcome is invalid")
    windows = receipt.get("windows")
    selectors = receipt.get("salvage_selectors")
    if not isinstance(windows, list) or not windows or not isinstance(selectors, list):
        raise AuditBlocked("receipt windows/selectors have invalid shape")
    subject_keys: list[tuple[str, str]] = []
    expected_selectors: set[str] = set()
    starts: list[str] = []
    ends: list[str] = []
    for item in windows:
        lane = item["lane"]
        identity_key = {"forcing": "forcing_version_id", "runs": "run_id", "states": "state_id"}[lane]
        subject_keys.append((lane, item["subject"][identity_key]))
        start, end = item["window"]["start"], item["window"]["end"]
        if _parse_time(start) > _parse_time(end):
            raise AuditBlocked("receipt contains inverted window")
        starts.append(start)
        ends.append(end)
        if item["verdict"] == "gap" and lane != "states":
            table = "met.forcing_station_timeseries" if lane == "forcing" else "hydro.river_timeseries"
            expected_selectors.add(
                _canonical(
                    {
                        "table": table,
                        "identity": {identity_key: item["subject"][identity_key]},
                        "window": item["window"],
                    }
                )
            )
    if len(set(subject_keys)) != len(subject_keys):
        raise AuditBlocked("receipt contains duplicate subject")
    if subjects is not None and set(subject_keys) != {subject.stable_key for subject in subjects}:
        raise AuditBlocked("receipt omitted or invented inventory subjects")
    actual_selectors = [_canonical(selector) for selector in selectors]
    if len(set(actual_selectors)) != len(actual_selectors) or set(actual_selectors) != expected_selectors:
        raise AuditBlocked("forcing/run gap-selector bijection failed")
    if receipt.get("coverage_bounds") != {"start": min(starts), "end": max(ends)}:
        raise AuditBlocked("receipt coverage_bounds do not match subject set")
    all_complete = all(item.get("verdict") == "complete" for item in windows)
    if (outcome == "complete") != all_complete:
        raise AuditBlocked("receipt outcome contradicts subject verdict aggregate")
    if outcome == "complete" and selectors:
        raise AuditBlocked("complete receipt must not contain salvage selectors")


def publish_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    path = _validate_output_path(path)
    payload = (_canonical(receipt) + "\n").encode()
    try:
        atomic_write_bytes_no_follow(
            path,
            payload,
            containment_root=path.parent,
            mode=0o600,
            require_durable_replace=True,
        )
    except SafeFilesystemError as error:
        if error.kind == "indeterminate":
            raise PublicationIndeterminate(f"receipt publication is indeterminate: {error}") from error
        raise AuditBlocked(f"failed to publish receipt safely: {error}") from error


def run_audit(
    config: AuditConfig,
    *,
    connect: ConnectionFactory | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    _validate_audit_roots(config)
    if connect is None:
        import psycopg2

        connect = psycopg2.connect
    connection = connect(config.database_url)
    try:
        audit_time, subjects = load_inventory(connection)
    finally:
        connection.close()
    salvage_mismatches: dict[str, str] = {}
    salvage = discover_salvage(config.archive_root, mismatch_evidence=salvage_mismatches)
    product: dict[tuple[str, str], Coverage | None] = {}
    hot: dict[tuple[str, str], Coverage | None] = {}
    for subject in subjects:
        product[subject.stable_key] = verify_product_archive(
            subject, config.archive_root, config.object_store_prefix, config.zstd_path
        )
        hot[subject.stable_key] = verify_hot(subject, config)
    receipt = build_receipt(
        subjects,
        audit_time=audit_time,
        archive_min_age_days=config.archive_min_age_days,
        product_coverage=product,
        salvage_selectors=salvage,
        hot_coverage=hot,
        salvage_mismatches=salvage_mismatches,
    )
    if publish:
        publish_receipt(config.receipt_path, receipt)
    return receipt


def config_from_args(args: argparse.Namespace) -> AuditConfig:
    database_url = (args.database_url or os.getenv("DATABASE_URL") or "").strip()
    object_root = _absolute(args.object_store_root or os.getenv("OBJECT_STORE_ROOT"), "object_store_root")
    archive_root = _absolute(
        args.archive_root or os.getenv("NODE27_STORAGE_INVENTORY_ARCHIVE_ROOT") or os.getenv("NHMS_ARCHIVE_ROOT"),
        "archive_root",
    )
    receipt_path = _absolute(args.receipt_path or os.getenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH"), "receipt_path")
    if not database_url:
        raise AuditBlocked("DATABASE_URL is required")
    raw_age: int | str = (
        args.archive_min_age_days
        if args.archive_min_age_days is not None
        else os.getenv("NHMS_ARCHIVE_MIN_AGE_DAYS", "45")
    )
    try:
        age = int(raw_age)
    except ValueError as error:
        raise AuditBlocked("NHMS_ARCHIVE_MIN_AGE_DAYS must be an integer") from error
    if age < DEFAULT_DB_RETENTION_DAYS:
        raise AuditBlocked(
            f"NHMS_ARCHIVE_MIN_AGE_DAYS must be at least DB retention ({DEFAULT_DB_RETENTION_DAYS} days)"
        )
    return AuditConfig(
        database_url,
        object_root,
        (args.object_store_prefix or os.getenv("OBJECT_STORE_PREFIX") or "").strip(),
        archive_root,
        age,
        receipt_path,
        _absolute(getattr(args, "zstd_path", None) or os.getenv("ZSTD_BIN") or "/usr/bin/zstd", "zstd_path"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _AuditArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--database-url")
    parser.add_argument("--object-store-root")
    parser.add_argument("--object-store-prefix")
    parser.add_argument("--archive-root")
    parser.add_argument("--archive-min-age-days", type=int)
    parser.add_argument("--receipt-path")
    parser.add_argument("--zstd-path")
    return parser


class _AuditArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AuditBlocked(f"invalid arguments: {message}")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        detail = message.strip() if message else "help requested" if status == 0 else "parser exited"
        raise AuditBlocked(f"invalid arguments: {detail}")


def bootstrap_receipt_path(argv: Sequence[str]) -> Path:
    """Resolve one exact receipt option before full CLI/config validation."""
    values: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            break
        if token == "--receipt-path":
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                raise AuditBlocked("--receipt-path requires one absolute value")
            values.append(argv[index + 1])
            index += 2
            continue
        if token.startswith("--receipt-path="):
            value = token.partition("=")[2]
            if not value:
                raise AuditBlocked("--receipt-path requires one absolute value")
            values.append(value)
        index += 1
    if len(values) > 1:
        raise AuditBlocked("--receipt-path must be supplied at most once")
    raw = values[0] if values else (os.getenv("NODE27_STORAGE_INVENTORY_RECEIPT_PATH") or "")
    return _validate_output_path(_absolute(raw, "receipt_path"))


def build_terminal_receipt(
    outcome: str,
    generated_at: datetime,
    *,
    reason: str,
    detail: str | None = None,
) -> dict[str, Any]:
    if outcome == "blocked":
        if reason not in BLOCKED_REASONS:
            raise AuditBlocked(f"unknown blocked reason: {reason}")
        receipt: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _time(generated_at),
            "outcome": "blocked",
            "refusal_reason": reason,
        }
    elif outcome == "indeterminate":
        if reason != UNEXPECTED_ERROR_REASON:
            raise AuditBlocked(f"unknown indeterminate reason: {reason}")
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _time(generated_at),
            "outcome": "indeterminate",
            "error_reason": reason,
        }
    else:
        raise AuditBlocked(f"invalid terminal outcome: {outcome}")
    if detail:
        receipt["detail"] = detail[:512]
    _validate_schema(receipt, _load_schema(COMPLETENESS_SCHEMA_PATH), "archive completeness receipt")
    return receipt


def _blocked_reason(error: AuditBlocked) -> str:
    message = str(error).lower()
    if "inventory is empty" in message:
        return "EMPTY_INVENTORY"
    if "outside configured prefix" in message:
        return "OBJECT_URI_PREFIX_MISMATCH"
    if "exceeds" in message or "resource bound" in message:
        return "RESOURCE_BOUND_EXCEEDED"
    if "receipt" in message and any(word in message for word in ("schema", "semantic", "selector", "outcome")):
        return "RECEIPT_INVALID"
    if any(
        word in message
        for word in (
            "required",
            "must be absolute",
            "invalid arguments",
            "minimum age",
            "at least db retention",
            "unsafe object-store root",
        )
    ):
        return "CONFIG_INVALID"
    return "EVIDENCE_BLOCKED"


def _sanitize_detail(error: BaseException, *, database_url: str | None = None) -> str:
    detail = str(error).replace("\n", " ").replace("\r", " ")
    for dsn in (database_url, os.getenv("DATABASE_URL")):
        if dsn:
            detail = detail.replace(dsn, "[DATABASE_URL]")
        detail = redact_database_dsn(detail, dsn)
    detail = redact_text(detail)
    return detail[:512] or type(error).__name__


def _emit_stderr(code: str, message: str) -> None:
    safe_message = redact_text(message)[:512]
    safe_code = redact_text(code)[:128]
    print(_canonical({"status": "blocked", "reason": safe_code, "message": safe_message}), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    generated_at = datetime.now(UTC)
    try:
        receipt_path = bootstrap_receipt_path(raw_argv)
    except Exception as error:
        _emit_stderr("CONFIG_INVALID", _sanitize_detail(error))
        return 1
    config: AuditConfig | None = None
    try:
        args = build_parser().parse_args(raw_argv)
        args.receipt_path = str(receipt_path)
        config = config_from_args(args)
        receipt = run_audit(config, publish=False)
        validate_receipt_semantics(receipt)
        _validate_schema(receipt, _load_schema(COMPLETENESS_SCHEMA_PATH), "archive completeness receipt")
        exit_code = 0
    except AuditBlocked as error:
        receipt = build_terminal_receipt(
            "blocked",
            generated_at,
            reason=_blocked_reason(error),
            detail=_sanitize_detail(error, database_url=config.database_url if config else None),
        )
        exit_code = 1
    except Exception as error:
        receipt = build_terminal_receipt(
            "indeterminate",
            generated_at,
            reason=UNEXPECTED_ERROR_REASON,
            detail=_sanitize_detail(error, database_url=config.database_url if config else None),
        )
        exit_code = 1
    try:
        publish_receipt(receipt_path, receipt)
    except PublicationIndeterminate as error:
        _emit_stderr(
            PUBLICATION_INDETERMINATE_CODE,
            _sanitize_detail(error, database_url=config.database_url if config else None),
        )
        return 1
    except Exception as error:
        _emit_stderr(
            PUBLICATION_FAILED_CODE,
            _sanitize_detail(error, database_url=config.database_url if config else None),
        )
        return 1
    print(
        _canonical(
            {
                "status": "published",
                "receipt_path": redact_text(str(receipt_path)),
                "outcome": receipt["outcome"],
                "subjects": len(receipt.get("windows", [])),
            }
        )
    )
    return exit_code


def _row_mapping(cursor: Any, row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {column[0]: value for column, value in zip(cursor.description, row, strict=True)}


def _validate_clone_provenance(row: Mapping[str, Any]) -> None:
    state_id = row["state_id"]
    fingerprint = row.get("clone_gate_fingerprint")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise AuditBlocked(f"state {state_id} has non-canonical clone fingerprint")
    if row.get("origin_state_id") is None:
        raise AuditBlocked(f"state {state_id} clone origin does not exist")
    checks = (
        (row.get("origin_state_id"), row.get("cloned_from_state_id"), "state_id"),
        (row.get("origin_model_id"), row.get("cloned_from_model_id"), "model_id"),
        (row.get("origin_valid_time"), row.get("valid_time"), "valid_time"),
        (row.get("origin_state_uri"), row.get("state_uri"), "state_uri"),
        (row.get("origin_checksum"), row.get("checksum"), "checksum"),
    )
    for actual, expected, field in checks:
        if actual != expected:
            raise AuditBlocked(f"state {state_id} clone origin {field} drift")
    if _clone_source_identity(row.get("origin_source_id")) != _clone_source_identity(row.get("source_id")):
        raise AuditBlocked(f"state {state_id} clone origin source_id drift")


def _clone_source_identity(value: Any) -> str:
    if value in (None, ""):
        return "legacy-unqualified"
    try:
        return normalize_source_id(str(value))
    except ValueError as error:
        raise AuditBlocked(f"invalid clone source identity: {value!r}") from error


def _load_schema(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_schema(instance: Any, schema: Mapping[str, Any], label: str) -> None:
    try:
        jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(instance)
    except jsonschema.ValidationError as error:
        raise AuditBlocked(
            f"{label} schema validation failed at {list(error.absolute_path)}: {error.message}"
        ) from error


def _read_json(path: Path, root: Path) -> dict[str, Any]:
    value, _size, _digest = _read_json_with_digest(path, root)
    return value


def _read_json_with_digest(path: Path, root: Path) -> tuple[dict[str, Any], int, str]:
    try:
        content = read_bytes_limited_no_follow(path, max_bytes=MAX_MANIFEST_BYTES, containment_root=root)
        if len(content) > MAX_MANIFEST_BYTES:
            raise AuditBlocked(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes: {path}")
        value = json.loads(content)
    except FileNotFoundError:
        raise
    except (OSError, SafeFilesystemError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuditBlocked(f"cannot read JSON evidence {path}: {error}") from error
    if not isinstance(value, dict):
        raise AuditBlocked(f"JSON evidence must be an object: {path}")
    return value, len(content), hashlib.sha256(content).hexdigest()


def _read_json_with_digest_optional(path: Path, root: Path) -> tuple[dict[str, Any], int, str] | None:
    try:
        return _read_json_with_digest(path, root)
    except FileNotFoundError:
        return None


def _safe_stat(path: Path, root: Path) -> os.stat_result:
    try:
        path.absolute().relative_to(root.absolute())
        return stat_no_follow(path, containment_root=root)
    except FileNotFoundError:
        raise
    except (OSError, SafeFilesystemError, ValueError) as error:
        raise AuditBlocked(f"unsafe or unreadable evidence path {path}: {error}") from error


def _directory_present(path: Path, root: Path) -> bool:
    try:
        info = _safe_stat(path, root)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        raise AuditBlocked(f"evidence is not a directory: {path}")
    return True


def _list_directory(path: Path, root: Path, max_entries: int) -> list[str]:
    try:
        return list_directory_no_follow_limited(path, max_entries=max_entries, containment_root=root)
    except FileNotFoundError:
        raise
    except (OSError, SafeFilesystemError) as error:
        raise AuditBlocked(f"cannot list directory safely {path}: {error}") from error


def _directory_has_regular_file(directory: Path, root: Path) -> bool:
    found = False
    seen = 0
    try:
        root_fd = open_directory_no_follow(directory, containment_root=root)
    except FileNotFoundError:
        raise
    except (OSError, SafeFilesystemError) as error:
        raise AuditBlocked(f"cannot open run output safely {directory}: {error}") from error
    stack: list[tuple[int, Path, int]] = [(root_fd, directory, 0)]
    try:
        while stack:
            current_fd, current, depth = stack.pop()
            try:
                names = _list_run_output_fd(current_fd, current, MAX_RUN_OUTPUT_ENTRIES - seen)
                if seen + len(names) > MAX_RUN_OUTPUT_ENTRIES:
                    raise AuditBlocked(f"run output exceeds {MAX_RUN_OUTPUT_ENTRIES} entries")
                seen += len(names)
                for name in names:
                    entry = current / name
                    info = _stat_run_output_entry(current_fd, name, entry)
                    if stat.S_ISDIR(info.st_mode):
                        if depth >= MAX_RUN_OUTPUT_DEPTH:
                            raise AuditBlocked(f"run output exceeds depth {MAX_RUN_OUTPUT_DEPTH}: {entry}")
                        child_fd = _open_run_output_child(current_fd, name, entry, info)
                        stack.append((child_fd, entry, depth + 1))
                    elif stat.S_ISREG(info.st_mode):
                        found = True
                    else:
                        raise AuditBlocked(f"unsafe non-regular run output: {entry}")
            finally:
                os.close(current_fd)
        return found
    finally:
        for pending_fd, _path, _depth in stack:
            os.close(pending_fd)


def _list_run_output_fd(directory_fd: int, path_label: Path, max_entries: int) -> list[str]:
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > max_entries:
                    break
    except OSError as error:
        raise AuditBlocked(f"cannot list run output safely {path_label}: {error}") from error
    return sorted(names)


def _stat_run_output_entry(directory_fd: int, name: str, path_label: Path) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise AuditBlocked(f"run output changed during traversal: {path_label}") from error
    except OSError as error:
        raise AuditBlocked(f"cannot stat run output safely {path_label}: {error}") from error


def _open_run_output_child(
    directory_fd: int, name: str, path_label: Path, expected: os.stat_result
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        child_fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        raise AuditBlocked(f"cannot open run output directory safely {path_label}: {error}") from error
    try:
        opened = os.fstat(child_fd)
    except OSError as error:
        os.close(child_fd)
        raise AuditBlocked(f"cannot verify run output directory safely {path_label}: {error}") from error
    if opened.st_dev != expected.st_dev or opened.st_ino != expected.st_ino:
        os.close(child_fd)
        raise AuditBlocked(f"run output directory changed while being opened: {path_label}")
    return child_fd


def _directory_has_regular_file_optional(directory: Path, root: Path) -> bool | None:
    try:
        return _directory_has_regular_file(directory, root)
    except FileNotFoundError:
        return None


def _object_key(uri: str, prefix: str) -> str:
    raw = uri.strip()
    if not raw or "?" in raw or "#" in raw or "\\" in raw:
        raise AuditBlocked(f"invalid object-store URI: {raw!r}")
    if raw.startswith("s3://"):
        parsed = urlparse(raw)
        if parsed.scheme != "s3" or not parsed.netloc:
            raise AuditBlocked(f"invalid object-store URI: {raw!r}")
        if not prefix:
            raise AuditBlocked("OBJECT_STORE_PREFIX is required for s3 URI binding")
        expected = urlparse(prefix.rstrip("/"))
        if expected.scheme != "s3" or expected.netloc != parsed.netloc:
            raise AuditBlocked(f"object URI outside configured prefix: {raw}")
        object_path, prefix_path = unquote(parsed.path).strip("/"), unquote(expected.path).strip("/")
        if prefix_path:
            if not object_path.startswith(prefix_path + "/"):
                raise AuditBlocked(f"object URI outside configured prefix: {raw}")
            object_path = object_path[len(prefix_path) + 1 :]
        raw = object_path
    elif "://" in raw or raw.startswith("/"):
        raise AuditBlocked(f"unsupported object-store URI: {raw}")
    key = raw.strip("/")
    parts = key.split("/")
    if (
        not key
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in key)
    ):
        raise AuditBlocked(f"unsafe object key: {key!r}")
    return key


def _validate_output_path(path: Path) -> Path:
    if not path.is_absolute():
        raise AuditBlocked(f"receipt path must be absolute: {path}")
    parent = path.parent
    try:
        verify_directory_no_follow(parent)
        stat_no_follow(path, containment_root=parent)
    except FileNotFoundError:
        pass
    except SafeFilesystemError as error:
        raise AuditBlocked(f"unsafe receipt path {path}: {error}") from error
    return path


def _validate_audit_roots(config: AuditConfig) -> None:
    try:
        verify_directory_no_follow(config.object_store_root)
    except (OSError, SafeFilesystemError) as error:
        raise AuditBlocked(f"unsafe object-store root: {error}") from error
    _directory_present(config.archive_root, Path(config.archive_root.anchor))
    _validate_output_path(config.receipt_path)


def _absolute(value: str | None, label: str) -> Path:
    if not value or not value.strip():
        raise AuditBlocked(f"{label} is required")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise AuditBlocked(f"{label} must be absolute: {path}")
    return path


def _require_aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AuditBlocked(f"timestamp must be timezone-aware: {value!r}")
    return value.astimezone(UTC)


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise AuditBlocked(f"timestamp must be a string: {value!r}")
    try:
        return _require_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as error:
        raise AuditBlocked(f"invalid timestamp: {value!r}") from error


def _time(value: datetime) -> str:
    return _require_aware(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(path: Path, root: Path) -> str:
    return _size_sha256(path, root)[1]


def _size_sha256(path: Path, root: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    file_fd: int | None = None
    try:
        file_fd = open_file_no_follow(path, containment_root=root)
        while chunk := os.read(file_fd, 1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    except FileNotFoundError:
        raise
    except (OSError, SafeFilesystemError) as error:
        raise AuditBlocked(f"cannot checksum evidence {path}: {error}") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
    return size, digest.hexdigest()


def _size_sha256_optional(path: Path, root: Path) -> tuple[int, str] | None:
    try:
        return _size_sha256(path, root)
    except FileNotFoundError:
        return None


def _sha256_optional(path: Path, root: Path) -> str | None:
    result = _size_sha256_optional(path, root)
    return None if result is None else result[1]


if __name__ == "__main__":
    raise SystemExit(main())
