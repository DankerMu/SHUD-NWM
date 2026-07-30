#!/usr/bin/env python
"""Replay a failed state-index copyback merge into the shared canonical index.

The scoped copyback merge that runs after ``state_save_qc`` is the only writer
that adds entries to the shared canonical state index.  When it fails closed
(``OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED``) the affected stage is already
terminal, so the entries never arrive and the chain stalls (#1189).  This
operator-invoked tool replays that merge for an explicit run-id or cycle set
using the same ``merge_state_snapshot_index_copyback`` code path, so the
scoping, locking, compare-and-swap and checksum semantics are identical to
production.

It must run as the provider owner (``frd_muziyao`` on node-22): the provider
lock requires the lock parent directory to be owned by the effective uid and
the compare-and-swap requires a matching preimage owner.

Default mode is a read-only dry run.  ``--enforce`` performs the merge and
writes a receipt under ``NHMS_SCHEDULER_COPYBACK_REPLAY_RECEIPT_ROOT``.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.provider_atomic import ProviderAtomicError, read_provider_snapshot
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    verify_directory_no_follow,
)
from packages.common.state_manager import (
    MAX_STATE_SNAPSHOT_INDEX_BYTES,
    StateManagerError,
    merge_state_snapshot_index_copyback,
)

SCHEMA_VERSION = "nhms.scheduler.state_index_copyback_replay_receipt.v1"
STATE_INDEX_RELATIVE_PATH = Path("scheduler/state-index/index-last.json")
REFERENCE_ROOT_ENV = "OBJECT_STORE_ROOT"
DESTINATION_ROOT_ENV = "NHMS_OBJECT_STORE_COPYBACK_ROOT"
OBJECT_STORE_PREFIX_ENV = "OBJECT_STORE_PREFIX"
RECEIPT_ROOT_ENV = "NHMS_SCHEDULER_COPYBACK_REPLAY_RECEIPT_ROOT"
MAX_RECEIPT_BYTES = 1024 * 1024


class ReplayError(RuntimeError):
    """Structured fail-closed refusal; never leaves a partial index write."""

    def __init__(self, reason: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"status": "refused", "reason": self.reason, **self.details}


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        receipt = replay_state_index_copyback(
            reference_root=args.reference_root,
            destination_root=args.destination_root,
            object_store_prefix=args.object_store_prefix,
            run_ids=args.run_ids,
            cycles=args.cycle,
            enforce=args.enforce,
        )
    except ReplayError as error:
        print(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


def replay_state_index_copyback(
    *,
    reference_root: Path | None,
    destination_root: Path | None,
    object_store_prefix: str | None,
    run_ids: str | None,
    cycles: Sequence[str] | None,
    enforce: bool,
) -> dict[str, Any]:
    started_at = datetime.now(tz=UTC)
    reference = _resolved_root(reference_root, env=REFERENCE_ROOT_ENV, field="reference_root")
    destination = _resolved_root(destination_root, env=DESTINATION_ROOT_ENV, field="destination_root")
    prefix = _object_store_prefix(object_store_prefix)
    _refuse_root_conflicts(reference, destination)
    receipt_root = _receipt_root(required=enforce)

    source_index = reference / STATE_INDEX_RELATIVE_PATH
    destination_index = destination / STATE_INDEX_RELATIVE_PATH
    source_entries = _read_index_entries(source_index, containment_root=reference, field="source_index")
    if not source_entries:
        raise ReplayError("source_index_empty", {"source_index": str(source_index)})
    destination_entries = _read_index_entries(
        destination_index,
        containment_root=destination,
        field="destination_index",
        allow_missing=True,
    )

    requested_cycles = [_normalize_cycle_id(value) for value in cycles or []]
    resolved_run_ids = (
        _run_ids_from_cycles(source_entries, requested_cycles)
        if requested_cycles
        else _run_ids_from_request(source_entries, run_ids)
    )
    matched_entries = [
        entry for entry in source_entries if str(entry.get("run_id") or "") in resolved_run_ids
    ]
    destination_state_ids = {str(entry.get("state_id") or "") for entry in destination_entries}
    # Advisory preview only: it compares state ids, not the merge's identity-key
    # collision semantics, so it is a lower bound on what enforce will publish.
    preview_new_state_ids = sorted(
        {str(entry.get("state_id") or "") for entry in matched_entries} - destination_state_ids
    )

    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "enforce" if enforce else "dry_run",
        "started_at": _format_time(started_at),
        "reference_root": str(reference),
        "destination_root": str(destination),
        "object_store_prefix": prefix,
        "source_index": str(source_index),
        "destination_index": str(destination_index),
        "requested_cycles": requested_cycles,
        "resolved_run_ids": sorted(resolved_run_ids),
        "source_entry_count": len(source_entries),
        "matched_source_entry_count": len(matched_entries),
        "destination_entry_count_before": len(destination_entries),
        "preview_new_state_ids": preview_new_state_ids,
        "preview_new_entry_count": len(preview_new_state_ids),
        "preview_is_advisory": True,
        "destination_entry_count_after": len(destination_entries),
        "checkpoint_copied_count": None,
        "checkpoint_reused_count": None,
        "checkpoint_replaced_count": None,
        "merge": None,
    }
    if enforce:
        try:
            merge = merge_state_snapshot_index_copyback(
                source_path=source_index,
                destination_path=destination_index,
                reference_object_store_root=reference,
                object_store_prefix=prefix,
                source_containment_root=reference,
                destination_containment_root=destination,
                authoritative_run_ids=sorted(resolved_run_ids),
            )
        except (StateManagerError, ProviderAtomicError) as error:
            raise ReplayError(
                "merge_failed",
                {
                    "error_reason": str(getattr(error, "reason", "") or ""),
                    "error": str(error),
                    "resolved_run_ids": sorted(resolved_run_ids),
                },
            ) from error
        after_entries = _read_index_entries(
            destination_index,
            containment_root=destination,
            field="destination_index",
        )
        receipt.update(
            {
                "destination_entry_count_after": len(after_entries),
                "checkpoint_copied_count": int(merge["checkpoint_copied_count"]),
                "checkpoint_reused_count": int(merge["checkpoint_reused_count"]),
                "checkpoint_replaced_count": int(merge["checkpoint_replaced_count"]),
                "merge": {
                    "source_entry_count": merge["source_entry_count"],
                    "authoritative_run_count": merge["authoritative_run_count"],
                    "merged_entry_count": merge["merged_entry_count"],
                    "published_entry_count": merge["entry_count"],
                    "content_sha256": merge.get("content_sha256"),
                    "generated_at": merge.get("generated_at"),
                },
            }
        )
    receipt["completed_at"] = _format_time(datetime.now(tz=UTC))
    if receipt_root is not None:
        _write_receipt(receipt_root, receipt)
        receipt["receipt_root"] = str(receipt_root)
    return receipt


def _run_ids_from_request(entries: Sequence[Mapping[str, Any]], run_ids: str | None) -> set[str]:
    requested = {value.strip() for value in (run_ids or "").split(",") if value.strip()}
    if not requested:
        raise ReplayError("run_ids_empty")
    present = {str(entry.get("run_id") or "") for entry in entries}
    missing = sorted(requested - present)
    if missing:
        raise ReplayError("run_ids_absent_from_source_index", {"missing_run_ids": missing})
    return requested


def _run_ids_from_cycles(entries: Sequence[Mapping[str, Any]], cycles: Sequence[str]) -> set[str]:
    resolved: set[str] = set()
    unresolved: list[str] = []
    for cycle in cycles:
        matched = {
            str(entry.get("run_id") or "")
            for entry in entries
            # `cycle_id` is a flat optional entry field and may be absent/None.
            if _normalize_cycle_id(entry.get("cycle_id")) == cycle and entry.get("run_id")
        }
        if not matched:
            unresolved.append(cycle)
            continue
        resolved |= matched
    if unresolved:
        raise ReplayError("cycles_absent_from_source_index", {"unresolved_cycles": sorted(unresolved)})
    if not resolved:
        raise ReplayError("run_ids_empty", {"requested_cycles": list(cycles)})
    return resolved


def _normalize_cycle_id(value: Any) -> str:
    # Production cycle ids are `<lowercase source>_<YYYYMMDDHH>`
    # (workers.data_adapters.base.cycle_id_for), so operator input is
    # lowercased instead of failing on `IFS_2026072000`.
    if value is None:
        return ""
    return str(value).strip().lower()


def _read_index_entries(
    path: Path,
    *,
    containment_root: Path,
    field: str,
    allow_missing: bool = False,
) -> list[dict[str, Any]]:
    try:
        content, _preimage = read_provider_snapshot(
            path,
            containment_root=containment_root,
            max_bytes=MAX_STATE_SNAPSHOT_INDEX_BYTES,
        )
    except ProviderAtomicError as error:
        if allow_missing and error.reason == "provider_destination_missing":
            return []
        raise ReplayError("index_unreadable", {"field": field, "path": str(path), "error": str(error)}) from error
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ReplayError("index_malformed_json", {"field": field, "path": str(path)}) from error
    if not isinstance(payload, Mapping):
        raise ReplayError("index_not_object", {"field": field, "path": str(path)})
    entries = payload.get("entries")
    if not isinstance(entries, Sequence) or isinstance(entries, str | bytes | bytearray):
        raise ReplayError("index_entries_invalid", {"field": field, "path": str(path)})
    if not all(isinstance(entry, Mapping) for entry in entries):
        raise ReplayError("index_entries_invalid", {"field": field, "path": str(path)})
    return [dict(entry) for entry in entries]


def _resolved_root(value: Path | None, *, env: str, field: str) -> Path:
    candidate = value if value is not None else _env_path(env, field=field)
    path = candidate.expanduser()
    if not path.is_absolute():
        raise ReplayError("root_not_absolute", {"field": field, "path": str(candidate)})
    try:
        resolved = path.resolve(strict=False)
        if not resolved.is_dir():
            raise FileNotFoundError(str(resolved))
        verify_directory_no_follow(resolved)
    except (OSError, SafeFilesystemError) as error:
        raise ReplayError("root_unavailable", {"field": field, "path": str(path), "error": str(error)}) from error
    return resolved


def _refuse_root_conflicts(reference: Path, destination: Path) -> None:
    if reference == destination:
        raise ReplayError(
            "roots_identical",
            {"reference_root": str(reference), "destination_root": str(destination)},
        )
    if _paths_overlap(reference, destination):
        raise ReplayError(
            "roots_overlap",
            {"reference_root": str(reference), "destination_root": str(destination)},
        )


def _paths_overlap(left: Path, right: Path) -> bool:
    for first, second in ((left, right), (right, left)):
        try:
            first.relative_to(second)
            return True
        except ValueError:
            continue
    return False


def _object_store_prefix(value: str | None) -> str:
    prefix = (value if value is not None else os.getenv(OBJECT_STORE_PREFIX_ENV, "")).strip()
    if not prefix.startswith("s3://") or not prefix[len("s3://") :].strip("/"):
        raise ReplayError("object_store_prefix_invalid", {"env": OBJECT_STORE_PREFIX_ENV})
    return prefix


def _env_path(env: str, *, field: str) -> Path:
    value = os.getenv(env, "").strip()
    if not value:
        raise ReplayError("root_unset", {"field": field, "env": env})
    return Path(value)


def _receipt_root(*, required: bool) -> Path | None:
    value = os.getenv(RECEIPT_ROOT_ENV, "").strip()
    if not value:
        if required:
            raise ReplayError("receipt_root_unset", {"env": RECEIPT_ROOT_ENV})
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ReplayError("receipt_root_invalid", {"env": RECEIPT_ROOT_ENV, "path": str(path)})
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        try:
            ensure_directory_no_follow(path)
            os.chmod(path, 0o700, follow_symlinks=False)
        except (OSError, SafeFilesystemError) as error:
            raise ReplayError("receipt_root_invalid", {"env": RECEIPT_ROOT_ENV, "error": str(error)}) from error
    else:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ReplayError("receipt_root_invalid", {"env": RECEIPT_ROOT_ENV, "path": str(path)})
    try:
        verify_directory_no_follow(path)
    except (OSError, SafeFilesystemError) as error:
        raise ReplayError("receipt_root_invalid", {"env": RECEIPT_ROOT_ENV, "error": str(error)}) from error
    metadata = os.lstat(path)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ReplayError(
            "receipt_root_not_private",
            {"env": RECEIPT_ROOT_ENV, "path": str(path), "mode": oct(stat.S_IMODE(metadata.st_mode))},
        )
    return path


def _write_receipt(root: Path, receipt: Mapping[str, Any]) -> None:
    content = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if len(content) > MAX_RECEIPT_BYTES:
        raise ReplayError("receipt_too_large", {"receipt_bytes": len(content)})
    stamp = str(receipt["started_at"]).replace(":", "").replace("-", "")
    name = f"{stamp}-{receipt['mode']}.json"
    try:
        atomic_write_bytes_no_follow(root / name, content, containment_root=root, mode=0o600)
        atomic_write_bytes_no_follow(root / "latest.json", content, containment_root=root, mode=0o600)
    except (OSError, SafeFilesystemError) as error:
        raise ReplayError("receipt_write_failed", {"root": str(root), "error": str(error)}) from error


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-root", type=Path, default=None)
    parser.add_argument("--destination-root", type=Path, default=None)
    parser.add_argument("--object-store-prefix", default=None)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--run-ids", default=None, help="Comma-separated run ids to replay.")
    selection.add_argument(
        "--cycle",
        action="append",
        default=None,
        help="Cycle id to replay (repeatable), e.g. gfs_2026072000.",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="Perform the real merge; without it the run is a read-only preview.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
