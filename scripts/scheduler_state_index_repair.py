#!/usr/bin/env python
"""Repair one logical identity in the two production state-index lanes.

The shared scheduler state index is a checksummed JSON payload. An out-of-band
edit invalidates the top-level checksum and blocks every candidate before
entries are parsed. This operator CLI is the supported recovery path: it
removes exactly one unique identity from both the private/reference scratch
index and the shared/destination canonical index, or recomputes the checksum
of one explicit lane, using the production publisher, validator, locks, and
compare-and-swap.

The two indexes are not whole-file mirrors. Repair never copies one payload
over the other; each lane keeps its own unrelated entries and order. Default
mode is a mutation-free dry-run. ``--enforce`` archives every required exact
pre-image before the first CAS.

It must run as the provider owner (``frd_muziyao`` on node-22): the provider
lock requires the lock parent directory to be owned by the effective uid and
the compare-and-swap requires a matching preimage owner.

Environment:

* ``OBJECT_STORE_ROOT`` — private/reference root default
* ``NHMS_OBJECT_STORE_COPYBACK_ROOT`` — shared/destination root default
* ``OBJECT_STORE_PREFIX`` — object-store prefix
* ``NHMS_SCHEDULER_STATE_INDEX_REPAIR_ARCHIVE_ROOT`` — owner-private archive root
* ``NHMS_SCHEDULER_STATE_INDEX_REPAIR_RECEIPT_ROOT`` — owner-private receipt root

Exit codes: ``0`` complete success, ``2`` only when no index CAS could have
occurred, ``3`` any partial, committed-incomplete, or commit-uncertain result.
After the first lane may have mutated, later failures never claim refusal.
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

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    verify_directory_no_follow,
)
from packages.common.state_manager import (
    STATE_INDEX_REPAIR_LANES,
    StateIndexRepairError,
    StateManagerError,
    repair_state_snapshot_index,
)

SCHEMA_VERSION = "nhms.scheduler.state_index_repair_receipt.v1"
REFERENCE_ROOT_ENV = "OBJECT_STORE_ROOT"
DESTINATION_ROOT_ENV = "NHMS_OBJECT_STORE_COPYBACK_ROOT"
OBJECT_STORE_PREFIX_ENV = "OBJECT_STORE_PREFIX"
ARCHIVE_ROOT_ENV = "NHMS_SCHEDULER_STATE_INDEX_REPAIR_ARCHIVE_ROOT"
RECEIPT_ROOT_ENV = "NHMS_SCHEDULER_STATE_INDEX_REPAIR_RECEIPT_ROOT"
MAX_RECEIPT_BYTES = 1024 * 1024

_POST_CAS_PHASES = frozenset({"replace_uncertain", "postcommit", "release_uncertain"})


class RepairCliError(RuntimeError):
    """Structured CLI failure that has not yet mutated either index."""

    def __init__(self, reason: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"status": "refused", "reason": self.reason, **self.details}


class RepairIncompleteError(RepairCliError):
    """A lane may have been replaced; this is never a refusal."""

    def __init__(
        self,
        reason: str,
        details: Mapping[str, Any] | None = None,
        *,
        summary: Mapping[str, Any],
    ) -> None:
        super().__init__(reason, details)
        self.summary: dict[str, Any] = dict(summary)

    def to_dict(self) -> dict[str, Any]:
        return {"status": "repair_committed_incomplete", "reason": self.reason, **self.details}


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        receipt = repair_state_index(
            operation=args.operation,
            reference_root=args.reference_root,
            destination_root=args.destination_root,
            object_store_prefix=args.object_store_prefix,
            enforce=args.enforce,
            lane=args.lane,
            state_id=args.state_id,
            run_id=args.run_id,
            model_id=args.model_id,
            source_id=args.source_id,
            valid_time=args.valid_time,
            allow_missing_reference=args.allow_missing_reference,
            allow_missing_destination=args.allow_missing_destination,
        )
    except RepairIncompleteError as error:
        print(json.dumps(error.summary, ensure_ascii=False, sort_keys=True))
        print(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 3
    except RepairCliError as error:
        print(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


def repair_state_index(
    *,
    operation: str,
    reference_root: Path | None,
    destination_root: Path | None,
    object_store_prefix: str | None,
    enforce: bool,
    lane: str | None,
    state_id: str | None,
    run_id: str | None,
    model_id: str | None,
    source_id: str | None,
    valid_time: str | None,
    allow_missing_reference: bool,
    allow_missing_destination: bool,
) -> dict[str, Any]:
    started_at = _format_time(datetime.now(tz=UTC))
    receipt_root = _receipt_root(required=enforce) if enforce else None
    archive_root = _archive_root(required=enforce) if enforce else None
    try:
        summary = repair_state_snapshot_index(
            reference_root=_resolved_root(reference_root, env=REFERENCE_ROOT_ENV, field="reference_root"),
            destination_root=_resolved_root(
                destination_root, env=DESTINATION_ROOT_ENV, field="destination_root"
            ),
            operation=operation,
            object_store_prefix=_object_store_prefix(object_store_prefix),
            archive_root=archive_root,
            enforce=enforce,
            lane=lane,
            state_id=state_id,
            run_id=run_id,
            model_id=model_id,
            source_id=source_id,
            valid_time=valid_time,
            allow_missing_reference=allow_missing_reference,
            allow_missing_destination=allow_missing_destination,
        )
    except StateIndexRepairError as error:
        return _raise_from_helper_error(error, started_at=started_at, receipt_root=receipt_root)
    except StateManagerError as error:
        raise RepairCliError(
            str(getattr(error, "reason", error)),
            {"field": str(getattr(error, "field", "repair"))},
        ) from error

    receipt = _receipt_from_summary(summary, started_at=started_at, receipt_root=receipt_root)
    if enforce and receipt_root is not None:
        try:
            _write_receipt(receipt_root, receipt)
        except RepairCliError as error:
            raise RepairIncompleteError(
                "receipt_write_failed_after_repair",
                error.details,
                summary=receipt,
            ) from error
        receipt["receipt_root"] = str(receipt_root)
    return receipt


def _raise_from_helper_error(
    error: StateIndexRepairError,
    *,
    started_at: str,
    receipt_root: Path | None,
) -> dict[str, Any]:
    summary = dict(error.summary or {})
    if not summary:
        summary = {
            "operation": None,
            "mode": "enforce" if receipt_root is not None else "dry_run",
            "lanes": {},
            "mutation_started": error.mutation_started,
        }
    summary.setdefault("mutation_started", error.mutation_started)
    receipt = _receipt_from_summary(summary, started_at=started_at, receipt_root=receipt_root)
    details = {
        "field": error.field,
        "error_reason": error.reason,
        **dict(error.evidence or {}),
    }
    if error.mutation_started or error.phase in _POST_CAS_PHASES:
        if receipt_root is not None:
            try:
                _write_receipt(receipt_root, receipt)
                receipt["receipt_root"] = str(receipt_root)
            except RepairCliError as write_error:
                details["receipt_write_failed"] = True
                details["receipt_failure_reason"] = write_error.reason
                raise RepairIncompleteError(
                    error.reason,
                    details,
                    summary=receipt,
                ) from write_error
        raise RepairIncompleteError(error.reason, details, summary=receipt)
    raise RepairCliError(error.reason, details) from error


def _receipt_from_summary(
    summary: Mapping[str, Any],
    *,
    started_at: str,
    receipt_root: Path | None,
) -> dict[str, Any]:
    lanes = {
        name: dict(summary.get("lanes", {}).get(name) or {})
        for name in STATE_INDEX_REPAIR_LANES
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "started_at": started_at,
        "completed_at": _format_time(datetime.now(tz=UTC)),
        "operation": summary.get("operation"),
        "mode": summary.get("mode"),
        "lane": summary.get("lane"),
        "selector": summary.get("selector"),
        "allow_missing_reference": summary.get("allow_missing_reference"),
        "allow_missing_destination": summary.get("allow_missing_destination"),
        "lock_order": list(summary.get("lock_order") or STATE_INDEX_REPAIR_LANES),
        "write_order": list(summary.get("write_order") or STATE_INDEX_REPAIR_LANES),
        "mutation_started": bool(summary.get("mutation_started")),
        "status": summary.get("status"),
        "lanes": lanes,
        "receipt_root": str(receipt_root) if receipt_root is not None else None,
    }


def _resolved_root(value: Path | None, *, env: str, field: str) -> Path:
    candidate = value if value is not None else _env_path(env, field=field)
    path = candidate.expanduser()
    if not path.is_absolute():
        raise RepairCliError("root_not_absolute", {"field": field, "path": str(candidate)})
    try:
        resolved = path.resolve(strict=False)
        if not resolved.is_dir():
            raise FileNotFoundError(str(resolved))
        verify_directory_no_follow(resolved)
    except (OSError, SafeFilesystemError, RuntimeError) as error:
        raise RepairCliError(
            "root_unavailable",
            {"field": field, "path": str(path), "error": type(error).__name__},
        ) from error
    return resolved


def _object_store_prefix(value: str | None) -> str:
    prefix = (value if value is not None else os.getenv(OBJECT_STORE_PREFIX_ENV, "")).strip()
    if not prefix.startswith("s3://") or not prefix[len("s3://") :].strip("/"):
        raise RepairCliError("object_store_prefix_invalid", {"env": OBJECT_STORE_PREFIX_ENV})
    return prefix


def _env_path(env: str, *, field: str) -> Path:
    value = os.getenv(env, "").strip()
    if not value:
        raise RepairCliError("root_unset", {"field": field, "env": env})
    return Path(value)


def _archive_root(*, required: bool) -> Path | None:
    return _private_root(ARCHIVE_ROOT_ENV, required=required, kind="archive")


def _receipt_root(*, required: bool) -> Path | None:
    return _private_root(RECEIPT_ROOT_ENV, required=required, kind="receipt")


def _private_root(env: str, *, required: bool, kind: str) -> Path | None:
    value = os.getenv(env, "").strip()
    if not value:
        if required:
            raise RepairCliError(f"{kind}_root_unset", {"env": env})
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RepairCliError(f"{kind}_root_invalid", {"env": env, "path": str(path)})
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as error:
        raise RepairCliError(f"{kind}_root_missing", {"env": env, "path": str(path)}) from error
    else:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RepairCliError(f"{kind}_root_invalid", {"env": env, "path": str(path)})
    try:
        verify_directory_no_follow(path)
    except (OSError, SafeFilesystemError) as error:
        raise RepairCliError(f"{kind}_root_invalid", {"env": env, "error": str(error)}) from error
    metadata = os.lstat(path)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RepairCliError(
            f"{kind}_root_not_private",
            {"env": env, "path": str(path), "mode": oct(stat.S_IMODE(metadata.st_mode))},
        )
    return path


def _write_receipt(root: Path, receipt: Mapping[str, Any]) -> None:
    content = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if len(content) > MAX_RECEIPT_BYTES:
        raise RepairCliError("receipt_too_large", {"receipt_bytes": len(content)})
    stamp = str(receipt["started_at"]).replace(":", "").replace("-", "")
    name = f"{stamp}-{receipt['mode']}.json"
    try:
        atomic_write_bytes_no_follow(root / name, content, containment_root=root, mode=0o600)
        atomic_write_bytes_no_follow(root / "latest.json", content, containment_root=root, mode=0o600)
    except (OSError, SafeFilesystemError) as error:
        raise RepairCliError("receipt_write_failed", {"root": str(root), "error": str(error)}) from error


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("remove-entry", "recompute-checksum"))
    parser.add_argument("--reference-root", type=Path, default=None)
    parser.add_argument("--destination-root", type=Path, default=None)
    parser.add_argument("--object-store-prefix", default=None)
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="Perform the real repair; without it the run is a read-only preview.",
    )
    parser.add_argument("--lane", choices=STATE_INDEX_REPAIR_LANES, default=None)
    parser.add_argument("--state-id", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--valid-time", default=None)
    parser.add_argument("--allow-missing-reference", action="store_true")
    parser.add_argument("--allow-missing-destination", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
