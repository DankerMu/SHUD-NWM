"""The three runs of the model succession tool: the dry-run, the apply and the abort.

Part of ``scripts/node22_model_succession.py`` (the entry point).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any

from packages.common import succession_receipt as succession
from scripts.merged_registry_publish.model import APPLY_RECEIPT_NAME as PUBLISH_APPLY_NAME
from scripts.model_succession import copyback, scheduler, systemd, tools
from scripts.model_succession import plan as planning
from scripts.model_succession.model import (
    ABORT_SCHEMA_VERSION,
    CLONE_APPLY_NAME,
    FAILURE_SCHEMA_VERSION,
    PLAN_NAME,
    PUBLISH_STATE_MANIFESTS_DIFFER,
    RUNBOOK,
    SERVICE_UNIT,
    STEP_RECEIPT_SCHEMA_VERSION,
    STEPS,
    STEPS_BEFORE_TIMER,
    STEPS_NEEDING_STOPPED_SCHEDULER,
    TIMER_LEFT_STOPPED,
    TIMER_UNIT,
    HardStop,
    Inputs,
    ModelSuccessionRefusal,
    Settings,
    StepFailure,
    TimerNotStopped,
    completed_steps,
    existing_receipts,
    manifests_differ_text,
    read_json,
    receipt_header,
    write_stamped_receipt,
)

STEP_FUNCTIONS: dict[str, Callable[[Settings, Inputs], dict[str, Any]]] = {
    "copyback": copyback.run,
    "preflight": tools.preflight,
    "begin": scheduler.begin,
    "clone": tools.clone,
    "publish": tools.publish,
    "refresh": scheduler.refresh,
    "finish": scheduler.finish,
}


def run_step(settings: Settings, inputs: Inputs, step: str) -> str:
    """Run one step unless its receipt exists; ``skipped`` or ``completed``.

    A step is refused when the receipt of the step before it is missing, and,
    for the steps that change what the scheduler reads, when the scheduler
    timer or service is running.  Both are checked before the step does anything.
    """

    target = settings.step_receipt(step)
    if os.path.lexists(target):
        return "skipped"
    index = STEPS.index(step)
    if index:
        previous = settings.step_receipt(STEPS[index - 1])
        if not os.path.lexists(previous):
            raise StepFailure(
                f"Refused: the {step} step requires the receipt of the {STEPS[index - 1]} step, which is missing: "
                f"{previous}. The {step} step wrote nothing."
            )
    if step in STEPS_NEEDING_STOPPED_SCHEDULER:
        systemd.require_scheduler_stopped(step)
    facts = STEP_FUNCTIONS[step](settings, inputs)
    header = receipt_header(settings, STEP_RECEIPT_SCHEMA_VERSION)
    # The step's own facts never replace the common fields.
    succession.write_receipt(target, {**facts, **header, "step": step, "outcome": "completed"})
    return "completed"


def _checked(settings: Settings) -> tuple[Inputs, bool]:
    """Everything that is checked before any step, in the dry-run and in the apply."""

    planning.refuse_database()
    planning.refuse_closed(settings)
    # The command line first: a different plan is named as such, whatever else it would be refused for.
    planning.compare_with_plan(settings, None)
    inputs = planning.check_inputs(settings)
    return inputs, planning.compare_with_plan(settings, inputs)


def _timer_stopped_by_this_tool(settings: Settings, error: Exception) -> bool | None:
    if isinstance(error, TimerNotStopped):
        return False
    try:
        record = scheduler.read_timer_record(settings)
    except StepFailure:
        return None
    return bool(record and record["timer_was_active"])


def _record_failure(settings: Settings, step: str, error: Exception) -> dict[str, Any]:
    hard_stop = isinstance(error, HardStop)
    known = isinstance(error, StepFailure | ModelSuccessionRefusal)
    reason = str(error) if known else f"{type(error).__name__}: {error}"
    before_timer = step in STEPS_BEFORE_TIMER
    if hard_stop:
        ways_on = [f"Hard stop: do not run this command again; continue by hand from the runbook ({RUNBOOK})."]
    else:
        ways_on = [
            "Remove the cause and run the same command again: completed steps are skipped.",
            "Or give the succession up: the same command with --abort --confirm-timer-start instead of --apply.",
        ]
    receipt: dict[str, Any] = {
        **receipt_header(settings, FAILURE_SCHEMA_VERSION),
        "step": step,
        "outcome": "failed",
        "hard_stop": hard_stop,
        "reason": reason,
        "completed_steps": completed_steps(settings),
        "receipts": existing_receipts(settings),
        # Observed now, not assumed.
        "observed_unit_states": {
            TIMER_UNIT: systemd.observed_state(TIMER_UNIT),
            SERVICE_UNIT: systemd.observed_state(SERVICE_UNIT),
        },
        "timer_touched_by_this_tool": not before_timer,
        "timer_stopped_by_this_tool": False if before_timer else _timer_stopped_by_this_tool(settings, error),
        "timer_started_by_this_tool": False,
        "timer_note": (
            f"The {step} step runs before the timer is touched: this tool did not stop or start it."
            if before_timer
            else "This tool does not start the timer after a failure; it stays as observed above until the "
            "succession finishes or is aborted."
        ),
        "ways_on": ways_on,
    }
    try:
        receipt["failure_receipt"] = str(write_stamped_receipt(settings, "succession-failed", receipt))
    except OSError as write_error:
        receipt["failure_receipt"] = f"could NOT be written: {write_error}"
    print(f"Model succession {settings.plan.succession_id!r} FAILED in step {step}: {reason}", file=sys.stderr)
    print(f"Timer: {receipt['timer_note']} Observed: {receipt['observed_unit_states']}", file=sys.stderr)
    for way in ways_on:
        print(f"  - {way}", file=sys.stderr)
    print(f"Failure receipt: {receipt['failure_receipt']}", file=sys.stderr)
    return receipt


def apply(settings: Settings) -> tuple[int, dict[str, Any]]:
    inputs, planned = _checked(settings)
    planning.refuse_other_timer_holder(settings)
    if not planned:
        planning.write_plan(settings, inputs)
    steps: dict[str, str] = {}
    for step in STEPS:
        try:
            steps[step] = run_step(settings, inputs, step)
        except Exception as error:  # noqa: BLE001 - every failure of a step is recorded; a kill is not caught
            return 1, {"outcome": "failed", "steps": steps, "failure": _record_failure(settings, step, error)}
    try:
        timer_action = read_json(settings.step_receipt(STEPS[-1])).get("timer_action")
    except (OSError, ValueError) as error:
        timer_action = f"unknown ({settings.step_receipt(STEPS[-1])} cannot be read: {error})"
    report: dict[str, Any] = {
        "succession_id": settings.plan.succession_id,
        "outcome": "completed",
        "steps": steps,
        "timer_action": timer_action,
        "receipt_directory": str(settings.directory),
    }
    if timer_action == TIMER_LEFT_STOPPED:
        report["timer_note"] = (
            f"{TIMER_UNIT} was left stopped: it was not active when this succession began, so the finish step did "
            "not start it. Start it by hand if the scheduler is meant to run."
        )
        print(f"Model succession {settings.plan.succession_id!r} completed. {report['timer_note']}", file=sys.stderr)
    return 0, report


def dry_run(settings: Settings) -> tuple[int, dict[str, Any]]:
    """Report what an apply would do.  Writes nothing and issues only ``is-active`` queries."""

    inputs, planned = _checked(settings)
    done = completed_steps(settings)
    steps: dict[str, Any] = {step: {"status": "completed"} for step in done}
    packages, refusals = copyback.report(settings, inputs)
    try:
        planning.refuse_other_timer_holder(settings)
    except ModelSuccessionRefusal as error:
        refusals.insert(0, str(error))
    if "copyback" not in done:
        steps["copyback"] = {"packages": packages}
    if "preflight" not in done:
        if all(package["outcome"] == copyback.ALREADY_PRESENT for package in packages):
            steps["preflight"], tool_refusals = tools.preview(settings, inputs)
            refusals.extend(tool_refusals)
        else:
            steps["preflight"] = {
                "status": "needs copyback",
                "note": "The clone gate and the publisher's package checks need the packages on the compute "
                "store; the apply runs both dry-runs in preflight, before the timer is stopped.",
            }
    states = {unit: systemd.observed_state(unit) for unit in (TIMER_UNIT, SERVICE_UNIT)}
    refusals.extend(f"{unit}: {state}" for unit, state in states.items() if state.startswith("unknown"))
    if "begin" not in done:
        steps["begin"] = {
            "unit_states_now": states,
            "would": f"record the timer state, stop {TIMER_UNIT}, wait up to {settings.pass_wait_seconds:.0f} s "
            f"for {SERVICE_UNIT} to end by itself",
        }
    for step, would in (("clone", "run the clone apply"), ("publish", "run the publish apply")):
        steps.setdefault(step, {"would": would})
    for step in ("refresh", "finish"):
        steps.setdefault(step, {"status": "not predicted"})
    report = {
        "succession_id": settings.plan.succession_id,
        "dry_run": True,
        "plan": settings.plan.record(),
        "plan_json": "present and equal to this command line" if planned else "absent: the first --apply writes it",
        "provision_apply_receipt": inputs.provision_receipt,
        "new_rows_registry": inputs.new_rows_registry,
        "unit_states_now": states,
        "steps": {step: steps[step] for step in STEPS},
        "would_be_refused": refusals,
    }
    return (1 if refusals else 0), report


def _publish_state(settings: Settings) -> str:
    # First: neither "published" nor "not published" can be said of two manifests that differ.
    if planning.manifests_differ(settings):
        return PUBLISH_STATE_MANIFESTS_DIFFER
    if os.path.lexists(settings.directory / PUBLISH_APPLY_NAME):
        return "published"
    return "published_without_receipt" if planning.published_without_receipt(settings) else "not_published"


def _abort_meaning(settings: Settings, publish_state: str, timer_was_active: bool | None) -> list[str]:
    directory = settings.directory
    timer = (
        ["The scheduler timer was not active when the succession began and is left as it is: nothing starts it."]
        if timer_was_active is False
        else []
    )
    by_hand = (
        "The remaining steps (the provider refresh and the final checks) must be finished by hand from the "
        f"runbook ({RUNBOOK})."
    )
    if publish_state == PUBLISH_STATE_MANIFESTS_DIFFER:
        meaning = [manifests_differ_text(settings), *timer]
        if timer_was_active:
            meaning.append(
                f"{TIMER_UNIT} was active when the succession began and is NOT started by this abort: started now, "
                "the scheduler would only produce failures. Start it by hand once both manifests are the same "
                "bytes again."
            )
        return meaning
    if publish_state == "published":
        return [f"The publish completed: the new models are live in both manifests. {by_hand}", *timer]
    if publish_state == "published_without_receipt":
        return [
            f"The publish is in effect although its receipt {directory / PUBLISH_APPLY_NAME} was not written: the "
            f"new models are live in both manifests. {by_hand}",
            *timer,
        ]
    if timer_was_active is False:
        meaning = ["The publish did not complete.", *timer]
    else:
        meaning = ["The publish did not complete: the scheduler keeps running the old models."]
    if any(directory.glob("publish-apply-failed-*.json")):
        meaning.append(
            "A publish attempt failed: compare the sha256 of both manifests before trusting that, as the runbook "
            f"describes ({RUNBOOK})."
        )
    if os.path.lexists(directory / CLONE_APPLY_NAME):
        meaning.append(
            "Clone rows of this succession stay in both state indexes. The scheduler uses the earliest clone row "
            "of a model as its cutover time, so a later succession of the same new model_id with a later cutover "
            "time would still take effect at this one."
        )
    return meaning


def abort(settings: Settings, *, confirm_timer_start: bool) -> tuple[int, dict[str, Any]]:
    """Close the succession; with the confirmation, start the timer when it was active at ``begin``."""

    planning.refuse_database()
    planning.refuse_closed(settings)
    if not planning.compare_with_plan(settings, None):
        raise ModelSuccessionRefusal(
            f"Refused: {settings.directory / PLAN_NAME} does not exist; no apply of succession "
            f"{settings.plan.succession_id!r} ever started, so there is nothing to abort."
        )
    done = completed_steps(settings)
    if "finish" in done:
        raise ModelSuccessionRefusal(
            f"Refused: succession {settings.plan.succession_id!r} has finished ({settings.step_receipt('finish')})."
        )
    try:
        record = scheduler.read_timer_record(settings)
    except StepFailure as error:
        raise ModelSuccessionRefusal(str(error)) from error
    timer_was_active = record["timer_was_active"] if record else None
    publish_state = _publish_state(settings)
    start_timer = bool(timer_was_active) and publish_state != PUBLISH_STATE_MANIFESTS_DIFFER
    if record is None:
        action = "none: the begin step never recorded or stopped the timer"
    elif start_timer:
        action = f"start {TIMER_UNIT}: it was active when the succession began"
    elif timer_was_active:
        action = (
            f"none: {TIMER_UNIT} was active when the succession began and is NOT started while the two registry "
            "manifests differ; start it by hand once they are the same bytes again"
        )
    else:
        action = "none: the timer was not active when the succession began"
    report: dict[str, Any] = {
        "succession_id": settings.plan.succession_id,
        "completed_steps": done,
        "receipts": existing_receipts(settings),
        "timer_was_active_at_begin": timer_was_active,
        "publish_state": publish_state,
        "what_this_state_means": _abort_meaning(settings, publish_state, timer_was_active),
    }
    if not confirm_timer_start:
        states = {unit: systemd.observed_state(unit) for unit in (TIMER_UNIT, SERVICE_UNIT)}
        note = "Report only: --abort without --confirm-timer-start changes nothing."
        report.update(aborted=False, timer_action_on_confirm=action, unit_states_now=states, note=note)
        return 0, report
    if start_timer:
        try:
            systemd.run_checked("start", TIMER_UNIT)
        except StepFailure as error:
            raise ModelSuccessionRefusal(f"The abort did not happen: {error} No abort receipt was written.") from error
    report = {
        **receipt_header(settings, ABORT_SCHEMA_VERSION),
        **report,
        "aborted": True,
        "timer_action": action,
        "unit_states_now": {unit: systemd.observed_state(unit) for unit in (TIMER_UNIT, SERVICE_UNIT)},
    }
    try:
        report["abort_receipt"] = str(write_stamped_receipt(settings, "abort", report))
    except OSError as error:
        raise ModelSuccessionRefusal(
            f"The timer action was taken ({action}) but the abort receipt could NOT be written: {error}. The "
            "succession is not closed."
        ) from error
    return 0, report
