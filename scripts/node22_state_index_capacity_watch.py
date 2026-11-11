#!/usr/bin/env python
"""Daily node-22 state-index capacity watch (#2653): ``prune-retention`` dry-run only.

Calls ``scripts.scheduler_state_index_repair.repair_state_index`` in-process
with ``operation="prune-retention"`` and a literal ``enforce=False``; there is
no enforce flag. Roots, prefix and cycle lag come from the environment exactly
as the repair CLI resolves them. A dry-run takes no lock and never resolves the
repair archive or repair receipt roots, so nothing is written except this
watch's own bounded receipt.

Environment:

* ``OBJECT_STORE_ROOT`` / ``NHMS_OBJECT_STORE_COPYBACK_ROOT`` / ``OBJECT_STORE_PREFIX``
  / ``NHMS_SCHEDULER_CYCLE_LAG_HOURS`` — as for the repair CLI
* the prune window is ``max(21, floor_hours // 24 + 1)`` days, ``floor_hours``
  from the repair's own floor helper and that lag (#3118); an unset or invalid
  lag keeps 21 days and the repair refuses it. The receipt's top-level
  ``retention_days_source`` is ``default`` or ``derived_from_cycle_lag``
  (``null`` only when the repair module failed to import)
* ``NHMS_STATE_INDEX_CAPACITY_WATCH_RECEIPT_ROOT`` — existing owner-private
  receipt directory (``--receipt-root`` overrides; never created here)

Exit codes: ``0`` healthy; ``1`` at least one alert (receipt still written);
``2`` repair refusal, unsafe/unset receipt root, or receipt write failure;
``3`` repair incomplete (defensive: a dry-run cannot mutate); ``4`` unexpected
failure. The receipt JSON is printed to stdout before the files are written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "nhms.scheduler.state_index_capacity_watch_receipt.v1"
RECEIPT_ROOT_ENV = "NHMS_STATE_INDEX_CAPACITY_WATCH_RECEIPT_ROOT"
LANES = ("reference", "destination")
PREIMAGE_CHANGED = "provider_preimage_changed"
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 5
SOURCE_DEFAULT = "default"
SOURCE_DERIVED = "derived_from_cycle_lag"
KEEP_RECEIPTS = 30
LATEST_NAME = "latest.json"
_RECEIPT_NAME = re.compile(r"^\d{8}T\d{6}Z\.json$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--receipt-root", default=None, help=f"defaults to ${RECEIPT_ROOT_ENV}")
    return parser


def main(argv: list[str] | None = None) -> int:
    started = datetime.now(tz=UTC)
    try:
        # Imported here so an import failure is exit 4, never an alert-shaped exit 1.
        from packages.common import safe_fs
        from packages.common.state_manager import minimum_state_index_retention_days
        from scripts import scheduler_state_index_repair as repair
    except Exception as error:  # noqa: BLE001 - any import failure is a watch failure
        return _publish(_refused(started, 4, _unexpected(error), attempts=0, source=None), None, None)
    args = build_parser().parse_args(argv)
    retention_days, source = _retention_window(repair, minimum_state_index_retention_days)
    root: Path | None = None
    attempts = 0
    try:
        root = repair._private_directory(
            args.receipt_root if args.receipt_root is not None else os.getenv(RECEIPT_ROOT_ENV, ""),
            env=RECEIPT_ROOT_ENV,
            required=True,
            kind="receipt",
        )
        while True:
            attempts += 1
            try:
                summary = _dry_run(repair, retention_days)
                break
            except repair.RepairIncompleteError:
                raise
            except repair.RepairCliError as error:
                if error.reason != PREIMAGE_CHANGED or attempts >= MAX_ATTEMPTS:
                    raise
                time.sleep(RETRY_DELAY_SECONDS)
    except repair.RepairIncompleteError as error:
        receipt = _refused(started, 3, _error(error), attempts=attempts, source=source)
    except repair.RepairCliError as error:
        receipt = _refused(started, 2, _error(error), attempts=attempts, source=source)
    except Exception as error:  # noqa: BLE001 - a programming error must not look like an alert
        receipt = _refused(started, 4, _unexpected(error), attempts=attempts, source=source)
    else:
        receipt = _evaluated(started, summary, attempts=attempts, source=source)
    return _publish(receipt, root, safe_fs)


def _retention_window(repair: Any, minimum_days: Any) -> tuple[int, str]:
    """Widen the default window to the repair's floor for the current lag (#3118).

    Parses the lag exactly as the repair's ``_env_cycle_lag_hours`` does. An
    unset, non-integer, or negative lag keeps the default and is left for the
    repair (which reads the same env) to refuse with its own reason and field.
    """

    default_days = repair.DEFAULT_STATE_INDEX_RETENTION_DAYS
    value = os.getenv(repair.CYCLE_LAG_HOURS_ENV, "").strip()
    try:
        lag = int(value)
    except ValueError:
        return default_days, SOURCE_DEFAULT
    if lag < 0:
        return default_days, SOURCE_DEFAULT
    minimum = minimum_days(lag)
    if minimum <= default_days:
        return default_days, SOURCE_DEFAULT
    return minimum, SOURCE_DERIVED


def _dry_run(repair: Any, retention_days: int) -> dict[str, Any]:
    return repair.repair_state_index(
        operation="prune-retention",
        reference_root=None,
        destination_root=None,
        object_store_prefix=None,
        enforce=False,
        lane=None,
        state_id=None,
        run_id=None,
        model_id=None,
        source_id=None,
        valid_time=None,
        allow_missing_reference=False,
        allow_missing_destination=False,
        retention_days=retention_days,
        cycle_lag_hours=None,
    )


def _evaluated(started: datetime, summary: dict[str, Any], *, attempts: int, source: str) -> dict[str, Any]:
    lanes: dict[str, Any] = {}
    alerts: list[dict[str, str]] = []
    raw_lanes = summary.get("lanes") if isinstance(summary.get("lanes"), dict) else {}
    for name in LANES:
        lane = raw_lanes.get(name)
        retention = lane.get("retention") if isinstance(lane, dict) else None
        if not isinstance(lane, dict) or not isinstance(retention, dict):
            lanes[name] = None
            alerts.append({"lane": name, "alert": "lane_summary_missing"})
            continue
        lanes[name] = {
            "root": lane.get("root"),
            "index": lane.get("index"),
            "checksum_valid": lane.get("checksum_valid"),
            "action": lane.get("action"),
            "untouched_reason": lane.get("untouched_reason"),
            "entry_count_before": retention.get("entry_count_before"),
            "removed_count": retention.get("removed_count"),
            "removed_state_ids_sha256": retention.get("removed_state_ids_sha256"),
            "retention_days": retention.get("retention_days"),
            "cycle_lag_hours": retention.get("cycle_lag_hours"),
            "capacity_before": retention.get("capacity_before"),
            "capacity_after": retention.get("capacity_after"),
        }
        before = retention.get("capacity_before") if isinstance(retention.get("capacity_before"), dict) else {}
        after = retention.get("capacity_after") if isinstance(retention.get("capacity_after"), dict) else {}
        if lane.get("checksum_valid") is not True:
            alerts.append({"lane": name, "alert": "checksum_invalid"})
        if before.get("warning") is True:
            alerts.append({"lane": name, "alert": "capacity_warning"})
        if after.get("warning") is True:
            alerts.append({"lane": name, "alert": "capacity_warning_unprunable"})
        if retention.get("entry_count_before") == 0:
            alerts.append({"lane": name, "alert": "lane_empty"})
    exit_code = 1 if alerts else 0
    return {
        **_header(started, "alert" if alerts else "healthy", exit_code, attempts=attempts, source=source),
        "alerts": alerts,
        "lanes": lanes,
    }


def _refused(
    started: datetime, exit_code: int, error: dict[str, Any], *, attempts: int, source: str | None
) -> dict[str, Any]:
    return {
        **_header(started, "refused", exit_code, attempts=attempts, source=source),
        "alerts": [],
        "lanes": None,
        "error": error,
    }


def _header(started: datetime, status: str, exit_code: int, *, attempts: int, source: str | None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "started_at": _format_utc_seconds(started),
        "finished_at": _format_utc_seconds(datetime.now(tz=UTC)),
        "status": status,
        "exit_code": exit_code,
        "attempts": attempts,
        "retention_days_source": source,
    }


def _error(error: Any) -> dict[str, Any]:
    details = error.details
    return {"reason": error.reason, "field": details.get("field") or details.get("env")}


def _unexpected(error: BaseException) -> dict[str, Any]:
    return {"reason": "unexpected_exception", "error_type": type(error).__name__}


def _publish(receipt: dict[str, Any], root: Path | None, safe_fs: Any) -> int:
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True), flush=True)
    if root is None:
        return int(receipt["exit_code"])
    try:
        _write_receipt(root, receipt, safe_fs)
    except Exception as error:  # noqa: BLE001 - any write/rotation failure is exit 2
        print(
            json.dumps(
                {
                    "status": "refused",
                    "error": {"reason": "receipt_write_failed", "error_type": type(error).__name__},
                    "root": str(root),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    return int(receipt["exit_code"])


def _write_receipt(root: Path, receipt: dict[str, Any], safe_fs: Any) -> None:
    content = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    name = str(receipt["started_at"]).replace("-", "").replace(":", "") + ".json"
    safe_fs.atomic_write_bytes_no_follow(root / name, content, containment_root=root, mode=0o600)
    safe_fs.atomic_write_bytes_no_follow(root / LATEST_NAME, content, containment_root=root, mode=0o600)
    stamped = sorted(
        entry
        for entry in os.listdir(root)
        if _RECEIPT_NAME.match(entry) and stat.S_ISREG(os.lstat(root / entry).st_mode)
    )
    for stale in stamped[:-KEEP_RECEIPTS]:
        os.unlink(root / stale)


def _format_utc_seconds(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
