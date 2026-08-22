"""State-lineage cutover resolution for cycle scoping (#1735).

A recalibration rollout mints a NEW content-derived ``model_id`` for a basin
whose state carried over from its predecessor (ADR 0005).  Every completeness
predicate in the scheduler is keyed strictly by ``model_id``, so the new
identity has zero pipeline history before its own cutover instant ``t*`` and
every in-window cycle flips ``complete`` → ``gap``.  Those gaps are unclosable
— closing one would require the new identity to have completed a cycle that
predates its own existence — and the backfill lane pins itself on the earliest
of them, starving the forward lane.

This module answers the one question that fixes it: **when did this
``model_id`` come into existence for this source?**  A model whose answer is
``t*`` is not scored, not admitted into the cohort, and not emitted as a
backfill predecessor for any cycle strictly earlier than ``t*``.

Boundaries (change ``lineage-scoped-cycle-completion``):

- Resolution is per ``(model_id, source_id)``: clone rows are written per
  source and GFS / IFS may cut over at different instants (D3).
- ``t*`` is the model's OWN cutover — the EARLIEST clone row under its own
  ``model_id``.  There is **no ancestry walk**: for ``M → M'`` at ``t1`` and
  ``M' → M''`` at ``t2``, ``M''``'s boundary is ``t2``, never ``t1``.  Cycles
  in ``[t1, t2)`` were run by ``M'``; leaving ``M''`` in scope for them would
  reproduce the very deadlock this module exists to prevent (D4).  Nothing
  recurses, so there is no cycle guard to write.
- The comparison is STRICT: scoped out iff ``cycle_time < t*``.  ``cycle_time
  == t*`` is the model's own first cycle, warm-started from the clone row, and
  is scored and admitted exactly as any other cycle.
- Absent provenance — and an unreadable index or an absent provider — mean
  "no lineage", never an error.  A model with no lineage keeps today's
  behavior byte-for-byte.
- A clone row naming ITSELF as its predecessor (``cloned_from_model_id ==
  model_id``) is corrupt provenance, not an existence-start, and confers no
  lineage.  Both planes reject it at the reader; this module rejects it again
  at the boundary so a provider that has not been tightened (a stub, a stale
  deployment, a future reader) cannot mint a ``t*`` out of nothing.  Honoring
  such a row would scope every earlier cycle out on the strength of a row that
  proves nothing — the silent direction, since the gap simply disappears.

The resolution result is a scoping input; the ``lineage_scoped_out_pre_cutover``
record it produces is an ANNOTATION and is never read back as a decision input.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "LINEAGE_SCOPED_OUT_REASON",
    "LineageCutover",
    "is_pre_cutover",
    "lineage_scoped_out_record",
    "resolve_lineage_cutover",
]

LINEAGE_SCOPED_OUT_REASON = "lineage_scoped_out_pre_cutover"


@dataclass(frozen=True)
class LineageCutover:
    """A model's own existence-start for one source.

    ``cutover_time`` is ``t*`` — the ``valid_time`` of the earliest clone row
    written under ``model_id`` for ``source_id``.  ``predecessor_model_id`` is
    that row's ``cloned_from_model_id``: the identity that ran the cycles
    before ``t*``.
    """

    model_id: str
    source_id: str
    predecessor_model_id: str
    cutover_time: datetime
    clone_gate_kind: str | None = None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if not value:
        return None
    try:
        return _ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _from_index_signal(
    signal: Mapping[str, Any],
    *,
    model_id: str,
    source_id: str,
) -> LineageCutover | None:
    if not bool(signal.get("has_lineage")):
        return None
    predecessor_model_id = str(signal.get("predecessor_model_id") or "").strip()
    cutover_time = _parse_utc(signal.get("cutover_valid_time"))
    if not predecessor_model_id or cutover_time is None:
        return None
    if predecessor_model_id == str(model_id):
        return None
    clone_gate_kind = signal.get("clone_gate_kind")
    return LineageCutover(
        model_id=str(model_id),
        source_id=str(signal.get("source_id") or source_id),
        predecessor_model_id=predecessor_model_id,
        cutover_time=cutover_time,
        clone_gate_kind=str(clone_gate_kind) if clone_gate_kind not in (None, "") else None,
    )


def _from_clone_row(row: Any, *, model_id: str, source_id: str) -> LineageCutover | None:
    predecessor_model_id = str(getattr(row, "cloned_from_model_id", "") or "").strip()
    cutover_time = _parse_utc(getattr(row, "valid_time", None))
    if not predecessor_model_id or cutover_time is None:
        return None
    if predecessor_model_id == str(model_id):
        return None
    clone_gate_kind = getattr(row, "clone_gate_kind", None)
    return LineageCutover(
        model_id=str(model_id),
        source_id=str(getattr(row, "source_id", "") or source_id),
        predecessor_model_id=predecessor_model_id,
        cutover_time=cutover_time,
        clone_gate_kind=str(clone_gate_kind) if clone_gate_kind not in (None, "") else None,
    )


def resolve_lineage_cutover(
    provider: Any | None,
    *,
    model_id: str,
    source_id: str,
) -> LineageCutover | None:
    """Resolve ``(model_id, source_id)`` to its own cutover, or ``None``.

    Duck-typed across the two persistence planes so neither the caller nor the
    filter is plane-aware:

    - **db-free**: ``clone_lineage_signal`` on the file state-snapshot index
      repository, which reads the already-loaded (cached) index snapshot and
      issues no additional read.
    - **DB**: ``get_earliest_clone_row_for_model_source`` — the earliest-row
      read, NOT the publisher's ``DESC`` reader (D3/D4).

    A provider that is ``None``, exposes neither accessor, or fails, yields
    ``None`` — "no lineage".  That errs toward keeping the model in scope,
    which can leave a loud stuck gap but can never silently hide completed
    work.
    """

    if provider is None or not str(model_id or "").strip() or not str(source_id or "").strip():
        return None
    index_signal = getattr(provider, "clone_lineage_signal", None)
    if callable(index_signal):
        try:
            signal = index_signal(model_id=model_id, source_id=source_id)
        except Exception:  # noqa: BLE001 — absent/unreadable provenance is "no lineage"
            return None
        if isinstance(signal, Mapping):
            return _from_index_signal(signal, model_id=model_id, source_id=source_id)
        return None
    earliest_clone_row = getattr(provider, "get_earliest_clone_row_for_model_source", None)
    if callable(earliest_clone_row):
        try:
            row = earliest_clone_row(model_id=model_id, source_id=source_id)
        except Exception:  # noqa: BLE001 — absent/unreadable provenance is "no lineage"
            return None
        if row is None:
            return None
        return _from_clone_row(row, model_id=model_id, source_id=source_id)
    return None


def is_pre_cutover(cutover: LineageCutover | None, cycle_time: datetime | None) -> bool:
    """Whether ``cycle_time`` predates the model's own existence.

    STRICT: ``cycle_time == t*`` is the model's first cycle and is NOT scoped
    out.  ``None`` lineage (or a missing cycle time) is never scoped out.
    """

    if cutover is None or cycle_time is None:
        return False
    return _ensure_utc(cycle_time) < cutover.cutover_time


def lineage_scoped_out_record(
    cutover: LineageCutover,
    *,
    cycle_time: datetime,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    """The ``lineage_scoped_out_pre_cutover`` evidence annotation.

    Names the excluded model, its predecessor, and the resolved ``t*`` so an
    operator can tell "scoped out because it did not exist yet" from "every
    model genuinely completed" without re-deriving lineage.  Annotation only —
    never read back as a completion, admission, or selection input.
    """

    record: dict[str, Any] = {
        "reason": LINEAGE_SCOPED_OUT_REASON,
        "model_id": cutover.model_id,
        "source_id": cutover.source_id,
        "predecessor_model_id": cutover.predecessor_model_id,
        "cutover_valid_time": cutover.cutover_time.isoformat().replace("+00:00", "Z"),
        "cycle_time_utc": _ensure_utc(cycle_time).isoformat().replace("+00:00", "Z"),
    }
    if cutover.clone_gate_kind is not None:
        record["clone_gate_kind"] = cutover.clone_gate_kind
    if cycle_id:
        record["cycle_id"] = str(cycle_id)
    return record
