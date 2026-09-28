"""Shared #2668 operator-verified reserved-bind helpers for the orchestrator CLI.

``bind-reserved-job`` is the sibling of the #1564 ``demote-reserved-job``: it
binds one held forecast cohort master whose job DID run (the comment-less
name-window fallback left it ``ambiguous_fallback_match`` or persistently
``query_unavailable``) to the Slurm master the operator matched in sacct.
Since #2675 it also binds a held FORCING master (``reserved`` /
``submit_result_ambiguous`` with a complete forcing submit identity) whose
``SubmitLine`` carries its own attempt comment and ``--array=0-<n-1>[%k]``;
the optional ``--slurm-user`` / ``--slurm-account`` (sacct ``User`` /
``Account``) are the forcing lane's owner evidence.  The
callable, its ISO-8601 parser, and both entrypoints' registration/dispatch
helpers live here so ``cli.py`` only registers them.  ``--confirm`` and every
input check run before the repository is constructed; the typed journal CAS
(``FileOrchestrationJournalRepository.bind_operator_verified_reserved_job``) is
the single authority for the bind and names every refusal.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

from .accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION
from .chain_types import OrchestratorError
from .file_orchestration_journal import FileOrchestrationJournalError, FileOrchestrationJournalRepository
from .journal_root_authority import verify_journal_root_authority

BIND_RESERVED_JOB_COMMAND = "bind-reserved-job"


def _parse_iso_utc(value: str, option_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{BIND_RESERVED_JOB_COMMAND} {option_name} must be an ISO-8601 timestamp")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{BIND_RESERVED_JOB_COMMAND} {option_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # sacct prints ``Submit`` as local naive time: the operator appends the
        # cluster's UTC offset, never lets this command guess one.
        raise ValueError(f"{BIND_RESERVED_JOB_COMMAND} {option_name} must include a timezone")
    return parsed


def _bind_reserved_job(
    *,
    journal_root: str,
    job_id: str,
    slurm_job_id: str,
    slurm_submit_time: str,
    submit_line: str,
    expected_attempt: int,
    expected_attempt_started_at: str,
    checked_by: str,
    checked_at: str,
    verification_note: str,
    slurm_user: str | None = None,
    slurm_account: str | None = None,
) -> dict[str, object]:
    """One operator-verified bind of a held forecast cohort master (#2668) or forcing master (#2675).

    Shared by both CLI entrypoints.  ``slurm_user`` / ``slurm_account`` are read
    only for a forcing row (its owner evidence); a forecast row ignores them.  Every value is validated here before the
    journal root is resolved or the repository constructed, and re-validated by
    the typed journal CAS before any write.  A named refusal raises
    ``ValueError`` (exit 2) and leaves the journal byte-identical.
    """

    if type(expected_attempt) is not int or expected_attempt < 1:
        raise ValueError(f"{BIND_RESERVED_JOB_COMMAND} --expected-attempt must be a positive integer")
    checked_at_value = _parse_iso_utc(checked_at, "--checked-at")
    expected_anchor_value = _parse_iso_utc(expected_attempt_started_at, "--expected-attempt-started-at")
    submit_time_value = _parse_iso_utc(slurm_submit_time, "--slurm-submit-time")
    for option_name, value in (
        ("--job-id", job_id),
        ("--slurm-job-id", slurm_job_id),
        ("--submit-line", submit_line),
        ("--checked-by", checked_by),
        ("--verification-note", verification_note),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{BIND_RESERVED_JOB_COMMAND} {option_name} must not be blank")
    # The SAME #1943 journal-root authority seam as demote: one expanded,
    # no-follow canonical location for the receipt AND every repository read
    # and write; a hostile root fails typed before any commit.
    display_journal_root = str(verify_journal_root_authority(journal_root, setting="--journal-root"))
    repository = FileOrchestrationJournalRepository(display_journal_root)
    result = repository.bind_operator_verified_reserved_job(
        job_id,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=expected_attempt,
        expected_submission_attempt_started_at=expected_anchor_value,
        slurm_job_id=slurm_job_id,
        slurm_submit_time=submit_time_value,
        submit_line=submit_line,
        checked_by=checked_by,
        checked_at=checked_at_value,
        verification_note=verification_note,
        slurm_user=slurm_user,
        slurm_account=slurm_account,
    )
    receipt = result.receipt
    if result.refusal is not None or receipt is None:
        raise ValueError(
            f"{BIND_RESERVED_JOB_COMMAND}: refused: {result.refusal or 'not_held'}; no journal bytes were written"
        )
    # The authority append is the commit point; a post-commit direct/latest
    # projection fault never turns a committed bind into a failure.
    warnings = [
        {
            "projection": warning.projection,
            "model_id": warning.model_id,
            "error_type": warning.error_type,
            "reason": warning.reason,
        }
        for warning in sorted(receipt.warnings, key=lambda item: (item.projection, item.model_id or ""))
    ]
    payload: dict[str, object] = {
        "command": BIND_RESERVED_JOB_COMMAND,
        "status": "bound_with_warnings" if warnings else "bound",
        "committed": True,
        # Operator strings are the exact normalized/redacted values the durable
        # ``operator_verified_bind`` event recorded; raw arguments (including
        # the SubmitLine, which may carry script paths) are never echoed.
        "journal_root": display_journal_root,
        "job_id": receipt.job_id,
        "status_from": receipt.status_from,
        "status_to": receipt.status_to,
        "reconciliation_source": receipt.reconciliation_source,
        "reconciliation_decision": receipt.reconciliation_decision,
        "matched_slurm_job_id": receipt.matched_slurm_job_id,
        "slurm_accounting_submitted_at": receipt.slurm_accounting_submitted_at,
        "submitline_key": receipt.submitline_key,
        "submission_attempt": receipt.submission_attempt,
        "submission_attempt_started_at": receipt.submission_attempt_started_at,
        "checked_by": receipt.checked_by,
        "checked_at": receipt.checked_at,
        "verification_note": receipt.verification_note,
        "written_record_count": receipt.written_record_count,
        "warnings": warnings,
    }
    if receipt.lane != "forecast":
        # #2675: only a forcing receipt names its lane and the verified array
        # spec, so the forecast receipt keeps its exact pre-change key set.
        payload["lane"] = receipt.lane
        payload["array_spec"] = receipt.array_spec
    return payload


def register_click_bind_command(cli: Any) -> None:
    """Register the ``bind-reserved-job`` Click command on ``cli``.

    ``--confirm`` is a required flag: Click enforces its presence before the
    command body (and therefore the repository) is ever reached.
    """

    import click

    @cli.command(BIND_RESERVED_JOB_COMMAND)
    @click.option("--journal-root", required=True)
    @click.option("--job-id", required=True)
    @click.option("--slurm-job-id", required=True)
    @click.option("--slurm-submit-time", required=True)
    @click.option("--submit-line", required=True)
    @click.option("--expected-attempt", required=True, type=int)
    @click.option("--expected-attempt-started-at", required=True)
    @click.option("--checked-by", required=True)
    @click.option("--checked-at", required=True)
    @click.option("--verification-note", required=True)
    @click.option("--slurm-user", default=None, help="Forcing rows: the master's sacct User.")
    @click.option("--slurm-account", default=None, help="Forcing rows: the master's sacct Account.")
    @click.option("--confirm", is_flag=True, required=True)
    def bind_reserved_job(
        journal_root: str,
        job_id: str,
        slurm_job_id: str,
        slurm_submit_time: str,
        submit_line: str,
        expected_attempt: int,
        expected_attempt_started_at: str,
        checked_by: str,
        checked_at: str,
        verification_note: str,
        slurm_user: str | None,
        slurm_account: str | None,
        confirm: bool,
    ) -> None:
        del confirm  # required + is_flag enforces presence before this body runs.
        try:
            receipt = _bind_reserved_job(
                journal_root=journal_root,
                job_id=job_id,
                slurm_job_id=slurm_job_id,
                slurm_submit_time=slurm_submit_time,
                submit_line=submit_line,
                expected_attempt=expected_attempt,
                expected_attempt_started_at=expected_attempt_started_at,
                checked_by=checked_by,
                checked_at=checked_at,
                verification_note=verification_note,
                slurm_user=slurm_user,
                slurm_account=slurm_account,
            )
            click.echo(json.dumps(receipt, sort_keys=True))
        except (FileOrchestrationJournalError, ValueError) as error:
            click.echo(str(error), err=True)
            raise SystemExit(2) from error
        except OrchestratorError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error


def add_argparse_bind_subparser(subparsers: Any) -> None:
    """Add the ``bind-reserved-job`` argparse subparser to ``subparsers``."""

    parser = subparsers.add_parser(BIND_RESERVED_JOB_COMMAND)
    for option in (
        "--journal-root",
        "--job-id",
        "--slurm-job-id",
        "--slurm-submit-time",
        "--submit-line",
        "--expected-attempt-started-at",
        "--checked-by",
        "--checked-at",
        "--verification-note",
    ):
        parser.add_argument(option, required=True)
    parser.add_argument("--expected-attempt", required=True, type=int)
    # #2675: the forcing lane's owner evidence (sacct User / Account).
    parser.add_argument("--slurm-user", default=None)
    parser.add_argument("--slurm-account", default=None)
    parser.add_argument("--confirm", action="store_true")


def run_argparse_bind_command(args: Any) -> int:
    """Dispatch one argparse ``bind-reserved-job`` invocation.

    ``--confirm`` is enforced here, before the repository is constructed.
    """

    try:
        if not args.confirm:
            raise ValueError(f"{BIND_RESERVED_JOB_COMMAND} requires --confirm (non-interactive confirmation)")
        print(
            json.dumps(
                _bind_reserved_job(
                    journal_root=args.journal_root,
                    job_id=args.job_id,
                    slurm_job_id=args.slurm_job_id,
                    slurm_submit_time=args.slurm_submit_time,
                    submit_line=args.submit_line,
                    expected_attempt=args.expected_attempt,
                    expected_attempt_started_at=args.expected_attempt_started_at,
                    checked_by=args.checked_by,
                    checked_at=args.checked_at,
                    verification_note=args.verification_note,
                    slurm_user=args.slurm_user,
                    slurm_account=args.slurm_account,
                ),
                sort_keys=True,
            )
        )
        return 0
    except (FileOrchestrationJournalError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except OrchestratorError as error:
        print(f"{error.error_code}: {error.message}", file=sys.stderr)
        return 1
