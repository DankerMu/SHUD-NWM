"""Fail-closed pre-write guard against writes into compressed TimescaleDB chunks.

Ingest and reingest into ``hydro.river_timeseries`` and
``met.forcing_station_timeseries`` must never mutate rows inside a compressed
chunk. TimescaleDB 2.10 rejects such writes with its own raw error, but that
error surfaces AFTER the DELETE fires; callers cannot distinguish it from an
unrelated transient failure. This guard runs a single catalog lookup against
``timescaledb_information.chunks`` before the caller's DELETE + INSERT, raising
a structured ``CompressedChunkWriteError`` that names the offending chunk and
points at the runbook decompress procedure.

Semantics:

* Overlap: any compressed chunk where ``range_start <= batch_valid_time_max``
  AND ``range_end > batch_valid_time_min`` blocks the write. ``range_end`` is
  exclusive and ``range_start`` is inclusive per the TimescaleDB chunk model
  ``[range_start, range_end)`` (see
  ``openspec/changes/tier-node27-timeseries-storage`` fixture #851 / design.md).
* Empty batch / unset window: short-circuit — no catalog query, no raise. AND
  semantics: BOTH endpoints must be ``None`` (a partial ``None`` is a caller
  bug and raises ``CompressedChunkGuardError``).
* Statement timeout: ``SET LOCAL statement_timeout = '5s'`` bounds the
  catalog query latency and does not leak across the transaction. The reset
  to ``DEFAULT`` fires in ``finally:`` so a raised catalog error still
  restores the session default.
* Fail-closed on catalog error: the guard NEVER silently permits a write. Any
  exception from the lookup wraps into ``CompressedChunkGuardError`` and
  propagates to the caller's transaction, which rolls back.
* Runtime registry enforcement: the guard refuses any ``(schema, table)``
  pair outside :data:`HYPERTABLES_GUARDED` (raising
  ``CompressedChunkGuardError`` before any SQL runs) so a wire-site typo
  cannot silently permit writes.

The guard is a shared helper (design D5); the four production write paths
(``workers/output_parser/parser.py::upsert_river_timeseries``,
``workers/forcing_producer/store.py::replace_forcing_timeseries``,
``packages/common/forcing_domain_handoff_apply.py::
_replace_forcing_station_timeseries``,
``scripts/node27_river_identity_backfill.py``) all import from this module.
Display API and frontend code paths never import this module (ADR 0001).

The first three are batch writers that know a ``valid_time`` window and use
:func:`check_batch_targets_uncompressed`. The fourth (issue #1339) walks named
chunks directly rather than time windows, so it uses
:func:`assert_chunk_uncompressed`, which answers the same question
("would this write land in compressed storage?") for a chunk identity instead
of a time range. Both share the registry check, the statement-timeout
discipline, and the fail-closed catalog-error contract; neither is a
per-caller reimplementation, which is exactly what design D5 forbids.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from packages.common.redaction import redact_text

RUNBOOK_ANCHOR = "docs/runbooks/tier-node27-timeseries-storage.md#43-decompress-procedure"
"""Runbook anchor referenced by every ``CompressedChunkWriteError`` message.

The runbook heading ``### 4.3 Decompress procedure`` renders as this GitHub
anchor (dot removed, spaces become dashes, lowercase). The anchor is a
caller-observable contract; changing it requires updating the runbook too.
"""

HYPERTABLES_GUARDED = frozenset(
    {
        ("hydro", "river_timeseries"),
        ("met", "forcing_station_timeseries"),
    }
)
"""Production hypertable identities the guard is wired to defend.

The archive rebuild drill (#854) writes to an isolated staging schema and
therefore never trips the guard; this constant is asserted by tests to catch
accidental additions/removals.
"""

_STATEMENT_TIMEOUT_LITERAL = "5s"
_COMPRESSED_CHUNK_QUERY = (
    "SELECT chunk_schema, chunk_name FROM timescaledb_information.chunks "
    "WHERE hypertable_schema = %s AND hypertable_name = %s "
    "AND is_compressed = true "
    "AND range_start <= %s AND range_end > %s "
    "ORDER BY range_start "
    "LIMIT 1"
)
_CHUNK_IDENTITY_QUERY = (
    "SELECT is_compressed FROM timescaledb_information.chunks "
    "WHERE hypertable_schema = %s AND hypertable_name = %s "
    "AND chunk_schema = %s AND chunk_name = %s"
)


class CompressedChunkGuardError(Exception):
    """Raised when the compressed-chunk guard cannot certify a batch as safe.

    Base class covering catalog lookup failures. Subclass
    ``CompressedChunkWriteError`` covers the specific case where a compressed
    chunk was detected. Callers that want to distinguish the two cases MUST
    check for the subclass first.
    """


class CompressedChunkWriteError(CompressedChunkGuardError):
    """Raised when a batch would write into a compressed chunk.

    The message names the offending chunk and references the runbook
    decompress procedure. Attributes preserve the structured identity for
    downstream monitoring or programmatic handling.
    """

    def __init__(
        self,
        *,
        chunk_schema: str,
        chunk_name: str,
        hypertable_schema: str,
        hypertable_name: str,
        runbook_anchor: str = RUNBOOK_ANCHOR,
    ) -> None:
        self.chunk_schema = chunk_schema
        self.chunk_name = chunk_name
        self.hypertable_schema = hypertable_schema
        self.hypertable_name = hypertable_name
        self.runbook_anchor = runbook_anchor
        message = (
            f"Reingest targets compressed chunk {chunk_schema}.{chunk_name} in "
            f"{hypertable_schema}.{hypertable_name}; run decompress procedure per "
            f"{runbook_anchor} before retrying."
        )
        super().__init__(message)


def check_batch_targets_uncompressed(
    cursor: Any,
    *,
    hypertable_schema: str,
    hypertable_name: str,
    valid_time_min: datetime | None,
    valid_time_max: datetime | None,
) -> None:
    """Fail-closed check that ``[valid_time_min, valid_time_max]`` misses every compressed chunk.

    Raises ``CompressedChunkWriteError`` if any compressed chunk overlaps the
    batch time range. Raises ``CompressedChunkGuardError`` if the catalog
    lookup itself fails, if the guard is called for an unregistered
    ``(schema, table)`` pair (see :data:`HYPERTABLES_GUARDED`), or if only one
    of ``valid_time_min`` / ``valid_time_max`` is ``None`` (partial batch
    window). Returns ``None`` on pass (or on empty/unset window where BOTH
    endpoints are ``None``).

    The cursor MUST share the same connection/transaction as the DELETE that
    follows so ``SET LOCAL statement_timeout`` scopes to the correct
    transaction. The catalog lookup runs once per batch (amortized over
    batch_size rows) and has no module-level cache — a stale cache would
    permit a silent partial write.

    Empty-batch semantics: BOTH endpoints must be ``None``. Callers computing
    ``min(... , default=None)`` on an empty iterable naturally produce
    ``(None, None)``; a partial ``None`` indicates a caller bug (see
    ``workers/forcing_producer/store.py::replace_forcing_timeseries`` and
    ``packages/common/forcing_domain_handoff_apply.py::
    _replace_forcing_station_timeseries``) and fails closed rather than
    silently short-circuiting.
    """
    # Empty batch short-circuit — BEFORE the registry check, since an empty
    # batch is a no-op regardless of which table would have been written to.
    if valid_time_min is None and valid_time_max is None:
        return
    if valid_time_min is None or valid_time_max is None:
        raise CompressedChunkGuardError(
            "guard called with partial batch range (one endpoint None); "
            "callers MUST pass both endpoints or both None for empty batch "
            f"(min={valid_time_min!r}, max={valid_time_max!r})"
        )

    # Wire-site typo guard: refuse any unregistered pair BEFORE any SQL runs.
    if (hypertable_schema, hypertable_name) not in HYPERTABLES_GUARDED:
        raise CompressedChunkGuardError(
            f"guard called with unregistered hypertable "
            f"{hypertable_schema}.{hypertable_name}; expected one of "
            f"{sorted(HYPERTABLES_GUARDED)}"
        )

    row: Any = None
    try:
        try:
            cursor.execute(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT_LITERAL}'")
            cursor.execute(
                _COMPRESSED_CHUNK_QUERY,
                (hypertable_schema, hypertable_name, valid_time_max, valid_time_min),
            )
            row = cursor.fetchone()
        except CompressedChunkWriteError:
            raise
        except Exception as error:
            reason = (
                "compressed-chunk guard catalog lookup failed on "
                f"{hypertable_schema}.{hypertable_name}: {type(error).__name__}: "
                f"{redact_text(str(error))}"
            )
            raise CompressedChunkGuardError(_mask_dsn(reason)) from error
    finally:
        # Restore the session's default statement_timeout so the guard's short
        # timeout does not apply to the DELETE + INSERT that follow in the same
        # transaction. This MUST fire even when the SELECT raised — otherwise
        # the caller inherits the guard's 5s cap on their own writes. Best
        # effort: if the reset itself raises (e.g. transaction already aborted
        # by the SELECT error), suppress and let the caller's rollback path
        # handle it.
        try:
            cursor.execute("SET LOCAL statement_timeout = DEFAULT")
        except Exception:
            pass

    if row is None:
        return

    chunk_schema, chunk_name = _extract_chunk_identity(row)
    raise CompressedChunkWriteError(
        chunk_schema=chunk_schema,
        chunk_name=chunk_name,
        hypertable_schema=hypertable_schema,
        hypertable_name=hypertable_name,
    )


def assert_chunk_uncompressed(
    cursor: Any,
    *,
    hypertable_schema: str,
    hypertable_name: str,
    chunk_schema: str,
    chunk_name: str,
) -> None:
    """Fail-closed check that one NAMED chunk is not compressed.

    The chunk-identity counterpart of
    :func:`check_batch_targets_uncompressed`, added for the identity-backfill
    runner (issue #1339), which iterates chunks by name rather than by
    ``valid_time`` window and therefore cannot express its target as a batch
    range. Same contract, same failure modes:

    * Raises ``CompressedChunkWriteError`` when the chunk is compressed.
      TimescaleDB 2.10 supports no DML at all against compressed chunks, so
      the caller must route through the runbook decompress procedure.
    * Raises ``CompressedChunkGuardError`` when the catalog lookup fails, when
      the ``(schema, table)`` pair is outside :data:`HYPERTABLES_GUARDED`, or
      when the chunk is **absent from the catalog**. An unknown chunk is not
      treated as "probably fine": it usually means the chunk was dropped or
      renamed between discovery and write, and certifying it would be a
      guess.
    * Returns ``None`` on pass.

    The cursor MUST share the connection/transaction of the UPDATE that
    follows, so ``SET LOCAL statement_timeout`` scopes correctly and the
    catalog read and the write see the same snapshot.
    """
    if (hypertable_schema, hypertable_name) not in HYPERTABLES_GUARDED:
        raise CompressedChunkGuardError(
            f"guard called with unregistered hypertable "
            f"{hypertable_schema}.{hypertable_name}; expected one of "
            f"{sorted(HYPERTABLES_GUARDED)}"
        )

    row: Any = None
    try:
        try:
            cursor.execute(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT_LITERAL}'")
            cursor.execute(
                _CHUNK_IDENTITY_QUERY,
                (hypertable_schema, hypertable_name, chunk_schema, chunk_name),
            )
            row = cursor.fetchone()
        except Exception as error:
            reason = (
                "compressed-chunk guard chunk lookup failed on "
                f"{chunk_schema}.{chunk_name}: {type(error).__name__}: "
                f"{redact_text(str(error))}"
            )
            raise CompressedChunkGuardError(_mask_dsn(reason)) from error
    finally:
        # Same rationale as the batch guard: the guard's short timeout must not
        # leak onto the caller's own write, and the reset must fire even when
        # the lookup raised.
        try:
            cursor.execute("SET LOCAL statement_timeout = DEFAULT")
        except Exception:
            pass

    if row is None:
        raise CompressedChunkGuardError(
            f"chunk {chunk_schema}.{chunk_name} is not present in "
            f"timescaledb_information.chunks for "
            f"{hypertable_schema}.{hypertable_name}; refusing to certify an "
            "unknown chunk as uncompressed"
        )

    if _extract_is_compressed(row):
        raise CompressedChunkWriteError(
            chunk_schema=chunk_schema,
            chunk_name=chunk_name,
            hypertable_schema=hypertable_schema,
            hypertable_name=hypertable_name,
        )


def _extract_is_compressed(row: Any) -> bool:
    """Extract ``is_compressed`` from a psycopg2 row (tuple or dict cursor)."""
    if isinstance(row, dict):
        return bool(row["is_compressed"])
    return bool(row[0])


def _extract_chunk_identity(row: Any) -> tuple[str, str]:
    """Extract ``(chunk_schema, chunk_name)`` from a psycopg2 row.

    Supports tuple/list rows and dict-cursor rows so the guard works
    regardless of which cursor factory the caller uses.
    """
    if isinstance(row, dict):
        return str(row["chunk_schema"]), str(row["chunk_name"])
    return str(row[0]), str(row[1])


def _mask_dsn(message: str) -> str:
    """Scrub any DSN-shaped text from an error message.

    Defense-in-depth: the guard's error path never intentionally exposes a
    DSN, but the underlying exception may embed one when a psycopg2 error is
    stringified by its type name only (this call is on the reason string
    before the ``from`` chain, so masking here is belt-and-suspenders).
    """
    return redact_text(message)
