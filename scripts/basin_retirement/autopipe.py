"""The wait for an autopipe round that started after a reference, and the one ``systemctl`` call behind it.

Part of ``scripts/node27_retire_basin.py`` (the entry point).  The tool issues
``systemctl --user show nhms-node27-autopipe.service -p ...`` and no other
call: it starts, stops, enables and disables nothing.

A round in flight when the exclusion list was edited has read the old list and
re-activates the rows of the basin; only a round that started after the edit
is known to honour it.  ``nhms-node27-autopipe.service`` is a oneshot unit,
``activating`` while it runs.

Production rounds take longer than the timer period (measured 2026-10-07:
about 13 minutes against 10, back to back for hours), so the next round starts
in the second the previous one ends: the unit is then never seen at rest and
the exit status of a finished round is overwritten at once.  The wait
therefore follows rounds by their start timestamp.  A round is the candidate
once its start is later than the reference, and it has ended, and qualifies,
in one of two ways:

* seen at rest -- the start is still the candidate's, the unit is not
  ``activating``, it exited by itself (``ExecMainCode`` 1, systemd's numeric
  ``CLD_EXITED``) with status 0 or 1.  Status 1 is a round in which some
  basin's run or seed failed, which read the list in full.  Status 2 (blocked
  at its bootstrap or preflight) and a round ended by a signal prove nothing:
  that candidate is dropped and the wait goes on with the next round.
* followed -- a later poll shows a later start and the candidate was never
  seen at rest.  A oneshot unit does not overlap itself, so the candidate has
  ended; its exit status is not known and is recorded as such.  (Residual: a
  blocked round followed by a new start inside one poll interval is accepted.)

A candidate seen at rest that only skipped (another round, started outside
systemd, held the autopipe lock) proves nothing either, so the lock is probed
after it; when it is held the wait goes on with the next round.  After a
followed candidate the lock is held by the round that followed it, and the
probe is skipped.
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
    "LoadState",
    "ActiveState",
    "ExecMainStartTimestampMonotonic",
    "ExecMainExitTimestampMonotonic",
    "ExecMainCode",
    "ExecMainStatus",
)
NUMERIC_PROPERTIES = PROPERTIES[2:]
QUERY_TIMEOUT_SECONDS = 60
POLL_SECONDS = 15.0
LOADED_STATE = "loaded"
RUNNING_STATE = "activating"
CLD_EXITED = 1
# 0: a clean round; 1: a round in which some basin's run or seed failed.  Both read the exclusion list in full.
QUALIFYING_EXIT_STATUSES = (0, 1)
# How a qualifying round is known to have ended, as the receipt says it.
ENDED_AT_REST = "seen_at_rest"
ENDED_FOLLOWED = "followed"
UNKNOWN = "unknown"

# Indirections for tests.
sleep = time.sleep


def monotonic_us() -> int:
    """``CLOCK_MONOTONIC`` in microseconds: the clock systemd's ``*TimestampMonotonic`` properties are read from."""

    return time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000


@dataclass(frozen=True)
class Round:
    """What the unit says about its last, or its running, main process."""

    load_state: str
    active_state: str
    start_us: int
    exit_us: int
    exit_code: int
    exit_status: int

    def record(self) -> dict[str, Any]:
        return {
            "load_state": self.load_state,
            "active_state": self.active_state,
            "start_monotonic_us": self.start_us,
            "exit_monotonic_us": self.exit_us,
            "exit_code": self.exit_code,
            "exit_status": self.exit_status,
        }

    @property
    def at_rest(self) -> bool:
        return self.active_state != RUNNING_STATE

    @property
    def ended_by_itself_past_its_preflight(self) -> bool:
        """Of a round at rest: it exited by itself with a status that means it read the exclusion list in full."""

        return (
            self.exit_us >= self.start_us
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
    for name in NUMERIC_PROPERTIES:
        if not values[name].isascii() or not values[name].isdigit():
            raise StepFailure(f"{text} printed {name}={values[name]!r}, which is not a number. Nothing was assumed.")
        numbers[name] = int(values[name])
    if not values["ActiveState"]:
        raise StepFailure(f"{text} printed an empty ActiveState. Nothing was assumed.")
    # A unit that is not loaded (not-found, masked, error) never runs a round: waiting for one would only time out.
    if values["LoadState"] != LOADED_STATE:
        raise StepFailure(
            f"{text} printed LoadState={values['LoadState']!r}, not {LOADED_STATE!r}: the autopipe unit is not "
            "installed for this user, so no round will run. Nothing was assumed."
        )
    return Round(
        load_state=values["LoadState"],
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
    """Poll until a round that started after the reference has ended in a way that counts; a ``StepFailure`` on timeout.

    Returns what the receipt records about the round.  The same command waits
    again after a timeout, with a new reference.
    """

    started_us = monotonic_us()
    deadline_us = started_us + int(timeout_seconds * 1_000_000)
    # The next candidate is the first round whose start is later than this.
    after_us = reference_us
    candidate_us: int | None = None
    dropped: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    polls = 0

    def result(facts: dict[str, Any]) -> dict[str, Any]:
        return {
            **facts,
            "unit": UNIT,
            "reference_monotonic_us": reference_us,
            "lock_path_probed": lock_path,
            "candidates_dropped": dropped,
            "rounds_skipped_while_lock_held": skipped,
            "polls": polls,
            "waited_seconds": round((monotonic_us() - started_us) / 1_000_000, 3),
        }

    while True:
        observed = read_unit()
        polls += 1
        if candidate_us is not None and observed.start_us > candidate_us:
            # Never seen at rest, and a oneshot unit does not overlap itself: the candidate ran until this start.
            return result(
                {
                    "start_monotonic_us": candidate_us,
                    "ended": ENDED_FOLLOWED,
                    "followed_by_start_monotonic_us": observed.start_us,
                    "exit_monotonic_us": UNKNOWN,
                    "exit_code": UNKNOWN,
                    "exit_status": UNKNOWN,
                    "active_state": observed.active_state,
                    # The round that followed holds the lock: a probe would say nothing about the candidate.
                    "lock_probe": "skipped: the round that followed the candidate holds the lock",
                    "lock_held": UNKNOWN,
                }
            )
        if candidate_us is None and observed.start_us > after_us:
            candidate_us = observed.start_us
        if candidate_us is not None and observed.start_us == candidate_us and observed.at_rest:
            if not observed.ended_by_itself_past_its_preflight:
                # Blocked at its bootstrap or preflight, or ended by a signal: it proves nothing.
                dropped.append(observed.record())
            elif lock_held(lock_path):
                # A round outside systemd is still running with the list it read earlier; this one only skipped.
                skipped.append(observed.record())
            else:
                facts = {**observed.record(), "ended": ENDED_AT_REST, "lock_probe": "free", "lock_held": False}
                return result(facts)
            after_us, candidate_us = candidate_us, None
        remaining_us = deadline_us - monotonic_us()
        if remaining_us <= 0:
            following = (
                f"it was following the round that started at {candidate_us} (monotonic microseconds), which was "
                "still running at the last reading"
                if candidate_us is not None
                else f"no round had started after {after_us} (monotonic microseconds) yet"
            )
            raise StepFailure(
                f"No autopipe round that started after the reference ended within {timeout_seconds:.0f} s: "
                f"{following} ({polls} readings of {UNIT}; the last: {observed.record()}; candidates dropped: "
                f"{len(dropped)}; rounds that ended while the autopipe lock {lock_path} was held: {len(skipped)}). "
                "This tool starts and stops nothing. Run the same command: it waits again, from a new reference."
            )
        sleep(min(POLL_SECONDS, remaining_us / 1_000_000))
