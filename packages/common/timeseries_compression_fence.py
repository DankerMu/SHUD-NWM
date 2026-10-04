"""DB advisory read/write fence between chunk DDL and ingest writes (#2713).

One fence per canonical hypertable FAMILY orders the periodic chunk DDL lanes
(``scripts/node27_timeseries_compression.py`` and the retention drop in
``scripts/node27_timeseries_retention.py``) against every production ingest
write transaction on that family:

* **Ingest** takes the fence SHARED, TRY-ONLY and TRANSACTION-SCOPED
  (``pg_try_advisory_xact_lock_shared``). It never blocks: the parser's
  sessions carry a 60 s ``statement_timeout`` and the forcing apply a 600 s
  one, so a blocking wait would be cancelled as ``57014`` and mark the run
  failed. A failed try raises :class:`IngestFenceBusy`; the writer rolls back
  and the next ingest tick retries the run.
* **Chunk DDL** takes the fence EXCLUSIVE and SESSION-SCOPED
  (``pg_advisory_lock``) with a bounded ``lock_timeout`` wait, while holding
  no other lock, before it touches the chunk. A timed-out wait raises
  :class:`FenceContended` and nothing has been copied or dropped.

PostgreSQL's lock queue makes a new shared request conflict with an already
QUEUED exclusive request, so once compression asks for the fence no new ingest
transaction can start on that family; in-flight writers drain and the DDL
proceeds. A backend that already holds the shared lock is granted it again.

The fence MUST be the FIRST statement of every writer transaction. A writer
that first locks an FK-referenced table (``hydro.hydro_run FOR UPDATE``,
``met.met_station`` upserts) or probes the hypertable (AccessShare on every
chunk) and only then tries the fence would re-create the #2713 cycle:
``compress_chunk`` / ``drop_chunks`` also lock those relations, so the DDL
would wait on the writer while the writer's later DELETE waits on the DDL.

Family, not hypertable: a ``X_legacy`` sibling shares the key of its canonical
``X``. Ingest only writes the canonical table, but the sibling references the
same FK tables (``core.river_segment``, ``hydro.hydro_run``,
``met.met_station``, ``met.forcing_version``), so DDL on a legacy chunk locks
exactly the relations a canonical writer holds. A separate legacy key would
leave that cycle open.

Operational effect: while one chunk of either family member compresses (about
20-35 min, at most the per-tick bound a day) or is dropped, ingest writes on
that family defer to later ticks instead of failing.

Keyspace: the two-int4 advisory form ``(FENCE_CLASS_ID, objid)``. It is
disjoint from the single-bigint ``hashtextextended`` advisory keys the parser
QC lock and the interp-weight lock use, because PostgreSQL records the two
forms under different ``objsubid`` values.
"""

from __future__ import annotations

import math
import time
import zlib
from collections.abc import Mapping, Sequence
from typing import Any

from packages.common.node27_timeseries_discovery import CANONICAL_HYPERTABLES, LEGACY_HYPERTABLES

# Fixed classid of the fence keyspace (#2713). Changing it, or the objid
# derivation below, silently splits a deployed fleet into two keyspaces that
# no longer exclude each other; both are pinned by literal in the tests.
FENCE_CLASS_ID = 2713
COMPRESSED_HYPERTABLES: tuple[str, ...] = tuple(
    f"{schema}.{name}" for schema, name in CANONICAL_HYPERTABLES + LEGACY_HYPERTABLES
)
# The two canonical family names, built from the discovery tuple rather than
# spelled as ``schema.name`` literals: the writers pass these to
# :func:`try_ingest_fence`, and the river/forcing text censuses count qualified
# spellings as SQL statements, which a fence argument is not.
_CANONICAL_BY_NAME = {name: f"{schema}.{name}" for schema, name in CANONICAL_HYPERTABLES}
RIVER_TIMESERIES_HYPERTABLE = _CANONICAL_BY_NAME["river_timeseries"]
FORCING_STATION_TIMESERIES_HYPERTABLE = _CANONICAL_BY_NAME["forcing_station_timeseries"]
# Each member of a family -> the canonical ``schema.name`` its key derives from.
_FAMILY_OF: dict[str, str] = {
    **{f"{schema}.{name}": f"{schema}.{name}" for schema, name in CANONICAL_HYPERTABLES},
    **{
        f"{legacy_schema}.{legacy_name}": f"{schema}.{name}"
        for (schema, name), (legacy_schema, legacy_name) in zip(
            CANONICAL_HYPERTABLES, LEGACY_HYPERTABLES, strict=True
        )
    },
}
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"

_TRY_INGEST_FENCE_SQL = "SELECT pg_try_advisory_xact_lock_shared(%s, %s)"
_ACQUIRE_FENCE_SQL = "SELECT pg_advisory_lock(%s, %s)"
_RELEASE_FENCE_SQL = "SELECT pg_advisory_unlock(%s, %s)"

# Injectable clock: tests replace this module attribute, never ``time`` itself.
_monotonic = time.monotonic


class IngestFenceBusy(RuntimeError):
    """An ingest writer could not take the shared fence; roll back and retry later.

    Not a failure: chunk DDL on the same hypertable is queued for or holds the
    fence. Callers map it to their own non-failing reason code.
    """

    def __init__(self, hypertable: str) -> None:
        self.hypertable = hypertable
        super().__init__(
            f"compression fence on {hypertable} is held or queued by chunk DDL; "
            "the write was rolled back and will be retried by a later tick"
        )


class FenceContended(RuntimeError):
    """Chunk DDL could not take the exclusive fence within its bounded wait.

    ``pgcode`` carries ``55P03`` (lock_not_available), the SQLSTATE of the
    ``lock_timeout`` that ended the wait, so SQLSTATE-keyed classifiers (the
    retention #1664 lock-contention segment) read it like the driver error.
    """

    pgcode = LOCK_NOT_AVAILABLE_SQLSTATE

    def __init__(self, hypertable: str, elapsed_ms: int) -> None:
        self.hypertable = hypertable
        self.elapsed_ms = elapsed_ms
        super().__init__(
            f"compression fence on {hypertable} not acquired within the bounded wait "
            f"({elapsed_ms} ms): ingest writers still hold it"
        )


def fence_key(hypertable: str) -> tuple[int, int]:
    """``(classid, objid)`` of the family of one compressed hypertable ``schema.name``.

    A ``_legacy`` sibling maps to its canonical hypertable (module docstring),
    and the objid is the CRC-32 of that canonical name folded into the signed
    int4 range. Only the closed lifecycle set is accepted: a typo would
    otherwise fence a key no other lane ever takes, which is the same as no
    fence at all.
    """

    if hypertable not in COMPRESSED_HYPERTABLES:
        raise ValueError(f"not a compressed lifecycle hypertable: {hypertable!r}")
    family = _FAMILY_OF[hypertable]
    digest = zlib.crc32(family.encode("utf-8"))
    objid = digest - 2**32 if digest >= 2**31 else digest
    return FENCE_CLASS_ID, objid


def _single_value(row: Any) -> Any:
    if isinstance(row, Mapping) and len(row) == 1:
        return next(iter(row.values()))
    if isinstance(row, Sequence) and not isinstance(row, str | bytes) and len(row) == 1:
        return row[0]
    raise RuntimeError(f"unexpected compression fence result row: {row!r}")


def try_ingest_fence(cursor: Any, hypertable: str) -> bool:
    """Try the shared, transaction-scoped ingest fence without blocking.

    Must be the first statement of the writer's transaction (module
    docstring). Fails closed on an unexpected result shape rather than
    treating it as busy or free.
    """

    cursor.execute(_TRY_INGEST_FENCE_SQL, fence_key(hypertable))
    value = _single_value(cursor.fetchone())
    if not isinstance(value, bool):
        raise RuntimeError(f"unexpected compression fence result: {value!r}")
    return value


def _elapsed_ms(started: float) -> int:
    return max(0, math.ceil((_monotonic() - started) * 1000))


def acquire_compression_fence(cursor: Any, hypertable: str, wait_ms: int) -> int:
    """Take the exclusive, session-scoped fence, waiting at most ``wait_ms``.

    Returns the client-measured wait in milliseconds. The caller must hold no
    other lock (call it first in a fresh session). ``lock_timeout`` bounds the
    advisory wait and is reset afterwards, so it does not leak into the DDL
    statement that follows. ``55P03`` raises :class:`FenceContended`.

    A session-level advisory lock survives COMMIT and ROLLBACK: the caller
    releases it with :func:`release_compression_fence` after rolling back,
    and closing the connection releases it too.
    """

    if isinstance(wait_ms, bool) or not isinstance(wait_ms, int) or wait_ms < 1:
        # lock_timeout = 0 means "wait forever"; never let that through.
        raise ValueError(f"compression fence wait must be a positive integer of ms, got {wait_ms!r}")
    classid, objid = fence_key(hypertable)
    cursor.execute(f"SET lock_timeout = {wait_ms}")
    started = _monotonic()
    try:
        cursor.execute(_ACQUIRE_FENCE_SQL, (classid, objid))
    except Exception as error:
        if getattr(error, "pgcode", None) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise FenceContended(hypertable, _elapsed_ms(started)) from error
        raise
    elapsed_ms = _elapsed_ms(started)
    cursor.execute("RESET lock_timeout")
    return elapsed_ms


def release_compression_fence(cursor: Any, hypertable: str) -> None:
    """Release the exclusive fence taken by :func:`acquire_compression_fence`."""

    cursor.execute(_RELEASE_FENCE_SQL, fence_key(hypertable))
