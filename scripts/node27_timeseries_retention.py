#!/usr/bin/env python3
"""TimescaleDB retention runner for node-27 (issue #855 §6.1 + §6.2).

Drops chunks strictly older than a configurable window (default 14 d) from
the two D3 detail hypertables ``hydro.river_timeseries`` and
``met.forcing_station_timeseries``.

ADR 0002 (as revised 2026-08-11): the archive lane was permanently retired
after the ``/dev/md0`` double-disk failure, and the ADR's "no deletion
without archive receipt" invariant was explicitly amended. #1370 completed
that retirement: the archive-completeness and archive-rebuild-drill gates,
their receipt loaders, and the thirteen archive-family wire codes no longer
exist, and ``enabled`` is a retired mode rather than a selectable one.

``NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE`` therefore MUST be set
explicitly to ``disabled``; unset, ``enabled``, or any other value refuses
with ``RETENTION_CONFIG_INVALID`` (exit 2, no receipt, diagnostics citing the
ADR revision). The fail-closed direction is unchanged — an unset variable
never deletes anything — and the deletion authority stays auditable: every
receipt records ``archive_gate.mode = "disabled"`` plus the pinned citation
``docs/adr/0002-node27-timeseries-hot-cold-tiering.md Revision 2026-08-11``.

Design references (design.md #855 fixture pins H1-H17), as they stand after
the archive-lane retirement:

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
- H6 wire codes byte-identical across code / runbook §8.2 / design #855
  (H1/H2/H8/H9 retired with the gates; the thirteen archive-family codes
  they defined are gone — see #1370).
- H7 predicate ``range_end <= cutoff`` (non-strict; divergence from #851's
  strict ``<`` — a chunk with ``range_end == cutoff`` has all row times
  strictly less than cutoff, satisfying "entire range older than window").
- H10 lock path byte-identical with runbook §8 + ``.example``.
- H12 statement timeouts: 60 s for catalog enumeration, 300 s per ``drop_chunks``.
- H13 env prefix ``NODE27_TIMESERIES_RETENTION_*``.

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
from packages.common.storage import DEFAULT_RETENTION_WINDOW_DAYS

# 1.1 (#1369): every receipt gained the required ``archive_gate`` object.
# Historical 1.0 receipts are NEVER rewritten — see the receipts README for
# the dual-version reading rule.
SCHEMA_VERSION = "1.1"
TOOL_VERSION = "node27-timeseries-retention/1"

# Archive-gate mode (#1369, ADR 0002 Revision 2026-08-11). Not a wire code and
# not a refusal: it is the recorded deletion authority on every receipt.
# ``ARCHIVE_GATE_ENABLED`` is RETIRED (#1370) and survives for exactly two
# reasons: the receipt schema's ``mode`` enum still admits it so historical
# 1.1 receipts stay validatable, and ``--archive-gate`` keeps it as a choice
# so requesting it lands in ``_resolve_archive_gate`` — the single refusal
# path that carries the wire code and the ADR citation — instead of an
# argparse usage error. The runner never emits it.
ARCHIVE_GATE_ENABLED = "enabled"
ARCHIVE_GATE_DISABLED = "disabled"
ARCHIVE_GATE_MODES: frozenset[str] = frozenset({ARCHIVE_GATE_ENABLED, ARCHIVE_GATE_DISABLED})
ARCHIVE_GATE_ENV = "NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE"
# Pinned citation for the disabled mode's authorization. The receipt schema
# repeats this as a ``const`` so a receipt cannot claim a vaguer source.
ARCHIVE_GATE_ADR_REFERENCE = (
    "docs/adr/0002-node27-timeseries-hot-cold-tiering.md Revision 2026-08-11"
)
# Operator-facing retirement diagnostics (#1370). Both fragments are pinned by
# tests: the first names the authority, the second says what to do about it.
ARCHIVE_GATE_RETIRED_DIAGNOSTIC = (
    "archive lane permanently retired (ADR 0002 Revision 2026-08-11)"
)

# H10 canonical lock path — byte-identical with runbook §8 + `.example`.
_DEFAULT_LOCK_PATH_STR = "/tmp/nhms-node27-timeseries-retention.lock"

# H3 per-tick bound + window defaults (matches spec §Window and mechanism).
_DEFAULT_WINDOW_DAYS = DEFAULT_RETENTION_WINDOW_DAYS
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
# * ``tests/test_node27_timeseries_retention.py``,
# * ``openspec/changes/tier-node27-timeseries-storage/design.md`` (#855) —
#   the forward-walk test reads this fixture too.
# Any addition / rename MUST land in all four surfaces in the same commit
# (same-class recurrence discipline from #854). Retirement is the asymmetry:
# retired codes stay verbatim in the #855 fixture and move to the reverse
# walk's allowlist instead of being deleted from it.
#
# #1370: the thirteen archive-family codes (``COMPLETENESS_*`` x5,
# ``DRILL_*`` x8) retired together with the archive lane and the ``enabled``
# gate mode. Only the runner's own refusals remain.
CODE_RETENTION_CONFIG_INVALID = "RETENTION_CONFIG_INVALID"
CODE_RETENTION_CONCURRENT_INVOCATION = "RETENTION_CONCURRENT_INVOCATION"
CODE_RETENTION_DROP_FAILED = "RETENTION_DROP_FAILED"
CODE_RETENTION_UNCAUGHT_ERROR = "RETENTION_UNCAUGHT_ERROR"

WIRE_CODES: frozenset[str] = frozenset(
    {
        CODE_RETENTION_CONFIG_INVALID,
        CODE_RETENTION_CONCURRENT_INVOCATION,
        CODE_RETENTION_DROP_FAILED,
        CODE_RETENTION_UNCAUGHT_ERROR,
    }
)

_ROOT = Path(__file__).resolve().parents[1]
_RECEIPT_SCHEMA_PATH = _ROOT / "schemas/timeseries_retention_receipt.schema.json"


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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionConfig:
    """Immutable retention runner configuration.

    Every field is populated via :func:`config_from_args` from the H13 env
    catalogue plus CLI overrides. Defaults are pinned to H10/H12/§Window.

    ``archive_gate`` is always ``disabled`` — :func:`_resolve_archive_gate`
    admits nothing else (#1370). It is carried on the config (rather than
    assumed) because every receipt must record the deletion authority in
    force when it was written.
    """

    database_url: str
    window_days: int
    per_tick_bound: int
    receipt_path: Path
    lock_path: Path
    enforce: bool
    archive_gate: str = ARCHIVE_GATE_DISABLED


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


def _resolve_archive_gate(args_value: str | None, env_value: str | None) -> str:
    """Resolve the archive-gate mode (CLI wins, then env). Only ``disabled``.

    #1370: the archive lane is permanently retired, so this is no longer a
    mode switch — it is an explicit acknowledgement. ``disabled`` (after
    strip + lowercase) is the only accepted value; unset, the retired
    ``enabled``, and every typo (``disable``, ``true``, ``1``, the empty
    string) refuse with ``RETENTION_CONFIG_INVALID``.

    The fail-closed direction is unchanged. Before this change an unset
    variable selected the archive gate, which then refused for want of
    receipts nothing produces any more; now it refuses at config parse. In
    both worlds an unset variable deletes nothing — what changed is that the
    refusal is diagnosable instead of a hourly gate refusal on a dead lane.
    """
    raw = args_value if args_value is not None else env_value
    if raw is not None and raw.strip().lower() == ARCHIVE_GATE_DISABLED:
        return ARCHIVE_GATE_DISABLED
    raise RetentionConfigError(
        f"{ARCHIVE_GATE_ENV} must be {ARCHIVE_GATE_DISABLED!r}, got {raw!r}: "
        f"{ARCHIVE_GATE_RETIRED_DIAGNOSTIC}; "
        f"set {ARCHIVE_GATE_ENV}={ARCHIVE_GATE_DISABLED} to acknowledge that "
        "chunks are dropped with no archive backstop"
    )


def config_from_args(
    args: argparse.Namespace, env: Mapping[str, str] | None = None
) -> RetentionConfig:
    """Strict env + CLI parse. No truthiness fallback. Fails closed on bad shape."""
    env = os.environ if env is None else env
    database_url = env.get("DATABASE_URL")
    if not database_url or not database_url.strip():
        raise RetentionConfigError("DATABASE_URL must be set")
    archive_gate = _resolve_archive_gate(
        getattr(args, "archive_gate", None), env.get(ARCHIVE_GATE_ENV)
    )
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
    # #1370: the two archive receipt paths and their two max-age variables are
    # gone with the gates. A box whose env file still carries them keeps
    # working — the runner simply never reads those keys.
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
        receipt_path=receipt_path,
        lock_path=lock_path,
        enforce=enforce,
        archive_gate=archive_gate,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="node27_timeseries_retention", description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--enforce", action="store_true", help="actually invoke drop_chunks")
    group.add_argument("--dry-run", action="store_true", help="dry-run (default)")
    parser.add_argument("--receipt-path", dest="receipt_path", type=str, default=None)
    parser.add_argument("--lock-path", dest="lock_path", type=str, default=None)
    parser.add_argument(
        "--archive-gate",
        dest="archive_gate",
        # ``enabled`` stays a CHOICE although it is retired: argparse would
        # reject it with a bare usage error, and the operator needs the wire
        # code plus the ADR citation that ``_resolve_archive_gate`` emits.
        choices=[ARCHIVE_GATE_ENABLED, ARCHIVE_GATE_DISABLED],
        default=None,
        help=(
            f"must be {ARCHIVE_GATE_DISABLED!r} (or set {ARCHIVE_GATE_ENV}): "
            f"{ARCHIVE_GATE_RETIRED_DIAGNOSTIC}, so chunks are DROPPED WITH "
            f"NO ARCHIVE BACKSTOP. {ARCHIVE_GATE_ENABLED!r} is retired and "
            "refuses with RETENTION_CONFIG_INVALID"
        ),
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
# Schema + timestamp helpers.
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

    The shared policy in ``packages/common/redaction.py`` strips the verbatim
    DSN plus its password in both URL-encoded and driver-decoded forms, and
    the marker is rendered as ``***`` for operator-facing text.

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
    format as informational and silently accepts any string). The schema is
    unmodified by #1370, so it still accepts the historical receipts whose
    ``salvage_backed_windows`` is non-empty.
    """
    schema = _load_schema(_RECEIPT_SCHEMA_PATH)
    jsonschema.validate(receipt, schema, format_checker=_RECEIPT_FORMAT_CHECKER)
    return receipt


def _archive_gate_block(mode: str) -> dict[str, Any]:
    """Build the required ``archive_gate`` object (#1369, schema 1.1).

    Mirrors the schema's nested ``oneOf``: a ``disabled`` block carries the
    pinned ADR citation, an ``enabled`` block MUST NOT. The runner only ever
    emits ``disabled`` (#1370) — the ``enabled`` shape is kept so this stays a
    faithful mirror of the unmodified schema that still validates historical
    1.1 receipts.
    """
    if mode not in ARCHIVE_GATE_MODES:
        raise ValueError(f"unknown archive gate mode: {mode!r}")
    block: dict[str, Any] = {"mode": mode}
    if mode == ARCHIVE_GATE_DISABLED:
        block["adr_reference"] = ARCHIVE_GATE_ADR_REFERENCE
    return block


def build_receipt(
    outcome: str,
    generated_at: datetime,
    *,
    refusal_reason: str | None = None,
    candidate_chunks: Sequence[str] = (),
    dropped_chunks: Sequence[Mapping[str, Any]] = (),
    deferred_remainder: Sequence[str] = (),
    reference_time: datetime | None = None,
    window_days: int | None = None,
    archive_gate: str = ARCHIVE_GATE_DISABLED,
) -> dict[str, Any]:
    """Assemble a schema-``oneOf``-conformant receipt.

    Terminates in exactly one of the three schema branches:
    ``dry-run`` / ``refused`` / ``enforced``. ``refused`` receipts always
    carry ``mode=enforce`` because the schema pins that pairing (dry-run
    invocations that hit a refusal path — concurrent invocation, uncaught
    error — surface an ``enforce+refused`` receipt so operators see the wire
    code).

    All three branches carry ``archive_gate`` (#1369) — a refused receipt
    must self-describe the deletion authority in force when it refused, or
    the audit trail has a hole exactly where that authority is questioned.
    """
    gate_block = _archive_gate_block(archive_gate)
    if outcome == "dry-run":
        if refusal_reason is not None:
            raise ValueError("dry-run outcome cannot carry refusal_reason")
        receipt: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso(generated_at),
            "mode": "dry-run",
            "outcome": "dry-run",
            "archive_gate": gate_block,
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
            "archive_gate": gate_block,
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
            "archive_gate": gate_block,
            "dropped_chunks": [
                {"name": item["name"], "freed_bytes": int(item["freed_bytes"])}
                for item in dropped_chunks
            ],
            "deferred_remainder": list(deferred_remainder),
            # #1370: with the archive lane retired nothing can vouch for a
            # deletion, so the schema-required list is structurally empty —
            # "nothing endorsed this", stated rather than left ambiguous.
            "salvage_backed_windows": [],
        }
        if reference_time is not None and window_days is not None:
            receipt.update(
                reference_time=_iso(reference_time),
                window_days=window_days,
                cutoff=_iso(reference_time - timedelta(days=window_days)),
            )
        return _validate_receipt(receipt)
    raise ValueError(f"unknown outcome: {outcome!r}")


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
    """Three-phase retention: enumerate → measure → drop.

    Returns the schema-validated receipt dict; the caller is responsible for
    publication.

    #1370: the gate phase is gone. ``config.archive_gate`` is always
    ``disabled`` (``_resolve_archive_gate`` admits nothing else) and is used
    only to stamp the deletion authority onto every receipt. Everything else —
    lock, watermark reference time, retention window, per-tick bound,
    measure-before-drop ordering, H5 fail-closed drop handling — is unchanged
    from the pre-retirement disabled-mode behavior, byte for byte.
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
            archive_gate=config.archive_gate,
            **kwargs,
        )

    # Phase 1: enumerate eligible chunks.
    # There is no archive coverage object any more and therefore no
    # "partially covered" notion, so a chunk straddling what used to be the
    # inventory coverage boundary is NOT deferred: it is a drop candidate like
    # any other (runbook §8.5).
    cutoff = reference_time - timedelta(days=config.window_days)
    eligible = fetch_chunks(config, cutoff)

    # Phase 2: apply H3 per-tick bound.
    selected = list(eligible[: config.per_tick_bound])
    selected_names = {chunk.qualified_name for chunk in selected}
    deferred_remainder = [
        chunk.qualified_name for chunk in eligible if chunk.qualified_name not in selected_names
    ]

    # Phase 3a: dry-run branch.
    if not config.enforce:
        return _build(
            "dry-run",
            candidate_chunks=[chunk.qualified_name for chunk in selected],
            deferred_remainder=deferred_remainder,
        )

    # Phase 3b: enforce — measure BEFORE drop (H4).
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

    # ``salvage_backed_windows`` is emitted as the empty list by
    # :func:`build_receipt` — nothing can endorse a deletion any more.
    return _build(
        "enforced",
        dropped_chunks=dropped,
        deferred_remainder=deferred_remainder,
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
                "refused",
                stamp,
                refusal_reason=CODE_RETENTION_CONCURRENT_INVOCATION,
                archive_gate=config.archive_gate,
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
            # RETENTION_UNCAUGHT_ERROR: emit a schema-valid refused receipt
            # rather than a raw stack trace.
            # #1213: this is the path a psycopg2 DSN-parse failure takes, and
            # that exception echoes the whole conninfo (password included), so
            # the text crosses the redaction chokepoint before it is persisted.
            # Wire code + exception type name are byte-unchanged.
            reason = (
                f"{CODE_RETENTION_UNCAUGHT_ERROR}:{type(error).__name__}: "
                f"{_redact_error_text(error, config.database_url)}"
            )
            receipt = build_receipt(
                "refused", stamp, refusal_reason=reason, archive_gate=config.archive_gate
            )
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
