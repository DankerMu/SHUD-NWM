"""Shared #1748 released-reservation-recovery helpers for the orchestrator CLI.

The CLI's ``recover-released-identity-blocked-reservation`` body is shared
verbatim by the Click and argparse entrypoints.  The callable, its diagnosis
mirror, the summary shape, and both entrypoints' registration/dispatch helpers
live here so the CLI module stays under the large-file guard while keeping the
one shared public seam ``_recover_released_identity_blocked_reservation``
re-exported from :mod:`services.orchestrator.cli` for the existing tests.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any

from .accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    IDENTITY_MISMATCH_RELEASED_DECISION,
    accepted_submit_contract_is_current,
    accepted_submit_row_kind,
)
from .file_orchestration_journal import (
    OPERATOR_RECOVERY_ATTESTATION_FIELD,
    RELEASED_RESERVATION_RECOVERY_COMMAND,
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
    _accepted_submit_source_cycle_from_job_id,
)

RECOVER_RELEASED_RESERVATION_HELP = (
    "Find and recover released identity-blocked cohort masters (#1748). Without "
    "--job-id it LISTS the wedged rows with the values needed to act on them; "
    "with --job-id it inspects one row. Nothing is written unless --attest is "
    "given. This performs NO Slurm-side liveness or absence check: on a cluster "
    "whose accounting does not retain job comments, absence is not provable, so "
    "--attest is an operator ATTESTATION, not a proof. Confirm the cohort is "
    "really gone before running it. Refusals are write-free and name the "
    "precondition that failed; a repeat attestation is an idempotent no-op."
)

# Diagnosis mirrors ``recover_released_identity_blocked_reservation``'s own
# precondition order.  It exists ONLY to name the failing precondition: the
# typed API returns a bare ``None`` for every refusal, which tells an operator
# nothing, and this command exists precisely because the wedge was
# undiagnosable.  Authority still sits with the API -- nothing here writes.
def _diagnose_released_reservation_recovery(row: Mapping[str, Any]) -> str | None:
    journal = row.get("file_journal")
    if isinstance(journal, Mapping) and journal.get("status") == "blocked":
        return "journal_unreadable"
    if not accepted_submit_contract_is_current(row):
        return "accepted_submit_contract_not_current"
    if accepted_submit_row_kind(row) != "master":
        return "row_is_not_a_cohort_master"
    if str(row.get("status") or "") != "reservation_lost":
        return "status_not_reservation_lost"
    if row.get("reconciliation_decision") != IDENTITY_MISMATCH_RELEASED_DECISION:
        return "decision_not_identity_mismatch_released"
    if row.get("slurm_job_id") not in (None, "") or row.get("matched_slurm_job_id") not in (None, ""):
        return "slurm_job_binding_present"
    if row.get("submission_attempt_started_at") is None:
        return "submission_attempt_anchor_missing"
    if type(row.get("submission_attempt")) is not int or int(row["submission_attempt"]) < 1:
        return "submission_attempt_invalid"
    return None


def _released_reservation_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """Everything an operator needs to decide AND to act, in one line of JSON."""

    return {
        "job_id": row.get("job_id"),
        "run_id": row.get("run_id"),
        "stage": row.get("stage"),
        "cohort_digest": row.get("cohort_digest"),
        "identity_blocked_streak": row.get("identity_blocked_streak"),
        "cohort_member_count": len(row.get("cohort_members") or []),
        "expected_submission_attempt": row.get("submission_attempt"),
        "expected_submission_attempt_started_at": _optional_str(
            row.get("submission_attempt_started_at")
        ),
        OPERATOR_RECOVERY_ATTESTATION_FIELD: row.get(OPERATOR_RECOVERY_ATTESTATION_FIELD),
    }


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _recover_released_identity_blocked_reservation(
    *,
    journal_root: str,
    job_id: str | None,
    dry_run: bool,
) -> tuple[dict[str, Any], int]:
    """Operator-initiated recovery of released identity-blocked reservations.

    Two halves, because an execute-only command would leave the operator stuck
    one step earlier: the ``IDENTITY_RELEASED_RESERVATION_NEEDS_OPERATOR``
    signal names an API whose CAS arguments a human has no supported way to
    read.  Without ``--job-id`` this LISTS the wedged rows with those values;
    with ``--job-id`` it inspects one row, and only ``--attest`` writes.  That
    is the same dry-run-by-default shape ``cleanup``/``plan-production`` use for
    read-then-mutate, not a new convention.

    There is deliberately no automatic caller: no scheduler pass, timer or
    reconcile arm reaches this function or the typed API beneath it.

    CAS values are read off the row rather than demanded from the operator.
    What that still protects: the API re-reads under ``_locked_cycle_write`` and
    compares against the values read here, so a writer landing between this
    read and the locked write loses the race, write-free -- the attestation can
    never be stamped onto an attempt that has already moved on.  What it no
    longer protects: a change between the operator's OWN reading of the listing
    and the invocation is invisible, because the command re-reads instead of
    holding the operator to what they saw.  That window is empty in practice --
    the release path is never re-entered for an already-released row and no
    automatic path can set or clear the attestation -- but it is a genuinely
    weaker guarantee than an operator-supplied expectation, and ``--dry-run``
    followed by ``--attest`` narrows it rather than closing it.
    """

    repository = FileOrchestrationJournalRepository(journal_root)
    if job_id is None:
        if not dry_run:
            return {
                "decision": "refused",
                "reason": "job_id_required_to_attest",
                "detail": "--attest acts on exactly one row; run without --job-id to list candidates",
            }, 2
        wedged = repository.query_released_identity_blocked_jobs()
        return {
            "decision": "listed",
            "slurm_liveness_checked": False,
            "wedged_count": len(wedged),
            "wedged": [_released_reservation_summary(row) for row in wedged],
        }, 0

    try:
        _accepted_submit_source_cycle_from_job_id(job_id)
    except FileOrchestrationJournalError as error:
        return {
            "job_id": job_id,
            "decision": "refused",
            "reason": "job_id_unparseable",
            "detail": error.reason,
        }, 2

    row = repository.get_pipeline_job(job_id)
    if row is None:
        return {"job_id": job_id, "decision": "refused", "reason": "job_not_found"}, 2

    observed = {
        "status": row.get("status"),
        "reconciliation_decision": row.get("reconciliation_decision"),
        "slurm_job_id": row.get("slurm_job_id"),
        "matched_slurm_job_id": row.get("matched_slurm_job_id"),
        "submission_attempt": row.get("submission_attempt"),
        OPERATOR_RECOVERY_ATTESTATION_FIELD: row.get(OPERATOR_RECOVERY_ATTESTATION_FIELD),
    }
    refusal = _diagnose_released_reservation_recovery(row)
    if refusal is not None:
        return {
            "job_id": job_id,
            "decision": "refused",
            "reason": refusal,
            "observed": observed,
        }, 2

    expected_attempt = int(row["submission_attempt"])
    expected_anchor = row["submission_attempt_started_at"]
    was_attested = row.get(OPERATOR_RECOVERY_ATTESTATION_FIELD) not in (None, "")
    receipt: dict[str, Any] = {
        "job_id": job_id,
        "expected_submission_attempt": expected_attempt,
        "expected_submission_attempt_started_at": _optional_str(expected_anchor),
        "slurm_liveness_checked": False,
        "observed": observed,
        "summary": _released_reservation_summary(row),
    }
    if dry_run:
        return {
            **receipt,
            "decision": "already_attested" if was_attested else "eligible",
            "detail": "no write performed; re-run with --attest to record the attestation",
        }, 0

    attested = repository.recover_released_identity_blocked_reservation(
        job_id,
        # From code, never from an operator flag: this is a stale-code guard,
        # not something the operator knows or should override.
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=expected_attempt,
        expected_submission_attempt_started_at=expected_anchor,
    )
    if attested is None:
        # The diagnosis passed but the locked re-read refused. Do not claim to
        # know why: a concurrent writer and a diagnosis/API drift are
        # indistinguishable from here.
        return {
            **receipt,
            "decision": "refused",
            "reason": "refused_by_api_after_diagnosis",
            "detail": "the row changed between diagnosis and the locked write, or "
            "the API has a precondition this command does not mirror; re-run to see "
            "the current shape",
        }, 2
    return {
        **receipt,
        "decision": "already_attested" if was_attested else "attested",
        OPERATOR_RECOVERY_ATTESTATION_FIELD: attested.get(OPERATOR_RECOVERY_ATTESTATION_FIELD),
    }, 0


def register_click_recovery_command(cli: Any) -> None:
    """Register the ``recover-released-identity-blocked-reservation`` Click command on ``cli``.

    ``--dry-run`` is the default; only ``--attest`` flips the body into the
    write path.  Click enforces option presence before the command body (and
    therefore the repository) is ever reached.
    """

    import click

    @cli.command(
        RELEASED_RESERVATION_RECOVERY_COMMAND,
        help=RECOVER_RELEASED_RESERVATION_HELP,
    )
    @click.option("--journal-root", required=True)
    @click.option("--job-id", default=None)
    @click.option("--dry-run", "dry_run", flag_value=True, default=True, show_default=True)
    @click.option(
        "--attest",
        "dry_run",
        flag_value=False,
        help="Record the operator attestation. Without it nothing is written.",
    )
    def recover_released_identity_blocked_reservation_command(
        journal_root: str,
        job_id: str | None,
        dry_run: bool,
    ) -> None:
        try:
            receipt, exit_code = _recover_released_identity_blocked_reservation(
                journal_root=journal_root,
                job_id=job_id,
                dry_run=dry_run,
            )
        except (FileOrchestrationJournalError, ValueError) as error:
            click.echo(str(error), err=True)
            raise SystemExit(2) from error
        click.echo(json.dumps(receipt, sort_keys=True))
        if exit_code != 0:
            raise SystemExit(exit_code)


def add_argparse_recovery_subparser(subparsers: Any) -> None:
    """Add the ``recover-released-identity-blocked-reservation`` argparse subparser to ``subparsers``."""
    recover_parser = subparsers.add_parser(
        RELEASED_RESERVATION_RECOVERY_COMMAND,
        help=RECOVER_RELEASED_RESERVATION_HELP,
        description=RECOVER_RELEASED_RESERVATION_HELP,
    )
    recover_parser.add_argument("--journal-root", required=True)
    recover_parser.add_argument("--job-id", default=None)
    recover_parser.add_argument("--dry-run", action="store_true", default=True)
    recover_parser.add_argument(
        "--attest",
        action="store_false",
        dest="dry_run",
        help="Record the operator attestation. Without it nothing is written.",
    )


def run_argparse_recovery_command(args: Any) -> int:
    """Dispatch one argparse ``recover-released-identity-blocked-reservation`` invocation."""
    try:
        receipt, exit_code = _recover_released_identity_blocked_reservation(
            journal_root=args.journal_root,
            job_id=args.job_id,
            dry_run=args.dry_run,
        )
    except (FileOrchestrationJournalError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return exit_code
