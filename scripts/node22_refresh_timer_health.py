#!/usr/bin/env python3
"""Read-only health probe for the node-22 DB-free file-provider refresh lane.

Issue #2146 / #2041.  The refresh timer can sit at ``UnitFileState=enabled``
with ``ActiveState=inactive`` and no next elapse: it looks green, never fires,
and nothing notices until the published manifest crosses the consumer's
168-hour freshness bound and compute goes ``file_manifest_stale`` ->
``db_free_registry_blocked``.  This probe grades four independent signals ---
``UnitFileState``, ``ActiveState``, the next elapse, and the published
manifest's age --- into exactly one verdict, writes a bounded receipt, and
exits non-zero for every verdict other than ``ok``.

Two structural properties are load-bearing (OpenSpec change
``harden-node22-scheduler-refresh-lane``, decision D4):

* **It never mutates a systemd unit.**  It shells out only to
  ``systemctl --user show`` and ``systemctl --user list-timers``, both
  read-only.  No mutation verb appears anywhere in this file, and a test
  scans the source to keep it that way.  Detection only --- there is no
  self-heal, by decision D1.
* **It is self-contained.**  Standard library only, no import of
  ``packages.common`` or any other repo package, so it can be staged and run
  from outside ``/scratch/frd_muziyao/NWM`` --- checking a feature branch out
  in that tree would deploy unreviewed code into the live 02:15Z tick.  That
  is why the bounded no-follow receipt read below is carried here rather than
  imported from ``packages/common/safe_fs.py``.

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

RECEIPT_SCHEMA_VERSION = "nhms.node22.refresh_timer_health.v1"

# Duplicated on purpose: the probe must not import
# `scripts/scheduler_file_provider_refresh.py` (D4, self-contained).  This is
# the schema the refresh runner writes to its receipt root.
REFRESH_RECEIPT_SCHEMA_VERSION = "nhms.scheduler.file_provider_refresh_receipt.v1"

# The consumer's hard bound, `DEFAULT_MAX_MANIFEST_AGE_HOURS` in
# `services/orchestrator/scheduler_file_providers.py`.  This probe never
# changes it; it only grades against it and keeps its own thresholds under it.
CONSUMER_MAX_MANIFEST_AGE_HOURS = 168

DEFAULT_UNIT = "nhms-scheduler-file-provider-refresh.timer"
DEFAULT_SYSTEMCTL = "/usr/bin/systemctl"
DEFAULT_REFRESH_RECEIPT = "/scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/receipts/latest.json"
DEFAULT_HEALTH_RECEIPT_ROOT = "/scratch/frd_muziyao/nhms-prod/workspace/refresh-timer-health/receipts"

DEFAULT_MAX_NEXT_DWELL_HOURS = 36
DEFAULT_MAX_MANIFEST_AGE_HOURS = 120
DEFAULT_STOPPED_DWELL_HOURS = 6

ENV_UNIT = "NHMS_REFRESH_HEALTH_UNIT"
ENV_SYSTEMCTL = "NHMS_REFRESH_HEALTH_SYSTEMCTL"
ENV_REFRESH_RECEIPT = "NHMS_REFRESH_HEALTH_REFRESH_RECEIPT"
ENV_HEALTH_RECEIPT_ROOT = "NHMS_REFRESH_HEALTH_RECEIPT_ROOT"
ENV_NOW = "NHMS_REFRESH_HEALTH_NOW"
ENV_JSON = "NHMS_REFRESH_HEALTH_JSON"
ENV_MAX_NEXT_DWELL_HOURS = "NHMS_REFRESH_HEALTH_MAX_NEXT_DWELL_HOURS"
ENV_MAX_MANIFEST_AGE_HOURS = "NHMS_REFRESH_HEALTH_MAX_MANIFEST_AGE_HOURS"
ENV_STOPPED_DWELL_HOURS = "NHMS_REFRESH_HEALTH_STOPPED_DWELL_HOURS"

SHOW_PROPERTIES = (
    "UnitFileState",
    "ActiveState",
    "SubState",
    "InactiveEnterTimestamp",
    "NextElapseUSecRealtime",
    "LastTriggerUSec",
)

# Bounded reads: the refresh receipt is a bounded JSON document and the probe's
# own receipt is a fixed handful of scalars.  Both caps are fail-closed.
MAX_REFRESH_RECEIPT_BYTES = 1024 * 1024
MAX_HEALTH_RECEIPT_BYTES = 8192
MAX_SIGNAL_LENGTH = 256
SYSTEMCTL_TIMEOUT_SECONDS = 30

# systemd prints these for a timestamp it does not have.
EMPTY_TIMESTAMPS = frozenset({"", "-", "n/a", "0"})

VERDICT_OK = "ok"
VERDICT_PROBE_FAILED = "probe_failed"
VERDICT_MANIFEST_EXPIRED = "manifest_expired"
VERDICT_TIMER_STOPPED = "timer_stopped"
VERDICT_TIMER_NOT_ENABLED = "timer_not_enabled"
VERDICT_TIMER_NOT_SCHEDULED = "timer_not_scheduled"
VERDICT_MANIFEST_STALE = "manifest_stale"


class ConfigError(Exception):
    """Refusal raised before any evidence is collected."""


class ProbeEvidenceError(Exception):
    """An evidence source could not be read or did not validate."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class Thresholds:
    """The three operator-tunable bounds, validated at config time."""

    __slots__ = ("max_next_dwell_hours", "max_manifest_age_hours", "stopped_dwell_hours")

    def __init__(
        self,
        *,
        max_next_dwell_hours: int,
        max_manifest_age_hours: int,
        stopped_dwell_hours: int,
    ) -> None:
        self.max_next_dwell_hours = max_next_dwell_hours
        self.max_manifest_age_hours = max_manifest_age_hours
        self.stopped_dwell_hours = stopped_dwell_hours


def _positive_int(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from error
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


def load_thresholds(env: dict[str, str] | None = None) -> Thresholds:
    """Resolve the three thresholds from the environment and validate them.

    Both freshness thresholds must sit strictly under the consumer's 168-hour
    bound: a threshold at or above it can never fire before the consumer has
    already fail-closed, which is the whole gap this probe exists to close.
    """
    source = os.environ if env is None else env
    thresholds = Thresholds(
        max_next_dwell_hours=_positive_int(
            ENV_MAX_NEXT_DWELL_HOURS,
            source.get(ENV_MAX_NEXT_DWELL_HOURS) or str(DEFAULT_MAX_NEXT_DWELL_HOURS),
        ),
        max_manifest_age_hours=_positive_int(
            ENV_MAX_MANIFEST_AGE_HOURS,
            source.get(ENV_MAX_MANIFEST_AGE_HOURS) or str(DEFAULT_MAX_MANIFEST_AGE_HOURS),
        ),
        stopped_dwell_hours=_positive_int(
            ENV_STOPPED_DWELL_HOURS,
            source.get(ENV_STOPPED_DWELL_HOURS) or str(DEFAULT_STOPPED_DWELL_HOURS),
        ),
    )
    for name, value in (
        (ENV_MAX_NEXT_DWELL_HOURS, thresholds.max_next_dwell_hours),
        (ENV_MAX_MANIFEST_AGE_HOURS, thresholds.max_manifest_age_hours),
    ):
        if value >= CONSUMER_MAX_MANIFEST_AGE_HOURS:
            raise ConfigError(
                f"{name}={value} must be strictly under the consumer bound "
                f"of {CONSUMER_MAX_MANIFEST_AGE_HOURS} hours"
            )
    return thresholds


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------


def parse_systemd_timestamp(raw: str) -> datetime | None:
    """Parse a ``systemctl show`` timestamp such as ``Fri 2026-08-28 00:11:18 CST``.

    ``%Z`` in ``strptime`` only accepts the running machine's own zone
    abbreviations, so it parses on node-22 and fails anywhere else.  Instead the
    zone token is handled explicitly: ``UTC``/``GMT``/``Z`` are read as UTC and
    anything else is read as the local zone, which is what systemd emitted.

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
    _weekday, date_token, time_token, zone_token = tokens[0], tokens[1], tokens[2], tokens[3]
    moment = datetime.strptime(f"{date_token} {time_token}", "%Y-%m-%d %H:%M:%S")
    if zone_token.upper() in {"UTC", "GMT", "Z"}:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone()


def parse_iso8601(raw: str) -> datetime:
    """Parse a receipt ISO-8601 instant; a naive value is a refusal."""
    value = (raw or "").strip()
    if not value:
        raise ValueError("empty ISO-8601 instant")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"ISO-8601 instant must carry a timezone: {value!r}")
    return moment


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
        raise ProbeEvidenceError(
            f"systemctl {' '.join(arguments)} exited {completed.returncode}"
        )
    return completed.stdout


def collect_systemd_signals(*, systemctl: str, unit: str) -> dict[str, str]:
    """Return the six raw ``systemctl show`` properties for ``unit``.

    ``list-timers`` is queried as well, per the change's read surface: it is the
    operator-facing rendering of the same schedule, and a timer subsystem that
    cannot answer it is unreadable evidence.  Its text is not graded --- the
    verdict's next-elapse signal is ``NextElapseUSecRealtime`` from ``show`` ---
    and it is deliberately kept out of the receipt, whose field set is closed.
    """
    output = _run_systemctl(systemctl, ["show", unit, "-p", ",".join(SHOW_PROPERTIES)])
    properties: dict[str, str] = {name: "" for name in SHOW_PROPERTIES}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in properties:
            properties[key] = value.strip()
    _run_systemctl(systemctl, ["list-timers", unit])
    return properties


# ---------------------------------------------------------------------------
# Evidence collection --- refresh receipt (bounded, no-follow)
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
            raise ProbeEvidenceError(f"refresh receipt is not a regular file: {path}")
        content = os.read(descriptor, max_bytes + 1)
    finally:
        os.close(descriptor)
    if len(content) > max_bytes:
        raise ProbeEvidenceError(f"refresh receipt exceeds {max_bytes} bytes: {path}")
    return content


def read_manifest_generated_at(path: Path) -> datetime:
    """Return the published manifest's ``generated_at`` from the refresh receipt.

    The value of record is the canonical registry provider's
    ``after_generated_at`` --- the provider is located by name, never by index,
    so a receipt whose provider list is shaped differently fails closed instead
    of reading the wrong row.
    """
    try:
        content = read_bounded_no_follow(path, max_bytes=MAX_REFRESH_RECEIPT_BYTES)
    except ProbeEvidenceError:
        raise
    except OSError as error:
        raise ProbeEvidenceError(f"refresh receipt unreadable: {path}: {error}") from error
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProbeEvidenceError(f"refresh receipt is not valid JSON: {path}") from error
    if not isinstance(payload, dict):
        raise ProbeEvidenceError("refresh receipt is not a JSON object")
    if payload.get("schema_version") != REFRESH_RECEIPT_SCHEMA_VERSION:
        raise ProbeEvidenceError(
            f"refresh receipt schema_version is not {REFRESH_RECEIPT_SCHEMA_VERSION}"
        )
    providers = payload.get("providers")
    if not isinstance(providers, list):
        raise ProbeEvidenceError("refresh receipt has no providers list")
    for provider in providers:
        if isinstance(provider, dict) and provider.get("name") == "registry":
            generated_at = provider.get("after_generated_at")
            if not isinstance(generated_at, str):
                raise ProbeEvidenceError("registry provider has no after_generated_at")
            try:
                return parse_iso8601(generated_at)
            except ValueError as error:
                raise ProbeEvidenceError(
                    f"registry after_generated_at is unparseable: {error}"
                ) from error
    raise ProbeEvidenceError("refresh receipt carries no registry provider")


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def grade(
    *,
    now: datetime,
    properties: dict[str, str],
    manifest_age_hours: float | None,
    thresholds: Thresholds,
    evidence_error: str | None,
) -> str:
    """Return exactly one verdict, first match wins.

    The order is fixed by decision D3 and is what makes the grading total:
    ``ok`` is a pure ``else`` reached only by falling through every failing
    condition, never by matching a positive predicate of its own.
    """
    # 1. Missing or undefined evidence can never fall through to healthy.
    if evidence_error is not None or manifest_age_hours is None:
        return VERDICT_PROBE_FAILED

    active_state = properties.get("ActiveState", "")
    unit_file_state = properties.get("UnitFileState", "")

    if active_state != "active":
        # The dwell arithmetic is undefined without a parseable instant.
        try:
            became_inactive = parse_systemd_timestamp(properties.get("InactiveEnterTimestamp", ""))
        except ValueError:
            return VERDICT_PROBE_FAILED
        if became_inactive is None:
            return VERDICT_PROBE_FAILED
    else:
        became_inactive = None

    # 2. The consumer is already fail-closed; that outranks every timer verdict.
    if manifest_age_hours >= CONSUMER_MAX_MANIFEST_AGE_HOURS:
        return VERDICT_MANIFEST_EXPIRED

    # 3. Idle past the dwell.  No `enabled` predicate here on purpose: a
    #    `disabled` timer that is also idle is reported as stopped, which is
    #    the operationally urgent fact.  Inside the dwell (a live #1104
    #    manual-publisher window) grading continues on the remaining signals.
    if became_inactive is not None:
        idle = now - became_inactive
        if idle > timedelta(hours=thresholds.stopped_dwell_hours):
            return VERDICT_TIMER_STOPPED

    # 4. A timer that is not `enabled` will not survive a reload or a reboot.
    if unit_file_state != "enabled":
        return VERDICT_TIMER_NOT_ENABLED

    # 5. Running, but with no tick coming inside the next-dwell.
    if active_state == "active":
        try:
            next_elapse = parse_systemd_timestamp(properties.get("NextElapseUSecRealtime", ""))
        except ValueError:
            return VERDICT_TIMER_NOT_SCHEDULED
        if next_elapse is None:
            return VERDICT_TIMER_NOT_SCHEDULED
        if next_elapse - now > timedelta(hours=thresholds.max_next_dwell_hours):
            return VERDICT_TIMER_NOT_SCHEDULED

    # 6. Manifest freshness, graded independently of every systemd signal.
    #    At-or-over, never strictly-greater: an age exactly equal to the
    #    threshold is already a finding.
    if manifest_age_hours >= thresholds.max_manifest_age_hours:
        return VERDICT_MANIFEST_STALE

    return VERDICT_OK


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def _clip(value: str) -> str:
    return value[:MAX_SIGNAL_LENGTH]


def build_receipt(
    *,
    now: datetime,
    unit: str,
    verdict: str,
    properties: dict[str, str],
    manifest_age_hours: float | None,
    thresholds: Thresholds,
) -> dict[str, Any]:
    """Assemble the verdict document.

    The field set is closed on purpose: the raw signals, the three integer
    thresholds and the inspected unit name.  No path, no other environment
    value, nothing that would turn an ops receipt into a configuration leak.
    """
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "generated_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "verdict": verdict,
        "unit": _clip(unit),
        "unit_file_state": _clip(properties.get("UnitFileState", "")),
        "active_state": _clip(properties.get("ActiveState", "")),
        "sub_state": _clip(properties.get("SubState", "")),
        "inactive_enter_timestamp": _clip(properties.get("InactiveEnterTimestamp", "")),
        "next_elapse": _clip(properties.get("NextElapseUSecRealtime", "")),
        "last_trigger": _clip(properties.get("LastTriggerUSec", "")),
        "manifest_age_hours": manifest_age_hours,
        "max_next_dwell_hours": thresholds.max_next_dwell_hours,
        "max_manifest_age_hours": thresholds.max_manifest_age_hours,
        "stopped_dwell_hours": thresholds.stopped_dwell_hours,
    }


def make_private_directory(path: Path) -> None:
    """Create ``path`` and every missing ancestor at 0700.

    ``Path.mkdir(mode=..., parents=True)`` applies ``mode`` to the LEAF only --
    intermediate levels are created with the default 0777 minus umask, which on
    an interactive shell leaves a world-readable directory above a receipt root
    that D2 requires to be private.  ``mode`` can only clear bits, never set
    them, so creating each level at 0700 is safe under any umask.
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


def write_receipt(root: Path, receipt: dict[str, Any]) -> Path:
    """Write the receipt 0600 under a 0700 root, refusing a symlinked target."""
    make_private_directory(root)
    content = (json.dumps(receipt, sort_keys=True) + "\n").encode()
    if len(content) > MAX_HEALTH_RECEIPT_BYTES:
        raise ProbeEvidenceError(f"probe receipt exceeds {MAX_HEALTH_RECEIPT_BYTES} bytes")
    target = root / "latest.json"
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, content)
    finally:
        os.close(descriptor)
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node22_refresh_timer_health.py",
        description=(
            "Read-only health probe for the node-22 file-provider refresh timer. "
            "It observes and reports; it never modifies a systemd unit."
        ),
    )
    parser.add_argument(
        "--unit",
        default=os.environ.get(ENV_UNIT) or DEFAULT_UNIT,
        help=f"timer unit to inspect (env {ENV_UNIT}, default {DEFAULT_UNIT})",
    )
    parser.add_argument(
        "--refresh-receipt",
        default=os.environ.get(ENV_REFRESH_RECEIPT) or DEFAULT_REFRESH_RECEIPT,
        help=f"refresh runner receipt to read (env {ENV_REFRESH_RECEIPT})",
    )
    parser.add_argument(
        "--health-receipt-root",
        default=os.environ.get(ENV_HEALTH_RECEIPT_ROOT) or DEFAULT_HEALTH_RECEIPT_ROOT,
        help=f"directory this probe writes its own receipt to (env {ENV_HEALTH_RECEIPT_ROOT})",
    )
    parser.add_argument(
        "--now",
        default=os.environ.get(ENV_NOW) or "",
        help=f"pin the clock to an ISO-8601 instant with a timezone (env {ENV_NOW})",
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        thresholds = load_thresholds()
        now = resolve_now(arguments.now)
    except ConfigError as error:
        print(f"node22-refresh-timer-health: {error}", file=sys.stderr)
        return 2

    systemctl = os.environ.get(ENV_SYSTEMCTL) or DEFAULT_SYSTEMCTL
    evidence_errors: list[str] = []

    properties: dict[str, str] = {name: "" for name in SHOW_PROPERTIES}
    try:
        properties = collect_systemd_signals(systemctl=systemctl, unit=arguments.unit)
    except ProbeEvidenceError as error:
        evidence_errors.append(str(error))

    # The manifest signal is computed independently of the systemd signals, so
    # the receipt still carries an age when systemd is unreadable and still
    # carries the systemd fields when the receipt is unreadable.  That is a
    # statement about the receipt's completeness only: an unreadable source
    # always grades `probe_failed`.
    manifest_age_hours: float | None = None
    try:
        generated_at = read_manifest_generated_at(Path(arguments.refresh_receipt))
        manifest_age_hours = round((now - generated_at).total_seconds() / 3600.0, 4)
    except (ProbeEvidenceError, OSError) as error:
        evidence_errors.append(str(error))

    verdict = grade(
        now=now,
        properties=properties,
        manifest_age_hours=manifest_age_hours,
        thresholds=thresholds,
        evidence_error="; ".join(evidence_errors) if evidence_errors else None,
    )
    receipt = build_receipt(
        now=now,
        unit=arguments.unit,
        verdict=verdict,
        properties=properties,
        manifest_age_hours=manifest_age_hours,
        thresholds=thresholds,
    )

    try:
        write_receipt(Path(arguments.health_receipt_root), receipt)
    except (ProbeEvidenceError, OSError) as error:
        print(f"node22-refresh-timer-health: receipt not written: {error}", file=sys.stderr)
        if arguments.json:
            print(json.dumps(receipt, sort_keys=True))
        return 1

    if arguments.json:
        print(json.dumps(receipt, sort_keys=True))
    if verdict != VERDICT_OK:
        for message in evidence_errors:
            print(f"node22-refresh-timer-health: {message}", file=sys.stderr)
        print(f"node22-refresh-timer-health: verdict={verdict}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
