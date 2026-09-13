"""Owner-local private-file IO for Bringup-C4 production acceptance.

This module is independent of ``node27_issue1895_*``. It reuses shared
``safe_fs`` / ``safe_fs_publication`` / ``evidence_io`` primitives and keeps a
stricter identity signature than ``evidence_io.FileIdentity``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.evidence_io import (
    BoundedEvidenceError,
    assert_paths_disjoint,
    normalized_absolute_path,
    reject_secret_material,
    validate_json_complexity,
)
from packages.common.safe_fs import (
    SafeFilesystemError,
    open_directory_no_follow,
    open_file_no_follow,
)
from packages.common.safe_fs_publication import (
    move_regular_file_no_follow_exclusive,
    write_bytes_no_follow_exclusive,
)

PARENT_MODE = 0o700
FILE_MODE = 0o600
MAX_RECORD_BYTES = 262_144
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 256
MAX_ARRAY_ITEMS = 32
MAX_OBJECT_ITEMS = 64
SHA_RE = r"^[0-9a-f]{40}$"
FILE_DIGEST_RE = r"^[0-9a-f]{64}$"
RFC3339_UTC_RE = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
STRICT_DECIMAL_RE = r"^\d{1,10}$"
ORIGIN_RE = r"^https?://[^/?#]+(?:/)?$"
ID_RE = r"^[A-Za-z0-9._:-]{1,96}$"

_SHA_COMPILED = re.compile(SHA_RE)
_FILE_DIGEST_COMPILED = re.compile(FILE_DIGEST_RE)
_RFC3339_COMPILED = re.compile(RFC3339_UTC_RE)
_DECIMAL_COMPILED = re.compile(STRICT_DECIMAL_RE)
_ORIGIN_COMPILED = re.compile(ORIGIN_RE)
_ID_COMPILED = re.compile(ID_RE)


class C4AcceptanceError(Exception):
    """Stable owner-coded refusal. Never interpolates raw paths, URLs, or OS text."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code
        self.stage = "c4-acceptance"


def refuse(message: str, code: str) -> None:
    raise C4AcceptanceError(message, code=code)


@dataclass(frozen=True)
class PrivateFileIdentity:
    """Full POSIX signature plus SHA-256 of the same held descriptor."""

    st_dev: int
    st_ino: int
    st_uid: int
    st_mode: int
    st_nlink: int
    st_size: int
    st_mtime_ns: int
    st_ctime_ns: int
    sha256: str

    def as_mapping(self) -> dict[str, int | str]:
        return {
            "st_dev": self.st_dev,
            "st_ino": self.st_ino,
            "st_uid": self.st_uid,
            "st_mode": self.st_mode,
            "st_nlink": self.st_nlink,
            "st_size": self.st_size,
            "st_mtime_ns": self.st_mtime_ns,
            "st_ctime_ns": self.st_ctime_ns,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ParentIdentity:
    st_dev: int
    st_ino: int
    st_uid: int
    st_mode: int

    def as_mapping(self) -> dict[str, int]:
        return {
            "st_dev": self.st_dev,
            "st_ino": self.st_ino,
            "st_uid": self.st_uid,
            "st_mode": self.st_mode,
        }


@dataclass(frozen=True)
class HeldPrivateFile:
    """Pinned regular file plus its parent directory, both no-follow."""

    path: Path
    file_fd: int
    parent_fd: int
    identity: PrivateFileIdentity
    parent: ParentIdentity
    raw: bytes


def require_absolute_path(value: object, *, code: str) -> Path:
    if isinstance(value, str):
        text = value
    elif isinstance(value, os.PathLike):
        text = os.fspath(value)
    else:
        text = ""
    if not isinstance(text, str) or not text.startswith("/") or "\x00" in text:
        refuse("path must be an absolute lexical path", code)
    path = Path(text)
    parts = path.parts
    if path.anchor != "/" or any(part in {"", ".", ".."} for part in parts[1:]):
        refuse("path must be lexically normalized", code)
    if path != normalized_absolute_path(path):
        refuse("path must be lexically normalized", code)
    return path


def require_sha(value: object) -> str:
    if not isinstance(value, str) or _SHA_COMPILED.fullmatch(value) is None:
        refuse("reviewed SHA must be a lowercase 40-hex digest", "C4_SHA_INVALID")
    return value


def require_origin(value: object) -> str:
    if not isinstance(value, str) or _ORIGIN_COMPILED.fullmatch(value) is None:
        refuse("origin must be a bare http or https origin", "C4_ORIGIN_INVALID")
    if "@" in value or value.count("://") != 1:
        refuse("origin must be a bare http or https origin", "C4_ORIGIN_INVALID")
    scheme, rest = value.split("://", 1)
    host = rest[:-1] if rest.endswith("/") else rest
    if not host or "/" in host or "?" in host or "#" in host or host.endswith(":"):
        refuse("origin must be a bare http or https origin", "C4_ORIGIN_INVALID")
    if scheme not in {"http", "https"}:
        refuse("origin must be a bare http or https origin", "C4_ORIGIN_INVALID")
    return value[:-1] if value.endswith("/") else value


def require_id(value: object, *, code: str) -> str:
    if not isinstance(value, str) or _ID_COMPILED.fullmatch(value) is None:
        refuse("identifier is empty or not a bounded token", code)
    return value


def require_bracket_second(value: object, *, code: str) -> int:
    if not isinstance(value, str) or _DECIMAL_COMPILED.fullmatch(value) is None:
        refuse("command bracket second is not a bounded canonical decimal", code)
    parsed = int(value)
    if parsed < 0:
        refuse("command bracket second is not a bounded canonical decimal", code)
    return parsed


def require_json_int(value: object, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        refuse("command bracket second is not a bounded canonical decimal", code)
    if value > 9_999_999_999:
        refuse("command bracket second is not a bounded canonical decimal", code)
    return value


def require_ordered_bracket(cmd_start: object, cmd_end: object) -> tuple[int, int]:
    start = require_bracket_second(cmd_start, code="C4_BRACKET_INVALID")
    end = require_bracket_second(cmd_end, code="C4_BRACKET_INVALID")
    if start > end:
        refuse("command bracket is not a finite ordered integer pair", "C4_BRACKET_INVALID")
    return start, end


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_utc(instant: datetime) -> str:
    if instant.tzinfo is None:
        refuse("timestamp is not UTC", "C4_TIMESTAMP_INVALID")
    aware = instant.astimezone(UTC)
    text = aware.isoformat().replace("+00:00", "Z")
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    return text


def parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or _RFC3339_COMPILED.fullmatch(value) is None:
        refuse("timestamp is not strict UTC RFC3339", "C4_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        refuse("timestamp is not calendar-valid UTC RFC3339", "C4_TIMESTAMP_INVALID")
    if parsed.tzinfo is None:
        refuse("timestamp is not calendar-valid UTC RFC3339", "C4_TIMESTAMP_INVALID")
    replayed = parsed.astimezone(UTC)
    if (
        replayed.year != parsed.year
        or replayed.month != parsed.month
        or replayed.day != parsed.day
        or replayed.hour != parsed.hour
        or replayed.minute != parsed.minute
        or replayed.second != parsed.second
    ):
        refuse("timestamp is not calendar-valid UTC RFC3339", "C4_TIMESTAMP_INVALID")
    return replayed


def cmd_start_instant(cmd_start: int) -> datetime:
    try:
        return datetime.fromtimestamp(cmd_start, UTC)
    except (OverflowError, OSError, ValueError):
        refuse("command bracket second is not a bounded canonical decimal", "C4_BRACKET_INVALID")
    raise AssertionError("unreachable")


def freeze_is_not_later_than_cmd_start(freeze_at: datetime, cmd_start: int) -> bool:
    """Compare the freeze instant to the cmd-start second without flooring freeze."""

    return freeze_at <= cmd_start_instant(cmd_start)


def _close_fd(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _identity_from_stat(info: os.stat_result, digest: str) -> PrivateFileIdentity:
    return PrivateFileIdentity(
        st_dev=int(info.st_dev),
        st_ino=int(info.st_ino),
        st_uid=int(info.st_uid),
        st_mode=int(info.st_mode & 0o777),
        st_nlink=int(info.st_nlink),
        st_size=int(info.st_size),
        st_mtime_ns=int(info.st_mtime_ns),
        st_ctime_ns=int(info.st_ctime_ns),
        sha256=digest,
    )


def _parent_from_stat(info: os.stat_result) -> ParentIdentity:
    return ParentIdentity(
        st_dev=int(info.st_dev),
        st_ino=int(info.st_ino),
        st_uid=int(info.st_uid),
        st_mode=int(info.st_mode & 0o777),
    )


def _assert_private_parent(info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        refuse("parent must be a real directory", "C4_PARENT_INVALID")
    if info.st_uid != os.geteuid():
        refuse("parent owner differs from the effective user", "C4_PARENT_OWNER")
    if (info.st_mode & 0o777) != PARENT_MODE:
        refuse("parent must be mode 0700", "C4_PARENT_MODE")


def _assert_private_regular(info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        refuse("path is not a regular file", "C4_NOT_REGULAR")
    if info.st_uid != os.geteuid() or (info.st_mode & 0o777) != FILE_MODE or info.st_nlink != 1:
        refuse("file is not an euid-owned mode-0600 single-link regular file", "C4_FILE_IDENTITY")


def _read_fd_exact(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        try:
            chunk = os.read(fd, min(65536, remaining))
        except OSError:
            refuse("file could not be read to completion", "C4_READ")
        if not chunk:
            refuse("file read was short of the descriptor size", "C4_SHORT_READ")
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) != size:
        refuse("file read was short of the descriptor size", "C4_SHORT_READ")
    try:
        extra = os.read(fd, 1)
    except OSError:
        refuse("file trailing byte could not be proven absent", "C4_TRAILING")
    if extra:
        refuse("file exceeds the descriptor size", "C4_TRAILING")
    return data


def _named_inode(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        refuse("named path could not be restated through the pinned parent", "C4_PATH_RACE")
    raise AssertionError("unreachable")


def capture_held_private_file(path: Path, *, max_bytes: int = MAX_RECORD_BYTES) -> HeldPrivateFile:
    """Open parent and file, hash from the held fd, and keep both fds open."""

    path = require_absolute_path(str(path), code="C4_PATH_INVALID")
    parent = path.parent
    parent_fd: int | None = None
    file_fd: int | None = None
    try:
        try:
            parent_fd = open_directory_no_follow(parent)
        except (OSError, SafeFilesystemError):
            refuse("parent cannot be opened without following", "C4_PARENT_OPEN")
        try:
            parent_info = os.fstat(parent_fd)
        except OSError:
            refuse("parent descriptor could not be stated", "C4_PARENT_FSTAT")
        _assert_private_parent(parent_info)
        parent_identity = _parent_from_stat(parent_info)
        try:
            file_fd = open_file_no_follow(path)
        except FileNotFoundError:
            refuse("path is unavailable", "C4_MISSING")
        except (OSError, SafeFilesystemError):
            refuse("path cannot be opened without following", "C4_OPEN")
        try:
            opened = os.fstat(file_fd)
        except OSError:
            refuse("opened descriptor could not be stated", "C4_FSTAT")
        _assert_private_regular(opened)
        if opened.st_size > max_bytes or opened.st_size < 1:
            refuse("file size is outside the bounded ceiling", "C4_TOO_LARGE")
        raw = _read_fd_exact(file_fd, int(opened.st_size))
        digest = hashlib.sha256(raw).hexdigest()
        try:
            after = os.fstat(file_fd)
        except OSError:
            refuse("opened descriptor could not be restated", "C4_FSTAT")
        identity = _identity_from_stat(opened, digest)
        if identity != _identity_from_stat(after, digest):
            refuse("descriptor identity drifted while reading", "C4_IDENTITY_DRIFT")
        named = _named_inode(parent_fd, path.name)
        if named is None:
            refuse("named path could not be restated through the pinned parent", "C4_PATH_RACE")
        if _identity_from_stat(named, digest) != identity:
            refuse("path inode drifted from the held descriptor", "C4_PATH_RACE")
        try:
            after_parent = os.fstat(parent_fd)
        except OSError:
            refuse("parent descriptor could not be restated", "C4_PARENT_FSTAT")
        if parent_identity != _parent_from_stat(after_parent):
            refuse("parent inode drifted while reading", "C4_PARENT_DRIFT")
        held = HeldPrivateFile(
            path=path,
            file_fd=file_fd,
            parent_fd=parent_fd,
            identity=identity,
            parent=parent_identity,
            raw=raw,
        )
        file_fd = None
        parent_fd = None
        return held
    finally:
        _close_fd(file_fd)
        _close_fd(parent_fd)


def recapture_held_private_file(held: HeldPrivateFile, *, max_bytes: int = MAX_RECORD_BYTES) -> PrivateFileIdentity:
    """Re-hash and re-stat the same held fd, then recheck the named path and parent."""

    try:
        os.lseek(held.file_fd, 0, os.SEEK_SET)
    except OSError:
        refuse("held descriptor could not be rewound", "C4_SEEK")
    try:
        opened = os.fstat(held.file_fd)
    except OSError:
        refuse("opened descriptor could not be stated", "C4_FSTAT")
    _assert_private_regular(opened)
    if opened.st_size > max_bytes or opened.st_size < 1:
        refuse("file size is outside the bounded ceiling", "C4_TOO_LARGE")
    raw = _read_fd_exact(held.file_fd, int(opened.st_size))
    digest = hashlib.sha256(raw).hexdigest()
    try:
        after = os.fstat(held.file_fd)
    except OSError:
        refuse("opened descriptor could not be restated", "C4_FSTAT")
    identity = _identity_from_stat(opened, digest)
    if identity != _identity_from_stat(after, digest):
        refuse("descriptor identity drifted while reading", "C4_IDENTITY_DRIFT")
    named = _named_inode(held.parent_fd, held.path.name)
    if named is None:
        refuse("named path could not be restated through the pinned parent", "C4_PATH_RACE")
    if _identity_from_stat(named, digest) != identity:
        refuse("path inode drifted from the held descriptor", "C4_PATH_RACE")
    try:
        after_parent = os.fstat(held.parent_fd)
    except OSError:
        refuse("parent descriptor could not be restated", "C4_PARENT_FSTAT")
    if held.parent != _parent_from_stat(after_parent):
        refuse("parent inode drifted while reading", "C4_PARENT_DRIFT")
    return identity


def close_held_private_file(held: HeldPrivateFile | None) -> None:
    if held is None:
        return
    _close_fd(held.file_fd)
    _close_fd(held.parent_fd)


def identity_from_mapping(value: object) -> PrivateFileIdentity:
    expected = {
        "st_dev",
        "st_ino",
        "st_uid",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        refuse("file identity fields are not closed", "C4_IDENTITY_SCHEMA")
    digest = value.get("sha256")
    if not isinstance(digest, str) or _FILE_DIGEST_COMPILED.fullmatch(digest) is None:
        refuse("file identity digest is invalid", "C4_IDENTITY_SCHEMA")
    ints: dict[str, int] = {}
    for key in (
        "st_dev",
        "st_ino",
        "st_uid",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    ):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            refuse("file identity fields are not closed", "C4_IDENTITY_SCHEMA")
        ints[key] = item
    if ints["st_uid"] != os.geteuid() or ints["st_mode"] != FILE_MODE or ints["st_nlink"] != 1:
        refuse("file identity does not describe a private regular file", "C4_FILE_IDENTITY")
    return PrivateFileIdentity(sha256=digest, **ints)


def parent_from_mapping(value: object) -> ParentIdentity:
    if not isinstance(value, Mapping) or set(value) != {"st_dev", "st_ino", "st_uid", "st_mode"}:
        refuse("parent identity fields are not closed", "C4_PARENT_SCHEMA")
    ints: dict[str, int] = {}
    for key in ("st_dev", "st_ino", "st_uid", "st_mode"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            refuse("parent identity fields are not closed", "C4_PARENT_SCHEMA")
        ints[key] = item
    if ints["st_uid"] != os.geteuid() or ints["st_mode"] != PARENT_MODE:
        refuse("parent identity does not describe a private directory", "C4_PARENT_IDENTITY")
    return ParentIdentity(**ints)


class _DuplicateKey(ValueError):
    """JSON object repeated a key."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKey("duplicate JSON key")
        seen.add(key)
        out[key] = value
    return out


def _reject_parse_constant(_item: str) -> None:
    raise ValueError("nonfinite")


def _reject_nonfinite(value: Any) -> None:
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, float) and (math.isnan(current) or math.isinf(current)):
            refuse("JSON numbers must be finite", "C4_JSON_INVALID")


def parse_closed_json(raw: bytes, *, label: str) -> dict[str, Any]:
    del label
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_parse_constant,
        )
    except _DuplicateKey:
        refuse("JSON object contains a duplicate key", "C4_JSON_DUPLICATE")
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError, TypeError):
        refuse("input is not valid UTF-8 JSON", "C4_JSON_INVALID")
    if not isinstance(value, dict):
        refuse("JSON root is not an object", "C4_JSON_INVALID")
    try:
        validate_json_complexity(
            value,
            label="c4-acceptance",
            max_depth=MAX_JSON_DEPTH,
            max_nodes=MAX_JSON_NODES,
            max_array_items=MAX_ARRAY_ITEMS,
            max_object_items=MAX_OBJECT_ITEMS,
        )
        reject_secret_material(value, label="c4-acceptance")
    except BoundedEvidenceError:
        refuse("JSON is oversized, too complex, or contains secret material", "C4_JSON_COMPLEX")
    _reject_nonfinite(value)
    return value


def encode_closed_json(document: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            dict(document),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        refuse("record could not be encoded as closed JSON", "C4_ENCODE")
    if len(encoded) > MAX_RECORD_BYTES:
        refuse("payload exceeds the byte ceiling", "C4_TOO_LARGE")
    return encoded


def assert_outputs_absent_and_disjoint(outputs: Sequence[Path], inputs: Sequence[Path]) -> None:
    for output in outputs:
        require_absolute_path(str(output), code="C4_PATH_INVALID")
        others = [item for item in outputs if item != output]
        try:
            assert_paths_disjoint(output, list(inputs) + others, label="c4-acceptance")
        except BoundedEvidenceError:
            refuse("output aliases an input or another output", "C4_ALIAS")
        if os.path.lexists(output):
            refuse("target already exists", "C4_EXISTS")


def assert_receipt_parent_ready(receipt: Path) -> None:
    """Receipt must be absent; its private parent must already exist."""

    receipt = require_absolute_path(str(receipt), code="C4_PATH_INVALID")
    parent_fd: int | None = None
    try:
        try:
            parent_fd = open_directory_no_follow(receipt.parent)
        except (OSError, SafeFilesystemError):
            refuse("parent cannot be opened without following", "C4_PARENT_OPEN")
        try:
            parent_info = os.fstat(parent_fd)
        except OSError:
            refuse("parent descriptor could not be stated", "C4_PARENT_FSTAT")
        _assert_private_parent(parent_info)
        if _named_inode(parent_fd, receipt.name) is not None:
            refuse("target already exists", "C4_EXISTS")
    finally:
        _close_fd(parent_fd)


def _temp_sibling(path: Path) -> Path:
    return path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")


def _unlink_created_temp(temp: Path, created_dev: int, created_ino: int) -> None:
    """Unlink only the inode this invocation created. Never touch a winner."""

    parent_fd: int | None = None
    file_fd: int | None = None
    try:
        try:
            parent_fd = open_directory_no_follow(temp.parent)
            file_fd = open_file_no_follow(temp)
        except (OSError, SafeFilesystemError, FileNotFoundError):
            return
        try:
            opened = os.fstat(file_fd)
        except OSError:
            return
        if opened.st_dev != created_dev or opened.st_ino != created_ino:
            return
        named = _named_inode(parent_fd, temp.name)
        if named is None or named.st_dev != created_dev or named.st_ino != created_ino:
            return
        try:
            os.unlink(temp.name, dir_fd=parent_fd)
        except OSError:
            return
    finally:
        _close_fd(file_fd)
        _close_fd(parent_fd)


def publish_private_json(path: Path, document: Mapping[str, Any], *, inputs: Sequence[Path]) -> PrivateFileIdentity:
    """Exclusive unique temp sibling, durable create, exclusive move, inode proof."""

    path = require_absolute_path(str(path), code="C4_PATH_INVALID")
    encoded = encode_closed_json(document)
    assert_outputs_absent_and_disjoint([path], inputs)
    parent = path.parent
    parent_fd: int | None = None
    temp: Path | None = None
    created_dev: int | None = None
    created_ino: int | None = None
    try:
        try:
            parent_fd = open_directory_no_follow(parent)
        except (OSError, SafeFilesystemError):
            refuse("parent cannot be opened without following", "C4_PARENT_OPEN")
        try:
            parent_info = os.fstat(parent_fd)
        except OSError:
            refuse("parent descriptor could not be stated", "C4_PARENT_FSTAT")
        _assert_private_parent(parent_info)
        parent_identity = _parent_from_stat(parent_info)
        if _named_inode(parent_fd, path.name) is not None:
            refuse("target already exists", "C4_EXISTS")
        temp = _temp_sibling(path)
        try:
            write_bytes_no_follow_exclusive(
                temp,
                encoded,
                require_durable_create=True,
                mode=FILE_MODE,
            )
        except FileExistsError:
            refuse("temporary sibling already exists", "C4_TEMP_EXISTS")
        except SafeFilesystemError as error:
            if error.kind == "indeterminate":
                refuse("temporary sibling create is indeterminate", "C4_TEMP_INDETERMINATE")
            refuse("temporary sibling cannot be created safely", "C4_TEMP_CREATE")
        except OSError:
            refuse("temporary sibling cannot be created safely", "C4_TEMP_CREATE")
        held = capture_held_private_file(temp)
        try:
            if held.raw != encoded:
                refuse("temporary sibling bytes drifted", "C4_TEMP_READBACK")
            if held.parent != parent_identity:
                refuse("parent inode drifted while creating temporary", "C4_PARENT_DRIFT")
            try:
                pinned_parent = os.fstat(parent_fd)
            except OSError:
                refuse("parent descriptor could not be restated", "C4_PARENT_FSTAT")
            if _parent_from_stat(pinned_parent) != parent_identity:
                refuse("parent inode drifted while creating temporary", "C4_PARENT_DRIFT")
            named_temp = _named_inode(parent_fd, temp.name)
            if (
                named_temp is None
                or named_temp.st_dev != held.identity.st_dev
                or named_temp.st_ino != held.identity.st_ino
            ):
                refuse("temporary sibling is not in the pinned parent", "C4_PARENT_DRIFT")
            temp_identity = held.identity
            created_dev = temp_identity.st_dev
            created_ino = temp_identity.st_ino
        finally:
            close_held_private_file(held)
        try:
            move_regular_file_no_follow_exclusive(temp.parent, temp.name, path.parent, path.name)
        except FileExistsError:
            _unlink_created_temp(temp, created_dev, created_ino)
            refuse("target already exists", "C4_EXISTS")
        except SafeFilesystemError as error:
            if error.kind == "indeterminate":
                refuse("exclusive publication is indeterminate", "C4_PUBLISH_INDETERMINATE")
            _unlink_created_temp(temp, created_dev, created_ino)
            refuse("payload could not be published exclusively", "C4_PUBLISH_FAILED")
        except OSError:
            _unlink_created_temp(temp, created_dev, created_ino)
            refuse("payload could not be published exclusively", "C4_PUBLISH_FAILED")
        published = capture_held_private_file(path)
        try:
            if published.identity.st_dev != temp_identity.st_dev or published.identity.st_ino != temp_identity.st_ino:
                refuse("published inode is not this invocation's temporary", "C4_INODE_SWAP")
            if published.raw != encoded:
                refuse("published bytes drifted", "C4_READBACK")
            try:
                after_parent = os.fstat(parent_fd)
            except OSError:
                refuse("parent descriptor could not be restated", "C4_PARENT_FSTAT")
            if published.parent != parent_identity or _parent_from_stat(after_parent) != parent_identity:
                refuse("parent inode drifted after publication", "C4_PARENT_DRIFT")
            if _named_inode(parent_fd, temp.name) is not None:
                refuse("temporary sibling was not removed", "C4_TEMP_RESIDUE")
            return published.identity
        finally:
            close_held_private_file(published)
    except C4AcceptanceError:
        dest_present = False
        if parent_fd is not None:
            dest_present = _named_inode(parent_fd, path.name) is not None
        if temp is not None and created_dev is not None and created_ino is not None and not dest_present:
            _unlink_created_temp(temp, created_dev, created_ino)
        raise
    finally:
        _close_fd(parent_fd)


def read_private_json(
    path: Path, *, max_bytes: int = MAX_RECORD_BYTES
) -> tuple[bytes, dict[str, Any], PrivateFileIdentity, ParentIdentity]:
    held = capture_held_private_file(path, max_bytes=max_bytes)
    try:
        document = parse_closed_json(held.raw, label="c4-acceptance")
        return held.raw, document, held.identity, held.parent
    finally:
        close_held_private_file(held)


__all__ = (
    "C4AcceptanceError",
    "FILE_MODE",
    "HeldPrivateFile",
    "MAX_RECORD_BYTES",
    "PARENT_MODE",
    "ParentIdentity",
    "PrivateFileIdentity",
    "assert_outputs_absent_and_disjoint",
    "assert_receipt_parent_ready",
    "capture_held_private_file",
    "close_held_private_file",
    "cmd_start_instant",
    "encode_closed_json",
    "format_utc",
    "freeze_is_not_later_than_cmd_start",
    "identity_from_mapping",
    "parent_from_mapping",
    "parse_closed_json",
    "parse_utc",
    "publish_private_json",
    "read_private_json",
    "recapture_held_private_file",
    "refuse",
    "require_absolute_path",
    "require_id",
    "require_json_int",
    "require_ordered_bracket",
    "require_origin",
    "require_sha",
    "utc_now",
)
