"""The wait for an autopipe round that started after a reference, and the one ``systemctl`` call behind it.

Part of ``scripts/node27_retire_basin.py`` (the entry point).  The tool issues
``systemctl --user show nhms-node27-autopipe.service -p ...`` and no other
call: it starts, stops, enables and disables nothing.

A round in flight when the exclusion list was edited has read the old list and
re-activates the rows of the basin; only a round that started after the edit
is known to honour it.  ``nhms-node27-autopipe.service`` is a oneshot unit,
``activating`` while it runs.  A round qualifies when it started after the
reference, has ended, exited by itself (``ExecMainCode`` 1, systemd's numeric
``CLD_EXITED``) and with status 0 or 1: status 1 is a round in which some
basin's run or seed failed, which read the list in full; status 2 is a round
blocked at its bootstrap or preflight and proves nothing, and so does a round
ended by a signal.

A qualifying round that only skipped (another round, started outside systemd,
held the autopipe lock) proves nothing either, so the lock is probed after it;
when it is held the wait goes on for the next round.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from scripts.basin_retirement.model import StepFailure

UNIT = "nhms-node27-autopipe.service"
SYSTEMCTL_ENV = "NHMS_BASIN_RETIREMENT_SYSTEMCTL"
DEFAULT_SYSTEMCTL = "/usr/bin/systemctl"
PROPERTIES = (
    "ActiveState",
    "ExecMainStartTimestampMonotonic",
    "ExecMainExitTimestampMonotonic",
    "ExecMainCode",
    "ExecMainStatus",
)
QUERY_TIMEOUT_SECONDS = 60
POLL_SECONDS = 15.0
RUNNING_STATE = "activating"
CLD_EXITED = 1
# 0: a clean round; 1: a round in which some basin's run or seed failed.  Both read the exclusion list in full.
QUALIFYING_EXIT_STATUSES = (0, 1)

# Indirections for tests.
sleep = time.sleep


def monotonic_us() -> int:
    """``CLOCK_MONOTONIC`` in microseconds: the clock systemd's ``*TimestampMonotonic`` properties are read from."""

    return time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000


@dataclass(frozen=True)
class Round:
    """What the unit says about its last, or its running, main process."""

    active_state: str
    start_us: int
    exit_us: int
    exit_code: int
    exit_status: int

    def record(self) -> dict[str, Any]:
        return {
            "active_state": self.active_state,
            "start_monotonic_us": self.start_us,
            "exit_monotonic_us": self.exit_us,
            "exit_code": self.exit_code,
            "exit_status": self.exit_status,
        }

    def qualifies(self, reference_us: int) -> bool:
        return (
            self.start_us > reference_us
            and self.exit_us >= self.start_us
            and self.active_state != RUNNING_STATE
            and self.exit_code == CLD_EXITED
            and self.exit_status in QUALIFYING_EXIT_STATUSES
        )


def read_unit() -> Round:
    """One ``systemctl --user show`` of the autopipe unit; a ``StepFailure`` when the answer cannot be relied on."""

    binary = os.environ.get(SYSTEMCTL_ENV) or DEFAULT_SYSTEMCTL
    command = [binary, "--user", "show", UNIT, "-p", ",".join(PROPERTIES)]
    text = f"systemctl --user show {UNIT}"
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise StepFailure(f"{text} could not be run: {error}") from error
    if completed.returncode != 0:
        raise StepFailure(f"{text} exited {completed.returncode}: {(completed.stderr or completed.stdout).strip()!r}.")
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        name, separator, value = line.partition("=")
        if not separator or name not in PROPERTIES or name in values:
            raise StepFailure(f"{text} printed a line this tool does not know: {line!r}. Nothing was assumed.")
        values[name] = value.strip()
    missing = [name for name in PROPERTIES if name not in values]
    if missing:
        raise StepFailure(f"{text} did not print {missing}; got {completed.stdout!r}. Nothing was assumed.")
    numbers: dict[str, int] = {}
    for name in PROPERTIES[1:]:
        if not values[name].isascii() or not values[name].isdigit():
            raise StepFailure(f"{text} printed {name}={values[name]!r}, which is not a number. Nothing was assumed.")
        numbers[name] = int(values[name])
    if not values["ActiveState"]:
        raise StepFailure(f"{text} printed an empty ActiveState. Nothing was assumed.")
    return Round(
        active_state=values["ActiveState"],
        start_us=numbers["ExecMainStartTimestampMonotonic"],
        exit_us=numbers["ExecMainExitTimestampMonotonic"],
        exit_code=numbers["ExecMainCode"],
        exit_status=numbers["ExecMainStatus"],
    )


def lock_held(path: str) -> bool:
    """Whether a round holds the autopipe lock: a shared non-blocking ``flock(2)`` probe, released at once.

    The cron script holds the lock with ``flock(1)``, that is ``flock(2)``;
    ``fcntl.lockf`` is a different lock and would not see it.  The file is
    opened read-only and never created: a missing file is a lock nobody holds.
    """

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise StepFailure(
            f"The autopipe lock file {path} cannot be opened ({error}); whether a round is running is unknown."
        ) from error
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError as error:
            raise StepFailure(f"The autopipe lock file {path} cannot be probed ({error}).") from error
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def wait_for_round(reference_us: int, *, timeout_seconds: float, lock_path: str) -> dict[str, Any]:
    """Poll until a round qualifies and the autopipe lock is free; a ``StepFailure`` on timeout.

    Returns what the receipt records about the round.  The same command waits
    again after a timeout, with a new reference.
    """

    started_us = monotonic_us()
    deadline_us = started_us + int(timeout_seconds * 1_000_000)
    after_us = reference_us
    skipped: list[dict[str, Any]] = []
    polls = 0
    while True:
        observed = read_unit()
        polls += 1
        if observed.qualifies(after_us):
            if not lock_held(lock_path):
                return {
                    **observed.record(),
                    "unit": UNIT,
                    "reference_monotonic_us": reference_us,
                    "lock_path_probed": lock_path,
                    "lock_held": False,
                    "rounds_skipped_while_lock_held": skipped,
                    "polls": polls,
                    "waited_seconds": round((monotonic_us() - started_us) / 1_000_000, 3),
                }
            # A round outside systemd is still running with the list it read earlier; this one only skipped.
            skipped.append(observed.record())
            after_us = observed.start_us
        remaining_us = deadline_us - monotonic_us()
        if remaining_us <= 0:
            raise StepFailure(
                f"No autopipe round that started after the reference ended within {timeout_seconds:.0f} s "
                f"({polls} readings of {UNIT}; the last: {observed.record()}; rounds that ended while the autopipe "
                f"lock {lock_path} was held: {len(skipped)}). This tool starts and stops nothing. Run the same "
                "command: it waits again, from a new reference."
            )
        sleep(min(POLL_SECONDS, remaining_us / 1_000_000))
