"""The steps that touch the scheduler units: ``begin``, ``refresh`` and ``finish``.

Part of ``scripts/node22_model_succession.py`` (the entry point).  ``begin``
stops the scheduler timer and waits for a running pass to end by itself;
``finish`` starts the timer again when it was active at ``begin``.  Nothing
here stops or kills the scheduler service.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from packages.common import succession_receipt as succession
from scripts.model_succession import systemd
from scripts.model_succession.model import (
    REFRESH_START_TIMEOUT_SECONDS,
    REFRESH_UNIT,
    SERVICE_UNIT,
    STEPS,
    TIMER_LEFT_STOPPED,
    TIMER_RECORD_CUT_OFF,
    TIMER_RECORD_NAME,
    TIMER_RECORD_SCHEMA_VERSION,
    TIMER_STARTED,
    TIMER_UNIT,
    Inputs,
    ModelSuccessionRefusal,
    Settings,
    StepFailure,
    TimerNotStopped,
    read_json,
    receipt_header,
    utc_now,
    utc_text,
)
from scripts.model_succession.plan import manifest_model_ids, refuse_other_timer_holder

POLL_SECONDS = 15.0
REFRESH_TOTALS = ("refused", "added", "removed", "package_changed")
# Replaced by the tests, which do not wait.
sleep = time.sleep
monotonic = time.monotonic


def read_timer_record(settings: Settings) -> dict[str, Any] | None:
    """``timer-before-stop.json`` as written at ``begin``; None when ``begin`` never got that far."""

    path = settings.directory / TIMER_RECORD_NAME
    if not os.path.lexists(path):
        return None
    try:
        record = read_json(path)
    except (OSError, ValueError) as error:
        raise StepFailure(
            f"{path} exists but cannot be read ({error}); the timer was not touched. {TIMER_RECORD_CUT_OFF}"
        ) from error
    if not isinstance(record.get("timer_was_active"), bool):
        raise StepFailure(f"{path} does not record timer_was_active; the timer was not touched. {TIMER_RECORD_CUT_OFF}")
    return record


def begin(settings: Settings, _inputs: Inputs) -> dict[str, Any]:
    record = read_timer_record(settings)
    if record is None:
        # Again, minutes after the check before any step: another succession may have taken the timer during
        # this one's copyback and preflight, and this one would then record the timer it stopped as inactive.
        try:
            refuse_other_timer_holder(
                settings, wrote="The begin step did not touch the timer and wrote no timer record."
            )
        except ModelSuccessionRefusal as error:
            raise StepFailure(str(error)) from error
        # Read once and never re-derived: after the stop below the timer is inactive whatever it was.
        state = systemd.unit_state(TIMER_UNIT)
        record = {
            **receipt_header(settings, TIMER_RECORD_SCHEMA_VERSION),
            "timer_unit": TIMER_UNIT,
            "timer_state": state,
            "timer_was_active": systemd.is_running(state),
        }
        succession.write_receipt(settings.directory / TIMER_RECORD_NAME, record)
    try:
        systemd.run_checked("stop", TIMER_UNIT)
    except StepFailure as error:
        raise TimerNotStopped(str(error)) from error
    timer_state = systemd.unit_state(TIMER_UNIT)
    if systemd.is_running(timer_state):
        raise TimerNotStopped(f"{TIMER_UNIT} is {timer_state} after systemctl --user stop returned zero.")

    started = monotonic()
    deadline = started + settings.pass_wait_seconds
    while True:
        service_state = systemd.unit_state(SERVICE_UNIT)
        if not systemd.is_running(service_state):
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise StepFailure(
                f"{SERVICE_UNIT} is still {service_state} after waiting {settings.pass_wait_seconds:.0f} s "
                "(--pass-wait-seconds) for the running pass to end. This tool never stops or kills it. The timer "
                "is stopped; the same command resumes the wait."
            )
        sleep(min(POLL_SECONDS, remaining))
    return {
        "timer_was_active": record["timer_was_active"],
        "timer_state_after_stop": timer_state,
        "service_state": service_state,
        "waited_seconds": round(monotonic() - started, 3),
    }


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def refresh(settings: Settings, _inputs: Inputs) -> dict[str, Any]:
    step_started = utc_now()
    systemd.run_checked("start", REFRESH_UNIT, timeout=REFRESH_START_TIMEOUT_SECONDS)
    latest = settings.refresh_receipt_root / "latest.json"
    try:
        receipt = read_json(latest)
    except (OSError, ValueError) as error:
        raise StepFailure(
            f"The provider refresh was started but its receipt {latest} cannot be read: {error}"
        ) from error
    receipt_started = _parse_time(receipt.get("started_at"))
    if receipt_started is None or receipt_started <= step_started:
        raise StepFailure(
            f"systemctl --user start {REFRESH_UNIT} returned zero, but {latest} is not newer than this step "
            f"(started_at {receipt.get('started_at')!r}, step started {utc_text(step_started)}): the start "
            "returned without a new latest.json, so no refresh of this step is counted. Possible causes: the "
            f"unit's start condition (it checks {SERVICE_UNIT}) skipped the run, or a refresh was already running "
            "and the start returned with that one."
        )
    if receipt.get("outcome") != "published":
        raise StepFailure(
            f"The provider refresh {receipt.get('run_id')!r} ended with outcome {receipt.get('outcome')!r} "
            f"(reason {receipt.get('reason')!r}), not 'published'; see {latest}."
        )
    classification = receipt.get("registry_classification")
    if not isinstance(classification, Mapping):
        raise StepFailure(f"{latest} has no registry_classification; the refresh cannot be judged.")
    totals: dict[str, Any] = {}
    for name in REFRESH_TOTALS:
        group = classification.get(name)
        totals[name] = group.get("total") if isinstance(group, Mapping) else None
    if any(type(total) is not int or total != 0 for total in totals.values()):
        raise StepFailure(
            f"The provider refresh {receipt.get('run_id')!r} classified a registry change, where a renewal of "
            f"the published manifest has none: {totals} (all four must be 0); see {latest}."
        )
    return {
        "refresh_receipt": {"path": str(latest), "sha256": succession.file_sha256(latest)},
        "refresh_run_id": receipt.get("run_id"),
        "refresh_started_at": receipt.get("started_at"),
        "refresh_outcome": receipt.get("outcome"),
        "registry_classification_totals": totals,
    }


def finish(settings: Settings, _inputs: Inputs) -> dict[str, Any]:
    receipts = [settings.step_receipt(step) for step in STEPS[:-1]]
    missing = [str(path) for path in receipts if not os.path.lexists(path)]
    if missing:
        raise StepFailure(f"Refused: finish requires every step receipt; missing: {missing}.")
    try:
        equal = settings.canonical_manifest.read_bytes() == settings.mirror_manifest.read_bytes()
    except OSError as error:
        raise StepFailure(f"Cannot read both manifests: {error}") from error
    if not equal:
        raise StepFailure(
            f"The canonical manifest {settings.canonical_manifest} and the worker mirror {settings.mirror_manifest} "
            "are not byte-identical; the timer was not started."
        )
    ids = manifest_model_ids(settings.canonical_manifest, what="the canonical manifest")
    absent = [model_id for model_id in settings.plan.new_ids if model_id not in ids]
    present = [model_id for model_id in settings.plan.old_ids if model_id in ids]
    if absent or present:
        raise StepFailure(
            f"The published manifest is not the plan's: new ids missing {absent}, old ids still present {present}; "
            "the timer was not started."
        )
    record = read_timer_record(settings)
    if record is None:
        raise StepFailure(f"{settings.directory / TIMER_RECORD_NAME} is missing; the timer was not started.")
    if record["timer_was_active"]:
        systemd.run_checked("start", TIMER_UNIT)
        action = TIMER_STARTED
    else:
        action = TIMER_LEFT_STOPPED
    return {
        "timer_was_active": record["timer_was_active"],
        "timer_action": action,
        "timer_state": systemd.observed_state(TIMER_UNIT),
        "manifest_sha256": succession.file_sha256(settings.canonical_manifest),
    }
