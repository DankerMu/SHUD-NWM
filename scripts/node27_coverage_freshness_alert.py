#!/usr/bin/env python3
"""node-27 coverage freshness alerting (issue #2080).

``hydro.run_display_coverage`` had no freshness observer anywhere in the repo.
Both coverage-refresh call sites are non-fatal, the autopipe unit carries no
``OnFailure=``, and the only stall alerter
(``scripts/node27_frontier_stall_alert.py``) reads ``hydro.hydro_run`` alone —
during a coverage stall the ingest frontier keeps advancing, so its
directional-progress criterion is reset every tick and it is *constructively*
silent. Since #2009 bounded national discharge discovery by
``NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS``, a coverage stall no longer degrades
to stale-but-online: the covered cycles age out of the window and the national
layer goes dark (``default_cycle=null``, ``valid_times=[]``) with zero alerts.

Design pins (``openspec/changes/node27-coverage-freshness-alert/design.md``):

- **D0 governing invariant / parity by construction** — the COVERED frontier is
  the value ``services/tiles/mvt.py:national_discharge_cycles`` returns as
  ``default_cycle`` for that source: the same call the display catalog serves.
  This module re-derives *no* coverage predicate. A ``segment_count > 0``-style
  re-derivation would have been strictly WEAKER than what lights the layer —
  the catalog also drops a cycle on inconsistent coverage-window columns, a
  wrong ``river_sample_count``, an off-stride clamped window, or an incomplete
  active-network set — so it would have reported ``gap = 0`` while the layer was
  already dark. The READY frontier is that same function's run-set minus the
  coverage join, and is this lane's one re-derived statement
  (``READY_FRONTIER_QUERY``).
- **D1 criterion** — per source key ``COALESCE(lower(source_id),
  '__null_source__')``, ``gap = ready_frontier - covered_frontier``; alert when
  the gap exceeds the threshold or the source has no ``default_cycle`` at all. A
  full ingest stall freezes both frontiers together and stays silent (owned by
  ``frontier-stalled``); a coverage stall advances only the ready frontier, so
  the gap grows monotonically and trips days before the layer goes dark. The
  criterion is OUTCOME-based, never rc-based: ``scripts/node27_autopipeline.py``
  records a legitimate rc=0 ``no_coverage_row`` path that no rc channel sees.
  The one wall-clock rule is the window filter — a source whose ready frontier
  already predates ``now() - NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS``
  contributes nothing to the national layer regardless of coverage, so it is
  reported ``not-evaluated`` instead of alerting forever from day one.
  ``__null_source__`` is always ``not-evaluated``: ``national_discharge_cycles``
  takes a ``str`` source and matches ``lower(h.source_id) = :source``, so such
  runs cannot be listed by the per-source catalog at all.
- **D2 threshold** — ``default_gap_days() = NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS
  / GAP_THRESHOLD_DIVISOR``. No day count is hard-coded, so shrinking the window
  automatically shrinks the threshold. ``NHMS_COVERAGE_GAP_DAYS`` overrides it;
  a non-finite value, ``<= 0``, or ``>= NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS``
  is a config error (a threshold at or beyond the window can only fire once the
  layer is already dark), never a silent clamp.
- **D3 mail channel** — the unit carries
  ``OnFailure=nhms-node27-unit-failure-alert@%n.service``; the alert IS the
  non-zero exit and the handler mails the last 30 journal lines. So the report
  goes to the JOURNAL (no ``StandardOutput=append:``), is capped at
  ``MAX_REPORT_LINES`` lines, prints the per-source table first (breaching
  sources sorted first, truncated with an explicit omission line) and the
  ``VERDICT:`` block LAST, so the operator-critical lines are the ones that
  survive the tail window after systemd's own ~4 framing lines.
- **D6 fail-closed** — every internal failure raises alerting tendency:
  exit 2 config invalid (before any observation), exit 3 observation failure or
  **zero source keys**, exit 1 gap breach / no covered cycle, exit 0 healthy.

Stateless by construction: no state file, no lock file, no receipt, no log file.
Every tick is a fresh observation, so re-running is idempotent and there is no
baseline to corrupt — which is also why this lane is a sibling of the frontier
alerter rather than a second criterion inside its per-source high-water state
machine.

Injection seams (unit tests need neither a database nor a display stack):
``main(argv, now=..., observe=..., env=...)``. ``default_observe(config)`` is
the thin real adapter and is covered by the node-27 live receipt plus one
call-shape test.

ADR 0001 display carve-out: this is a node-27 ops script that imports
``services.tiles.mvt``; it touches neither ``apps/api`` nor ``apps/frontend``.
The display import is function-local so ``--help`` and config parsing work on a
host without the display stack.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Constants (D1 / D2 / D3 wire vocabulary).
# ---------------------------------------------------------------------------

#: Source key for ``hydro.hydro_run`` rows with ``source_id IS NULL``. Mirrors
#: ``scripts/node27_frontier_stall_alert.py``'s spelling so an operator reading
#: both mails sees one vocabulary.
NULL_SOURCE_KEY = "__null_source__"

#: D2. ``default_gap_days() = NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS / 3`` →
#: 4.0 d at the current window of 12, leaving ~8 days of margin after the first
#: trip. Expressed as a divisor of the display constant rather than a day count
#: so a future window shrink carries the threshold with it.
GAP_THRESHOLD_DIVISOR = 3

#: D3 journal budget. ``journalctl -n 30`` keeps the TAIL and systemd itself
#: contributes roughly four framing lines (`Starting…`, `Main process exited…`,
#: `Failed with result…`, `nhms-…: Failed…`), plus this lane's single structured
#: stderr line on the failure paths. 24 stdout lines therefore still fit.
MAX_REPORT_LINES = 24

#: Bounds, matching the sibling frontier lane. An unbounded connect would let
#: the MONITOR hang the way the pipeline it watches can hang.
CONNECT_TIMEOUT_SEC = 10
QUERY_TIMEOUT_MS = 30_000

EXIT_OK = 0
EXIT_ALERT = 1
EXIT_CONFIG = 2
EXIT_OBSERVATION = 3

CODE_CONFIG_INVALID = "COVERAGE_FRESHNESS_CONFIG_INVALID"
CODE_OBSERVATION_FAILED = "COVERAGE_FRESHNESS_OBSERVATION_FAILED"
CODE_NO_SOURCES = "COVERAGE_FRESHNESS_NO_SOURCES"

STATUS_OK = "ok"
STATUS_GAP_EXCEEDED = "gap-exceeded"
STATUS_NO_COVERED_CYCLE = "no-covered-cycle"
STATUS_NOT_EVALUATED = "not-evaluated"

REASON_NULL_SOURCE = "null-source"
REASON_OUTSIDE_WINDOW = "outside-window"
REASON_NO_READY_FRONTIER = "no-ready-frontier"

RUNBOOK_REFERENCE = "docs/runbooks/current-production-ops.md - 覆盖新鲜度告警"

#: Longest source-key list the verdict line may spell before it degrades to a
#: count; keeps the verdict block a FIXED number of lines whatever the source
#: cardinality, which is what makes the D3 budget arithmetic hold.
VERDICT_SOURCE_LIST_LIMIT = 8

#: Flattened-error cap so one exception cannot blow the journal budget.
ERROR_TEXT_MAX_CHARS = 300

#: D0's ready frontier: ``national_discharge_cycles``' run set with the coverage
#: join removed, and this module's ONLY re-derived statement.
#:
#: ``lower(h.source_id)``: production stores ``gfs`` and ``IFS``, and the display
#: path matches ``lower(h.source_id)``, so the alerter's source keys must be
#: lower-cased or it would split a source the display treats as one.
#:
#: The ``core.model_instance`` join is load-bearing in both directions: it is
#: what makes the two frontiers comparable, and it is also why an EMPTY result
#: is fail-closed (D6) — a mass ``active_flag`` flip or a
#: ``river_network_version_id`` drift empties this while ``hydro.hydro_run``
#: keeps advancing, which is precisely the state the frontier lane cannot see.
READY_FRONTIER_QUERY = """
SELECT COALESCE(lower(h.source_id), '__null_source__') AS source_key,
       max(h.cycle_time) AS ready_frontier
FROM hydro.hydro_run h
JOIN core.model_instance mi ON mi.basin_version_id = h.basin_version_id
WHERE h.status IN ('succeeded', 'parsed', 'published')
  AND h.cycle_time IS NOT NULL
  AND mi.river_network_version_id IS NOT NULL
  AND mi.active_flag
GROUP BY 1
ORDER BY 1
"""


class CoverageAlertConfigError(Exception):
    """Configuration is unusable — refuse before observing anything (D6)."""


# ---------------------------------------------------------------------------
# The display constant (D2). Imported LAZILY, mirroring the frontier lane's
# deferred psycopg2 import: `--help` and config parsing must not require the
# display stack, and a test must be able to read the constant from its owner
# rather than restating it.
# ---------------------------------------------------------------------------


def lookback_days() -> float:
    """``NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS``, from its owner."""

    from services.tiles.mvt import NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS

    return float(NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS)


def default_gap_days() -> float:
    """D2's ``DEFAULT_GAP_DAYS``, spelled as a function because the constant it
    derives from is imported lazily (see ``lookback_days``)."""

    return lookback_days() / GAP_THRESHOLD_DIVISOR


@dataclass(frozen=True)
class CoverageAlertConfig:
    database_url: str
    gap_days: float
    lookback_days: float

    @property
    def gap_delta(self) -> timedelta:
        return timedelta(days=self.gap_days)

    @property
    def lookback_delta(self) -> timedelta:
        return timedelta(days=self.lookback_days)


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if value is None or not value.strip():
        raise CoverageAlertConfigError(f"{name} must be set")
    return value.strip()


def _gap_days_env(env: Mapping[str, str], lookback: float) -> float:
    """Parse ``NHMS_COVERAGE_GAP_DAYS``, refusing every unusable value (D2).

    ``float()`` happily returns ``nan``/``inf``; a threshold at or beyond the
    lookback window can only fire once the layer is ALREADY dark, which is the
    outage this lane exists to pre-empt. Both are refused here — a config error
    with zero side effects — never clamped.
    """

    raw = env.get("NHMS_COVERAGE_GAP_DAYS")
    if raw is None or not raw.strip():
        return lookback / GAP_THRESHOLD_DIVISOR
    try:
        value = float(raw.strip())
    except ValueError as error:
        raise CoverageAlertConfigError(
            f"NHMS_COVERAGE_GAP_DAYS must be a number, got {raw!r}"
        ) from error
    if not math.isfinite(value):
        raise CoverageAlertConfigError(
            f"NHMS_COVERAGE_GAP_DAYS must be a finite number, got {raw!r}"
        )
    if value <= 0:
        raise CoverageAlertConfigError(f"NHMS_COVERAGE_GAP_DAYS must be positive, got {raw!r}")
    if value >= lookback:
        raise CoverageAlertConfigError(
            f"NHMS_COVERAGE_GAP_DAYS must be smaller than the display lookback window "
            f"({_format_window(lookback)} d), got {raw!r}"
        )
    try:
        timedelta(days=value)
    except (OverflowError, ValueError) as error:  # pragma: no cover - guarded by `< lookback`
        raise CoverageAlertConfigError(
            f"NHMS_COVERAGE_GAP_DAYS is out of range for a time window, got {raw!r}"
        ) from error
    return value


def config_from_env(env: Mapping[str, str] | None = None) -> CoverageAlertConfig:
    """Strict env parse. Fails closed BEFORE any observation (D6 row 1).

    An ``ImportError`` from ``lookback_days()`` (no display stack on the host)
    surfaces here as a configuration error rather than an observation failure:
    it happens before any database work, and the unit fails and mails either
    way.
    """

    env = os.environ if env is None else env
    database_url = _required_env(env, "DATABASE_URL")
    lookback = lookback_days()
    return CoverageAlertConfig(
        database_url=database_url,
        gap_days=_gap_days_env(env, lookback),
        lookback_days=lookback,
    )


# ---------------------------------------------------------------------------
# Redaction. Same discipline and chokepoint contract as
# ``scripts/node27_frontier_stall_alert.py:_redact_error_text`` (#1368) and
# ``scripts/node27_timeseries_retention.py`` (#1213).
# ---------------------------------------------------------------------------


def _redact_error_text(error: BaseException | str, dsn: str) -> str:
    """Scrub credentials out of an error before it reaches ANY outlet.

    Every error text that reaches stdout (the mail body) or the structured
    stderr line crosses this function exactly once. psycopg2/libpq echo the
    whole conninfo — plaintext password included — on DSN parse/connect
    failures, so an unredacted interpolation would be a password-grade leak into
    journald and from there into the mail the failure handler sends.

    The ``packages.common.redaction`` import is function-local (that module
    imports ``psycopg2.extensions`` at module scope), and because of that
    deferral the chokepoint is TOTAL — it never raises. On a driver-less host
    the very error being reported is typically the missing driver, so any
    internal failure degrades to a credential-free placeholder naming the
    original exception type. libpq additionally echoes the ROLE name back
    (``password authentication failed for user "x"``); that is scrubbed only
    when the quoted name matches the DSN's own username, because a bare literal
    replace of a username such as ``nwm`` would mangle ``/home/nwm/...``.
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
        name = type(error).__name__ if isinstance(error, BaseException) else "str"
        return f"<error text withheld: redaction unavailable ({name})>"


def _flatten(text: str) -> str:
    """One journal line, bounded — the D3 budget must survive any exception."""

    collapsed = " | ".join(part.strip() for part in text.splitlines() if part.strip())
    if len(collapsed) > ERROR_TEXT_MAX_CHARS:
        return collapsed[: ERROR_TEXT_MAX_CHARS - 1] + "…"
    return collapsed


# ---------------------------------------------------------------------------
# Observation model (D0).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceFrontiers:
    """One source key's two frontiers, as observed.

    ``covered_cycle`` is held RAW — it is whatever ``national_discharge_cycles``
    put in ``default_cycle`` (a formatted string, or ``None`` when the layer is
    dark for that source). Parsing happens in ``evaluate``, so the observation
    adapter provably passes the catalog's answer through unmodified (D0).
    """

    source_key: str
    ready_frontier: datetime | None
    covered_cycle: Any


Observation = dict[str, SourceFrontiers]
ObservationProvider = Callable[[CoverageAlertConfig], Mapping[str, SourceFrontiers]]


def _coerce_timestamp(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        value = raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(raw, str):
        text = raw.strip()
        if " " in text and "T" not in text:
            text = text.replace(" ", "T", 1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    raise ValueError(f"unsupported timestamp type {type(raw).__name__}")


def default_observe(config: CoverageAlertConfig) -> Observation:
    """The real adapter (T1). Read-only, and re-derives NOTHING on the covered side.

    One SQLAlchemy session carries both halves: ``SET statement_timeout`` and the
    ready-frontier statement run on the same connection the per-source
    ``national_discharge_cycles`` calls then use, so the bound applies to the
    catalog's own two statements as well.

    ``national_discharge_cycles`` is called with its DEFAULT listing limit and
    nothing else — passing a ``limit`` would stop it being "the same call the
    catalog serves" (and ``limit=0`` empties every cycle's valid-time list,
    which would report every source as having no covered cycle). ``now()``
    inside it is not injectable, which is why the unit tests inject at the
    ``observe`` seam and this adapter is proven by the node-27 live receipt.

    ``__null_source__`` is skipped: the catalog matches
    ``lower(h.source_id) = :source`` and can never list a NULL-source run.
    """

    import sqlalchemy
    import sqlalchemy.orm

    from services.tiles import mvt

    engine = sqlalchemy.create_engine(
        config.database_url,
        future=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SEC},
    )
    try:
        with sqlalchemy.orm.Session(engine, future=True) as session:
            session.execute(sqlalchemy.text(f"SET statement_timeout = {QUERY_TIMEOUT_MS}"))
            rows = session.execute(sqlalchemy.text(READY_FRONTIER_QUERY)).mappings().all()
            observation: Observation = {}
            for row in rows:
                key = str(row["source_key"])
                covered: Any = None
                if key != NULL_SOURCE_KEY:
                    covered = mvt.national_discharge_cycles(session, source=key)["default_cycle"]
                observation[key] = SourceFrontiers(
                    source_key=key,
                    ready_frontier=_coerce_timestamp(row["ready_frontier"]),
                    covered_cycle=covered,
                )
    finally:
        engine.dispose()
    return observation


# ---------------------------------------------------------------------------
# Criterion (D1).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceVerdict:
    source_key: str
    status: str
    reason: str | None
    ready_frontier: datetime | None
    covered_frontier: datetime | None
    gap: timedelta | None

    @property
    def breaching(self) -> bool:
        return self.status in {STATUS_GAP_EXCEEDED, STATUS_NO_COVERED_CYCLE}

    @property
    def evaluated(self) -> bool:
        return self.status != STATUS_NOT_EVALUATED

    @property
    def gap_days(self) -> float | None:
        if self.gap is None:
            return None
        return self.gap.total_seconds() / 86400.0


def evaluate(
    observation: Mapping[str, SourceFrontiers],
    *,
    now: datetime,
    config: CoverageAlertConfig,
) -> list[SourceVerdict]:
    """Per-source criterion. Sorted breaching-first, then by key (D3 truncation)."""

    window_floor = now - config.lookback_delta
    verdicts: list[SourceVerdict] = []
    for key in sorted(observation):
        frontiers = observation[key]
        ready = _coerce_timestamp(frontiers.ready_frontier)
        covered = _coerce_timestamp(frontiers.covered_cycle)

        if key == NULL_SOURCE_KEY:
            # The per-source catalog can never list these runs, so they can
            # neither be covered nor be a coverage stall (D1).
            verdicts.append(
                SourceVerdict(key, STATUS_NOT_EVALUATED, REASON_NULL_SOURCE, ready, covered, None)
            )
            continue
        if ready is None:
            # UNREACHABLE against READY_FRONTIER_QUERY (`max()` over rows already
            # filtered by `cycle_time IS NOT NULL`, grouped, so a returned key
            # always carries an instant). Kept for failure MODE: a future
            # relaxation of that statement must degrade to an honest
            # "not evaluated" row rather than a TypeError mid-report.
            verdicts.append(
                SourceVerdict(key, STATUS_NOT_EVALUATED, REASON_NO_READY_FRONTIER, ready, covered, None)
            )
            continue
        if ready < window_floor:
            # The one wall-clock rule, and legitimate precisely because the
            # observed thing — the lookback window — is itself anchored on
            # `now()`. A source whose newest display-ready cycle already
            # predates the window contributes nothing to the national layer
            # regardless of coverage; its darkness is an ingest/retention
            # question owned by `frontier-stalled`.
            verdicts.append(
                SourceVerdict(key, STATUS_NOT_EVALUATED, REASON_OUTSIDE_WINDOW, ready, covered, None)
            )
            continue
        if covered is None:
            # The layer is already dark for this source.
            verdicts.append(
                SourceVerdict(key, STATUS_NO_COVERED_CYCLE, None, ready, None, None)
            )
            continue

        gap = ready - covered
        # Compared in timedelta space, not in floats: "exact equality does not
        # trip, strictly greater does" must not hinge on a float division.
        status = STATUS_GAP_EXCEEDED if gap > config.gap_delta else STATUS_OK
        verdicts.append(SourceVerdict(key, status, None, ready, covered, gap))

    verdicts.sort(key=lambda item: (not item.breaching, item.source_key))
    return verdicts


# ---------------------------------------------------------------------------
# Report (D3). Table first, VERDICT block LAST, <= MAX_REPORT_LINES lines.
# ---------------------------------------------------------------------------


def _format_days(value: float) -> str:
    """``0.0`` / ``5.0`` / ``4.0`` / ``2.5`` — three decimals, no trailing zeros."""

    return str(round(value, 3))


def _format_window(value: float) -> str:
    """Whole-day windows read as ``12``, not ``12.0`` — the operator's anchor for
    the lookback constant is the integer the runbook and `mvt.py` both spell."""

    return str(int(value)) if float(value).is_integer() else _format_days(value)


def _format_instant(value: datetime | None) -> str:
    if value is None:
        return "none"
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _source_row(verdict: SourceVerdict) -> str:
    gap = "-" if verdict.gap_days is None else f"{_format_days(verdict.gap_days)}d"
    return (
        f"source={verdict.source_key} "
        f"status={verdict.status} "
        f"reason={verdict.reason or '-'} "
        f"ready={_format_instant(verdict.ready_frontier)} "
        f"covered={_format_instant(verdict.covered_frontier)} "
        f"gap={gap}"
    )


def _verdict_block(verdicts: list[SourceVerdict], *, config: CoverageAlertConfig) -> list[str]:
    breaching = [item for item in verdicts if item.breaching]
    evaluated = [item for item in verdicts if item.evaluated]
    threshold = _format_days(config.gap_days)
    if not breaching:
        return [
            f"VERDICT: OK {len(evaluated)} evaluated source(s) within threshold={threshold}d; "
            f"{len(verdicts) - len(evaluated)} not evaluated"
        ]

    shown = breaching[:VERDICT_SOURCE_LIST_LIMIT]
    parts = []
    for item in shown:
        if item.status == STATUS_NO_COVERED_CYCLE:
            parts.append(f"{item.source_key}({STATUS_NO_COVERED_CYCLE})")
        else:
            parts.append(f"{item.source_key}(gap={_format_days(item.gap_days or 0.0)}d)")
    omitted = len(breaching) - len(shown)
    if omitted > 0:
        parts.append(f"+{omitted} more")
    return [
        f"VERDICT: FAIL the national catalog frontier is behind ingest on "
        f"{len(breaching)} of {len(evaluated)} evaluated source(s) threshold={threshold}d",
        f"VERDICT: breaching={' '.join(parts)}",
        f"VERDICT: runbook={RUNBOOK_REFERENCE}",
    ]


def _failure_block(summary: str) -> list[str]:
    return [f"VERDICT: FAIL {summary}", f"VERDICT: runbook={RUNBOOK_REFERENCE}"]


def build_report(
    verdicts: list[SourceVerdict],
    *,
    now: datetime,
    config: CoverageAlertConfig,
    verdict_lines: list[str],
) -> list[str]:
    """Header + per-source table (+ omission line) + verdict block, in that order.

    Budget arithmetic: the verdict block is sized first and is never truncated —
    it is the part that must survive ``journalctl -n 30``. Whatever is left goes
    to the table, and because ``evaluate`` sorted breaching sources first, the
    truncation eats healthy rows before it ever eats an alerting one.

    That last guarantee is BOUNDED, not absolute: with the 3-line failing verdict
    block the table holds 19 rows, so a run with more than 19 breaching sources
    loses alerting rows off the table too. Nothing is hidden when it happens —
    the omission line carries the exact count, the header carries
    ``breaching=<n>``, and ``VERDICT: breaching=`` still names the first
    ``VERDICT_SOURCE_LIST_LIMIT`` sources plus ``+N more``. Growing the table
    instead would push the verdict out of the journal tail the failure handler
    mails, which is the strictly worse failure (D3).
    """

    breaching = sum(1 for item in verdicts if item.breaching)
    evaluated = sum(1 for item in verdicts if item.evaluated)
    header = (
        f"coverage-freshness now={_format_instant(now)} "
        f"threshold={_format_days(config.gap_days)}d "
        f"lookback={_format_window(config.lookback_days)}d "
        f"sources={len(verdicts)} evaluated={evaluated} breaching={breaching}"
    )

    budget = max(0, MAX_REPORT_LINES - 1 - len(verdict_lines))
    rows = [_source_row(item) for item in verdicts]
    if len(rows) > budget:
        kept = max(0, budget - 1)
        omitted = len(rows) - kept
        rows = rows[:kept] + [f"… {omitted} more sources omitted"]
    return [header, *rows, *verdict_lines]


def _emit(report: list[str], *, structured: Mapping[str, Any] | None = None) -> None:
    """One structured stderr line FIRST, then the report on stdout.

    Ordering is deliberate and flushed on both streams: under systemd stdout is
    block-buffered while stderr is not, so without this the machine-readable
    line would land after the verdict in the journal and push it out of the
    30-line tail the failure handler mails.
    """

    if structured is not None:
        print(json.dumps(dict(structured), sort_keys=True, ensure_ascii=False), file=sys.stderr, flush=True)
    print("\n".join(report), flush=True)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="node-27 coverage freshness alerting (issue #2080): compare, per source, "
        "the national catalog's own newest listed cycle against the display-ready ingest frontier"
    )


def main(
    argv: list[str] | None = None,
    *,
    now: datetime | None = None,
    observe: ObservationProvider | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    build_parser().parse_args(argv)
    env_map = os.environ if env is None else env

    try:
        config = config_from_env(env_map)
    except CoverageAlertConfigError as error:
        _emit(
            _failure_block(f"configuration invalid: {_flatten(str(error))}"),
            structured={"status": "failed", "code": CODE_CONFIG_INVALID, "reason": str(error)},
        )
        return EXIT_CONFIG
    except Exception as error:  # noqa: BLE001 - whole-class containment
        # The config stage guards the whole lane, so an escaping class would mean
        # the same permanent silence as a rejected config — but as a raw
        # traceback, which is unstructured AND can echo the DSN straight into
        # journald past the redaction chokepoint.
        reason = f"{type(error).__name__}: {_redact_error_text(error, env_map.get('DATABASE_URL', ''))}"
        _emit(
            _failure_block(f"configuration invalid: {_flatten(reason)}"),
            structured={"status": "failed", "code": CODE_CONFIG_INVALID, "reason": reason},
        )
        return EXIT_CONFIG

    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    provider = default_observe if observe is None else observe

    try:
        observation = dict(provider(config))
    except Exception as error:  # noqa: BLE001 - every failure raises alerting tendency
        reason = f"{type(error).__name__}: {_redact_error_text(error, config.database_url)}"
        _emit(
            _failure_block(f"observation failed: {_flatten(reason)}"),
            structured={"status": "failed", "code": CODE_OBSERVATION_FAILED, "reason": reason},
        )
        return EXIT_OBSERVATION

    if not observation:
        # D6: zero observable sources is fail-closed, not fail-open. The
        # ready-frontier statement joins `core.model_instance`, so a mass
        # `active_flag` flip or a `river_network_version_id` drift empties it
        # while `hydro.hydro_run` keeps advancing — the frontier lane does not
        # join that table, still sees progress, and stays silent, while
        # `national_discharge_cycles` already returns `default_cycle = null`.
        # That is exactly the constructive silence this lane exists to remove.
        summary = (
            "no display-ready source key observed — the check cannot see the run set the "
            "national catalog is built from (fail-closed)"
        )
        _emit(
            _failure_block(summary),
            structured={"status": "failed", "code": CODE_NO_SOURCES, "reason": summary},
        )
        return EXIT_OBSERVATION

    try:
        verdicts = evaluate(observation, now=stamp, config=config)
    except Exception as error:  # noqa: BLE001 - unusable observation is still fail-closed
        reason = f"{type(error).__name__}: {_redact_error_text(error, config.database_url)}"
        _emit(
            _failure_block(f"observation unusable: {_flatten(reason)}"),
            structured={"status": "failed", "code": CODE_OBSERVATION_FAILED, "reason": reason},
        )
        return EXIT_OBSERVATION

    verdict_lines = _verdict_block(verdicts, config=config)
    _emit(build_report(verdicts, now=stamp, config=config, verdict_lines=verdict_lines))
    return EXIT_ALERT if any(item.breaching for item in verdicts) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
