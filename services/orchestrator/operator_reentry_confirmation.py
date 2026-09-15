"""``confirm-operator-reentry`` (#1555 + #1768): record a pinned one-shot re-entry confirmation.

Two completed-skip fail-stops say ``manual_retry_required: true`` but the manual
retry marker cannot reach them (``record_manual_repair`` refuses terminal-success
rows, and the completed skip returns before the marker is read):

* ``blocked_journal_predecessor_identity_quarantine`` -- the §8.7 breaker;
* ``blocked_strict_warm_start_init_state_mismatch`` -- the strict warm-start budget.

This command writes one ``forecast_cycle`` pipeline event of the dedicated type
``operator_reentry_confirmation``.  The scheduler lets exactly one retry through
while the confirmation's ``pin`` equals the live value the rerun will move (the
model-level quarantine rerun count for the breaker, the model-level budget
re-entry count for the budget), then the fail-stop takes over again by itself.
The writer can not see whether the budget is exhausted (#2400 residual): a
budget confirmation written early stays armed until consumed, so the runbook
confines it to a target the newest scheduler pass evidence file lists as
budget-blocked, with no rerun in flight.

Dry run by default; only ``--attest`` writes.  Refusals are write-free, print a
receipt naming the failed precondition, and exit 2.
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any

from workers.data_adapters.base import parse_cycle_time

from . import scheduler_generation as _scheduler_generation
from .chain_types import OrchestratorError
from .file_orchestration_journal import (
    OPERATOR_REENTRY_CONFIRMATION_EVENT,
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
    _cycle_id_for_file_source,
)
from .journal_root_authority import journal_root_refusal_line, verify_journal_root_authority
from .scheduler_state import _format_utc

CONFIRM_OPERATOR_REENTRY_COMMAND = "confirm-operator-reentry"
BREAKER_DECISION = "blocked_journal_predecessor_identity_quarantine"
BUDGET_DECISION = "blocked_strict_warm_start_init_state_mismatch"
REENTRY_DECISIONS = (BREAKER_DECISION, BUDGET_DECISION)

CONFIRM_OPERATOR_REENTRY_HELP = (
    "Record a pinned one-shot operator re-entry confirmation (#1555/#1768) for one "
    "(source, cycle, model) blocked by the §8.7 quarantine breaker or the strict "
    "warm-start retry budget. --pin must be the live value the rerun moves: the "
    "model's quarantine rerun count (breaker, with --recorded-init-state-id; see "
    "live.quarantine_rerun_count in the dry-run receipt) or the model's budget "
    "re-entry count (budget; see live.budget_reentry_count in the dry-run receipt). "
    "The scheduler re-enters "
    "once; the rerun moves the value when it is accepted for submission (whatever "
    "its outcome) and the fail-stop re-engages. A budget confirmation written "
    "before the budget is exhausted stays armed until consumed (#2400): only "
    "confirm a target the newest scheduler pass evidence file lists as "
    "blocked_strict_warm_start_init_state_mismatch, never while its rerun is in "
    "flight; if written in error, stop and escalate instead of writing another. Dry run "
    "unless --attest. Runbook: docs/runbooks/node22-control-plane-manual-recovery.md"
)


def confirm_operator_reentry(
    *,
    journal_root: str,
    source_id: str,
    cycle_time: str,
    model_id: str,
    decision: str,
    pin: int,
    operator: str,
    reason: str,
    recorded_init_state_id: str | None,
    dry_run: bool,
) -> tuple[dict[str, Any], int]:
    verified_root = verify_journal_root_authority(journal_root, setting="--journal-root")
    required = {
        "source_id": source_id,
        "cycle_time": cycle_time,
        "model_id": model_id,
        "operator": operator,
        "reason": reason,
    }
    if decision == BREAKER_DECISION:
        required["recorded_init_state_id"] = recorded_init_state_id
    blank = sorted(name for name, value in required.items() if not str(value or "").strip())
    if blank:
        return _refused("required_argument_blank", fields=blank), 2
    if decision not in REENTRY_DECISIONS:
        return _refused("decision_not_reentry_eligible", decision=decision), 2
    try:
        parsed_cycle_time = parse_cycle_time(cycle_time)
    except (TypeError, ValueError):
        return _refused("cycle_time_invalid"), 2
    model_id = model_id.strip()
    target = {
        "source_id": source_id.strip(),
        "cycle_time": _format_utc(parsed_cycle_time),
        "model_id": model_id,
        "decision": decision,
    }
    if type(pin) is not int or pin < 0:
        return _refused("pin_invalid", target=target, pin=pin), 2

    repository = FileOrchestrationJournalRepository(verified_root)
    identity_query = {"source_id": target["source_id"], "cycle_time": parsed_cycle_time, "model_id": model_id}
    if repository.completed_pipeline_init_state_identity(**identity_query) is None:
        return _refused("completed_identity_absent", target=target, pin=pin), 2

    live: dict[str, Any] | None = None
    details: dict[str, Any] = {"model_id": model_id, "decision": decision, "pin": pin}
    if decision == BREAKER_DECISION:
        token = str(recorded_init_state_id).strip()
        live_token = repository.completed_pipeline_init_state_id(**identity_query)
        # Engagement is judged on the LIVE token, exactly as the scheduler judges it.
        occurrences = (
            repository.completed_pipeline_init_state_id_occurrences(**identity_query, init_state_id=live_token)
            if live_token
            else 0
        )
        # The pin is the model-level rerun count, not ``occurrences``: it moves when
        # a stamped rerun is accepted, whatever its outcome or recorded token.
        rerun_count = repository.quarantine_rerun_count(**identity_query)
        live = {"occurrences": occurrences, "quarantine_rerun_count": rerun_count}
        if not _scheduler_generation.journal_identity_quarantine_breaker_engaged(occurrences):
            return _refused("breaker_not_engaged", target=target, pin=pin, live=live), 2
        # The token stays an intent precondition: the operator confirms the
        # stale lineage the breaker actually reports.
        if live_token != token:
            return _refused("recorded_init_state_id_mismatch", target=target, pin=pin, live=live), 2
        if rerun_count != pin:
            return _refused("pin_mismatch", target=target, pin=pin, live=live), 2
    else:
        # Round 3 r3-02: the budget pin is the model-level budget re-entry count,
        # the same journal-direct count the scheduler compares it with; the
        # stage-scoped attempt is not (a re-entry minted under another job-id
        # prefix leaves it unmoved).
        reentry_count = repository.budget_reentry_count(**identity_query)
        live = {"budget_reentry_count": reentry_count}
        if reentry_count != pin:
            return _refused("pin_mismatch", target=target, pin=pin, live=live), 2

    receipt: dict[str, Any] = {"decision": "dry_run", "target": target, "pin": pin, "live": live}
    if dry_run:
        return receipt, 0
    request_id = uuid.uuid4().hex
    details.update({"operator": operator.strip(), "reason": reason.strip(), "request_id": request_id})
    if decision == BREAKER_DECISION:
        details["recorded_init_state_id"] = str(recorded_init_state_id).strip()
    repository.insert_pipeline_event(
        entity_type="forecast_cycle",
        entity_id=_cycle_id_for_file_source(target["source_id"], parsed_cycle_time),
        event_type=OPERATOR_REENTRY_CONFIRMATION_EVENT,
        status_from=None,
        status_to="confirmed",
        details=details,
    )
    return {**receipt, "decision": "recorded", "request_id": request_id}, 0


def _refused(reason: str, **fields: Any) -> dict[str, Any]:
    return {"decision": "refused", "reason": reason, **fields}


def register_click_confirm_reentry_command(cli: Any) -> None:
    """Register ``confirm-operator-reentry`` on the Click group ``cli``."""

    import click

    @cli.command(CONFIRM_OPERATOR_REENTRY_COMMAND, help=CONFIRM_OPERATOR_REENTRY_HELP)
    @click.option("--journal-root", required=True)
    @click.option("--source-id", required=True)
    @click.option("--cycle-time", required=True)
    @click.option("--model-id", required=True)
    @click.option("--decision", required=True, type=click.Choice(REENTRY_DECISIONS))
    @click.option("--pin", required=True, type=int)
    @click.option("--recorded-init-state-id", default=None)
    @click.option("--operator", required=True)
    @click.option("--reason", required=True)
    @click.option("--dry-run", "dry_run", flag_value=True, default=True, show_default=True)
    @click.option(
        "--attest", "dry_run", flag_value=False, help="Record the confirmation. Without it nothing is written."
    )
    def confirm_operator_reentry_command(**options: Any) -> None:
        try:
            receipt, exit_code = confirm_operator_reentry(**options)
        except OrchestratorError as error:
            # #1955: an invalid journal root is a typed refusal, not a traceback.
            click.echo(journal_root_refusal_line(error), err=True)
            raise SystemExit(2) from error
        except FileOrchestrationJournalError as error:
            click.echo(error.reason, err=True)
            raise SystemExit(2) from error
        click.echo(json.dumps(receipt, sort_keys=True))
        if exit_code != 0:
            raise SystemExit(exit_code)


def add_argparse_confirm_reentry_subparser(subparsers: Any) -> None:
    """Add the ``confirm-operator-reentry`` argparse subparser to ``subparsers``."""

    parser = subparsers.add_parser(
        CONFIRM_OPERATOR_REENTRY_COMMAND,
        help=CONFIRM_OPERATOR_REENTRY_HELP,
        description=CONFIRM_OPERATOR_REENTRY_HELP,
    )
    parser.add_argument("--journal-root", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--cycle-time", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--decision", required=True, choices=REENTRY_DECISIONS)
    parser.add_argument("--pin", required=True, type=int)
    parser.add_argument("--recorded-init-state-id", default=None)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument(
        "--attest",
        action="store_false",
        dest="dry_run",
        help="Record the confirmation. Without it nothing is written.",
    )


def run_argparse_confirm_reentry_command(args: Any) -> int:
    """Dispatch one argparse ``confirm-operator-reentry`` invocation."""

    try:
        receipt, exit_code = confirm_operator_reentry(
            journal_root=args.journal_root,
            source_id=args.source_id,
            cycle_time=args.cycle_time,
            model_id=args.model_id,
            decision=args.decision,
            pin=args.pin,
            operator=args.operator,
            reason=args.reason,
            recorded_init_state_id=args.recorded_init_state_id,
            dry_run=args.dry_run,
        )
    except OrchestratorError as error:
        print(journal_root_refusal_line(error), file=sys.stderr)
        return 2
    except FileOrchestrationJournalError as error:
        print(error.reason, file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return exit_code
