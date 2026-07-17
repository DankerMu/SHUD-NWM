"""One durable state machine for issue-1069 compression terminal evidence.

The live verifier, replay supervisor, and ``ExecStopPost`` finalizer all use
this module.  It owns the only terminal publication implementation and the
only adjacent failure-intent representation.  Every operation follows the
bounded intent-gate -> anchored terminal-lock order.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

from packages.common.evidence_io import (
    BoundedEvidenceError,
    FileIdentity,
    acquire_exclusive_flock_until,
    inspect_bounded_file_no_follow,
    read_bounded_json_no_follow,
    reject_secret_material,
    validate_json_complexity,
)
from packages.common.safe_fs import SafeFilesystemError, open_directory_no_follow

SCHEMA_VERSION = "3.0"
INTENT_STATE_SCHEMA_VERSION = "1.0"
MAX_TERMINAL_BYTES = 16 * 1024**2
MAX_INTENT_STATE_BYTES = 64 * 1024
DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
CANONICAL_SCHEMA = Path(__file__).parents[2] / "schemas/timeseries_compression_live_evidence.schema.json"
_ANY_EXPECTED_IDENTITY = object()

# A refused tombstone must never present to the operator as merely "the
# finalizer did not replace the receipt".  Warnings reach the unit's captured
# stderr through logging's last-resort handler even with no handler installed.
_LOGGER = logging.getLogger(__name__)


class TerminalStateError(RuntimeError):
    """The terminal cannot be read or changed without violating its contract."""


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TerminalStateError(f"{label} must be an object")
    return value


def _require_exact_keys(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise TerminalStateError(f"{label} identity keys differ from the terminal contract")


def _reject_secrets(value: Any, label: str) -> None:
    try:
        reject_secret_material(value, label=label)
    except BoundedEvidenceError as error:
        raise TerminalStateError(str(error)) from error


def _terminal_lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.publish.lock")


def _terminal_intent_gate_path(path: Path) -> Path:
    """The contentless ``flock`` object whose inode anchors every intent binding."""

    return path.with_name(f".{path.name}.intent-gate.lock")


def _terminal_intent_state_path(path: Path) -> Path:
    """The gate's state document, replaced only by rename under the gate lock."""

    return path.with_name(f".{path.name}.intent-gate.json")


def _terminal_intent_root_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.failure-intent")


def _terminal_intent_path(path: Path) -> Path:
    return _terminal_intent_root_path(path) / "intent.json"


def _terminal_intent_identity_path(path: Path) -> Path:
    return _terminal_intent_root_path(path) / "identity.json"


def terminal_identity(path: Path) -> FileIdentity | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise TerminalStateError("terminal output identity is unavailable") from error
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise TerminalStateError("terminal output is not a single-link regular file")
    try:
        return inspect_bounded_file_no_follow(path, max_bytes=MAX_TERMINAL_BYTES, label="terminal output")
    except BoundedEvidenceError as error:
        raise TerminalStateError(str(error)) from error


def _identity_document(identity: FileIdentity | None) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {
        "device": identity.device,
        "inode": identity.inode,
        "bytes": identity.size,
        "sha256": identity.sha256,
    }


def _validate_identity_document(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    document = _require_mapping(value, "terminal expected identity")
    _require_exact_keys(document, {"device", "inode", "bytes", "sha256"}, "terminal expected identity")
    if (
        any(
            not isinstance(document[key], int) or isinstance(document[key], bool) or document[key] < 0
            for key in ("device", "inode", "bytes")
        )
        or re.fullmatch(r"[0-9a-f]{64}", str(document["sha256"])) is None
    ):
        raise TerminalStateError("terminal expected identity is invalid")
    return dict(document)


def identity_from_document(path: Path, value: Mapping[str, Any]) -> FileIdentity:
    document = _validate_identity_document(value)
    if document is None:
        raise TerminalStateError("terminal expected identity is absent")
    return FileIdentity(
        path=path,
        normalized_path=Path(os.path.normpath(os.path.abspath(path))),
        device=int(document["device"]),
        inode=int(document["inode"]),
        size=int(document["bytes"]),
        sha256=str(document["sha256"]),
    )


def _gate_identity_document(info: os.stat_result) -> dict[str, int]:
    return {"device": info.st_dev, "inode": info.st_ino}


def _validate_gate_identity_document(value: Any) -> dict[str, int]:
    document = _require_mapping(value, "terminal intent gate identity")
    _require_exact_keys(document, {"device", "inode"}, "terminal intent gate identity")
    if any(
        not isinstance(document[key], int) or isinstance(document[key], bool) or document[key] < 0
        for key in ("device", "inode")
    ):
        raise TerminalStateError("terminal intent gate identity is invalid")
    return {"device": int(document["device"]), "inode": int(document["inode"])}


def _validate_intent_context(value: Any) -> dict[str, Any]:
    context = _require_mapping(value, "terminal intent context")
    provenance_state = context.get("provenance_state")
    if provenance_state == "unavailable":
        _require_exact_keys(
            context,
            {"schema_version", "provenance_state", "verifier_head_sha"},
            "unavailable terminal intent context",
        )
        verifier = context["verifier_head_sha"]
        if verifier is not None and re.fullmatch(r"[0-9a-f]{40}", str(verifier)) is None:
            raise TerminalStateError("terminal intent trusted verifier identity is invalid")
    else:
        _require_exact_keys(
            context,
            {"schema_version", "provenance_state", "run_id", "verifier_head_sha", "mutation_head_sha"},
            "bound terminal intent context",
        )
        verifier = context["verifier_head_sha"]
        if (
            provenance_state != "bound"
            or not isinstance(context["run_id"], str)
            or not context["run_id"]
            or len(context["run_id"].encode()) > 256
            or re.fullmatch(r"[0-9a-f]{40}", str(context["mutation_head_sha"])) is None
            or (verifier is not None and re.fullmatch(r"[0-9a-f]{40}", str(verifier)) is None)
        ):
            raise TerminalStateError("terminal intent bound provenance identity is invalid")
    if context["schema_version"] != SCHEMA_VERSION:
        raise TerminalStateError("terminal intent schema identity is invalid")
    result = dict(context)
    _reject_secrets(result, "terminal intent context")
    return result


def unavailable_intent_context(verifier_head_sha: str | None) -> dict[str, Any]:
    return _validate_intent_context(
        {
            "schema_version": SCHEMA_VERSION,
            "provenance_state": "unavailable",
            "verifier_head_sha": verifier_head_sha,
        }
    )


def bound_intent_context(
    *, run_id: str, mutation_head_sha: str, verifier_head_sha: str | None = None
) -> dict[str, Any]:
    return _validate_intent_context(
        {
            "schema_version": SCHEMA_VERSION,
            "provenance_state": "bound",
            "run_id": run_id,
            "verifier_head_sha": verifier_head_sha,
            "mutation_head_sha": mutation_head_sha,
        }
    )


def _intent_context_from_terminal(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("provenance_state") == "unavailable":
        context = _require_mapping(value.get("failure_context"), "unavailable terminal failure context")
        return unavailable_intent_context(context.get("verifier_head_sha"))
    run_id = value.get("run_id")
    if value.get("qualifies_task_4_5") is True:
        execution = _require_mapping(value.get("execution"), "terminal execution")
        run_id = execution.get("run_id")
    return bound_intent_context(
        run_id=str(run_id or ""),
        mutation_head_sha=str(value.get("mutation_head_sha", "")),
        verifier_head_sha=(None if value.get("verifier_head_sha") is None else str(value["verifier_head_sha"])),
    )


def _payload_context_matches(
    payload_context: Mapping[str, Any], intent_context: Mapping[str, Any]
) -> bool:
    payload = _validate_intent_context(payload_context)
    intent = _validate_intent_context(intent_context)
    if payload["provenance_state"] == "unavailable" or intent["provenance_state"] == "unavailable":
        return payload == intent
    return (
        payload["run_id"] == intent["run_id"]
        and payload["mutation_head_sha"] == intent["mutation_head_sha"]
        and payload["verifier_head_sha"] in {None, intent["verifier_head_sha"]}
    )


def _schema(schema_path: Path) -> Mapping[str, Any]:
    try:
        _, value = read_bounded_json_no_follow(
            schema_path,
            max_bytes=1024**2,
            label="compression terminal schema",
            max_depth=48,
            max_nodes=50_000,
            max_array_items=10_000,
        )
    except BoundedEvidenceError as error:
        raise TerminalStateError(str(error)) from error
    return _require_mapping(value, "compression terminal schema")


def validate_terminal_document(
    value: Any, *, schema_path: Path = CANONICAL_SCHEMA
) -> dict[str, Any]:
    document = _require_mapping(value, "compression terminal")
    _reject_secrets(document, "compression terminal")
    try:
        jsonschema.Draft202012Validator(
            _schema(schema_path), format_checker=jsonschema.FormatChecker()
        ).validate(document)
    except jsonschema.ValidationError as error:
        raise TerminalStateError("compression terminal is not schema-valid") from error
    if document.get("schema_version") == SCHEMA_VERSION and document.get("outcome") == "failed":
        failure = _require_mapping(document.get("failure"), "terminal failure")
        if document.get("provenance_state") == "unavailable":
            context = _require_mapping(document.get("failure_context"), "unavailable terminal failure context")
            _require_exact_keys(
                context,
                {"reason_category", "expected_output", "verifier_head_sha"},
                "unavailable terminal failure context",
            )
            _validate_identity_document(context["expected_output"])
            verifier = context["verifier_head_sha"]
            if (
                context["reason_category"] != failure.get("stage")
                or (verifier is not None and re.fullmatch(r"[0-9a-f]{40}", str(verifier)) is None)
                or "run_id" in document
                or "mutation_head_sha" in document
            ):
                raise TerminalStateError("unavailable terminal failure context differs")
        elif (
            document.get("provenance_state") != "bound"
            or not isinstance(document.get("run_id"), str)
            or not document["run_id"]
            or re.fullmatch(r"[0-9a-f]{40}", str(document.get("mutation_head_sha", ""))) is None
            or "failure_context" in document
        ):
            raise TerminalStateError("bound terminal failure provenance differs")
    if document.get("schema_version") == SCHEMA_VERSION and document.get("qualifies_task_4_5") is True:
        if any(key in document for key in ("failure", "failure_context", "provenance_state", "outcome")):
            raise TerminalStateError("qualifying terminal contains failure-only state")
    return dict(document)


def _failure_payload(
    *,
    stage: str,
    expected_output: FileIdentity | None,
    context: Mapping[str, Any],
    mutation_state: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    validated_context = _validate_intent_context(context)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "qualifies_task_4_5": False,
        "generated_at": generated_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "outcome": "failed",
        "provenance_state": validated_context["provenance_state"],
        "failure": {"stage": stage, "mutation_state": mutation_state},
    }
    if validated_context["provenance_state"] == "unavailable":
        payload["failure_context"] = {
            "reason_category": stage,
            "expected_output": _identity_document(expected_output),
            "verifier_head_sha": validated_context["verifier_head_sha"],
        }
    else:
        payload["run_id"] = validated_context["run_id"]
        payload["mutation_head_sha"] = validated_context["mutation_head_sha"]
    return validate_terminal_document(payload)


def unavailable_failure_payload(
    *, stage: str, expected_output: FileIdentity | None, verifier_head_sha: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    context = unavailable_intent_context(verifier_head_sha)
    return (
        _failure_payload(
            stage=stage,
            expected_output=expected_output,
            context=context,
            mutation_state="indeterminate",
        ),
        context,
    )


def bound_failure_payload(
    *,
    stage: str,
    expected_output: FileIdentity,
    run_id: str,
    mutation_head_sha: str,
    possible_mutation: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    context = bound_intent_context(run_id=run_id, mutation_head_sha=mutation_head_sha)
    return (
        _failure_payload(
            stage=stage,
            expected_output=expected_output,
            context=context,
            mutation_state=("indeterminate" if possible_mutation else "failed_before_mutation"),
        ),
        context,
    )


def _validate_failure_payload(value: Any, *, stage: str | None = None) -> dict[str, Any]:
    payload = validate_terminal_document(value)
    failure = _require_mapping(payload.get("failure"), "terminal failure payload")
    if payload.get("outcome") != "failed" or (stage is not None and failure.get("stage") != stage):
        raise TerminalStateError("terminal failure payload stage differs")
    return payload


def _revalidate_directory_fd(directory_fd: int, path: Path, *, label: str) -> None:
    try:
        expected = os.fstat(directory_fd)
        fresh_fd = open_directory_no_follow(path)
        try:
            current = os.fstat(fresh_fd)
        finally:
            os.close(fresh_fd)
    except (OSError, SafeFilesystemError) as error:
        raise TerminalStateError(f"{label} identity is unavailable") from error
    if not stat.S_ISDIR(expected.st_mode) or (expected.st_dev, expected.st_ino) != (
        current.st_dev,
        current.st_ino,
    ):
        raise TerminalStateError(f"{label} identity changed")


def _fsync_directory_fd(directory_fd: int, path: Path, *, label: str) -> None:
    try:
        os.fsync(directory_fd)
    except OSError as error:
        raise TerminalStateError(f"{label} fsync failed") from error
    _revalidate_directory_fd(directory_fd, path, label=label)


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise TerminalStateError("terminal state write made no progress")
        view = view[written:]


def _read_regular_fd(fd: int, *, max_bytes: int, label: str) -> bytes:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_bytes:
        raise TerminalStateError(f"{label} is not a bounded single-link regular file")
    os.lseek(fd, 0, os.SEEK_SET)
    raw = bytearray()
    while len(raw) < before.st_size:
        chunk = os.read(fd, min(64 * 1024, before.st_size - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
    after = os.fstat(fd)
    if len(raw) != before.st_size or (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ):
        raise TerminalStateError(f"{label} changed while being read")
    return bytes(raw)


def _read_identity_at(
    parent_fd: int, *, name: str, path: Path, max_bytes: int, require_mode: int | None = None
) -> tuple[bytes, FileIdentity]:
    expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(expected.st_mode)
        or expected.st_nlink != 1
        or (require_mode is not None and stat.S_IMODE(expected.st_mode) != require_mode)
    ):
        raise TerminalStateError("terminal state file identity/mode differs")
    fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise TerminalStateError("terminal state file changed while opening")
        raw = _read_regular_fd(fd, max_bytes=max_bytes, label="terminal state file")
    finally:
        os.close(fd)
    return raw, FileIdentity(
        path=path,
        normalized_path=Path(os.path.normpath(os.path.abspath(path))),
        device=expected.st_dev,
        inode=expected.st_ino,
        size=expected.st_size,
        sha256=_sha256(raw),
    )


def _read_json_at(
    parent_fd: int, *, name: str, path: Path, max_bytes: int, require_mode: int | None = None
) -> tuple[bytes, FileIdentity, Mapping[str, Any]]:
    raw, identity = _read_identity_at(
        parent_fd, name=name, path=path, max_bytes=max_bytes, require_mode=require_mode
    )
    try:
        parsed = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise TerminalStateError("terminal state file is not JSON") from error
    try:
        validate_json_complexity(
            parsed,
            label="terminal state file",
            max_depth=48,
            max_nodes=250_000,
            max_array_items=25_000,
        )
    except BoundedEvidenceError as error:
        raise TerminalStateError(str(error)) from error
    value = _require_mapping(parsed, "terminal state file")
    if raw != _canonical(value):
        raise TerminalStateError("terminal state file is not canonical JSON")
    return raw, identity, value


def _write_gate_state(parent_fd: int, path: Path, state: Mapping[str, Any]) -> None:
    """Replace the whole gate state document by rename while holding the gate lock.

    The lock file and the state document are deliberately different inodes, so
    a state transition is one atomic rename rather than an ``ftruncate``/
    ``write`` pair over the very file callers are blocked on.  No crash can
    therefore observe a partial gate state.
    """

    raw = _canonical(state)
    if len(raw) > MAX_INTENT_STATE_BYTES:
        raise TerminalStateError("terminal intent gate exceeds its byte ceiling")
    parent_path = path.parent
    state_name = _terminal_intent_state_path(path).name
    temp_name = f".{path.name}.intent-gate.{uuid.uuid4().hex}.tmp"
    fd = os.open(
        temp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=parent_fd,
    )
    replaced = False
    try:
        _write_all(fd, raw)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size != len(raw)
        ):
            raise TerminalStateError("terminal intent gate state identity/mode differs")
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temp_name, state_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        replaced = True
        _fsync_directory_fd(parent_fd, parent_path, label="terminal intent parent")
    finally:
        if fd >= 0:
            os.close(fd)
        if not replaced:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _read_gate_state(parent_fd: int, path: Path) -> dict[str, Any]:
    state_path = _terminal_intent_state_path(path)
    try:
        raw, _ = _read_identity_at(
            parent_fd,
            name=state_path.name,
            path=state_path,
            max_bytes=MAX_INTENT_STATE_BYTES,
            require_mode=0o600,
        )
    except FileNotFoundError:
        # A gate that never transitioned has no state document at all.
        return {"schema_version": INTENT_STATE_SCHEMA_VERSION, "state": "idle"}
    try:
        parsed = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise TerminalStateError("terminal intent gate is not JSON") from error
    try:
        validate_json_complexity(
            parsed,
            label="terminal intent gate",
            max_depth=12,
            max_nodes=128,
            max_array_items=8,
        )
    except BoundedEvidenceError as error:
        raise TerminalStateError(str(error)) from error
    value = _require_mapping(parsed, "terminal intent gate")
    if raw != _canonical(value):
        raise TerminalStateError("terminal intent gate is not canonical JSON")
    if value.get("state") == "idle":
        _require_exact_keys(value, {"schema_version", "state"}, "terminal intent gate idle state")
    else:
        keys = {
            "schema_version",
            "state",
            "intent_directory",
            "output_path",
            "intent",
            "identity",
            "failure_payload_sha256",
            "context",
        }
        if value.get("state") in {"consuming", "committed_cleanup"}:
            keys.add("consumed_directory")
        if value.get("state") == "committed_cleanup":
            keys.update(
                {
                    "published_terminal",
                    "expected",
                    "cleanup_entries",
                    "consumed_directory_identity",
                }
            )
        _require_exact_keys(value, keys, "terminal intent gate pending state")
        if value.get("state") not in {"pending", "consuming", "committed_cleanup"}:
            raise TerminalStateError("terminal intent gate state differs")
        _validate_identity_document(value["intent"])
        _validate_identity_document(value["identity"])
        _validate_intent_context(value["context"])
        if re.fullmatch(r"[0-9a-f]{64}", str(value["failure_payload_sha256"])) is None:
            raise TerminalStateError("terminal intent gate payload digest is invalid")
        if value.get("state") == "committed_cleanup":
            if _validate_identity_document(value["published_terminal"]) is None:
                raise TerminalStateError("committed cleanup terminal identity is absent")
            _validate_identity_document(value["expected"])
            _validate_gate_identity_document(value["consumed_directory_identity"])
            entries = _require_mapping(value["cleanup_entries"], "committed cleanup entries")
            _require_exact_keys(entries, {"intent.json", "identity.json"}, "committed cleanup entries")
            if (
                _validate_identity_document(entries["intent.json"])
                != _validate_identity_document(value["intent"])
                or _validate_identity_document(entries["identity.json"])
                != _validate_identity_document(value["identity"])
            ):
                raise TerminalStateError("committed cleanup entry identities differ")
    if value.get("schema_version") != INTENT_STATE_SCHEMA_VERSION:
        raise TerminalStateError("terminal intent gate schema differs")
    _reject_secrets(value, "terminal intent gate")
    return dict(value)


@contextmanager
def _locked_intent_gate(
    path: Path, *, label: str, deadline_monotonic: float | None = None
) -> Iterator[tuple[int, int]]:
    """Hold the contentless gate lock and yield its fd plus the anchored parent.

    The yielded gate fd is never written: it exists only to serialize the state
    machine and to give the intent sidecar a stable ``(dev, ino)`` anchor.  Gate
    state lives in a separate document (see ``_write_gate_state``).
    """

    gate_path = _terminal_intent_gate_path(path)
    parent_path = gate_path.parent
    deadline = deadline_monotonic or (time.monotonic() + DEFAULT_LOCK_TIMEOUT_SECONDS)
    try:
        parent_fd = open_directory_no_follow(parent_path)
    except SafeFilesystemError as error:
        raise TerminalStateError("terminal intent parent is unavailable or unsafe") from error
    gate_fd: int | None = None
    gate_info: os.stat_result | None = None
    locked = False
    creation_committed = False
    release_error: Exception | None = None
    try:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        created = False
        try:
            gate_fd = os.open(gate_path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            gate_fd = os.open(gate_path.name, flags, dir_fd=parent_fd)
        gate_info = os.fstat(gate_fd)
        current = os.stat(gate_path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(gate_info.st_mode)
            or gate_info.st_nlink != 1
            or stat.S_IMODE(gate_info.st_mode) != 0o600
            or (current.st_dev, current.st_ino) != (gate_info.st_dev, gate_info.st_ino)
        ):
            raise TerminalStateError("terminal intent gate identity/mode differs")
        if created:
            os.fsync(gate_fd)
            _fsync_directory_fd(parent_fd, parent_path, label="terminal intent parent")
            creation_committed = True
        acquire_exclusive_flock_until(gate_fd, deadline_monotonic=deadline, label=label)
        locked = True
        yield gate_fd, parent_fd
    except (OSError, BoundedEvidenceError) as error:
        raise TerminalStateError(f"{label} failed safely") from error
    finally:
        if gate_fd is not None and created and not creation_committed and gate_info is not None:
            try:
                current = os.stat(gate_path.name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (gate_info.st_dev, gate_info.st_ino):
                    os.unlink(gate_path.name, dir_fd=parent_fd)
            except OSError:
                pass
        if gate_fd is not None:
            if locked:
                try:
                    _revalidate_directory_fd(parent_fd, parent_path, label="terminal intent parent")
                    current = os.stat(gate_path.name, dir_fd=parent_fd, follow_symlinks=False)
                    if gate_info is None or (current.st_dev, current.st_ino) != (
                        gate_info.st_dev,
                        gate_info.st_ino,
                    ):
                        raise TerminalStateError("terminal intent gate changed before release")
                except Exception as error:
                    release_error = error
                fcntl.flock(gate_fd, fcntl.LOCK_UN)
            os.close(gate_fd)
        os.close(parent_fd)
        if release_error is not None:
            raise release_error


def _open_intent_directory(parent_fd: int, parent_path: Path, name: str) -> int:
    if "/" in name or name in {"", ".", ".."}:
        raise TerminalStateError("terminal intent directory name is unsafe")
    expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(expected.st_mode) or stat.S_IMODE(expected.st_mode) != 0o700:
        raise TerminalStateError("terminal intent path is not a mode-0700 directory")
    fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(fd)
        raise TerminalStateError("terminal intent directory identity changed")
    _revalidate_directory_fd(parent_fd, parent_path, label="terminal intent parent")
    return fd


def _create_json_file_at(directory_fd: int, *, name: str, path: Path, raw: bytes) -> FileIdentity:
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        _write_all(fd, raw)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size != len(raw)
        ):
            raise TerminalStateError("new terminal intent file identity/mode differs")
        os.fsync(fd)
        return FileIdentity(
            path=path,
            normalized_path=Path(os.path.normpath(os.path.abspath(path))),
            device=info.st_dev,
            inode=info.st_ino,
            size=info.st_size,
            sha256=_sha256(raw),
        )
    finally:
        os.close(fd)


def _validate_intent_sidecar(
    sidecar: Mapping[str, Any],
    *,
    path: Path,
    gate_fd: int,
    state: Mapping[str, Any],
    intent_identity: FileIdentity,
    identity_identity: FileIdentity,
    payload_digest: str,
    context: Mapping[str, Any],
) -> None:
    _require_exact_keys(
        sidecar,
        {"schema_version", "output_path", "gate", "intent", "failure_payload_sha256", "context"},
        "terminal intent identity sidecar",
    )
    if (
        sidecar["schema_version"] != INTENT_STATE_SCHEMA_VERSION
        or sidecar["output_path"] != str(path)
        or _validate_gate_identity_document(sidecar["gate"]) != _gate_identity_document(os.fstat(gate_fd))
        or _validate_identity_document(sidecar["intent"]) != _identity_document(intent_identity)
        or sidecar["failure_payload_sha256"] != payload_digest
        or _validate_intent_context(sidecar["context"]) != context
        or state["output_path"] != str(path)
        or _validate_identity_document(state["intent"]) != _identity_document(intent_identity)
        or _validate_identity_document(state["identity"]) != _identity_document(identity_identity)
        or state["failure_payload_sha256"] != payload_digest
        or _validate_intent_context(state["context"]) != context
    ):
        raise TerminalStateError("terminal intent durable identity cross-binding differs")
    _reject_secrets(sidecar, "terminal intent identity sidecar")


def _bound_intent_directory_locked(
    path: Path, *, gate_fd: int, parent_fd: int, directory_name: str
) -> dict[str, Any] | None:
    """Return a fully cross-bound intent directory, or ``None`` when it is not.

    The binding proves the directory is this gate's own durable intent: the
    sidecar names this gate lock's inode, the observed ``intent.json`` identity,
    the payload digest, and a context the payload agrees with.  Because the
    sidecar is written by the same uid that owns the terminal, this proves
    durable self-consistency, not authorship.
    """

    parent_path = path.parent
    try:
        directory_fd = _open_intent_directory(parent_fd, parent_path, directory_name)
    except (OSError, TerminalStateError):
        return None
    try:
        if set(os.listdir(directory_fd)) != {"intent.json", "identity.json"}:
            return None
        _, intent_identity, intent_raw = _read_json_at(
            directory_fd,
            name="intent.json",
            path=parent_path / directory_name / "intent.json",
            max_bytes=MAX_INTENT_STATE_BYTES,
            require_mode=0o600,
        )
        _, identity_identity, sidecar_raw = _read_json_at(
            directory_fd,
            name="identity.json",
            path=parent_path / directory_name / "identity.json",
            max_bytes=MAX_INTENT_STATE_BYTES,
            require_mode=0o600,
        )
        sidecar = _require_mapping(sidecar_raw, "terminal intent identity sidecar")
        _require_exact_keys(
            sidecar,
            {"schema_version", "output_path", "gate", "intent", "failure_payload_sha256", "context"},
            "terminal intent identity sidecar",
        )
        intent = _require_mapping(intent_raw, "terminal publication intent")
        _require_exact_keys(
            intent, {"output_path", "expected", "payload"}, "terminal publication intent"
        )
        _validate_identity_document(intent["expected"])
        payload = _validate_failure_payload(intent["payload"])
        context = _validate_intent_context(sidecar["context"])
        payload_digest = _sha256(_canonical(payload))
        if (
            sidecar["schema_version"] != INTENT_STATE_SCHEMA_VERSION
            or sidecar["output_path"] != str(path)
            or intent["output_path"] != str(path)
            or _validate_gate_identity_document(sidecar["gate"])
            != _gate_identity_document(os.fstat(gate_fd))
            or _validate_identity_document(sidecar["intent"]) != _identity_document(intent_identity)
            or sidecar["failure_payload_sha256"] != payload_digest
            or not _payload_context_matches(_intent_context_from_terminal(payload), context)
        ):
            return None
        _reject_secrets(intent, "terminal publication intent")
        _reject_secrets(sidecar, "terminal intent identity sidecar")
    except (OSError, TerminalStateError):
        # Every rejection is fail-closed: callers either keep the original
        # error or refuse to rebuild, and never see a foreign exception class.
        return None
    finally:
        os.close(directory_fd)
    return {
        "intent_identity": intent_identity,
        "identity_identity": identity_identity,
        "payload_digest": payload_digest,
        "context": context,
    }


def _intent_directory_entries_locked(
    parent_fd: int, parent_path: Path, name: str
) -> frozenset[str] | None:
    """List an anchored mode-0700 intent directory, or ``None`` when unreadable."""

    try:
        directory_fd = _open_intent_directory(parent_fd, parent_path, name)
    except (OSError, TerminalStateError):
        return None
    try:
        return frozenset(os.listdir(directory_fd))
    except OSError:
        return None
    finally:
        os.close(directory_fd)


def _collect_create_crash_prefix_locked(
    path: Path, *, parent_fd: int, root_name: str, entries: frozenset[str]
) -> bool:
    """Drop a strict create prefix the idle gate proves was never committed.

    Only the two known entry names are ever unlinked, each as a mode-0600
    single-link regular file reached through the anchored directory descriptor,
    so an aliased evidence input can never be removed.  Collection is
    opportunistic: it reports failure instead of raising, and anything
    unprovable stays untouched.
    """

    parent_path = path.parent
    try:
        directory_fd = _open_intent_directory(parent_fd, parent_path, root_name)
    except (OSError, TerminalStateError):
        return False
    try:
        for name in sorted(entries):
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_INTENT_STATE_BYTES
            ):
                return False
            os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        if os.listdir(directory_fd):
            return False
    except OSError:
        return False
    finally:
        os.close(directory_fd)
    try:
        os.rmdir(root_name, dir_fd=parent_fd)
    except OSError:
        return False
    _fsync_directory_fd(parent_fd, parent_path, label="terminal intent parent")
    return True


def _resolve_uncommitted_intent_locked(
    path: Path, *, gate_fd: int, parent_fd: int, root_name: str
) -> dict[str, Any] | None:
    """Finish or collect an intent directory whose creation the gate never committed.

    The gate state document is replaced atomically, so an idle gate is durable
    proof that no intent ever reached its commit point and the directory can
    only be a create-crash remnant.  A strict create prefix (neither entry, or
    just one of them) can never publish anything, so it is collected and the
    caller sees no intent.  A complete directory still carries the whole
    cross-binding, so its interrupted commit is finished rather than discarding
    a decision the verifier durably recorded.  A complete-but-unbound directory
    is neither: it fails closed, untouched.
    """

    entries = _intent_directory_entries_locked(parent_fd, path.parent, root_name)
    if entries is not None and entries <= {"intent.json", "identity.json"} and len(entries) < 2:
        if _collect_create_crash_prefix_locked(
            path, parent_fd=parent_fd, root_name=root_name, entries=entries
        ):
            return None
        raise TerminalStateError("terminal intent create-crash remnant cannot be collected")
    bound = _bound_intent_directory_locked(
        path, gate_fd=gate_fd, parent_fd=parent_fd, directory_name=root_name
    )
    if bound is None:
        raise TerminalStateError("terminal intent exists without durable gate state")
    _write_gate_state(
        parent_fd,
        path,
        {
            "schema_version": INTENT_STATE_SCHEMA_VERSION,
            "state": "pending",
            "intent_directory": root_name,
            "output_path": str(path),
            "intent": _identity_document(bound["intent_identity"]),
            "identity": _identity_document(bound["identity_identity"]),
            "failure_payload_sha256": bound["payload_digest"],
            "context": bound["context"],
        },
    )
    return _read_gate_state(parent_fd, path)


def _load_pending_intent_locked(
    path: Path,
    *,
    gate_fd: int,
    parent_fd: int,
    expected: FileIdentity | None | object = _ANY_EXPECTED_IDENTITY,
) -> dict[str, Any] | None:
    root_name = _terminal_intent_root_path(path).name
    try:
        os.stat(root_name, dir_fd=parent_fd, follow_symlinks=False)
        active_exists = True
    except FileNotFoundError:
        active_exists = False
    state = _read_gate_state(parent_fd, path)
    if state["state"] == "idle":
        if not active_exists:
            return None
        state = _resolve_uncommitted_intent_locked(
            path, gate_fd=gate_fd, parent_fd=parent_fd, root_name=root_name
        )
        if state is None:
            return None
    if state["intent_directory"] != root_name:
        raise TerminalStateError("terminal intent directory differs from gate state")

    parent_path = path.parent
    directory_name: str | None = root_name
    if state["state"] in {"consuming", "committed_cleanup"}:
        consumed_name = str(state["consumed_directory"])
        if not consumed_name.startswith(f"{root_name}.consumed-") or "/" in consumed_name:
            raise TerminalStateError("terminal consumed-intent directory differs")
        try:
            os.stat(consumed_name, dir_fd=parent_fd, follow_symlinks=False)
            consumed_exists = True
        except FileNotFoundError:
            consumed_exists = False
        if state["state"] == "consuming":
            if active_exists == consumed_exists:
                raise TerminalStateError("terminal consuming intent has ambiguous durable location")
            directory_name = root_name if active_exists else consumed_name
        else:
            if active_exists:
                raise TerminalStateError("committed cleanup cannot retain the active intent directory")
            directory_name = consumed_name if consumed_exists else None
    elif not active_exists:
        raise TerminalStateError("terminal pending intent directory is absent")

    if state["state"] == "committed_cleanup":
        stored_expected = _validate_identity_document(state["expected"])
        if expected is not _ANY_EXPECTED_IDENTITY and stored_expected != _identity_document(expected):
            raise TerminalStateError("committed cleanup expected identity differs")
        context = _validate_intent_context(state["context"])
        entries_document = _require_mapping(state["cleanup_entries"], "committed cleanup entries")
        intent_identity = identity_from_document(
            parent_path / str(state["consumed_directory"]) / "intent.json",
            _require_mapping(entries_document["intent.json"], "committed intent identity"),
        )
        identity_identity = identity_from_document(
            parent_path / str(state["consumed_directory"]) / "identity.json",
            _require_mapping(entries_document["identity.json"], "committed sidecar identity"),
        )
        if directory_name is None:
            return {
                "state": state,
                "directory_name": None,
                "entries": frozenset(),
                "intent_identity": intent_identity,
                "identity_identity": identity_identity,
                "expected": stored_expected,
                "payload": None,
                "context": context,
            }
        directory_info = os.stat(directory_name, dir_fd=parent_fd, follow_symlinks=False)
        if _validate_gate_identity_document(
            state["consumed_directory_identity"]
        ) != _gate_identity_document(directory_info):
            raise TerminalStateError("committed cleanup directory identity changed")
        directory_fd = _open_intent_directory(parent_fd, parent_path, directory_name)
        try:
            entries = frozenset(os.listdir(directory_fd))
            if not entries <= {"intent.json", "identity.json"}:
                raise TerminalStateError("committed cleanup directory contains foreign entries")
            if entries == {"intent.json"}:
                raise TerminalStateError(
                    "committed cleanup identity-first prefix is unreachable and unsafe"
                )
            intent_raw: Mapping[str, Any] | None = None
            payload: dict[str, Any] | None = None
            if "intent.json" in entries:
                _, observed_intent_identity, intent_raw = _read_json_at(
                    directory_fd,
                    name="intent.json",
                    path=parent_path / directory_name / "intent.json",
                    max_bytes=MAX_INTENT_STATE_BYTES,
                    require_mode=0o600,
                )
                if _identity_document(observed_intent_identity) != _identity_document(intent_identity):
                    raise TerminalStateError("committed cleanup intent survivor identity changed")
                intent = _require_mapping(intent_raw, "terminal publication intent")
                _require_exact_keys(
                    intent, {"output_path", "expected", "payload"}, "terminal publication intent"
                )
                if (
                    intent["output_path"] != str(path)
                    or _validate_identity_document(intent["expected"]) != stored_expected
                ):
                    raise TerminalStateError("committed cleanup intent binding differs")
                payload = _validate_failure_payload(intent["payload"])
                if _sha256(_canonical(payload)) != state["failure_payload_sha256"]:
                    raise TerminalStateError("committed cleanup intent payload digest differs")
                if not _payload_context_matches(_intent_context_from_terminal(payload), context):
                    raise TerminalStateError("committed cleanup intent context differs")
                _reject_secrets(intent, "terminal publication intent")
            if "identity.json" in entries:
                _, observed_identity_identity, sidecar_raw = _read_json_at(
                    directory_fd,
                    name="identity.json",
                    path=parent_path / directory_name / "identity.json",
                    max_bytes=MAX_INTENT_STATE_BYTES,
                    require_mode=0o600,
                )
                if _identity_document(observed_identity_identity) != _identity_document(identity_identity):
                    raise TerminalStateError("committed cleanup sidecar survivor identity changed")
                _validate_intent_sidecar(
                    _require_mapping(sidecar_raw, "terminal intent identity sidecar"),
                    path=path,
                    gate_fd=gate_fd,
                    state=state,
                    intent_identity=intent_identity,
                    identity_identity=identity_identity,
                    payload_digest=str(state["failure_payload_sha256"]),
                    context=context,
                )
        finally:
            os.close(directory_fd)
        return {
            "state": state,
            "directory_name": directory_name,
            "entries": entries,
            "intent_identity": intent_identity,
            "identity_identity": identity_identity,
            "expected": stored_expected,
            "payload": payload,
            "context": context,
        }

    directory_fd = _open_intent_directory(parent_fd, parent_path, str(directory_name))
    try:
        if set(os.listdir(directory_fd)) != {"intent.json", "identity.json"}:
            raise TerminalStateError("terminal intent directory contents differ")
        _, intent_identity, intent_raw = _read_json_at(
            directory_fd,
            name="intent.json",
            path=parent_path / str(directory_name) / "intent.json",
            max_bytes=MAX_INTENT_STATE_BYTES,
            require_mode=0o600,
        )
        _, identity_identity, sidecar_raw = _read_json_at(
            directory_fd,
            name="identity.json",
            path=parent_path / str(directory_name) / "identity.json",
            max_bytes=MAX_INTENT_STATE_BYTES,
            require_mode=0o600,
        )
    finally:
        os.close(directory_fd)
    intent = _require_mapping(intent_raw, "terminal publication intent")
    _require_exact_keys(intent, {"output_path", "expected", "payload"}, "terminal publication intent")
    stored_expected = _validate_identity_document(intent["expected"])
    if intent["output_path"] != str(path) or (
        expected is not _ANY_EXPECTED_IDENTITY and stored_expected != _identity_document(expected)
    ):
        raise TerminalStateError("terminal intent output/expected identity differs")
    payload = _validate_failure_payload(intent["payload"])
    context = _validate_intent_context(
        _require_mapping(sidecar_raw, "terminal intent identity sidecar")["context"]
    )
    payload_digest = _sha256(_canonical(payload))
    if not _payload_context_matches(_intent_context_from_terminal(payload), context):
        raise TerminalStateError("terminal intent identity payload/context differs")
    _validate_intent_sidecar(
        _require_mapping(sidecar_raw, "terminal intent identity sidecar"),
        path=path,
        gate_fd=gate_fd,
        state=state,
        intent_identity=intent_identity,
        identity_identity=identity_identity,
        payload_digest=payload_digest,
        context=context,
    )
    if payload["provenance_state"] == "unavailable":
        failure_context = _require_mapping(payload["failure_context"], "terminal failure context")
        if failure_context["expected_output"] != stored_expected:
            raise TerminalStateError("terminal unavailable intent expected identity differs")
    _reject_secrets(intent, "terminal publication intent")
    return {
        "state": state,
        "directory_name": directory_name,
        "entries": frozenset({"intent.json", "identity.json"}),
        "intent_identity": intent_identity,
        "identity_identity": identity_identity,
        "expected": stored_expected,
        "payload": payload,
        "context": context,
    }

def _create_pending_intent_locked(
    path: Path,
    *,
    gate_fd: int,
    parent_fd: int,
    expected: FileIdentity | None,
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    if _read_gate_state(parent_fd, path)["state"] != "idle":
        raise TerminalStateError("terminal intent gate is already pending")
    validated_payload = _validate_failure_payload(payload)
    validated_context = _validate_intent_context(context)
    if not _payload_context_matches(_intent_context_from_terminal(validated_payload), validated_context):
        raise TerminalStateError("terminal failure payload/context differs")
    root_path = _terminal_intent_root_path(path)
    parent_path = root_path.parent
    os.mkdir(root_path.name, 0o700, dir_fd=parent_fd)
    directory_info = os.stat(root_path.name, dir_fd=parent_fd, follow_symlinks=False)
    directory_identity = (directory_info.st_dev, directory_info.st_ino)
    directory_fd = _open_intent_directory(parent_fd, parent_path, root_path.name)
    created: dict[str, FileIdentity] = {}
    gate_pending = False
    try:
        intent = {"output_path": str(path), "expected": _identity_document(expected), "payload": validated_payload}
        intent_identity = _create_json_file_at(
            directory_fd, name="intent.json", path=_terminal_intent_path(path), raw=_canonical(intent)
        )
        created["intent.json"] = intent_identity
        payload_digest = _sha256(_canonical(validated_payload))
        sidecar = {
            "schema_version": INTENT_STATE_SCHEMA_VERSION,
            "output_path": str(path),
            "gate": _gate_identity_document(os.fstat(gate_fd)),
            "intent": _identity_document(intent_identity),
            "failure_payload_sha256": payload_digest,
            "context": validated_context,
        }
        identity_identity = _create_json_file_at(
            directory_fd,
            name="identity.json",
            path=_terminal_intent_identity_path(path),
            raw=_canonical(sidecar),
        )
        created["identity.json"] = identity_identity
        os.fsync(directory_fd)
        gate_state = {
            "schema_version": INTENT_STATE_SCHEMA_VERSION,
            "state": "pending",
            "intent_directory": root_path.name,
            "output_path": str(path),
            "intent": _identity_document(intent_identity),
            "identity": _identity_document(identity_identity),
            "failure_payload_sha256": payload_digest,
            "context": validated_context,
        }
        # The rename can land even when the parent fsync behind it fails, so the
        # rollback must assume the gate may already be pending from here on.
        gate_pending = True
        _write_gate_state(parent_fd, path, gate_state)
    except Exception:
        if gate_pending:
            try:
                _write_gate_state(
                    parent_fd, path, {"schema_version": INTENT_STATE_SCHEMA_VERSION, "state": "idle"}
                )
            except (OSError, TerminalStateError):
                pass
        try:
            current_directory = os.stat(root_path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (current_directory.st_dev, current_directory.st_ino) == directory_identity:
                for name, identity in created.items():
                    try:
                        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if (current.st_dev, current.st_ino, current.st_size) == (
                        identity.device,
                        identity.inode,
                        identity.size,
                    ):
                        os.unlink(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
                os.rmdir(root_path.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(directory_fd)
    pending = _load_pending_intent_locked(path, gate_fd=gate_fd, parent_fd=parent_fd, expected=expected)
    if pending is None:
        raise TerminalStateError("terminal intent disappeared after durable creation")
    return pending


def _verify_committed_survivor(
    path: Path,
    *,
    gate_fd: int,
    directory_fd: int,
    directory_name: str,
    pending: Mapping[str, Any],
    name: str,
) -> None:
    identity_key = "intent_identity" if name == "intent.json" else "identity_identity"
    _, observed_identity, value = _read_json_at(
        directory_fd,
        name=name,
        path=path.parent / directory_name / name,
        max_bytes=MAX_INTENT_STATE_BYTES,
        require_mode=0o600,
    )
    saved_identity = pending[identity_key]
    if _identity_document(observed_identity) != _identity_document(saved_identity):
        raise TerminalStateError(f"committed cleanup {name} survivor identity changed")
    state = _require_mapping(pending["state"], "committed cleanup gate state")
    if name == "intent.json":
        intent = _require_mapping(value, "committed cleanup intent")
        _require_exact_keys(intent, {"output_path", "expected", "payload"}, "committed cleanup intent")
        payload = _validate_failure_payload(intent["payload"])
        if (
            intent["output_path"] != str(path)
            or _validate_identity_document(intent["expected"]) != pending["expected"]
            or _sha256(_canonical(payload)) != state["failure_payload_sha256"]
            or not _payload_context_matches(_intent_context_from_terminal(payload), pending["context"])
        ):
            raise TerminalStateError("committed cleanup intent survivor binding changed")
    else:
        _validate_intent_sidecar(
            _require_mapping(value, "committed cleanup identity sidecar"),
            path=path,
            gate_fd=gate_fd,
            state=state,
            intent_identity=pending["intent_identity"],
            identity_identity=pending["identity_identity"],
            payload_digest=str(state["failure_payload_sha256"]),
            context=pending["context"],
        )
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino, current.st_size) != (
        saved_identity.device,
        saved_identity.inode,
        saved_identity.size,
    ):
        raise TerminalStateError(f"committed cleanup {name} survivor changed before unlink")


def _recover_committed_cleanup_locked(
    path: Path,
    *,
    gate_fd: int,
    parent_fd: int,
    pending: Mapping[str, Any],
) -> None:
    state = _require_mapping(pending["state"], "committed cleanup gate state")
    if state.get("state") != "committed_cleanup":
        raise TerminalStateError("terminal cleanup recovery lacks a committed phase")
    saved_terminal = identity_from_document(
        path,
        _require_mapping(state["published_terminal"], "committed terminal identity"),
    )
    current_terminal = _terminal_identity_at(parent_fd, path)
    if current_terminal is None:
        raise TerminalStateError("committed cleanup terminal disappeared")
    if current_terminal != saved_terminal:
        _, current_document = _read_terminal_at(parent_fd, path)
        current_context = _intent_context_from_terminal(current_document)
        if current_context != pending["context"] and not _contexts_allow_reconcile(
            pending["context"],
            current_context,
            proposed_is_success=current_document.get("qualifies_task_4_5") is True,
        ):
            raise TerminalStateError("committed cleanup terminal changed without safe newer-wins provenance")

    parent_path = path.parent
    _fsync_directory_fd(parent_fd, parent_path, label="terminal intent parent")
    directory_name = pending["directory_name"]
    if directory_name is None:
        _write_gate_state(
            parent_fd, path, {"schema_version": INTENT_STATE_SCHEMA_VERSION, "state": "idle"}
        )
        return
    entries = frozenset(pending["entries"])
    if entries == {"intent.json"}:
        raise TerminalStateError("committed cleanup identity-first prefix is unreachable and unsafe")
    saved_directory_identity = _validate_gate_identity_document(
        state["consumed_directory_identity"]
    )
    current_directory = os.stat(str(directory_name), dir_fd=parent_fd, follow_symlinks=False)
    if _gate_identity_document(current_directory) != saved_directory_identity:
        raise TerminalStateError("committed cleanup directory identity changed before cleanup")
    directory_fd = _open_intent_directory(parent_fd, parent_path, str(directory_name))
    opened_directory_identity: dict[str, int] | None = None
    try:
        if "intent.json" in entries:
            _verify_committed_survivor(
                path,
                gate_fd=gate_fd,
                directory_fd=directory_fd,
                directory_name=str(directory_name),
                pending=pending,
                name="intent.json",
            )
            os.unlink("intent.json", dir_fd=directory_fd)
            os.fsync(directory_fd)
        if "identity.json" in entries:
            _verify_committed_survivor(
                path,
                gate_fd=gate_fd,
                directory_fd=directory_fd,
                directory_name=str(directory_name),
                pending=pending,
                name="identity.json",
            )
            os.unlink("identity.json", dir_fd=directory_fd)
            os.fsync(directory_fd)
        if os.listdir(directory_fd):
            raise TerminalStateError("committed cleanup directory is not empty after exact deletion")
        opened_directory_identity = _gate_identity_document(os.fstat(directory_fd))
        if opened_directory_identity != saved_directory_identity:
            raise TerminalStateError("committed cleanup opened directory identity changed")
    finally:
        os.close(directory_fd)
    current_directory = os.stat(str(directory_name), dir_fd=parent_fd, follow_symlinks=False)
    if (
        opened_directory_identity is None
        or _gate_identity_document(current_directory) != opened_directory_identity
    ):
        raise TerminalStateError("committed cleanup directory changed before removal")
    os.rmdir(str(directory_name), dir_fd=parent_fd)
    _fsync_directory_fd(parent_fd, parent_path, label="terminal intent parent")
    _write_gate_state(
        parent_fd, path, {"schema_version": INTENT_STATE_SCHEMA_VERSION, "state": "idle"}
    )


def _consume_pending_intent_locked(
    path: Path,
    *,
    gate_fd: int,
    parent_fd: int,
    pending: Mapping[str, Any],
    published_terminal: FileIdentity,
) -> None:
    state = dict(_require_mapping(pending["state"], "pending terminal gate state"))
    if state["state"] == "committed_cleanup":
        _recover_committed_cleanup_locked(
            path, gate_fd=gate_fd, parent_fd=parent_fd, pending=pending
        )
        return
    if _terminal_identity_at(parent_fd, path) != published_terminal:
        raise TerminalStateError("published terminal changed before committed cleanup")
    root_path = _terminal_intent_root_path(path)
    parent_path = root_path.parent
    directory_name = str(pending["directory_name"])
    if state["state"] == "pending":
        consumed_name = f"{root_path.name}.consumed-{uuid.uuid4().hex}"
        state = {**state, "state": "consuming", "consumed_directory": consumed_name}
        # The consuming state is durable before the rename, so a crash can never
        # strand a renamed directory behind an idle gate.
        _write_gate_state(parent_fd, path, state)
        os.rename(directory_name, consumed_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        _fsync_directory_fd(parent_fd, parent_path, label="terminal intent parent")
        directory_name = consumed_name
    elif directory_name == root_path.name:
        consumed_name = str(state["consumed_directory"])
        os.rename(directory_name, consumed_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        _fsync_directory_fd(parent_fd, parent_path, label="terminal intent parent")
        directory_name = consumed_name
    else:
        _fsync_directory_fd(parent_fd, parent_path, label="terminal intent parent")
    reloaded = _load_pending_intent_locked(path, gate_fd=gate_fd, parent_fd=parent_fd)
    if reloaded is None or (
        _identity_document(reloaded["intent_identity"]) != _identity_document(pending["intent_identity"])
        or _identity_document(reloaded["identity_identity"]) != _identity_document(pending["identity_identity"])
        or reloaded["payload"] is None
        or pending["payload"] is None
        or _canonical(reloaded["payload"]) != _canonical(pending["payload"])
        or reloaded["context"] != pending["context"]
    ):
        raise TerminalStateError("terminal intent changed before committed cleanup")
    committed_state = {
        **state,
        "state": "committed_cleanup",
        "consumed_directory": directory_name,
        "published_terminal": _identity_document(published_terminal),
        "expected": reloaded["expected"],
        "cleanup_entries": {
            "intent.json": _identity_document(reloaded["intent_identity"]),
            "identity.json": _identity_document(reloaded["identity_identity"]),
        },
        "consumed_directory_identity": _gate_identity_document(
            os.stat(directory_name, dir_fd=parent_fd, follow_symlinks=False)
        ),
    }
    _write_gate_state(parent_fd, path, committed_state)
    committed = _load_pending_intent_locked(path, gate_fd=gate_fd, parent_fd=parent_fd)
    if committed is None or committed["state"]["state"] != "committed_cleanup":
        raise TerminalStateError("committed cleanup phase disappeared")
    _recover_committed_cleanup_locked(
        path, gate_fd=gate_fd, parent_fd=parent_fd, pending=committed
    )


def _recover_cleanup_if_possible_locked(
    path: Path,
    *,
    gate_fd: int,
    parent_fd: int,
    pending: Mapping[str, Any],
) -> dict[str, Any] | None:
    if pending["state"]["state"] == "committed_cleanup":
        _recover_committed_cleanup_locked(
            path, gate_fd=gate_fd, parent_fd=parent_fd, pending=pending
        )
        return _load_pending_intent_locked(path, gate_fd=gate_fd, parent_fd=parent_fd)
    current = _terminal_identity_at(parent_fd, path)
    if current is None or _identity_document(current) == pending["expected"]:
        return pending
    _, current_document = _read_terminal_at(parent_fd, path)
    if not _safe_newer_terminal(current_document, pending):
        return pending
    _consume_pending_intent_locked(
        path,
        gate_fd=gate_fd,
        parent_fd=parent_fd,
        pending=pending,
        published_terminal=current,
    )
    return _load_pending_intent_locked(path, gate_fd=gate_fd, parent_fd=parent_fd)

def _open_terminal_lock(path: Path, *, parent_fd: int) -> int:
    lock_name = _terminal_lock_path(path).name
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    created = False
    try:
        lock_fd = os.open(lock_name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        lock_fd = os.open(lock_name, flags, dir_fd=parent_fd)
    info = os.fstat(lock_fd)
    current = os.stat(lock_name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
    ):
        os.close(lock_fd)
        raise TerminalStateError("terminal publication lock identity/mode differs")
    if created:
        try:
            os.fsync(lock_fd)
            os.fsync(parent_fd)
        except OSError:
            os.close(lock_fd)
            raise
    return lock_fd


def _terminal_identity_at(parent_fd: int, path: Path) -> FileIdentity | None:
    try:
        _, identity = _read_identity_at(
            parent_fd, name=path.name, path=path, max_bytes=MAX_TERMINAL_BYTES
        )
    except FileNotFoundError:
        return None
    return identity


def _atomic_replace_terminal_at(
    parent_fd: int,
    parent_path: Path,
    path: Path,
    raw: bytes,
    *,
    expected: FileIdentity | None,
) -> FileIdentity:
    temp_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(
        temp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=parent_fd,
    )
    replaced = False
    try:
        _write_all(fd, raw)
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        _revalidate_directory_fd(parent_fd, parent_path, label="terminal parent")
        try:
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
            raise TerminalStateError("terminal target changed to an unsafe file")
        if _terminal_identity_at(parent_fd, path) != expected:
            raise TerminalStateError("terminal output changed immediately before atomic replacement")
        os.replace(temp_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        replaced = True
        _fsync_directory_fd(parent_fd, parent_path, label="terminal parent")
        identity = _terminal_identity_at(parent_fd, path)
        if identity is None or identity.sha256 != _sha256(raw) or identity.size != len(raw):
            raise TerminalStateError("published terminal identity differs")
        return identity
    finally:
        if fd >= 0:
            os.close(fd)
        if not replaced:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _contexts_allow_reconcile(
    stored: Mapping[str, Any], proposed: Mapping[str, Any], *, proposed_is_success: bool
) -> bool:
    stored_context = _validate_intent_context(stored)
    proposed_context = _validate_intent_context(proposed)
    if stored_context == proposed_context:
        return proposed_is_success
    if stored_context["provenance_state"] == "unavailable":
        if proposed_context["provenance_state"] != "bound":
            return False
        trusted = stored_context["verifier_head_sha"]
        return trusted is None or proposed_context["verifier_head_sha"] in {None, trusted}
    if proposed_context["provenance_state"] != "bound":
        return False
    return (
        stored_context["run_id"] == proposed_context["run_id"]
        and stored_context["mutation_head_sha"] == proposed_context["mutation_head_sha"]
        and (
            stored_context["verifier_head_sha"] is None
            or proposed_context["verifier_head_sha"] is None
            or stored_context["verifier_head_sha"] == proposed_context["verifier_head_sha"]
        )
    )


def _safe_newer_terminal(
    current: Mapping[str, Any], pending: Mapping[str, Any]
) -> bool:
    if _canonical(current) == _canonical(pending["payload"]):
        return True
    current_context = _intent_context_from_terminal(current)
    return _contexts_allow_reconcile(
        pending["context"],
        current_context,
        proposed_is_success=current.get("qualifies_task_4_5") is True,
    )


def _read_terminal_at(parent_fd: int, path: Path) -> tuple[FileIdentity, dict[str, Any]]:
    raw, identity, value = _read_json_at(
        parent_fd, name=path.name, path=path, max_bytes=MAX_TERMINAL_BYTES
    )
    document = validate_terminal_document(value)
    if raw != _canonical(document):
        raise TerminalStateError("terminal is not canonical JSON")
    return identity, document


def read_authoritative_terminal(
    path: Path,
    *,
    max_bytes: int = MAX_TERMINAL_BYTES,
    deadline_monotonic: float | None = None,
) -> Mapping[str, Any]:
    deadline = deadline_monotonic or (time.monotonic() + DEFAULT_LOCK_TIMEOUT_SECONDS)
    with _locked_intent_gate(path, label="terminal authoritative-read intent gate", deadline_monotonic=deadline) as (
        gate_fd,
        parent_fd,
    ):
        pending = _load_pending_intent_locked(path, gate_fd=gate_fd, parent_fd=parent_fd)
        if pending is not None:
            pending = _recover_cleanup_if_possible_locked(
                path, gate_fd=gate_fd, parent_fd=parent_fd, pending=pending
            )
        if pending is not None:
            raise TerminalStateError("terminal is not authoritative while failure intent is pending")
        lock_fd = _open_terminal_lock(path, parent_fd=parent_fd)
        try:
            acquire_exclusive_flock_until(
                lock_fd, deadline_monotonic=deadline, label="terminal authoritative read"
            )
            raw, _, value = _read_json_at(
                parent_fd, name=path.name, path=path, max_bytes=max_bytes
            )
            document = validate_terminal_document(value)
            if raw != _canonical(document):
                raise TerminalStateError("authoritative terminal is not canonical JSON")
            return document
        except BoundedEvidenceError as error:
            raise TerminalStateError(str(error)) from error
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def publish_terminal_cas(
    path: Path,
    payload: bytes,
    expected: FileIdentity | None,
    *,
    intent_context: Mapping[str, Any] | None = None,
    deadline_monotonic: float | None = None,
) -> FileIdentity:
    try:
        proposed_raw = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise TerminalStateError("proposed terminal is not JSON") from error
    proposed = validate_terminal_document(proposed_raw)
    if payload != _canonical(proposed):
        raise TerminalStateError("proposed terminal is not canonical JSON")
    proposed_is_failure = proposed.get("outcome") == "failed"
    proposed_context = _intent_context_from_terminal(proposed)
    if intent_context is not None and _validate_intent_context(intent_context) != proposed_context:
        raise TerminalStateError("proposed terminal intent context differs")
    deadline = deadline_monotonic or (time.monotonic() + DEFAULT_LOCK_TIMEOUT_SECONDS)
    with _locked_intent_gate(path, label="terminal publication intent gate", deadline_monotonic=deadline) as (
        gate_fd,
        parent_fd,
    ):
        pending = _load_pending_intent_locked(path, gate_fd=gate_fd, parent_fd=parent_fd)
        if pending is not None and pending["state"]["state"] == "committed_cleanup":
            _recover_committed_cleanup_locked(
                path, gate_fd=gate_fd, parent_fd=parent_fd, pending=pending
            )
            pending = _load_pending_intent_locked(path, gate_fd=gate_fd, parent_fd=parent_fd)
        current_before_lock = _terminal_identity_at(parent_fd, path)
        if current_before_lock != expected:
            if pending is not None and current_before_lock is not None:
                _, current_document = _read_terminal_at(parent_fd, path)
                if _safe_newer_terminal(current_document, pending):
                    _consume_pending_intent_locked(
                        path,
                        gate_fd=gate_fd,
                        parent_fd=parent_fd,
                        pending=pending,
                        published_terminal=current_before_lock,
                    )
                    idempotent_failure_retry = (
                        proposed_is_failure
                        and current_document == pending["payload"]
                        and proposed_context == pending["context"]
                        and current_document.get("failure") == proposed.get("failure")
                    )
                    if current_document == proposed or idempotent_failure_retry:
                        return current_before_lock
            elif current_before_lock is not None:
                _, current_document = _read_terminal_at(parent_fd, path)
                if current_document == proposed:
                    return current_before_lock
            raise TerminalStateError("terminal output changed after its CAS identity was frozen")
        if pending is None:
            intent_payload = proposed
            if not proposed_is_failure:
                intent_payload = _failure_payload(
                    stage="success-publication-indeterminate",
                    expected_output=expected,
                    context=proposed_context,
                    mutation_state="indeterminate",
                )
            pending = _create_pending_intent_locked(
                path,
                gate_fd=gate_fd,
                parent_fd=parent_fd,
                expected=expected,
                payload=intent_payload,
                context=proposed_context,
            )
        elif pending is not None:
            if pending["expected"] != _identity_document(expected):
                current_identity, current_document = _read_terminal_at(parent_fd, path)
                if (
                    current_identity == expected
                    and current_document == proposed
                    and _safe_newer_terminal(current_document, pending)
                ):
                    _consume_pending_intent_locked(
                        path,
                        gate_fd=gate_fd,
                        parent_fd=parent_fd,
                        pending=pending,
                        published_terminal=current_identity,
                    )
                    return current_identity
                raise TerminalStateError("pending terminal intent expected identity differs")
            exact_retry = (
                proposed_is_failure
                and pending["context"] == proposed_context
                and _canonical(pending["payload"]) == payload
            )
            if proposed_is_failure and pending["context"] == proposed_context and not exact_retry:
                pending_failure = _require_mapping(pending["payload"]["failure"], "pending terminal failure")
                proposed_failure = _require_mapping(proposed["failure"], "proposed terminal failure")
                if pending_failure == proposed_failure:
                    proposed = dict(pending["payload"])
                    payload = _canonical(proposed)
                    exact_retry = True
            if not exact_retry and not _contexts_allow_reconcile(
                pending["context"],
                proposed_context,
                proposed_is_success=not proposed_is_failure,
            ):
                raise TerminalStateError("pending terminal intent cannot reconcile this publisher")
        lock_fd = _open_terminal_lock(path, parent_fd=parent_fd)
        try:
            acquire_exclusive_flock_until(
                lock_fd, deadline_monotonic=deadline, label="terminal evidence publication"
            )
            current = _terminal_identity_at(parent_fd, path)
            if current != expected:
                raise TerminalStateError("terminal output changed during its CAS publication")
            published = _atomic_replace_terminal_at(
                parent_fd, path.parent, path, payload, expected=expected
            )
            if pending is not None:
                _consume_pending_intent_locked(
                    path,
                    gate_fd=gate_fd,
                    parent_fd=parent_fd,
                    pending=pending,
                    published_terminal=published,
                )
            return published
        except BoundedEvidenceError as error:
            raise TerminalStateError(str(error)) from error
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def publish_unavailable_failure(
    path: Path,
    *,
    stage: str,
    expected: FileIdentity | None,
    verifier_head_sha: str | None,
    deadline_monotonic: float | None = None,
) -> bool:
    payload, context = unavailable_failure_payload(
        stage=stage, expected_output=expected, verifier_head_sha=verifier_head_sha
    )
    try:
        publish_terminal_cas(
            path,
            _canonical(payload),
            expected,
            intent_context=context,
            deadline_monotonic=deadline_monotonic,
        )
    except TerminalStateError as error:
        _LOGGER.warning(
            "terminal unavailable-failure publication refused for %s at stage %s: %s",
            path,
            stage,
            error,
        )
        return False
    return True


def publish_bound_failure(
    path: Path,
    *,
    stage: str,
    expected: FileIdentity,
    run_id: str,
    mutation_head_sha: str,
    possible_mutation: bool,
    deadline_monotonic: float | None = None,
) -> bool:
    payload, context = bound_failure_payload(
        stage=stage,
        expected_output=expected,
        run_id=run_id,
        mutation_head_sha=mutation_head_sha,
        possible_mutation=possible_mutation,
    )
    try:
        publish_terminal_cas(
            path,
            _canonical(payload),
            expected,
            intent_context=context,
            deadline_monotonic=deadline_monotonic,
        )
    except TerminalStateError as error:
        _LOGGER.warning(
            "terminal bound-failure publication refused for %s at stage %s (run %s): %s",
            path,
            stage,
            run_id,
            error,
        )
        return False
    return True


def terminal_is_authoritative(path: Path, *, deadline_monotonic: float | None = None) -> bool:
    try:
        read_authoritative_terminal(path, deadline_monotonic=deadline_monotonic)
    except TerminalStateError:
        return False
    return True
