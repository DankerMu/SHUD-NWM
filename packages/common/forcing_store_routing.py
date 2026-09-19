"""Per-version routing between the two forcing timeseries stores (#1991, task 7.3).

``db/migrations/000061_forcing_station_timeseries_narrow_expand.sql`` renames
``met.forcing_station_timeseries`` to ``…_legacy``, creates a narrow key/enum
table under the canonical name, and adds
``met.forcing_version.timeseries_store`` (``NOT NULL DEFAULT 'narrow'``,
``CHECK (timeseries_store IN ('legacy','narrow'))``). From that migration on,
ONE forcing version's rows live in exactly ONE of the two tables, and both
tables are live until task 8.2 drops the legacy one.

This module owns the three things every side of that transition needs and none
of them owns naturally:

* the two store names, spelled once;
* the SQL that answers "which store is this forcing version in, and what is its
  surrogate key" — one row, primary-key lookup on ``met.forcing_version``;
* the refusal vocabulary a WRITER uses when it is handed a ``legacy`` version.

It deliberately holds no SQL naming the fact table, so it stays outside the
forcing template census's discovery set (``tests/forcing_ts_template_registry``
counts schema-qualified mentions of ``met.forcing_station_timeseries``) and
outside ``packages/common/forcing_ts_render``'s concerns, which are about SQL
TEXT and not about the database.

Why the refusal is permanent without a decline row
--------------------------------------------------

River's equivalent refusal records an ``ops.ingest_recompute_decline`` row
because a river recompute is reopened by a newer ``product_mtime``, so the
refusal needs somewhere to be remembered. Forcing's refusal is keyed on
``met.forcing_version.timeseries_store``, which 000061 sets once and which no
writer ever flips back — re-reading it on every attempt is idempotent and
permanent by construction. ``ops.ingest_recompute_decline`` has zero forcing-side
uses and gains none here (fixture ``I12-1991.md`` R2.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from packages.common.met_store import MetStoreError

#: A forcing version whose rows are in ``met.forcing_station_timeseries_legacy``.
#: Readable, never writable: 000061 classified it and the writers refuse it.
FORCING_STORE_LEGACY = "legacy"

#: A forcing version whose rows are in the narrow ``met.forcing_station_timeseries``.
FORCING_STORE_NARROW = "narrow"

#: The column's own default. A version row inserted after 000061 is ``narrow``
#: without anybody saying so, which is what makes "the writers write only the
#: narrow table" true for every new version with no extra write.
FORCING_STORE_DEFAULT = FORCING_STORE_NARROW

#: One row, primary-key lookup. Both projected columns are wanted together by
#: the writers — the store decides whether to refuse, the key is what the narrow
#: statements predicate on — so asking for them separately would be two lookups
#: for one fact.
FORCING_VERSION_ROUTING_SQL = """
SELECT forcing_version_key, timeseries_store
FROM met.forcing_version
WHERE forcing_version_id = %s
"""

#: The store alone, for READERS. They never need the surrogate key: their narrow
#: templates join ``met.forcing_version`` for it.
FORCING_VERSION_STORE_SQL = """
SELECT timeseries_store
FROM met.forcing_version
WHERE forcing_version_id = %s
"""

#: The distinct, permanent code a writer reports when it is handed a ``legacy``
#: forcing version (spec ``forcing-narrow-store`` :21). It is NOT
#: ``FORCING_COMPRESSED_CHUNK_BLOCKED`` (a compressed chunk was detected; an
#: operator decompresses and the write becomes possible) and NOT
#: ``FORCING_COMPRESSED_CHUNK_GUARD_FAILED`` (the guard could not certify the
#: batch; often transient). This one has no remedy at all: the version's rows are
#: in the other table and stay there until 8.2 drops it.
LEGACY_STORE_REFUSED_CODE = "FORCING_LEGACY_STORE_REFUSED"

#: The narrow fact table's column list, in the order both writers bind it.
#: Spelled once because the two writers must not drift: they write the same rows
#: from the same producer, one on node-22's own database and one across the
#: node-22 -> node-27 handoff.
FORCING_NARROW_INSERT_COLUMNS: tuple[str, ...] = (
    "forcing_version_key",
    "station_key",
    "valid_time",
    "variable_e",
    "value",
    "unit_e",
    "quality_flag_e",
    "native_resolution",
)

#: ``execute_values`` per-row template for that INSERT. Spelled out rather than
#: left to psycopg2's default ``(%s, %s, …)`` because the three enum columns need
#: an explicit cast: psycopg2 sends a Python ``str`` as ``text`` and Postgres has
#: no implicit ``text -> enum`` coercion in an INSERT source list. The casts
#: double as the vocabulary gate — a variable/unit/quality_flag literal outside
#: ``000061_forcing_station_timeseries_narrow_expand.sql``'s enums fails the whole
#: batch here rather than landing as free text nobody ever notices.
FORCING_NARROW_INSERT_TEMPLATE = (
    "(%s, %s, %s, %s::met.forcing_variable, %s, %s::met.forcing_unit, %s::met.forcing_quality_flag, %s)"
)


class LegacyForcingStoreRefusedError(MetStoreError):
    """A replace targeting a forcing version routed to the LEGACY store (#1991).

    Raised BEFORE any DELETE (must-preserve M3): both forcing write paths open a
    DELETE-then-INSERT replace window, so a refusal raised after it opens would
    destroy the legacy rows and only then decline to replace them.

    It lives HERE and not in ``workers/forcing_producer/store.py``, where it is
    raised, because ``store.py`` imports from
    ``workers/forcing_producer/producer.py`` and ``producer.py`` has to catch
    this class — the other direction is an import cycle.

    Distinct from ``CompressedChunkWriteError`` (a compressed chunk was detected;
    an operator decompresses and the write becomes possible) and from
    ``CompressedChunkGuardError`` (the guard could not certify the batch; often
    transient). This refusal has no remedy: the version's rows live in
    ``met.forcing_station_timeseries_legacy`` and stay there until task 8.2 drops
    that table.
    """

    error_code = LEGACY_STORE_REFUSED_CODE


def legacy_store_refusal_message(forcing_version_id: str) -> str:
    """The one sentence every refusing writer reports, spelled once."""
    return (
        f"{LEGACY_STORE_REFUSED_CODE}: forcing version {forcing_version_id!r} is routed to the legacy "
        "timeseries store (met.forcing_version.timeseries_store = 'legacy'), which the narrow-only "
        "writers must not touch. No DELETE was issued. The refusal is permanent for this version."
    )


def row_value(row: Any, key: str, index: int) -> Any:
    """One field out of a cursor row, whatever cursor factory produced it.

    The forcing write and read paths do not agree on a cursor factory --
    ``workers/forcing_producer/store.py`` opens a plain tuple cursor,
    ``packages/common/forecast_store.py`` a ``RealDictCursor`` -- and this
    module is called from both. Reading ``row[0]`` off a dict row silently
    raises ``KeyError: 0``; reading ``row["x"]`` off a tuple raises
    ``TypeError``. Neither diagnostic names the cursor.
    """
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index]


def forcing_store_for_version(cursor: Any, forcing_version_id: str) -> str | None:
    """The store of ``forcing_version_id``, or ``None`` when no such version row exists.

    ``None`` rather than a default: a reader that cannot find the version has
    nothing to render and its own 404 to raise, and guessing a store here would
    turn "no such forcing version" into "zero rows", which reads as "no data".
    """
    cursor.execute(FORCING_VERSION_STORE_SQL, (forcing_version_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    store = row_value(row, "timeseries_store", 0)
    return None if store is None else str(store)


def forcing_store_or_default(store: Any) -> str:
    """A store name that may be ``None``, resolved to a renderable one.

    ``None`` reaches here from an outer-joined ``met.forcing_version`` row, where
    it means "this candidate has no forcing version at all" — a case the reader
    will find empty on either table. Resolving it to
    :data:`FORCING_STORE_DEFAULT` rather than to ``legacy`` points the render at
    the table that survives task 8.2.
    """
    return str(store) if store else FORCING_STORE_DEFAULT


def forcing_version_store(forcing_version: Mapping[str, Any]) -> str:
    """The store carried on an already-fetched ``met.forcing_version`` row.

    Falls back to :data:`FORCING_STORE_DEFAULT` when the mapping does not carry
    the column, which after 000061 can only be a row this repository's tests
    built by hand: the column is ``NOT NULL`` in the deployed schema, so a
    production row always has it. Defaulting to ``narrow`` rather than
    ``legacy`` keeps the fallback pointing at the table that is not going to be
    dropped.
    """
    store = forcing_version.get("timeseries_store")
    return str(store) if store else FORCING_STORE_DEFAULT
