#!/usr/bin/env python3
"""node-27 parse-stage failure residency alert (issue #2529, design D5).

On 2026-09-19 two autopipe ticks exited rc=1 with 11 ``OUTPUT_PARSE_DB_ERROR``
(57014 statement timeout) failures, and nothing noticed: the autopipe unit has
no ``OnFailure=``, the frontier lane (4 h) and the coverage lane (days) cannot
reach an hour-scale failure, and the parse retry is unbounded and uncounted.
That burst healed within three ticks; the one that does not heal (#1781's
shape: permanent rc=1, found by a manual grep after weeks) is what this lane
exists for.

Criterion — RESIDENCY, not a tick's rc:

- **watched set**: ``hydro.hydro_run`` rows with ``status = 'failed'``, an
  ``OUTPUT_PARSE_`` error code, and ``updated_at`` inside the retry-liveness
  bound (default 6 h). The autopipe re-queues a failed run every tick and
  ``mark_run_failed`` renews ``updated_at`` on every retry, so the bound keeps
  exactly the runs still being retried. An abandoned historical failure (the two
  ``OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED`` rows last touched 2026-08-28) is not
  a residency signal; without the bound it would mail every re-alert interval
  forever. Residual: a tick hung for longer than the bound ages its failures out
  of the set — the 4 h frontier-stall lane owns that geometry.
- **state**: ``{run_id: {first_observed_failing_at, last_alerted_at}}``; a run
  absent from an observation (it parsed, or aged out) is dropped, so a run that
  fails again later starts a fresh clock. The parser never writes an
  intermediate status — a retried run goes straight back to ``failed`` or to
  ``parsed`` — so presence across consecutive observations IS "still failing".
- **verdict**: a run is *resident* once ``now - first_observed_failing_at >=
  threshold`` (default 2 h; ``0`` makes every watched run resident, the
  live-receipt setting). Exit 1 iff some resident run was never alerted or was
  last alerted at least the re-alert interval ago (default 24 h); those runs'
  ``last_alerted_at`` is stamped before exiting. Otherwise exit 0 — the report
  still lists already-alerted residents.
- **exit 2**: configuration, database or state error — one typed line, no
  traceback. A corrupt state file is NOT rebuilt (a deliberate divergence from
  the frontier lane's silent rebuild): rebuilding would restart every residency
  clock and hide a permanent failure for another threshold; the operator
  inspects and removes the file. Exit 2 fails the unit, so the handler mails on
  every tick while the observer is broken — undeduped on purpose, a broken
  observer is operator-actionable (same posture as the coverage-freshness lane).
- A second instance holding the lock exits 0 with one line (a skipped tick is
  not an alert).

Delivery: the unit carries ``OnFailure=nhms-node27-unit-failure-alert@%n.service``
and keeps stdout/stderr on the JOURNAL; the handler mails ``journalctl -n 30``,
so this report is the mail body. It is compact on purpose: one summary line,
at most ``--report-runs`` (default 10) resident lines, then ``... and K more``
— 12 lines plus systemd's own five framing lines fit the 30-line tail.

The DSN is the read-only ``nhms_display_ro`` role (``SELECT`` on
``hydro.hydro_run``), shared with the frontier / coverage-freshness lanes.

Injection seams: ``main(argv, now=..., observe=..., env=...)``;
``observe(config, liveness_floor)`` returns ``FailingRun`` rows.
``default_observe`` is the real, read-only adapter.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

EXIT_OK = 0
EXIT_ALERT = 1
EXIT_ERROR = 2

STATE_SCHEMA_VERSION = 1
DEFAULT_THRESHOLD_HOURS = 2.0
DEFAULT_REALERT_HOURS = 24.0
DEFAULT_RETRY_LIVENESS_HOURS = 6.0
DEFAULT_REPORT_RUNS = 10
DEFAULT_STATE_PATH = Path("/home/nwm/node27-parse-failure-residency-alert/state.json")

ENV_THRESHOLD = "NHMS_PARSE_RESIDENCY_THRESHOLD_HOURS"
ENV_REALERT = "NHMS_PARSE_RESIDENCY_REALERT_HOURS"
ENV_RETRY_LIVENESS = "NHMS_PARSE_RESIDENCY_RETRY_LIVENESS_HOURS"
ENV_REPORT_RUNS = "NHMS_PARSE_RESIDENCY_REPORT_RUNS"
ENV_STATE_PATH = "NHMS_PARSE_RESIDENCY_STATE_PATH"

CONNECT_TIMEOUT_SEC = 10
QUERY_TIMEOUT_MS = 30_000
APPLICATION_NAME = "nhms-parse-failure-residency-alert"
ERROR_TEXT_MAX_CHARS = 300

CODE_CONFIG_INVALID = "PARSE_FAILURE_RESIDENCY_CONFIG_INVALID"
CODE_STATE_CORRUPT = "PARSE_FAILURE_RESIDENCY_STATE_CORRUPT"
CODE_OBSERVATION_FAILED = "PARSE_FAILURE_RESIDENCY_OBSERVATION_FAILED"

RUNBOOK_REFERENCE = "docs/runbooks/production-ops/parse-failure-residency-alert.md"

# `\_` because `_` is a LIKE wildcard; `%%` because the statement is executed
# with a parameter mapping. The liveness floor is bound from the tick's own
# clock, so the SQL bound and the in-process re-check are the same instant.
OBSERVATION_QUERY = r"""
SELECT run_id, run_key, error_code, updated_at
FROM hydro.hydro_run
WHERE status = 'failed'
  AND error_code LIKE 'OUTPUT\_PARSE\_%%'
  AND updated_at > %(liveness_floor)s
ORDER BY run_id
"""


class ResidencyConfigError(Exception):
    """Configuration unusable — refuse before observing (exit 2)."""


class ResidencyStateError(Exception):
    """State file unusable — refuse without rewriting it (exit 2)."""


@dataclass(frozen=True)
class ResidencyConfig:
    database_url: str
    threshold: timedelta
    realert: timedelta
    retry_liveness: timedelta
    report_runs: int
    state_path: Path

    @property
    def lock_path(self) -> Path:
        return self.state_path.with_name(self.state_path.name + ".lock")


@dataclass(frozen=True)
class FailingRun:
    run_id: str
    run_key: int
    error_code: str
    updated_at: datetime


@dataclass(frozen=True)
class RunState:
    first_observed_failing_at: datetime
    last_alerted_at: datetime | None


Observer = Callable[[ResidencyConfig, datetime], Iterable[FailingRun]]


# ---------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------


def _hours(raw: str | None, *, name: str, default: float, allow_zero: bool) -> timedelta:
    if raw is None or not str(raw).strip():
        return timedelta(hours=default)
    try:
        value = float(str(raw).strip())
    except ValueError as error:
        raise ResidencyConfigError(f"{name} must be a number of hours, got {raw!r}") from error
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        bound = ">= 0" if allow_zero else "> 0"
        raise ResidencyConfigError(f"{name} must be a finite number of hours {bound}, got {raw!r}")
    try:
        return timedelta(hours=value)
    except OverflowError as error:
        raise ResidencyConfigError(f"{name} is out of range, got {raw!r}") from error


def _report_runs(raw: str | None) -> int:
    if raw is None or not str(raw).strip():
        return DEFAULT_REPORT_RUNS
    try:
        value = int(str(raw).strip())
    except ValueError as error:
        raise ResidencyConfigError(f"{ENV_REPORT_RUNS} must be an integer, got {raw!r}") from error
    if not 1 <= value <= 20:
        raise ResidencyConfigError(f"{ENV_REPORT_RUNS} must be within 1..20 (journal budget), got {raw!r}")
    return value


def _state_path(raw: str | None) -> Path:
    if raw is None or not str(raw).strip():
        return DEFAULT_STATE_PATH
    path = Path(str(raw).strip())
    if not path.is_absolute():
        raise ResidencyConfigError(f"{ENV_STATE_PATH} must be an absolute path, got {raw!r}")
    return path


def config_from_env(
    env: Mapping[str, str] | None = None, args: argparse.Namespace | None = None
) -> ResidencyConfig:
    """CLI flags win over the env; every value is validated before observing."""

    env = os.environ if env is None else env

    def pick(flag: str, name: str) -> str | None:
        value = getattr(args, flag, None) if args is not None else None
        return value if value is not None else env.get(name)

    database_url = (env.get("DATABASE_URL") or "").strip()
    if not database_url:
        raise ResidencyConfigError("DATABASE_URL must be set (the read-only nhms_display_ro DSN)")
    return ResidencyConfig(
        database_url=database_url,
        threshold=_hours(
            pick("threshold_hours", ENV_THRESHOLD),
            name=ENV_THRESHOLD,
            default=DEFAULT_THRESHOLD_HOURS,
            allow_zero=True,
        ),
        realert=_hours(
            pick("realert_hours", ENV_REALERT),
            name=ENV_REALERT,
            default=DEFAULT_REALERT_HOURS,
            allow_zero=False,
        ),
        retry_liveness=_hours(
            pick("retry_liveness_hours", ENV_RETRY_LIVENESS),
            name=ENV_RETRY_LIVENESS,
            default=DEFAULT_RETRY_LIVENESS_HOURS,
            allow_zero=False,
        ),
        report_runs=_report_runs(pick("report_runs", ENV_REPORT_RUNS)),
        state_path=_state_path(pick("state_path", ENV_STATE_PATH)),
    )


# ---------------------------------------------------------------------------
# Redaction: same chokepoint shape as the frontier / coverage-freshness lanes.
# ---------------------------------------------------------------------------


def _redact(error: BaseException | str, dsn: str) -> str:
    try:
        from packages.common.redaction import REDACTION_MARKER, redact_database_dsn

        text = redact_database_dsn(str(error), dsn).replace(REDACTION_MARKER, "***")
        try:
            username = urlsplit(dsn).username
        except ValueError:  # pragma: no cover - malformed DSN, nothing to scrub
            username = None
        if username:
            text = re.sub(rf'\b(user|role) "{re.escape(username)}"', r'\1 "***"', text)
    except Exception:  # noqa: BLE001 - the chokepoint never raises
        name = type(error).__name__ if isinstance(error, BaseException) else "str"
        return f"<error text withheld: redaction unavailable ({name})>"
    collapsed = " | ".join(part.strip() for part in text.splitlines() if part.strip())
    if len(collapsed) > ERROR_TEXT_MAX_CHARS:
        collapsed = collapsed[: ERROR_TEXT_MAX_CHARS - 1] + "…"
    return collapsed


# ---------------------------------------------------------------------------
# Observation.
# ---------------------------------------------------------------------------


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def default_observe(config: ResidencyConfig, liveness_floor: datetime) -> list[FailingRun]:
    """Read-only, bounded (connect 10 s, statement 30 s). Lazy driver import."""

    import psycopg2  # type: ignore[import-untyped]

    connection = psycopg2.connect(
        config.database_url,
        connect_timeout=CONNECT_TIMEOUT_SEC,
        fallback_application_name=APPLICATION_NAME,
    )
    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = %s", (QUERY_TIMEOUT_MS,))
            cursor.execute(OBSERVATION_QUERY, {"liveness_floor": liveness_floor})
            rows = cursor.fetchall()
        connection.rollback()
    finally:
        connection.close()
    return [
        FailingRun(run_id=str(run_id), run_key=int(run_key), error_code=str(code), updated_at=_utc(updated_at))
        for run_id, run_key, code, updated_at in rows
    ]


# ---------------------------------------------------------------------------
# State (JSON, atomic replace, fcntl lock beside it).
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_instant(raw: Any, *, field: str) -> datetime:
    if not isinstance(raw, str):
        raise ResidencyStateError(f"state field {field} is not a timestamp")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as error:
        raise ResidencyStateError(f"state field {field} is not a timestamp: {raw!r}") from error
    if parsed.tzinfo is None:
        raise ResidencyStateError(f"state field {field} has no timezone: {raw!r}")
    return parsed.astimezone(UTC)


def load_state(path: Path) -> dict[str, RunState]:
    """Missing file = empty state; anything unusable raises ResidencyStateError."""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except Exception as error:  # noqa: BLE001 - whole-class containment
        raise ResidencyStateError(f"state file unreadable: {type(error).__name__}: {error}") from error
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ResidencyStateError(f"state schema_version is not {STATE_SCHEMA_VERSION}")
        runs = payload.get("runs")
        if not isinstance(runs, dict):
            raise ResidencyStateError("state runs is not an object")
        state: dict[str, RunState] = {}
        for run_id, entry in runs.items():
            if not isinstance(entry, dict):
                raise ResidencyStateError(f"state entry for {run_id!r} is not an object")
            last = entry.get("last_alerted_at")
            state[str(run_id)] = RunState(
                first_observed_failing_at=_parse_instant(
                    entry.get("first_observed_failing_at"), field="first_observed_failing_at"
                ),
                last_alerted_at=None if last is None else _parse_instant(last, field="last_alerted_at"),
            )
        return state
    except ResidencyStateError:
        raise
    except Exception as error:  # noqa: BLE001 - whole-class containment
        raise ResidencyStateError(f"state file unusable: {type(error).__name__}: {error}") from error


def write_state(path: Path, state: Mapping[str, RunState]) -> None:
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "runs": {
            run_id: {
                "first_observed_failing_at": _iso(entry.first_observed_failing_at),
                "last_alerted_at": _iso(entry.last_alerted_at),
            }
            for run_id, entry in sorted(state.items())
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)


def acquire_lock(path: Path) -> int | None:
    """Non-blocking ``fcntl.flock``; ``None`` means another instance holds it."""

    fd: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ResidencyConfigError("lock file must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd
    except ResidencyConfigError:
        if fd is not None:
            os.close(fd)
        raise
    except Exception as error:  # noqa: BLE001 - whole-class containment
        if fd is not None:
            os.close(fd)
        raise ResidencyConfigError(f"cannot acquire lock file {path}: {type(error).__name__}: {error}") from error


def release_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Verdict + report.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resident:
    run: FailingRun
    first_observed: datetime
    due: bool


def evaluate(
    observed: Iterable[FailingRun],
    previous: Mapping[str, RunState],
    *,
    now: datetime,
    config: ResidencyConfig,
) -> tuple[dict[str, RunState], list[Resident], int]:
    """Next state, the resident runs (due first, oldest first), watched count."""

    floor = now - config.retry_liveness
    watched = {run.run_id: run for run in observed if _utc(run.updated_at) > floor}
    next_state: dict[str, RunState] = {}
    residents: list[Resident] = []
    for run_id, run in watched.items():
        prior = previous.get(run_id)
        first = prior.first_observed_failing_at if prior else now
        last = prior.last_alerted_at if prior else None
        due = False
        if now - first >= config.threshold:
            due = last is None or now - last >= config.realert
            if due:
                last = now
            residents.append(Resident(run=run, first_observed=first, due=due))
        next_state[run_id] = RunState(first_observed_failing_at=first, last_alerted_at=last)
    residents.sort(key=lambda item: (not item.due, item.first_observed, item.run.run_id))
    return next_state, residents, len(watched)


def _format_hours(delta: timedelta) -> str:
    return f"{delta.total_seconds() / 3600:g}h"


def build_report(residents: list[Resident], *, watched: int, now: datetime, config: ResidencyConfig) -> list[str]:
    due = sum(1 for item in residents if item.due)
    lines = [
        f"parse-failure-residency now={_iso(now)} watched={watched} resident={len(residents)} "
        f"newly_alerted={due} already_alerted={len(residents) - due} "
        f"threshold={_format_hours(config.threshold)} realert={_format_hours(config.realert)} "
        f"liveness={_format_hours(config.retry_liveness)} runbook={RUNBOOK_REFERENCE}"
    ]
    for item in residents[: config.report_runs]:
        residency = (now - item.first_observed).total_seconds() / 3600
        lines.append(
            f"resident run_id={item.run.run_id} error_code={item.run.error_code} "
            f"first_observed={_iso(item.first_observed)} residency_h={residency:.1f} "
            f"alert={'due' if item.due else 'within-realert'}"
        )
    omitted = len(residents) - config.report_runs
    if omitted > 0:
        lines.append(f"... and {omitted} more")
    return lines


def _emit(lines: list[str]) -> None:
    print("\n".join(lines), flush=True)


def _fail(code: str, reason: str) -> int:
    _emit([f"{code} reason={reason} runbook={RUNBOOK_REFERENCE}"])
    return EXIT_ERROR


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="node-27 parse-stage failure residency alert (#2529): exit 1 when a run stays "
        "failed with an OUTPUT_PARSE_* code for at least the threshold"
    )
    parser.add_argument("--threshold-hours", help=f"residency threshold (env {ENV_THRESHOLD}, default 2; 0 allowed)")
    parser.add_argument("--realert-hours", help=f"re-alert interval (env {ENV_REALERT}, default 24)")
    parser.add_argument(
        "--retry-liveness-hours",
        help=f"watch only runs updated within this bound (env {ENV_RETRY_LIVENESS}, default 6)",
    )
    parser.add_argument("--report-runs", help=f"resident lines in the report (env {ENV_REPORT_RUNS}, default 10)")
    parser.add_argument("--state-path", help=f"absolute state file path (env {ENV_STATE_PATH})")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    now: datetime | None = None,
    observe: Observer | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    env_map = os.environ if env is None else env
    dsn = env_map.get("DATABASE_URL", "")

    try:
        config = config_from_env(env_map, args)
    except ResidencyConfigError as error:
        return _fail(CODE_CONFIG_INVALID, _redact(error, dsn))

    stamp = _utc(now or datetime.now(UTC))
    try:
        fd = acquire_lock(config.lock_path)
    except ResidencyConfigError as error:
        return _fail(CODE_CONFIG_INVALID, _redact(error, dsn))
    if fd is None:
        _emit(["parse-failure-residency: another instance holds the lock; tick skipped"])
        return EXIT_OK
    try:
        try:
            previous = load_state(config.state_path)
        except ResidencyStateError as error:
            return _fail(CODE_STATE_CORRUPT, f"{_redact(error, dsn)} (inspect, then remove {config.state_path})")

        provider = default_observe if observe is None else observe
        try:
            observed = list(provider(config, stamp - config.retry_liveness))
        except Exception as error:  # noqa: BLE001 - every observation failure is exit 2
            return _fail(CODE_OBSERVATION_FAILED, f"{type(error).__name__}: {_redact(error, config.database_url)}")

        next_state, residents, watched = evaluate(observed, previous, now=stamp, config=config)
        try:
            write_state(config.state_path, next_state)
        except Exception as error:  # noqa: BLE001 - an unwritable state is a config error
            return _fail(CODE_CONFIG_INVALID, f"cannot write state: {type(error).__name__}: {_redact(error, dsn)}")
    finally:
        release_lock(fd)

    _emit(build_report(residents, watched=watched, now=stamp, config=config))
    return EXIT_ALERT if any(item.due for item in residents) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
