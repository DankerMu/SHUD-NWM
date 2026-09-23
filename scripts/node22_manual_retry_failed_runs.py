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

A hydro run id (``fcst_<source>_<cycle>_<model>``) whose forecast succeeded but whose
cohort's ``state_save_qc`` failed is refused with ``no_retryable_failed_job``: the failed
row belongs to the cohort master run, not to the hydro run.  For that refusal the preview
lists, read-only, the failed cohort masters of the cycle whose recorded membership covers
the model (``cohort_candidates``), with a warning that marking one re-runs the whole
cohort from convert.  The tool never substitutes the id itself (#2584).

Execution host: node-22 (the DB-free file journal lives on its ``/scratch``).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from packages.common.source_identity import normalize_source_id  # noqa: E402
from services.orchestrator.chain_types import OrchestratorError  # noqa: E402
from services.orchestrator.file_orchestration_journal import (  # noqa: E402
    FileJournalRetryService,
    FileOrchestrationJournalRepository,
    RetryEvidenceInvalidError,
    _blocked_query_job_fault,
    _complete_cohort_members_by_run,
    _file_retry_job_truth_sort_key,
    _is_blocked_query_job,
)
from services.orchestrator.journal_root_authority import (  # noqa: E402
    journal_root_refusal_line,
    verify_journal_root_authority,
)
from services.orchestrator.retry import MANUAL_RETRY_SOURCE_STATUSES  # noqa: E402
from services.orchestrator.run_identity import FORECAST_RUN_ID_RE  # noqa: E402
from services.orchestrator.scheduler_state_types import DOWNSTREAM_STAGE_ALIASES  # noqa: E402
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time  # noqa: E402

COHORT_RESTART_WARNING = (
    "Marking a cohort master re-runs the WHOLE cohort from convert, not only the failed "
    "stage: the forecast of each of its member_count models is recomputed."
)


def _cohort_hint_scope(run_id: str) -> tuple[str, datetime, str] | None:
    """``(source_id, cycle_time, model_id)`` of a ``fcst_<source>_<YYYYMMDDHH>_<model>`` id."""

    match = FORECAST_RUN_ID_RE.fullmatch(run_id)
    if match is None:
        return None
    try:
        return normalize_source_id(match.group(1)), parse_cycle_time(match.group(2)), match.group(3)
    except (TypeError, ValueError):
        return None


def _covered_member_count(
    job: Mapping[str, Any], members_by_run: Mapping[str, frozenset[str]], model_id: str
) -> int | None:
    """Member count of the cohort a ``state_save_qc`` row provably covers ``model_id`` for, else ``None``."""

    if job.get("model_id") not in (None, ""):
        return 1 if str(job.get("model_id")) == model_id else None
    members = members_by_run.get(str(job.get("run_id") or ""))
    return len(members) if members is not None and model_id in members else None


def _cohort_candidates(
    jobs: Sequence[Mapping[str, Any]], *, source_id: str, cycle_time: datetime, model_id: str
) -> list[dict[str, Any]]:
    """Cohort masters of the cycle whose latest ``state_save_qc`` row failed and covers the model.

    Coverage is read, never guessed: a row carrying ``model_id`` is a single-model cohort
    and covers exactly that model; a model-less row covers the model only when its run's
    recorded membership is provable (``_complete_cohort_members_by_run``, the rule the
    scheduler already applies to split cohorts).  A failed master is superseded -- not
    listed -- when a later succeeded ``state_save_qc`` row of a ``cycle_<source>_<stamp>_*``
    run covers the model under that same rule, e.g. a rerun recorded as ``..._full_<model>`` (#2605).
    """

    cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
    members_by_run = _complete_cohort_members_by_run(jobs, source_id=source_id, cycle_time=cycle_time)
    latest_by_run: dict[str, Mapping[str, Any]] = {}
    latest_success_key: tuple[Any, ...] | None = None
    for job in sorted(jobs, key=_file_retry_job_truth_sort_key):
        job_run_id = str(job.get("run_id") or "")
        stage = str(job.get("stage") or "")
        if job_run_id != cycle_run_id and not job_run_id.startswith(f"{cycle_run_id}_"):
            continue
        if DOWNSTREAM_STAGE_ALIASES.get(stage, stage) != "state_save_qc":
            continue
        latest_by_run[job_run_id] = job
        if (
            job_run_id.startswith(f"{cycle_run_id}_")
            and str(job.get("status") or "") == "succeeded"
            and _covered_member_count(job, members_by_run, model_id) is not None
        ):
            latest_success_key = _file_retry_job_truth_sort_key(job)
    candidates: list[dict[str, Any]] = []
    for job_run_id, job in latest_by_run.items():
        if str(job.get("status") or "") not in MANUAL_RETRY_SOURCE_STATUSES:
            continue
        member_count = _covered_member_count(job, members_by_run, model_id)
        if member_count is None:
            continue
        if latest_success_key is not None and _file_retry_job_truth_sort_key(job) < latest_success_key:
            continue
        candidates.append(
            {
                "run_id": job_run_id,
                "job_id": str(job.get("job_id") or ""),
                "stage": job.get("stage"),
                "status": job.get("status"),
                "error_code": job.get("error_code"),
                "member_count": member_count,
            }
        )
    return sorted(candidates, key=lambda candidate: candidate["run_id"])


def _cohort_candidates_hint(repository: Any, run_id: str) -> dict[str, Any]:
    """Read-only hint for a refused hydro run id; never changes the refusal itself."""

    scope = _cohort_hint_scope(run_id)
    if scope is None:
        return {}
    source_id, cycle_time, model_id = scope
    try:
        jobs = repository.query_pipeline_jobs_by_cycle(cycle_id_for(source_id, cycle_time))
        blocked = next((job for job in jobs if _is_blocked_query_job(job)), None)
        if blocked is not None:
            journal_reason, journal_field = _blocked_query_job_fault(blocked)
            return {
                "cohort_candidates_error": {
                    "reason": "journal_read_blocked",
                    "journal_reason": journal_reason,
                    "journal_field": journal_field,
                }
            }
        candidates = _cohort_candidates(jobs, source_id=source_id, cycle_time=cycle_time, model_id=model_id)
    except Exception as error:  # noqa: BLE001 -- a hint failure must not change the preview answer
        return {
            "cohort_candidates_error": {
                "reason": "cohort_candidates_query_failed",
                "error": f"{type(error).__name__}: {error}",
            }
        }
    hint: dict[str, Any] = {"cohort_candidates": candidates}
    if candidates:
        hint["warning"] = COHORT_RESTART_WARNING
    return hint


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
        return {
            "decision": "refused",
            "reason": "no_retryable_failed_job",
            **_cohort_candidates_hint(service.repository, run_id),
        }
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
