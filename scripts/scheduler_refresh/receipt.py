"""Receipt construction, emergency-slot channel and primary receipt publication.

Split out of ``scripts/scheduler_file_provider_refresh.py`` by #1099; the
historical module remains the executable entrypoint and attribute facade.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.provider_atomic import provider_destination_lock
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    open_directory_no_follow,
    read_bytes_limited_no_follow,
)
from packages.scheduler.registry_audit import normalize_cutover_gate_audit
from scripts.scheduler_refresh.config import EmergencySlot
from scripts.scheduler_refresh.constants import (
    MAX_COLLECTION_ITEMS,
    MAX_HISTORY,
    MAX_ORPHAN_EVIDENCE,
    MAX_RECEIPT_BYTES,
    SCHEMA_VERSION,
    RefreshError,
)
from scripts.scheduler_refresh.receipt_validation import _parse_receipt_datetime, _receipt_bytes, _validate_receipt


def _reserve_emergency_slot(root: Path, run_id: str) -> EmergencySlot:
    ensure_directory_no_follow(root)
    path = root / f"{run_id}.reserved.json"
    parent_fd = open_directory_no_follow(root)
    fd = -1
    try:
        fd = os.open(
            path.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        os.fchmod(fd, 0o600)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("emergency slot is not regular")
        os.fsync(fd)
        os.fsync(parent_fd)
        return EmergencySlot(path, parent_fd, fd, opened.st_dev, opened.st_ino)
    except Exception:
        if fd >= 0:
            try:
                current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
                    os.unlink(path.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
            except OSError:
                pass
            os.close(fd)
        os.close(parent_fd)
        raise

def _finalize_emergency_slot(slot: EmergencySlot, receipt: Mapping[str, Any]) -> None:
    content = _receipt_bytes(_validate_receipt(receipt))
    completed = False
    try:
        _verify_emergency_slot(slot)
        os.fchmod(slot.file_fd, 0o600)
        os.ftruncate(slot.file_fd, 0)
        os.lseek(slot.file_fd, 0, os.SEEK_SET)
        remaining = memoryview(content)
        while remaining:
            written = os.write(slot.file_fd, remaining)
            if written <= 0:
                raise OSError("emergency receipt write made no progress")
            remaining = remaining[written:]
        os.fsync(slot.file_fd)
        if os.fstat(slot.file_fd).st_size != len(content):
            raise OSError("emergency receipt size mismatch")
        os.lseek(slot.file_fd, 0, os.SEEK_SET)
        verified = bytearray()
        while len(verified) < len(content):
            chunk = os.read(slot.file_fd, len(content) - len(verified))
            if not chunk:
                break
            verified.extend(chunk)
        if bytes(verified) != content:
            raise OSError("emergency receipt digest mismatch")
        os.fsync(slot.parent_fd)
        completed = True
    finally:
        if not completed:
            try:
                _verify_emergency_slot(slot)
                os.unlink(slot.path.name, dir_fd=slot.parent_fd)
                os.fsync(slot.parent_fd)
            except (OSError, RefreshError):
                pass
        os.close(slot.file_fd)
        os.close(slot.parent_fd)

def _verify_emergency_slot(slot: EmergencySlot) -> None:
    opened = os.fstat(slot.file_fd)
    current = os.stat(slot.path.name, dir_fd=slot.parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (slot.device, slot.inode)
        or (current.st_dev, current.st_ino) != (slot.device, slot.inode)
    ):
        raise RefreshError("receipt_channels_failed", outcome="replace_uncertain", phase="receipt")

def _discard_emergency_slot(slot: EmergencySlot) -> None:
    try:
        _verify_emergency_slot(slot)
        os.unlink(slot.path.name, dir_fd=slot.parent_fd)
        os.fsync(slot.parent_fd)
    finally:
        os.close(slot.file_fd)
        os.close(slot.parent_fd)

def _lenient_receipt_order(payload: Any) -> tuple[datetime, str] | None:
    """Extract ``(started_at, run_id)`` from an untrusted receipt payload.

    Used only when reading an existing ``latest.json`` for the monotonic-order
    comparison and history rotation.  Legacy pre-#1080 receipts on disk lack
    the ``registry_classification`` field required by ``_validate_receipt``;
    running the strict validator on them would brick the first post-#1080
    refresh (see finding C-A2).  This lenient reader accepts any payload
    whose ``started_at`` and ``run_id`` parse cleanly; anything malformed
    returns ``None`` and lets the caller default to "replace".
    """
    if not isinstance(payload, Mapping):
        return None
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    try:
        started = _parse_receipt_datetime(payload.get("started_at"))
    except ValueError:
        return None
    return started, run_id

def _publish_primary_receipt(root: Path, receipt: Mapping[str, Any]) -> None:
    canonical = _validate_receipt(receipt)
    content = _receipt_bytes(canonical)
    with provider_destination_lock(root / "receipt-publication", containment_root=root):
        history = root / "history"
        ensure_directory_no_follow(history, containment_root=root)
        run_id = str(canonical["run_id"])
        atomic_write_bytes_no_follow(history / f"{run_id}.json", content, containment_root=root, mode=0o600)
        latest_path = root / "latest.json"
        replace_latest = True
        try:
            existing_bytes = read_bytes_limited_no_follow(
                latest_path,
                max_bytes=MAX_RECEIPT_BYTES,
                containment_root=root,
            )
        except FileNotFoundError:
            existing_bytes = None
        if existing_bytes is not None:
            # Read the existing latest.json leniently — legacy pre-#1080
            # receipts lack `registry_classification` and would otherwise
            # brick this write (finding C-A2).  Validation is a publish-time
            # invariant we hold for receipts THIS process writes, not a
            # gate on the previous generation's shape.
            try:
                existing_payload = json.loads(existing_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError):
                existing_payload = None
            existing_order = _lenient_receipt_order(existing_payload)
            if existing_order is not None:
                replace_latest = _receipt_order(canonical) >= existing_order
        if replace_latest:
            atomic_write_bytes_no_follow(latest_path, content, containment_root=root, mode=0o600)
        files: list[tuple[tuple[datetime, str], Path]] = []
        for item in history.iterdir():
            if not item.is_file() or item.is_symlink():
                continue
            try:
                historical_bytes = read_bytes_limited_no_follow(
                    item,
                    max_bytes=MAX_RECEIPT_BYTES,
                    containment_root=root,
                )
                historical_payload = json.loads(historical_bytes)
                lenient = _lenient_receipt_order(historical_payload)
                if lenient is None:
                    order = (datetime.min.replace(tzinfo=UTC), item.name)
                else:
                    order = lenient
            except (OSError, SafeFilesystemError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                order = (datetime.min.replace(tzinfo=UTC), item.name)
            files.append((order, item))
        files.sort(key=lambda pair: pair[0], reverse=True)
        for _order, obsolete in files[MAX_HISTORY:]:
            try:
                obsolete.unlink()
            except FileNotFoundError:
                pass

def _receipt_order(receipt: Mapping[str, Any]) -> tuple[datetime, str]:
    return _parse_receipt_datetime(receipt["started_at"]), str(receipt["run_id"])

def _receipt(
    *,
    run_id: str,
    started: datetime,
    outcome: str,
    reason: str,
    phase: str,
    providers: Sequence[Mapping[str, Any]],
    orphan_paths: Sequence[str] = (),
    orphan_total: int | None = None,
    orphan_discovered_total: int | None = None,
    orphan_attempted_total: int | None = None,
    registry_classification: Mapping[str, Any] | None = None,
    cutover_gate: Mapping[str, Any] | None = None,
    calibration_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = list(orphan_paths[:MAX_ORPHAN_EVIDENCE])
    total = len(orphan_paths) if orphan_total is None else orphan_total
    discovered_total = total if orphan_discovered_total is None else orphan_discovered_total
    attempted_total = total if orphan_attempted_total is None else orphan_attempted_total
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "outcome": outcome,
        "reason": reason,
        "operation_outcome": outcome,
        "operation_reason": reason,
        "phase": phase,
        "database_free": True,
        "providers": [dict(item) for item in providers[:MAX_COLLECTION_ITEMS]],
        "orphans": {
            "items": evidence,
            "total": total,
            "discovered_total": discovered_total,
            "attempted_total": attempted_total,
            "created_total": total,
            "truncated": total > len(evidence),
        },
        "residues": [],
    }
    if registry_classification is not None:
        # deep-copy through json to freeze the payload against later mutation.
        receipt["registry_classification"] = json.loads(json.dumps(registry_classification))
    if cutover_gate is not None:
        # #1132: the shared normalizer is the single definition point of the
        # audit block, and it returns a fresh scalar-only dict — no separate
        # freeze needed.  Runs that never built a block omit the key.
        receipt["cutover_gate"] = normalize_cutover_gate_audit(cutover_gate)
    if calibration_overrides is not None:
        # Same freeze-through-json treatment as `registry_classification`.
        receipt["calibration_overrides"] = json.loads(json.dumps(calibration_overrides))
    return receipt

def _provider_evidence(name: str, before: Mapping[str, Any], result: Any) -> dict[str, Any]:
    result_map = result if isinstance(result, Mapping) else {}
    checksum = str(result_map.get("content_sha256") or result_map.get("checksum") or "")
    return {
        "name": name,
        "before_sha256": before.get("sha256"),
        "before_inode": before.get("inode"),
        "before_schema_version": before.get("schema_version"),
        "before_generated_at": before.get("generated_at"),
        "before_payload_checksum": before.get("checksum"),
        "after_sha256": checksum.removeprefix("sha256:") or before.get("sha256"),
        "after_schema_version": result_map.get("schema_version") or before.get("schema_version"),
        "after_generated_at": result_map.get("generated_at") or before.get("generated_at"),
        "after_payload_checksum": result_map.get("checksum") or before.get("checksum"),
        "entry_count": int(
            result_map.get("entry_count")
            or result_map.get("model_count")
            or result_map.get("selected_model_count")
            or 0
        ),
    }

def _read_provider_header(path: Path, *, containment_root: Path, max_bytes: int) -> dict[str, Any]:
    try:
        content = read_bytes_limited_no_follow(path, max_bytes=max_bytes, containment_root=containment_root)
        if len(content) > max_bytes:
            raise RefreshError("provider_invalid")
        payload = json.loads(content)
    except (OSError, SafeFilesystemError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RefreshError("provider_invalid") from error
    if not isinstance(payload, Mapping):
        raise RefreshError("provider_invalid")
    return {
        "schema_version": payload.get("schema_version"),
        "generated_at": payload.get("generated_at"),
        "checksum": payload.get("checksum"),
    }
