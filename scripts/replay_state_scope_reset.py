#!/usr/bin/env python3
"""Scoped, archived, dual-lane state-index reset for the six-basin replay (#1164).

The replay's first cycle can only take the packaged-IC bootstrap path when the
target ``(model_id, source)`` scopes hold NO usable state-index entry
(``state_manager.exists_any_generation`` accepts an entry at ANY valid time).
This operator tool removes exactly those entries from both index lanes -- the
scratch lane Slurm writes (``NHMS_SLURM_SCHEDULER_STATE_INDEX``) and the NFS
lane the control plane reads (``NHMS_SCHEDULER_STATE_INDEX``) -- after archiving
everything it is about to make unreachable.

Order and safety posture (design D4):

1. **Pre-refusals, zero writes**: the scheduler timer must be provably inactive
   (an undeterminable probe counts as ACTIVE), no journal lock file may be fresh,
   both lane indexes must be readable, and the archive root must be a writable
   directory with enough free space.
2. **Archive first**: byte snapshots of both lanes, the removed-entry list, and
   for every affected ``states/`` object a three-way stat, a sha256 and a byte
   archive.  A missing or unreadable object is recorded three-way and does NOT
   block the reset -- the index entry still goes, because an entry pointing at an
   unreadable object is exactly what must not survive into the replay.
3. **Remove scratch lane first, NFS lane second**: the NFS lane is the admission
   read surface, so a half-finished reset can only ever look like "bootstrap not
   yet reachable" (fail-closed), never like "bootstrap reachable while scratch
   still holds the old chain".
4. **Atomic write-back + read-back verify**.  A failed read-back is
   ``commit_uncertain`` (exit 3), NEVER a refusal: the lane may already hold the
   new bytes (#1190 invariant).

Dry run is the default and writes nothing at all; ``--enforce`` performs the
reset.  ``states/`` objects are never deleted (the replay overwrites them cycle
by cycle and the pre-image is archived), and entries outside the requested
scopes -- including the legacy ``basins_*_shud`` rows -- are left byte-identical.

Exit codes: ``0`` success (or dry run), ``2`` refusal with provably zero
mutation, ``3`` commit-uncertain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    list_directory_no_follow_limited,
    read_bytes_limited_no_follow,
    stat_no_follow,
)
from packages.common.source_identity import normalize_source_id
from packages.common.state_manager import (
    FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION,
    MAX_STATE_SNAPSHOT_INDEX_BYTES,
)
from packages.common.state_manager import (
    # Reused deliberately: a rewritten index must be serialized and checksummed
    # EXACTLY the way `publish_state_snapshot_index` does it, or every reader
    # would reject the result with `state_snapshot_index_checksum_mismatch`.
    _canonical_json_bytes as canonical_index_bytes,
)
from packages.common.state_manager import (
    _payload_checksum as index_payload_checksum,
)

SCHEMA_VERSION = "nhms.replay_state_scope_reset.v1"
_ROOT = Path(__file__).resolve().parents[1]
RECEIPT_SCHEMA_PATH = _ROOT / "schemas/replay_state_scope_reset_receipt.schema.json"

NFS_LANE_ENV = "NHMS_SCHEDULER_STATE_INDEX"
SCRATCH_LANE_ENV = "NHMS_SLURM_SCHEDULER_STATE_INDEX"
ARCHIVE_ROOT_ENV = "NHMS_REPLAY_ARCHIVE_ROOT"
JOURNAL_ROOT_ENV = "NHMS_SCHEDULER_JOURNAL_ROOT"
OBJECT_STORE_ROOT_ENV = "OBJECT_STORE_ROOT"
OBJECT_STORE_PREFIX_ENV = "OBJECT_STORE_PREFIX"

TIMER_UNIT = "nhms-compute-scheduler.timer"
#: A journal lock file touched inside this window counts as a live scheduler.
LOCK_FRESHNESS_SECONDS = 600
#: Free-space headroom over the measured archive payload.
ARCHIVE_FREE_SPACE_FACTOR = 2.0
MAX_JOURNAL_LOCK_ENTRIES = 4096
MAX_STATE_OBJECT_BYTES = 512 * 1024 * 1024
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
#: Lane order is load-bearing: scratch first, NFS (admission read surface) last.
LANE_ORDER = ("scratch", "nfs")

PROBE_PRESENT = "present"
PROBE_ABSENT = "absent"
PROBE_UNDETERMINABLE = "undeterminable"


class ResetRefused(RuntimeError):
    """A refusal decided before any byte was written: exit 2, zero mutation."""

    def __init__(self, reason: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": "refused", "refusal_reason": self.reason, **self.details}


class ResetCommitUncertain(RuntimeError):
    """A lane write may already stand: exit 3, never reported as a refusal."""

    def __init__(self, reason: str, *, receipt: Mapping[str, Any], details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.receipt: dict[str, Any] = dict(receipt)
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": "commit_uncertain", "commit_uncertain_reason": self.reason, **self.details}


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


def default_timer_probe(unit: str = TIMER_UNIT) -> tuple[str, str]:
    """Return ``(verdict, detail)`` for the scheduler timer.

    ``verdict`` is one of ``active`` / ``inactive`` / ``undeterminable``.  The
    caller treats ``undeterminable`` as ACTIVE: not knowing whether a scheduler
    pass may start mid-reset is not permission to reset.
    """

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["systemctl", "--user", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return PROBE_UNDETERMINABLE, f"timer probe failed: {error}"
    status = (completed.stdout or "").strip() or (completed.stderr or "").strip()
    if status in {"inactive", "failed", "dead"}:
        return "inactive", status
    if status in {"active", "activating", "reloading", "deactivating"}:
        return "active", status
    return PROBE_UNDETERMINABLE, f"unrecognised systemctl output: {status!r}"


def journal_lock_probe(journal_root: Path | None, *, now: float) -> dict[str, Any]:
    """Fresh journal lock files mean a scheduler pass is (or just was) running."""

    if journal_root is None:
        return {"status": PROBE_UNDETERMINABLE, "detail": f"{JOURNAL_ROOT_ENV} is not set"}
    lock_dir = journal_root / ".locks"
    try:
        names = list_directory_no_follow_limited(lock_dir, max_entries=MAX_JOURNAL_LOCK_ENTRIES)
    except FileNotFoundError:
        return {"status": PROBE_ABSENT, "lock_dir": str(lock_dir), "fresh_locks": []}
    except (OSError, SafeFilesystemError) as error:
        return {"status": PROBE_UNDETERMINABLE, "lock_dir": str(lock_dir), "detail": str(error)}
    fresh: list[dict[str, Any]] = []
    for name in sorted(names)[:MAX_JOURNAL_LOCK_ENTRIES]:
        path = lock_dir / name
        try:
            metadata = stat_no_follow(path)
        except FileNotFoundError:
            continue
        except (OSError, SafeFilesystemError) as error:
            return {"status": PROBE_UNDETERMINABLE, "lock_dir": str(lock_dir), "detail": str(error)}
        age_seconds = now - float(metadata.st_mtime)
        if age_seconds < LOCK_FRESHNESS_SECONDS:
            fresh.append({"name": name, "age_seconds": round(age_seconds, 3)})
    return {
        "status": PROBE_PRESENT if fresh else PROBE_ABSENT,
        "lock_dir": str(lock_dir),
        "fresh_locks": fresh,
        "freshness_seconds": LOCK_FRESHNESS_SECONDS,
    }


# ---------------------------------------------------------------------------
# index io
# ---------------------------------------------------------------------------


def _read_index(path: Path, *, lane: str) -> tuple[dict[str, Any], bytes]:
    try:
        content = read_bytes_limited_no_follow(path, max_bytes=MAX_STATE_SNAPSHOT_INDEX_BYTES)
    except FileNotFoundError as error:
        raise ResetRefused("lane_index_missing", {"lane": lane, "path": str(path)}) from error
    except (OSError, SafeFilesystemError) as error:
        raise ResetRefused(
            "lane_index_unreadable", {"lane": lane, "path": str(path), "error": str(error)}
        ) from error
    if len(content) > MAX_STATE_SNAPSHOT_INDEX_BYTES:
        raise ResetRefused("lane_index_too_large", {"lane": lane, "path": str(path)})
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ResetRefused("lane_index_malformed", {"lane": lane, "path": str(path)}) from error
    if not isinstance(payload, Mapping):
        raise ResetRefused("lane_index_not_object", {"lane": lane, "path": str(path)})
    entries = payload.get("entries")
    if not isinstance(entries, list) or not all(isinstance(entry, Mapping) for entry in entries):
        raise ResetRefused("lane_index_entries_invalid", {"lane": lane, "path": str(path)})
    return dict(payload), content


def _rewritten_index_bytes(payload: Mapping[str, Any], entries: Sequence[Mapping[str, Any]]) -> bytes:
    """Serialize the surviving entries exactly like the index publisher does."""

    rebuilt: dict[str, Any] = {
        "schema_version": payload.get("schema_version") or FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION,
        # The index carries a freshness contract; a removal IS a republication,
        # so the stamp moves.  Keeping the old stamp would leave readers that
        # enforce freshness rejecting a lane this tool just proved correct.
        "generated_at": _format_time(datetime.now(tz=UTC)),
        "entries": [dict(entry) for entry in entries],
    }
    rebuilt["checksum"] = f"sha256:{index_payload_checksum(rebuilt)}"
    return canonical_index_bytes(rebuilt, pretty=True)


def _entry_in_scope(entry: Mapping[str, Any], scopes: frozenset[tuple[str, str]]) -> bool:
    model_id = str(entry.get("model_id") or "")
    source_id = str(entry.get("source_id") or "")
    if not model_id or not source_id:
        return False
    return (model_id, normalize_source_id(source_id)) in scopes


# ---------------------------------------------------------------------------
# object archiving
# ---------------------------------------------------------------------------


def _object_key(uri_or_key: str, object_store_prefix: str) -> str:
    candidate = str(uri_or_key).strip()
    prefix = (object_store_prefix or "").rstrip("/")
    if prefix and candidate.startswith(prefix + "/"):
        candidate = candidate[len(prefix) + 1 :]
    elif candidate.startswith("s3://"):
        candidate = urlparse(candidate).path.strip("/")
    return candidate.strip("/")


def _safe_relative_key(key: str) -> Path | None:
    candidate = Path(key)
    if candidate.is_absolute() or any(part in ("..", "") for part in candidate.parts):
        return None
    return candidate


def _probe_state_object(
    entry: Mapping[str, Any],
    *,
    object_store_root: Path | None,
    object_store_prefix: str,
) -> dict[str, Any]:
    """Three-way stat + sha256 of one indexed ``states/`` object.

    ``absent`` and ``undeterminable`` are distinct on purpose: only the first is
    a completed negative.  Neither blocks the reset.
    """

    state_uri = str(entry.get("state_uri") or "")
    record: dict[str, Any] = {
        "state_id": str(entry.get("state_id") or ""),
        "model_id": str(entry.get("model_id") or ""),
        "source_id": str(entry.get("source_id") or ""),
        "valid_time": str(entry.get("valid_time") or ""),
        "state_uri": state_uri,
        "object_key": None,
        "stat_status": PROBE_UNDETERMINABLE,
        "size_bytes": None,
        "mtime": None,
        "sha256": None,
        "archived_path": None,
        "detail": None,
    }
    if object_store_root is None:
        record["detail"] = f"{OBJECT_STORE_ROOT_ENV} is not set"
        return record
    key = _safe_relative_key(_object_key(state_uri, object_store_prefix)) if state_uri else None
    if key is None:
        record["detail"] = "state uri does not resolve inside the object store"
        return record
    record["object_key"] = str(key)
    path = object_store_root / key
    try:
        metadata = stat_no_follow(path, containment_root=object_store_root)
    except FileNotFoundError:
        record["stat_status"] = PROBE_ABSENT
        record["detail"] = "object is absent"
        return record
    except (OSError, SafeFilesystemError) as error:
        record["detail"] = f"object stat failed: {error}"
        return record
    if not stat.S_ISREG(metadata.st_mode):
        record["detail"] = "object is not a regular file"
        return record
    record["stat_status"] = PROBE_PRESENT
    record["size_bytes"] = int(metadata.st_size)
    record["mtime"] = _format_time(datetime.fromtimestamp(metadata.st_mtime, tz=UTC))
    if int(metadata.st_size) > MAX_STATE_OBJECT_BYTES:
        record["stat_status"] = PROBE_UNDETERMINABLE
        record["detail"] = "object exceeds the archive size bound"
        return record
    try:
        content = read_bytes_limited_no_follow(
            path,
            max_bytes=MAX_STATE_OBJECT_BYTES,
            containment_root=object_store_root,
        )
    except (OSError, SafeFilesystemError) as error:
        record["stat_status"] = PROBE_UNDETERMINABLE
        record["detail"] = f"object read failed: {error}"
        return record
    record["sha256"] = hashlib.sha256(content).hexdigest()
    record["_content"] = content
    return record


# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------


def reset_state_scopes(
    *,
    scopes: Sequence[tuple[str, str]],
    enforce: bool,
    nfs_index: Path | None = None,
    scratch_index: Path | None = None,
    archive_root: Path | None = None,
    journal_root: Path | None = None,
    object_store_root: Path | None = None,
    object_store_prefix: str | None = None,
    timer_probe: Callable[[], tuple[str, str]] | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    started_at = datetime.now(tz=UTC)
    resolved_scopes = _normalized_scopes(scopes)
    lanes = {
        "nfs": _resolved_path(nfs_index, env=NFS_LANE_ENV, field="nfs_index"),
        "scratch": _resolved_path(scratch_index, env=SCRATCH_LANE_ENV, field="scratch_index"),
    }
    if lanes["nfs"] == lanes["scratch"]:
        raise ResetRefused(
            "lane_paths_identical",
            {"nfs_index": str(lanes["nfs"]), "scratch_index": str(lanes["scratch"])},
        )
    resolved_journal_root = _optional_path(journal_root, env=JOURNAL_ROOT_ENV)
    resolved_object_root = _optional_path(object_store_root, env=OBJECT_STORE_ROOT_ENV)
    resolved_prefix = (
        object_store_prefix if object_store_prefix is not None else os.getenv(OBJECT_STORE_PREFIX_ENV, "")
    )

    probe = timer_probe if timer_probe is not None else default_timer_probe
    timer_verdict, timer_detail = probe()
    preflight: dict[str, Any] = {"timer": {"verdict": timer_verdict, "detail": timer_detail, "unit": TIMER_UNIT}}
    if timer_verdict != "inactive":
        raise ResetRefused(
            "scheduler_timer_not_provably_inactive",
            {"timer": preflight["timer"]},
        )
    locks = journal_lock_probe(resolved_journal_root, now=now())
    preflight["journal_locks"] = locks
    if locks["status"] != PROBE_ABSENT:
        raise ResetRefused("journal_lock_activity", {"journal_locks": locks})

    lane_state: dict[str, dict[str, Any]] = {}
    for lane in LANE_ORDER:
        payload, content = _read_index(lanes[lane], lane=lane)
        entries = [dict(entry) for entry in payload["entries"]]
        removed = [entry for entry in entries if _entry_in_scope(entry, resolved_scopes)]
        kept = [entry for entry in entries if not _entry_in_scope(entry, resolved_scopes)]
        lane_state[lane] = {
            "payload": payload,
            "content": content,
            "entries": entries,
            "removed": removed,
            "kept": kept,
            "path": lanes[lane],
        }

    objects = _collect_objects(
        lane_state,
        object_store_root=resolved_object_root,
        object_store_prefix=resolved_prefix,
    )
    archive_payload_bytes = sum(int(item.get("size_bytes") or 0) for item in objects) + sum(
        len(state["content"]) for state in lane_state.values()
    )
    preflight["archive_root"] = _archive_root_preflight(
        archive_root,
        required_bytes=archive_payload_bytes,
        enforce=enforce,
    )

    archive_dir: Path | None = None
    if enforce:
        archive_dir = _create_archive_dir(Path(preflight["archive_root"]["path"]), started_at)

    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "outcome": "completed",
        "enforced": bool(enforce),
        "started_at": _format_time(started_at),
        "finished_at": None,
        "scopes": [{"model_id": model_id, "source_id": source_id} for model_id, source_id in sorted(resolved_scopes)],
        "archive_dir": str(archive_dir) if archive_dir is not None else None,
        "preflight": preflight,
        "lanes": [],
        "objects": [],
        "totals": {
            "removed_entries": 0,
            "objects_present": 0,
            "objects_absent": 0,
            "objects_undeterminable": 0,
            "archive_payload_bytes": archive_payload_bytes,
        },
        "commit_uncertain_reason": None,
    }

    if enforce and archive_dir is not None:
        _archive_lane_snapshots(archive_dir, lane_state)
        _archive_objects(archive_dir, objects)

    for item in objects:
        item.pop("_content", None)
        receipt["objects"].append(item)
        if item["stat_status"] == PROBE_PRESENT:
            receipt["totals"]["objects_present"] += 1
        elif item["stat_status"] == PROBE_ABSENT:
            receipt["totals"]["objects_absent"] += 1
        else:
            receipt["totals"]["objects_undeterminable"] += 1

    # Removal order is load-bearing (scratch first, NFS last).
    for lane in LANE_ORDER:
        state = lane_state[lane]
        lane_receipt: dict[str, Any] = {
            "lane": lane,
            "index_path": str(state["path"]),
            "index_sha256_before": hashlib.sha256(state["content"]).hexdigest(),
            "snapshot_path": str(archive_dir / f"{lane}-index-before.json") if archive_dir else None,
            "entry_count_before": len(state["entries"]),
            "entry_count_after": len(state["kept"]),
            "removed_count": len(state["removed"]),
            "removed_entries": [dict(entry) for entry in state["removed"]],
            "readback_verified": None,
            "readback_reason": None,
        }
        receipt["totals"]["removed_entries"] += len(state["removed"])
        if enforce and state["removed"]:
            content = _rewritten_index_bytes(state["payload"], state["kept"])
            try:
                atomic_write_bytes_no_follow(state["path"], content, mode=0o644)
            except (OSError, SafeFilesystemError) as error:
                receipt["lanes"].append(lane_receipt)
                receipt["finished_at"] = _format_time(datetime.now(tz=UTC))
                receipt["outcome"] = "commit_uncertain"
                receipt["commit_uncertain_reason"] = "lane_write_failed"
                lane_receipt["readback_verified"] = False
                lane_receipt["readback_reason"] = f"write failed: {error}"
                _write_receipt(archive_dir, receipt)
                raise ResetCommitUncertain(
                    "lane_write_failed",
                    receipt=receipt,
                    details={"lane": lane, "index_path": str(state["path"]), "error": str(error)},
                ) from error
            verified, reason = _verify_lane_readback(state["path"], expected=state["kept"], scopes=resolved_scopes)
            lane_receipt["readback_verified"] = verified
            lane_receipt["readback_reason"] = reason
            if not verified:
                receipt["lanes"].append(lane_receipt)
                receipt["finished_at"] = _format_time(datetime.now(tz=UTC))
                receipt["outcome"] = "commit_uncertain"
                receipt["commit_uncertain_reason"] = "lane_readback_failed"
                _write_receipt(archive_dir, receipt)
                # NOT a refusal: the atomic replace already ran, so the lane may
                # hold the new bytes even though the verification did not
                # complete (#1190 invariant).
                raise ResetCommitUncertain(
                    "lane_readback_failed",
                    receipt=receipt,
                    details={"lane": lane, "index_path": str(state["path"]), "error": reason},
                )
        receipt["lanes"].append(lane_receipt)

    receipt["finished_at"] = _format_time(datetime.now(tz=UTC))
    if enforce:
        _write_receipt(archive_dir, receipt)
    return receipt


def _collect_objects(
    lane_state: Mapping[str, Mapping[str, Any]],
    *,
    object_store_root: Path | None,
    object_store_prefix: str,
) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for lane in LANE_ORDER:
        for entry in lane_state[lane]["removed"]:
            key = (str(entry.get("state_id") or ""), str(entry.get("state_uri") or ""))
            record = seen.get(key)
            if record is None:
                record = _probe_state_object(
                    entry,
                    object_store_root=object_store_root,
                    object_store_prefix=object_store_prefix,
                )
                record["lanes"] = []
                seen[key] = record
            record["lanes"].append(lane)
    return [seen[key] for key in sorted(seen)]


def _verify_lane_readback(
    path: Path,
    *,
    expected: Sequence[Mapping[str, Any]],
    scopes: frozenset[tuple[str, str]],
) -> tuple[bool, str | None]:
    try:
        payload, _content = _read_index(path, lane="readback")
    except ResetRefused as error:
        return False, f"{error.reason}: {error.details}"
    entries = payload["entries"]
    if len(entries) != len(expected):
        return False, f"entry count {len(entries)} != expected {len(expected)}"
    if any(_entry_in_scope(entry, scopes) for entry in entries):
        return False, "in-scope entries still present after write-back"
    checksum = str(payload.get("checksum") or "")
    if checksum != f"sha256:{index_payload_checksum({k: v for k, v in payload.items() if k != 'checksum'})}":
        return False, "index checksum does not match the written payload"
    return True, None


# ---------------------------------------------------------------------------
# archive helpers
# ---------------------------------------------------------------------------


def _archive_root_preflight(
    archive_root: Path | None,
    *,
    required_bytes: int,
    enforce: bool,
) -> dict[str, Any]:
    root = archive_root if archive_root is not None else _env_path(ARCHIVE_ROOT_ENV)
    if root is None:
        raise ResetRefused("archive_root_unset", {"env": ARCHIVE_ROOT_ENV})
    root = root.expanduser()
    if not root.is_absolute():
        raise ResetRefused("archive_root_not_absolute", {"path": str(root)})
    try:
        metadata = os.lstat(root)
    except OSError as error:
        raise ResetRefused("archive_root_unavailable", {"path": str(root), "error": str(error)}) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ResetRefused("archive_root_not_directory", {"path": str(root)})
    if enforce and not os.access(root, os.W_OK | os.X_OK):
        raise ResetRefused("archive_root_not_writable", {"path": str(root)})
    try:
        usage = shutil.disk_usage(root)
    except OSError as error:
        raise ResetRefused("archive_root_space_undeterminable", {"path": str(root), "error": str(error)}) from error
    needed = int(required_bytes * ARCHIVE_FREE_SPACE_FACTOR)
    if usage.free < needed:
        raise ResetRefused(
            "archive_root_insufficient_space",
            {"path": str(root), "free_bytes": int(usage.free), "required_bytes": needed},
        )
    return {
        "path": str(root),
        "free_bytes": int(usage.free),
        "required_bytes": needed,
        "writable": bool(os.access(root, os.W_OK | os.X_OK)),
    }


def _create_archive_dir(root: Path, started_at: datetime) -> Path:
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    archive_dir = root / f"six-basin-replay-{stamp}"
    try:
        ensure_directory_no_follow(archive_dir, containment_root=root)
        os.chmod(archive_dir, 0o700, follow_symlinks=False)
    except (OSError, SafeFilesystemError) as error:
        raise ResetRefused("archive_dir_unwritable", {"path": str(archive_dir), "error": str(error)}) from error
    return archive_dir


def _archive_lane_snapshots(archive_dir: Path, lane_state: Mapping[str, Mapping[str, Any]]) -> None:
    for lane in LANE_ORDER:
        state = lane_state[lane]
        _archive_write(archive_dir / f"{lane}-index-before.json", state["content"])
        removed = json.dumps(
            {
                "lane": lane,
                "index_path": str(state["path"]),
                "removed_entries": [dict(entry) for entry in state["removed"]],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        _archive_write(archive_dir / f"{lane}-removed-entries.json", removed)


def _archive_objects(archive_dir: Path, objects: Sequence[dict[str, Any]]) -> None:
    objects_dir = archive_dir / "objects"
    try:
        ensure_directory_no_follow(objects_dir, containment_root=archive_dir)
    except (OSError, SafeFilesystemError) as error:
        raise ResetRefused("archive_dir_unwritable", {"path": str(objects_dir), "error": str(error)}) from error
    for item in objects:
        content = item.get("_content")
        if not isinstance(content, bytes):
            continue
        name = f"{item['state_id'] or 'unknown'}.bin".replace("/", "_")
        target = objects_dir / name
        _archive_write(target, content)
        item["archived_path"] = str(target)


def _archive_write(path: Path, content: bytes) -> None:
    try:
        atomic_write_bytes_no_follow(path, content, containment_root=path.parent, mode=0o600)
    except (OSError, SafeFilesystemError) as error:
        # Archiving happens BEFORE any lane write, so a failure here is still a
        # provably-zero-mutation refusal.
        raise ResetRefused("archive_write_failed", {"path": str(path), "error": str(error)}) from error


def _write_receipt(archive_dir: Path | None, receipt: Mapping[str, Any]) -> None:
    if archive_dir is None:
        return
    content = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if len(content) > MAX_RECEIPT_BYTES:
        content = json.dumps(
            {**dict(receipt), "objects": [], "lanes": [], "receipt_truncated": True},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
    try:
        atomic_write_bytes_no_follow(
            archive_dir / "reset-receipt.json",
            content,
            containment_root=archive_dir,
            mode=0o600,
        )
    except (OSError, SafeFilesystemError) as error:  # pragma: no cover - archive proven writable above
        print(f"receipt write failed: {error}", file=sys.stderr)


# ---------------------------------------------------------------------------
# argument / env plumbing
# ---------------------------------------------------------------------------


def _normalized_scopes(scopes: Sequence[tuple[str, str]]) -> frozenset[tuple[str, str]]:
    resolved = {
        (str(model_id).strip(), normalize_source_id(str(source_id).strip()))
        for model_id, source_id in scopes
        if str(model_id).strip() and str(source_id).strip()
    }
    if not resolved:
        raise ResetRefused("scopes_empty")
    return frozenset(resolved)


def _resolved_path(value: Path | None, *, env: str, field: str) -> Path:
    candidate = value if value is not None else _env_path(env)
    if candidate is None:
        raise ResetRefused("lane_index_unset", {"field": field, "env": env})
    path = candidate.expanduser()
    if not path.is_absolute():
        raise ResetRefused("lane_index_not_absolute", {"field": field, "path": str(path)})
    return path


def _optional_path(value: Path | None, *, env: str) -> Path | None:
    candidate = value if value is not None else _env_path(env)
    return candidate.expanduser() if candidate is not None else None


def _env_path(env: str) -> Path | None:
    raw = os.getenv(env, "").strip()
    return Path(raw) if raw else None


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Forecast source id, e.g. gfs or IFS.")
    parser.add_argument(
        "--model-id",
        action="append",
        required=True,
        dest="model_ids",
        help="Model id to reset (repeatable); every id is scoped to --source.",
    )
    parser.add_argument("--nfs-index", type=Path, default=None)
    parser.add_argument("--scratch-index", type=Path, default=None)
    parser.add_argument("--archive-root", type=Path, default=None)
    parser.add_argument("--journal-root", type=Path, default=None)
    parser.add_argument("--object-store-root", type=Path, default=None)
    parser.add_argument("--object-store-prefix", default=None)
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="Perform the reset; without it the run is a read-only preview.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        receipt = reset_state_scopes(
            scopes=[(model_id, args.source) for model_id in args.model_ids],
            enforce=bool(args.enforce),
            nfs_index=args.nfs_index,
            scratch_index=args.scratch_index,
            archive_root=args.archive_root,
            journal_root=args.journal_root,
            object_store_root=args.object_store_root,
            object_store_prefix=args.object_store_prefix,
        )
    except ResetCommitUncertain as error:
        print(json.dumps(error.receipt, ensure_ascii=False, sort_keys=True))
        print(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 3
    except ResetRefused as error:
        print(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
