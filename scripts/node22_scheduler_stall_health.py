#!/usr/bin/env python3
"""Read-only health probe for the node-22 DB-free scheduler lane.

Issue #2570 (group C).  When the DB-free scheduler on node-22 quits making
progress, nothing rings.  ``services/orchestrator/monitoring.py`` refuses to
construct without ``DATABASE_URL`` and node-22 by design reaches no live
database, so that module cannot run here; node-27 can run it but cannot see
node-22's ``/scratch`` evidence root.  The no-progress circuit writes one
stderr line into a journal nobody reads, and the resource-limit fallback lands
only in an artifact.  The 2026-09-22T21:13 incident was found by a human
reading 279 artifacts by hand, with zero alerts in the meantime.

This probe grades three evidence sources --- ``systemctl --user show`` for the
scheduler timer and service, the governed terminal pass artifacts under the
evidence root, and the no-progress tracker --- into exactly one verdict by a
fixed precedence, writes a bounded receipt, and exits non-zero for every
verdict other than ``ok``.  A failed unit in the journal is this host's only
alerting channel: node-22 carries no ``OnFailure=`` mail lane.

Two structural properties are load-bearing, inherited verbatim from the
precedent probe ``scripts/node22_refresh_timer_health.py`` (OpenSpec change
``harden-node22-scheduler-refresh-lane``, decision D4):

* **It never mutates a systemd unit.**  It shells out only to
  ``systemctl --user show``, which is read-only.  No mutation verb appears
  anywhere in this file and a test scans the source to keep it that way.
  Detection only: it never touches a lock, never writes under the evidence
  root, and has no self-heal path.
* **It is self-contained.**  Standard library only, no import of
  ``services.*``, ``packages.*`` or any other repo package, so it can be
  staged and run from outside ``/scratch/frd_muziyao/NWM`` --- checking a
  feature branch out in that tree would put unreviewed code into the live
  scheduler tick.  That is why the governed pass filename predicate, its
  prefix and suffixes, the writer's evidence byte bound and the bounded
  no-follow read below are carried here rather than imported from
  ``services/orchestrator/scheduler_evidence.py``.  The duplication is pinned
  by a parity test that asserts the constants themselves (a predicate later
  narrowed to a stricter name shape would still classify any fixed sample of
  names alike while this copy silently grew permissive), and that test is
  routed in ``scripts/select_ci_tests.py`` so it runs on the very diff that
  could break parity.

Liveness comes from systemd, never from artifact age.  A healthy pass on this
lane has been measured at 193 minutes, so the newest completed artifact's age
conflates "the timer is dead" with "a long pass is in flight".  The scheduler
service is ``Type=oneshot``: in flight it reports ``ActiveState=activating``
with a non-dead sub-state and **never** ``active``.  Every verdict conditioned
on the service not running therefore treats ``activating`` as running, via the
single ``service_is_running`` helper.

Exit status is the contract: 0 for ``ok``, 1 for every other verdict, 2 for a
configuration refusal.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA_VERSION = "nhms.node22.scheduler_stall_health.v1"

# ---------------------------------------------------------------------------
# Duplicated on purpose (D4, see the module docstring).  Each of the four
# values below has a single authority in
# `services/orchestrator/scheduler_evidence.py`, and
# `tests/test_node22_scheduler_stall_health.py` imports that module and asserts
# every one of these constants equals its authority -- not merely that a sample
# of filenames is classified alike.
# ---------------------------------------------------------------------------

#: `scheduler_evidence.SCHEDULER_PASS_EVIDENCE_PREFIX`.
PASS_EVIDENCE_PREFIX = "scheduler_"
#: `scheduler_evidence.SCHEDULER_PASS_EVIDENCE_SUFFIXES`, tuple order included.
PASS_EVIDENCE_SUFFIXES = (".pre_execution.json", ".json")
#: `scheduler_evidence.MAX_EVIDENCE_BYTES` -- the writer's own upper bound, so
#: an artifact above it cannot have been written by the governed writer.
MAX_EVIDENCE_BYTES = 5_000_000
#: The snapshot suffix.  A snapshot is a governed pass filename but is not a
#: terminal artifact, and it sorts AFTER the terminal artifact of the same pass
#: (`.json` vs `.pre_execution.json` compare at `j` < `p`), which is why the
#: exclusion has to happen before the lexical truncation below.
PRE_EXECUTION_SUFFIX = ".pre_execution.json"


def is_pass_evidence_filename(name: str) -> bool:
    """Return whether ``name`` is a governed scheduler pass artifact filename.

    Byte-for-byte the rule of
    ``scheduler_evidence.is_scheduler_pass_evidence_filename``: the governed
    prefix plus one of the accepted pass JSON suffixes.  Non-pass state
    (``no-progress-tracker.json``), operator repair and stale-lock-clear
    receipts, the ``retention/`` subdirectory and unrelated JSON are not pass
    evidence.
    """

    if not name.startswith(PASS_EVIDENCE_PREFIX):
        return False
    return any(name.endswith(suffix) for suffix in PASS_EVIDENCE_SUFFIXES)


def is_terminal_pass_filename(name: str) -> bool:
    """Return whether ``name`` is a governed *terminal* pass artifact."""

    return is_pass_evidence_filename(name) and not name.endswith(PRE_EXECUTION_SUFFIX)


# Duplicated on purpose as well (D4): the tracker's schema version and filename
# belong to `services/orchestrator/scheduler_no_progress.py`, whose
# `load_state(dir_fd)` is part of the WRITE path and must not be imported by a
# read-only probe.  The tracker is deliberately unprefixed so pass-evidence
# retention skips it, which is also why the predicate above rejects it.
TRACKER_FILENAME = "no-progress-tracker.json"
TRACKER_SCHEMA_VERSION = "nhms.scheduler.no_progress_tracker.v1"
MAX_TRACKER_BYTES = 4 * 1024 * 1024

DEFAULT_TIMER_UNIT = "nhms-compute-scheduler.timer"
DEFAULT_SERVICE_UNIT = "nhms-compute-scheduler.service"
DEFAULT_SYSTEMCTL = "/usr/bin/systemctl"
DEFAULT_EVIDENCE_ROOT = "/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence"
# Deliberately NOT under the evidence root: the probe's own output must never
# enter readiness root discovery or pass-evidence retention.  A configuration
# that puts it there is refused (`load_config`).
DEFAULT_RECEIPT_ROOT = "/scratch/frd_muziyao/nhms-prod/workspace/scheduler-stall-health/receipts"

# Every default below is the node-22 production value and every one is derived
# from measurement (2026-09-23, 276 terminal pass artifacts), not from the
# timer's nominal cadence.  See the OpenSpec change's design document.
#
# Adjacent `started_at` intervals measured med/p90/p99/max = 8.3 / 13.4 /
# 160.4 / 193.9 minutes, so 360 minutes is ~1.9x the longest observed gap.
DEFAULT_MAX_TRIGGER_AGE_MINUTES = 360
DEFAULT_MAX_PASS_AGE_MINUTES = 360
MIN_AGE_MINUTES = 240
# `resource_limit_blocked` holds only while its pass is the newest one, which
# was measured at 4.5-27 minutes of idle lane.  The lookback floor must cover
# the probe period (15 minutes) plus `RandomizedDelaySec=60` plus margin, or a
# legal configuration could miss the condition entirely.
DEFAULT_LIMIT_LOOKBACK_MINUTES = 120
MIN_LIMIT_LOOKBACK_MINUTES = 30
# One `lock_contended` pass is ordinary noise (1 in 276 measured); the shape
# that needed an operator is a run of them -- the evidence root still holds
# three manual stale-lock-clear receipts.
DEFAULT_LOCK_PASSES = 5
MIN_LOCK_PASSES = 2
# ~2-3 hours of sustained "blocked candidates present and nothing submitted".
DEFAULT_NO_SUBMISSION_PASSES = 20
MIN_NO_SUBMISSION_PASSES = 2
# Deliberately above the scheduler's own observe-only threshold of 3
# (`NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES`): the circuit observes, the
# probe alerts, and the two thresholds are decoupled on purpose.
DEFAULT_CIRCUIT_PASSES = 20
MIN_CIRCUIT_PASSES = 1
# The lexical candidate scan is sound only with room above the largest hour
# bucket: bucket order is chronological because the bucket and `started_at`
# share the cycle hour, so disorder is confined inside a bucket.  The largest
# measured bucket holds 8 passes; 12 is the shipped margin and the config
# check below demands `scan_limit >= max(streak thresholds) + HOUR_BUCKET_MARGIN`,
# which is what keeps the ordering-safe prefix `scan_limit - HOUR_BUCKET_MARGIN`
# (the streak search bound, `Config.streak_window`) at least as long as either
# threshold.
HOUR_BUCKET_MARGIN = 12
DEFAULT_SCAN_LIMIT = 64
# The evidence root held 309 entries when measured and grows.  Reaching this
# bound is a REPORTED FACT, never a silent truncation: `os.scandir` returns
# filesystem order, so a truncated listing makes "the lexically greatest names
# are the highest hour buckets" false, and the margin argument above collapses
# without a trace.
DEFAULT_MAX_ENTRIES_SCANNED = 4096

# Chronic tracker subjects that cannot converge.  `ambiguous_fallback_match`
# with reason class `comment_accounting_unproven` is written by
# `services/orchestrator/reconcile.py:2564` when two or more owned in-window
# masters match and the controller never proves forcing identity -- this Slurm
# cluster does not return `job_comment`, so exact-comment accounting is
# permanently unproven and the count only ever grows (measured 1300 / 1198
# consecutive passes).  A bare threshold would pin the alert open forever and
# train operators to ignore it.  Suppression is stateless configuration with a
# checked-in default; it applies to the no-progress verdict ALONE and every
# suppressed entry is still written to the receipt.  See runbook section 6.2.
DEFAULT_SUPPRESSED_REASONS = "ambiguous_fallback_match:comment_accounting_unproven"

ENV_TIMER_UNIT = "NHMS_SCHEDULER_STALL_TIMER_UNIT"
ENV_SERVICE_UNIT = "NHMS_SCHEDULER_STALL_SERVICE_UNIT"
ENV_SYSTEMCTL = "NHMS_SCHEDULER_STALL_SYSTEMCTL"
ENV_EVIDENCE_ROOT = "NHMS_SCHEDULER_STALL_EVIDENCE_ROOT"
ENV_RECEIPT_ROOT = "NHMS_SCHEDULER_STALL_RECEIPT_ROOT"
ENV_MAX_TRIGGER_AGE_MINUTES = "NHMS_SCHEDULER_STALL_MAX_TRIGGER_AGE_MINUTES"
ENV_MAX_PASS_AGE_MINUTES = "NHMS_SCHEDULER_STALL_MAX_PASS_AGE_MINUTES"
ENV_LIMIT_LOOKBACK_MINUTES = "NHMS_SCHEDULER_STALL_LIMIT_LOOKBACK_MINUTES"
ENV_LOCK_PASSES = "NHMS_SCHEDULER_STALL_LOCK_PASSES"
ENV_NO_SUBMISSION_PASSES = "NHMS_SCHEDULER_STALL_NO_SUBMISSION_PASSES"
ENV_CIRCUIT_PASSES = "NHMS_SCHEDULER_STALL_CIRCUIT_PASSES"
ENV_SUPPRESSED_REASONS = "NHMS_SCHEDULER_STALL_SUPPRESSED_REASONS"
ENV_SCAN_LIMIT = "NHMS_SCHEDULER_STALL_SCAN_LIMIT"
ENV_MAX_ENTRIES_SCANNED = "NHMS_SCHEDULER_STALL_MAX_ENTRIES_SCANNED"
ENV_JSON = "NHMS_SCHEDULER_STALL_JSON"
# There is deliberately NO env name for the clock: `--now` is CLI-only, as on
# the precedent probe.  An inherited or dropped-in value would pin the probe to
# a past instant and grade the exact geometry this file exists to catch as
# `ok`/exit 0, silently and forever.

TIMER_SHOW_PROPERTIES = ("UnitFileState", "ActiveState", "SubState", "LastTriggerUSec")
SERVICE_SHOW_PROPERTIES = ("UnitFileState", "ActiveState", "SubState", "Result")

SYSTEMCTL_TIMEOUT_SECONDS = 30
MAX_HEALTH_RECEIPT_BYTES = 65536
MAX_SIGNAL_LENGTH = 256
# The receipt's variable-length lists are clipped so a very large tracker or a
# directory full of damaged artifacts can never push the receipt past its byte
# bound -- an unwritable receipt would cost the operator the verdict itself.
MAX_RECEIPT_LIST_ENTRIES = 20

# systemd prints one of these for a timestamp it does not have.
EMPTY_TIMESTAMPS = frozenset({"", "-", "n/a", "0"})

# The service is `Type=oneshot`.  MEASURED on node-22 across a full pass:
# in flight it reports `ActiveState=activating` with a non-dead sub-state, on
# completion `inactive`/`dead`.  It is NEVER `active`.  Grading a verdict on
# `ActiveState == "active"` would make every guard below fail open during a
# healthy 193-minute pass, which is the single failure this probe must not
# have.
SERVICE_RUNNING_ACTIVE_STATES = frozenset({"active", "activating"})
# Sub-states that mean "no ExecStart is executing".  `dead` is the measured
# between-pass value.  `failed` is here because the often-repeated shorthand
# "running is equivalent to SubState != dead" is NOT true: a oneshot whose run
# exited non-zero reports `failed`/`failed`, and reading that as running would
# gate off the stopped-timer verdict and report the lower-precedence
# service-failure verdict instead.  This value was not observed on node-22 --
# only `success` ever was -- so it is handled by construction rather than by
# measurement, which is exactly why it is named rather than left to a
# not-equal test.
SERVICE_NOT_RUNNING_SUB_STATES = frozenset({"dead", "failed"})

VERDICT_PROBE_FAILED = "probe_failed"
VERDICT_TIMER_NOT_ENABLED = "timer_not_enabled"
VERDICT_TIMER_STOPPED = "timer_stopped"
VERDICT_SCHEDULER_SERVICE_FAILED = "scheduler_service_failed"
VERDICT_SCHEDULER_NOT_TRIGGERING = "scheduler_not_triggering"
VERDICT_EVIDENCE_UNAVAILABLE = "evidence_unavailable"
VERDICT_EVIDENCE_STALE = "evidence_stale"
VERDICT_PASS_LIMIT_BLOCKED = "pass_limit_blocked"
VERDICT_LOCK_CONTENDED_PERSISTENT = "lock_contended_persistent"
VERDICT_SUBMISSION_STALLED = "submission_stalled"
VERDICT_NO_PROGRESS_CIRCUIT_OPEN = "no_progress_circuit_open"
VERDICT_OK = "ok"

RUNBOOK_PATH = "docs/runbooks/production-ops/stuck-detection.md"
#: Every non-healthy verdict names a runbook section, in the receipt and on
#: stderr.  Each anchor is a heading in section 6.2 whose text IS the anchor,
#: so the renderer's slug of that heading equals it verbatim (lowercase ASCII
#: letters and hyphens only, nothing for a slugger to rewrite).  The repo's
#: Markdown lint admits no inline HTML, which rules out explicit ids.
RUNBOOK_ANCHORS = {
    VERDICT_PROBE_FAILED: "stall-probe-failed",
    VERDICT_TIMER_NOT_ENABLED: "stall-timer-not-enabled",
    VERDICT_TIMER_STOPPED: "stall-timer-stopped",
    VERDICT_SCHEDULER_SERVICE_FAILED: "stall-scheduler-service-failed",
    VERDICT_SCHEDULER_NOT_TRIGGERING: "stall-scheduler-not-triggering",
    VERDICT_EVIDENCE_UNAVAILABLE: "stall-evidence-unavailable",
    VERDICT_EVIDENCE_STALE: "stall-evidence-stale",
    VERDICT_PASS_LIMIT_BLOCKED: "stall-pass-limit-blocked",
    VERDICT_LOCK_CONTENDED_PERSISTENT: "stall-lock-contended-persistent",
    VERDICT_SUBMISSION_STALLED: "stall-submission-stalled",
    VERDICT_NO_PROGRESS_CIRCUIT_OPEN: "stall-no-progress-circuit-open",
    VERDICT_OK: "stall-ok",
}

# Pass shapes.  `neutral` is decided by an observable the pass WRITER produces,
# never by a status allowlist -- see `classify_pass`.
SHAPE_PROGRESS = "progress"
SHAPE_BLOCKED = "blocked"
SHAPE_IDLE = "idle"
SHAPE_NEUTRAL = "neutral"

STATUS_RESOURCE_LIMIT_BLOCKED = "resource_limit_blocked"
STATUS_LOCK_CONTENDED = "lock_contended"

PROGRAM = "node22-scheduler-stall-health"


class ConfigError(Exception):
    """Refusal raised before any evidence is collected."""


class ProbeEvidenceError(Exception):
    """An evidence source could not be read or did not validate."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class Config:
    """Every operator-tunable input, validated before any evidence is read."""

    __slots__ = (
        "timer_unit",
        "service_unit",
        "systemctl",
        "evidence_root",
        "receipt_root",
        "max_trigger_age_minutes",
        "max_pass_age_minutes",
        "limit_lookback_minutes",
        "lock_passes",
        "no_submission_passes",
        "circuit_passes",
        "suppressed_reasons",
        "scan_limit",
        "max_entries_scanned",
    )

    def __init__(
        self,
        *,
        timer_unit: str,
        service_unit: str,
        systemctl: str,
        evidence_root: Path,
        receipt_root: Path,
        max_trigger_age_minutes: int,
        max_pass_age_minutes: int,
        limit_lookback_minutes: int,
        lock_passes: int,
        no_submission_passes: int,
        circuit_passes: int,
        suppressed_reasons: frozenset[str],
        scan_limit: int,
        max_entries_scanned: int,
    ) -> None:
        self.timer_unit = timer_unit
        self.service_unit = service_unit
        self.systemctl = systemctl
        self.evidence_root = evidence_root
        self.receipt_root = receipt_root
        self.max_trigger_age_minutes = max_trigger_age_minutes
        self.max_pass_age_minutes = max_pass_age_minutes
        self.limit_lookback_minutes = limit_lookback_minutes
        self.lock_passes = lock_passes
        self.no_submission_passes = no_submission_passes
        self.circuit_passes = circuit_passes
        self.suppressed_reasons = suppressed_reasons
        self.scan_limit = scan_limit
        self.max_entries_scanned = max_entries_scanned

    @property
    def streak_window(self) -> int:
        """How many newest terminal passes the streak searches may walk.

        NOT the alert threshold.  A neutral pass is skipped by the submission
        streak but would still occupy a slot in any positional window, so a
        window equal to the threshold (the shipped ``max(20, 5) = 20``) caps
        the streak at 19 as soon as one neutral pass falls inside it -- and the
        live receipt measured roughly one neutral pass in sixteen, which made
        ``submission_stalled`` unreachable at the shipped defaults.  The bound
        is instead the prefix the ordering argument vouches for: once the
        ``scan_limit`` lexically greatest names are sorted by ``started_at``,
        the first ``scan_limit - HOUR_BUCKET_MARGIN`` of them are guaranteed
        to be the true newest passes, because the arbitrary order inside the
        boundary hour bucket can displace at most ``HOUR_BUCKET_MARGIN``.  The
        config check keeps this at least ``max(no_submission, lock)``.
        """

        return self.scan_limit - HOUR_BUCKET_MARGIN


def _int_at_least(name: str, raw: str, minimum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from error
    if value < minimum:
        raise ConfigError(f"{name}={value} must be at least {minimum}")
    return value


def _parse_suppressed_reasons(raw: str) -> frozenset[str]:
    """Split the comma-separated EXACT reason strings.

    Exact strings, not prefixes and not subject ids: tracker subjects carry a
    cycle, so a subject-keyed list would need re-editing every cycle and would
    rot into silence.
    """

    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def load_config(env: dict[str, str] | None = None) -> Config:
    """Resolve and validate every input, refusing before any evidence is read.

    A value outside its range, a candidate scan that cannot cover the longest
    graded streak plus one hour bucket of margin, an evidence root that is not
    a directory, or a receipt root inside the evidence root, are all
    configuration refusals (exit 2) rather than verdicts.  The receipt-root
    rule is structural, not stylistic: the probe's own output inside the
    evidence root would enter readiness discovery and pass-evidence retention.
    """

    source = os.environ if env is None else env

    def get(name: str, default: str) -> str:
        return source.get(name) or default

    # The suppression list is the one input whose job is to SILENCE an alert,
    # so "unset" and "explicitly empty" must differ: a drop-in writing
    # `Environment=NHMS_SCHEDULER_STALL_SUPPRESSED_REASONS=` is an operator
    # clearing the list, and folding it back into the default would keep the
    # checked-in suppression in force against their explicit instruction.
    raw_suppressed_reasons = source.get(ENV_SUPPRESSED_REASONS)
    if raw_suppressed_reasons is None:
        raw_suppressed_reasons = DEFAULT_SUPPRESSED_REASONS

    max_trigger_age_minutes = _int_at_least(
        ENV_MAX_TRIGGER_AGE_MINUTES,
        get(ENV_MAX_TRIGGER_AGE_MINUTES, str(DEFAULT_MAX_TRIGGER_AGE_MINUTES)),
        MIN_AGE_MINUTES,
    )
    max_pass_age_minutes = _int_at_least(
        ENV_MAX_PASS_AGE_MINUTES,
        get(ENV_MAX_PASS_AGE_MINUTES, str(DEFAULT_MAX_PASS_AGE_MINUTES)),
        MIN_AGE_MINUTES,
    )
    limit_lookback_minutes = _int_at_least(
        ENV_LIMIT_LOOKBACK_MINUTES,
        get(ENV_LIMIT_LOOKBACK_MINUTES, str(DEFAULT_LIMIT_LOOKBACK_MINUTES)),
        MIN_LIMIT_LOOKBACK_MINUTES,
    )
    lock_passes = _int_at_least(
        ENV_LOCK_PASSES, get(ENV_LOCK_PASSES, str(DEFAULT_LOCK_PASSES)), MIN_LOCK_PASSES
    )
    no_submission_passes = _int_at_least(
        ENV_NO_SUBMISSION_PASSES,
        get(ENV_NO_SUBMISSION_PASSES, str(DEFAULT_NO_SUBMISSION_PASSES)),
        MIN_NO_SUBMISSION_PASSES,
    )
    circuit_passes = _int_at_least(
        ENV_CIRCUIT_PASSES, get(ENV_CIRCUIT_PASSES, str(DEFAULT_CIRCUIT_PASSES)), MIN_CIRCUIT_PASSES
    )
    scan_limit = _int_at_least(ENV_SCAN_LIMIT, get(ENV_SCAN_LIMIT, str(DEFAULT_SCAN_LIMIT)), 1)
    max_entries_scanned = _int_at_least(
        ENV_MAX_ENTRIES_SCANNED,
        get(ENV_MAX_ENTRIES_SCANNED, str(DEFAULT_MAX_ENTRIES_SCANNED)),
        1,
    )

    required_scan_limit = max(no_submission_passes, lock_passes) + HOUR_BUCKET_MARGIN
    if scan_limit < required_scan_limit:
        raise ConfigError(
            f"{ENV_SCAN_LIMIT}={scan_limit} must be at least {required_scan_limit}: the longest "
            f"graded streak is {max(no_submission_passes, lock_passes)} passes and the lexical "
            f"candidate scan needs {HOUR_BUCKET_MARGIN} more names so the arbitrary ordering "
            f"inside the boundary hour bucket cannot displace a pass that belongs in the window"
        )
    if max_entries_scanned < scan_limit:
        raise ConfigError(
            f"{ENV_MAX_ENTRIES_SCANNED}={max_entries_scanned} must be at least "
            f"{ENV_SCAN_LIMIT}={scan_limit}"
        )

    evidence_root = Path(get(ENV_EVIDENCE_ROOT, DEFAULT_EVIDENCE_ROOT))
    if not evidence_root.is_dir():
        raise ConfigError(f"{ENV_EVIDENCE_ROOT}={evidence_root} is not a directory")
    receipt_root = Path(get(ENV_RECEIPT_ROOT, DEFAULT_RECEIPT_ROOT))
    # Both sides resolved the same way.  `realpath` on the evidence root but
    # only `abspath` on the receipt root would let a receipt root reached
    # THROUGH a symlink into the evidence root pass the containment test.
    # `realpath` resolves the existing prefix of a not-yet-created path, so a
    # first run is covered too.
    resolved_evidence = Path(os.path.realpath(evidence_root))
    resolved_receipt = Path(os.path.realpath(receipt_root))
    if resolved_receipt == resolved_evidence or resolved_receipt.is_relative_to(resolved_evidence):
        raise ConfigError(
            f"{ENV_RECEIPT_ROOT}={receipt_root} must not be inside {ENV_EVIDENCE_ROOT}"
            f"={evidence_root}: the probe's own output would enter readiness discovery and "
            f"pass-evidence retention"
        )

    return Config(
        timer_unit=get(ENV_TIMER_UNIT, DEFAULT_TIMER_UNIT),
        service_unit=get(ENV_SERVICE_UNIT, DEFAULT_SERVICE_UNIT),
        systemctl=get(ENV_SYSTEMCTL, DEFAULT_SYSTEMCTL),
        evidence_root=evidence_root,
        receipt_root=receipt_root,
        max_trigger_age_minutes=max_trigger_age_minutes,
        max_pass_age_minutes=max_pass_age_minutes,
        limit_lookback_minutes=limit_lookback_minutes,
        lock_passes=lock_passes,
        no_submission_passes=no_submission_passes,
        circuit_passes=circuit_passes,
        suppressed_reasons=_parse_suppressed_reasons(raw_suppressed_reasons),
        scan_limit=scan_limit,
        max_entries_scanned=max_entries_scanned,
    )


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------


def parse_systemd_timestamp(raw: str) -> datetime | None:
    """Parse a ``systemctl show`` timestamp such as ``Tue 2026-09-23 06:11:18 CST``.

    ``%Z`` in ``strptime`` only accepts the running machine's own zone
    abbreviations, so it parses on node-22 and fails anywhere else.  The zone
    token is therefore handled explicitly: ``UTC``/``GMT``/``Z`` are read as
    UTC and anything else is read as the local zone, which is what systemd
    emitted.

    Returns ``None`` when the timestamp is absent; raises ``ValueError`` when
    it is present but unparseable, so the caller can fail closed rather than
    guess.
    """

    value = (raw or "").strip()
    if value in EMPTY_TIMESTAMPS:
        return None
    tokens = value.split()
    if len(tokens) < 4:
        raise ValueError(f"unparseable systemd timestamp: {value!r}")
    date_token, time_token, zone_token = tokens[1], tokens[2], tokens[3]
    moment = datetime.strptime(f"{date_token} {time_token}", "%Y-%m-%d %H:%M:%S")
    if zone_token.upper() in {"UTC", "GMT", "Z"}:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone()


def parse_iso8601(raw: str) -> datetime:
    """Parse an artifact ISO-8601 instant; a naive value is a refusal."""

    value = (raw or "").strip()
    if not value:
        raise ValueError("empty ISO-8601 instant")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"ISO-8601 instant must carry a timezone: {value!r}")
    return moment


def _age_minutes(now: datetime, moment: datetime) -> float:
    return round((now - moment).total_seconds() / 60.0, 4)


# ---------------------------------------------------------------------------
# Evidence collection --- systemd (read-only)
# ---------------------------------------------------------------------------


def _run_systemctl(systemctl: str, arguments: list[str]) -> str:
    try:
        completed = subprocess.run(
            [systemctl, "--user", *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=SYSTEMCTL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProbeEvidenceError(f"systemctl query failed: {error}") from error
    if completed.returncode != 0:
        raise ProbeEvidenceError(f"systemctl {' '.join(arguments)} exited {completed.returncode}")
    return completed.stdout


def read_show_properties(*, systemctl: str, unit: str, properties: tuple[str, ...]) -> dict[str, str]:
    """Return the raw ``systemctl show`` properties for ``unit``.

    ``show`` is the only subcommand this file ever names.  A property that came
    back empty stays empty here; the caller decides whether an empty value is
    unreadable evidence for the signal it feeds.
    """

    output = _run_systemctl(systemctl, ["show", unit, "-p", ",".join(properties)])
    resolved: dict[str, str] = {name: "" for name in properties}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in resolved:
            resolved[key] = value.strip()
    return resolved


def service_is_running(properties: dict[str, str]) -> bool:
    """Return whether the scheduler oneshot is executing a pass right now.

    MEASURED, not assumed: ``nhms-compute-scheduler.service`` is
    ``Type=oneshot``, so while a pass runs it reports ``activating`` with a
    non-dead sub-state and it never reports ``active``.  Both the active-state
    set and the sub-state are read, so either observation alone answers "this
    lane is working" --- but the sub-state is tested against a NAMED set of
    not-running values rather than ``!= "dead"``, which is not the same thing:
    see ``SERVICE_NOT_RUNNING_SUB_STATES``.  This is the single gate behind
    the stopped-timer, the not-triggering and the stale-evidence verdicts;
    written as equality with ``active`` it would fail open on every one of
    them during a healthy 193-minute pass.
    """

    active_state = properties.get("ActiveState", "")
    sub_state = properties.get("SubState", "")
    if active_state in SERVICE_RUNNING_ACTIVE_STATES:
        return True
    return bool(sub_state) and sub_state not in SERVICE_NOT_RUNNING_SUB_STATES


# ---------------------------------------------------------------------------
# Evidence collection --- pass artifacts (bounded, no-follow)
# ---------------------------------------------------------------------------


def read_bounded_no_follow(path: Path, *, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` from a regular file, refusing symlinks.

    Deliberately duplicated rather than imported from
    ``packages/common/safe_fs.py`` --- see the module docstring (D4).  Reads
    ``max_bytes + 1`` so an oversize file is detectable instead of silently
    truncated.
    """

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise ProbeEvidenceError(f"not a regular file: {path.name}")
        content = os.read(descriptor, max_bytes + 1)
    finally:
        os.close(descriptor)
    if len(content) > max_bytes:
        raise ProbeEvidenceError(f"exceeds {max_bytes} bytes: {path.name}")
    return content


def candidate_pass_names(root: Path, *, scan_limit: int, max_entries_scanned: int) -> list[str]:
    """Return the terminal pass filenames worth opening.

    Three ordered steps, and the order is load-bearing:

    1. Enumerate the directory under ``max_entries_scanned``.  **Reaching the
       bound is an error, never a silent truncation.**  ``os.scandir`` returns
       filesystem order, which is arbitrary, so a truncated listing makes "the
       lexically greatest names live in the highest hour buckets" false and
       the margin argument for step 3 collapses without a trace.
    2. Keep governed pass filenames, then drop the pre-execution snapshots.
       This MUST precede the truncation in step 3: a snapshot sorts after the
       terminal artifact of the same pass, the two coexist on disk long-term
       (21 such pairs measured), so truncating first would keep the snapshot
       and discard the terminal artifact at the boundary.
    3. Take the ``scan_limit`` lexically greatest names.  The pass id carries
       the cycle only to the hour, so lexical order is chronological BETWEEN
       hour buckets and arbitrary INSIDE one; ``HOUR_BUCKET_MARGIN`` names of
       slack (checked at config time) keep the boundary bucket from displacing
       a pass that belongs in the window.

    The recency order itself is decided later, from each artifact's own
    recorded ``started_at`` --- never from this lexical order and never from a
    modification time.
    """

    names: list[str] = []
    scanned = 0
    try:
        scanner = os.scandir(root)
    except OSError as error:
        raise ProbeEvidenceError(f"evidence root cannot be listed: {error}") from error
    try:
        for entry in scanner:
            scanned += 1
            if scanned > max_entries_scanned:
                raise ProbeEvidenceError(
                    f"evidence root holds more than {max_entries_scanned} entries; a bounded "
                    f"listing in arbitrary filesystem order cannot be graded"
                )
            if is_terminal_pass_filename(entry.name):
                names.append(entry.name)
    except OSError as error:
        raise ProbeEvidenceError(f"evidence root enumeration failed: {error}") from error
    finally:
        scanner.close()
    names.sort(reverse=True)
    return names[:scan_limit]


class PassRecord:
    """One successfully parsed terminal pass artifact."""

    __slots__ = ("name", "started_at", "status", "shape", "submitted_count", "blocked_candidate_count")

    def __init__(
        self,
        *,
        name: str,
        started_at: datetime,
        status: str,
        shape: str,
        submitted_count: int | None,
        blocked_candidate_count: int | None,
    ) -> None:
        self.name = name
        self.started_at = started_at
        self.status = status
        self.shape = shape
        self.submitted_count = submitted_count
        self.blocked_candidate_count = blocked_candidate_count


def _count_or_none(counts: Any, key: str) -> int | None:
    if not isinstance(counts, dict):
        return None
    value = counts.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def classify_pass(payload: dict[str, Any]) -> tuple[str, int | None, int | None]:
    """Return ``(shape, submitted_count, blocked_candidate_count)``.

    Neutral is decided by an observable the pass WRITER produces, never by a
    list of status strings.  The scheduler's own rule --- early-exit, pre-lock,
    lock-contended and resource-limit-aborted passes neither count nor clear
    --- names four early-return sites, not four status values.  The progress
    guard is constructed only after that region
    (``scheduler_runtime.py:834``), so a pass that returned earlier carries no
    ``progress_guard`` block at all.  Measured over 276 terminal artifacts the
    correlation is exact: the guard is absent on the four
    ``resource_limit_blocked`` fallbacks and the one ``lock_contended`` pass,
    and present on everything else.

    A status allowlist would be wrong in both directions here.
    ``scheduler_2026092223_7e6955b406ba.json`` is ``restart_reconciled`` with
    ``blocked_candidate_count=47`` and is the FIRST pass of the incident this
    probe exists for; a ``preflight_blocked`` pass is fully observed and
    counted by the scheduler itself; and an early-exit pass has no status
    string of its own, so it would fall through to idle and reset the streak.

    The absent-count case is the second neutral rule and is concentrated
    exactly where the probe is needed most: the resource-limit fallback
    artifact is the shape that omits the counts.  Such a pass is NOT read as
    having submitted nothing, and does not by itself make the tick
    probe-failed --- a legitimate degraded artifact inside the window would
    then suppress every other signal.  The condition it reports is carried by
    the higher-precedence resource-limit verdict instead.

    The third rule is the same writer read a second time, not a return to a
    status allowlist.  The resource-limit path (``scheduler_runtime.py``
    around ``:1505-1527``) writes both counts as zero and attaches a progress
    guard whenever the error details carry one.  That shape slips past the two
    rules above and lands in idle, which would RESET the streak --- the
    opposite of the scheduler's own "resource-limit-aborted neither counts nor
    clears".  The measured fallbacks all lacked the guard, so the first two
    rules sufficed on the data; this one closes the shape the writer can
    still produce.
    """

    counts = payload.get("counts")
    submitted = _count_or_none(counts, "submitted_count")
    blocked = _count_or_none(counts, "blocked_candidate_count")
    if (
        "progress_guard" not in payload
        or submitted is None
        or blocked is None
        or payload.get("status") == STATUS_RESOURCE_LIMIT_BLOCKED
    ):
        return SHAPE_NEUTRAL, submitted, blocked
    if submitted > 0:
        return SHAPE_PROGRESS, submitted, blocked
    if blocked > 0:
        return SHAPE_BLOCKED, submitted, blocked
    # Blocked candidates that have disappeared are no longer blocked, so an
    # idle pass BREAKS the streak.  Early-return passes never reach this
    # branch: the guard rule above has already taken them.
    return SHAPE_IDLE, submitted, blocked


def read_pass_record(path: Path) -> PassRecord:
    """Read, bound-check and classify one terminal pass artifact."""

    try:
        content = read_bounded_no_follow(path, max_bytes=MAX_EVIDENCE_BYTES)
    except ProbeEvidenceError:
        raise
    except OSError as error:
        raise ProbeEvidenceError(f"unreadable: {path.name}: {error}") from error
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProbeEvidenceError(f"not valid JSON: {path.name}") from error
    if not isinstance(payload, dict):
        raise ProbeEvidenceError(f"not a JSON object: {path.name}")
    raw_started_at = payload.get("started_at")
    if not isinstance(raw_started_at, str):
        raise ProbeEvidenceError(f"records no started_at: {path.name}")
    try:
        started_at = parse_iso8601(raw_started_at)
    except ValueError as error:
        raise ProbeEvidenceError(f"started_at is unparseable: {path.name}: {error}") from error
    status = payload.get("status")
    shape, submitted, blocked = classify_pass(payload)
    return PassRecord(
        name=path.name,
        started_at=started_at,
        status=status if isinstance(status, str) else "",
        shape=shape,
        submitted_count=submitted,
        blocked_candidate_count=blocked,
    )


def read_pass_records(root: Path, *, config: Config) -> tuple[list[PassRecord], list[str]]:
    """Return ``(records, unreadable)``, newest recorded ``started_at`` first.

    Recency comes from the artifact's own ``started_at`` and from nothing else.
    The governed pass filename carries the cycle only to the hour followed by a
    random suffix, so within any hour the lexical order contradicts the
    recorded order --- measured, the eight lexically greatest names came back
    as 02:35, 02:14, 02:46, 03:15, 03:21, 03:09, 03:27, 03:03.  No
    modification time is consulted anywhere: it is a property of the
    filesystem, not of the pass, and a restore or an rsync rewrites it.

    EVERY candidate is attempted, and every failure is collected: one damaged
    artifact makes the tick probe-failed either way, and an operator holding
    that verdict wants the whole list, not whichever name the directory
    happened to yield first.  An enumeration failure is different --- it means
    no candidate set exists at all --- so it still propagates immediately.
    """

    names = candidate_pass_names(
        root, scan_limit=config.scan_limit, max_entries_scanned=config.max_entries_scanned
    )
    records: list[PassRecord] = []
    unreadable: list[str] = []
    for name in names:
        try:
            records.append(read_pass_record(root / name))
        except ProbeEvidenceError as error:
            unreadable.append(str(error))
    records.sort(key=lambda record: record.started_at, reverse=True)
    return records, unreadable


# ---------------------------------------------------------------------------
# Evidence collection --- no-progress tracker (bounded, no-follow)
# ---------------------------------------------------------------------------


class TrackerEntry:
    """One ``(subject, reason)`` row of the no-progress tracker."""

    __slots__ = ("subject_kind", "subject_id", "reason", "consecutive_passes")

    def __init__(self, *, subject_kind: str, subject_id: str, reason: str, consecutive_passes: int) -> None:
        self.subject_kind = subject_kind
        self.subject_id = subject_id
        self.reason = reason
        self.consecutive_passes = consecutive_passes


def read_tracker_entries(path: Path) -> tuple[list[TrackerEntry], bool]:
    """Return ``(entries, present)`` for the no-progress tracker.

    An ABSENT tracker is a definite observation, not missing evidence: the
    circuit writes the file on its first enabled fully-observed pass and the
    runbook documents ``state_reset: "missing"`` as the ordinary
    first-pass shape, so ``ENOENT`` is reported as "no entries, tracker
    absent" and recorded in the receipt.  Every OTHER failure --- a symlink, a
    non-regular file, an oversize or unparseable document, an unrecognised
    schema version, a malformed row --- is unreadable evidence and makes the
    whole tick probe-failed.  The distinction is the fail-closed one: "the
    file is not there" is an answer, "I could not read it" is not.

    The tracker is parsed here rather than imported from
    ``scheduler_no_progress`` because of D4, and because that module's
    ``load_state(dir_fd)`` is part of the circuit's WRITE path.
    """

    try:
        content = read_bounded_no_follow(path, max_bytes=MAX_TRACKER_BYTES)
    except ProbeEvidenceError:
        raise
    except FileNotFoundError:
        return [], False
    except OSError as error:
        raise ProbeEvidenceError(f"no-progress tracker unreadable: {error}") from error
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProbeEvidenceError("no-progress tracker is not valid JSON") from error
    if not isinstance(document, dict):
        raise ProbeEvidenceError("no-progress tracker is not a JSON object")
    if document.get("schema_version") != TRACKER_SCHEMA_VERSION:
        raise ProbeEvidenceError(
            f"no-progress tracker schema_version is not {TRACKER_SCHEMA_VERSION}"
        )
    rows = document.get("entries")
    if not isinstance(rows, list):
        raise ProbeEvidenceError("no-progress tracker carries no entries list")
    entries: list[TrackerEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ProbeEvidenceError("no-progress tracker entry is not a JSON object")
        subject_kind = row.get("subject_kind")
        subject_id = row.get("subject_id")
        reason = row.get("reason")
        consecutive_passes = row.get("consecutive_passes")
        if not isinstance(subject_kind, str) or not isinstance(subject_id, str) or not isinstance(reason, str):
            raise ProbeEvidenceError("no-progress tracker entry is missing a subject or reason")
        if isinstance(consecutive_passes, bool) or not isinstance(consecutive_passes, int):
            raise ProbeEvidenceError("no-progress tracker entry has no integer consecutive_passes")
        entries.append(
            TrackerEntry(
                subject_kind=subject_kind,
                subject_id=subject_id,
                reason=reason,
                consecutive_passes=consecutive_passes,
            )
        )
    return entries, True


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def blocked_streak(records: list[PassRecord], *, window: int) -> tuple[int, int]:
    """Return ``(streak, neutral_skipped)`` over the newest ``window`` passes.

    The streak counts consecutive newest-first passes that submitted nothing
    while blocked.  Neutral passes are SKIPPED: they neither extend nor break
    the run, and ``neutral_skipped`` counts the ones the search walked past so
    the receipt can show them.  A progress or idle pass halts the search.

    ``window`` must be the ordering-safe prefix (``Config.streak_window``),
    never the alert threshold: a skipped neutral pass still occupies a
    position in the slice, so a threshold-sized window could never reach the
    threshold once it held a single neutral pass.
    """

    streak = 0
    neutral_skipped = 0
    for record in records[:window]:
        if record.shape == SHAPE_NEUTRAL:
            neutral_skipped += 1
            continue
        if record.shape != SHAPE_BLOCKED:
            break
        streak += 1
    return streak, neutral_skipped


def lock_contended_streak(records: list[PassRecord], *, window: int) -> int:
    """Consecutive newest-first passes reporting the lock-contended status.

    Graded on the status directly, not on the pass shape: a lock-contended
    pass is neutral for the submission streak (it carries no progress guard),
    yet a RUN of them is its own operational shape --- the artifacts stay
    fresh, no candidate is blocked, and the tracker does not grow.  Any other
    status halts the count.
    """

    streak = 0
    for record in records[:window]:
        if record.status != STATUS_LOCK_CONTENDED:
            break
        streak += 1
    return streak


def resource_limit_passes_in_window(
    records: list[PassRecord], *, now: datetime, lookback_minutes: int
) -> list[PassRecord]:
    """Passes carrying the resource-limit fallback status inside the lookback.

    Evaluated over EVERY artifact the probe successfully read, not over the
    shorter streak window: on a busy lane a 120-minute lookback would
    otherwise be silently shortened to however many passes the streak window
    holds.  A window test rather than a check against the newest pass, because
    that status holds only while its pass is newest --- measured at 4.5 to 27
    minutes --- and a 15-minute probe would almost never catch it.
    """

    bound = timedelta(minutes=lookback_minutes)
    return [
        record
        for record in records
        if record.status == STATUS_RESOURCE_LIMIT_BLOCKED and (now - record.started_at) <= bound
    ]


def partition_tracker_entries(
    entries: list[TrackerEntry], *, threshold: int, suppressed_reasons: frozenset[str]
) -> tuple[list[TrackerEntry], list[TrackerEntry]]:
    """Split the at-or-above-threshold entries into ``(open, suppressed)``.

    Suppression matches the reason EXACTLY and touches this signal alone.
    """

    open_entries: list[TrackerEntry] = []
    suppressed: list[TrackerEntry] = []
    for entry in entries:
        if entry.consecutive_passes < threshold:
            continue
        if entry.reason in suppressed_reasons:
            suppressed.append(entry)
        else:
            open_entries.append(entry)
    return open_entries, suppressed


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


class Observations:
    """Everything the grader reads, collected once."""

    __slots__ = (
        "evidence_errors",
        "timer_properties",
        "service_properties",
        "records",
        "tracker_entries",
        "tracker_present",
        "last_trigger",
        "last_trigger_age_minutes",
        "newest_pass_age_minutes",
        "unreadable",
    )

    def __init__(self) -> None:
        self.evidence_errors: list[str] = []
        self.timer_properties: dict[str, str] = {name: "" for name in TIMER_SHOW_PROPERTIES}
        self.service_properties: dict[str, str] = {name: "" for name in SERVICE_SHOW_PROPERTIES}
        self.records: list[PassRecord] = []
        self.tracker_entries: list[TrackerEntry] = []
        self.tracker_present = False
        self.last_trigger: datetime | None = None
        self.last_trigger_age_minutes: float | None = None
        self.newest_pass_age_minutes: float | None = None
        self.unreadable: list[str] = []


def grade(*, now: datetime, config: Config, observations: Observations) -> str:
    """Return exactly one verdict; the first matching condition wins.

    The precedence is fixed and is what makes the grading total: ``ok`` is a
    pure fall-through, reached only when no condition matched, never by a
    positive predicate of its own and never as a substitute for evidence the
    probe could not obtain.

    Completeness comes first --- evidence that could not be read cannot be
    graded.  The scheduler unit signals outrank the artifact signals: once the
    lane is down, every statement an artifact makes describes a moment in the
    past, and reporting it would point the operator at the wrong thing.  Among
    the unit signals, "dead after a reboot too" outranks "dead now" outranks
    "the last pass failed" outranks "no trigger is arriving".  The
    resource-limit verdict sits above the three slower artifact signals
    because that artifact is itself degraded and its diagnostic window is the
    narrowest of all.
    """

    # 1. Unreadable evidence can never fall through to healthy.
    if observations.evidence_errors:
        return VERDICT_PROBE_FAILED

    timer = observations.timer_properties
    service = observations.service_properties
    running = service_is_running(service)

    # 2. A timer that is not `enabled` does not survive a daemon reexec or
    #    a reboot.
    if timer.get("UnitFileState", "") != "enabled":
        return VERDICT_TIMER_NOT_ENABLED

    # 3. The timer is not active AND nothing is executing, so no further tick
    #    can come from this lane.  The conjunction is conservative, not a
    #    description of any reachable in-flight geometry: the timer stays
    #    active for the whole of a pass (its sub-state merely moves between
    #    running and waiting), so it is never inactive in flight.
    if timer.get("ActiveState", "") != "active" and not running:
        return VERDICT_TIMER_STOPPED

    # 4. The last pass did not succeed.  Fail-closed on ANY non-success value:
    #    systemd's oneshot result domain includes exit-code, signal, timeout,
    #    core-dump, resources, protocol and a rate-limit value, and only
    #    success has ever been observed on this lane -- so a failure-value
    #    allowlist would be a guess, and a wrong guess reads as healthy.
    if service.get("Result", "") != "success":
        return VERDICT_SCHEDULER_SERVICE_FAILED

    # 5. No trigger inside the bound, while nothing is executing.  The gate
    #    matters: a 193-minute pass naturally ages its own last trigger.
    if not running:
        if observations.last_trigger_age_minutes is None:
            # The timer is enabled and active but has never fired, and no pass
            # is executing.  Treated as infinitely overdue rather than skipped.
            return VERDICT_SCHEDULER_NOT_TRIGGERING
        if observations.last_trigger_age_minutes >= config.max_trigger_age_minutes:
            return VERDICT_SCHEDULER_NOT_TRIGGERING

    # 6. No governed terminal pass artifact exists at all --- distinct from an
    #    old one, because the two need different operator actions.
    if not observations.records:
        return VERDICT_EVIDENCE_UNAVAILABLE

    # 7. The units look healthy but artifacts have dried up.  An INDEPENDENT
    #    backstop for "the timer fires and the service exits in a second
    #    without writing", which every systemd-side signal above would miss.
    #    It carries the not-running gate STRUCTURALLY: without it a healthy
    #    pass longer than the artifact-age bound would be graded stale, and
    #    that 193.9 < 360 holds today is a numerical accident, not a guarantee.
    if not running and observations.newest_pass_age_minutes is not None:
        if observations.newest_pass_age_minutes >= config.max_pass_age_minutes:
            return VERDICT_EVIDENCE_STALE

    # 8. A degraded resource-limit pass anywhere inside the lookback window.
    if resource_limit_passes_in_window(
        observations.records, now=now, lookback_minutes=config.limit_lookback_minutes
    ):
        return VERDICT_PASS_LIMIT_BLOCKED

    # 9. Sustained lock contention -- a shape this lane has reached three times
    #    and that no other signal covers.
    if lock_contended_streak(observations.records, window=config.streak_window) >= config.lock_passes:
        return VERDICT_LOCK_CONTENDED_PERSISTENT

    # 10. Sustained blocked work with nothing submitted.
    streak, _neutral_skipped = blocked_streak(observations.records, window=config.streak_window)
    if streak >= config.no_submission_passes:
        return VERDICT_SUBMISSION_STALLED

    # 11. An unsuppressed tracker subject at or above the alert threshold.
    #     Suppression reaches this verdict and nothing above it.
    open_entries, _suppressed = partition_tracker_entries(
        observations.tracker_entries,
        threshold=config.circuit_passes,
        suppressed_reasons=config.suppressed_reasons,
    )
    if open_entries:
        return VERDICT_NO_PROGRESS_CIRCUIT_OPEN

    return VERDICT_OK


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def _clip(value: str) -> str:
    return value[:MAX_SIGNAL_LENGTH]


def _clip_list(values: list[Any]) -> tuple[list[Any], int]:
    if len(values) <= MAX_RECEIPT_LIST_ENTRIES:
        return values, 0
    return values[:MAX_RECEIPT_LIST_ENTRIES], len(values) - MAX_RECEIPT_LIST_ENTRIES


def runbook_pointer(verdict: str) -> str:
    return f"{RUNBOOK_PATH}#{RUNBOOK_ANCHORS[verdict]}"


def build_receipt(
    *, now: datetime, config: Config, verdict: str, exit_code: int, observations: Observations
) -> dict[str, Any]:
    """Assemble the verdict document.

    Every graded signal appears beside the threshold it was compared against,
    so the verdict can be re-derived from the receipt alone without the
    evidence root.  The two artifact windows are reported separately on
    purpose: the streak verdicts walk the ordering-safe prefix of newest
    passes (``streak_window``, NOT the alert threshold), while the
    resource-limit verdict reads a time window over every artifact that
    parsed, and conflating them would make the receipt unable to explain
    either.  The resource-limit passes are named, bounded, so the operator can
    open the exact artifacts the verdict was graded on.  Paths are not recorded --- unit names, filenames and counters
    only --- so an ops receipt never becomes a configuration leak.
    """

    records = observations.records
    shapes = {shape: 0 for shape in (SHAPE_PROGRESS, SHAPE_BLOCKED, SHAPE_IDLE, SHAPE_NEUTRAL)}
    for record in records:
        shapes[record.shape] += 1
    open_entries, suppressed_entries = partition_tracker_entries(
        observations.tracker_entries,
        threshold=config.circuit_passes,
        suppressed_reasons=config.suppressed_reasons,
    )
    limit_window = resource_limit_passes_in_window(
        records, now=now, lookback_minutes=config.limit_lookback_minutes
    )
    limit_names, limit_names_truncated = _clip_list([_clip(record.name) for record in limit_window])
    no_submission_streak, neutral_skipped = blocked_streak(records, window=config.streak_window)
    suppressed_rows, suppressed_truncated = _clip_list(
        [
            {
                "subject_kind": _clip(entry.subject_kind),
                "subject_id": _clip(entry.subject_id),
                "reason": _clip(entry.reason),
                "consecutive_passes": entry.consecutive_passes,
                "matched_rule": _clip(entry.reason),
            }
            for entry in sorted(suppressed_entries, key=lambda item: -item.consecutive_passes)
        ]
    )
    open_rows, open_truncated = _clip_list(
        [
            {
                "subject_kind": _clip(entry.subject_kind),
                "subject_id": _clip(entry.subject_id),
                "reason": _clip(entry.reason),
                "consecutive_passes": entry.consecutive_passes,
            }
            for entry in sorted(open_entries, key=lambda item: -item.consecutive_passes)
        ]
    )
    unreadable_rows, unreadable_truncated = _clip_list([_clip(item) for item in observations.unreadable])
    error_rows, error_truncated = _clip_list([_clip(item) for item in observations.evidence_errors])
    # Clipped like every other variable-length list: the suppressed-reason set
    # comes from the environment, so an operator with a long drop-in could
    # otherwise push the receipt past its byte bound and cost themselves the
    # verdict.
    reason_rows, reason_truncated = _clip_list(
        [_clip(reason) for reason in sorted(config.suppressed_reasons)]
    )
    lookback_window_passes = len(
        [
            record
            for record in records
            if (now - record.started_at) <= timedelta(minutes=config.limit_lookback_minutes)
        ]
    )

    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "generated_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "verdict": verdict,
        "exit_code": exit_code,
        "runbook": runbook_pointer(verdict),
        "timer": {
            "unit": _clip(config.timer_unit),
            "unit_file_state": _clip(observations.timer_properties.get("UnitFileState", "")),
            "active_state": _clip(observations.timer_properties.get("ActiveState", "")),
            "sub_state": _clip(observations.timer_properties.get("SubState", "")),
            "last_trigger": _clip(observations.timer_properties.get("LastTriggerUSec", "")),
            "last_trigger_age_minutes": observations.last_trigger_age_minutes,
            "max_trigger_age_minutes": config.max_trigger_age_minutes,
        },
        "service": {
            "unit": _clip(config.service_unit),
            "unit_file_state": _clip(observations.service_properties.get("UnitFileState", "")),
            "active_state": _clip(observations.service_properties.get("ActiveState", "")),
            "sub_state": _clip(observations.service_properties.get("SubState", "")),
            "result": _clip(observations.service_properties.get("Result", "")),
            "running": service_is_running(observations.service_properties),
        },
        "evidence": {
            "max_entries_scanned": config.max_entries_scanned,
            "scan_limit": config.scan_limit,
            "terminal_passes_parsed": len(records),
            "streak_window_passes": min(config.streak_window, len(records)),
            "streak_window": config.streak_window,
            "lookback_window_passes": lookback_window_passes,
            "newest_started_at": (
                records[0].started_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
                if records
                else None
            ),
            "newest_pass_age_minutes": observations.newest_pass_age_minutes,
            "max_pass_age_minutes": config.max_pass_age_minutes,
            "unreadable": unreadable_rows,
            "unreadable_truncated": unreadable_truncated,
        },
        "passes": {
            "progress_count": shapes[SHAPE_PROGRESS],
            "blocked_count": shapes[SHAPE_BLOCKED],
            "idle_count": shapes[SHAPE_IDLE],
            "neutral_count": shapes[SHAPE_NEUTRAL],
        },
        "signals": {
            "resource_limit_passes_in_window": len(limit_window),
            "resource_limit_pass_names": limit_names,
            "resource_limit_pass_names_truncated": limit_names_truncated,
            "limit_lookback_minutes": config.limit_lookback_minutes,
            "lock_contended_streak": lock_contended_streak(records, window=config.streak_window),
            "lock_passes": config.lock_passes,
            "no_submission_streak": no_submission_streak,
            "no_submission_neutral_skipped": neutral_skipped,
            "no_submission_passes": config.no_submission_passes,
            "circuit_open_entries": len(open_entries),
            "circuit_passes": config.circuit_passes,
            "tracker_present": observations.tracker_present,
            "tracker_entries": len(observations.tracker_entries),
        },
        "open": open_rows,
        "open_truncated": open_truncated,
        "suppressed": suppressed_rows,
        "suppressed_truncated": suppressed_truncated,
        "suppressed_reasons": reason_rows,
        "suppressed_reasons_truncated": reason_truncated,
        "errors": error_rows,
        "errors_truncated": error_truncated,
    }


def make_private_directory(path: Path) -> None:
    """Create ``path`` and every missing ancestor at 0700.

    ``Path.mkdir(mode=..., parents=True)`` applies ``mode`` to the LEAF only;
    intermediate levels get the default 0777 minus umask, which on an
    interactive shell leaves a world-readable directory above a private
    receipt root.  ``mode`` can only clear bits, never set them, so creating
    each level at 0700 is safe under any umask.
    """

    if path.is_dir():
        return
    parent = path.parent
    if parent != path:
        make_private_directory(parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise


def _write_all(descriptor: int, content: bytes) -> None:
    """Write every byte or fail closed.

    ``os.write`` may write fewer bytes than it was given; a single unchecked
    call silently truncates the receipt, which on a watchdog produces a
    shorter lie instead of a visible failure.
    """

    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise ProbeEvidenceError(
                f"probe receipt write made no progress at byte {offset} of {len(content)}"
            )
        offset += written


def write_receipt(root: Path, receipt: dict[str, Any]) -> Path:
    """Write the receipt 0600 under a 0700 root, durably and non-destructively.

    Write-temp, ``fsync``, then ``os.replace``: a failed or partial write must
    neither truncate the receipt nor destroy the previous good one, because on
    this lane the previous receipt is the only durable evidence there is.
    ``latest.json`` is ``lstat``-checked first --- ``os.replace`` would happily
    overwrite a symlink placed there, and that path must be refused rather
    than followed.
    """

    make_private_directory(root)
    content = (json.dumps(receipt, sort_keys=True) + "\n").encode()
    if len(content) > MAX_HEALTH_RECEIPT_BYTES:
        raise ProbeEvidenceError(f"probe receipt exceeds {MAX_HEALTH_RECEIPT_BYTES} bytes")
    target = root / "latest.json"
    try:
        status = os.lstat(target)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(status.st_mode):
            raise ProbeEvidenceError(f"probe receipt target is not a regular file: {target}")
    staged = root / f"latest.json.{os.getpid()}.partial"
    descriptor = os.open(
        staged,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, content)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise
    os.close(descriptor)
    try:
        os.replace(staged, target)
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node22_scheduler_stall_health.py",
        description=(
            "Read-only health probe for the node-22 DB-free scheduler lane. "
            "It observes and reports; it never modifies a systemd unit, never "
            "writes under the evidence root, and never clears a lock."
        ),
    )
    parser.add_argument(
        "--now",
        default="",
        help=(
            "pin the clock to an ISO-8601 instant with a timezone. CLI-only "
            "with NO environment default, by design: a pinned past instant "
            "would grade a dead lane as healthy, so this input must be "
            "impossible to inherit"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=os.environ.get(ENV_JSON, "").strip().lower() in {"1", "true", "yes", "on"},
        help=f"also print the verdict document to stdout (env {ENV_JSON})",
    )
    return parser


def resolve_now(raw: str) -> datetime:
    if not raw:
        return datetime.now(UTC)
    try:
        return parse_iso8601(raw)
    except ValueError as error:
        raise ConfigError(f"--now must be a timezone-aware ISO-8601 instant: {error}") from error


def collect(*, now: datetime, config: Config) -> Observations:
    """Gather every evidence source, recording failures rather than raising.

    Each source is collected independently so the receipt still carries what
    WAS readable when another source was not, exactly as on the precedent
    probe.  Any recorded failure makes the tick probe-failed at precedence 1.
    """

    observations = Observations()

    try:
        observations.timer_properties = read_show_properties(
            systemctl=config.systemctl, unit=config.timer_unit, properties=TIMER_SHOW_PROPERTIES
        )
    except ProbeEvidenceError as error:
        observations.evidence_errors.append(f"timer: {error}")
    try:
        observations.service_properties = read_show_properties(
            systemctl=config.systemctl, unit=config.service_unit, properties=SERVICE_SHOW_PROPERTIES
        )
    except ProbeEvidenceError as error:
        observations.evidence_errors.append(f"service: {error}")

    # A property the grader needs but systemd did not answer is unreadable
    # evidence, not a default.  `LastTriggerUSec` is exempt: an empty value is
    # systemd's way of saying "never triggered", which verdict 5 grades.
    for label, properties, required in (
        ("timer", observations.timer_properties, ("UnitFileState", "ActiveState", "SubState")),
        ("service", observations.service_properties, ("ActiveState", "SubState", "Result")),
    ):
        for name in required:
            if not properties.get(name, ""):
                observations.evidence_errors.append(f"{label}: systemctl returned no {name}")
    try:
        observations.last_trigger = parse_systemd_timestamp(
            observations.timer_properties.get("LastTriggerUSec", "")
        )
    except ValueError as error:
        observations.evidence_errors.append(f"timer: LastTriggerUSec is unparseable: {error}")
    if observations.last_trigger is not None:
        observations.last_trigger_age_minutes = _age_minutes(now, observations.last_trigger)

    try:
        observations.records, observations.unreadable = read_pass_records(
            config.evidence_root, config=config
        )
    except ProbeEvidenceError as error:
        # The candidate set itself could not be built, so there is no per-file
        # list to report; the single enumeration failure is the whole story.
        observations.unreadable = [str(error)]
    observations.evidence_errors.extend(
        f"pass evidence: {message}" for message in observations.unreadable
    )
    if observations.records:
        observations.newest_pass_age_minutes = _age_minutes(now, observations.records[0].started_at)

    try:
        observations.tracker_entries, observations.tracker_present = read_tracker_entries(
            config.evidence_root / TRACKER_FILENAME
        )
    except ProbeEvidenceError as error:
        observations.evidence_errors.append(f"tracker: {error}")

    return observations


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    # Configuration is resolved and refused BEFORE any evidence is gathered:
    # no artifact is opened and no systemctl query is issued on this path.
    try:
        config = load_config()
        now = resolve_now(arguments.now)
    except ConfigError as error:
        print(f"{PROGRAM}: {error}", file=sys.stderr)
        return 2

    observations = collect(now=now, config=config)
    verdict = grade(now=now, config=config, observations=observations)
    exit_code = 0 if verdict == VERDICT_OK else 1
    receipt = build_receipt(
        now=now, config=config, verdict=verdict, exit_code=exit_code, observations=observations
    )

    # The journal is this host's alerting channel, so the verdict and every
    # evidence error reach it BEFORE the receipt is attempted: a receipt
    # failure must not also swallow the verdict.
    for message in observations.evidence_errors:
        print(f"{PROGRAM}: {message}", file=sys.stderr)
    if verdict != VERDICT_OK:
        print(
            f"{PROGRAM}: verdict={verdict} exit_code={exit_code} "
            f"timer={config.timer_unit} service={config.service_unit} "
            f"runbook={runbook_pointer(verdict)}",
            file=sys.stderr,
        )
    else:
        print(f"{PROGRAM}: verdict={verdict}", file=sys.stderr)

    try:
        write_receipt(config.receipt_root, receipt)
    except (ProbeEvidenceError, OSError) as error:
        print(f"{PROGRAM}: receipt not written: {error}", file=sys.stderr)
        if arguments.json:
            print(json.dumps(receipt, sort_keys=True))
        return 1

    if arguments.json:
        print(json.dumps(receipt, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
