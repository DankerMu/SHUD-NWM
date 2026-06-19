"""Re-ingest every Basins source under NHMS_BASINS_ROOT and emit an aggregate receipt.

Calls ``reingest_basin`` in-process per basin (no subprocess) so error
payloads stay structured and Python startup cost is paid once.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workers.model_registry.basins_discovery import (
    BasinsDiscoveryError,
    discover_basins_inventory,
    resolve_basins_root,
)
from workers.model_registry.basins_reingest import BasinsReingestError, reingest_basin

AGGREGATE_RECEIPT_SCHEMA_VERSION = "basins.reingest_aggregate.v1"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    work_dir = Path(args.work_dir).expanduser()
    output_path = Path(args.output).expanduser()
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        root = resolve_basins_root(args.basins_root)
        inventory = discover_basins_inventory(root)
    except BasinsDiscoveryError as error:
        print(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1

    discovered_slugs = sorted(
        str(model.get("basin_slug") or "")
        for model in inventory.get("models", [])
        if isinstance(model, dict) and model.get("basin_slug")
    )
    if args.basin_slug:
        # Allow operator to narrow to specific basins; verify each exists.
        requested = list(args.basin_slug)
        missing = [slug for slug in requested if slug not in discovered_slugs]
        if missing:
            print(
                json.dumps(
                    {
                        "error_code": "BASINS_REINGEST_BASIN_NOT_FOUND",
                        "missing_basin_slugs": missing,
                        "available_basin_slugs": discovered_slugs,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 1
        target_slugs = requested
    else:
        target_slugs = discovered_slugs

    receipts: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for basin_slug in sorted(target_slugs):
        per_basin_dir = work_dir / basin_slug
        per_basin_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = per_basin_dir / "receipt.json"
        model_id = args.model_id_template.format(slug=_slug_id(basin_slug))
        try:
            receipt = reingest_basin(
                basin_slug=basin_slug,
                model_id=model_id,
                package_version=args.package_version,
                basins_root=args.basins_root,
                database_url=args.database_url,
                work_dir=per_basin_dir,
                output_path=receipt_path,
                auth_actor_id=args.auth_actor_id,
                auth_roles=args.auth_role,
            )
            receipts.append(receipt)
        except BasinsReingestError as error:
            failure_payload = error.to_payload()
            failure_payload.setdefault("basin_slug", basin_slug)
            failure_payload.setdefault("model_id", model_id)
            failures.append(failure_payload)
            if not args.continue_on_error:
                _write_aggregate(output_path, receipts=receipts, failures=failures)
                print(json.dumps(failure_payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
                return 1

    aggregate = _write_aggregate(output_path, receipts=receipts, failures=failures)
    summary = {
        "status": "ok" if not failures else "partial",
        "output": str(output_path),
        "basin_count": len(receipts),
        "failure_count": len(failures),
        **aggregate["totals"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if not failures else 1


def _write_aggregate(
    output_path: Path,
    *,
    receipts: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    totals = {
        "imported_reach_count": sum(int(r.get("imported_reach_count") or 0) for r in receipts),
        "crosswalk_row_count": sum(int(r.get("crosswalk_row_count") or 0) for r in receipts),
        "geom_null_count": sum(int(r.get("geom_null_count") or 0) for r in receipts),
        "multi_part_violation_count": sum(
            int(r.get("multi_part_violation_count") or 0) for r in receipts
        ),
        "failure_count": len(failures),
    }
    payload = {
        "schema_version": AGGREGATE_RECEIPT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "basins": receipts,
        "failures": failures,
        "totals": totals,
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _slug_id(value: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_").lower()
    return normalized or "unknown"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="reingest-all-basins",
        description="Re-ingest each Basins source under --basins-root and emit an aggregate receipt.",
    )
    parser.add_argument("--basins-root", default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--package-version", required=True)
    parser.add_argument(
        "--basin-slug",
        action="append",
        default=[],
        help="Restrict to specific basin slugs. Repeatable. Defaults to every discovered basin.",
    )
    parser.add_argument(
        "--model-id-template",
        default="basins_{slug}_shud",
        help="Template for the per-basin model_id; {slug} is substituted (lowercased, sanitized).",
    )
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--auth-actor-id", default=None)
    parser.add_argument("--auth-role", action="append", default=[])
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
