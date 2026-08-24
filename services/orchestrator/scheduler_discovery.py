from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from packages.common.redaction import redact_payload
from packages.common.source_identity import normalize_source_id
from services.orchestrator import scheduler_discovery_evidence as _evidence
from services.orchestrator import scheduler_generation as _scheduler_generation
from services.orchestrator import scheduler_lineage as _scheduler_lineage
from services.orchestrator.scheduler_discovery_evidence import (  # noqa: F401
    SOURCE_DISCOVERY_SENSITIVE_KEY_RE,
    SOURCE_DISCOVERY_SENSITIVE_TEXT_RE,
    _source_cycle_not_selected_reason,
    _source_cycle_status_candidate,
)
from services.orchestrator.scheduler_init_state_match import (
    TERMINAL_INIT_STATE_ABSENT,
    TERMINAL_INIT_STATE_CONFLICT,
    init_state_field,
    terminal_init_state_match,
)
from services.orchestrator.scheduler_state import _ensure_utc, _evidence_safe, _format_utc
from workers.data_adapters.base import CycleDiscovery, cycle_id_for

MAX_DISCOVERED_CYCLES = 10000

# Verdict-path-only classification (#1775).  The shared helper's value domain
# stays exactly {match, absent, conflict}; ``unverifiable`` is produced by
# :func:`_terminal_init_state_verdict` AHEAD of the helper, for the one case the
# helper cannot answer: the strict resolution is not ready, so it names no state
# to compare the terminal row against.
TERMINAL_INIT_STATE_UNVERIFIABLE = "unverifiable"

# The `unverifiable` relaxation is admitted by CLOSED ALLOWLIST, never by
# denylist (#1775).  `strict_warm_start_evidence` reports not-ready for
# heterogeneous reasons: some mean "there is genuinely no predecessor state
# here" (the wedge this change exists to unblock), others report that something
# is WRONG with a state that DOES exist — a lineage/checksum mismatch, an
# unusable or unreadable checkpoint, a missing or unavailable index.  Only the
# first kind may borrow the successor-continuity tolerance; granting it to the
# second would let a run that started from a wrong-generation or corrupt
# predecessor score its cycle `complete` merely because a successor exists.
# Anything not enumerated here — including a not-ready evidence mapping that
# carries no reason at all, and any reason introduced later — classifies
# `conflict` and keeps today's hard gap.  Adding a member is a deliberate act.
#
# Deliberately EXCLUDED, despite the "missing" in its name:
# `state_snapshot_index_prior_checkpoint_missing_after_history` means history
# EXISTS and its checkpoint is gone — the #1150/#1152 operator-backfill
# population.  That is an anomaly, not an absence, and must keep blocking.
TERMINAL_INIT_STATE_UNVERIFIABLE_NOT_READY_REASONS = frozenset(
    {
        "state_snapshot_index_exact_checkpoint_missing",
    }
)


class SchedulerResourceLimitError(ValueError):
    def __init__(self, reason: str, details: Mapping[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details)


class CycleDiscoveryAdapter(Protocol):
    def discover_cycles(
        self,
        cycle_date: str | date | datetime,
        end_date: str | date | datetime | None = None,
    ) -> list[CycleDiscovery]:
        raise NotImplementedError


class SchedulerConfigLike(Protocol):
    sources: Sequence[str]
    allowed_cycle_hours_utc: Sequence[int]
    lookback_hours: int
    cycle_lag_hours: int
    max_cycles_per_source: int
    backfill_enabled: bool
    retry_limit: int
    candidate_state_job_limit: int
    candidate_state_event_limit: int


class SchedulerModelLike(Protocol):
    model_id: str


class SchedulerCandidateLike(Protocol):
    source_id: str
    cycle_time_utc: datetime
    model_id: str
    run_id: str
    forcing_version_id: str
    candidate_id: str


class CandidateStateDecisionLike(Protocol):
    action: str
    reason: str | None
    evidence: Mapping[str, Any]


class DiscoverSourceWindowProvider(Protocol):
    def __call__(
        self,
        adapter: CycleDiscoveryAdapter,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[CycleDiscovery]:
        raise NotImplementedError


class CycleCompletionStatusProvider(Protocol):
    def __call__(
        self,
        discovery: CycleDiscovery,
        models: Sequence[SchedulerModelLike],
        *,
        horizon: Mapping[str, Any] | None = None,
    ) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class SchedulerSourceCycle:
    discovery: CycleDiscovery
    horizon: Mapping[str, Any]


@dataclass(frozen=True)
class SchedulerDiscoveryContext:
    config: SchedulerConfigLike
    adapters: Mapping[str, CycleDiscoveryAdapter]
    active_repository: Any | None
    floor_to_source_cycle_boundary: Callable[[datetime, Sequence[str]], datetime]
    source_horizon_metadata: Callable[[CycleDiscovery, CycleDiscoveryAdapter], dict[str, Any]]
    candidate_factory: Callable[..., SchedulerCandidateLike]
    candidate_state_provider_caller: Callable[..., Mapping[str, Any] | None]
    candidate_state_decider: Callable[
        [SchedulerCandidateLike, Mapping[str, Any] | None],
        CandidateStateDecisionLike | None,
    ]
    model_source_is_out_of_scope: Callable[[SchedulerModelLike, CycleDiscovery], bool] | None = None
    strict_warm_start_for_candidate: Callable[
        [SchedulerCandidateLike, SchedulerSourceCycle],
        Mapping[str, Any] | None,
    ] | None = None
    successor_state_for_candidate: Callable[
        [SchedulerCandidateLike, SchedulerSourceCycle],
        Mapping[str, Any] | None,
    ] | None = None
    discover_source_window_provider: DiscoverSourceWindowProvider | None = None
    cycle_completion_status_provider: CycleCompletionStatusProvider | None = None
    # §8.7 (#1107): required warm-start lead hours for a candidate, used only
    # to compose the EXPECTED predecessor identity token for the journal
    # identity filter.  Optional — ``None`` means no judgement (legacy
    # behavior), and no second copy of the lead derivation lives here.
    required_lead_hours_for_candidate: Callable[
        [SchedulerCandidateLike, SchedulerSourceCycle],
        int,
    ] | None = None
    # #1735: resolves ``(model_id, source_id)`` to the model's own state-
    # lineage cutover ``t*`` (or ``None`` — no lineage).  Optional: ``None``
    # means no lineage is resolvable in this context, and every model is
    # scored exactly as before this change.
    lineage_cutover_for_model_source: Callable[
        [str, str],
        _scheduler_lineage.LineageCutover | None,
    ] | None = None


@dataclass(frozen=True)
class _CompletionScope:
    """The completion scope of one cycle, with the two filter stages kept apart.

    ``source_scoped`` is the scope after the source-scope filter and BEFORE
    the #1735 lineage filter; ``models`` is the final scope.  The caller needs
    both to tell an empty scope caused by missing configuration (a
    misconfiguration guard that must keep its ``gap`` verdict) from one caused
    by every model being lineage-scoped out (not a gap) — a distinction
    ``_cycle_completion_verdict`` cannot make, since it sees only the final
    tuple and returns ``gap`` unconditionally when it is empty.
    """

    models: tuple[SchedulerModelLike, ...]
    source_scoped: tuple[SchedulerModelLike, ...]
    lineage_excluded: tuple[
        tuple[SchedulerModelLike, _scheduler_lineage.LineageCutover], ...
    ]

    @property
    def emptied_by_lineage_filter(self) -> bool:
        return not self.models and bool(self.lineage_excluded)


def cycle_completion_status(
    context: SchedulerDiscoveryContext,
    discovery: CycleDiscovery,
    models: Sequence[SchedulerModelLike],
    *,
    horizon: Mapping[str, Any] | None = None,
) -> str:
    """Return 'complete' if every model's full pipeline is done for this cycle, else 'gap'."""

    scope = _completion_scope(context, discovery, models)
    # #1735 (D5): an empty scope has two causes that must not share a verdict.
    # "Nothing was configured / everything was source-scoped out" keeps its
    # ``gap`` misconfiguration guard below.  "Every model was lineage-scoped
    # out" is NOT a gap: there is no model that could ever have run this
    # cycle under its current identity, so scheduling it can only starve the
    # forward lane.  Reachable on an all-basin recalibration or a
    # single-basin deployment.
    if scope.emptied_by_lineage_filter:
        return "complete"
    scoped_models = scope.models
    verdict = _cycle_completion_verdict(context, discovery, scoped_models, horizon=horizon)
    if verdict != "complete":
        return verdict
    # §8.7 (#1107) single choke point: EVERY "complete" verdict must clear the
    # journal predecessor identity filter, not just the completed-provider
    # branch.  Under production ``NHMS_SCHEDULER_DB_FREE_REQUIRED=true`` the
    # strict/successor branch preempts and used to return "complete" without
    # any identity check.  No-judgement shapes leave the verdict untouched, so
    # legacy behavior is byte-identical outside a positive mismatch.
    for model in scoped_models:
        if _journal_predecessor_identity_is_stale(context, discovery, model, horizon=horizon):
            return "gap"
    return "complete"


def _models_in_completion_scope(
    context: SchedulerDiscoveryContext,
    discovery: CycleDiscovery,
    models: Sequence[SchedulerModelLike],
) -> tuple[SchedulerModelLike, ...]:
    return _completion_scope(context, discovery, models).models


def _completion_scope(
    context: SchedulerDiscoveryContext,
    discovery: CycleDiscovery,
    models: Sequence[SchedulerModelLike],
) -> _CompletionScope:
    """The single choke point every verdict tier consumes.

    Two filters, in order:

    1. source scope — a model variant that does not apply to this source;
    2. #1735 lineage existence — a model whose own cutover ``t*`` for this
       source is strictly later than the cycle time did not exist yet, so the
       completeness predicate ("every model's full pipeline is done for this
       cycle") cannot be asked about it.  Strict: ``cycle_time == t*`` stays
       in scope and must genuinely complete.
    """

    source_scope_filter = context.model_source_is_out_of_scope
    if callable(source_scope_filter):
        source_scoped = tuple(model for model in models if not source_scope_filter(model, discovery))
    else:
        source_scoped = tuple(models)
    resolver = context.lineage_cutover_for_model_source
    if not callable(resolver) or not source_scoped:
        return _CompletionScope(models=source_scoped, source_scoped=source_scoped, lineage_excluded=())
    in_scope: list[SchedulerModelLike] = []
    excluded: list[tuple[SchedulerModelLike, _scheduler_lineage.LineageCutover]] = []
    cycle_time = _ensure_utc(discovery.cycle_time)
    for model in source_scoped:
        cutover = resolver(str(model.model_id), str(discovery.source_id))
        if cutover is not None and _scheduler_lineage.is_pre_cutover(cutover, cycle_time):
            excluded.append((model, cutover))
            continue
        in_scope.append(model)
    return _CompletionScope(
        models=tuple(in_scope),
        source_scoped=source_scoped,
        lineage_excluded=tuple(excluded),
    )


def lineage_scoped_out_evidence(
    context: SchedulerDiscoveryContext,
    discovery: CycleDiscovery,
    models: Sequence[SchedulerModelLike],
) -> list[dict[str, Any]]:
    """#1735 §4: one ``lineage_scoped_out_pre_cutover`` annotation per excluded pair.

    Pure annotation — the pass evidence carries it so an operator can tell a
    cycle that scored ``complete`` with a recalibrated model scoped out from
    one where every model genuinely completed.  Nothing reads it back.
    """

    scope = _completion_scope(context, discovery, models)
    return [
        {
            "type": _scheduler_lineage.LINEAGE_SCOPED_OUT_REASON,
            **_evidence_safe(
                _scheduler_lineage.lineage_scoped_out_record(
                    cutover,
                    cycle_time=_ensure_utc(discovery.cycle_time),
                    cycle_id=cycle_id_for(discovery.source_id, discovery.cycle_time),
                )
            ),
        }
        for _model, cutover in scope.lineage_excluded
    ]


def _cycle_completion_verdict(
    context: SchedulerDiscoveryContext,
    discovery: CycleDiscovery,
    models: Sequence[SchedulerModelLike],
    *,
    horizon: Mapping[str, Any] | None = None,
) -> str:
    """Pre-#1107 completion scoring: source scope already applied, no identity gate."""

    state_provider = (
        getattr(context.active_repository, "candidate_state", None)
        if context.active_repository is not None
        else None
    )
    strict_provider = context.strict_warm_start_for_candidate
    successor_provider = context.successor_state_for_candidate
    if (callable(strict_provider) or callable(successor_provider)) and callable(state_provider) and models:
        cycle_horizon = dict(horizon or {})
        source_cycle = SchedulerSourceCycle(discovery=discovery, horizon=cycle_horizon)
        checked = False
        for model in models:
            candidate = context.candidate_factory(discovery=discovery, model=model, horizon=cycle_horizon)
            strict_evidence = strict_provider(candidate, source_cycle) if callable(strict_provider) else None
            successor_evidence = (
                successor_provider(candidate, source_cycle) if callable(successor_provider) else None
            )
            if strict_evidence is None and successor_evidence is None:
                continue
            checked = True
            state = context.candidate_state_provider_caller(
                state_provider,
                source_id=candidate.source_id,
                cycle_time=candidate.cycle_time_utc,
                model_id=candidate.model_id,
                run_id=candidate.run_id,
                forcing_version_id=candidate.forcing_version_id,
                candidate_id=candidate.candidate_id,
                retry_limit=context.config.retry_limit,
                job_limit=context.config.candidate_state_job_limit,
                event_limit=context.config.candidate_state_event_limit,
            )
            decision = context.candidate_state_decider(candidate, state)
            # #1775 D1: the terminal decision is evaluated BEFORE the strict /
            # successor admission early-returns.  Warm-start admission answers
            # "should this run be STARTED"; it must never veto the finding that
            # a run ALREADY COMPLETED.  Ordered the other way, an already
            # succeeded cycle whose strict resolution is not ready scored `gap`
            # forever and the backfill window could never advance past it.
            if decision is None or decision.reason not in {
                "terminal_hydro_success",
                "terminal_pipeline_success",
            }:
                # Non-terminal candidate: the pre-#1775 ordering evaluated the
                # strict gate, then the successor gate, then this one — all
                # three return `gap`, so falling back to `gap` here is the same
                # verdict for every non-terminal shape.
                return "gap"
            # Successor gating is NOT dropped for terminal candidates: the gate
            # that used to sit ahead of the terminal decision keeps its place
            # ahead of the init-state verdict.
            if successor_evidence is not None and not bool(successor_evidence.get("ready")):
                return "gap"
            if strict_evidence is not None:
                recorded = None
                if (
                    bool(strict_evidence.get("ready"))
                    and isinstance(selected := strict_evidence.get("candidate_state"), Mapping)
                    and init_state_field(selected, "state_id") not in (None, "")
                ):
                    provider = getattr(context.active_repository, "completed_pipeline_init_state_identity", None)
                    if callable(provider):
                        full_identity = provider(
                            source_id=candidate.source_id, cycle_time=candidate.cycle_time_utc,
                            model_id=candidate.model_id,
                        )
                        recorded = full_identity if isinstance(full_identity, Mapping) else None
                init_state_verdict = _terminal_init_state_verdict(decision.evidence, strict_evidence, observed=recorded)
                if init_state_verdict == TERMINAL_INIT_STATE_CONFLICT:
                    return "gap"
                if init_state_verdict in {
                    TERMINAL_INIT_STATE_ABSENT,
                    TERMINAL_INIT_STATE_UNVERIFIABLE,
                } and not _successor_state_proves_continuity(successor_evidence):
                    # #1775 D3: `unverifiable` reuses the `absent` branch's
                    # physical-continuity standard verbatim — no new leniency.
                    return "gap"
        if checked:
            return "complete"

    completed_provider = (
        getattr(context.active_repository, "has_completed_pipeline", None)
        if context.active_repository is not None
        else None
    )
    if callable(completed_provider) and models:
        all_completed = True
        for model in models:
            if not completed_provider(
                source_id=discovery.source_id,
                cycle_time=discovery.cycle_time,
                model_id=model.model_id,
            ):
                all_completed = False
                break
        if all_completed:
            return "complete"

    if callable(state_provider) and models:
        cycle_horizon = dict(horizon or {})
        for model in models:
            candidate = context.candidate_factory(discovery=discovery, model=model, horizon=cycle_horizon)
            state = context.candidate_state_provider_caller(
                state_provider,
                source_id=candidate.source_id,
                cycle_time=candidate.cycle_time_utc,
                model_id=candidate.model_id,
                run_id=candidate.run_id,
                forcing_version_id=candidate.forcing_version_id,
                candidate_id=candidate.candidate_id,
                retry_limit=context.config.retry_limit,
                job_limit=context.config.candidate_state_job_limit,
                event_limit=context.config.candidate_state_event_limit,
            )
            decision = context.candidate_state_decider(candidate, state)
            if decision is None or decision.reason not in {
                "terminal_hydro_success",
                "terminal_pipeline_success",
            }:
                return "gap"
        return "complete"

    if not callable(completed_provider) or not models:
        return "gap"
    for model in models:
        if not completed_provider(
            source_id=discovery.source_id,
            cycle_time=discovery.cycle_time,
            model_id=model.model_id,
        ):
            return "gap"
    return "complete"


def _journal_predecessor_identity_is_stale(
    context: SchedulerDiscoveryContext,
    discovery: CycleDiscovery,
    model: SchedulerModelLike,
    *,
    horizon: Mapping[str, Any] | None,
) -> bool:
    """Whether the journal's completed entry for this model has a stale lineage (§8.7, #1107).

    ``True`` only for a POSITIVE identity mismatch (same expected base key,
    different lineage suffix), which disqualifies the model from counting
    toward cycle completion so the cycle stays eligible for backfill.  Every
    other shape — no accessor on the repository, no lead-hours provider, no
    recorded identity, matching token, suffix-less legacy id, different base
    key (legal fallback warm start) — yields ``False``: no judgement, legacy
    behavior.  Read-only: no journal mutation, no run-manifest read.
    """
    return (
        _journal_predecessor_identity_stale_tokens(context, discovery, model, horizon=horizon)
        is not None
    )


def _journal_predecessor_identity_stale_tokens(
    context: SchedulerDiscoveryContext,
    discovery: CycleDiscovery,
    model: SchedulerModelLike,
    *,
    horizon: Mapping[str, Any] | None,
) -> tuple[str, str] | None:
    """Return ``(recorded, expected)`` tokens for a POSITIVE §8.7 mismatch, else ``None``.

    Single judgement body behind both the completion-scoring predicate
    :func:`_journal_predecessor_identity_is_stale` and the #1157 backfill-slot
    breaker, so the two can never disagree on which models keep a cycle a gap.
    """
    lead_hours_provider = context.required_lead_hours_for_candidate
    identity_provider = (
        getattr(context.active_repository, "completed_pipeline_init_state_id", None)
        if context.active_repository is not None
        else None
    )
    if not callable(lead_hours_provider) or not callable(identity_provider):
        return None
    recorded_init_state_id = identity_provider(
        source_id=discovery.source_id,
        cycle_time=discovery.cycle_time,
        model_id=model.model_id,
    )
    if recorded_init_state_id in (None, ""):
        return None
    cycle_horizon = dict(horizon or {})
    candidate = context.candidate_factory(discovery=discovery, model=model, horizon=cycle_horizon)
    source_cycle = SchedulerSourceCycle(discovery=discovery, horizon=cycle_horizon)
    try:
        required_lead_hours = int(lead_hours_provider(candidate, source_cycle))
        _expected_base, expected_init_state_id = _scheduler_generation.expected_journal_init_state_tokens(
            source_id=candidate.source_id,
            model_id=candidate.model_id,
            candidate_valid_time=candidate.cycle_time_utc,
            required_lead_hours=required_lead_hours,
        )
    except _scheduler_generation.JOURNAL_IDENTITY_INPUT_ERRORS:
        return None
    matches = _scheduler_generation.journal_init_state_lineage_matches_expected(
        str(recorded_init_state_id),
        source_id=candidate.source_id,
        model_id=candidate.model_id,
        candidate_valid_time=candidate.cycle_time_utc,
        required_lead_hours=required_lead_hours,
    )
    if matches is not False:
        return None
    return str(recorded_init_state_id), expected_init_state_id


def _breaker_engaged_gap_identities(
    context: SchedulerDiscoveryContext,
    discovery: CycleDiscovery,
    models: Sequence[SchedulerModelLike],
    *,
    horizon: Mapping[str, Any] | None,
) -> list[dict[str, Any]] | None:
    """Per-model identity evidence when a gap's ONLY cause is a broken quarantine (#1157 D4).

    Returns ``None`` — keep the execution slot — unless the cycle would score
    ``complete`` but for §8.7 stale flags AND every model carrying such a flag
    is breaker-engaged.  A mixed cycle (one model breaker-engaged, another
    genuinely incomplete) therefore still takes the slot and executes normally
    for the incomplete model: excluding it would starve real work.

    The cycle's completion status is untouched by this — it stays a gap, never
    ``complete`` (the §8.7 no-ADMIT invariant).  Read-only throughout.
    """
    # Without the occurrence accessor the breaker can never engage, so skip the
    # whole probe rather than re-scoring completion for every pass that has no
    # journal to count from.
    if not callable(
        getattr(context.active_repository, "completed_pipeline_init_state_id_occurrences", None)
    ):
        return None
    scoped_models = _models_in_completion_scope(context, discovery, models)
    if not scoped_models:
        return None
    if _cycle_completion_verdict(context, discovery, scoped_models, horizon=horizon) != "complete":
        return None
    identities: list[dict[str, Any]] = []
    for model in scoped_models:
        tokens = _journal_predecessor_identity_stale_tokens(context, discovery, model, horizon=horizon)
        if tokens is None:
            continue
        recorded_init_state_id, expected_init_state_id = tokens
        occurrences = _scheduler_generation.journal_identity_quarantine_occurrence_count(
            context.active_repository,
            source_id=discovery.source_id,
            cycle_time=discovery.cycle_time,
            model_id=model.model_id,
            recorded_init_state_id=recorded_init_state_id,
        )
        if not _scheduler_generation.journal_identity_quarantine_breaker_engaged(occurrences):
            return None
        identities.append(
            {
                "model_id": model.model_id,
                "recorded_init_state_id": recorded_init_state_id,
                "expected_init_state_id": expected_init_state_id,
                "occurrences": occurrences,
            }
        )
    return identities or None


def _terminal_init_state_verdict(
    terminal_evidence: Mapping[str, Any],
    strict_evidence: Mapping[str, Any],
    *,
    observed: Mapping[str, Any] | None = None,
) -> str:
    """Classify the terminal row's recorded init state for the cycle verdict.

    Two different facts used to share one ``conflict`` answer here (#1775 D2);
    the discriminator between them is the strict resolution's READY flag, never
    the mere presence or absence of ``candidate_state``:

    NOT READY, for an allowlisted reason
        a state was required for the candidate but could not be resolved
        because none exists, so nothing was named to compare against —
        ``unverifiable``.  This reverses
        the decision this docstring used to record ("keeps today's gap, which
        ``conflict`` expresses here").  That decision was taken when this path
        was reachable only for a READY resolution, where "names no state" could
        only mean a cold-start shape.  D1's reorder — evaluating the terminal
        decision before the strict early-return — makes the not-ready case
        reachable, and there "names no state" means the checkpoint the candidate
        would have needed is MISSING, which says nothing at all about whether
        the run already completed.  The cycle verdict tolerates it if and only
        if the successor state proves continuity (D3).
    NOT READY, for any other reason
        the resolution failed because something is wrong with a state that
        DOES exist (lineage/checksum mismatch, unusable or unreadable
        checkpoint, missing index), or for a reason nobody has classified yet.
        ``conflict`` — today's hard gap, unchanged.  See
        :data:`TERMINAL_INIT_STATE_UNVERIFIABLE_NOT_READY_REASONS`.
    READY but naming no state
        the cold-start generation shapes ``COLD_NEW_MODEL`` /
        ``COLD_DECLARED_CUTOVER`` (``scheduler_generation_gate.py:349-376``):
        the resolution genuinely resolved, and it resolved to no warm start.
        Unchanged — the verdict path bypasses the shared helper and keeps
        today's gap, which ``conflict`` expresses here.
    """

    if not bool(strict_evidence.get("ready")):
        not_ready_reason = str(strict_evidence.get("reason") or "")
        if not_ready_reason in TERMINAL_INIT_STATE_UNVERIFIABLE_NOT_READY_REASONS:
            return TERMINAL_INIT_STATE_UNVERIFIABLE
        return TERMINAL_INIT_STATE_CONFLICT
    selected = strict_evidence.get("candidate_state")
    if not isinstance(selected, Mapping) or init_state_field(selected, "state_id") in (None, ""):
        return TERMINAL_INIT_STATE_CONFLICT
    return terminal_init_state_match(selected, observed if observed is not None else terminal_evidence.get("hydro_run"))


def _successor_state_proves_continuity(successor_evidence: Mapping[str, Any] | None) -> bool:
    """Whether the successor checkpoint is a physical continuity proof.

    ``None`` is the third state — "no verdict was reached" (db-free disabled,
    no next allowed cycle, outside the strict window; ``scheduler_core.py:
    773-803``).  Silence is not proof, so absence tolerance stays disengaged.
    """

    return successor_evidence is not None and bool(successor_evidence.get("ready"))


def discover_cycles(
    context: SchedulerDiscoveryContext,
    started_at: datetime,
    models: Sequence[SchedulerModelLike] = (),
) -> tuple[list[SchedulerSourceCycle], list[dict[str, Any]]]:
    raw_end_time = started_at - timedelta(hours=context.config.cycle_lag_hours)
    end_time = context.floor_to_source_cycle_boundary(raw_end_time, context.config.sources)
    start_time = context.floor_to_source_cycle_boundary(
        end_time - timedelta(hours=context.config.lookback_hours),
        context.config.sources,
    )
    source_cycles: list[SchedulerSourceCycle] = []
    evidence: list[dict[str, Any]] = []
    seen_cycles: set[tuple[str, str]] = set()
    source_order = {source_id.lower(): index for index, source_id in enumerate(context.config.sources)}
    backfill_mode = bool(context.config.backfill_enabled and models)

    for source_id in context.config.sources:
        adapter = context.adapters.get(source_id)
        if adapter is None:
            source_evidence = {
                "source_id": source_id,
                "available": False,
                "status": "blocked",
                "reason": "source_adapter_unavailable",
                "cycle_id": None,
                "cycle_time_utc": None,
            }
            evidence.append(source_evidence)
            continue

        source_window_provider = context.discover_source_window_provider or discover_source_window
        discoveries = source_window_provider(
            adapter,
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
        )
        discoveries = [
            discovery
            for discovery in discoveries
            if discovery.source_id == source_id and start_time <= _ensure_utc(discovery.cycle_time) <= end_time
        ]
        discoveries, disallowed = _filter_allowed_cycle_hours(
            discoveries,
            allowed_cycle_hours_utc=context.config.allowed_cycle_hours_utc,
        )
        evidence.extend(_cycle_hour_not_allowed_evidence(discovery) for discovery in disallowed)
        discoveries.sort(key=lambda discovery: discovery.cycle_time, reverse=not backfill_mode)
        deduped: list[CycleDiscovery] = []
        for discovery in discoveries:
            cycle_key = (source_id, cycle_id_for(source_id, discovery.cycle_time))
            if cycle_key in seen_cycles:
                evidence.append(_duplicate_cycle_evidence(discovery, reason="duplicate_source_cycle"))
                continue
            seen_cycles.add(cycle_key)
            deduped.append(discovery)

        if backfill_mode:
            selected_for_source = _select_backfill_source_cycles(
                context,
                source_id=source_id,
                adapter=adapter,
                discoveries=deduped,
                models=models,
                evidence=evidence,
            )
        else:
            selected_for_source = _select_legacy_source_cycles(
                context,
                adapter=adapter,
                discoveries=deduped,
                evidence=evidence,
            )

        for discovery in selected_for_source:
            horizon = context.source_horizon_metadata(discovery, adapter)
            source_cycles.append(SchedulerSourceCycle(discovery=discovery, horizon=horizon))
            evidence.append(_source_cycle_evidence(discovery, horizon=horizon))

    source_cycles.sort(
        key=lambda item: (
            item.discovery.cycle_time,
            source_order.get(item.discovery.source_id.lower(), 999),
            item.discovery.cycle_hour,
        )
    )
    if backfill_mode and source_cycles:
        earliest_cycle_time = min(item.discovery.cycle_time for item in source_cycles)
        deferred_later_cycles = [
            item for item in source_cycles if item.discovery.cycle_time > earliest_cycle_time
        ]
        if deferred_later_cycles:
            source_cycles = [
                item for item in source_cycles if item.discovery.cycle_time == earliest_cycle_time
            ]
            for item in deferred_later_cycles:
                evidence.append(
                    _backfill_deferred_evidence(
                        item.discovery,
                        reason="backfill_deferred_waiting_for_global_prior_cycle",
                    )
                )
    return source_cycles, evidence


def _select_backfill_source_cycles(
    context: SchedulerDiscoveryContext,
    *,
    source_id: str,
    adapter: CycleDiscoveryAdapter,
    discoveries: Sequence[CycleDiscovery],
    models: Sequence[SchedulerModelLike],
    evidence: list[dict[str, Any]],
) -> list[CycleDiscovery]:
    complete_count = 0
    gaps: list[CycleDiscovery] = []
    for discovery in discoveries:
        horizon = context.source_horizon_metadata(discovery, adapter)
        completion_status_provider = context.cycle_completion_status_provider
        if completion_status_provider is None:
            status = cycle_completion_status(context, discovery, models, horizon=horizon)
        else:
            status = completion_status_provider(discovery, models, horizon=horizon)
        # #1735 §4: annotate every (model, cycle) pair the lineage filter
        # excluded, whatever the verdict, so a ``complete`` that only holds
        # because a recalibrated model did not exist yet is never mistaken
        # for "every model genuinely completed".  Annotation only.
        evidence.extend(lineage_scoped_out_evidence(context, discovery, models))
        if status == "complete":
            complete_count += 1
            continue
        gaps.append(discovery)
    available_gaps = [discovery for discovery in gaps if discovery.available]
    unavailable_gaps = [discovery for discovery in gaps if not discovery.available]
    # §8.7 quarantine breaker (#1157): a cycle whose ONLY reason for being a
    # gap is a stale journal lineage that a provenance-stamped quarantine
    # rerun already re-recorded can never converge by rerunning, so it must
    # stop holding the source's single oldest-first execution slot.  Walk from
    # the oldest gap and release consecutive breaker-engaged cycles to the next
    # one; the walk stops at the first cycle with real work, which keeps the slot.
    breaker_released: list[tuple[CycleDiscovery, list[dict[str, Any]]]] = []
    remaining_gaps = list(available_gaps)
    while remaining_gaps:
        identities = _breaker_engaged_gap_identities(
            context,
            remaining_gaps[0],
            models,
            horizon=context.source_horizon_metadata(remaining_gaps[0], adapter),
        )
        if identities is None:
            break
        breaker_released.append((remaining_gaps.pop(0), identities))
    # Backfill cycles feed cross-cycle warm start state. Even when operators
    # raise max_cycles_per_source for discovery breadth, only the oldest
    # available incomplete cycle may execute in this pass; later available gaps
    # wait until the prior window has produced a usable state. Unavailable
    # source cycles are evidence only and must not consume the execution slot.
    selected_for_source = remaining_gaps[:1]
    deferred = remaining_gaps[1:]
    for discovery in unavailable_gaps:
        item = _source_cycle_evidence(discovery, horizon=context.source_horizon_metadata(discovery, adapter))
        item["selection_status"] = "not_selected"
        item["selection_reason"] = _source_cycle_not_selected_reason(discovery)
        evidence.append(item)
    for discovery, identities in breaker_released:
        item = _source_cycle_evidence(discovery, horizon=context.source_horizon_metadata(discovery, adapter))
        item["selection_status"] = "not_selected"
        item["selection_reason"] = "journal_predecessor_identity_quarantine_breaker_engaged"
        # The cycle stays a GAP in completion scoring — this entry only records
        # that it no longer consumes the execution slot.
        item["journal_predecessor_identity_quarantine"] = _evidence_safe(
            {
                "models": identities,
                "occurrence_threshold": (
                    _scheduler_generation._JOURNAL_IDENTITY_QUARANTINE_BREAKER_THRESHOLD
                ),
            }
        )
        evidence.append(item)
    for discovery in deferred:
        evidence.append(
            _backfill_deferred_evidence(
                discovery,
                reason="backfill_deferred_waiting_for_prior_cycle",
            )
        )
    evidence.append(
        {
            "type": "backfill_audit",
            "source_id": source_id,
            "discovered_count": len(discoveries),
            "complete_count": complete_count,
            "gap_count": len(gaps),
            "available_gap_count": len(available_gaps),
            "unavailable_gap_count": len(unavailable_gaps),
            "breaker_released_gap_count": len(breaker_released),
            "selected_count": len(selected_for_source),
            "deferred_count": len(deferred),
        }
    )
    return selected_for_source


def _select_legacy_source_cycles(
    context: SchedulerDiscoveryContext,
    *,
    adapter: CycleDiscoveryAdapter,
    discoveries: Sequence[CycleDiscovery],
    evidence: list[dict[str, Any]],
) -> list[CycleDiscovery]:
    available: list[CycleDiscovery] = []
    unavailable_deferred: list[CycleDiscovery] = []
    for discovery in discoveries:
        if discovery.available:
            available.append(discovery)
        else:
            unavailable_deferred.append(discovery)
    if available:
        selected_for_source = available[: context.config.max_cycles_per_source]
    else:
        selected_for_source = unavailable_deferred[: context.config.max_cycles_per_source]
    selected_ids = {discovery.cycle_id for discovery in selected_for_source}
    for discovery in [item for item in unavailable_deferred if item.cycle_id not in selected_ids]:
        item = _source_cycle_evidence(discovery, horizon=context.source_horizon_metadata(discovery, adapter))
        item["selection_status"] = "not_selected"
        item["selection_reason"] = _source_cycle_not_selected_reason(discovery)
        evidence.append(item)
    return selected_for_source


def discover_source_window(
    adapter: CycleDiscoveryAdapter,
    *,
    source_id: str,
    start_time: datetime,
    end_time: datetime,
) -> list[CycleDiscovery]:
    # Thin owner wrapper: passes current owner globals so reassigning
    # MAX_DISCOVERED_CYCLES / SchedulerResourceLimitError here is observed.
    return _evidence._discover_source_window_impl(
        adapter,
        source_id=source_id,
        start_time=start_time,
        end_time=end_time,
        max_discovered_cycles=MAX_DISCOVERED_CYCLES,
        resource_limit_error=SchedulerResourceLimitError,
    )


def _filter_allowed_cycle_hours(
    discoveries: Sequence[CycleDiscovery],
    *,
    allowed_cycle_hours_utc: Sequence[int],
) -> tuple[list[CycleDiscovery], list[CycleDiscovery]]:
    return _evidence._filter_allowed_cycle_hours_impl(
        discoveries,
        allowed_cycle_hours_utc=allowed_cycle_hours_utc,
        ensure_utc=_ensure_utc,
    )


def _source_cycle_evidence(discovery: CycleDiscovery, *, horizon: Mapping[str, Any]) -> dict[str, Any]:
    available = bool(discovery.available)
    status = discovery.status or ("discovered" if available else "unavailable")
    cycle_time_hour_utc = _ensure_utc(discovery.cycle_time).hour
    evidence = {
        "source_id": discovery.source_id,
        "cycle_id": discovery.cycle_id,
        "cycle_time_utc": _format_utc(discovery.cycle_time),
        "cycle_hour": cycle_time_hour_utc,
        "horizon": dict(horizon),
        "available": available,
        "status": status,
        "reason": (
            discovery.reason if discovery.reason is not None else (None if available else "source_cycle_unavailable")
        ),
        "classifier": discovery.classifier,
        "retryable": discovery.retryable,
        "probe_uri": _source_secret_text_safe(discovery.probe_uri) if discovery.probe_uri is not None else None,
        "db_cycle_status_written": None,
        "cycle_status_candidate": _source_cycle_status_candidate(discovery, available=available),
    }
    if discovery.evidence:
        evidence["discovery_evidence"] = _source_discovery_evidence_safe(discovery.evidence)
    return _evidence_safe(evidence)


def _cycle_hour_not_allowed_evidence(discovery: CycleDiscovery) -> dict[str, Any]:
    evidence = _source_cycle_evidence(discovery, horizon={})
    evidence["selection_status"] = "excluded"
    evidence["selection_reason"] = "cycle_hour_not_allowed"
    evidence["status"] = "excluded"
    evidence["reason"] = "cycle_hour_not_allowed"
    return evidence


def _source_discovery_evidence_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            if SOURCE_DISCOVERY_SENSITIVE_KEY_RE.search(key_text):
                redacted["[redacted_key]"] = "[redacted]"
            else:
                redacted[key_text] = _source_discovery_evidence_safe(nested)
        return _evidence_safe(redacted)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_source_discovery_evidence_safe(item) for item in value]
    if isinstance(value, str):
        return _source_secret_text_safe(value)
    return _evidence_safe(value)


def _source_secret_text_safe(value: str) -> str:
    return _evidence._source_secret_text_safe_impl(
        value,
        redact_payload_fn=redact_payload,
        sensitive_text_re=SOURCE_DISCOVERY_SENSITIVE_TEXT_RE,
    )


def _duplicate_cycle_evidence(discovery: CycleDiscovery, *, reason: str) -> dict[str, Any]:
    return _evidence._duplicate_cycle_evidence_impl(
        discovery,
        reason=reason,
        ensure_utc=_ensure_utc,
        format_utc=_format_utc,
        cycle_id_for_fn=cycle_id_for,
    )


def _backfill_deferred_evidence(discovery: CycleDiscovery, *, reason: str) -> dict[str, Any]:
    return _evidence._backfill_deferred_evidence_impl(
        discovery,
        reason=reason,
        ensure_utc=_ensure_utc,
        format_utc=_format_utc,
        cycle_id_for_fn=cycle_id_for,
    )


def source_horizon_metadata(discovery: CycleDiscovery, adapter: CycleDiscoveryAdapter) -> dict[str, Any]:
    return _evidence.source_horizon_metadata_impl(
        discovery,
        adapter,
        ensure_utc=_ensure_utc,
        normalize_source_id_fn=normalize_source_id,
    )
