"""Shared private current-run receipt publication and descriptor-held input readers.

The #1895 readiness owners publish one unique PASS receipt per invocation.  This
module deliberately delegates output publication and held-descriptor reads to the
already shipping ``node27_issue1895_commit`` primitive rather than recreating a
weaker atomic-write path.
"""

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

from packages.common.evidence_io import (
    BoundedEvidenceError,
    read_bounded_json_with_identity_no_follow,
    validate_json_complexity,
)
from packages.common.node27_issue1895_commit import (
    FILE_MODE,
    MAX_RECEIPT_BYTES,
    PARENT_MODE,
    _parent_facts,
    _publish_regular_file,
    _read_held_descriptor,
    descriptor_facts,
)
from packages.common.node27_issue1895_probe import assert_report_within_command_bracket, parse_bracket_instant
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from packages.common.redaction import redact_text
from packages.common.safe_fs import SafeFilesystemError, stat_no_follow

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
MAX_INPUT_BYTES = 1024 * 1024
PRIVATE_RECEIPT_MAX_DEPTH = 64
PRIVATE_RECEIPT_MAX_NODES = 100_000
PRIVATE_RECEIPT_MAX_ARRAY_ITEMS = 10_000
PRIVATE_RECEIPT_MAX_OBJECT_ITEMS = 10_000


def refuse(message: str, code: str, *, stage: str = "readiness") -> None:
    raise Issue1895ReadinessError(redact_text(message), code=code, stage=stage)


def require_sha(value: object, *, label: str, stage: str = "readiness") -> str:
    text = str(value or "").strip()
    if SHA_RE.fullmatch(text) is None:
        refuse(f"{label} is not a lowercase 40-hex SHA", "READINESS_SHA_INVALID", stage=stage)
    return text


def require_matching_shas(head_sha: object, reviewed_sha: object, *, stage: str = "readiness") -> str:
    head = require_sha(head_sha, label="head_sha", stage=stage)
    reviewed = require_sha(reviewed_sha, label="reviewed_sha", stage=stage)
    if head != reviewed:
        refuse("head_sha is not the reviewed SHA", "READINESS_SHA_MISMATCH", stage=stage)
    return head


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def require_utc(value: object, *, label: str, stage: str = "readiness") -> datetime:
    text = str(value or "").strip()
    if RFC3339_UTC_RE.fullmatch(text) is None:
        refuse(f"{label} is not strict UTC RFC3339", "READINESS_TIMESTAMP_INVALID", stage=stage)
    try:
        return parse_bracket_instant(text)
    except Issue1895ReadinessError:
        refuse(f"{label} is not calendar-valid UTC RFC3339", "READINESS_TIMESTAMP_INVALID", stage=stage)
        raise AssertionError("unreachable")


def assert_timestamp_order(
    document: Mapping[str, Any], *, stage: str = "readiness"
) -> tuple[datetime, datetime, datetime]:
    started = require_utc(document.get("started_at"), label="started_at", stage=stage)
    ended = require_utc(document.get("ended_at"), label="ended_at", stage=stage)
    generated = require_utc(document.get("generated_at"), label="generated_at", stage=stage)
    if started > ended or ended > generated:
        refuse("receipt timestamps are not ordered", "READINESS_TIMESTAMP_ORDER", stage=stage)
    return started, ended, generated


def assert_bracket(
    *,
    info: os.stat_result,
    document: Mapping[str, Any],
    cmd_start: str,
    cmd_end: str,
    stage: str = "readiness",
) -> None:
    try:
        start = parse_bracket_instant(cmd_start)
        end = parse_bracket_instant(cmd_end)
        assert_report_within_command_bracket(report_mtime=info.st_mtime, start=start, end=end)
        _started, _ended, generated = assert_timestamp_order(document, stage=stage)
        assert_report_within_command_bracket(report_mtime=generated.timestamp(), start=start, end=end)
    except Issue1895ReadinessError as error:
        if error.code in {"BRACKET_INVALID", "REPORT_OUT_OF_BRACKET", "REPORT_MTIME_INVALID"}:
            refuse("receipt is outside the command bracket", "READINESS_BRACKET", stage=stage)
        raise


def read_private_receipt(
    path: Path, *, code_prefix: str, stage: str = "readiness"
) -> tuple[bytes, dict[str, Any], os.stat_result]:
    """Read a mode-0600 receipt through the shipping held-descriptor primitive."""

    try:
        raw, info, _parent = _read_held_descriptor(path, code_prefix=code_prefix)
        value = json.loads(raw.decode("utf-8"))
        validate_json_complexity(
            value,
            label="private receipt",
            max_depth=PRIVATE_RECEIPT_MAX_DEPTH,
            max_nodes=PRIVATE_RECEIPT_MAX_NODES,
            max_array_items=PRIVATE_RECEIPT_MAX_ARRAY_ITEMS,
            max_object_items=PRIVATE_RECEIPT_MAX_OBJECT_ITEMS,
        )
    except Issue1895ReadinessError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, BoundedEvidenceError):
        refuse("private receipt is not valid bounded UTF-8 JSON", f"{code_prefix}_JSON", stage=stage)
        raise AssertionError("unreachable")
    if not isinstance(value, Mapping):
        refuse("private receipt root is not an object", f"{code_prefix}_JSON", stage=stage)
    return raw, dict(value), info


def publish_private_receipt(
    path: Path, document: Mapping[str, Any], *, code_prefix: str, stage: str = "readiness"
) -> None:
    """Exclusive, no-follow, fsync/readback publication via the shipping primitive."""

    if not path.is_absolute() or "\x00" in str(path):
        refuse("receipt path must be absolute", f"{code_prefix}_PATH", stage=stage)
    encoded = json.dumps(dict(document), sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        _publish_regular_file(path, encoded, code_prefix=code_prefix)
        readback, _document, _info = read_private_receipt(path, code_prefix=code_prefix, stage=stage)
    except Issue1895ReadinessError:
        raise
    if readback != encoded:
        refuse("published receipt readback differs", f"{code_prefix}_READBACK", stage=stage)


def _assert_regular_private(info: os.stat_result, *, code: str, stage: str) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        refuse("authoritative input is not a regular file", code, stage=stage)
    if info.st_uid != os.geteuid() or (info.st_mode & 0o777) != FILE_MODE or info.st_nlink != 1:
        refuse("authoritative input ownership or mode drifted", code, stage=stage)


def read_authoritative_json(
    path: Path,
    *,
    containment_root: Path,
    label: str,
    max_bytes: int = MAX_INPUT_BYTES,
    require_private_file: bool = True,
    require_mapping: bool = True,
    stage: str = "readiness",
) -> tuple[bytes, Any, dict[str, int]]:
    """Bounded no-follow JSON reader that also returns descriptor facts and digest.

    Unlike private receipt output, canonical validator evidence owns its nested
    directory layout.  The file itself must nevertheless be an euid-owned 0600
    nlink-1 regular file before it may become an acceptance input.
    """

    try:
        raw, identity, value = read_bounded_json_with_identity_no_follow(
            path,
            max_bytes=max_bytes,
            label=label,
            max_depth=PRIVATE_RECEIPT_MAX_DEPTH,
            max_nodes=PRIVATE_RECEIPT_MAX_NODES,
            max_array_items=PRIVATE_RECEIPT_MAX_ARRAY_ITEMS,
            max_object_items=PRIVATE_RECEIPT_MAX_OBJECT_ITEMS,
        )
    except BoundedEvidenceError:
        refuse("authoritative JSON input is unavailable, unsafe, or oversized", "READINESS_INPUT_INVALID", stage=stage)
        raise AssertionError("unreachable")
    try:
        root_lexical = Path(os.path.abspath(os.fspath(containment_root.expanduser())))
        path_lexical = Path(os.path.abspath(os.fspath(path.expanduser())))
        path_lexical.relative_to(root_lexical)
    except ValueError:
        refuse("authoritative input escapes its expected root", "READINESS_INPUT_PATH", stage=stage)
    # Preserve lexical containment: resolving before the held no-follow read could
    # traverse a symlink. The reader above already rejects symlinked components.
    try:
        info = stat_no_follow(path)
    except (OSError, SafeFilesystemError):
        refuse("authoritative input cannot be stated", "READINESS_INPUT_INVALID", stage=stage)
        raise AssertionError("unreachable")
    if require_private_file:
        _assert_regular_private(info, code="READINESS_INPUT_IDENTITY", stage=stage)
    if int(info.st_dev) != identity.device or int(info.st_ino) != identity.inode or int(info.st_size) != identity.size:
        refuse("authoritative input pathname drifted from held descriptor", "READINESS_INPUT_TOCTOU", stage=stage)
    if require_mapping and not isinstance(value, Mapping):
        refuse("authoritative JSON input root is not an object", "READINESS_INPUT_JSON", stage=stage)
    facts = {
        **descriptor_facts(info),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    return raw, dict(value) if isinstance(value, Mapping) else value, facts


def private_parent_facts(path: Path, *, code_prefix: str) -> dict[str, int]:
    """Expose only non-secret receipt-parent facts for receipt semantic binders."""

    info = _parent_facts(path, code_prefix=code_prefix)
    if (info.st_mode & 0o777) != PARENT_MODE:
        refuse("private receipt parent mode drifted", f"{code_prefix}_PARENT_MODE")
    return {"st_dev": int(info.st_dev), "st_ino": int(info.st_ino), "st_uid": int(info.st_uid), "st_mode": PARENT_MODE}


__all__ = (
    "FILE_MODE",
    "MAX_INPUT_BYTES",
    "MAX_RECEIPT_BYTES",
    "PARENT_MODE",
    "assert_bracket",
    "assert_timestamp_order",
    "descriptor_facts",
    "private_parent_facts",
    "publish_private_receipt",
    "read_authoritative_json",
    "read_private_receipt",
    "refuse",
    "require_matching_shas",
    "require_sha",
    "require_utc",
    "utc_now",
)
