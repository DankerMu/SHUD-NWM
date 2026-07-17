"""§8 generation-aware cutover gate for the DB-free scheduler (Issue #1081).

Extracted from ``scheduler_core.py`` to keep the core module below the
1000-line governance guard.  The functions here are module-level and take a
scheduler-like object as the first argument (`` scheduler`` — a
``services.orchestrator.scheduler_core.ProductionScheduler`` instance in
practice); this keeps the public seam of ``ProductionScheduler`` unchanged
while confining the §8 gating logic to a single, testable file co-located
with ``scheduler_generation.py``.

Contents
--------
- :data:`CUTOVER_DECLARATION_UNLOADED`: sentinel that distinguishes
  "declaration not yet loaded" from "loaded and returned ``None``" (env
  unset — no declaration configured).
- :func:`load_cutover_declaration`: per-pass cached loader (D8.1).
- :func:`evaluate_transition_decision`: runs the §8 transition-decision
  matrix for one candidate (returns ``None`` when the state-index signal is
  not ready so the caller can defer to the legacy path).
- :func:`legacy_strict_warm_start_evidence`: pre-§8 strict-warm-start
  evidence path — byte-identical to the original flow so the existing
  corrupt-index / stale-index / missing exact-checkpoint regression tests
  continue to pass.
- :func:`forecast_warm_start_env_enabled`: env-level flag check for the
  ``NHMS_REQUIRE_FORECAST_WARM_START`` compat toggle.
- :func:`candidate_pipeline_already_complete`: journal preflight for the
  D8.9 compat-mode terminal-skip path.
- :func:`strict_warm_start_evidence`: full §8-gated evidence path invoked
  from ``ProductionScheduler._strict_warm_start_for_candidate``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from services.orchestrator import scheduler as _scheduler
from services.orchestrator import scheduler_generation as _generation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from services.orchestrator.scheduler_core import ProductionScheduler

#: Sentinel that separates "declaration not yet loaded" from "loaded and
#: returned ``None``" (env unset — no declaration configured).  Kept as a
#: module-level object so both this module and ``scheduler_core.py`` reference
#: the same identity when checking cache freshness.
CUTOVER_DECLARATION_UNLOADED: object = object()


# ---------------------------------------------------------------------------
# Per-pass declaration cache
# ---------------------------------------------------------------------------


def load_cutover_declaration(scheduler: ProductionScheduler) -> Any:
    """Return the parsed cutover declaration cached for this scheduler.

    Issue #1081 §8.1 / D8.1: declaration loading happens once at planning
    time.  A subsequent env change during the same ``ProductionScheduler``
    lifetime is deliberately NOT observed — the scheduler must operate
    against a single stable declaration snapshot.
    """
    if scheduler._cutover_declaration_cache is CUTOVER_DECLARATION_UNLOADED:
        env_path = _scheduler.os.getenv(_generation.CUTOVER_DECLARATION_ENV) or None
        scheduler._cutover_declaration_cache = _generation.load_cutover_declaration(
            env_path,
            now=scheduler.config.now,
        )
    return scheduler._cutover_declaration_cache


# ---------------------------------------------------------------------------
# Env flag + journal completion probe
# ---------------------------------------------------------------------------


def forecast_warm_start_env_enabled(scheduler: ProductionScheduler) -> bool:
    """Return True when ``NHMS_REQUIRE_FORECAST_WARM_START`` is set truthy.

    Unlike ``_db_free_strict_warm_start_required_for`` this is a plain
    env-level flag check — it does not consider
    ``NHMS_FORECAST_WARM_START_REQUIRED_FROM``.  This is intentional: the
    Issue #1081 §8 preflight for completed cycles is a compat-mode toggle
    (env=false → preserve pre-§8 terminal-skip flow; env=true → emit §8
    evidence for auditability even for cycles rolled out before
    ``required_from``).  See D8.9 alignment in
    :func:`strict_warm_start_evidence`.
    """
    del scheduler  # unused; env is read from the process environment.
    try:
        return bool(_scheduler.OrchestratorConfig.from_env().require_forecast_warm_start)
    except Exception:
        return False


def candidate_pipeline_already_complete(
    scheduler: ProductionScheduler, candidate: _scheduler.SchedulerCandidate
) -> bool:
    """Check whether the active repository already has a completed pipeline.

    Returns False if the active repository is missing or does not expose
    ``has_completed_pipeline``.  A concrete probe error narrows to the
    filesystem / permission / OS errors we expect from the journal reader
    and returns False (fail-CLOSED w.r.t. the D8.9 admission seam — a
    False return short-circuits the compat-mode terminal-skip so §8
    gating still fires).  Any other exception re-raises: a genuine bug in
    the journal reader must surface rather than be silently swallowed.
    """
    active_repo = getattr(scheduler, "active_repository", None)
    provider = getattr(active_repo, "has_completed_pipeline", None) if active_repo is not None else None
    if not callable(provider):
        return False
    try:
        return bool(
            provider(
                source_id=candidate.source_id,
                cycle_time=candidate.cycle_time_utc,
                model_id=candidate.model_id,
            )
        )
    except (FileNotFoundError, PermissionError, OSError):
        # Expected probe-error surface: journal file missing / unreadable /
        # containment root moved.  §8 gating still runs.
        return False


# ---------------------------------------------------------------------------
# Transition-decision evaluation
# ---------------------------------------------------------------------------


def evaluate_transition_decision(
    scheduler: ProductionScheduler,
    candidate: _scheduler.SchedulerCandidate,
    cycle: _scheduler.SchedulerSourceCycle,
    *,
    required_lead_hours: int,
    package_checksum: str | None,
) -> _generation.TransitionEvaluation | None:
    """Run the §8 generation-aware transition decision matrix for one candidate.

    Returns ``None`` when the state-index history signal is not ready
    (e.g. corrupt / unreadable index) so the caller can defer to the
    existing ``strict_warm_start_evidence`` path — that path surfaces the
    precise malformed / unavailable index reason and preserves the
    pre-§8 blocker semantics.  §8's new admit decisions cannot fire
    without a trustworthy history read.
    """
    del cycle  # kept in the signature for callsite parity / future use.
    declaration = load_cutover_declaration(scheduler)
    candidate_time = _scheduler._ensure_utc(candidate.cycle_time_utc)
    expected_predecessor_cycle_id = _scheduler.cycle_id_for(
        candidate.source_id,
        candidate_time - _scheduler.timedelta(hours=required_lead_hours),
    )
    history_signal_evidence = scheduler._db_free_state_index_provider().generation_scoped_history_signal(
        model_id=candidate.model_id,
        source_id=candidate.source_id,
        before_time=candidate_time,
        current_package_checksum=package_checksum,
        expected_predecessor_cycle_id=expected_predecessor_cycle_id,
        expected_predecessor_lead_hours=required_lead_hours,
    )
    if not bool(history_signal_evidence.get("ready")):
        return None
    signal = _generation._HistorySignal(
        exists_current_generation=bool(
            history_signal_evidence.get("history_exists_current_generation")
        ),
        exists_any_generation=bool(
            history_signal_evidence.get("history_exists_any_generation")
        ),
        latest_current_generation_checkpoint=history_signal_evidence.get(
            "latest_current_generation_checkpoint"
        ),
        latest_any_generation_checkpoint=history_signal_evidence.get(
            "latest_any_generation_checkpoint"
        ),
        wrong_generation_predecessor_present=bool(
            history_signal_evidence.get("wrong_generation_predecessor_present")
        ),
        wrong_generation_predecessor_checksum=str(
            history_signal_evidence.get("wrong_generation_predecessor_checksum") or ""
        ),
    )
    return _generation.evaluate_transition_decision(
        model_id=candidate.model_id,
        package_checksum=package_checksum,
        source_id=candidate.source_id,
        candidate_cycle_time_utc=candidate_time,
        required_lead_hours=required_lead_hours,
        history=signal,
        declaration=declaration,
    )


# ---------------------------------------------------------------------------
# Pre-§8 (legacy) strict-warm-start path
# ---------------------------------------------------------------------------


def legacy_strict_warm_start_evidence(
    scheduler: ProductionScheduler,
    candidate: _scheduler.SchedulerCandidate,
    *,
    required_lead_hours: int,
    package_checksum: str | None,
) -> dict[str, Any] | None:
    """Pre-§8 strict-warm-start evidence path.

    Used when the state-index history signal cannot be trusted (corrupt,
    unreadable, or missing index).  The output is byte-identical to the
    original flow so the existing corrupt-index / stale-index / missing
    exact-checkpoint regression tests continue to pass.
    """
    evidence = scheduler._db_free_state_index_provider().strict_warm_start_evidence(
        model_id=candidate.model_id,
        source_id=candidate.source_id,
        valid_time=candidate.cycle_time_utc,
        model_package_version=candidate.model_package_uri,
        model_package_checksum=package_checksum,
        required_lead_hours=required_lead_hours,
    )
    if scheduler._db_free_strict_warm_start_required_for(candidate):
        return evidence
    if bool(evidence.get("ready")):
        evidence["mode"] = "db_free_exact_warm_start"
        return evidence
    if str(evidence.get("reason") or "") != "state_snapshot_index_exact_checkpoint_missing":
        evidence["mode"] = "db_free_state_continuity"
        return evidence
    history = scheduler._db_free_state_index_provider().usable_state_history_evidence(
        model_id=candidate.model_id,
        source_id=candidate.source_id,
        before_time=candidate.cycle_time_utc,
    )
    if not bool(history.get("ready")):
        history["mode"] = "db_free_state_continuity"
        return history
    if not bool(history.get("history_exists")):
        return None
    producer_cycle_time = _scheduler._ensure_utc(candidate.cycle_time_utc) - _scheduler.timedelta(
        hours=required_lead_hours
    )
    return _scheduler._evidence_safe(
        {
            **dict(evidence),
            "status": "blocked",
            "ready": False,
            "reason": "state_snapshot_index_prior_checkpoint_missing_after_history",
            "mode": "db_free_state_continuity",
            "required_lead_hours": required_lead_hours,
            "required_prior_cycle_time": _scheduler._format_utc(producer_cycle_time),
            "required_prior_cycle_id": _scheduler.cycle_id_for(candidate.source_id, producer_cycle_time),
            "continuity_policy": {
                "decision": "block_or_backfill_prior_cycle",
                "first_cold_seed_allowed": False,
                "history_required_exact_successor": True,
            },
            "state_history": history,
            "failure": {
                "classifier": "file_state_snapshot_index_unavailable",
                "reason_code": "STATE_SNAPSHOT_INDEX_PRIOR_CHECKPOINT_MISSING_AFTER_HISTORY",
                "dependency": "file_state_snapshot_index",
                "retryable": True,
                "permanent": False,
            },
        }
    )


# ---------------------------------------------------------------------------
# §8 top-level entry point
# ---------------------------------------------------------------------------


_DECLARATION_LEVEL_BLOCKS = frozenset(
    {
        _generation.TransitionDecision.BLOCK_DECLARATION_MISSING,
        _generation.TransitionDecision.BLOCK_DECLARATION_STALE,
        _generation.TransitionDecision.BLOCK_COLD_START_OUT_OF_WINDOW,
        _generation.TransitionDecision.BLOCK_WRONG_GENERATION,
    }
)


def strict_warm_start_evidence(
    scheduler: ProductionScheduler,
    candidate: _scheduler.SchedulerCandidate,
    cycle: _scheduler.SchedulerSourceCycle,
) -> dict[str, Any] | None:
    """Return §8-gated strict-warm-start evidence for ``candidate``.

    The core delegator (``ProductionScheduler._strict_warm_start_for_candidate``)
    calls into this after checking ``db_free_required`` and the D8.9
    completed-pipeline preflight so the §8 hook remains a pure function of
    the scheduler + candidate + cycle triple.  Behavior mirrors what the
    pre-split ``_strict_warm_start_for_candidate`` body did.
    """
    required_lead_hours = scheduler._required_warm_start_lead_hours(candidate, cycle)
    model_package_checksum = (
        candidate.resource_profile.get("package_checksum")
        or candidate.resource_profile.get("model_package_checksum")
    )
    checksum_str = (
        str(model_package_checksum) if model_package_checksum not in (None, "") else None
    )

    # Issue #1081 §8: run the generation-aware transition decision BEFORE
    # the existing exact-warm-start check.  D8.9 requires this to gate
    # regardless of ``NHMS_REQUIRE_FORECAST_WARM_START`` — the env can
    # only weaken *warm-start hints*, never admit a declaration-less
    # cutover / missing predecessor / wrong-generation checkpoint.
    #
    # If the candidate does not carry a registry ``package_checksum`` we
    # cannot compute a generation identity for §8 gating; fall through
    # to the legacy strict-warm-start path when no declaration is
    # configured either, preserving pre-§8 behavior for callers whose
    # model rows omit the checksum from ``resource_profile``.  When a
    # declaration IS configured, the transition matrix still runs and
    # will surface ``block_declaration_stale`` — we cannot admit a
    # declared cutover without a verifiable candidate identity.
    if checksum_str is None and load_cutover_declaration(scheduler) is None:
        return legacy_strict_warm_start_evidence(
            scheduler,
            candidate,
            required_lead_hours=required_lead_hours,
            package_checksum=checksum_str,
        )
    transition = evaluate_transition_decision(
        scheduler,
        candidate,
        cycle,
        required_lead_hours=required_lead_hours,
        package_checksum=checksum_str,
    )
    if transition is None:
        # State-index unavailable / corrupt — the existing
        # strict_warm_start_evidence path (below) will emit the precise
        # index-level typed reason.  Skip §8 evidence attachment because
        # we cannot trust the history signal.
        return legacy_strict_warm_start_evidence(
            scheduler,
            candidate,
            required_lead_hours=required_lead_hours,
            package_checksum=checksum_str,
        )
    transition_evidence = _generation.generation_evidence(transition)

    if transition.decision == _generation.TransitionDecision.COLD_NEW_MODEL:
        return _scheduler._evidence_safe(
            {
                "status": "ready",
                "ready": True,
                "reason": None,
                "mode": "db_free_cold_new_model",
                "model_id": candidate.model_id,
                "source_id": candidate.source_id,
                "generation": transition.generation,
                "cold_start_reason": transition.cold_start_reason,
                "registry_cutover_transition": transition_evidence,
            }
        )
    if transition.decision == _generation.TransitionDecision.COLD_DECLARED_CUTOVER:
        return _scheduler._evidence_safe(
            {
                "status": "ready",
                "ready": True,
                "reason": None,
                "mode": "db_free_cold_declared_cutover",
                "model_id": candidate.model_id,
                "source_id": candidate.source_id,
                "generation": transition.generation,
                "cold_start_reason": transition.cold_start_reason,
                "registry_cutover_transition": transition_evidence,
            }
        )
    # Declaration-level block decisions have no additional information
    # beyond the transition matrix — emit them directly.  Predecessor
    # pending falls through to the existing strict_warm_start_evidence
    # path so the precise field-level reason (lead-hours mismatch, object
    # missing, checksum mismatch, etc.) is preserved for operators.
    if transition.decision in _DECLARATION_LEVEL_BLOCKS:
        producer_cycle_time = _scheduler._ensure_utc(candidate.cycle_time_utc) - _scheduler.timedelta(
            hours=required_lead_hours
        )
        return _scheduler._evidence_safe(
            {
                "status": "blocked",
                "ready": False,
                "reason": transition.typed_reason,
                "mode": "db_free_registry_cutover_transition",
                "model_id": candidate.model_id,
                "source_id": candidate.source_id,
                "generation": transition.generation,
                "registry_cutover_transition": transition_evidence,
                "required_lead_hours": required_lead_hours,
                "required_prior_cycle_time": _scheduler._format_utc(producer_cycle_time),
                "required_prior_cycle_id": _scheduler.cycle_id_for(
                    candidate.source_id, producer_cycle_time
                ),
                "selected_predecessor": transition.selected_predecessor,
                "failure": {
                    "classifier": "registry_cutover_transition_blocked",
                    "reason_code": (transition.typed_reason or "").upper(),
                    "dependency": "registry_cutover_transition",
                    "retryable": False,
                    "permanent": False,
                },
            }
        )

    # warm_continue AND block_predecessor_pending: fall through to
    # the existing exact-warm-start check so we still validate the object
    # exists, checksum matches, lineage ties, etc.  Attach the transition
    # summary to whichever evidence the existing check returns so audit
    # can trace the decision.
    evidence = scheduler._db_free_state_index_provider().strict_warm_start_evidence(
        model_id=candidate.model_id,
        source_id=candidate.source_id,
        valid_time=candidate.cycle_time_utc,
        model_package_version=candidate.model_package_uri,
        model_package_checksum=checksum_str,
        required_lead_hours=required_lead_hours,
    )
    evidence["generation"] = transition.generation
    evidence["registry_cutover_transition"] = transition_evidence
    if scheduler._db_free_strict_warm_start_required_for(candidate):
        return evidence
    if bool(evidence.get("ready")):
        evidence["mode"] = "db_free_exact_warm_start"
        return evidence
    if str(evidence.get("reason") or "") != "state_snapshot_index_exact_checkpoint_missing":
        evidence["mode"] = "db_free_state_continuity"
        return evidence

    history = scheduler._db_free_state_index_provider().usable_state_history_evidence(
        model_id=candidate.model_id,
        source_id=candidate.source_id,
        before_time=candidate.cycle_time_utc,
    )
    if not bool(history.get("ready")):
        history["mode"] = "db_free_state_continuity"
        history["registry_cutover_transition"] = transition_evidence
        return history
    # NOTE: In warm_continue, current-generation history exists by
    # definition — the exact predecessor was just observed by the
    # generation-scoped history signal.  If ``strict_warm_start_evidence``
    # then says the exact match is missing, it means the object failed
    # verification (checksum / usable_flag / lineage) — we fall through
    # to the same block-with-prior-checkpoint reason as before so the
    # public reason string stays stable.
    if not bool(history.get("history_exists")):
        # Should not happen for warm_continue; keep the existing
        # cold-seed passthrough as a defensive fallback for other paths.
        return None
    producer_cycle_time = _scheduler._ensure_utc(candidate.cycle_time_utc) - _scheduler.timedelta(
        hours=required_lead_hours
    )
    return _scheduler._evidence_safe(
        {
            **dict(evidence),
            "status": "blocked",
            "ready": False,
            "reason": "state_snapshot_index_prior_checkpoint_missing_after_history",
            "mode": "db_free_state_continuity",
            "generation": transition.generation,
            "registry_cutover_transition": transition_evidence,
            "required_lead_hours": required_lead_hours,
            "required_prior_cycle_time": _scheduler._format_utc(producer_cycle_time),
            "required_prior_cycle_id": _scheduler.cycle_id_for(candidate.source_id, producer_cycle_time),
            "continuity_policy": {
                "decision": "block_or_backfill_prior_cycle",
                "first_cold_seed_allowed": False,
                "history_required_exact_successor": True,
            },
            "state_history": history,
            "failure": {
                "classifier": "file_state_snapshot_index_unavailable",
                "reason_code": "STATE_SNAPSHOT_INDEX_PRIOR_CHECKPOINT_MISSING_AFTER_HISTORY",
                "dependency": "file_state_snapshot_index",
                "retryable": True,
                "permanent": False,
            },
        }
    )
