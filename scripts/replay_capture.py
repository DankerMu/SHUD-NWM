#!/usr/bin/env python3
"""Evidence-capture primitives for the six-basin replay driver (#1164 change 2).

Split out of :mod:`scripts.replay_driver` so the driver module stays orchestration
only.  Everything here is read-only and three-way: ``present`` / ``absent`` /
``undeterminable`` are distinct outcomes and an uncompletable probe is never
reported as a completed negative.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packages.common.safe_fs import (
    SafeFilesystemError,
    list_directory_no_follow_limited,
    read_bytes_limited_no_follow,
    stat_no_follow,
)
from packages.common.source_identity import normalize_source_id

RESET_RECEIPT_SCHEMA_VERSION = "nhms.replay_state_scope_reset.v1"

MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_RECEIPT_BYTES = 32 * 1024 * 1024
MAX_DIGEST_FILES = 20_000
MAX_DIGEST_FILE_BYTES = 512 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 20_000

PROBE_PRESENT = "present"
PROBE_ABSENT = "absent"
PROBE_UNDETERMINABLE = "undeterminable"


class ReplayDriverRefused(RuntimeError):
    """Refusal decided before anything was submitted: exit 2."""

    def __init__(self, reason: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": "refused", "refusal_reason": self.reason, **self.details}


# ---------------------------------------------------------------------------
# digests and probes
# ---------------------------------------------------------------------------


def _sha256_file(path: Path, *, containment_root: Path | None = None) -> str:
    content = read_bytes_limited_no_follow(
        path,
        max_bytes=MAX_DIGEST_FILE_BYTES,
        containment_root=containment_root,
    )
    return hashlib.sha256(content).hexdigest()


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            names = list_directory_no_follow_limited(current, max_entries=MAX_DIRECTORY_ENTRIES)
        except (FileNotFoundError, OSError, SafeFilesystemError) as error:
            raise _DigestUndeterminable(str(error)) from error
        for name in sorted(names):
            path = current / name
            try:
                metadata = stat_no_follow(path)
            except (FileNotFoundError, OSError, SafeFilesystemError) as error:
                raise _DigestUndeterminable(str(error)) from error
            if stat.S_ISDIR(metadata.st_mode):
                stack.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                files.append(path)
            if len(files) > MAX_DIGEST_FILES:
                raise _DigestUndeterminable("directory fan-out exceeds the digest bound")
    return sorted(files)


class _DigestUndeterminable(RuntimeError):
    pass


def directory_digest(root: Path | None, *, label: str) -> dict[str, Any]:
    """Three-way content digest of a directory tree (sorted relpath + sha256)."""

    record: dict[str, Any] = {
        "label": label,
        "path": str(root) if root is not None else None,
        "status": PROBE_UNDETERMINABLE,
        "file_count": None,
        "total_bytes": None,
        "sha256": None,
        "detail": None,
    }
    if root is None:
        record["status"] = PROBE_ABSENT
        record["detail"] = "path not resolved"
        return record
    try:
        metadata = stat_no_follow(root)
    except FileNotFoundError:
        record["status"] = PROBE_ABSENT
        record["detail"] = "directory absent"
        return record
    except (OSError, SafeFilesystemError) as error:
        record["detail"] = f"stat failed: {error}"
        return record
    if not stat.S_ISDIR(metadata.st_mode):
        record["detail"] = "path is not a directory"
        return record
    try:
        files = _iter_files(root)
        digest = hashlib.sha256()
        total = 0
        for path in files:
            relative = path.relative_to(root).as_posix()
            content = read_bytes_limited_no_follow(path, max_bytes=MAX_DIGEST_FILE_BYTES)
            total += len(content)
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(content).hexdigest().encode("ascii"))
            digest.update(b"\n")
    except (_DigestUndeterminable, OSError, SafeFilesystemError) as error:
        record["detail"] = f"digest failed: {error}"
        return record
    record["status"] = PROBE_PRESENT
    record["file_count"] = len(files)
    record["total_bytes"] = total
    record["sha256"] = digest.hexdigest()
    return record


def _read_json(path: Path, *, max_bytes: int) -> Mapping[str, Any] | None:
    try:
        content = read_bytes_limited_no_follow(path, max_bytes=max_bytes)
    except (FileNotFoundError, OSError, SafeFilesystemError):
        return None
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _object_key(uri_or_key: str, object_store_prefix: str) -> str:
    candidate = str(uri_or_key).strip()
    prefix = (object_store_prefix or "").rstrip("/")
    if prefix and candidate.startswith(prefix + "/"):
        candidate = candidate[len(prefix) + 1 :]
    elif candidate.startswith("s3://"):
        candidate = urlparse(candidate).path.strip("/")
    return candidate.strip("/")


# ---------------------------------------------------------------------------
# run tree helpers
# ---------------------------------------------------------------------------


def run_id_for(source_id: str, cycle_time: datetime, model_id: str) -> str:
    return f"fcst_{normalize_source_id(source_id).lower()}_{cycle_time.strftime('%Y%m%d%H')}_{model_id}"


def _run_root(object_store_root: Path, run_id: str) -> Path:
    return object_store_root / "runs" / run_id


def run_capture(object_store_root: Path, run_id: str) -> dict[str, Any]:
    """Manifest sha256 + output inventory digests of one run tree."""

    root = _run_root(object_store_root, run_id)
    manifest_path = root / "input" / "manifest.json"
    capture: dict[str, Any] = {
        "run_id": run_id,
        "run_root": str(root),
        "manifest_sha256": None,
        "manifest": None,
        "output_inventory": directory_digest(root / "output", label="run_output"),
        "no_prior_run": False,
    }
    try:
        capture["manifest_sha256"] = _sha256_file(manifest_path)
    except (FileNotFoundError, OSError, SafeFilesystemError):
        capture["manifest_sha256"] = None
    manifest = _read_json(manifest_path, max_bytes=MAX_MANIFEST_BYTES)
    capture["manifest"] = manifest
    if manifest is None and capture["output_inventory"]["status"] == PROBE_ABSENT:
        capture["no_prior_run"] = True
    return capture


def manifest_summary(manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    if manifest is None:
        return {
            "state_id": None,
            "state_checksum": None,
            "init_mode": None,
            "quality": None,
            "packaged_ic_checksum": None,
            "river_network_version_id": None,
            "variable_keys": [],
        }
    initial_state = manifest.get("initial_state") if isinstance(manifest.get("initial_state"), Mapping) else {}
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), Mapping) else {}
    model = manifest.get("model") if isinstance(manifest.get("model"), Mapping) else {}
    outputs = manifest.get("outputs") if isinstance(manifest.get("outputs"), Mapping) else {}
    variables = outputs.get("variables")
    if isinstance(variables, Mapping):
        variable_keys = sorted(str(key) for key in variables)
    elif isinstance(variables, Sequence) and not isinstance(variables, str | bytes):
        variable_keys = sorted(str(item) for item in variables)
    else:
        variable_keys = []
    init_mode = runtime.get("init_mode")
    return {
        "state_id": initial_state.get("state_id"),
        "state_checksum": initial_state.get("checksum"),
        "init_mode": int(init_mode) if isinstance(init_mode, int) else None,
        "quality": initial_state.get("quality"),
        "packaged_ic_checksum": initial_state.get("packaged_ic_checksum"),
        "river_network_version_id": model.get("river_network_version_id"),
        "variable_keys": variable_keys,
    }


def key_consistency(prior: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """R3: a replay must not leave rows under a drifted key set behind.

    The parser deletes the union window, so window residue is not the risk;
    a changed ``river_network_version_id`` or variable key set is, because those
    rows are outside the delete predicate.
    """

    if prior.get("river_network_version_id") in (None, "") or not prior.get("variable_keys"):
        return {
            "status": PROBE_UNDETERMINABLE,
            "detail": "prior run evidence does not carry a comparable key set",
            "prior_river_network_version_id": prior.get("river_network_version_id"),
            "new_river_network_version_id": new.get("river_network_version_id"),
        }
    same_network = str(prior.get("river_network_version_id")) == str(new.get("river_network_version_id"))
    same_variables = list(prior.get("variable_keys") or []) == list(new.get("variable_keys") or [])
    return {
        "status": "consistent" if (same_network and same_variables) else "drift",
        "detail": None if (same_network and same_variables) else "river network version or variable key set changed",
        "prior_river_network_version_id": prior.get("river_network_version_id"),
        "new_river_network_version_id": new.get("river_network_version_id"),
        "prior_variable_keys": list(prior.get("variable_keys") or []),
        "new_variable_keys": list(new.get("variable_keys") or []),
    }


# ---------------------------------------------------------------------------
# reset receipt / state index
# ---------------------------------------------------------------------------


def load_reset_removed_entries(path: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Index the reset receipt's removed entries by (model, source, valid_time).

    This is the ONLY admissible source for the prior state fields: the scope was
    cleared before the replay started, so the live index is empty by
    construction and a "read it back" implementation would silently record
    nulls.
    """

    payload = _read_json(path, max_bytes=MAX_RECEIPT_BYTES)
    if payload is None:
        raise ReplayDriverRefused("reset_receipt_unreadable", {"path": str(path)})
    if payload.get("schema_version") != RESET_RECEIPT_SCHEMA_VERSION:
        raise ReplayDriverRefused("reset_receipt_schema_unsupported", {"path": str(path)})
    lanes = payload.get("lanes")
    if not isinstance(lanes, Sequence) or not lanes:
        raise ReplayDriverRefused("reset_receipt_lanes_missing", {"path": str(path)})
    resolved: dict[tuple[str, str, str], dict[str, Any]] = {}
    for lane in lanes:
        if not isinstance(lane, Mapping):
            continue
        for entry in lane.get("removed_entries") or []:
            if not isinstance(entry, Mapping):
                continue
            key = (
                str(entry.get("model_id") or ""),
                normalize_source_id(str(entry.get("source_id") or "")),
                str(entry.get("valid_time") or ""),
            )
            resolved.setdefault(
                key,
                {
                    "state_id": entry.get("state_id"),
                    "checksum": entry.get("checksum"),
                    "created_at": entry.get("created_at"),
                    "valid_time": entry.get("valid_time"),
                    "source": "reset_receipt",
                },
            )
    if not resolved:
        raise ReplayDriverRefused("reset_receipt_has_no_removed_entries", {"path": str(path)})
    return resolved


def read_state_index_entries(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path, max_bytes=MAX_INDEX_BYTES)
    if payload is None:
        raise ReplayDriverRefused("state_index_unreadable", {"path": str(path)})
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ReplayDriverRefused("state_index_entries_invalid", {"path": str(path)})
    return [dict(entry) for entry in entries if isinstance(entry, Mapping)]


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def successor_state_entries(
    entries: Sequence[Mapping[str, Any]],
    *,
    source_id: str,
    model_ids: Sequence[str],
    valid_time: datetime,
    since: datetime | None,
) -> dict[str, dict[str, Any]]:
    """Fresh successor entries per model (``valid_time`` match + ``created_at`` gate)."""

    canonical_source = normalize_source_id(source_id)
    found: dict[str, dict[str, Any]] = {}
    for entry in entries:
        model_id = str(entry.get("model_id") or "")
        if model_id not in set(model_ids):
            continue
        if normalize_source_id(str(entry.get("source_id") or "")) != canonical_source:
            continue
        entry_valid_time = _parse_time(entry.get("valid_time"))
        if entry_valid_time is None or entry_valid_time != valid_time:
            continue
        if since is not None:
            created_at = _parse_time(entry.get("created_at"))
            if created_at is None or created_at <= since:
                continue
        found[model_id] = dict(entry)
    return found
