#!/usr/bin/env python3
"""Gated TimescaleDB retention runner for node-27 (issue #855 §6.1 + §6.2).

Drops chunks strictly older than a configurable window (default 14 d) from
the two D3 detail hypertables ``hydro.river_timeseries`` and
``met.forcing_station_timeseries``. The runner refuses enforce mode unless
BOTH gate receipts are fresh AND cover the drop window:

1. **Archive-completeness receipt** (emitted by ``node27_storage_inventory_audit``,
   schema ``schemas/archive_completeness_receipt.schema.json``). Runner reads
   only from the receipt — no shadow DB oracle. Physical chunks wholly outside
   ``coverage_bounds`` are deferred; the runner refuses when no eligible chunk
   is fully evidenced or when any subject overlapping the covered candidate
   window carries ``verdict != complete``.
2. **Archive-rebuild-drill receipt** (emitted by
   ``node27_archive_rebuild_drill``, schema
   ``schemas/archive_rebuild_drill_receipt.schema.json``). Refuses if the
   drill is FAIL, stale, or its declared ``coverage[]`` tuples fail the per-
   source rule in ``docs/runbooks/tier-node27-timeseries-storage.md §7.5``:
   the forcing-recovery UNION (``forcing`` plus verified ``db-export``) and
   the ``runs`` UNION MUST span the drop window (the drill emits per-cycle
   24 h tuples — no single tuple is expected to cover a 14 d drop window on
   its own); ``source=db-export`` remains independently required iff the
   completeness receipt reports any overlapping db-export subject.

Design references (design.md #855 fixture pins H1-H17):

- H1 completeness receipt authority; H2 drill per-source coverage rule.
- H3 catalog enumeration honours per-tick bound (``drop_chunks`` cannot
  bound cardinality server-side; runner enumerates
  ``timescaledb_information.chunks`` for the two D3 hypertables, orders by
  ``range_end ASC``, takes ``per_tick_bound``, then invokes ``drop_chunks``
  per selected chunk with exact ``newer_than`` and ``older_than`` bounds).
- H4 ``freed_bytes`` measured BEFORE drop (post-drop the chunk is gone;
  ``chunks_detailed_size`` would no longer report it). Sized via
  ``chunks_detailed_size(<hypertable>).total_bytes`` filtered to the chunk so
  a compressed chunk's compressed sibling relation counts too (#1125).
- H5 fail-closed on per-chunk drop failure — whole tick refuses.
- H6 wire codes byte-identical across code / runbook §8.2 / design #855.
- H7 predicate ``range_end <= cutoff`` (non-strict; divergence from #851's
  strict ``<`` — a chunk with ``range_end == cutoff`` has all row times
  strictly less than cutoff, satisfying "entire range older than window").
- H8 freshness defaults (completeness 26 h, drill 30 d).
- H9 ``salvage_backed_windows[]`` derived from completeness receipt subjects
  only (chunk boundaries do not carry lane/subject identity).
- H10 lock path byte-identical with runbook §8 + ``.example``.
- H12 statement timeouts: 60 s for catalog enumeration, 300 s per ``drop_chunks``.
- H13 env prefix ``NODE27_TIMESERIES_RETENTION_*``.

ADR 0002: retention gate IS the archive receipt gate — never bypassed.

ADR 0001 display carve-out: no imports touching ``apps/api`` or
``apps/frontend``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import jsonschema

from packages.common.display_watermark import fetch_display_watermark
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    open_directory_no_follow,
)

SCHEMA_VERSION = "1.0"
TOOL_VERSION = "node27-timeseries-retention/1"

# H10 canonical lock path — byte-identical with runbook §8 + `.example`.
_DEFAULT_LOCK_PATH_STR = "/tmp/nhms-node27-timeseries-retention.lock"

# H8 freshness defaults.
_DEFAULT_COMPLETENESS_MAX_AGE_HOURS = 26
_DEFAULT_DRILL_MAX_AGE_DAYS = 30

# H3 per-tick bound + window defaults (matches spec §Window and mechanism).
_DEFAULT_WINDOW_DAYS = 14
_DEFAULT_PER_TICK_BOUND = 5

# H12 statement timeouts.
_QUERY_TIMEOUT_MS = 60_000
_DROP_TIMEOUT_MS = 300_000

# TARGET_HYPERTABLES — the two D3 detail hypertables. Metadata/coverage
# tables (`hydro_run`, `run_display_coverage`, `forcing_version`,
# `state_snapshot`, QC/lineage) MUST NEVER appear here. Structural
# guarantee: `drop_chunks` only accepts hypertables, and the two hypertables
# below are the ONLY targets.
TARGET_HYPERTABLES: frozenset[tuple[str, str]] = frozenset(
    {("hydro", "river_timeseries"), ("met", "forcing_station_timeseries")}
)

# H6 wire-format codes — byte-identical across:
# * this module (``WIRE_CODES`` frozenset),
# * ``docs/runbooks/tier-node27-timeseries-storage.md`` §8.2,
# * ``openspec/changes/tier-node27-timeseries-storage/design.md`` #855 fixture,
# * ``tests/test_node27_timeseries_retention.py``.
# Any addition / rename / removal MUST land in all four surfaces in the same
# commit (same-class recurrence discipline from #854).
CODE_COMPLETENESS_RECEIPT_MISSING = "COMPLETENESS_RECEIPT_MISSING"
CODE_COMPLETENESS_RECEIPT_STALE = "COMPLETENESS_RECEIPT_STALE"
CODE_COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT = "COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT"
CODE_COMPLETENESS_RECEIPT_GAP_IN_DROP_WINDOW = "COMPLETENESS_RECEIPT_GAP_IN_DROP_WINDOW"
CODE_COMPLETENESS_RECEIPT_PENDING_IN_DROP_WINDOW = "COMPLETENESS_RECEIPT_PENDING_IN_DROP_WINDOW"
CODE_DRILL_RECEIPT_MISSING = "DRILL_RECEIPT_MISSING"
CODE_DRILL_RECEIPT_STALE = "DRILL_RECEIPT_STALE"
CODE_DRILL_RECEIPT_FAIL = "DRILL_RECEIPT_FAIL"
CODE_DRILL_DERIVATION_WINDOW_TOO_NARROW = "DRILL_DERIVATION_WINDOW_TOO_NARROW"
CODE_DRILL_COVERAGE_FORCING_MISSING = "DRILL_COVERAGE_FORCING_MISSING"
CODE_DRILL_COVERAGE_RUNS_MISSING = "DRILL_COVERAGE_RUNS_MISSING"
CODE_DRILL_COMPLETENESS_SNAPSHOT_UNBOUND = "DRILL_COMPLETENESS_SNAPSHOT_UNBOUND"
CODE_DRILL_COVERAGE_DB_EXPORT_MISSING = "DRILL_COVERAGE_DB_EXPORT_MISSING"
CODE_RETENTION_CONFIG_INVALID = "RETENTION_CONFIG_INVALID"
CODE_RETENTION_CONCURRENT_INVOCATION = "RETENTION_CONCURRENT_INVOCATION"
CODE_RETENTION_DROP_FAILED = "RETENTION_DROP_FAILED"
CODE_RETENTION_UNCAUGHT_ERROR = "RETENTION_UNCAUGHT_ERROR"

WIRE_CODES: frozenset[str] = frozenset(
    {
        CODE_COMPLETENESS_RECEIPT_MISSING,
        CODE_COMPLETENESS_RECEIPT_STALE,
        CODE_COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT,
        CODE_COMPLETENESS_RECEIPT_GAP_IN_DROP_WINDOW,
        CODE_COMPLETENESS_RECEIPT_PENDING_IN_DROP_WINDOW,
        CODE_DRILL_RECEIPT_MISSING,
        CODE_DRILL_RECEIPT_STALE,
        CODE_DRILL_RECEIPT_FAIL,
        CODE_DRILL_DERIVATION_WINDOW_TOO_NARROW,
        CODE_DRILL_COVERAGE_FORCING_MISSING,
        CODE_DRILL_COVERAGE_RUNS_MISSING,
        CODE_DRILL_COMPLETENESS_SNAPSHOT_UNBOUND,
        CODE_DRILL_COVERAGE_DB_EXPORT_MISSING,
        CODE_RETENTION_CONFIG_INVALID,
        CODE_RETENTION_CONCURRENT_INVOCATION,
        CODE_RETENTION_DROP_FAILED,
        CODE_RETENTION_UNCAUGHT_ERROR,
    }
)

_ROOT = Path(__file__).resolve().parents[1]
_RECEIPT_SCHEMA_PATH = _ROOT / "schemas/timeseries_retention_receipt.schema.json"
_COMPLETENESS_SCHEMA_PATH = _ROOT / "schemas/archive_completeness_receipt.schema.json"
_DRILL_SCHEMA_PATH = _ROOT / "schemas/archive_rebuild_drill_receipt.schema.json"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RetentionConfigError(RuntimeError):
    """Fail-closed configuration parse error before any DB call.

    Emitted with wire code ``RETENTION_CONFIG_INVALID`` on the diagnostic
    stderr channel. When the runner cannot parse enough config to produce a
    receipt at all, the process exits non-zero before touching the DB or
    the receipt path.
    """


class ReceiptGateError(RuntimeError):
    """Signal a gate refusal tagged with a wire-format code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        message = f"{code}: {detail}" if detail else code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionConfig:
    """Immutable retention runner configuration.

    Every field is populated via :func:`config_from_args` from the H13 env
    catalogue plus CLI overrides. Defaults are pinned to H8/H10/H12/§Window.
    """

    database_url: str
    window_days: int
    per_tick_bound: int
    completeness_receipt_path: Path
    drill_receipt_path: Path
    completeness_max_age_hours: int
    drill_max_age_days: int
    receipt_path: Path
    lock_path: Path
    enforce: bool


@dataclass(frozen=True)
class ChunkRow:
    """One row from ``timescaledb_information.chunks`` filtered by D3.

    ``qualified_name`` is used as the receipt key for both
    ``candidate_chunks[]`` and ``dropped_chunks[].name`` — it is
    ``<chunk_schema>.<chunk_name>`` so operators can copy the string
    straight into a ``psql`` query.
    """

    hypertable_schema: str
    hypertable_name: str
    chunk_schema: str
    chunk_name: str
    range_start: datetime
    range_end: datetime
    is_compressed: bool

    @property
    def qualified_name(self) -> str:
        return f"{self.chunk_schema}.{self.chunk_name}"

    @property
    def hypertable_key(self) -> str:
        return f"{self.hypertable_schema}.{self.hypertable_name}"


@dataclass(frozen=True)
class DropWindow:
    """The time interval [start, end] the runner is asking gates to cover."""

    start: datetime
    end: datetime


def _parse_positive_int(raw: str | None, *, name: str, minimum: int) -> int:
    if raw is None or raw == "":
        raise RetentionConfigError(f"{name} must be set")
    stripped = raw.strip()
    if stripped == "" or stripped != raw:
        raise RetentionConfigError(f"{name} must not contain leading/trailing whitespace")
    try:
        value = int(stripped)
    except ValueError as error:
        raise RetentionConfigError(f"{name} must be an integer, got {raw!r}") from error
    if value < minimum:
        raise RetentionConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _default_lock_path() -> Path:
    """Return the canonical retention lock path (H10, zero-arg).

    Byte-identical with runbook §8 + ``infra/env/node27-timeseries-retention.example``
    so operators reading either surface can rely on the same absolute path.
    Env override: ``NODE27_TIMESERIES_RETENTION_LOCK_PATH`` (must be
    absolute).
    """
    return Path(_DEFAULT_LOCK_PATH_STR)


def _optional_positive_int(raw: str | None, *, name: str, default: int) -> int:
    if raw is None or raw == "":
        return default
    return _parse_positive_int(raw, name=name, minimum=1)


def _resolve_path(
    args_value: str | None,
    env_value: str | None,
    *,
    name: str,
    required: bool = True,
    default: str | None = None,
) -> Path:
    raw = args_value if args_value is not None else env_value
    if not raw:
        if default is not None:
            raw = default
        elif required:
            raise RetentionConfigError(f"{name} must be set (CLI or env)")
        else:
            raise RetentionConfigError(f"{name} is unresolved")
    path = Path(str(raw))
    if not path.is_absolute():
        raise RetentionConfigError(f"{name} must be an absolute path, got {raw!r}")
    return path


def config_from_args(
    args: argparse.Namespace, env: Mapping[str, str] | None = None
) -> RetentionConfig:
    """Strict env + CLI parse. No truthiness fallback. Fails closed on bad shape."""
    env = os.environ if env is None else env
    database_url = env.get("DATABASE_URL")
    if not database_url or not database_url.strip():
        raise RetentionConfigError("DATABASE_URL must be set")
    window_days = _optional_positive_int(
        env.get("NODE27_TIMESERIES_RETENTION_WINDOW_DAYS"),
        name="NODE27_TIMESERIES_RETENTION_WINDOW_DAYS",
        default=_DEFAULT_WINDOW_DAYS,
    )
    per_tick_bound = _optional_positive_int(
        env.get("NODE27_TIMESERIES_RETENTION_PER_TICK_BOUND"),
        name="NODE27_TIMESERIES_RETENTION_PER_TICK_BOUND",
        default=_DEFAULT_PER_TICK_BOUND,
    )
    completeness_max_age_hours = _optional_positive_int(
        env.get("NODE27_TIMESERIES_RETENTION_COMPLETENESS_MAX_AGE_HOURS"),
        name="NODE27_TIMESERIES_RETENTION_COMPLETENESS_MAX_AGE_HOURS",
        default=_DEFAULT_COMPLETENESS_MAX_AGE_HOURS,
    )
    drill_max_age_days = _optional_positive_int(
        env.get("NODE27_TIMESERIES_RETENTION_DRILL_MAX_AGE_DAYS"),
        name="NODE27_TIMESERIES_RETENTION_DRILL_MAX_AGE_DAYS",
        default=_DEFAULT_DRILL_MAX_AGE_DAYS,
    )
    completeness_receipt_path = _resolve_path(
        getattr(args, "completeness_receipt_path", None),
        env.get("NODE27_TIMESERIES_RETENTION_COMPLETENESS_RECEIPT_PATH"),
        name="NODE27_TIMESERIES_RETENTION_COMPLETENESS_RECEIPT_PATH",
    )
    drill_receipt_path = _resolve_path(
        getattr(args, "drill_receipt_path", None),
        env.get("NODE27_TIMESERIES_RETENTION_DRILL_RECEIPT_PATH"),
        name="NODE27_TIMESERIES_RETENTION_DRILL_RECEIPT_PATH",
    )
    receipt_path = _resolve_path(
        getattr(args, "receipt_path", None),
        env.get("NODE27_TIMESERIES_RETENTION_RECEIPT_PATH"),
        name="NODE27_TIMESERIES_RETENTION_RECEIPT_PATH",
    )
    lock_path = _resolve_path(
        getattr(args, "lock_path", None),
        env.get("NODE27_TIMESERIES_RETENTION_LOCK_PATH"),
        name="NODE27_TIMESERIES_RETENTION_LOCK_PATH",
        default=_DEFAULT_LOCK_PATH_STR,
    )
    # H13 env-toggled enforce: --enforce CLI wins; otherwise env presence
    # (any non-empty value that is not "0" / "false") toggles.
    if bool(getattr(args, "enforce", False)):
        enforce = True
    else:
        raw = env.get("NODE27_TIMESERIES_RETENTION_ENFORCE", "").strip().lower()
        enforce = bool(raw) and raw not in {"0", "false", "no"}
    return RetentionConfig(
        database_url=database_url,
        window_days=window_days,
        per_tick_bound=per_tick_bound,
        completeness_receipt_path=completeness_receipt_path,
        drill_receipt_path=drill_receipt_path,
        completeness_max_age_hours=completeness_max_age_hours,
        drill_max_age_days=drill_max_age_days,
        receipt_path=receipt_path,
        lock_path=lock_path,
        enforce=enforce,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="node27_timeseries_retention", description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--enforce", action="store_true", help="actually invoke drop_chunks")
    group.add_argument("--dry-run", action="store_true", help="dry-run (default)")
    parser.add_argument("--receipt-path", dest="receipt_path", type=str, default=None)
    parser.add_argument("--lock-path", dest="lock_path", type=str, default=None)
    parser.add_argument(
        "--completeness-receipt-path",
        dest="completeness_receipt_path",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--drill-receipt-path",
        dest="drill_receipt_path",
        type=str,
        default=None,
    )
    return parser


# ---------------------------------------------------------------------------
# Lock acquisition (mirrors compression `:180-211`).
# ---------------------------------------------------------------------------


def acquire_lock(path: Path) -> int | None:
    """Take a nonblocking flock on a mode-0600 lock file. Return None on contention."""
    if not path.is_absolute():
        raise RetentionConfigError("lock path must be absolute")
    ensure_directory_no_follow(path.parent)
    common_flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_fd = open_directory_no_follow(path.parent)
    fd: int | None = None
    try:
        try:
            fd = os.open(path.name, common_flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            fd = os.open(path.name, common_flags, dir_fd=parent_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise RetentionConfigError("lock file must be a mode-0600 regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd
    except RetentionConfigError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as error:
        if fd is not None:
            os.close(fd)
        raise RetentionConfigError(f"cannot acquire lock file: {error}") from error
    finally:
        os.close(parent_fd)


# ---------------------------------------------------------------------------
# Receipt-loading helpers (H1/H2).
# ---------------------------------------------------------------------------


def _load_schema(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; treat a naive result as UTC."""
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    """Emit RFC-3339 ``Z``-suffixed UTC — mirrors compression `:260-261`."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def load_completeness_receipt(path: Path) -> dict[str, Any]:
    """Load + schema-validate the archive-completeness receipt.

    Raises :class:`ReceiptGateError` with
    ``CODE_COMPLETENESS_RECEIPT_MISSING`` on missing / unreadable / schema-
    invalid file; the missing wire code covers all "receipt is not usable"
    conditions since the schema does not carve out a separate invalid-shape
    code.

    Uses ``_RECEIPT_FORMAT_CHECKER`` so ``format: date-time`` on
    ``generated_at`` / ``coverage_bounds`` / ``windows[].window.*`` is
    ENFORCED at load — symmetric with the emitter side. Without a format
    checker jsonschema treats ``format`` as informational and any
    malformed subject window ``start`` / ``end`` would fall through to the
    per-subject silent-False fallback in ``_subject_overlaps_drop`` (RF-F1
    R2 fix — loader-side symmetry with emit side).
    """
    if not path.is_file() or path.is_symlink():
        raise ReceiptGateError(CODE_COMPLETENESS_RECEIPT_MISSING, str(path))
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ReceiptGateError(CODE_COMPLETENESS_RECEIPT_MISSING, str(error)) from error
    try:
        jsonschema.validate(
            data,
            _load_schema(_COMPLETENESS_SCHEMA_PATH),
            format_checker=_RECEIPT_FORMAT_CHECKER,
        )
    except jsonschema.ValidationError as error:
        raise ReceiptGateError(
            CODE_COMPLETENESS_RECEIPT_MISSING,
            f"schema violation: {error.message}",
        ) from error
    if not isinstance(data, dict):
        raise ReceiptGateError(
            CODE_COMPLETENESS_RECEIPT_MISSING, "receipt is not a JSON object"
        )
    return data


def load_drill_receipt(path: Path) -> dict[str, Any]:
    """Load + schema-validate the archive-rebuild-drill receipt.

    Uses ``_RECEIPT_FORMAT_CHECKER`` so ``format: date-time`` on the
    receipt's timestamped fields is ENFORCED at load — symmetric with the
    emitter side (RF-F1 R2 fix). A malformed ``coverage[].window.start``
    would otherwise fall through to the silent-False fallback in
    ``_tuples_cover_window``.
    """
    if not path.is_file() or path.is_symlink():
        raise ReceiptGateError(CODE_DRILL_RECEIPT_MISSING, str(path))
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ReceiptGateError(CODE_DRILL_RECEIPT_MISSING, str(error)) from error
    try:
        jsonschema.validate(
            data,
            _load_schema(_DRILL_SCHEMA_PATH),
            format_checker=_RECEIPT_FORMAT_CHECKER,
        )
    except jsonschema.ValidationError as error:
        raise ReceiptGateError(
            CODE_DRILL_RECEIPT_MISSING,
            f"schema violation: {error.message}",
        ) from error
    if not isinstance(data, dict):
        raise ReceiptGateError(CODE_DRILL_RECEIPT_MISSING, "receipt is not a JSON object")
    return data


# ---------------------------------------------------------------------------
# Gate checks (H1, H2).
# ---------------------------------------------------------------------------


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Two closed intervals overlap iff neither ends before the other begins."""
    return a_start <= b_end and b_start <= a_end


def _subject_overlaps_drop(subject: Mapping[str, Any], drop: DropWindow) -> bool:
    window = subject.get("window") or {}
    try:
        s_start = _parse_iso(window["start"])
        s_end = _parse_iso(window["end"])
    except (KeyError, TypeError, ValueError):
        return False
    return _overlaps(s_start, s_end, drop.start, drop.end)


def check_completeness_gate(
    receipt: Mapping[str, Any],
    drop_window: DropWindow | None,
    max_age_hours: int,
    now: datetime,
) -> list[str]:
    """Evaluate H1: bounds + per-subject verdict + staleness.

    Returns a list containing at most one refusal code (in refusal-priority
    order). An empty list means the completeness gate is satisfied.
    Order: STALE → BOUNDS → GAP → PENDING (MISSING is raised by the loader).
    """
    reasons: list[str] = []
    generated_at_raw = receipt.get("generated_at")
    if not isinstance(generated_at_raw, str):
        # Fail-closed — schema validation should have caught this already;
        # treat as stale so the receipt is not consulted further.
        reasons.append(CODE_COMPLETENESS_RECEIPT_STALE)
        return reasons
    try:
        generated_at = _parse_iso(generated_at_raw)
    except ValueError:
        reasons.append(CODE_COMPLETENESS_RECEIPT_STALE)
        return reasons
    age = now - generated_at
    # Future-dated receipts (negative age) are not fresh — the receipt was
    # emitted with a clock ahead of the runner's; reuse STALE rather than
    # introducing a new wire code (byte-identity discipline from #854 R2).
    if age < timedelta(0) or age > timedelta(hours=max_age_hours):
        reasons.append(CODE_COMPLETENESS_RECEIPT_STALE)
        return reasons
    if drop_window is None:
        # H17 zero-eligible enforce: no drop window to gate; return empty.
        return reasons
    bounds = receipt.get("coverage_bounds") or {}
    try:
        bounds_start = _parse_iso(bounds["start"])
        bounds_end = _parse_iso(bounds["end"])
    except (KeyError, TypeError, ValueError):
        reasons.append(CODE_COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT)
        return reasons
    if not (bounds_start <= drop_window.start and bounds_end >= drop_window.end):
        reasons.append(CODE_COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT)
        return reasons
    windows = receipt.get("windows") or []
    # H1 (b): any in-window subject with verdict != complete refuses. Codes
    # are distinct per verdict so operators know which failure mode fired.
    # Priority: gap (missing) before pending-archive (in-progress).
    for subject in windows:
        if not isinstance(subject, Mapping):
            continue
        if not _subject_overlaps_drop(subject, drop_window):
            continue
        if subject.get("verdict") == "gap":
            reasons.append(CODE_COMPLETENESS_RECEIPT_GAP_IN_DROP_WINDOW)
            return reasons
    for subject in windows:
        if not isinstance(subject, Mapping):
            continue
        if not _subject_overlaps_drop(subject, drop_window):
            continue
        if subject.get("verdict") == "pending-archive":
            reasons.append(CODE_COMPLETENESS_RECEIPT_PENDING_IN_DROP_WINDOW)
            return reasons
    return reasons


def _completeness_has_db_export_overlap(
    receipt: Mapping[str, Any], drop_window: DropWindow
) -> bool:
    """H2 driver: is db-export coverage required by the drill?

    True iff the completeness receipt has any subject with
    ``coverage == "db-export"`` whose window overlaps the drop window.
    """
    for subject in receipt.get("windows") or []:
        if not isinstance(subject, Mapping):
            continue
        if subject.get("coverage") != "db-export":
            continue
        if _subject_overlaps_drop(subject, drop_window):
            return True
    return False


def _drill_derivation_window_contains(
    receipt: Mapping[str, Any], drop_window: DropWindow
) -> bool:
    """#1207 layer 1: does the drill's DECLARED judgment span cover this drop?

    The drill records the drop window it was invoked with at
    ``salvage_derivation.drop_window`` (machine-readable since #1206). A
    drill that declared a NARROWER span than what retention wants to drop
    cannot vouch for this drop: its coverage tuples carry no subject
    identity, so subject A's wide tuple would silently vouch for a subject
    B that the narrowed drill never derived and never restore-verified.

    Unified rule (design D2 of ``fix-retention-drill-window-guard``):

    * key ``salvage_derivation`` entirely ABSENT → ``True`` (no-derivation-
      section receipt: pre-#1206 receipts AND today's explicit-manifest
      drills, which never write the section — behavior unchanged);
    * key PRESENT but unusable (not a Mapping, ``drop_window`` key missing,
      window neither Mapping nor ``null``, unparseable, or inverted) →
      ``False``. A section that exists but cannot be judged is never
      evidence — symmetric with the inverted-tuple defence in
      :func:`_tuples_cover_window`;
    * ``drop_window`` is ``null`` → ``True`` (the drill ran un-narrowed);
    * well-formed window → closed-interval containment. Equality on either
      endpoint PASSES: the runbook §7.5 standard invocation passes the §7.3
      step 3 interval verbatim, and that interval is a documented
      conservative SUPERSET of the runner's own drop window (the runner also
      intersects the eligible chunks with the completeness
      ``coverage_bounds``), so a live drill records a window EQUAL to or
      WIDER than the retention one, never narrower.
    """
    if "salvage_derivation" not in receipt:
        return True
    section = receipt.get("salvage_derivation")
    if not isinstance(section, Mapping) or "drop_window" not in section:
        return False
    window = section["drop_window"]
    if window is None:
        return True
    if not isinstance(window, Mapping):
        return False
    start_raw = window.get("start")
    end_raw = window.get("end")
    if not isinstance(start_raw, str) or not isinstance(end_raw, str):
        return False
    try:
        start = _parse_iso(start_raw)
        end = _parse_iso(end_raw)
    except ValueError:
        return False
    # Redundant for any well-ordered drop window (containment below already
    # refuses an inverted recorded window); kept as an explicit defence so an
    # inverted window can never be read as evidence.
    if end < start:
        return False
    return start <= drop_window.start and end >= drop_window.end


def _drill_snapshot_binds(
    receipt: Mapping[str, Any], targets: Sequence[Mapping[str, str]]
) -> bool:
    """#1220: was every gate-time requirement window in the drill's snapshot?

    The gate's requirement set comes from the completeness receipt it loads
    NOW; the drill's evidence comes from a receipt up to
    ``drill_max_age_days`` old. The completeness receipt is rewritten in
    place daily, so a db-export subject added AFTER the drill (backfill /
    rerun selector) enters the requirement set without ever having been
    restore-verified — and because coverage tuples carry no subject
    identity, an older subject's wide tuple vouches for it.

    The drill records the db-export universe of the snapshot it consumed at
    ``salvage_derivation.db_export_windows`` (unfiltered by any drop window,
    same normalization as :func:`derive_salvage_backed_windows`). Rule
    (design D3 of ``fix-retention-drill-snapshot-binding``):

    * key ``salvage_derivation`` entirely ABSENT → ``True`` (pre-#1206
      receipts AND explicit-manifest drills — same population and same
      neutral treatment as :func:`_drill_derivation_window_contains`);
    * section present but ``db_export_windows`` key ABSENT → ``True``
      (post-#1206 pre-binding receipts; recorded residual in runbook §7.5 —
      the guard is simply dormant for them);
    * key present but unusable (not a list, an entry that is not a Mapping,
      missing / non-string endpoints) → ``False``. A record that exists but
      cannot be judged is never evidence — symmetric with #1207's D2;
    * else ``True`` iff EVERY target's exact ``(start, end)`` pair is a
      member of the recorded set. The subset direction is deliberate:
      requirement SHRINK (a subject removed since the drill) passes, only
      ADDITIONS the drill never saw refuse. Binding the whole gate-time
      universe instead of the drop-filtered targets would refuse whenever
      any db-export subject appears anywhere — the daily-outage direction
      design D1 rejects.

    Binding is judged at WINDOW granularity because that is the gate's own
    requirement granularity (``derive_salvage_backed_windows`` dedups to
    ``{start, end}`` pairs). A new subject whose window is byte-identical to
    a verified one is indistinguishable here and remains layer-2's job
    (residual, runbook §7.5).
    """
    if "salvage_derivation" not in receipt:
        return True
    section = receipt.get("salvage_derivation")
    if not isinstance(section, Mapping):
        # Unreachable through `check_drill_gate` (#1207's guard already
        # refused a non-Mapping section before this runs); fail-closed here
        # so the helper is safe to call on its own.
        return False
    if "db_export_windows" not in section:
        return True
    recorded_raw = section["db_export_windows"]
    if not isinstance(recorded_raw, list):
        return False
    recorded: set[tuple[str, str]] = set()
    for entry in recorded_raw:
        if not isinstance(entry, Mapping):
            return False
        start = entry.get("start")
        end = entry.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            return False
        recorded.add((start, end))
    for target in targets:
        if (target["start"], target["end"]) not in recorded:
            return False
    return True


def check_drill_gate(
    receipt: Mapping[str, Any],
    completeness_receipt: Mapping[str, Any],
    drop_window: DropWindow | None,
    max_age_days: int,
    now: datetime,
) -> list[str]:
    """Evaluate H2: PASS/FAIL + staleness + forcing/runs/db-export coverage.

    Order: STALE → FAIL → derivation-window → forcing → runs → db-export
    [empty-derivation → snapshot-binding → per-target coverage]
    (MISSING is raised by the loader).

    The derivation-window guard (#1207) runs BEFORE every coverage leg: if
    the drill recorded a ``salvage_derivation.drop_window`` that does not
    CONTAIN this drop window, no coverage-union evidence from that run is
    consulted at all — see :func:`_drill_derivation_window_contains`.

    Inside the db-export leg the snapshot-binding guard (#1220) runs after
    the empty-derivation refusal and BEFORE any per-target coverage tuple is
    consulted: a requirement window the drill's completeness snapshot never
    contained refuses with ``DRILL_COMPLETENESS_SNAPSHOT_UNBOUND`` — see
    :func:`_drill_snapshot_binds`.

    The db-export leg is salvage-window-scoped (#1162): drill ``db-export``
    coverage is required only over each salvage-backed window intersected
    with the drop window, judged per window — product-archive-backed
    stretches of the drop window have no db-export package and must not be
    required to; ``forcing`` / ``runs`` keep whole-drop-window semantics.
    """
    reasons: list[str] = []
    generated_at_raw = receipt.get("generated_at")
    if not isinstance(generated_at_raw, str):
        reasons.append(CODE_DRILL_RECEIPT_STALE)
        return reasons
    try:
        generated_at = _parse_iso(generated_at_raw)
    except ValueError:
        reasons.append(CODE_DRILL_RECEIPT_STALE)
        return reasons
    age = now - generated_at
    # Future-dated receipts (negative age) reuse STALE per H8 discipline.
    if age < timedelta(0) or age > timedelta(days=max_age_days):
        reasons.append(CODE_DRILL_RECEIPT_STALE)
        return reasons
    if receipt.get("verdict") != "PASS":
        reasons.append(CODE_DRILL_RECEIPT_FAIL)
        return reasons
    if drop_window is None:
        return reasons
    if not _drill_derivation_window_contains(receipt, drop_window):
        reasons.append(CODE_DRILL_DERIVATION_WINDOW_TOO_NARROW)
        return reasons
    coverage = receipt.get("coverage") or []
    if not _drill_covers(coverage, "forcing", drop_window):
        reasons.append(CODE_DRILL_COVERAGE_FORCING_MISSING)
        return reasons
    if not _drill_covers(coverage, "runs", drop_window):
        reasons.append(CODE_DRILL_COVERAGE_RUNS_MISSING)
        return reasons
    if _completeness_has_db_export_overlap(completeness_receipt, drop_window):
        targets = derive_salvage_backed_windows(completeness_receipt, drop_window)
        if not targets:
            # D2 fail-closed (#1162): the driver saw a db-export subject but
            # nothing is derivable from it (verdict != complete). An empty
            # derivation is NEVER "requirement satisfied".
            reasons.append(CODE_DRILL_COVERAGE_DB_EXPORT_MISSING)
            return reasons
        if not _drill_snapshot_binds(receipt, targets):
            # #1220: the requirement set drifted past the drill's snapshot.
            # Judged BEFORE the tuple union below, otherwise the exact
            # substitution this guard kills would decide first.
            reasons.append(CODE_DRILL_COMPLETENESS_SNAPSHOT_UNBOUND)
            return reasons
        for target in targets:
            clipped = _clip_to_drop(target, drop_window)
            # Zero-length intersections (subject endpoint exactly touching a
            # drop boundary, closed-interval `_overlaps`) stay in scope; an
            # INVERTED clip (`end < start`, only reachable from an inverted
            # subject window) is refused fail-closed instead of being handed
            # to `_drill_covers`, which would vacuously "cover" it — see
            # `_clip_to_drop`.
            if clipped.end < clipped.start or not _drill_covers(coverage, "db-export", clipped):
                reasons.append(CODE_DRILL_COVERAGE_DB_EXPORT_MISSING)
                return reasons
    return reasons


def _clip_to_drop(window: Mapping[str, str], drop_window: DropWindow) -> DropWindow:
    """Intersect a salvage-backed window with the drop window (#1162).

    Subject windows overrun the drop window on BOTH sides in practice (7 d
    forcing_version windows vs. chunk boundaries). Inputs come from
    :func:`derive_salvage_backed_windows`, which only yields subjects whose
    window parsed and overlapped, so parsing is total here and the result is
    normally non-empty (possibly zero-length, which stays in scope).

    An INVERTED subject window (``end`` before ``start``) is the one shape
    that yields ``end < start`` here; the caller refuses fail-closed on it.
    Symmetric with the inverted-tuple defence in :func:`_tuples_cover_window`
    (``if end < start: continue``) — a nonsense interval never counts as
    evidence.
    """
    start = max(_parse_iso(window["start"]), drop_window.start)
    end = min(_parse_iso(window["end"]), drop_window.end)
    return DropWindow(start=start, end=end)


def _tuples_cover_window(
    tuples: Sequence[Mapping[str, Any]], drop_window: DropWindow
) -> bool:
    """Return True iff the UNION of tuple windows covers ``drop_window``.

    H2 semantics per runbook §7.5: the drill emits per-cycle 24 h coverage
    tuples (one per verified product manifest); a 14 d drop window is
    covered by ~14 daily tuples whose union spans it — no single tuple
    needs to individually contain the drop window.

    Standard interval-merge: sort by start, coalesce overlapping/adjacent
    intervals, then check whether any merged interval fully contains the
    drop window.
    """
    parsed: list[tuple[datetime, datetime]] = []
    for entry in tuples:
        if not isinstance(entry, Mapping):
            continue
        window = entry.get("window") or {}
        try:
            start = _parse_iso(window["start"])
            end = _parse_iso(window["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end < start:
            continue
        parsed.append((start, end))
    if not parsed:
        return False
    parsed.sort(key=lambda w: w[0])
    merged: list[tuple[datetime, datetime]] = []
    for start, end in parsed:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    for start, end in merged:
        if start <= drop_window.start and end >= drop_window.end:
            return True
    return False


def _drill_covers(
    coverage: Sequence[Mapping[str, Any]], source: str, drop: DropWindow
) -> bool:
    """H2 recovery coverage: the source tuple union spans the drop window.

    A verified ``db-export`` selector is a forcing-timeseries recovery object,
    so it participates in the forcing union while retaining its independent
    db-export gate. This lets historical DB-only forcing remain recoverable
    without inventing a missing product-archive forcing package.
    """
    accepted_sources = {source}
    if source == "forcing":
        accepted_sources.add("db-export")
    filtered = [
        entry
        for entry in coverage
        if isinstance(entry, Mapping) and entry.get("source") in accepted_sources
    ]
    return _tuples_cover_window(filtered, drop)


def derive_salvage_backed_windows(
    completeness_receipt: Mapping[str, Any], drop_window: DropWindow | None
) -> list[dict[str, str]]:
    """H9: unique ``{start, end}`` dicts sorted ascending.

    From completeness subjects where ``coverage == "db-export"`` AND
    ``verdict == "complete"`` AND the subject window overlaps the drop
    window. Chunk boundaries deliberately are NOT the input (chunks do not
    carry lane/subject identity; recovery per §3.2 is
    completeness-selector-scoped).
    """
    if drop_window is None:
        return []
    seen: set[tuple[str, str]] = set()
    windows: list[dict[str, str]] = []
    for subject in completeness_receipt.get("windows") or []:
        if not isinstance(subject, Mapping):
            continue
        if subject.get("coverage") != "db-export":
            continue
        if subject.get("verdict") != "complete":
            continue
        if not _subject_overlaps_drop(subject, drop_window):
            continue
        window = subject.get("window") or {}
        start = window.get("start")
        end = window.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            continue
        key = (start, end)
        if key in seen:
            continue
        seen.add(key)
        windows.append({"start": start, "end": end})
    windows.sort(key=lambda w: (w["start"], w["end"]))
    return windows


# ---------------------------------------------------------------------------
# DB interaction (H3, H7, H12) — injectable so unit tests substitute stubs.
# ---------------------------------------------------------------------------


FetchChunks = Callable[["RetentionConfig", datetime], list[ChunkRow]]
MeasureChunkBytes = Callable[["RetentionConfig", Sequence[ChunkRow]], dict[str, int]]
DropChunk = Callable[["RetentionConfig", ChunkRow], None]


# SQL: catalog-only enumeration of the two D3 hypertables.
# H3 divergence from #851 compression sibling: retention MUST NOT filter
# `is_compressed = false` — compressed chunks older than 14 d are exactly
# the retention target, so both compressed and uncompressed chunks are
# enumerated. Predicate is `range_end <= %s` (H7 non-strict), which differs
# from compression's strict `<`: a chunk with `range_end == cutoff` has all
# row times strictly less than cutoff and therefore satisfies "entire range
# older than window" per spec §Window and mechanism.
_CHUNK_QUERY = """
SELECT hypertable_schema, hypertable_name, chunk_schema, chunk_name,
       range_start, range_end, is_compressed
FROM timescaledb_information.chunks
WHERE (hypertable_schema, hypertable_name) IN (
    ('hydro', 'river_timeseries'),
    ('met', 'forcing_station_timeseries')
)
  AND range_end <= %s
ORDER BY hypertable_schema, hypertable_name, range_end ASC
"""


def _row_to_chunk(row: Mapping[str, Any]) -> ChunkRow:
    range_start = row["range_start"]
    range_end = row["range_end"]
    if isinstance(range_start, str):
        range_start = _parse_iso(range_start)
    if isinstance(range_end, str):
        range_end = _parse_iso(range_end)
    if range_start.tzinfo is None:
        range_start = range_start.replace(tzinfo=UTC)
    if range_end.tzinfo is None:
        range_end = range_end.replace(tzinfo=UTC)
    return ChunkRow(
        hypertable_schema=str(row["hypertable_schema"]),
        hypertable_name=str(row["hypertable_name"]),
        chunk_schema=str(row["chunk_schema"]),
        chunk_name=str(row["chunk_name"]),
        range_start=range_start.astimezone(UTC),
        range_end=range_end.astimezone(UTC),
        is_compressed=bool(row["is_compressed"]),
    )


def _default_fetch_chunks(config: RetentionConfig, cutoff: datetime) -> list[ChunkRow]:
    import psycopg2  # type: ignore[import-untyped]
    import psycopg2.extras  # type: ignore[import-untyped]

    connection = psycopg2.connect(
        config.database_url, cursor_factory=psycopg2.extras.RealDictCursor
    )
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SET statement_timeout = {_QUERY_TIMEOUT_MS}")
                cursor.execute(_CHUNK_QUERY, (cutoff,))
                return [_row_to_chunk(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _redact_error_text(error: BaseException, dsn: str) -> str:
    """Scrub credentials out of an error before it reaches an operator surface.

    #1213: this is the module's SINGLE error-redaction chokepoint. Every
    driver/uncaught exception text this runner persists crosses it exactly
    once — the per-chunk measurement stderr diagnostic, the drop-phase
    ``RETENTION_DROP_FAILED`` refusal reason, and the ``RETENTION_UNCAUGHT_ERROR``
    fallback refusal reason. Both refusal reasons land in the receipt file AND
    (via ``_emit_stderr_diagnostic``) in the wrapper's long-lived
    ``retention.log``; psycopg2 echoes the full conninfo — plaintext password
    included — on DSN parse failures, so an unredacted interpolation is a
    password-grade leak onto two persisted surfaces.

    Mirrors the salvage runner's ``_mask_dsn_in_message``
    (``scripts/node27_db_export_salvage.py:1039-1041``): the shared policy in
    ``packages/common/redaction.py`` strips the verbatim DSN plus its password
    in both URL-encoded and driver-decoded forms, and the marker is rendered
    as ``***`` for operator-facing text.

    libpq additionally echoes the ROLE name back on connection failures, in
    two shapes the DSN policy does not cover:

    * ``FATAL:  password authentication failed for user "nwm_writer"``
    * ``FATAL:  role "nwm_writer" does not exist``

    Both keywords are scrubbed (every occurrence), and ONLY when the quoted
    name matches the DSN's own username. A bare literal replace of the
    username is deliberately NOT done: a username like ``nwm`` also occurs
    inside unrelated substrings (``/home/nwm/...``) and replacing those would
    mangle the diagnostic.

    libpq's host/port echo (``connection to server at "10.x.x.x", port 55432``)
    is deliberately RETAINED — a diagnosability trade-off recorded in #1213:
    the caller keeps the wire code and exception type name, and the operator
    keeps enough of the driver text to tell an auth failure from a timeout.

    The ``redaction`` import is function-local because that module imports
    ``psycopg2.extensions`` at module scope, and this runner keeps psycopg2
    strictly deferred (config parsing / ``--help`` must work without the
    driver installed).

    That deferral is exactly why this chokepoint is TOTAL — it never raises.
    On a driver-less host (e.g. a venv rebuild window) the very error being
    reported is typically ``DisplayWatermarkError("psycopg2 is unavailable")``,
    and re-importing ``packages.common.redaction`` to redact it would raise
    ``ModuleNotFoundError`` straight out of ``main()``'s except handler —
    destroying the refused receipt and dumping a raw traceback into
    ``retention.log``. Any internal failure therefore degrades to a
    credential-free placeholder naming the original exception type; the
    caller's wire-code prefix and type name are unaffected, so the receipt is
    still published and still greppable.
    """
    try:
        from packages.common.redaction import REDACTION_MARKER, redact_database_dsn

        text = redact_database_dsn(str(error), dsn).replace(REDACTION_MARKER, "***")
        try:
            username = urlsplit(dsn).username
        except ValueError:  # pragma: no cover — malformed DSN, nothing to scrub
            username = None
        if username:
            text = re.sub(rf'\b(user|role) "{re.escape(username)}"', r'\1 "***"', text)
        return text
    except Exception:
        # Fail closed on BOTH axes: withhold the unredactable text entirely
        # (no credential can survive) while keeping the receipt publishable.
        return f"<error text withheld: redaction unavailable ({type(error).__name__})>"


def _default_measure_chunk_bytes(
    config: RetentionConfig, chunks: Sequence[ChunkRow]
) -> dict[str, int]:
    """H4: measure ``chunks_detailed_size(...).total_bytes`` BEFORE drop.

    #1125: ``pg_total_relation_size(chunk)`` sizes only the main chunk
    relation. For a compressed chunk the data lives in the compressed
    sibling (``_timescaledb_internal.compress_hyper_*``), which
    ``drop_chunks`` also removes — the main relation reads ~57 kB while the
    real reclaim is hundreds of MB. TimescaleDB's public
    ``chunks_detailed_size(<hypertable>::regclass)`` returns
    ``total_bytes`` inclusive of the compressed sibling and indexes;
    filtering it to ``(chunk_schema, chunk_name)`` yields this chunk's total
    reclaim. Deliberate divergence from the compression sibling's private-
    catalog join (``node27_timeseries_compression.py`` ``_COMPRESSED_SIBLING_QUERY``):
    retention needs one total-reclaim number the public function returns
    directly (validated byte-exactly against a live ``pg_database_size``
    delta), not a two-step computation over private catalog tables.

    Per-chunk connection (mirrors compression sibling
    ``scripts/node27_timeseries_compression.py:387-428`` — same per-chunk
    isolation and 60 s statement timeout; the sibling additionally passes
    ``connect_timeout``, a pre-existing divergence this change does not
    touch): a shared transaction would enter ``InFailedSqlTransaction`` on the
    first per-chunk failure, silently zeroing every subsequent chunk's
    ``freed_bytes``. Isolating each measurement in its own connection keeps
    the receipt faithful when a single chunk fails to size.

    Per-chunk try/except records ``0`` on failure so the drop phase can
    still proceed and report best-effort ``freed_bytes``; a chunk that
    returns no row (dropped/renamed between enumeration and measurement) or
    a NULL ``total_bytes`` records ``0`` through the same coercion. A failure
    additionally emits one JSON diagnostic line on stderr naming the chunk and
    the (credential-redacted) error — the receipt's ``0`` is otherwise
    indistinguishable from a genuinely empty chunk; the recorded value and
    control flow are unchanged.
    Each connection has a 60 s ``statement_timeout``; the DROP phase opens its
    own connection (300 s) per chunk.
    """
    import psycopg2  # type: ignore[import-untyped]

    if not chunks:
        return {}
    result: dict[str, int] = {}
    for chunk in chunks:
        try:
            connection = psycopg2.connect(config.database_url)
            try:
                with connection:
                    with connection.cursor() as cursor:
                        cursor.execute(f"SET statement_timeout = {_QUERY_TIMEOUT_MS}")
                        cursor.execute(
                            "SELECT total_bytes FROM chunks_detailed_size(%s::regclass) "
                            "WHERE chunk_schema = %s AND chunk_name = %s",
                            (
                                f"{chunk.hypertable_schema}.{chunk.hypertable_name}",
                                chunk.chunk_schema,
                                chunk.chunk_name,
                            ),
                        )
                        row = cursor.fetchone()
                        result[chunk.qualified_name] = int((row[0] if row else 0) or 0)
            finally:
                connection.close()
        except Exception as error:
            # A per-chunk measure failure is not a whole-tick fault. Record
            # 0 so the receipt is faithful; a fresh connection for the next
            # chunk guarantees this chunk's abort does not poison the rest.
            # The receipt cannot distinguish this 0 from a genuine 0, so the
            # cause goes to stderr (diagnostic only — no control-flow change).
            # The error text is credential-redacted: psycopg2 connection
            # failures echo the DSN and the libpq role name back verbatim.
            print(
                json.dumps(
                    {
                        "warning": "freed_bytes measurement failed; recording 0",
                        "chunk": chunk.qualified_name,
                        "error": _redact_error_text(error, config.database_url),
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            result[chunk.qualified_name] = 0
    return result


def _default_drop_chunk(config: RetentionConfig, chunk: ChunkRow) -> None:
    """H3: invoke ``drop_chunks`` for exactly one selected chunk.

    Both time bounds isolate the selected physical interval. The lower bound
    is essential when an older boundary-partial chunk was deferred for
    incomplete evidence: an upper-bound-only call would cascade through that
    protected chunk. Server-side ``drop_chunks`` returns one row per dropped
    chunk; any count other than one fails closed.
    """
    import psycopg2  # type: ignore[import-untyped]

    connection = psycopg2.connect(config.database_url)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SET statement_timeout = {_DROP_TIMEOUT_MS}")
                cursor.execute(
                    "SELECT drop_chunks("
                    "older_than := (%s::timestamptz + INTERVAL '1 microsecond'), "
                    "newer_than := (%s::timestamptz - INTERVAL '1 microsecond'), "
                    "relation := %s::regclass"
                    ")",
                    (
                        chunk.range_end,
                        chunk.range_start,
                        f"{chunk.hypertable_schema}.{chunk.hypertable_name}",
                    ),
                )
                rows = cursor.fetchall()
                # ``drop_chunks`` returns one row per dropped chunk. Bind the
                # returned identity as well as cardinality so a surprising
                # server-side range decision cannot masquerade as success.
                dropped_names = [str(row[0]) for row in rows if row]
                if dropped_names != [chunk.qualified_name]:
                    raise RuntimeError(
                        f"drop_chunks returned {dropped_names!r} for "
                        f"{chunk.qualified_name}; expected exact selected chunk"
                    )
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Receipt build (schema oneOf-strict).
# ---------------------------------------------------------------------------


# Custom ``date-time`` format checker — jsonschema treats ``format`` as
# informational unless a checker registers it, and the built-in FormatChecker
# only registers ``date-time`` when an optional ``rfc3339-validator``-style
# dep is installed (not a project dep here). Register a local checker that
# reuses ``_parse_iso`` so the receipt emitter's OWN output is the acceptance
# oracle — no drift between what we emit and what we accept.
_RECEIPT_FORMAT_CHECKER = jsonschema.FormatChecker()


@_RECEIPT_FORMAT_CHECKER.checks("date-time", raises=(ValueError, TypeError))
def _validate_date_time_format(value: object) -> bool:
    if not isinstance(value, str):
        return False
    _parse_iso(value)
    return True


def _validate_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Validate the receipt with a FormatChecker so ``format: date-time`` on
    ``generated_at`` and ``salvage_backed_windows[].{start,end}`` is
    enforced (schema pins the format; without a checker, jsonschema treats
    format as informational and silently accepts any string).
    """
    schema = _load_schema(_RECEIPT_SCHEMA_PATH)
    jsonschema.validate(receipt, schema, format_checker=_RECEIPT_FORMAT_CHECKER)
    return receipt


def build_receipt(
    outcome: str,
    generated_at: datetime,
    *,
    refusal_reason: str | None = None,
    candidate_chunks: Sequence[str] = (),
    dropped_chunks: Sequence[Mapping[str, Any]] = (),
    deferred_remainder: Sequence[str] = (),
    salvage_backed_windows: Sequence[Mapping[str, str]] = (),
    reference_time: datetime | None = None,
    window_days: int | None = None,
) -> dict[str, Any]:
    """Assemble a schema-``oneOf``-conformant receipt.

    Terminates in exactly one of the three schema branches:
    ``dry-run`` / ``refused`` / ``enforced``. ``refused`` receipts always
    carry ``mode=enforce`` because the schema pins that pairing (dry-run
    invocations that hit a refusal path — concurrent invocation, uncaught
    error, gate refusal — surface an ``enforce+refused`` receipt so
    operators see the wire code).
    """
    if outcome == "dry-run":
        if refusal_reason is not None:
            raise ValueError("dry-run outcome cannot carry refusal_reason")
        receipt: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso(generated_at),
            "mode": "dry-run",
            "outcome": "dry-run",
            "candidate_chunks": list(candidate_chunks),
            "deferred_remainder": list(deferred_remainder),
        }
        if reference_time is not None and window_days is not None:
            receipt.update(
                reference_time=_iso(reference_time),
                window_days=window_days,
                cutoff=_iso(reference_time - timedelta(days=window_days)),
            )
        return _validate_receipt(receipt)
    if outcome == "refused":
        if not refusal_reason:
            raise ValueError("refused outcome requires refusal_reason")
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso(generated_at),
            "mode": "enforce",
            "outcome": "refused",
            "refusal_reason": refusal_reason,
        }
        if reference_time is not None and window_days is not None:
            receipt.update(
                reference_time=_iso(reference_time),
                window_days=window_days,
                cutoff=_iso(reference_time - timedelta(days=window_days)),
            )
        return _validate_receipt(receipt)
    if outcome == "enforced":
        if refusal_reason is not None:
            raise ValueError("enforced outcome cannot carry refusal_reason")
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso(generated_at),
            "mode": "enforce",
            "outcome": "enforced",
            "dropped_chunks": [
                {"name": item["name"], "freed_bytes": int(item["freed_bytes"])}
                for item in dropped_chunks
            ],
            "deferred_remainder": list(deferred_remainder),
            "salvage_backed_windows": [dict(w) for w in salvage_backed_windows],
        }
        if reference_time is not None and window_days is not None:
            receipt.update(
                reference_time=_iso(reference_time),
                window_days=window_days,
                cutoff=_iso(reference_time - timedelta(days=window_days)),
            )
        return _validate_receipt(receipt)
    raise ValueError(f"unknown outcome: {outcome!r}")


def _drop_window_from_eligible(
    eligible: Sequence[ChunkRow], cutoff: datetime
) -> DropWindow | None:
    if not eligible:
        return None
    starts = [c.range_start for c in eligible]
    ends = [c.range_end for c in eligible]
    # drop window is the covering interval of the eligible chunks. This is
    # what the completeness + drill receipts must cover.
    return DropWindow(start=min(starts), end=max(ends))


def _partition_by_completeness_bounds(
    eligible: Sequence[ChunkRow], receipt: Mapping[str, Any]
) -> tuple[list[ChunkRow], list[ChunkRow]]:
    """Keep boundary-partial chunks deferred instead of blocking later work.

    Timescale aligns chunk boundaries to the partition interval, not to the
    first observed row. The first physical chunk can therefore begin before
    the inventory receipt's truthful coverage start. Such a chunk must stay
    intact, while later chunks whose entire physical range is evidenced may
    still progress.

    Invalid bounds deliberately produce no covered chunks so the caller
    retains the existing fail-closed bounds refusal.
    """
    bounds = receipt.get("coverage_bounds") or {}
    try:
        start = _parse_iso(bounds["start"])
        end = _parse_iso(bounds["end"])
    except (KeyError, TypeError, ValueError):
        return [], list(eligible)
    if end < start:
        return [], list(eligible)
    covered: list[ChunkRow] = []
    deferred: list[ChunkRow] = []
    for chunk in eligible:
        target = covered if start <= chunk.range_start and chunk.range_end <= end else deferred
        target.append(chunk)
    return covered, deferred


def publish_receipt(config: RetentionConfig, receipt: Mapping[str, Any]) -> None:
    """Atomically publish the receipt (mode 0600, no-follow, durable replace)."""
    payload = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    atomic_write_bytes_no_follow(
        config.receipt_path, payload, mode=0o600, require_durable_replace=True
    )


def _emit_stderr_diagnostic(receipt: Mapping[str, Any]) -> None:
    """Emit the receipt outcome + refusal_reason (if any) to stderr as JSON."""
    payload: dict[str, Any] = {
        "outcome": receipt.get("outcome"),
        "mode": receipt.get("mode"),
    }
    if "refusal_reason" in receipt:
        payload["refusal_reason"] = receipt["refusal_reason"]
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)


# ---------------------------------------------------------------------------
# Runner core (4 phases per brief §Deliverables 1).
# ---------------------------------------------------------------------------


@dataclass
class _RunnerTrace:
    """Optional in-memory record of receipt path decisions (unit-test aid)."""

    steps: list[tuple[str, Any]] = field(default_factory=list)

    def add(self, name: str, value: Any) -> None:
        self.steps.append((name, value))


def run_retention(
    config: RetentionConfig,
    now: datetime,
    *,
    reference_time: datetime | None = None,
    fetch_chunks: FetchChunks | None = None,
    measure_chunk_bytes: MeasureChunkBytes | None = None,
    drop_chunk: DropChunk | None = None,
) -> dict[str, Any]:
    """Four-phase retention: gate → enumerate → measure → drop.

    Returns the schema-validated receipt dict; the caller is responsible for
    publication.
    """
    fetch_chunks = fetch_chunks or _default_fetch_chunks
    measure_chunk_bytes = measure_chunk_bytes or _default_measure_chunk_bytes
    drop_chunk = drop_chunk or _default_drop_chunk
    reference_time = (reference_time or now).astimezone(UTC)

    def _build(outcome: str, **kwargs: Any) -> dict[str, Any]:
        return build_receipt(
            outcome,
            now,
            reference_time=reference_time,
            window_days=config.window_days,
            **kwargs,
        )

    # Phase 1a: load completeness receipt (raises MISSING on IO/schema fail).
    try:
        completeness = load_completeness_receipt(config.completeness_receipt_path)
    except ReceiptGateError as error:
        return _build("refused", refusal_reason=error.code)

    # Phase 1b: completeness freshness check runs BEFORE enumeration so a
    # stale receipt does not cause a needless DB round-trip. Bounds / gap /
    # pending checks require the drop window (post-enumeration).
    stale_reasons = check_completeness_gate(
        completeness, drop_window=None, max_age_hours=config.completeness_max_age_hours, now=now
    )
    if stale_reasons:
        return _build("refused", refusal_reason=stale_reasons[0])

    # Phase 2a: enumerate chunks + compute drop window.
    cutoff = reference_time - timedelta(days=config.window_days)
    eligible = fetch_chunks(config, cutoff)
    covered_eligible, _boundary_deferred = _partition_by_completeness_bounds(
        eligible, completeness
    )
    if eligible and not covered_eligible:
        return _build(
            "refused",
            refusal_reason=CODE_COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT,
        )
    drop_window = _drop_window_from_eligible(covered_eligible, cutoff)

    # Phase 2b: rerun completeness gate against the concrete drop window
    # (bounds / gap / pending — H1a + H1b).
    reasons = check_completeness_gate(
        completeness,
        drop_window=drop_window,
        max_age_hours=config.completeness_max_age_hours,
        now=now,
    )
    if reasons:
        return _build("refused", refusal_reason=reasons[0])

    # Phase 1c/2c: drill receipt (MISSING / STALE / FAIL / coverage). Loaded
    # here so completeness bounds/gap/pending refusals fire first (matches
    # brief's refusal-ordering table).
    try:
        drill = load_drill_receipt(config.drill_receipt_path)
    except ReceiptGateError as error:
        return _build("refused", refusal_reason=error.code)
    drill_reasons = check_drill_gate(
        drill,
        completeness_receipt=completeness,
        drop_window=drop_window,
        max_age_days=config.drill_max_age_days,
        now=now,
    )
    if drill_reasons:
        return _build("refused", refusal_reason=drill_reasons[0])

    # Phase 3: apply H3 per-tick bound.
    selected = list(covered_eligible[: config.per_tick_bound])
    selected_names = {chunk.qualified_name for chunk in selected}
    deferred_remainder = [
        chunk.qualified_name for chunk in eligible if chunk.qualified_name not in selected_names
    ]

    # Phase 4a: dry-run branch.
    if not config.enforce:
        return _build(
            "dry-run",
            candidate_chunks=[chunk.qualified_name for chunk in selected],
            deferred_remainder=deferred_remainder,
        )

    # Phase 4b: enforce — measure BEFORE drop (H4).
    measured = measure_chunk_bytes(config, selected)

    dropped: list[dict[str, Any]] = []
    for chunk in selected:
        try:
            drop_chunk(config, chunk)
        except Exception as error:
            # H5 whole-tick fail-closed: subsequent chunks NOT attempted.
            # #1213: the driver text is credential-redacted before it reaches
            # the receipt file and the wrapper log; the wire-code +
            # `<hypertable_schema>.<chunk_name>` prefix is byte-unchanged so
            # operator greps keep matching.
            reason = (
                f"{CODE_RETENTION_DROP_FAILED}:"
                f"{chunk.hypertable_schema}.{chunk.chunk_name}: "
                f"{_redact_error_text(error, config.database_url)}"
            )
            return _build("refused", refusal_reason=reason)
        dropped.append(
            {
                "name": chunk.qualified_name,
                "freed_bytes": int(measured.get(chunk.qualified_name, 0)),
            }
        )

    salvage_windows = derive_salvage_backed_windows(completeness, drop_window)
    return _build(
        "enforced",
        dropped_chunks=dropped,
        deferred_remainder=deferred_remainder,
        salvage_backed_windows=salvage_windows,
    )


# ---------------------------------------------------------------------------
# CLI entry.
# ---------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    now: datetime | None = None,
    fetch_chunks: FetchChunks | None = None,
    measure_chunk_bytes: MeasureChunkBytes | None = None,
    drop_chunk: DropChunk | None = None,
) -> int:
    try:
        args = _parser().parse_args(argv)
        config = config_from_args(args)
    except RetentionConfigError as error:
        # RETENTION_CONFIG_INVALID: unable to produce a valid receipt at
        # all (no receipt path validated). Log and exit non-zero.
        print(
            json.dumps(
                {"status": "failed", "code": CODE_RETENTION_CONFIG_INVALID, "reason": str(error)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    stamp = now or datetime.now(UTC)

    # Acquire single-instance lock. On contention, publish a refused
    # receipt with RETENTION_CONCURRENT_INVOCATION and exit non-zero.
    try:
        lock_fd = acquire_lock(config.lock_path)
    except RetentionConfigError as error:
        print(
            json.dumps(
                {"status": "failed", "code": CODE_RETENTION_CONFIG_INVALID, "reason": str(error)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    if lock_fd is None:
        try:
            receipt = build_receipt(
                "refused", stamp, refusal_reason=CODE_RETENTION_CONCURRENT_INVOCATION
            )
            publish_receipt(config, receipt)
            _emit_stderr_diagnostic(receipt)
        except Exception as pub_error:  # pragma: no cover — receipt best-effort
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "code": CODE_RETENTION_CONCURRENT_INVOCATION,
                        "reason": f"receipt publication failed: {pub_error}",
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        return 1

    try:
        try:
            reference_time = (
                now
                if now is not None
                else fetch_display_watermark(config.database_url)
            )
            receipt = run_retention(
                config,
                stamp,
                reference_time=reference_time,
                fetch_chunks=fetch_chunks,
                measure_chunk_bytes=measure_chunk_bytes,
                drop_chunk=drop_chunk,
            )
        except Exception as error:
            # RETENTION_UNCAUGHT_ERROR — symmetric with #854
            # DRILL_UNCAUGHT_ERROR. Emit a schema-valid refused receipt
            # rather than a raw stack trace.
            # #1213: this is the path a psycopg2 DSN-parse failure takes, and
            # that exception echoes the whole conninfo (password included), so
            # the text crosses the redaction chokepoint before it is persisted.
            # Wire code + exception type name are byte-unchanged.
            reason = (
                f"{CODE_RETENTION_UNCAUGHT_ERROR}:{type(error).__name__}: "
                f"{_redact_error_text(error, config.database_url)}"
            )
            receipt = build_receipt("refused", stamp, refusal_reason=reason)
            try:
                publish_receipt(config, receipt)
            except SafeFilesystemError as pub_error:
                print(
                    json.dumps(
                        {
                            "status": "failed",
                            "code": CODE_RETENTION_UNCAUGHT_ERROR,
                            "reason": f"receipt publication failed: {pub_error}",
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                return 1
            _emit_stderr_diagnostic(receipt)
            return 1
        try:
            publish_receipt(config, receipt)
        except SafeFilesystemError as pub_error:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "code": CODE_RETENTION_UNCAUGHT_ERROR,
                        "reason": f"receipt publication failed: {pub_error}",
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 1
        _emit_stderr_diagnostic(receipt)
        outcome = receipt.get("outcome")
        if outcome in {"dry-run", "enforced"}:
            return 0
        return 1
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
