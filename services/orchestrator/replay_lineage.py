"""SUB-8 replay lineage resolver — provenance-pinned policy contract.

Epic #982 SUB-8 (``mapping-variant-state-compatibility`` task 5.1):
reads ``t*`` from the ``ops.audit_log`` activation-class record plus the
``hydro.state_snapshot`` clone rows' ``valid_time``; classifies a
requested replay interval as ``entirely_pre_cutover`` /
``entirely_post_cutover`` / ``straddling_cutover`` /
``no_cutover_history``; produces lineage-pinned sub-intervals with NO
cross-variant splicing.

This is a **non-activation policy resolver**. Zero lifecycle calls,
zero writes. Change 4's activation-scoped legacy-reactivation guard
(``_fetch_direct_grid_activation_history``) fires only inside
``model_lifecycle_operation`` on ``activate`` / ``switch_version`` /
``rollback_version`` — the resolver makes no such calls, so the guard
cannot mis-fire on offline replay. An actual ``activate(M0)`` request
for the same basin is still refused by the guard (see the SUB-9 guard
implementation).

Design rationale
----------------
Two orthogonal authorities determine the cutover:

* ``ops.audit_log`` identifies WHICH cutover to consider — the latest
  successful activation-class record for the ``basin_version_id`` whose
  resulting-active model classifies as direct-grid. This yields the
  ``M1`` identity plus the ``M0`` identity (from
  ``details -> 'previous_model' ->> 'model_id'``).
* ``hydro.state_snapshot`` clone rows (``clone_gate_fingerprint IS NOT
  NULL``) carry the cutover ``t*`` by construction (SUB-4 hook writes
  them at ``t*``). ``t*`` = the single shared ``valid_time`` across all
  clone rows for ``M1``.

The spec invariant is that all clone rows for a single ``M1`` share the
same ``valid_time`` (one ``t*`` per cutover). If clone rows disagree,
:class:`ReplayLineageAmbiguityError` is raised — the resolver never
guesses.

The alternative "read ``t*`` from data availability" (e.g. take
``MAX(valid_time)`` from ``hydro.state_snapshot`` for the basin) is
explicitly REJECTED: data availability is not the audit authority, and
downstream forecast rows can lie above ``t*`` without affecting the
cutover identity.

Contract for consumers
----------------------
The returned :class:`ReplayLineagePlan` is a lineage-pinned execution
plan. Straddle intervals split into TWO sub-intervals (``[start, t*)``
with ``M0`` and ``[t*, end]`` with ``M1``); consumers execute each
sub-interval per-variant separately and MUST NOT splice cross-variant
series. The offline replay executor that consumes this plan is owned by
Change 6 / Change 7 and is explicitly out of scope here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

__all__ = [
    "PsycopgReplayLineageAuditReader",
    "PsycopgReplayLineageCloneReader",
    "ReplayLineageAmbiguityError",
    "ReplayLineageAuditReader",
    "ReplayLineageCloneReader",
    "ReplayLineageError",
    "ReplayLineagePlan",
    "ReplayLineageSubInterval",
    "resolve_replay_lineage",
]


ReplayLineageClassification = Literal[
    "no_cutover_history",
    "entirely_pre_cutover",
    "entirely_post_cutover",
    "straddling_cutover",
]

ReplayLineageTag = Literal["M0", "M1", "legacy"]


@dataclass(frozen=True)
class ReplayLineageSubInterval:
    """One lineage-pinned sub-interval in a :class:`ReplayLineagePlan`.

    ``model_id`` is the pinned model identity for this sub-interval.
    For the ``no_cutover_history`` case there is no audit-identified
    model, so ``model_id`` is the empty string and ``lineage`` is
    ``"legacy"``; consumers key on ``lineage`` in that case.
    """

    start: datetime
    end: datetime
    model_id: str
    lineage: ReplayLineageTag


@dataclass(frozen=True)
class ReplayLineagePlan:
    """A lineage-pinned execution plan for a replay interval.

    The plan is data — it holds no execution semantics beyond the
    splicing-forbidden invariant on ``sub_intervals``: straddle plans
    carry two sub-intervals split at ``t*`` and consumers execute each
    per-variant separately without splicing.
    """

    basin_version_id: str
    interval_start: datetime
    interval_end: datetime
    classification: ReplayLineageClassification
    cutover_valid_time: datetime | None
    m0_model_id: str | None
    m1_model_id: str | None
    sub_intervals: tuple[ReplayLineageSubInterval, ...]


class ReplayLineageError(Exception):
    """Base for replay-lineage resolver errors."""


class ReplayLineageAmbiguityError(ReplayLineageError):
    """Clone rows for the direct-grid ``M1`` disagree on ``valid_time``.

    Raised when the resolver finds multiple distinct ``valid_time``
    values across the ``M1`` clone rows (``clone_gate_fingerprint IS
    NOT NULL``). The spec invariant is one ``t*`` per cutover; a
    violation means either the DB was mutated outside the atomic-cutover
    transaction or an unexpected multi-cutover state exists. The
    resolver refuses to guess and raises for the caller to investigate.
    """


class ReplayLineageAuditReader(Protocol):
    """Read side of ``ops.audit_log`` for the resolver.

    Production impl (:class:`PsycopgReplayLineageAuditReader`) reads
    from PostgreSQL. Tests inject in-memory fakes.
    """

    def get_latest_direct_grid_activation(
        self, basin_version_id: str
    ) -> Mapping[str, Any] | None:
        """Return the LATEST direct-grid activation for a basin, or None.

        Returns a mapping with keys ``m1_model_id`` (resulting-active
        model that classifies as direct-grid), ``m0_model_id``
        (``details -> 'previous_model' ->> 'model_id'``, may be
        ``None`` on a fresh-basin activation), and ``audit_log_id`` (the
        ``log_id`` of the source audit row).

        Returns ``None`` when the basin has no direct-grid activation
        history.
        """
        ...


class ReplayLineageCloneReader(Protocol):
    """Read side of ``hydro.state_snapshot`` clone rows for the resolver.

    Production impl (:class:`PsycopgReplayLineageCloneReader`) reads
    from PostgreSQL. Tests inject in-memory fakes.
    """

    def get_cutover_valid_time_for_model(
        self, model_id: str
    ) -> datetime | None:
        """Return the single shared ``valid_time`` across all clone rows for ``model_id``.

        Filters ``hydro.state_snapshot`` by
        ``model_id = model_id AND clone_gate_fingerprint IS NOT NULL``.

        Returns ``None`` when no clone rows exist for ``model_id``.
        Raises :class:`ReplayLineageAmbiguityError` when clone rows
        exist but carry disagreeing ``valid_time`` values.
        """
        ...


def resolve_replay_lineage(
    *,
    basin_version_id: str,
    interval_start: datetime,
    interval_end: datetime,
    audit_reader: ReplayLineageAuditReader,
    clone_reader: ReplayLineageCloneReader,
) -> ReplayLineagePlan:
    """Resolve a lineage-pinned plan for a replay interval.

    Contract:

    * Zero lifecycle calls — the resolver reads ``ops.audit_log`` and
      ``hydro.state_snapshot`` only. Change 4's legacy-reactivation
      guard is activation-scoped and does not fire here.
    * ``t*`` comes from the audit-authority-identified ``M1``'s clone
      rows, cross-checked for single-``valid_time`` invariance across
      sources; a disagreement raises
      :class:`ReplayLineageAmbiguityError`.
    * Straddle intervals produce TWO sub-intervals — ``[start, t*)``
      with ``M0`` and ``[t*, end]`` with ``M1`` — with NO cross-variant
      splicing.
    """
    if interval_end < interval_start:
        raise ValueError(
            "interval_end must be >= interval_start "
            f"(got start={interval_start!r}, end={interval_end!r})"
        )

    start = _ensure_utc(interval_start)
    end = _ensure_utc(interval_end)

    audit_record = audit_reader.get_latest_direct_grid_activation(basin_version_id)
    if audit_record is None:
        # No direct-grid activation history: legacy lane end-to-end.
        return ReplayLineagePlan(
            basin_version_id=basin_version_id,
            interval_start=start,
            interval_end=end,
            classification="no_cutover_history",
            cutover_valid_time=None,
            m0_model_id=None,
            m1_model_id=None,
            sub_intervals=(
                ReplayLineageSubInterval(
                    start=start,
                    end=end,
                    model_id="",
                    lineage="legacy",
                ),
            ),
        )

    m1_model_id_raw = audit_record.get("m1_model_id")
    if not m1_model_id_raw:
        raise ReplayLineageError(
            "audit reader returned a direct-grid activation with no m1_model_id: "
            f"{audit_record!r}"
        )
    m1_model_id = str(m1_model_id_raw)
    m0_model_id_raw = audit_record.get("m0_model_id")
    m0_model_id = str(m0_model_id_raw) if m0_model_id_raw else None

    cutover_valid_time = clone_reader.get_cutover_valid_time_for_model(m1_model_id)
    if cutover_valid_time is None:
        # Audit says direct-grid was activated but no clone rows exist.
        # This should not happen under the SUB-4 hook (clone rows are
        # written in the same transaction as activation). Raise so the
        # caller can distinguish "no cutover" from "cutover audit exists
        # but state was mutated out-of-band".
        raise ReplayLineageError(
            "audit reader identified direct-grid activation but no clone "
            f"rows exist for m1_model_id={m1_model_id!r} in "
            f"basin_version_id={basin_version_id!r}"
        )
    cutover_valid_time = _ensure_utc(cutover_valid_time)

    if end <= cutover_valid_time:
        classification: ReplayLineageClassification = "entirely_pre_cutover"
        sub_intervals: tuple[ReplayLineageSubInterval, ...] = (
            ReplayLineageSubInterval(
                start=start,
                end=end,
                model_id=m0_model_id or "",
                lineage="M0",
            ),
        )
    elif start >= cutover_valid_time:
        classification = "entirely_post_cutover"
        sub_intervals = (
            ReplayLineageSubInterval(
                start=start,
                end=end,
                model_id=m1_model_id,
                lineage="M1",
            ),
        )
    else:
        # start < cutover_valid_time < end
        classification = "straddling_cutover"
        sub_intervals = (
            ReplayLineageSubInterval(
                start=start,
                end=cutover_valid_time,
                model_id=m0_model_id or "",
                lineage="M0",
            ),
            ReplayLineageSubInterval(
                start=cutover_valid_time,
                end=end,
                model_id=m1_model_id,
                lineage="M1",
            ),
        )

    return ReplayLineagePlan(
        basin_version_id=basin_version_id,
        interval_start=start,
        interval_end=end,
        classification=classification,
        cutover_valid_time=cutover_valid_time,
        m0_model_id=m0_model_id,
        m1_model_id=m1_model_id,
        sub_intervals=sub_intervals,
    )


# ---------------------------------------------------------------------------
# Production Psycopg readers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PsycopgReplayLineageAuditReader:
    """Production ``ops.audit_log`` reader for the replay lineage resolver.

    Filters ``ops.audit_log`` for the LATEST successful activation-class
    record on the given ``basin_version_id`` whose resulting-active
    model classifies as direct-grid under Change 4's single classifier
    (:func:`packages.common.model_registry._classify_forcing_mapping_mode`).
    The audit-authority identity plus the ``details ->
    'previous_model' ->> 'model_id'`` extract yields ``(m1_model_id,
    m0_model_id, audit_log_id)``.

    Ordering: ``ORDER BY al.created_at DESC, al.log_id DESC`` — the
    resolver wants the LATEST cutover (in the M1->M1' fix-forward path
    the LATEST direct-grid activation is the currently-active ``M1'``;
    for a single-cutover basin LATEST equals EARLIEST).
    """

    database_url: str

    @classmethod
    def from_env(cls) -> PsycopgReplayLineageAuditReader:
        from packages.common.state_manager import default_database_url

        return cls(default_database_url())

    def get_latest_direct_grid_activation(
        self, basin_version_id: str
    ) -> Mapping[str, Any] | None:
        # Delayed import: keep the module import-safe on environments
        # without psycopg2 (e.g. the resolver's dataclass consumers).
        from packages.common.model_registry import _classify_forcing_mapping_mode

        rows = self._fetch_all(
            """
            SELECT
                al.log_id,
                al.details -> 'updated_model' ->> 'model_id' AS updated_model_id,
                al.details -> 'previous_model' ->> 'model_id' AS previous_model_id,
                mi.resource_profile AS updated_resource_profile
            FROM ops.audit_log al
            JOIN core.model_instance mi
              ON mi.model_id = (al.details -> 'updated_model' ->> 'model_id')
            WHERE al.entity_type = 'model_instance'
              AND al.action IN (
                'models.activate',
                'models.switch_version',
                'models.rollback_version'
              )
              AND al.details ->> 'outcome' IN ('allowed', 'rollback')
              AND al.details ->> 'basin_version_id' = %s
            ORDER BY al.created_at DESC, al.log_id DESC
            """,
            (basin_version_id,),
        )
        for row in rows:
            classification = _classify_forcing_mapping_mode(
                {"resource_profile": row.get("updated_resource_profile")}
            )
            if classification == "direct_grid":
                return {
                    "m1_model_id": row.get("updated_model_id"),
                    "m0_model_id": row.get("previous_model_id"),
                    "audit_log_id": row.get("log_id"),
                }
        return None

    def _fetch_all(
        self, statement: str, parameters: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        try:
            import psycopg2
            from psycopg2.extras import (
                RealDictCursor,
                register_default_json,
                register_default_jsonb,
            )
        except ImportError as error:  # pragma: no cover - environmental
            raise ReplayLineageError(
                "psycopg2 is required for PsycopgReplayLineageAuditReader"
            ) from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = True
            register_default_json(conn_or_curs=connection)
            register_default_jsonb(conn_or_curs=connection)
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(statement, parameters)
                return [dict(row) for row in cursor.fetchall()]
        finally:
            if connection is not None:
                connection.close()


@dataclass(frozen=True)
class PsycopgReplayLineageCloneReader:
    """Production ``hydro.state_snapshot`` clone-row reader.

    Reads the DISTINCT ``valid_time`` values across all clone rows
    (``clone_gate_fingerprint IS NOT NULL``) for a given ``model_id``.
    Returns the single shared ``valid_time`` when the invariant holds,
    ``None`` when no clone rows exist, or raises
    :class:`ReplayLineageAmbiguityError` when clone rows carry
    disagreeing ``valid_time`` values.

    The ``clone_gate_fingerprint IS NOT NULL`` filter isolates rows
    written by the SUB-4 state-clone hook from SHUD
    forecast / save-state rows (which never populate
    ``clone_gate_fingerprint``), so post-cutover forecast rows at
    higher ``valid_time`` values cannot spuriously widen the DISTINCT
    set.
    """

    database_url: str

    @classmethod
    def from_env(cls) -> PsycopgReplayLineageCloneReader:
        from packages.common.state_manager import default_database_url

        return cls(default_database_url())

    def get_cutover_valid_time_for_model(
        self, model_id: str
    ) -> datetime | None:
        rows = self._fetch_all(
            """
            SELECT DISTINCT valid_time
            FROM hydro.state_snapshot
            WHERE model_id = %s
              AND clone_gate_fingerprint IS NOT NULL
            """,
            (model_id,),
        )
        if not rows:
            return None
        distinct_valid_times = {row.get("valid_time") for row in rows}
        if len(distinct_valid_times) > 1:
            raise ReplayLineageAmbiguityError(
                "clone rows for model_id="
                f"{model_id!r} disagree on valid_time: "
                f"{sorted(str(vt) for vt in distinct_valid_times)}"
            )
        (only_valid_time,) = distinct_valid_times
        if only_valid_time is None:  # pragma: no cover - schema guard
            raise ReplayLineageError(
                f"clone row for model_id={model_id!r} has NULL valid_time"
            )
        return _ensure_utc(only_valid_time)

    def _fetch_all(
        self, statement: str, parameters: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
        except ImportError as error:  # pragma: no cover - environmental
            raise ReplayLineageError(
                "psycopg2 is required for PsycopgReplayLineageCloneReader"
            ) from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = True
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(statement, parameters)
                return [dict(row) for row in cursor.fetchall()]
        finally:
            if connection is not None:
                connection.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as a UTC-aware datetime.

    Naive datetimes are treated as UTC; aware datetimes are converted
    to UTC. Mirrors
    :func:`packages.common.state_manager._ensure_utc` locally so the
    resolver does not depend on the state manager's private helper.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
