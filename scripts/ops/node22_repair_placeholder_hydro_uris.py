"""Repair hydro_run URI fields polluted with display placeholders.

The 2026-07-21/22 orphan repairs round-tripped public (sanitized) journal
rows back into journal writes, persisting "[object-uri]" placeholders into
hydro_run.log_uri / output_uri / run_manifest_uri. This script reconstructs
the deterministic URIs from each run_id, verifies the run manifest exists on
the object store, and appends a corrected hydro_run record through the
journal repository so journal + latest views stay consistent.

Dry-run by default; pass --apply to write. Emits a JSON receipt either way,
except on a journal root the #1955 authority seam refuses: that exits 2 with a
single ``FILE_JOURNAL_INVALID_ROOT: <message>`` line on stderr and no receipt,
before any directory is read.

Usage (node-22, exact active interpreter — never bare uv before maintenance):
    /scratch/frd_muziyao/NWM/.venv/bin/python scripts/ops/node22_repair_placeholder_hydro_uris.py \
        --journal-root /scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal \
        --object-store-root /ghdc/data/nwm/object-store \
        [--apply] [--receipt PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from services.orchestrator.chain_types import OrchestratorError
from services.orchestrator.file_orchestration_journal import (
    FileOrchestrationJournalRepository,
    _format_utc,
    _parse_cycle_time_field,
)
from services.orchestrator.journal_root_authority import (
    journal_root_refusal_line,
    verify_journal_root_authority,
)

PLACEHOLDERS = {"[object-uri]", "[uri]"}
URI_FIELDS = ("log_uri", "output_uri", "run_manifest_uri")


def _expected_uris(run_id: str, prefix: str) -> dict[str, str]:
    return {
        "log_uri": f"{prefix}/runs/{run_id}/logs/",
        "output_uri": f"{prefix}/runs/{run_id}/output/",
        "run_manifest_uri": f"{prefix}/runs/{run_id}/input/manifest.json",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-root", required=True, type=Path)
    parser.add_argument("--object-store-root", required=True, type=Path)
    parser.add_argument("--uri-prefix", default="s3://nhms")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path, default=None)
    args = parser.parse_args()

    # #1955: verified BEFORE the glob, not only before the repository.  The
    # dry-run half reads the tree too, so a blank or relative root would
    # otherwise still report a clean receipt over the working directory, and a
    # symlinked ancestor would report over a tree the operator never named.
    try:
        journal_root = verify_journal_root_authority(args.journal_root, setting="--journal-root")
    except OrchestratorError as error:
        print(journal_root_refusal_line(error), file=sys.stderr)
        return 2

    repaired: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    repository = FileOrchestrationJournalRepository(journal_root) if args.apply else None

    for latest_path in sorted(journal_root.glob("latest/*/*/*.json")):
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
        hydro_run = payload.get("hydro_run")
        if not isinstance(hydro_run, dict):
            continue
        polluted = [field for field in URI_FIELDS if hydro_run.get(field) in PLACEHOLDERS]
        if not polluted:
            continue
        run_id = str(hydro_run.get("run_id") or "")
        entry = {
            # The verified root, never the raw one: ``relative_to`` against an
            # unexpanded ``~/journal`` literal raises ``ValueError`` for every
            # legitimate tilde root.
            "latest": str(latest_path.relative_to(journal_root)),
            "run_id": run_id,
            "fields": ",".join(polluted),
        }
        manifest_path = args.object_store_root / "runs" / run_id / "input" / "manifest.json"
        if not run_id or not manifest_path.is_file():
            skipped.append({**entry, "reason": "run_manifest_missing_on_disk"})
            continue
        if repository is None:
            repaired.append({**entry, "mode": "dry_run"})
            continue
        expected = _expected_uris(run_id, args.uri_prefix)
        source_id = str(hydro_run["source_id"])
        cycle_time = _parse_cycle_time_field(hydro_run, "cycle_time")
        with repository._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            row = repository._hydro_run_for(run_id)
            if row is None:
                skipped.append({**entry, "reason": "hydro_run_not_found_in_journal"})
                continue
            still_polluted = [field for field in URI_FIELDS if row.get(field) in PLACEHOLDERS]
            if not still_polluted:
                skipped.append({**entry, "reason": "already_clean"})
                continue
            corrected = dict(row)
            for field in still_polluted:
                corrected[field] = expected[field]
            corrected["updated_at"] = _format_utc(datetime.now(UTC))
            repository._append_validated_record_unlocked(
                "hydro_run",
                corrected,
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=str(corrected["model_id"]),
                materialize_model_id=str(corrected["model_id"]),
            )
        repaired.append({**entry, "mode": "applied", "fields": ",".join(still_polluted)})

    receipt = {
        "created_at": _format_utc(datetime.now(UTC)),
        "mode": "apply" if args.apply else "dry_run",
        "repaired_count": len(repaired),
        "skipped_count": len(skipped),
        "repaired": repaired,
        "skipped": skipped,
    }
    rendered = json.dumps(receipt, indent=2, sort_keys=True)
    if args.receipt is not None:
        args.receipt.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if not skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
