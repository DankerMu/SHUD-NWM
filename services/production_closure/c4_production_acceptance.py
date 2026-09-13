"""Bringup-C4 production-acceptance owner.

Composes the unchanged Node C4 receipt binder. Independent of
``node27_issue1895_*``. Freeze, bind, and verify are three immutable stages
of one private outer chain.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from packages.common.node27_pgdata_command import CommandError, CommandRunner, run_bounded_command
from services.production_closure.c4_production_acceptance_io import (
    C4AcceptanceError,
    HeldPrivateFile,
    ParentIdentity,
    PrivateFileIdentity,
    assert_outputs_absent_and_disjoint,
    assert_receipt_parent_ready,
    capture_held_private_file,
    close_held_private_file,
    format_utc,
    freeze_is_not_later_than_cmd_start,
    identity_from_mapping,
    parent_from_mapping,
    parse_utc,
    publish_private_json,
    read_private_json,
    recapture_held_private_file,
    refuse,
    require_absolute_path,
    require_id,
    require_json_int,
    require_ordered_bracket,
    require_origin,
    require_sha,
    utc_now,
)

SCHEMA_VERSION = "1.0"
FREEZE_ARTIFACT = "nhms-c4-production-acceptance-freeze"
BINDING_ARTIFACT = "nhms-c4-production-acceptance-binding"
ACCEPTANCE_ARTIFACT = "nhms-c4-production-acceptance"
BINDER_RELATIVE = "apps/frontend/scripts/c4-receipt-binder.mjs"
TRUSTED_NODE = Path("/usr/bin/node")
BINDER_TIMEOUT_SECONDS = 10
BINDER_MAX_OUTPUT_BYTES = 64 * 1024
BINDER_PASS = "BINDER: PASS\n"
REPO_ROOT = Path(__file__).resolve().parents[2]

_FREEZE_KEYS = (
    "artifact",
    "schema_version",
    "stage",
    "frozen_at",
    "reviewed_sha",
    "approved_record",
    "c4_inputs",
)
_BINDING_KEYS = (
    "artifact",
    "schema_version",
    "stage",
    "bound_at",
    "reviewed_sha",
    "freeze",
    "c4",
    "c4_inputs",
    "bracket",
)
_ACCEPTANCE_KEYS = (
    "artifact",
    "schema_version",
    "stage",
    "accepted_at",
    "status",
    "reviewed_sha",
    "freeze",
    "binding",
    "c4",
    "c4_inputs",
    "bracket",
)
_APPROVED_RECORD_KEYS = ("path", "identity", "parent", "status", "head_sha", "reviewed_sha")
_FILE_REF_KEYS = ("path", "identity", "parent")
_INPUT_KEYS = ("receipt", "frontend_origin", "api_origin", "basin_id", "segment_id")
_BRACKET_KEYS = ("cmd_start", "cmd_end")


def binder_script_path() -> Path:
    return REPO_ROOT / BINDER_RELATIVE


def _require_closed_object(value: object, keys: Sequence[str], *, code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        refuse("record fields are not the closed owner schema", code)
    return value


def _file_ref(path: Path, identity: PrivateFileIdentity, parent: ParentIdentity) -> dict[str, Any]:
    return {
        "path": str(path),
        "identity": identity.as_mapping(),
        "parent": parent.as_mapping(),
    }


def _parse_file_ref(value: object, *, code: str) -> tuple[Path, PrivateFileIdentity, ParentIdentity]:
    mapping = _require_closed_object(value, _FILE_REF_KEYS, code=code)
    path = require_absolute_path(mapping["path"], code="C4_PATH_INVALID")
    return path, identity_from_mapping(mapping["identity"]), parent_from_mapping(mapping["parent"])


def _parse_inputs(value: object) -> dict[str, str]:
    mapping = _require_closed_object(value, _INPUT_KEYS, code="C4_INPUT_SCHEMA")
    receipt = str(require_absolute_path(mapping["receipt"], code="C4_PATH_INVALID"))
    return {
        "receipt": receipt,
        "frontend_origin": require_origin(mapping["frontend_origin"]),
        "api_origin": require_origin(mapping["api_origin"]),
        "basin_id": require_id(mapping["basin_id"], code="C4_BASIN_INVALID"),
        "segment_id": require_id(mapping["segment_id"], code="C4_SEGMENT_INVALID"),
    }


def _parse_bracket(value: object) -> tuple[int, int]:
    mapping = _require_closed_object(value, _BRACKET_KEYS, code="C4_BRACKET_INVALID")
    start = require_json_int(mapping["cmd_start"], code="C4_BRACKET_INVALID")
    end = require_json_int(mapping["cmd_end"], code="C4_BRACKET_INVALID")
    if start > end:
        refuse("command bracket is not a finite ordered integer pair", "C4_BRACKET_INVALID")
    return start, end


def _read_record(
    path: Path,
) -> tuple[bytes, dict[str, Any], PrivateFileIdentity, ParentIdentity]:
    return read_private_json(path)


def _assert_identity_match(
    observed: PrivateFileIdentity,
    expected: PrivateFileIdentity,
    *,
    code: str,
) -> None:
    if observed != expected:
        refuse("original file identity drifted", code)


def _assert_parent_match(observed: ParentIdentity, expected: ParentIdentity, *, code: str) -> None:
    if observed != expected:
        refuse("original parent identity drifted", code)


def _assert_source_unchanged(
    path: Path,
    expected_identity: PrivateFileIdentity,
    expected_parent: ParentIdentity,
) -> tuple[bytes, dict[str, Any], PrivateFileIdentity, ParentIdentity]:
    raw, document, identity, parent = _read_record(path)
    _assert_identity_match(identity, expected_identity, code="C4_SOURCE_DRIFT")
    _assert_parent_match(parent, expected_parent, code="C4_PARENT_DRIFT")
    return raw, document, identity, parent


def _extract_approval(document: Mapping[str, Any], reviewed_sha: str) -> tuple[str, str, str]:
    status = document.get("status")
    if status != "PASS":
        refuse("approved record status is not PASS", "C4_APPROVAL_STATUS")
    head = document.get("head_sha")
    reviewed = document.get("reviewed_sha")
    if head != reviewed_sha or reviewed != reviewed_sha:
        refuse("approved record SHA does not bind the supplied reviewed SHA", "C4_SHA_MISMATCH")
    if not isinstance(head, str) or not isinstance(reviewed, str):
        refuse("approved record SHA does not bind the supplied reviewed SHA", "C4_SHA_MISMATCH")
    return status, head, reviewed


def _assert_binder_script() -> Path:
    script = binder_script_path()
    try:
        info = os.lstat(script)
    except OSError:
        refuse("C4 binder is unavailable", "C4_BINDER_UNAVAILABLE")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        refuse("C4 binder is unavailable", "C4_BINDER_UNAVAILABLE")
    return script


def _assert_trusted_node() -> None:
    try:
        node_info = os.lstat(TRUSTED_NODE)
    except OSError:
        refuse("C4 binder runtime is unavailable", "C4_BINDER_UNAVAILABLE")
    if stat.S_ISLNK(node_info.st_mode) or not stat.S_ISREG(node_info.st_mode):
        refuse("C4 binder runtime is unavailable", "C4_BINDER_UNAVAILABLE")
    if not os.access(TRUSTED_NODE, os.X_OK):
        refuse("C4 binder runtime is unavailable", "C4_BINDER_UNAVAILABLE")


def binder_argv(
    *,
    receipt: Path,
    frontend_origin: str,
    api_origin: str,
    basin_id: str,
    segment_id: str,
    cmd_start: int,
    cmd_end: int,
) -> list[str]:
    script = _assert_binder_script()
    return [
        str(TRUSTED_NODE),
        str(script),
        "--receipt",
        str(receipt),
        "--frontend-origin",
        frontend_origin,
        "--api-origin",
        api_origin,
        "--basin-id",
        basin_id,
        "--segment-id",
        segment_id,
        "--cmd-start",
        str(cmd_start),
        "--cmd-end",
        str(cmd_end),
    ]


def _translate_command_error(error: CommandError) -> None:
    text = str(error)
    if text == "target inspector timed out":
        refuse("C4 binder timed out", "C4_BINDER_TIMEOUT")
    if text == "target inspector output exceeds the byte ceiling":
        refuse("C4 binder output exceeds the byte ceiling", "C4_BINDER_OUTPUT")
    refuse("C4 binder is unavailable", "C4_BINDER_UNAVAILABLE")


def run_real_c4_binder(
    *,
    receipt: Path,
    frontend_origin: str,
    api_origin: str,
    basin_id: str,
    segment_id: str,
    cmd_start: int,
    cmd_end: int,
    command_runner: CommandRunner | None = None,
) -> None:
    if command_runner is None:
        _assert_trusted_node()
    argv = binder_argv(
        receipt=receipt,
        frontend_origin=frontend_origin,
        api_origin=api_origin,
        basin_id=basin_id,
        segment_id=segment_id,
        cmd_start=cmd_start,
        cmd_end=cmd_end,
    )
    try:
        result = run_bounded_command(
            argv,
            timeout=BINDER_TIMEOUT_SECONDS,
            max_bytes=BINDER_MAX_OUTPUT_BYTES,
            runner=command_runner,
        )
    except CommandError as error:
        _translate_command_error(error)
        raise AssertionError("unreachable") from error
    if result.returncode != 0:
        refuse("C4 binder refused the receipt", "C4_BINDER_FAILED")
    if result.stdout != BINDER_PASS:
        refuse("C4 binder did not emit the PASS terminal", "C4_BINDER_NOT_PASS")


def freeze(
    *,
    approved_record: Path,
    reviewed_sha: str,
    receipt: Path,
    frontend_origin: str,
    api_origin: str,
    basin_id: str,
    segment_id: str,
    output: Path,
    now: Callable[[], datetime] | None = None,
) -> PrivateFileIdentity:
    approved_record = require_absolute_path(str(approved_record), code="C4_PATH_INVALID")
    receipt = require_absolute_path(str(receipt), code="C4_PATH_INVALID")
    output = require_absolute_path(str(output), code="C4_PATH_INVALID")
    reviewed_sha = require_sha(reviewed_sha)
    frontend_origin = require_origin(frontend_origin)
    api_origin = require_origin(api_origin)
    basin_id = require_id(basin_id, code="C4_BASIN_INVALID")
    segment_id = require_id(segment_id, code="C4_SEGMENT_INVALID")
    assert_outputs_absent_and_disjoint([output], [approved_record, receipt])
    assert_receipt_parent_ready(receipt)
    _raw, document, source_identity, source_parent = _read_record(approved_record)
    status, head_sha, source_reviewed = _extract_approval(document, reviewed_sha)
    frozen_at = format_utc((now or utc_now)())
    record = {
        "artifact": FREEZE_ARTIFACT,
        "schema_version": SCHEMA_VERSION,
        "stage": "freeze",
        "frozen_at": frozen_at,
        "reviewed_sha": reviewed_sha,
        "approved_record": {
            "path": str(approved_record),
            "identity": source_identity.as_mapping(),
            "parent": source_parent.as_mapping(),
            "status": status,
            "head_sha": head_sha,
            "reviewed_sha": source_reviewed,
        },
        "c4_inputs": {
            "receipt": str(receipt),
            "frontend_origin": frontend_origin,
            "api_origin": api_origin,
            "basin_id": basin_id,
            "segment_id": segment_id,
        },
    }
    return publish_private_json(output, record, inputs=[approved_record, receipt])


def _load_freeze(path: Path, reviewed_sha: str) -> tuple[dict[str, Any], PrivateFileIdentity, ParentIdentity]:
    _raw, document, identity, parent = _read_record(path)
    mapping = _require_closed_object(document, _FREEZE_KEYS, code="C4_FREEZE_SCHEMA")
    if mapping["artifact"] != FREEZE_ARTIFACT or mapping["schema_version"] != SCHEMA_VERSION:
        refuse("freeze artifact is not the closed owner schema", "C4_FREEZE_SCHEMA")
    if mapping["stage"] != "freeze":
        refuse("freeze artifact is not the closed owner schema", "C4_FREEZE_SCHEMA")
    if mapping["reviewed_sha"] != reviewed_sha:
        refuse("freeze SHA does not bind the supplied reviewed SHA", "C4_SHA_MISMATCH")
    parse_utc(mapping["frozen_at"])
    approved = _require_closed_object(
        mapping["approved_record"],
        _APPROVED_RECORD_KEYS,
        code="C4_FREEZE_SCHEMA",
    )
    if approved["status"] != "PASS":
        refuse("approved record status is not PASS", "C4_APPROVAL_STATUS")
    if approved["head_sha"] != reviewed_sha or approved["reviewed_sha"] != reviewed_sha:
        refuse("approved record SHA does not bind the supplied reviewed SHA", "C4_SHA_MISMATCH")
    _parse_file_ref(
        {"path": approved["path"], "identity": approved["identity"], "parent": approved["parent"]},
        code="C4_FREEZE_SCHEMA",
    )
    _parse_inputs(mapping["c4_inputs"])
    return mapping, identity, parent


def _load_binding(path: Path, reviewed_sha: str) -> tuple[dict[str, Any], PrivateFileIdentity, ParentIdentity]:
    _raw, document, identity, parent = _read_record(path)
    mapping = _require_closed_object(document, _BINDING_KEYS, code="C4_BINDING_SCHEMA")
    if mapping["artifact"] != BINDING_ARTIFACT or mapping["schema_version"] != SCHEMA_VERSION:
        refuse("binding artifact is not the closed owner schema", "C4_BINDING_SCHEMA")
    if mapping["stage"] != "bind":
        refuse("binding artifact is not the closed owner schema", "C4_BINDING_SCHEMA")
    if mapping["reviewed_sha"] != reviewed_sha:
        refuse("binding SHA does not bind the supplied reviewed SHA", "C4_SHA_MISMATCH")
    parse_utc(mapping["bound_at"])
    _parse_file_ref(mapping["freeze"], code="C4_BINDING_SCHEMA")
    _parse_file_ref(mapping["c4"], code="C4_BINDING_SCHEMA")
    _parse_inputs(mapping["c4_inputs"])
    _parse_bracket(mapping["bracket"])
    return mapping, identity, parent


def _run_binder_with_held_receipt(
    *,
    inputs: Mapping[str, str],
    cmd_start: int,
    cmd_end: int,
    command_runner: CommandRunner | None,
) -> tuple[PrivateFileIdentity, ParentIdentity]:
    receipt = require_absolute_path(inputs["receipt"], code="C4_PATH_INVALID")
    held: HeldPrivateFile | None = None
    try:
        held = capture_held_private_file(receipt)
        before = held.identity
        run_real_c4_binder(
            receipt=receipt,
            frontend_origin=inputs["frontend_origin"],
            api_origin=inputs["api_origin"],
            basin_id=inputs["basin_id"],
            segment_id=inputs["segment_id"],
            cmd_start=cmd_start,
            cmd_end=cmd_end,
            command_runner=command_runner,
        )
        after = recapture_held_private_file(held)
        if after != before:
            refuse("C4 receipt identity drifted across the binder", "C4_IDENTITY_DRIFT")
        return after, held.parent
    finally:
        close_held_private_file(held)


def bind(
    *,
    freeze_path: Path,
    reviewed_sha: str,
    cmd_start: str,
    cmd_end: str,
    output: Path,
    command_runner: CommandRunner | None = None,
    now: Callable[[], datetime] | None = None,
) -> PrivateFileIdentity:
    freeze_path = require_absolute_path(str(freeze_path), code="C4_PATH_INVALID")
    output = require_absolute_path(str(output), code="C4_PATH_INVALID")
    reviewed_sha = require_sha(reviewed_sha)
    start, end = require_ordered_bracket(cmd_start, cmd_end)
    freeze_document, freeze_identity, freeze_parent = _load_freeze(freeze_path, reviewed_sha)
    inputs = _parse_inputs(freeze_document["c4_inputs"])
    receipt = require_absolute_path(inputs["receipt"], code="C4_PATH_INVALID")
    approved_path, approved_identity, approved_parent = _parse_file_ref(
        {
            "path": freeze_document["approved_record"]["path"],
            "identity": freeze_document["approved_record"]["identity"],
            "parent": freeze_document["approved_record"]["parent"],
        },
        code="C4_FREEZE_SCHEMA",
    )
    assert_outputs_absent_and_disjoint(
        [output],
        [freeze_path, approved_path, receipt],
    )
    freeze_at = parse_utc(freeze_document["frozen_at"])
    if not freeze_is_not_later_than_cmd_start(freeze_at, start):
        refuse("freeze is later than the C4 command-start second", "C4_FREEZE_AFTER_START")
    _assert_source_unchanged(approved_path, approved_identity, approved_parent)
    _raw, reread, reread_identity, reread_parent = _read_record(freeze_path)
    _assert_identity_match(reread_identity, freeze_identity, code="C4_SOURCE_DRIFT")
    _assert_parent_match(reread_parent, freeze_parent, code="C4_PARENT_DRIFT")
    if reread != freeze_document:
        refuse("original freeze bytes drifted", "C4_SOURCE_DRIFT")
    c4_identity, c4_parent = _run_binder_with_held_receipt(
        inputs=inputs,
        cmd_start=start,
        cmd_end=end,
        command_runner=command_runner,
    )
    _assert_source_unchanged(approved_path, approved_identity, approved_parent)
    _raw, after_freeze, after_identity, after_parent = _read_record(freeze_path)
    _assert_identity_match(after_identity, freeze_identity, code="C4_SOURCE_DRIFT")
    _assert_parent_match(after_parent, freeze_parent, code="C4_PARENT_DRIFT")
    if after_freeze != freeze_document:
        refuse("original freeze bytes drifted", "C4_SOURCE_DRIFT")
    record = {
        "artifact": BINDING_ARTIFACT,
        "schema_version": SCHEMA_VERSION,
        "stage": "bind",
        "bound_at": format_utc((now or utc_now)()),
        "reviewed_sha": reviewed_sha,
        "freeze": _file_ref(freeze_path, freeze_identity, freeze_parent),
        "c4": _file_ref(receipt, c4_identity, c4_parent),
        "c4_inputs": dict(inputs),
        "bracket": {"cmd_start": start, "cmd_end": end},
    }
    return publish_private_json(output, record, inputs=[freeze_path, approved_path, receipt])


def verify(
    *,
    freeze_path: Path,
    binding_path: Path,
    reviewed_sha: str,
    output: Path,
    command_runner: CommandRunner | None = None,
    now: Callable[[], datetime] | None = None,
) -> PrivateFileIdentity:
    freeze_path = require_absolute_path(str(freeze_path), code="C4_PATH_INVALID")
    binding_path = require_absolute_path(str(binding_path), code="C4_PATH_INVALID")
    output = require_absolute_path(str(output), code="C4_PATH_INVALID")
    reviewed_sha = require_sha(reviewed_sha)
    freeze_document, freeze_identity, freeze_parent = _load_freeze(freeze_path, reviewed_sha)
    binding_document, binding_identity, binding_parent = _load_binding(binding_path, reviewed_sha)
    bound_freeze_path, bound_freeze_identity, bound_freeze_parent = _parse_file_ref(
        binding_document["freeze"],
        code="C4_BINDING_SCHEMA",
    )
    if bound_freeze_path != freeze_path:
        refuse("binding does not name the original freeze", "C4_FREEZE_MISMATCH")
    _assert_identity_match(freeze_identity, bound_freeze_identity, code="C4_FREEZE_MISMATCH")
    _assert_parent_match(freeze_parent, bound_freeze_parent, code="C4_FREEZE_MISMATCH")
    inputs = _parse_inputs(freeze_document["c4_inputs"])
    bound_inputs = _parse_inputs(binding_document["c4_inputs"])
    if inputs != bound_inputs:
        refuse("binding inputs drifted from the original freeze", "C4_INPUT_MISMATCH")
    start, end = _parse_bracket(binding_document["bracket"])
    freeze_at = parse_utc(freeze_document["frozen_at"])
    if not freeze_is_not_later_than_cmd_start(freeze_at, start):
        refuse("freeze is later than the C4 command-start second", "C4_FREEZE_AFTER_START")
    receipt = require_absolute_path(inputs["receipt"], code="C4_PATH_INVALID")
    approved_path, approved_identity, approved_parent = _parse_file_ref(
        {
            "path": freeze_document["approved_record"]["path"],
            "identity": freeze_document["approved_record"]["identity"],
            "parent": freeze_document["approved_record"]["parent"],
        },
        code="C4_FREEZE_SCHEMA",
    )
    expected_c4_path, expected_c4_identity, expected_c4_parent = _parse_file_ref(
        binding_document["c4"],
        code="C4_BINDING_SCHEMA",
    )
    if expected_c4_path != receipt:
        refuse("binding C4 path drifted from the original freeze", "C4_INPUT_MISMATCH")
    assert_outputs_absent_and_disjoint(
        [output],
        [freeze_path, binding_path, approved_path, receipt],
    )
    _assert_source_unchanged(approved_path, approved_identity, approved_parent)
    _raw, reread_binding, reread_binding_identity, reread_binding_parent = _read_record(binding_path)
    _assert_identity_match(reread_binding_identity, binding_identity, code="C4_SOURCE_DRIFT")
    _assert_parent_match(reread_binding_parent, binding_parent, code="C4_PARENT_DRIFT")
    if reread_binding != binding_document:
        refuse("original binding bytes drifted", "C4_SOURCE_DRIFT")
    observed_c4, observed_parent = _run_binder_with_held_receipt(
        inputs=inputs,
        cmd_start=start,
        cmd_end=end,
        command_runner=command_runner,
    )
    _assert_identity_match(observed_c4, expected_c4_identity, code="C4_IDENTITY_DRIFT")
    _assert_parent_match(observed_parent, expected_c4_parent, code="C4_PARENT_DRIFT")
    _assert_source_unchanged(approved_path, approved_identity, approved_parent)
    _raw, after_freeze, after_freeze_identity, after_freeze_parent = _read_record(freeze_path)
    _assert_identity_match(after_freeze_identity, freeze_identity, code="C4_SOURCE_DRIFT")
    _assert_parent_match(after_freeze_parent, freeze_parent, code="C4_PARENT_DRIFT")
    if after_freeze != freeze_document:
        refuse("original freeze bytes drifted", "C4_SOURCE_DRIFT")
    _raw, after_binding, after_binding_identity, after_binding_parent = _read_record(binding_path)
    _assert_identity_match(after_binding_identity, binding_identity, code="C4_SOURCE_DRIFT")
    _assert_parent_match(after_binding_parent, binding_parent, code="C4_PARENT_DRIFT")
    if after_binding != binding_document:
        refuse("original binding bytes drifted", "C4_SOURCE_DRIFT")
    record = {
        "artifact": ACCEPTANCE_ARTIFACT,
        "schema_version": SCHEMA_VERSION,
        "stage": "verify",
        "accepted_at": format_utc((now or utc_now)()),
        "status": "PASS",
        "reviewed_sha": reviewed_sha,
        "freeze": _file_ref(freeze_path, freeze_identity, freeze_parent),
        "binding": _file_ref(binding_path, binding_identity, binding_parent),
        "c4": _file_ref(receipt, expected_c4_identity, expected_c4_parent),
        "c4_inputs": dict(inputs),
        "bracket": {"cmd_start": start, "cmd_end": end},
    }
    return publish_private_json(
        output,
        record,
        inputs=[freeze_path, binding_path, approved_path, receipt],
    )


__all__ = (
    "ACCEPTANCE_ARTIFACT",
    "BINDING_ARTIFACT",
    "BINDER_PASS",
    "C4AcceptanceError",
    "FREEZE_ARTIFACT",
    "SCHEMA_VERSION",
    "TRUSTED_NODE",
    "bind",
    "binder_argv",
    "binder_script_path",
    "freeze",
    "run_real_c4_binder",
    "verify",
)
