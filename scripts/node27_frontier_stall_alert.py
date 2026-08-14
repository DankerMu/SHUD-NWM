#!/usr/bin/env python3
"""node-27 published-frontier stall alerting (issue #1368).

The 2026-08-12 incident: a scheduler pass hung inside the forcing stage on an
NFS lock. The unit stayed ``activating`` forever and never failed, so nothing
in the systemd surface noticed that production had silently stopped producing
for ~11 h. Unit-level timeouts cannot cover that class (a full pass legitimately
runs 2 h 16 m - 3 h 01 m, so no safe unit deadline exists), therefore this lane
watches the *outcome* instead: the final landed artifact in
``hydro.hydro_run``. Any node-22 failure class collapses into the same
observable — the frontier stops advancing.

Design pins (``openspec/changes/node27-frontier-stall-alert/design.md``):

- **D1 criterion** — per ``COALESCE(source_id, '__null_source__')`` the runner
  takes three markers over post-ingest lifecycle rows
  (``status IN ('succeeded','parsed','published')``, ``cycle_time NOT NULL``):
  ``max(cycle_time)`` (frontier), ``count(DISTINCT cycle_time)`` (backfill
  count) and ``max(created_at)`` (arrival high-water). Progress is
  **directional**: a new source key, or a *strict increase* of any marker
  against the persisted **high-water** baseline. Equality and decreases (a row
  leaving the lifecycle set, a manual delete, a source disappearing) are not
  progress and never lower the baseline — otherwise a
  ``succeeded -> failed -> parsed`` round trip would forge a strict increase
  and reset the stall clock in the middle of a real outage. The criterion is
  never compared against wall-clock lag: while backfilling, the frontier is
  legitimately days behind and a wall-clock rule would alert forever.
- **D2 fail-safe table** — every internal failure of the alerter may only
  *increase* alerting tendency:
  missing state = silent bootstrap; corrupt/schema-mismatched state =
  ``monitoring-degraded`` mail + honestly recorded baseline rebuild; query
  failure never resets the stall clock and after ``query_fail_ticks``
  consecutive failures raises ``observability-unavailable``; a non-zero
  sendmail exit is NOT recorded as delivered so the next tick retries.
- **D3 mail channel** — local ``sendmail -t -i``, no SMTP credentials. Every
  error text crossing an operator outlet (email body, log line, receipt,
  state) is DSN-redacted first (see ``_redact_error_text``).
- **D4 systemd/env** — ``nhms-node27-frontier-alert.{service,timer}`` with
  ``OnCalendar=*:00/30``; single-instance mutex is *in this process*
  (``fcntl.flock`` on a lockfile beside the state file) so the wrapper stays a
  thin shell and the mutex is unit-testable.

Injection seams (unit tests need neither a database nor a real sendmail):
``run_tick(config, now=..., observe=..., sendmail_runner=...)`` and
``main(argv, now=..., observe=..., sendmail_runner=..., env=...)``. Environment
variables are outer wiring only.

ADR 0001 display carve-out: no imports touching ``apps/api`` / ``apps/frontend``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import socket
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Constants (D1 / D3 wire vocabulary).
# ---------------------------------------------------------------------------

STATE_SCHEMA_VERSION = 1
NULL_SOURCE_KEY = "__null_source__"

#: D1 post-ingest lifecycle set. On node-27 these three statuses are one
#: monotone lane inside a single autopipe pass: ingest inserts ``succeeded``
#: (``scripts/node27_ingest_run.py``), the parser lifts it to ``parsed``
#: (``workers/output_parser/parser.py``), publish lifts it to ``published``
#: (``scripts/node27_autopipeline.py``). A transition *inside* the set touches
#: the same row of the same cycle, so it moves no marker — no forged progress.
POST_INGEST_STATUSES: tuple[str, ...] = ("succeeded", "parsed", "published")

#: Statuses that must never enter the snapshot. ``failed``/``cancelled``/
#: ``superseded`` are the "row left the set" family whose appearance must not
#: look like progress; ``pending`` (migration 000013) and the pre-ingest
#: statuses are simply not node-27 landed artifacts.
EXCLUDED_STATUSES: tuple[str, ...] = (
    "created",
    "staged",
    "submitted",
    "running",
    "pending",
    "failed",
    "cancelled",
    "superseded",
)

OBSERVATION_QUERY = """
SELECT COALESCE(source_id, '__null_source__') AS source_key,
       max(cycle_time) AS frontier,
       count(DISTINCT cycle_time) AS cycles,
       max(created_at) AS latest_created
FROM hydro.hydro_run
WHERE cycle_time IS NOT NULL
  AND status IN ('succeeded','parsed','published')
GROUP BY 1
ORDER BY 1
"""

EVENT_STALLED = "frontier-stalled"
EVENT_RECOVERED = "frontier-recovered"
EVENT_DEGRADED = "monitoring-degraded"
DEGRADED_STATE_CORRUPT = "state-corrupt"
DEGRADED_OBSERVABILITY_UNAVAILABLE = "observability-unavailable"

CODE_CONFIG_INVALID = "FRONTIER_ALERT_CONFIG_INVALID"
CODE_CONCURRENT_INVOCATION = "FRONTIER_ALERT_CONCURRENT_INVOCATION"

BASELINE_ORIGIN_BOOTSTRAP = "bootstrap"
BASELINE_ORIGIN_RESET = "reset"

STATE_LOAD_OK = "ok"
STATE_LOAD_MISSING = "missing"
STATE_LOAD_CORRUPT = "corrupt"

DEFAULT_STALL_HOURS = 4.0
DEFAULT_RESEND_HOURS = 6.0
DEFAULT_QUERY_FAIL_TICKS = 2
DEFAULT_SENDMAIL_PATH = "/usr/sbin/sendmail"
DEFAULT_LOG_ROOT = Path("/home/nwm/node27-frontier-alert-logs")
DEFAULT_STATE_PATH = DEFAULT_LOG_ROOT / "frontier-alert-state.json"
DEFAULT_RECEIPT_PATH = DEFAULT_LOG_ROOT / "frontier-alert-receipt.json"
EVENTS_FILENAME = "frontier-alert-events.jsonl"

RUNBOOK_REFERENCE = "docs/runbooks/current-production-ops.md - 前沿停摆告警"

#: Every blocking call in a tick is bounded (D2 row 3, review round-1 B-C1):
#: an unbounded connect would let the *monitor* reproduce the very hang it
#: exists to report — the TCP default can block for minutes while the timer
#: fires again every 30 min. Same value/discipline as the compression sibling
#: ``scripts/node27_timeseries_compression.py:119`` (``_CONNECT_TIMEOUT_SECONDS``,
#: used at :464 and :506).
CONNECT_TIMEOUT_SEC = 10
QUERY_TIMEOUT_MS = 30_000
SENDMAIL_TIMEOUT_SEC = 60

#: Return code recorded when the mail chain itself fails before/around the
#: sendmail process (message build, encode, an unexpected runner error).
#: ``EX_SOFTWARE`` from sysexits, chosen for its meaning ("internal software
#: error"). It is NOT unique to this runner — sendmail speaks sysexits and can
#: exit 70 itself; the recorded error text is what disambiguates the two.
SEND_INTERNAL_FAILURE_RC = 70

#: Characters that would break out of a mail header. ``sendmail -t`` takes its
#: recipients from the headers this runner writes, so a CR/LF in the configured
#: address is a header-injection / silent-misdelivery vector (D3).
HEADER_FORBIDDEN_CHARS = ("\r", "\n", "\x00")


class FrontierAlertConfigError(Exception):
    """Structured, fail-closed configuration error (D2 last row)."""


# ---------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlertConfig:
    database_url: str
    email_to: str
    email_from: str
    stall_hours: float
    resend_hours: float
    state_path: Path
    receipt_path: Path
    sendmail_path: str
    query_fail_ticks: int

    @property
    def lock_path(self) -> Path:
        """D4: single-instance lockfile lives beside the state file."""
        return self.state_path.with_name(self.state_path.name + ".lock")

    @property
    def events_path(self) -> Path:
        """Append-only alert event log, derived beside the receipt."""
        return self.receipt_path.with_name(EVENTS_FILENAME)

    @property
    def stall_delta(self) -> timedelta:
        return timedelta(hours=self.stall_hours)

    @property
    def resend_delta(self) -> timedelta:
        return timedelta(hours=self.resend_hours)


def _required_env(env: Mapping[str, str], name: str, *, header_safe: bool = False) -> str:
    value = env.get(name)
    if value is None or not value.strip():
        raise FrontierAlertConfigError(f"{name} must be set")
    if header_safe:
        # Validate the RAW value: stripping first would hide a trailing CR/LF.
        return _header_safe(name, value)
    return value.strip()


def _header_safe(name: str, value: str) -> str:
    """Reject header-breaking control characters BEFORE they reach a header.

    Checked on the RAW value, not the stripped one: ``"ops@x\\r\\nBcc: ..."``
    strips clean at the tail but injects a header in the middle, and even a
    trailing bare CR/LF signals a mangled env file that must fail closed
    rather than mail somewhere unintended (D3 / review round-1 C-C3).
    """

    for char in HEADER_FORBIDDEN_CHARS:
        if char in value:
            raise FrontierAlertConfigError(
                f"{name} must not contain control characters (found {char!r})"
            )
    return value.strip()


def _positive_hours_env(env: Mapping[str, str], name: str, default: float) -> float:
    """Parse an hour window and prove it can become a ``timedelta`` HERE.

    ``float()`` happily returns ``nan``/``inf``, and ``1e30`` is finite yet
    overflows ``timedelta`` — so ``math.isfinite`` alone is not the test. Any
    of those would sail through config parsing and blow up mid-tick inside
    ``run_tick`` (``stall_delta``), i.e. after the alerter had decided it was
    configured: uncaught error, zero mail. Constructing the timedelta during
    config parsing turns every unusable value into the same structured,
    zero-side-effect config error as a missing variable (1S.6).
    """

    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as error:
        raise FrontierAlertConfigError(f"{name} must be a number, got {raw!r}") from error
    if not math.isfinite(value):
        raise FrontierAlertConfigError(f"{name} must be a finite number, got {raw!r}")
    if value <= 0:
        raise FrontierAlertConfigError(f"{name} must be positive, got {raw!r}")
    try:
        timedelta(hours=value)
    except (OverflowError, ValueError) as error:
        raise FrontierAlertConfigError(
            f"{name} is out of range for a time window, got {raw!r}"
        ) from error
    return value


def _positive_int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise FrontierAlertConfigError(f"{name} must be an integer, got {raw!r}") from error
    if value <= 0:
        raise FrontierAlertConfigError(f"{name} must be positive, got {raw!r}")
    return value


def _absolute_path_env(env: Mapping[str, str], name: str, default: Path) -> Path:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    path = Path(raw.strip())
    if not path.is_absolute():
        raise FrontierAlertConfigError(f"{name} must be an absolute path, got {raw!r}")
    return path


def default_email_from(hostname: str | None = None) -> str:
    """Derive the default sender, SANITIZING the hostname (1S.7).

    The hostname is decorative here — the recipient is what matters. A machine
    whose ``gethostname()`` somehow contains a CR/LF must therefore not be able
    to turn a cosmetic anomaly into a zero-mail configuration error: strip the
    header-breaking characters and fall back to a literal. Rejecting would be
    the wrong direction (silence), which is exactly what this lane forbids. A
    CR/LF in the OPERATOR-SUPPLIED ``NHMS_ALERT_EMAIL_FROM`` is a different
    thing and is still rejected — that one is a misconfiguration to fix.
    """

    host = hostname if hostname is not None else socket.gethostname()
    for char in HEADER_FORBIDDEN_CHARS:
        host = (host or "").replace(char, "")
    host = (host or "").strip()
    return f"NHMS Frontier Alert <nwm@{host or 'node-27'}>"


def config_from_env(env: Mapping[str, str] | None = None) -> AlertConfig:
    """Strict env parse. Fails closed before any observation (D2 last row)."""

    env = os.environ if env is None else env
    database_url = _required_env(env, "DATABASE_URL")
    email_to = _required_env(env, "NHMS_ALERT_EMAIL_TO", header_safe=True)
    email_from_raw = env.get("NHMS_ALERT_EMAIL_FROM") or ""
    email_from = _header_safe("NHMS_ALERT_EMAIL_FROM", email_from_raw) or default_email_from()
    sendmail_path_raw = env.get("NHMS_FRONTIER_SENDMAIL") or ""
    sendmail_path = sendmail_path_raw.strip() or DEFAULT_SENDMAIL_PATH
    if not Path(sendmail_path).is_absolute():
        raise FrontierAlertConfigError(
            f"NHMS_FRONTIER_SENDMAIL must be an absolute path, got {sendmail_path!r}"
        )
    return AlertConfig(
        database_url=database_url,
        email_to=email_to,
        email_from=email_from,
        stall_hours=_positive_hours_env(env, "NHMS_FRONTIER_STALL_HOURS", DEFAULT_STALL_HOURS),
        resend_hours=_positive_hours_env(env, "NHMS_FRONTIER_RESEND_HOURS", DEFAULT_RESEND_HOURS),
        state_path=_absolute_path_env(env, "NHMS_FRONTIER_STATE_PATH", DEFAULT_STATE_PATH),
        receipt_path=_absolute_path_env(env, "NHMS_FRONTIER_RECEIPT_PATH", DEFAULT_RECEIPT_PATH),
        sendmail_path=sendmail_path,
        query_fail_ticks=_positive_int_env(
            env, "NHMS_FRONTIER_QUERY_FAIL_TICKS", DEFAULT_QUERY_FAIL_TICKS
        ),
    )


# ---------------------------------------------------------------------------
# Redaction (D3). Provenance: same discipline as
# ``scripts/node27_timeseries_retention.py:_redact_error_text`` (#1213). Both
# route through the shared policy in ``packages/common/redaction.py``.
# ---------------------------------------------------------------------------


def _redact_error_text(error: BaseException | str, dsn: str) -> str:
    """Scrub credentials out of an error before it reaches ANY outlet.

    This module's single error-redaction chokepoint: every error text that
    reaches the email body, a log line, the receipt or the state file crosses
    it exactly once. psycopg2 echoes the whole conninfo — plaintext password
    included — on DSN parse/connect failures, so an unredacted interpolation
    would be a password-grade leak onto four persisted operator surfaces.

    Structure copied verbatim in spirit from
    ``scripts/node27_timeseries_retention.py:1055`` (#1213):

    * the ``packages.common.redaction`` import is function-local because that
      module imports ``psycopg2.extensions`` at module scope while this runner
      keeps psycopg2 strictly deferred (``--help`` and config parsing must work
      on a driver-less host);
    * because of that deferral the chokepoint is TOTAL — it never raises. On a
      driver-less host the very error being reported is typically the missing
      driver, and re-importing the redaction policy would raise straight out of
      the handler. Any internal failure degrades to a credential-free
      placeholder naming the original exception type;
    * libpq additionally echoes the ROLE name back on connection failures
      (``password authentication failed for user "x"`` /
      ``role "x" does not exist``); both keywords are scrubbed, and ONLY when
      the quoted name matches the DSN's own username (a bare literal replace of
      a username such as ``nwm`` would mangle ``/home/nwm/...``).
    """

    try:
        from packages.common.redaction import REDACTION_MARKER, redact_database_dsn

        text = redact_database_dsn(str(error), dsn).replace(REDACTION_MARKER, "***")
        try:
            username = urlsplit(dsn).username
        except ValueError:  # pragma: no cover - malformed DSN, nothing to scrub
            username = None
        if username:
            text = re.sub(rf'\b(user|role) "{re.escape(username)}"', r'\1 "***"', text)
        return text
    except Exception:
        # Fail closed on BOTH axes: withhold the unredactable text entirely
        # (no credential can survive) while keeping the outlet publishable.
        name = type(error).__name__ if isinstance(error, BaseException) else "str"
        return f"<error text withheld: redaction unavailable ({name})>"


# ---------------------------------------------------------------------------
# Observation model (D1).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceObservation:
    """Per-source three-marker observation / high-water baseline entry."""

    source_key: str
    frontier: datetime | None
    cycles: int
    latest_created: datetime | None

    def to_json(self) -> dict[str, Any]:
        return {
            "frontier": _iso_or_none(self.frontier),
            "cycles": self.cycles,
            "latest_created": _iso_or_none(self.latest_created),
        }

    @classmethod
    def from_json(cls, source_key: str, payload: Any) -> SourceObservation:
        if not isinstance(payload, Mapping):
            raise ValueError(f"snapshot entry for {source_key!r} is not an object")
        cycles = payload.get("cycles")
        if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles < 0:
            raise ValueError(f"snapshot entry for {source_key!r} has invalid cycles")
        return cls(
            source_key=source_key,
            frontier=_parse_iso_or_none(payload.get("frontier")),
            cycles=cycles,
            latest_created=_parse_iso_or_none(payload.get("latest_created")),
        )


Snapshot = dict[str, SourceObservation]
ObservationProvider = Callable[[AlertConfig], Snapshot]


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _parse_iso_or_none(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"expected ISO-8601 string, got {type(raw).__name__}")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _coerce_timestamp(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        value = raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(raw, str):
        return _parse_iso_or_none(raw)
    raise ValueError(f"unsupported timestamp type {type(raw).__name__}")


def snapshot_to_json(snapshot: Mapping[str, SourceObservation]) -> dict[str, Any]:
    return {key: snapshot[key].to_json() for key in sorted(snapshot)}


def snapshot_from_json(payload: Any) -> Snapshot:
    if not isinstance(payload, Mapping):
        raise ValueError("snapshot is not an object")
    return {str(key): SourceObservation.from_json(str(key), value) for key, value in payload.items()}


def default_observe(config: AlertConfig) -> Snapshot:
    """Real observation provider (lazy psycopg2 import, read-only DSN)."""

    import psycopg2  # type: ignore[import-untyped]
    import psycopg2.extras  # type: ignore[import-untyped]

    connection = psycopg2.connect(
        config.database_url,
        connect_timeout=CONNECT_TIMEOUT_SEC,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SET statement_timeout = {QUERY_TIMEOUT_MS}")
                cursor.execute(OBSERVATION_QUERY)
                rows = cursor.fetchall()
    finally:
        connection.close()
    snapshot: Snapshot = {}
    for row in rows:
        key = str(row["source_key"])
        snapshot[key] = SourceObservation(
            source_key=key,
            frontier=_coerce_timestamp(row["frontier"]),
            cycles=int(row["cycles"]),
            latest_created=_coerce_timestamp(row["latest_created"]),
        )
    return snapshot


# ---------------------------------------------------------------------------
# Directional progress (D1).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgressResult:
    progressed: bool
    reasons: tuple[str, ...]
    baseline: Snapshot


def _marker_increased(previous: Any, current: Any) -> bool:
    if current is None:
        return False
    if previous is None:
        return True
    return bool(current > previous)


def _high_water(previous: SourceObservation | None, current: SourceObservation) -> SourceObservation:
    """Element-wise max. A baseline marker NEVER goes down (D1)."""

    if previous is None:
        return current
    return SourceObservation(
        source_key=current.source_key,
        frontier=_max_or_none(previous.frontier, current.frontier),
        cycles=max(previous.cycles, current.cycles),
        latest_created=_max_or_none(previous.latest_created, current.latest_created),
    )


def _max_or_none(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def evaluate_progress(
    baseline: Mapping[str, SourceObservation], observation: Mapping[str, SourceObservation]
) -> ProgressResult:
    """Directional progress against the persisted HIGH-WATER baseline.

    Progress = a new source key, or a strict increase of ``frontier`` /
    ``cycles`` / ``latest_created`` for some source. Equal markers, decreased
    markers, a source that disappeared, or a lifecycle transition inside the
    post-ingest set are all NON-progress, and the returned baseline is the
    element-wise maximum of old and new — a marker that dropped keeps its
    high-water value so that recovering to the old value is not a later
    "strict increase".
    """

    reasons: list[str] = []
    merged: Snapshot = dict(baseline)
    for key in sorted(observation):
        current = observation[key]
        previous = baseline.get(key)
        if previous is None:
            reasons.append(f"{key}: new source")
        else:
            if _marker_increased(previous.frontier, current.frontier):
                reasons.append(
                    f"{key}: frontier {_iso_or_none(previous.frontier)}"
                    f" -> {_iso_or_none(current.frontier)}"
                )
            if current.cycles > previous.cycles:
                reasons.append(f"{key}: cycles {previous.cycles} -> {current.cycles}")
            if _marker_increased(previous.latest_created, current.latest_created):
                reasons.append(
                    f"{key}: latest_created {_iso_or_none(previous.latest_created)}"
                    f" -> {_iso_or_none(current.latest_created)}"
                )
        merged[key] = _high_water(previous, current)
    return ProgressResult(progressed=bool(reasons), reasons=tuple(reasons), baseline=merged)


# ---------------------------------------------------------------------------
# State persistence.
# ---------------------------------------------------------------------------


@dataclass
class AlertState:
    snapshot: Snapshot
    last_change_at: datetime
    alert_active: bool = False
    last_alert_at: datetime | None = None
    consecutive_query_failures: int = 0
    baseline_established_at: datetime | None = None
    baseline_reset_at: datetime | None = None
    # A degraded-alert dedup clock PER KIND (round-2 P2): one shared clock let
    # a delivered ``state-corrupt`` mail suppress the first-ever
    # ``observability-unavailable`` for up to a resend window — a whole event
    # class silently lost, which is the under-report direction. Keeping the
    # family separate from ``last_alert_at`` also stops a stall alert from
    # throttling a degraded one.
    last_degraded_alert_by_kind: dict[str, datetime] = field(default_factory=dict)
    # ``last_error`` keeps the most recent (already redacted) failure text on
    # the state outlet for post-mortem.
    last_error: str | None = None
    # ``baseline_pending``: a rebuild/bootstrap tick whose observation ALSO
    # failed holds no baseline at all (D1 review round-1 A-C1). Persisting an
    # empty snapshot as if it were a live baseline would make every source look
    # "new" on the next successful observation — forged progress that pushes
    # the stall clock out by the whole outage.
    # ``degraded_pending``: degraded alerts whose send failed, retried on the
    # next tick under the 6 h dedup clock (D2 row 2, review round-1 A-C2).
    baseline_pending: bool = False
    #: Which rebuild put the baseline in pending: ``bootstrap`` or ``reset``.
    #: The fill must stamp the matching field — a corrupt-origin rebuild that
    #: stamped ``baseline_established_at`` would read, forever after, as a
    #: fresh installation instead of a recorded degradation.
    baseline_pending_kind: str = BASELINE_ORIGIN_BOOTSTRAP
    degraded_pending: list[dict[str, str]] = field(default_factory=list)
    schema_version: int = STATE_SCHEMA_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot": snapshot_to_json(self.snapshot),
            "last_change_at": _iso_or_none(self.last_change_at),
            "alert_active": self.alert_active,
            "last_alert_at": _iso_or_none(self.last_alert_at),
            "consecutive_query_failures": self.consecutive_query_failures,
            "baseline_established_at": _iso_or_none(self.baseline_established_at),
            "baseline_reset_at": _iso_or_none(self.baseline_reset_at),
            "last_degraded_alert_by_kind": {
                kind: _iso_or_none(stamp)
                for kind, stamp in sorted(self.last_degraded_alert_by_kind.items())
            },
            "last_error": self.last_error,
            "baseline_pending": self.baseline_pending,
            "baseline_pending_kind": self.baseline_pending_kind,
            "degraded_pending": [dict(entry) for entry in self.degraded_pending],
        }

    @classmethod
    def from_json(cls, payload: Any) -> AlertState:
        if not isinstance(payload, Mapping):
            raise ValueError("state payload is not an object")
        version = payload.get("schema_version")
        if version != STATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported state schema_version {version!r}")
        last_change_at = _parse_iso_or_none(payload.get("last_change_at"))
        if last_change_at is None:
            raise ValueError("state is missing last_change_at")
        failures = payload.get("consecutive_query_failures", 0)
        if not isinstance(failures, int) or isinstance(failures, bool) or failures < 0:
            raise ValueError("state has invalid consecutive_query_failures")
        alert_active = payload.get("alert_active", False)
        if not isinstance(alert_active, bool):
            raise ValueError("state has invalid alert_active")
        last_error = payload.get("last_error")
        if last_error is not None and not isinstance(last_error, str):
            raise ValueError("state has invalid last_error")
        # Both round-1 fields default when absent: a state written before they
        # existed stays readable, so schema_version remains 1 (an unreadable
        # old state would fire a spurious monitoring-degraded email).
        baseline_pending = payload.get("baseline_pending", False)
        if not isinstance(baseline_pending, bool):
            raise ValueError("state has invalid baseline_pending")
        baseline_pending_kind = payload.get(
            "baseline_pending_kind", BASELINE_ORIGIN_BOOTSTRAP
        )
        if baseline_pending_kind not in (BASELINE_ORIGIN_BOOTSTRAP, BASELINE_ORIGIN_RESET):
            raise ValueError(
                f"state has invalid baseline_pending_kind {baseline_pending_kind!r}"
            )
        degraded_pending = _degraded_pending_from_json(payload.get("degraded_pending", []))
        return cls(
            snapshot=snapshot_from_json(payload.get("snapshot", {})),
            last_change_at=last_change_at,
            alert_active=alert_active,
            last_alert_at=_parse_iso_or_none(payload.get("last_alert_at")),
            consecutive_query_failures=failures,
            baseline_established_at=_parse_iso_or_none(payload.get("baseline_established_at")),
            baseline_reset_at=_parse_iso_or_none(payload.get("baseline_reset_at")),
            last_degraded_alert_by_kind=_degraded_clock_from_json(payload),
            last_error=last_error,
            baseline_pending=baseline_pending,
            baseline_pending_kind=baseline_pending_kind,
            degraded_pending=degraded_pending,
            schema_version=STATE_SCHEMA_VERSION,
        )


def _degraded_clock_from_json(payload: Mapping[str, Any]) -> dict[str, datetime]:
    """Read the per-kind degraded clock. A legacy scalar is NOT migrated.

    A state written by the previous revision carries a single
    ``last_degraded_alert_at`` that cannot be attributed to a kind — it may
    have been armed by ``state-corrupt`` or by ``observability-unavailable``,
    and nothing in the file says which. Fanning it out onto both kinds
    reinstates exactly the cross-kind suppression the per-kind clock exists to
    remove ("绝不允许" column of D2 row 3): one upgrade could swallow the
    first-ever alert of the other kind for a whole resend window.

    So the scalar is dropped and the clocks start empty (bootstrap). Cost of
    NOT migrating: at most one duplicate mail per kind, immediately after the
    upgrade — the allowed over-report direction. ``schema_version`` stays 1:
    the old file is still perfectly readable, so bumping it would classify
    every deployed state as corrupt and mail a degradation for a routine
    upgrade.
    """

    raw = payload.get("last_degraded_alert_by_kind")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("state has invalid last_degraded_alert_by_kind")
    clocks: dict[str, datetime] = {}
    for kind, stamp in raw.items():
        if kind not in (DEGRADED_STATE_CORRUPT, DEGRADED_OBSERVABILITY_UNAVAILABLE):
            raise ValueError(f"state has unknown degraded clock kind {kind!r}")
        parsed = _parse_iso_or_none(stamp)
        if parsed is not None:
            clocks[kind] = parsed
    return clocks


def _degraded_pending_from_json(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, list):
        raise ValueError("state has invalid degraded_pending")
    entries: list[dict[str, str]] = []
    for entry in payload:
        if not isinstance(entry, Mapping):
            raise ValueError("state has invalid degraded_pending entry")
        kind = entry.get("kind")
        reason = entry.get("reason")
        if kind not in (DEGRADED_STATE_CORRUPT, DEGRADED_OBSERVABILITY_UNAVAILABLE):
            raise ValueError(f"state has unknown degraded_pending kind {kind!r}")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("state has invalid degraded_pending reason")
        entries.append({"kind": kind, "reason": reason or ""})
    return entries


@dataclass(frozen=True)
class StateLoad:
    status: str
    state: AlertState | None
    reason: str | None = None


def load_state(path: Path) -> StateLoad:
    """D2 rows 1+2: split *missing* (bootstrap) from *corrupt* (degraded).

    Containment here is WHOLE-CLASS, not an enumeration of exception types
    (round-2 invariant-audit ruling, after the enumeration missed a class for
    the second review round running). ``Path.read_text`` raises
    ``UnicodeDecodeError`` (a ``ValueError``, not an ``OSError``) on
    byte-corrupted state; ``json.loads`` raises ``RecursionError`` (a
    ``RuntimeError``) on pathological nesting; ``datetime.astimezone`` raises
    ``OverflowError`` (neither) on an extreme-offset timestamp such as
    ``9999-12-31T23:59:59-14:00``; a huge state can raise ``MemoryError``. The
    enumeration is not closed over the failure class, and every miss has the
    same shape: the alerter dies on every tick, sends nothing, and never
    rewrites the state that is killing it — permanent silence, the exact
    under-report failure this lane exists to prevent. So both stages converge
    on ``except Exception``. ``KeyboardInterrupt``/``SystemExit`` derive from
    ``BaseException`` and stay uncaught, as they must.
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return StateLoad(STATE_LOAD_MISSING, None)
    except Exception as error:  # noqa: BLE001 - whole-class containment, see docstring
        return StateLoad(
            STATE_LOAD_CORRUPT,
            None,
            f"state file unreadable: {type(error).__name__}: {error}",
        )
    try:
        return StateLoad(STATE_LOAD_OK, AlertState.from_json(json.loads(raw)))
    except RecursionError:
        # Kept as its own arm: the stack is freshly unwound here and formatting
        # the exception buys the operator nothing beyond the class name.
        return StateLoad(STATE_LOAD_CORRUPT, None, "state file unusable: RecursionError")
    except Exception as error:  # noqa: BLE001 - whole-class containment, see docstring
        return StateLoad(
            STATE_LOAD_CORRUPT,
            None,
            f"state file unusable: {type(error).__name__}: {error}",
        )


def _atomic_write(path: Path, text: str) -> None:
    """tmp + rename. A crash mid-write can leave a tmp file but never a
    truncated state/receipt (B5)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)


def write_state(path: Path, state: AlertState) -> None:
    _atomic_write(path, json.dumps(state.to_json(), indent=2, sort_keys=True) + "\n")


def write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    _atomic_write(path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")


def append_events(path: Path, events: list[Mapping[str, Any]]) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(lines)


# ---------------------------------------------------------------------------
# Single-instance mutex (D4 / B13).
# ---------------------------------------------------------------------------


def acquire_lock(path: Path) -> int | None:
    """Non-blocking ``fcntl.flock``. ``None`` means another instance holds it.

    Directory creation and ``os.open`` are INSIDE the mapped try (review
    round-1 B-C4): a read-only or non-directory lock parent (EACCES / EROFS /
    ENOTDIR) is an operator misconfiguration and must surface as the same
    structured config error as a missing env var, not as a raw traceback out
    of ``main``.

    The mapping is WHOLE-CLASS (round-3 invariant closure). This stage guards
    the whole lane: if it raises, the tick never observes and never mails, so
    an escaping class buys nothing but an unstructured traceback for the same
    permanent silence. ``OSError`` keeps its own arm for the operator-readable
    wording only, exactly like ``default_sendmail_runner``'s arms.
    ``BlockingIOError`` is NOT containment — it is the documented
    lock-contention signal and its meaning is "another tick is running".
    """

    fd: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise FrontierAlertConfigError("lock file must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd
    except FrontierAlertConfigError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as error:
        if fd is not None:
            os.close(fd)
        raise FrontierAlertConfigError(f"cannot acquire lock file: {error}") from error
    except Exception as error:  # noqa: BLE001 - whole-class containment, see docstring
        if fd is not None:
            os.close(fd)
        raise FrontierAlertConfigError(
            f"cannot acquire lock file: {type(error).__name__}: {error}"
        ) from error


def release_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Mail channel (D3).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SendResult:
    returncode: int
    error: str | None = None
    #: Last stderr line of a SUCCESSFUL send, verbatim (redacted downstream).
    #: The authenticated SMTP shim prints one ``SMTP-ACCEPTED host=... code=250
    #: recipients=<n>`` line there; keeping it makes the difference between "the
    #: destination synchronously accepted" and the null-routed-postfix era's
    #: meaningless exit 0 visible in the receipt. ``None`` for a classic
    #: sendmail, which prints nothing.
    evidence: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0


SendmailRunner = Callable[[list[str], str], SendResult]


def default_sendmail_runner(argv: list[str], message: str) -> SendResult:
    """``sendmail -t -i`` over ``subprocess``. No SMTP credentials involved.

    The specific arms below exist for their operator-readable wording only;
    the trailing ``except Exception`` is what makes the containment WHOLE-CLASS
    (round-3 invariant closure). ``message.encode("utf-8")`` raises
    ``UnicodeEncodeError`` — neither an ``OSError`` nor a ``TimeoutExpired`` —
    whenever a surrogate reaches the message, which ``os.environ`` produces for
    free from a single non-UTF-8 byte in ``NHMS_ALERT_EMAIL_TO`` or in the
    hostname. Escaping here killed the tick before ANY state/receipt/event was
    written, so the runner died identically on every tick forever while
    ``--dry-run`` stayed green. A send that cannot even be built is a FAILED
    send, which is the over-report direction: unrecorded stall alerts retry,
    degraded alerts queue in ``degraded_pending``.
    """

    try:
        completed = subprocess.run(
            argv,
            input=message.encode("utf-8"),
            capture_output=True,
            timeout=SENDMAIL_TIMEOUT_SEC,
            check=False,
        )
    except OSError as error:
        return SendResult(returncode=127, error=f"sendmail invocation failed: {error}")
    except subprocess.TimeoutExpired as error:
        return SendResult(returncode=124, error=f"sendmail timed out: {error}")
    except Exception as error:  # noqa: BLE001 - whole-class containment, see docstring
        return SendResult(
            returncode=SEND_INTERNAL_FAILURE_RC,
            error=f"sendmail invocation failed internally: {type(error).__name__}: {error}",
        )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        return SendResult(returncode=completed.returncode, error=stderr or "sendmail exited non-zero")
    # Success still carries evidence: the shim's SMTP-ACCEPTED line is the only
    # proof the destination said 250, and dropping it made a real delivery
    # byte-identical to the null-routed era's fictitious one.
    accepted = [line.strip() for line in completed.stderr.decode("utf-8", "replace").splitlines() if line.strip()]
    return SendResult(returncode=0, evidence=accepted[-1] if accepted else None)


def build_message(
    config: AlertConfig,
    *,
    event: str,
    subject: str,
    now: datetime,
    body_lines: list[str],
) -> str:
    headers = [
        f"From: {config.email_from}",
        f"To: {config.email_to}",
        f"Subject: {subject}",
        f"Date: {format_datetime(now.astimezone(UTC))}",
        f"X-NHMS-Alert-Event: {event}",
        "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"',
    ]
    return "\r\n".join(headers) + "\r\n\r\n" + "\n".join(body_lines) + "\n"


def _hours(value: float) -> str:
    """Render an hour threshold without a trailing ``.0`` (4h, not 4.0h)."""

    return f"{value:g}h"


def _format_duration(delta: timedelta) -> str:
    total = max(delta.total_seconds(), 0.0)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    return f"{hours}h{minutes:02d}m"


def _snapshot_lines(title: str, snapshot: Mapping[str, SourceObservation]) -> list[str]:
    if not snapshot:
        return [f"{title}: (empty)"]
    lines = [f"{title}:"]
    for key in sorted(snapshot):
        entry = snapshot[key]
        lines.append(
            f"  - {key}: frontier={_iso_or_none(entry.frontier)}"
            f" cycles={entry.cycles} latest_created={_iso_or_none(entry.latest_created)}"
        )
    return lines


# ---------------------------------------------------------------------------
# Tick.
# ---------------------------------------------------------------------------


@dataclass
class _Outbox:
    """Collects the emails a tick decided to send (or would send)."""

    config: AlertConfig
    now: datetime
    dry_run: bool
    runner: SendmailRunner
    records: list[dict[str, Any]]

    def send(self, *, event: str, subject: str, body_lines: list[str], detail: str | None = None) -> bool:
        """Attempt one mail. NEVER raises (round-3 invariant closure).

        The whole mail chain — message construction AND the runner call — sits
        inside one whole-class ``except``. Enumerating classes here has already
        failed three review rounds in this file, and the failure geometry is
        always the same: the exception escapes ``run_tick`` before the state,
        receipt and event log are written, so the tick dies identically every
        30 minutes with a frozen receipt and no mail — total silence from the
        component whose entire job is not being silent. Containment must also
        wrap ``build_message``: a runner-only guard leaves message construction
        (header interpolation of operator-supplied values) outside the net.

        ``BaseException`` (KeyboardInterrupt / SystemExit) is deliberately NOT
        caught. A contained failure is recorded as a failed send, which is the
        over-report direction: stall alerts stay unrecorded and retry next
        tick, degraded alerts land in ``degraded_pending``.
        """

        record: dict[str, Any] = {
            "event": event,
            "detail": detail,
            "subject": subject,
            "recipient": self.config.email_to,
            "at": _iso_or_none(self.now),
        }
        if self.dry_run:
            record.update(
                {"sent": False, "dry_run": True, "returncode": None, "error": None, "evidence": None}
            )
            self.records.append(record)
            return False
        try:
            argv = [self.config.sendmail_path, "-t", "-i"]
            message = build_message(
                self.config, event=event, subject=subject, now=self.now, body_lines=body_lines
            )
            result = self.runner(argv, message)
        except Exception as error:  # noqa: BLE001 - whole-class containment, see docstring
            result = SendResult(
                returncode=SEND_INTERNAL_FAILURE_RC,
                error=f"mail chain failed internally: {type(error).__name__}: {error}",
            )
        error = None
        if result.error:
            error = _redact_error_text(result.error, self.config.database_url)
        evidence = None
        if result.evidence:
            # Same chokepoint as ``error``: the delivery channel's own output is
            # operator-supplied text that must never carry the DSN into the
            # receipt/JSONL.
            evidence = _redact_error_text(result.evidence, self.config.database_url)
        record.update(
            {
                "sent": result.ok,
                "dry_run": False,
                "returncode": result.returncode,
                "error": error,
                "evidence": evidence,
            }
        )
        self.records.append(record)
        return result.ok


def _degraded_subject(kind: str, consecutive_failures: int, *, retry: bool = False) -> str:
    if kind == DEGRADED_OBSERVABILITY_UNAVAILABLE:
        if retry and consecutive_failures == 0:
            # A retry after the query recovered: quoting "0 consecutive query
            # failures" would read as nonsense in the operator's inbox.
            return (
                f"[NHMS] {EVENT_DEGRADED}: {DEGRADED_OBSERVABILITY_UNAVAILABLE}"
                " (undelivered alert from an earlier tick)"
            )
        return (
            f"[NHMS] {EVENT_DEGRADED}: {DEGRADED_OBSERVABILITY_UNAVAILABLE}"
            f" ({consecutive_failures} consecutive query failures)"
        )
    return f"[NHMS] {EVENT_DEGRADED}: {kind} on node-27 frontier alerter"


def _degraded_body(
    config: AlertConfig,
    *,
    kind: str,
    reason: str,
    retry: bool,
    now: datetime,
    state: AlertState,
    observation: Snapshot | None,
    observation_error: str | None,
    stall_for: timedelta,
) -> list[str]:
    lines: list[str] = []
    if retry:
        lines += [
            "(retry: a previous tick raised this alert but the send failed;"
            " the alert was persisted rather than dropped)",
            "",
        ]
    if kind == DEGRADED_STATE_CORRUPT:
        lines += [
            "The frontier alerter's persisted state was unusable and has been rebuilt.",
            f"Reason: {reason}",
            f"Baseline reset at: {_iso_or_none(state.baseline_reset_at)}",
            "",
            "The stall clock was NOT silently reset in the operator's favour:"
            " the rebuild is recorded and the next stall window starts now.",
            "",
        ]
        if state.baseline_pending:
            lines += [
                "Baseline is PENDING: the observation query failed on the same"
                " tick, so no baseline was invented. The next successful"
                " observation fills it silently and does not count as progress.",
                f"Observation error: {observation_error}",
            ]
        else:
            lines += _snapshot_lines("Rebuilt baseline", state.snapshot)
    elif retry and state.consecutive_query_failures == 0:
        lines += [
            "An observability-unavailable alert was raised on an earlier tick and"
            " could not be delivered then. The observation query has since"
            " succeeded — this mail exists so the outage is not lost from the"
            " operator's timeline.",
            f"Original error: {reason}",
        ]
    else:
        lines += [
            f"The frontier observation query has failed {state.consecutive_query_failures}"
            f" consecutive ticks (budget {config.query_fail_ticks}).",
            f"Last error: {reason}",
            "",
            "The stall clock keeps running across query failures:"
            f" last observed change {_iso_or_none(state.last_change_at)}"
            f" ({_format_duration(stall_for)} ago).",
        ]
    lines += ["", f"Now: {_iso_or_none(now)}", f"Runbook: {RUNBOOK_REFERENCE}"]
    return lines


def _emit_log(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def run_tick(
    config: AlertConfig,
    *,
    now: datetime,
    observe: ObservationProvider | None = None,
    sendmail_runner: SendmailRunner | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """One observation tick. Returns the receipt payload (also persisted).

    Never raises for observation/mail/state failures: each one is a D2 row that
    resolves into *more* alerting, a receipt entry and a non-zero exit code.
    """

    observe = default_observe if observe is None else observe
    runner = default_sendmail_runner if sendmail_runner is None else sendmail_runner
    now = now.astimezone(UTC)
    outbox = _Outbox(config=config, now=now, dry_run=dry_run, runner=runner, records=[])

    load = load_state(config.state_path)

    observation: Snapshot | None = None
    observation_error: str | None = None
    try:
        observation = observe(config)
    except Exception as error:  # noqa: BLE001 - D2 row 3: any query failure
        observation_error = _redact_error_text(error, config.database_url)

    rebuilt = not (load.status == STATE_LOAD_OK and load.state is not None)
    if not rebuilt:
        state = load.state  # type: ignore[assignment]
    else:
        # Missing (bootstrap) and corrupt (degraded) both rebuild the baseline
        # from the current observation; they differ only in whether an email
        # goes out and which timestamp is recorded (D2 rows 1+2).
        state = AlertState(snapshot={}, last_change_at=now)
        if observation is None:
            # A-C1: no observation means no baseline. Recording an empty
            # snapshot as established would make every source look new on the
            # next successful tick — forged progress worth the whole outage.
            state.baseline_pending = True
            state.baseline_pending_kind = (
                BASELINE_ORIGIN_BOOTSTRAP
                if load.status == STATE_LOAD_MISSING
                else BASELINE_ORIGIN_RESET
            )
        else:
            state.snapshot = dict(observation)
            if load.status == STATE_LOAD_MISSING:
                state.baseline_established_at = now
            else:
                state.baseline_reset_at = now

    corrupt_reason = (
        _redact_error_text(load.reason or "state unusable", config.database_url)
        if load.status == STATE_LOAD_CORRUPT
        else None
    )

    progressed = False
    progress_reasons: tuple[str, ...] = ()
    baseline_filled = False
    if observation is not None:
        if rebuilt:
            # Freshly (re)built baseline: the observation IS the baseline (or,
            # when it was unobtainable, the baseline stays pending), so there
            # is nothing to compare against this tick.
            pass
        elif state.baseline_pending:
            # A-C1 fill: a pending baseline is filled SILENTLY. Not progress,
            # ``last_change_at`` untouched (the stall clock keeps running from
            # the rebuild point) and ``alert_active`` untouched.
            state.snapshot = dict(observation)
            state.baseline_pending = False
            baseline_filled = True
            # Stamp the field matching the ORIGIN of the pending (1S.4): a
            # corrupt-origin rebuild is a recorded degradation, not a fresh
            # installation, and the degraded retry mail on this same tick
            # prints "Baseline reset at" from it.
            if state.baseline_pending_kind == BASELINE_ORIGIN_RESET:
                state.baseline_reset_at = now
            elif state.baseline_established_at is None:
                state.baseline_established_at = now
        else:
            progress = evaluate_progress(state.snapshot, observation)
            progressed = progress.progressed
            progress_reasons = progress.reasons
            state.snapshot = progress.baseline
        state.consecutive_query_failures = 0
        state.last_error = None
    else:
        # D2 row 3: a query failure NEVER touches ``last_change_at``; the stall
        # clock keeps running so a stall still fires on time.
        state.consecutive_query_failures += 1
        state.last_error = observation_error

    recovered_sent = False
    if progressed:
        state.last_change_at = now
        if state.alert_active:
            stall_end_lines = [
                f"Frontier progress resumed at {_iso_or_none(now)}.",
                "",
                "Progress evidence:",
                *[f"  - {reason}" for reason in progress_reasons],
                "",
                *_snapshot_lines("Current high-water baseline", state.snapshot),
                "",
                f"Runbook: {RUNBOOK_REFERENCE}",
            ]
            recovered_sent = outbox.send(
                event=EVENT_RECOVERED,
                subject="[NHMS] frontier-recovered: node-27 frontier is advancing again",
                body_lines=stall_end_lines,
            )
            # Exactly one recovery email: the alert closes regardless of the
            # send result. A recovery that failed to send is recorded in the
            # receipt, it must not re-arm a stall that no longer exists.
            state.alert_active = False
            state.last_alert_at = None

    stall_for = now - state.last_change_at
    stalled = stall_for >= config.stall_delta
    stall_event: str | None = None
    if stalled:
        due = (
            not state.alert_active
            or state.last_alert_at is None
            or (now - state.last_alert_at) >= config.resend_delta
        )
        if due:
            # D3 / review round-1 C-C2: the label follows DELIVERY fact, not
            # ``alert_active``. A first alert whose send failed leaves
            # ``alert_active`` true with ``last_alert_at`` unset; its retry is
            # still the first delivered alert of this stall and must say so —
            # the operator rebuilds the outage timeline from these labels.
            stall_event = "initial" if state.last_alert_at is None else "resend"
            body = [
                f"No directional frontier progress for {_format_duration(stall_for)}"
                f" (threshold {_hours(config.stall_hours)}).",
                f"Last observed change: {_iso_or_none(state.last_change_at)}",
                f"Now: {_iso_or_none(now)}",
                f"Alert kind: {stall_event}; resend window {_hours(config.resend_hours)}",
                "",
                *_snapshot_lines("High-water baseline", state.snapshot),
                "",
                *(
                    _snapshot_lines("Current observation", observation)
                    if observation is not None
                    else [f"Current observation: UNAVAILABLE ({observation_error})"]
                ),
                "",
                "Progress criterion: a new source, or a strict increase of"
                " frontier / distinct-cycle count / latest arrival.",
                f"Runbook: {RUNBOOK_REFERENCE}",
            ]
            sent = outbox.send(
                event=EVENT_STALLED,
                detail=stall_event,
                subject=(
                    f"[NHMS] frontier-stalled: no progress for {_format_duration(stall_for)}"
                    f" (threshold {_hours(config.stall_hours)})"
                ),
                body_lines=body,
            )
            state.alert_active = True
            if sent:
                state.last_alert_at = now
            # D2 row 4: a failed send is NOT recorded as delivered, so the next
            # tick retries immediately.

    # ---- degraded family: retries first, then this tick's new events -------
    # The dedup clock is PER KIND (round-2 P2). A single family clock let a
    # delivered ``state-corrupt`` mail throttle the FIRST EVER
    # ``observability-unavailable`` — an event class the operator had never
    # been told about, silently dropped for a whole resend window. A corrupt
    # state carries no clocks at all (they were unreadable), so that branch
    # always sends: the accepted over-report of D2 row 2.
    pending_before = [dict(entry) for entry in state.degraded_pending]
    queue: list[dict[str, Any]] = [
        {"kind": entry["kind"], "reason": entry.get("reason", ""), "retry": True}
        for entry in pending_before
    ]
    if load.status == STATE_LOAD_CORRUPT:
        queue.append(
            {"kind": DEGRADED_STATE_CORRUPT, "reason": corrupt_reason or "", "retry": False}
        )
    if observation is None and state.consecutive_query_failures >= config.query_fail_ticks:
        queue.append(
            {
                "kind": DEGRADED_OBSERVABILITY_UNAVAILABLE,
                "reason": observation_error or "",
                "retry": False,
            }
        )
    # One email per kind per tick: a fresh occurrence supersedes the retry's
    # (older) reason text but INHERITS its retry flag — the undelivered alert
    # is still undelivered, so if this tick is throttled it must stay pending
    # rather than be silently absorbed by the fresh occurrence.
    deduped: dict[str, dict[str, Any]] = {}
    for entry in queue:
        previous = deduped.get(entry["kind"])
        if previous is None:
            deduped[entry["kind"]] = dict(entry)
        elif not entry["retry"]:
            deduped[entry["kind"]] = {
                "kind": entry["kind"],
                "reason": entry["reason"],
                "retry": previous["retry"] or entry["retry"],
            }

    degraded_events: list[str] = []
    still_pending: list[dict[str, str]] = []
    for kind in (DEGRADED_STATE_CORRUPT, DEGRADED_OBSERVABILITY_UNAVAILABLE):
        entry = deduped.get(kind)
        if entry is None:
            continue
        last_for_kind = state.last_degraded_alert_by_kind.get(kind)
        degraded_due = (
            last_for_kind is None or (now - last_for_kind) >= config.resend_delta
        )
        if not degraded_due:
            # Throttled, not lost: a previously FAILED send stays pending so a
            # later tick retries it. A throttled FRESH occurrence is dropped,
            # and with per-kind clocks that is now literally true: a set clock
            # for this kind means a mail of this kind was actually delivered
            # inside the window, so this one is a repeat.
            if entry["retry"]:
                still_pending.append({"kind": kind, "reason": entry["reason"]})
            continue
        body = _degraded_body(
            config,
            kind=kind,
            reason=entry["reason"],
            retry=bool(entry["retry"]),
            now=now,
            state=state,
            observation=observation,
            observation_error=observation_error,
            stall_for=stall_for,
        )
        sent = outbox.send(
            event=EVENT_DEGRADED,
            detail=kind,
            subject=_degraded_subject(
                kind, state.consecutive_query_failures, retry=bool(entry["retry"])
            ),
            body_lines=body,
        )
        degraded_events.append(kind)
        if sent:
            state.last_degraded_alert_by_kind[kind] = now
        elif not dry_run:
            # A-C2: a degraded alert that failed to send is NOT lost; it is
            # persisted and retried on the next tick under the same clock.
            still_pending.append({"kind": kind, "reason": entry["reason"]})
    # ``--dry-run`` decides nothing durable: the queue it would have drained
    # stays exactly as it was on disk, and the receipt reports that truth.
    state.degraded_pending = pending_before if dry_run else still_pending

    send_failures = [record for record in outbox.records if not record["dry_run"] and not record["sent"]]
    receipt: dict[str, Any] = {
        "runner": "node27_frontier_stall_alert",
        "schema_version": STATE_SCHEMA_VERSION,
        "generated_at": _iso_or_none(now),
        "dry_run": dry_run,
        "state_load": load.status,
        "state_load_reason": corrupt_reason,
        "observation_status": "ok" if observation is not None else "failed",
        "observation_error": observation_error,
        "observation": snapshot_to_json(observation) if observation is not None else None,
        "baseline": snapshot_to_json(state.snapshot),
        "progress": progressed,
        "progress_reasons": list(progress_reasons),
        "baseline_pending": state.baseline_pending,
        "baseline_filled": baseline_filled,
        "degraded_pending": [dict(entry) for entry in state.degraded_pending],
        "last_change_at": _iso_or_none(state.last_change_at),
        "stall_seconds": stall_for.total_seconds(),
        "stall_hours_threshold": config.stall_hours,
        "resend_hours": config.resend_hours,
        "stalled": stalled,
        "stall_alert": stall_event,
        "alert_active": state.alert_active,
        "last_alert_at": _iso_or_none(state.last_alert_at),
        "recovered": any(record["event"] == EVENT_RECOVERED for record in outbox.records),
        "recovered_sent": recovered_sent,
        "degraded_events": degraded_events,
        "consecutive_query_failures": state.consecutive_query_failures,
        "baseline_established_at": _iso_or_none(state.baseline_established_at),
        "baseline_reset_at": _iso_or_none(state.baseline_reset_at),
        "emails": outbox.records,
        "send_failures": len(send_failures),
        "status": _tick_status(
            observation is not None, bool(send_failures), bool(degraded_events), stalled
        ),
    }

    if not dry_run:
        write_state(config.state_path, state)
        write_receipt(config.receipt_path, receipt)
        append_events(
            config.events_path,
            [
                {
                    "generated_at": _iso_or_none(now),
                    "event": record["event"],
                    "detail": record["detail"],
                    "subject": record["subject"],
                    "sent": record["sent"],
                    "returncode": record["returncode"],
                    "error": record["error"],
                    "evidence": record["evidence"],
                }
                for record in outbox.records
            ],
        )
    return receipt


def _tick_status(observation_ok: bool, send_failed: bool, degraded: bool, stalled: bool) -> str:
    if not observation_ok or send_failed or degraded:
        return "degraded"
    return "stalled" if stalled else "ok"


def receipt_exit_code(receipt: Mapping[str, Any]) -> int:
    return 0 if receipt.get("status") in {"ok", "stalled"} else 1


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="node-27 frontier stall alerting (issue #1368)")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run exactly one tick (the only supported mode; kept for wrapper symmetry)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate the full criterion and print the actions that would happen; zero side effects",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    now: datetime | None = None,
    observe: ObservationProvider | None = None,
    sendmail_runner: SendmailRunner | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    env_map = os.environ if env is None else env
    try:
        config = config_from_env(env_map)
    except FrontierAlertConfigError as error:
        # Fail closed BEFORE any observation and before any send (D2 last row).
        print(
            json.dumps(
                {"status": "failed", "code": CODE_CONFIG_INVALID, "reason": str(error)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as error:  # noqa: BLE001 - whole-class containment
        # Round-3 invariant closure: the config stage guards the whole lane, so
        # an escaping class means the same permanent silence as a rejected
        # config — but as a raw traceback, which is unstructured AND can echo
        # the DSN past the redaction chokepoint into journald.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": CODE_CONFIG_INVALID,
                    "reason": f"{type(error).__name__}: "
                    f"{_redact_error_text(error, env_map.get('DATABASE_URL', ''))}",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    stamp = (now or datetime.now(UTC)).astimezone(UTC)

    try:
        lock_fd = acquire_lock(config.lock_path)
    except FrontierAlertConfigError as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": CODE_CONFIG_INVALID,
                    "reason": _redact_error_text(error, config.database_url),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    if lock_fd is None:
        # D4 / B13: another tick is still running. Structured no-op, exit 0,
        # zero observation, zero email.
        _emit_log(
            {
                "status": "skipped",
                "code": CODE_CONCURRENT_INVOCATION,
                "generated_at": _iso_or_none(stamp),
                "lock_path": str(config.lock_path),
            }
        )
        return 0

    try:
        try:
            receipt = run_tick(
                config,
                now=stamp,
                observe=observe,
                sendmail_runner=sendmail_runner,
                dry_run=args.dry_run,
            )
        except Exception as error:  # noqa: BLE001 - never dump a raw traceback
            _emit_log(
                {
                    "status": "failed",
                    "code": "FRONTIER_ALERT_UNCAUGHT_ERROR",
                    "generated_at": _iso_or_none(stamp),
                    "reason": f"{type(error).__name__}: "
                    f"{_redact_error_text(error, config.database_url)}",
                }
            )
            return 1
    finally:
        release_lock(lock_fd)

    _emit_log(
        {
            "status": receipt["status"],
            "generated_at": receipt["generated_at"],
            "dry_run": receipt["dry_run"],
            "state_load": receipt["state_load"],
            "observation_status": receipt["observation_status"],
            "observation_error": receipt["observation_error"],
            "progress": receipt["progress"],
            "stalled": receipt["stalled"],
            "stall_alert": receipt["stall_alert"],
            "alert_active": receipt["alert_active"],
            "degraded_events": receipt["degraded_events"],
            "emails": [
                {
                    "event": record["event"],
                    "detail": record["detail"],
                    "subject": record["subject"],
                    "sent": record["sent"],
                    "dry_run": record["dry_run"],
                    "error": record["error"],
                }
                for record in receipt["emails"]
            ],
            "consecutive_query_failures": receipt["consecutive_query_failures"],
        }
    )
    return receipt_exit_code(receipt)


if __name__ == "__main__":
    raise SystemExit(main())
