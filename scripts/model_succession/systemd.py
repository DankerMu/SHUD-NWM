"""Every ``systemctl`` call of the model succession tool, and how a unit's state is read.

Part of ``scripts/node22_model_succession.py`` (the entry point).  The tool
issues three kinds of call and no other: ``is-active`` queries, ``stop`` of the
scheduler timer, and ``start`` of the scheduler timer or of the provider
refresh service.  It never stops the scheduler service, never enables,
disables or masks a unit and writes nothing under a systemd directory.
"""

from __future__ import annotations

import os
import subprocess

from scripts.model_succession.model import (
    DEFAULT_SYSTEMCTL,
    SERVICE_UNIT,
    SYSTEMCTL_ENV,
    TIMER_UNIT,
    StepFailure,
)

QUERY_TIMEOUT_SECONDS = 60
NOT_RUNNING_STATES = frozenset({"inactive", "failed"})
RUNNING_STATES = frozenset({"active", "activating", "deactivating", "reloading"})


class UnitStateError(StepFailure):
    """``is-active`` could not be run or printed something this tool does not know."""


def systemctl(*arguments: str, timeout: float = QUERY_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    """Run ``<systemctl> --user <arguments>``; the one place this tool starts ``systemctl``."""

    binary = os.environ.get(SYSTEMCTL_ENV) or DEFAULT_SYSTEMCTL
    return subprocess.run(
        [binary, "--user", *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def unit_state(unit: str) -> str:
    """What ``is-active <unit>`` prints.  Its exit status is not an error: it is non-zero for every state but active."""

    try:
        completed = systemctl("is-active", unit)
    except (OSError, subprocess.SubprocessError) as error:
        raise UnitStateError(f"systemctl --user is-active {unit} could not be run: {error}") from error
    state = completed.stdout.strip()
    if state not in NOT_RUNNING_STATES | RUNNING_STATES:
        raise UnitStateError(
            f"systemctl --user is-active {unit} printed {state!r} (exit {completed.returncode}, stderr "
            f"{completed.stderr.strip()!r}); expected one of {sorted(NOT_RUNNING_STATES | RUNNING_STATES)}. "
            "The state of the unit is unknown, so nothing was done."
        )
    return state


def is_running(state: str) -> bool:
    return state in RUNNING_STATES


def observed_state(unit: str) -> str:
    """The state for a report: what ``is-active`` printed, or why it could not be read."""

    try:
        return unit_state(unit)
    except UnitStateError as error:
        return f"unknown ({error})"


def require_scheduler_stopped(step: str) -> dict[str, str]:
    """Refuse ``step`` unless the scheduler timer and service are both not running."""

    states = {TIMER_UNIT: unit_state(TIMER_UNIT), SERVICE_UNIT: unit_state(SERVICE_UNIT)}
    running = [f"{unit} is {state}" for unit, state in states.items() if is_running(state)]
    if running:
        raise StepFailure(
            f"Refused before {step}: {'; '.join(running)}. Somebody started the scheduler during the succession; "
            f"the {step} step wrote nothing. Stop the timer again, let a running pass finish, and run the same "
            "command."
        )
    return states


def run_checked(*arguments: str, timeout: float = QUERY_TIMEOUT_SECONDS) -> None:
    """Run a ``stop`` / ``start``; a non-zero exit or the timeout is a failure."""

    command = f"systemctl --user {' '.join(arguments)}"
    try:
        completed = systemctl(*arguments, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise StepFailure(f"{command} did not return within {timeout:.0f} s.") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise StepFailure(f"{command} could not be run: {error}") from error
    if completed.returncode != 0:
        raise StepFailure(
            f"{command} exited {completed.returncode}: {(completed.stderr or completed.stdout).strip()!r}."
        )
