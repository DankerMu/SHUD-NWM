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
- A resolution FAILURE is distinguished from a resolved "no lineage" (#1740).
  Absent provenance, an absent provider, and a state-snapshot index that has
  never been published mean "no lineage" — resolved answers, memoizable, and a
  model with no lineage keeps today's behavior byte-for-byte.  An index that
  EXISTS but cannot be read, parsed or validated, a plane read that raises, and
  a provider that breaks the resolution contract are failures and raise
  :class:`LineageResolutionError`.  Collapsing both into ``None`` is what let a
  single transient DB blip be memoized as a permanent "no lineage" for the rest
  of the process, silently reverting the pair to pre-#1735 semantics with no
  operator signal.  The caller still ends up treating a failure as "no lineage"
  for the CALL it is in — per call, NOT per pass.  Nothing is memoized, so a
  later call for the same pair inside the SAME pass may resolve, and one pass's
  evidence can disagree with itself: a model scored in scope by one consumer and
  annotated ``lineage_scoped_out_pre_cutover`` by another.  The disagreement is
  monotone (``None`` first, then the cutover — a SUCCESS is memoized the moment
  it happens and the cache is never cleared mid-pass) and it is always in the
  LOUD direction, because every consumer that sees ``None`` keeps the model in
  scope, which at worst leaves a visible stuck gap.  It self-heals on the next
  pass.  The point is that a failure must not be REMEMBERED, and must say so out
  loud.
- A clone row naming ITSELF as its predecessor (``cloned_from_model_id ==
  model_id``) is corrupt provenance, not an existence-start, and confers no
  lineage.  Both planes reject it at the reader; this module rejects it again
  at the boundary so a provider that has not been tightened (a stub, a stale
  deployment, a future reader) cannot mint a ``t*`` out of nothing.  Honoring
  such a row would scope every earlier cycle out on the strength of a row that
  proves nothing — the silent direction, since the gap simply disappears.

The resolution result is a scoping input; the ``lineage_scoped_out_pre_cutover``
record it produces is an ANNOTATION, and is not re-derived into a decision
anywhere.  One carrier is the exception to "never read back": the backfill
predecessor lane stows the record on its own pending entry
(``scheduler_backfill_predecessor.py``'s ``_extract_pending_predecessors``) and
``emit_predecessor_candidates`` branches on its presence to skip the prepend.
That is the SAME decision handed forward between two functions of one pass, not
a consumer turning an annotation into a new decision — but it does mean the
record IS read, so do not delete it on the strength of "nothing reads it".
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "LINEAGE_RESOLUTION_FAILED_LOG_TOKEN",
    "LINEAGE_SCOPED_OUT_REASON",
    "LineageCutover",
    "LineageResolutionError",
    "is_pre_cutover",
    "lineage_scoped_out_record",
    "log_lineage_resolution_failure",
    "resolve_lineage_cutover",
]

LOGGER = logging.getLogger(__name__)

LINEAGE_SCOPED_OUT_REASON = "lineage_scoped_out_pre_cutover"

#: Grep token for the one operator-visible signal this module emits.
LINEAGE_RESOLUTION_FAILED_LOG_TOKEN = "LINEAGE_RESOLUTION_FAILED"

#: The blocker reason a file state-snapshot index reports when the index has
#: never been published (``packages/common/state_manager.py``'s ``_read_payload``
#: raises it on ``FileNotFoundError`` / missing-object under
#: ``allow_empty=False``).  Spelled out rather than imported: this module is
#: deliberately plane-agnostic and imports no repository, so the two suites that
#: exercise it drive a REAL repository and would catch a drift in this literal.
_STATE_INDEX_NEVER_PUBLISHED_REASON = "state_snapshot_index_missing"


class LineageResolutionError(Exception):
    """Lineage for ``(model_id, source_id)`` could not be RESOLVED (#1740).

    Distinct from "resolved: this pair has no lineage", which stays a ``None``
    return.  The distinction exists because the two have opposite caching
    rules: "no lineage" is a stable answer and may be memoized, while a failure
    says nothing about the pair and must be re-attempted on the next pass.

    An exception rather than a richer return type on purpose: the caller can
    ignore a ``failed`` flag on a result object, and every one of the existing
    ``is None`` callsites would have kept compiling while reading a failure as
    "no lineage" — the exact lossy collapse this change removes.  Raising makes
    handling non-optional.
    """

    def __init__(self, *, model_id: str, source_id: str, reason: str) -> None:
        super().__init__(f"lineage resolution failed: {reason} (model_id={model_id} source_id={source_id})")
        self.model_id = str(model_id)
        self.source_id = str(source_id)
        self.reason = str(reason)


def log_lineage_resolution_failure(error: LineageResolutionError) -> None:
    """Emit the operator-visible signal for one failed resolution.

    Lives here rather than at the scheduler callsite because this module owns
    the wording of this signal; ``scheduler_core`` owns only the decision not
    to cache.  ``detail`` renders the underlying exception (``__cause__``) so a
    read failure names the driver error instead of just "it failed".

    Deliberately NOT de-duplicated: the same key may warn several times in one
    pass (discovery / candidates / backfill each ask).  That repetition is on
    an already-failing path and is the LOUD side; a de-dup cache would store
    failure state in the process again, which is precisely what #1740 removes.
    """

    LOGGER.warning(
        "%s model_id=%s source_id=%s reason=%s detail=%r",
        LINEAGE_RESOLUTION_FAILED_LOG_TOKEN,
        error.model_id,
        error.source_id,
        error.reason,
        error.__cause__,
    )


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

    Returns ``None`` — "resolved, no lineage" — for a provider that is
    ``None``, blank arguments, a provider exposing neither accessor, an index
    that has never been published, and provenance that simply is not there or
    does not establish a predecessor.  That errs toward keeping the model in
    scope, which can leave a loud stuck gap but can never silently hide
    completed work.

    Raises :class:`LineageResolutionError` when the answer is UNKNOWN rather
    than "none" (#1740): an accessor that raises, a provider that returns a
    non-``Mapping``, or a file plane that reports ``status == "blocked"`` for
    any reason other than a never-published index.  The file plane does not
    raise — it answers ``{"status": "blocked", "has_lineage": False, ...}`` —
    so without this branch a corrupt index read exactly like a model that never
    cloned, and got memoized as such.
    """

    if provider is None or not str(model_id or "").strip() or not str(source_id or "").strip():
        return None
    index_signal = getattr(provider, "clone_lineage_signal", None)
    if callable(index_signal):
        try:
            signal = index_signal(model_id=model_id, source_id=source_id)
        except Exception as error:  # noqa: BLE001 — any raise is an unknown answer, not "no lineage"
            raise LineageResolutionError(
                model_id=model_id, source_id=source_id, reason="clone_lineage_signal_failed"
            ) from error
        if not isinstance(signal, Mapping):
            # The resolution contract is broken, which is not the same claim as
            # "this model has no predecessor".
            raise LineageResolutionError(
                model_id=model_id,
                source_id=source_id,
                reason="clone_lineage_signal_contract_violation",
            )
        if str(signal.get("status") or "") == "blocked":
            reason = str(signal.get("reason") or "state_snapshot_index_unavailable")
            # The split is by blocker reason, NOT by status.  A deployment that
            # has never published an index is HEALTHY with no clones, and "no
            # index" and "this pair is not in the index" are the same answer;
            # failing it would make every pair on node-22's db-free plane warn
            # every pass forever and never converge.  Everything else here —
            # unreadable, malformed JSON, not an object, over the size limit,
            # failed validation or freshness — means "there should have been an
            # answer in there and I could not get it".
            if reason == _STATE_INDEX_NEVER_PUBLISHED_REASON:
                return None
            raise LineageResolutionError(
                model_id=model_id, source_id=source_id, reason=reason
            )
        return _from_index_signal(signal, model_id=model_id, source_id=source_id)
    earliest_clone_row = getattr(provider, "get_earliest_clone_row_for_model_source", None)
    if callable(earliest_clone_row):
        try:
            row = earliest_clone_row(model_id=model_id, source_id=source_id)
        except Exception as error:  # noqa: BLE001 — a DB read that raises is the #1740 trigger
            raise LineageResolutionError(
                model_id=model_id, source_id=source_id, reason="earliest_clone_row_read_failed"
            ) from error
        if row is None:
            # A successful read that found nothing: this pair has no clone row.
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
    model genuinely completed" without re-deriving lineage.  Annotation only: it
    is never re-derived into a completion, admission or selection decision.  The
    backfill predecessor lane does READ one copy back, to carry its own
    already-made skip decision across two functions of the same pass — see the
    module docstring.
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
