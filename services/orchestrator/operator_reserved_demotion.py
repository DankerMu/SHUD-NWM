"""Shared #1564 operator-reserved-demotion helpers for the orchestrator CLI.

The CLI's demote-reserved-job body is shared verbatim by the Click and
argparse entrypoints.  The callable, its ISO-8601 parser, and both
entrypoints' registration/dispatch helpers live here so the CLI module stays
under the large-file guard while keeping the one shared public seam
``_demote_reserved_job`` re-exported from :mod:`services.orchestrator.cli`
for the existing tests.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from packages.common.safe_fs import SafeFilesystemError, verify_directory_no_follow

from .accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION
from .chain_types import OrchestratorError
from .file_orchestration_journal import FileOrchestrationJournalError, FileOrchestrationJournalRepository


def _parse_iso_utc(value: str, option_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"demote-reserved-job {option_name} must be an ISO-8601 timestamp")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"demote-reserved-job {option_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"demote-reserved-job {option_name} must include a timezone")
    return parsed


def _demote_reserved_job(
    *,
    journal_root: str,
    job_id: str,
    expected_attempt: int,
    expected_attempt_started_at: str,
    checked_by: str,
    checked_at: str,
    verification_note: str,
) -> dict[str, object]:
    """One operator-verified demotion of a comment-unobservable reserved master (#1564).

    The callable body is shared by both CLI entrypoints.  ``--confirm`` is
    enforced by the entrypoints (missing confirmation fails before this
    function constructs the repository), and every value is re-validated by the
    typed journal CAS before any write.
    """

    if type(expected_attempt) is not int or expected_attempt < 1:
        raise ValueError("demote-reserved-job --expected-attempt must be a positive integer")
    checked_at_value = _parse_iso_utc(checked_at, "--checked-at")
    expected_anchor_value = _parse_iso_utc(expected_attempt_started_at, "--expected-attempt-started-at")
    if not checked_by.strip():
        raise ValueError("demote-reserved-job --checked-by must not be blank")
    if not verification_note.strip():
        raise ValueError("demote-reserved-job --verification-note must not be blank")
    # One journal-root authority (#1564 D10): the receipt locator is derived
    # from the same safe-FS expansion/no-follow canonicalization the repository
    # I/O uses for every read and write, never bare Path.resolve() on operator
    # input.  A hostile root (symlink loop) fails typed and pre-commit; a
    # literal unexpanded ``~`` expands to the exact authority location.  All
    # fallible canonicalization happens before the authority commit.
    try:
        display_journal_root = str(verify_directory_no_follow(Path(journal_root)))
    except (OSError, SafeFilesystemError) as error:
        raise OrchestratorError(
            "FILE_JOURNAL_INVALID_ROOT",
            "journal root failed safe filesystem verification",
            {"error_type": type(error).__name__},
        ) from error
    # The repository I/O root is the SAME expanded authority location: a literal
    # unexpanded ``~`` must never make repository reads/writes diverge from the
    # verified receipt root.
    repository = FileOrchestrationJournalRepository(display_journal_root)
    if repository.get_pipeline_job(job_id) is None:
        raise ValueError(f"demote-reserved-job: pipeline job not found: {job_id}")
    receipt = repository.demote_operator_verified_reserved_job(
        job_id,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=expected_attempt,
        expected_submission_attempt_started_at=expected_anchor_value,
        checked_by=checked_by,
        checked_at=checked_at_value,
        verification_note=verification_note,
    )
    if receipt is None:
        raise ValueError(
            "demote-reserved-job: compare-and-swap refused (stale or mismatched durable state); "
            "no journal bytes were written"
        )
    # The authority append is the commit point; a post-commit direct/latest
    # projection fault never turns a committed demotion into a failure.
    warnings = [
        {
            "projection": warning.projection,
            "model_id": warning.model_id,
            "error_type": warning.error_type,
            "reason": warning.reason,
        }
        for warning in sorted(receipt.warnings, key=lambda item: (item.projection, item.model_id or ""))
    ]
    return {
        "command": "demote-reserved-job",
        "status": "demoted_with_warnings" if warnings else "demoted",
        "committed": True,
        # The receipt's operator strings are the exact normalized/redacted
        # values the durable audit event recorded; raw CLI arguments are never
        # echoed back, so success stdout cannot leak a redacted secret.
        "journal_root": display_journal_root,
        "job_id": receipt.job_id,
        "status_from": receipt.status_from,
        "status_to": receipt.status_to,
        "reconciliation_decision": receipt.reconciliation_decision,
        "submission_attempt": receipt.submission_attempt,
        "submission_attempt_started_at": receipt.submission_attempt_started_at,
        "checked_by": receipt.checked_by,
        "checked_at": receipt.checked_at,
        "verification_note": receipt.verification_note,
        "written_record_count": receipt.written_record_count,
        "warnings": warnings,
    }


def register_click_demote_command(cli: Any) -> None:
    """Register the ``demote-reserved-job`` Click command on ``cli``.

    ``--confirm`` is a required flag: Click enforces its presence before the
    command body (and therefore the repository) is ever reached.
    """

    import click

    @cli.command("demote-reserved-job")
    @click.option("--journal-root", required=True)
    @click.option("--job-id", required=True)
    @click.option("--expected-attempt", required=True, type=int)
    @click.option("--expected-attempt-started-at", required=True)
    @click.option("--checked-by", required=True)
    @click.option("--checked-at", required=True)
    @click.option("--verification-note", required=True)
    @click.option("--confirm", is_flag=True, required=True)
    def demote_reserved_job(
        journal_root: str,
        job_id: str,
        expected_attempt: int,
        expected_attempt_started_at: str,
        checked_by: str,
        checked_at: str,
        verification_note: str,
        confirm: bool,
    ) -> None:
        del confirm  # required + is_flag enforces presence before this body runs.
        try:
            receipt = _demote_reserved_job(
                journal_root=journal_root,
                job_id=job_id,
                expected_attempt=expected_attempt,
                expected_attempt_started_at=expected_attempt_started_at,
                checked_by=checked_by,
                checked_at=checked_at,
                verification_note=verification_note,
            )
            click.echo(json.dumps(receipt, sort_keys=True))
        except (FileOrchestrationJournalError, ValueError) as error:
            click.echo(str(error), err=True)
            raise SystemExit(2) from error
        except OrchestratorError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error


def add_argparse_demote_subparser(subparsers: Any) -> None:
    """Add the ``demote-reserved-job`` argparse subparser to ``subparsers``."""
    demote_parser = subparsers.add_parser("demote-reserved-job")
    demote_parser.add_argument("--journal-root", required=True)
    demote_parser.add_argument("--job-id", required=True)
    demote_parser.add_argument("--expected-attempt", required=True, type=int)
    demote_parser.add_argument("--expected-attempt-started-at", required=True)
    demote_parser.add_argument("--checked-by", required=True)
    demote_parser.add_argument("--checked-at", required=True)
    demote_parser.add_argument("--verification-note", required=True)
    demote_parser.add_argument("--confirm", action="store_true")


def run_argparse_demote_command(args: Any) -> int:
    """Dispatch one argparse ``demote-reserved-job`` invocation.

    ``--confirm`` is enforced here, before the repository is constructed.
    """
    try:
        if not args.confirm:
            raise ValueError(
                "demote-reserved-job requires --confirm (non-interactive confirmation)"
            )
        print(
            json.dumps(
                _demote_reserved_job(
                    journal_root=args.journal_root,
                    job_id=args.job_id,
                    expected_attempt=args.expected_attempt,
                    expected_attempt_started_at=args.expected_attempt_started_at,
                    checked_by=args.checked_by,
                    checked_at=args.checked_at,
                    verification_note=args.verification_note,
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
