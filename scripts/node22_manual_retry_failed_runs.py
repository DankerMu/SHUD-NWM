#!/usr/bin/env python
"""Mark failed DB-free forecast runs for one manual retry.

A run that failed for a reason the classifier calls permanent -- ``ARTIFACT_NOT_FOUND``
is the motivating case -- never retries on its own, even after the cause is gone.  The
scheduler keeps reporting it as ``blocked`` / ``permanent_failure_guard``, so a repaired
input (for example forcing backfilled under a new ``model_id``; see
``node22_backfill_forcing_for_model_ids.py``) does not by itself restart the run.

The sanctioned way to restart one is the policy-gated manual-retry marker,
``FileJournalRetryService.record_manual_repair``: it takes the cycle write lock, refuses
when the run is already active or absent, and writes an evidence trail.
``classify_failure(..., manual=True)`` flips ``permanent`` to ``False`` for exactly the
marked run, so the next scheduler pass selects it again.  Hand-editing journal rows is
NOT an equivalent -- the runbook forbids it, and it leaves no evidence.

Execution host: node-22 (the DB-free file journal lives on its ``/scratch``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.orchestrator.chain_types import OrchestratorError  # noqa: E402
from services.orchestrator.file_orchestration_journal import (  # noqa: E402
    FileJournalRetryService,
    FileOrchestrationJournalRepository,
    RetryEvidenceInvalidError,
)
from services.orchestrator.journal_root_authority import (  # noqa: E402
    journal_root_refusal_line,
    verify_journal_root_authority,
)


def _preview(service: FileJournalRetryService, run_id: str) -> dict[str, Any]:
    """What the marker WOULD act on, without mutating anything.

    Worth a separate read: the forecast stage also has a cohort-master row covering
    every model in the cycle, and a marker aimed at that row would restart the whole
    cohort.  The preview names the row so the operator sees which one it is before
    anything is written.

    #2385: the selector now REFUSES a run whose by-run journal read was blocked
    instead of reporting it as "no retryable failed job".  This call sits at
    ``main``'s ``:100``, OUTSIDE its ``try``, so the refusal has to be turned into
    a receipt entry here or the operator would get a traceback and no receipt at
    all.  ``RetryEvidenceInvalidError`` is the one carrying the journal's own
    ``reason``/``field``, so it is caught by that type rather than as a bare
    ``RetryError``.
    """

    try:
        failed_job, active_job = service._manual_retry_source_for_run(run_id)
    except RetryEvidenceInvalidError as error:
        details = error.details if isinstance(error.details, dict) else {}
        return {
            "decision": "refused",
            "reason": "journal_read_blocked",
            "journal_reason": str(details.get("journal_reason") or ""),
            "journal_field": str(details.get("journal_field") or ""),
        }
    if active_job is not None:
        return {"decision": "refused", "reason": "run_active", "job_id": str(active_job.get("job_id") or "")}
    if failed_job is None:
        return {"decision": "refused", "reason": "no_retryable_failed_job"}
    return {
        "decision": "would_mark",
        "job_id": str(failed_job.get("job_id") or ""),
        "stage": failed_job.get("stage"),
        "status": failed_job.get("status"),
        "error_code": failed_job.get("error_code"),
        "retry_count": failed_job.get("retry_count"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--journal-root", required=True, type=Path, help="DB-free scheduler journal root.")
    parser.add_argument(
        "--run-id",
        action="append",
        required=True,
        dest="run_ids",
        help="Run id to mark (repeatable). One marker per run; never a sweep.",
    )
    parser.add_argument("--reason", required=True, help="Why the run is being restarted. Recorded in the marker.")
    parser.add_argument("--requested-by", required=True, help="Operator identity. Recorded in the marker.")
    parser.add_argument("--execute", action="store_true", help="Write the markers. Default is a preview.")
    parser.add_argument("--output", type=Path, default=None, help="Write the receipt JSON here.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # #1955: this script writes markers, and it has no wrapping handler, so the
    # typed refusal has to be produced (and rendered) here.  Every later use of
    # the root -- the repository AND the receipt's ``journal_root`` field --
    # takes the verified, tilde-expanded value.
    try:
        journal_root = verify_journal_root_authority(args.journal_root, setting="--journal-root")
    except OrchestratorError as error:
        print(journal_root_refusal_line(error), file=sys.stderr)
        return 2
    repository = FileOrchestrationJournalRepository(journal_root)
    service = FileJournalRetryService(repository)

    results: list[dict[str, Any]] = []
    for run_id in args.run_ids:
        entry: dict[str, Any] = {"run_id": run_id, "preview": _preview(service, run_id)}
        if args.execute and entry["preview"]["decision"] == "would_mark":
            try:
                marker = service.record_manual_repair(
                    run_id,
                    requested_by=args.requested_by,
                    reason=args.reason,
                    trusted_internal=True,
                )
            except Exception as error:  # noqa: BLE001 -- every refusal shape is reported, not raised
                entry["outcome"] = "refused"
                entry["error"] = f"{type(error).__name__}: {error}"
            else:
                entry["outcome"] = "marked"
                entry["marker"] = {
                    key: getattr(marker, key)
                    for key in ("job_id", "status", "retry_count")
                    if hasattr(marker, key)
                }
        elif args.execute:
            # Refused at preview, under --execute.  This is a refusal, not a
            # preview: the operator asked for a marker and did not get one, and
            # the exit code has to say so.
            entry["outcome"] = "refused"
            entry["error"] = str(entry["preview"].get("reason") or "refused")
        else:
            entry["outcome"] = "preview_only"
        results.append(entry)

    outcomes: dict[str, int] = {}
    for entry in results:
        outcomes[str(entry["outcome"])] = outcomes.get(str(entry["outcome"]), 0) + 1
    receipt = {
        "journal_root": str(journal_root),
        "executed": bool(args.execute),
        "reason": args.reason,
        "requested_by": args.requested_by,
        "outcome_counts": outcomes,
        "runs": results,
    }
    text = json.dumps(receipt, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    sys.stdout.write(text + "\n")
    return 1 if outcomes.get("refused") else 0


if __name__ == "__main__":
    raise SystemExit(main())
