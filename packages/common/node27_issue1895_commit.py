"""Exclusive two-artifact commit: receipt plus digest-bound marker."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.node27_issue1895_performance import commit_marker_name
from packages.common.node27_issue1895_probe import assert_report_within_command_bracket, parse_bracket_instant
from packages.common.node27_issue1895_receipt_validate import validate_performance_receipt
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_text
from packages.common.safe_fs import SafeFilesystemError, open_file_no_follow
from packages.common.safe_fs_publication import move_regular_file_no_follow_exclusive

RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
PARENT_MODE = 0o700
FILE_MODE = 0o600
OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)

COMMIT_ARTIFACT = "nhms-issue1895-performance-commit"
COMMIT_SCHEMA = "1.0"
MAX_RECEIPT_BYTES = 262144
REQUIRED_MARKER_KEYS = (
    "artifact",
    "schema_version",
    "receipt_name",
    "receipt_sha256",
    "st_dev",
    "st_ino",
    "st_uid",
    "st_mode",
    "st_nlink",
    "st_size",
    "head_sha",
    "reviewed_sha",
    "generated_at",
)


def _refuse(message: str, code: str) -> None:
    raise Issue1895ReadinessError(redact_text(message), code=code, stage="performance")


def _assert_regular_private(info: os.stat_result, *, code_prefix: str) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        _refuse("path is not a regular file", f"{code_prefix}_NOT_REGULAR")
    if info.st_uid != os.geteuid() or (info.st_mode & 0o777) != FILE_MODE or info.st_nlink != 1:
        _refuse("path identity/mode drifted", f"{code_prefix}_IDENTITY_DRIFT")


def _lstat_regular(path: Path, *, code_prefix: str) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError:
        _refuse("path is unavailable", f"{code_prefix}_MISSING")
        raise
    _assert_regular_private(info, code_prefix=code_prefix)
    return info


def _parent_facts(
    path: Path, *, code_prefix: str, require_private_parent: bool = True
) -> os.stat_result:
    parent = path.parent
    try:
        info = os.lstat(parent)
    except OSError:
        _refuse("parent is unavailable", f"{code_prefix}_PARENT_INVALID")
        raise
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        _refuse("parent must be a real directory", f"{code_prefix}_PARENT_INVALID")
    if require_private_parent:
        if info.st_uid != os.geteuid():
            _refuse("parent owner differs from the effective user", f"{code_prefix}_PARENT_OWNER")
        if (info.st_mode & 0o777) != PARENT_MODE:
            _refuse("parent must be mode 0700", f"{code_prefix}_PARENT_MODE")
    return info


def _open_held_regular(path: Path, *, code_prefix: str, require_private_parent: bool) -> int:
    """Open the named regular file without following the leaf or its parent.

    Current-run callers keep the historical pathname ``O_NOFOLLOW`` open after
    ``_parent_facts`` has already required a real 0700 parent. File-only
    callers additionally walk every directory component through the shipping
    no-follow opener so a parent symlink cannot authorize the file.
    """

    try:
        if require_private_parent:
            return os.open(path, OPEN_FLAGS)
        return open_file_no_follow(path)
    except (OSError, SafeFilesystemError):
        _refuse("path cannot be opened without following", f"{code_prefix}_OPEN")
        raise


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _close_fd(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _read_held_descriptor(
    path: Path,
    *,
    code_prefix: str,
    expected_parent: os.stat_result | None = None,
    max_bytes: int = MAX_RECEIPT_BYTES,
    require_private_parent: bool = True,
) -> tuple[bytes, os.stat_result, os.stat_result]:
    before_parent = (
        expected_parent
        if expected_parent is not None
        else _parent_facts(path, code_prefix=code_prefix, require_private_parent=require_private_parent)
    )
    before = _lstat_regular(path, code_prefix=code_prefix)
    fd: int | None = None
    try:
        try:
            fd = _open_held_regular(
                path, code_prefix=code_prefix, require_private_parent=require_private_parent
            )
        except Issue1895ReadinessError:
            raise
        except OSError:
            _refuse("path cannot be opened without following", f"{code_prefix}_OPEN")
            raise
        try:
            opened = os.fstat(fd)
        except OSError:
            _refuse("opened descriptor could not be stated", f"{code_prefix}_FSTAT")
            raise
        _assert_regular_private(opened, code_prefix=code_prefix)
        if not _same_inode(before, opened):
            _refuse("opened inode drifted from the path lstat", f"{code_prefix}_INODE_SWAP")
        if opened.st_size > max_bytes:
            _refuse("file exceeds the byte ceiling", f"{code_prefix}_TOO_LARGE")
        chunks: list[bytes] = []
        remaining = int(opened.st_size)
        while remaining > 0:
            try:
                chunk = os.read(fd, min(65536, remaining))
            except OSError:
                _refuse("file could not be read to completion", f"{code_prefix}_READ")
                raise
            if not chunk:
                _refuse("file read was short of the descriptor size", f"{code_prefix}_SHORT_READ")
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != opened.st_size:
            _refuse("file read was short of the descriptor size", f"{code_prefix}_SHORT_READ")
        try:
            extra = os.read(fd, 1)
        except OSError:
            _refuse("file trailing byte could not be proven absent", f"{code_prefix}_TRAILING")
            raise
        if extra:
            _refuse("file exceeds the descriptor size", f"{code_prefix}_TRAILING")
        try:
            after = os.fstat(fd)
        except OSError:
            _refuse("opened descriptor could not be restated", f"{code_prefix}_FSTAT")
            raise
        if (
            not _same_inode(opened, after)
            or after.st_size != opened.st_size
            or (after.st_mode & 0o777) != FILE_MODE
            or after.st_uid != os.geteuid()
            or after.st_nlink != 1
        ):
            _refuse("descriptor identity drifted while reading", f"{code_prefix}_IDENTITY_DRIFT")
        after_path = _lstat_regular(path, code_prefix=code_prefix)
        if not _same_inode(after, after_path) or after_path.st_size != after.st_size:
            _refuse("path inode drifted from the held descriptor", f"{code_prefix}_INODE_SWAP")
        after_parent = _parent_facts(
            path, code_prefix=code_prefix, require_private_parent=require_private_parent
        )
        if not _same_inode(before_parent, after_parent):
            _refuse("parent inode drifted while reading", f"{code_prefix}_PARENT_DRIFT")
        if require_private_parent and (
            (after_parent.st_mode & 0o777) != PARENT_MODE or after_parent.st_uid != os.geteuid()
        ):
            _refuse("parent mode/owner drifted while reading", f"{code_prefix}_PARENT_DRIFT")
        return data, after, after_parent
    finally:
        _close_fd(fd)


def _read_no_follow(path: Path, *, code_prefix: str) -> bytes:
    data, _info, _parent = _read_held_descriptor(path, code_prefix=code_prefix)
    return data


def _publish_regular_file(path: Path, encoded: bytes, *, code_prefix: str) -> None:
    if not path.is_absolute() or "\x00" in str(path):
        _refuse("path must be absolute", f"{code_prefix}_PATH_INVALID")
    parent = path.parent
    try:
        parent_info = os.lstat(parent)
    except OSError:
        _refuse("parent is unavailable", f"{code_prefix}_PARENT_INVALID")
        return
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        _refuse("parent must be a real directory", f"{code_prefix}_PARENT_INVALID")
    if parent_info.st_uid != os.geteuid():
        _refuse("parent owner differs from the effective user", f"{code_prefix}_PARENT_OWNER")
    if (parent_info.st_mode & 0o777) != 0o700:
        _refuse("parent must be mode 0700", f"{code_prefix}_PARENT_MODE")
    if os.path.lexists(path):
        _refuse("target already exists", f"{code_prefix}_EXISTS")
    if len(encoded) > MAX_RECEIPT_BYTES:
        _refuse("payload exceeds the byte ceiling", f"{code_prefix}_TOO_LARGE")
    temp = path.with_name(f".{path.name}.tmp")
    if os.path.lexists(temp):
        _refuse("temporary sibling already exists", f"{code_prefix}_TEMP_EXISTS")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(temp, flags, 0o600)
    except OSError:
        _refuse("temporary sibling cannot be created safely", f"{code_prefix}_TEMP_CREATE")
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise Issue1895ReadinessError(
                "temporary sibling is not a private regular file",
                code=f"{code_prefix}_TEMP_INVALID",
                stage="performance",
            )
        os.fchmod(fd, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        temp_facts = descriptor_facts(os.fstat(fd))
    except Exception:
        os.close(fd)
        try:
            os.unlink(temp)
        except OSError:
            pass
        _refuse("payload could not be written durably", f"{code_prefix}_WRITE_FAILED")
        return
    else:
        os.close(fd)
    try:
        move_regular_file_no_follow_exclusive(temp.parent, temp.name, path.parent, path.name)
    except FileExistsError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        _refuse("target already exists", f"{code_prefix}_EXISTS")
        return
    except (SafeFilesystemError, OSError):
        try:
            os.unlink(temp)
        except OSError:
            pass
        _refuse("payload could not be published exclusively", f"{code_prefix}_PUBLISH_FAILED")
        return
    linked: os.stat_result | None = None
    try:
        linked = os.lstat(path)
        if (
            int(linked.st_dev) != temp_facts["st_dev"]
            or int(linked.st_ino) != temp_facts["st_ino"]
        ):
            _refuse("published inode is not this invocation's temporary", f"{code_prefix}_INODE_SWAP")
        parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        _lstat_regular(path, code_prefix=code_prefix)
        readback, after, after_parent = _read_held_descriptor(
            path, code_prefix=code_prefix, expected_parent=parent_info
        )
        if readback != encoded:
            _refuse("published bytes drifted", f"{code_prefix}_READBACK")
        if not _same_inode(linked, after) or not _same_inode(parent_info, after_parent):
            _refuse("published inode drifted after link", f"{code_prefix}_IDENTITY_DRIFT")
        if os.path.lexists(temp):
            _refuse("temporary sibling was not removed", f"{code_prefix}_TEMP_RESIDUE")
    except Issue1895ReadinessError:
        if linked is not None:
            try:
                current = os.lstat(path)
            except OSError:
                current = None
            if current is not None and _same_inode(current, linked):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        raise
    except Exception:
        if linked is not None:
            try:
                current = os.lstat(path)
            except OSError:
                current = None
            if current is not None and _same_inode(current, linked):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        _refuse("payload could not be published exclusively", f"{code_prefix}_PUBLISH_FAILED")
        return


def descriptor_facts(info: os.stat_result) -> dict[str, int]:
    return {
        "st_dev": int(info.st_dev),
        "st_ino": int(info.st_ino),
        "st_uid": int(info.st_uid),
        "st_mode": int(info.st_mode & 0o777),
        "st_nlink": int(info.st_nlink),
        "st_size": int(info.st_size),
    }


def marker_payload(
    *,
    receipt_name: str,
    receipt_bytes: bytes,
    facts: Mapping[str, int],
    head_sha: str,
    reviewed_sha: str,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "artifact": COMMIT_ARTIFACT,
        "schema_version": COMMIT_SCHEMA,
        "receipt_name": receipt_name,
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        **facts,
        "head_sha": head_sha,
        "reviewed_sha": reviewed_sha,
        "generated_at": generated_at,
    }


def publish_performance_receipt(path: Path, document: Mapping[str, Any]) -> None:
    encoded = json.dumps(dict(document), sort_keys=True, separators=(",", ":")).encode("utf-8")
    _publish_regular_file(path, encoded, code_prefix="RECEIPT")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def publish_performance_artifacts(path: Path, document: Mapping[str, Any]) -> None:
    """Publish receipt, descriptor-readback, then exclusive digest-bound marker."""

    marker = path.with_name(commit_marker_name(path.name))
    if os.path.lexists(marker):
        _refuse("commit marker already exists", "COMMIT_EXISTS")
    if os.path.lexists(path):
        _refuse("receipt already exists", "RECEIPT_EXISTS")
    validated = validate_performance_receipt(document)
    identity = validated["identity"]
    encoded = json.dumps(dict(validated), sort_keys=True, separators=(",", ":")).encode("utf-8")
    publish_performance_receipt(path, validated)
    facts = descriptor_facts(_lstat_regular(path, code_prefix="RECEIPT"))
    readback = _read_no_follow(path, code_prefix="RECEIPT")
    if readback != encoded:
        _refuse("published receipt bytes drifted", "RECEIPT_READBACK")
    payload = marker_payload(
        receipt_name=path.name,
        receipt_bytes=readback,
        facts=facts,
        head_sha=str(identity["head_sha"]),
        reviewed_sha=str(identity["reviewed_sha"]),
        generated_at=_iso_now(),
    )
    marker_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        _publish_regular_file(marker, marker_bytes, code_prefix="COMMIT")
    except Issue1895ReadinessError:
        # Leave the newly written receipt as an uncommitted orphan. The binder
        # rejects a receipt without a matching marker; do not delete an
        # unproven winner that may already have been accepted elsewhere.
        raise
    bind_performance_artifacts(
        path,
        marker,
        expected_sha=str(identity["head_sha"]),
        expected_basin=str(identity["basin_id"]),
        expected_segment=str(identity["segment_id"]),
    )


def parse_generated_at(value: object) -> datetime:
    text = str(value or "").strip()
    if RFC3339_UTC_RE.fullmatch(text) is None:
        _refuse("generated_at is not strict UTC RFC3339", "COMMIT_GENERATED_AT")
    return parse_bracket_instant(text)


def bind_performance_artifacts(
    receipt_path: Path,
    marker_path: Path,
    *,
    expected_sha: str,
    expected_basin: str,
    expected_segment: str,
    cmd_start: str | None = None,
    cmd_end: str | None = None,
) -> dict[str, Any]:
    """PASS-only binder: held descriptors, parent pin, digest/facts, optional bracket."""

    if marker_path.name != commit_marker_name(receipt_path.name):
        _refuse("commit marker name does not bind the receipt basename", "COMMIT_NAME")
    if os.path.islink(receipt_path) or os.path.islink(marker_path):
        _refuse("artifact is a symlink", "COMMIT_SYMLINK")
    parent = _parent_facts(receipt_path, code_prefix="RECEIPT")
    marker_parent = _parent_facts(marker_path, code_prefix="COMMIT")
    if not _same_inode(parent, marker_parent):
        _refuse("receipt and marker are not in the same parent directory", "COMMIT_PARENT")
    receipt_bytes, receipt_info, after_parent = _read_held_descriptor(
        receipt_path, code_prefix="RECEIPT", expected_parent=parent
    )
    marker_bytes, marker_info, marker_after_parent = _read_held_descriptor(
        marker_path, code_prefix="COMMIT", expected_parent=parent
    )
    if not _same_inode(after_parent, marker_after_parent):
        _refuse("receipt and marker parent drifted while reading", "COMMIT_PARENT")
    if receipt_info.st_dev != marker_info.st_dev:
        _refuse("receipt and marker are not on the same parent device", "COMMIT_PARENT")
    try:
        document = json.loads(receipt_bytes.decode("utf-8"))
        marker = json.loads(marker_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        _refuse("artifact JSON is not parseable", "COMMIT_JSON")
        raise
    validated = validate_performance_receipt(document)
    if validated.get("status") != "PASS":
        _refuse("binder accepts PASS receipts only", "COMMIT_NOT_PASS")
    if not isinstance(marker, Mapping) or set(marker) != set(REQUIRED_MARKER_KEYS):
        _refuse("commit marker keys are not closed", "COMMIT_KEYS")
    if marker.get("artifact") != COMMIT_ARTIFACT or marker.get("schema_version") != COMMIT_SCHEMA:
        _refuse("commit marker schema drifted", "COMMIT_SCHEMA")
    if marker.get("receipt_name") != receipt_path.name:
        _refuse("commit marker receipt_name drifted", "COMMIT_RECEIPT_NAME")
    digest = hashlib.sha256(receipt_bytes).hexdigest()
    if marker.get("receipt_sha256") != digest:
        _refuse("commit marker digest does not match receipt bytes", "COMMIT_DIGEST")
    facts = descriptor_facts(receipt_info)
    for key, value in facts.items():
        if marker.get(key) != value:
            _refuse("commit marker descriptor facts drifted", "COMMIT_FACTS")
    if marker.get("head_sha") != expected_sha or marker.get("reviewed_sha") != expected_sha:
        _refuse("commit marker SHA is not REVIEWED_SHA", "COMMIT_SHA")
    identity = validated["identity"]
    if identity.get("head_sha") != expected_sha or identity.get("reviewed_sha") != expected_sha:
        _refuse("receipt SHA is not REVIEWED_SHA", "COMMIT_RECEIPT_SHA")
    if identity.get("basin_id") != expected_basin or identity.get("segment_id") != expected_segment:
        _refuse("receipt basin/segment pin drifted", "COMMIT_PIN")
    generated = parse_generated_at(marker.get("generated_at"))
    if cmd_start is not None or cmd_end is not None:
        if cmd_start is None or cmd_end is None:
            _refuse("command bracket is incomplete", "COMMIT_BRACKET")
        start = parse_bracket_instant(cmd_start)
        end = parse_bracket_instant(cmd_end)
        assert_report_within_command_bracket(report_mtime=receipt_info.st_mtime, start=start, end=end)
        assert_report_within_command_bracket(report_mtime=marker_info.st_mtime, start=start, end=end)
        assert_report_within_command_bracket(report_mtime=generated.timestamp(), start=start, end=end)
    return {"receipt": validated, "marker": dict(marker)}
