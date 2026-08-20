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
- :func:`forecast_warm_start_env_enabled`: three-valued env-level check for
  the ``NHMS_REQUIRE_FORECAST_WARM_START`` compat toggle (enabled / disabled
  / unreadable).
- :func:`candidate_pipeline_already_complete`: journal preflight for the
  D8.9 compat-mode terminal-skip path.
- :func:`strict_warm_start_evidence`: full §8-gated evidence path invoked
  from ``ProductionScheduler._strict_warm_start_for_candidate``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from packages.common import state_qc as _state_qc
from services.orchestrator import scheduler as _scheduler
from services.orchestrator import scheduler_file_providers as _file_providers
from services.orchestrator import scheduler_generation as _generation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from services.orchestrator.scheduler_core import ProductionScheduler

#: Sentinel that separates "declaration not yet loaded" from "loaded and
#: returned ``None``" (env unset — no declaration configured).  Defined on the
#: leaf module ``scheduler_generation`` and re-exported here so this module,
#: ``scheduler_core`` and the tests keep one identity; ``scheduler_core`` must
#: read it from the leaf, because importing this module first leaves it only
#: partially initialized while the ``scheduler`` import cycle runs.
CUTOVER_DECLARATION_UNLOADED: object = _generation.CUTOVER_DECLARATION_UNLOADED

LOGGER = logging.getLogger(__name__)

#: Bounded read guard for a model package manifest (#1164 D1).  Bound to
#: ``scheduler_file_providers.MAX_MODEL_PACKAGE_MANIFEST_BYTES`` (the cap the
#: registry loader already applies, so nothing larger can reach the gate) rather
#: than restating a number that could drift out of sync — a corrupt / oversized
#: manifest must not exhaust scheduler memory during planning.
MAX_PACKAGE_MANIFEST_BYTES = _file_providers.MAX_MODEL_PACKAGE_MANIFEST_BYTES

#: Bounded read guard for the tier-(b) canonical packaged-IC object probe
#: (#1164 D1).  Defined next to the qualification policy it belongs to
#: (``scheduler_generation``) and re-exported here so the gate's IO reads one
#: constant.
MAX_PACKAGED_IC_PROBE_BYTES = _generation.MAX_PACKAGED_IC_PROBE_BYTES

#: Candidate ``state_evidence.mode`` emitted for an admitted packaged-IC
#: bootstrap.  Re-exported from ``scheduler_generation`` so gate callers keep a
#: local name while the constant has a single definition site (the cohort
#: carrier in ``chain_forecast_cycle`` reads it from there).
PACKAGED_IC_BOOTSTRAP_MODE = _generation.PACKAGED_IC_BOOTSTRAP_MODE


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


def forecast_warm_start_env_enabled(scheduler: ProductionScheduler) -> bool | None:
    """Return the three-valued ``NHMS_REQUIRE_FORECAST_WARM_START`` toggle.

    ``True`` / ``False`` mean the orchestrator env parsed and the compat
    toggle is explicitly enabled / disabled (unset parses to the disabled
    default).  ``None`` means the env could not be read AT ALL — for a reason
    that need not involve this flag — and the caller must not fold it into
    "explicitly disabled": ``False`` is the value that ENABLES the D8.9
    terminal-skip shortcut at ``_strict_warm_start_for_candidate``, so the
    collapse turned "the check could not be completed" into "the check
    answered no" and silently skipped §8 gating (Issue #1196).

    Unlike ``_db_free_strict_warm_start_required_for`` this is a plain
    env-level flag check — it does not consider
    ``NHMS_FORECAST_WARM_START_REQUIRED_FROM``.  This is intentional: the
    Issue #1081 §8 preflight for completed cycles is a compat-mode toggle
    (env=false → preserve pre-§8 terminal-skip flow; env=true → emit §8
    evidence for auditability even for cycles rolled out before
    ``required_from``).  See D8.9 alignment in
    :func:`strict_warm_start_evidence`.

    On ``None`` the caller takes the strict warm-start path instead.  The
    strict branches that read the env again (:func:`legacy_strict_warm_start_evidence`
    and the warm-continue / blocked-predecessor tail of
    :func:`strict_warm_start_evidence`) re-raise the same parse failure —
    a loud, attributable failure consistent with how every other
    ``OrchestratorConfig.from_env()`` call site propagates; the early-return
    decision branches never read it and return their evidence.  Neither shape
    is a silent skip, and no degraded parallel mode exists for "unreadable".
    The warning below is what makes either shape attributable: one line per
    scheduler instance carrying ``repr`` of the parse error, so the operator
    reads the broken variable from the log instead of guessing which of the
    seven ``from_env()`` consumers blew up.
    """
    try:
        return bool(_scheduler.OrchestratorConfig.from_env().require_forecast_warm_start)
    except Exception as error:  # noqa: BLE001 — any parse failure means "unreadable"
        if not getattr(scheduler, "_warm_start_env_unreadable_warned", False):
            LOGGER.warning(
                "SCHEDULER_WARM_START_ENV_UNREADABLE: orchestrator env config did "
                "not parse; taking the strict warm-start path instead of the "
                "completed-cycle terminal skip: %s",
                repr(error),
            )
            scheduler._warm_start_env_unreadable_warned = True
        return None


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
# #1164: packaged calibrated-IC qualification (all IO for D1 lives here)
# ---------------------------------------------------------------------------


def _package_manifest_reader(scheduler: ProductionScheduler) -> Any | None:
    """Return an object-store reader for model package manifests, or ``None``.

    Prefers an ``object_store`` the scheduler already carries (tests and future
    callers can inject one) and otherwise builds a filesystem-backed store from
    the same config knobs ``_db_free_state_manager_from_config`` uses, so the
    manifest read resolves ``s3://<prefix>/<key>`` exactly like every other
    DB-free object read.  Cached per scheduler lifetime: the registry manifest
    is itself bound at planning time, so the store cannot need re-resolution
    mid-pass.
    """
    cached = getattr(scheduler, "_package_manifest_store_cache", None)
    if cached is not None:
        return cached
    store = getattr(scheduler, "object_store", None)
    if store is None:
        from packages.common.object_store import LocalObjectStore, ObjectStoreError

        config = getattr(scheduler, "config", None)
        root = getattr(config, "object_store_root", None) or getattr(config, "workspace_root", None)
        if not root:
            return None
        try:
            store = LocalObjectStore(root, _scheduler.os.getenv("OBJECT_STORE_PREFIX") or "")
        except (ObjectStoreError, OSError, TypeError, ValueError):
            # Two-way, fail-CLOSED: ``None`` (here and for a missing root above)
            # is mapped by ``packaged_initial_condition_signal`` to
            # ``PACKAGED_IC_UNREADABLE`` / ``package_manifest_reader_unavailable``,
            # which blocks the candidate.  It never degrades to "the package
            # ships no IC", so no qualification contract rides on separating a
            # bad root from a bad prefix.
            return None
    try:
        scheduler._package_manifest_store_cache = store
    except AttributeError:  # pragma: no cover - defensive; scheduler is mutable
        pass
    return store


def _canonical_packaged_ic_probe(reader: Any, object_uri: str) -> _generation.PackagedIcObjectProbe:
    """Bounded no-follow stat + digest probe of ONE canonical packaged-IC object.

    Tier (b) of the D1 qualification: used only when the referenced package
    manifest carries no ``included_files`` inventory (the direct-grid variant
    shape), and only on the first-cycle branch.  A probe that cannot complete
    (unsafe/oversized/unsupported reference, IO error) reports
    ``unreadable_detail`` so the caller fails CLOSED; a probe that completes and
    finds no object reports plain non-existence.
    """
    try:
        if not reader.exists(object_uri):
            # Confirmed absence — the reader completed and found nothing.
            return _generation.PackagedIcObjectProbe(exists=False)
    except Exception:
        # THREE-way, not two: this branch is "the existence check could not be
        # completed", and ``unreadable_detail`` (evaluated before ``exists`` by
        # ``_classify_packaged_ic_by_object_probe``) keeps it distinct from the
        # confirmed absence above.  Carries the qualification contract and
        # honours it.
        return _generation.PackagedIcObjectProbe(
            exists=False,
            unreadable_detail="packaged_initial_condition_object_probe_failed",
        )
    try:
        content = reader.read_bytes_limited(object_uri, max_bytes=MAX_PACKAGED_IC_PROBE_BYTES)
    except Exception:
        # THREE-way: ``exists=True`` + ``unreadable_detail`` is neither the
        # confirmed absence above nor a completed digest.  Includes the oversize
        # refusal: an IC larger than the cap is unreadable for qualification
        # purposes, never "the package ships no IC".
        return _generation.PackagedIcObjectProbe(
            exists=True,
            unreadable_detail="packaged_initial_condition_object_probe_failed",
        )
    return _generation.PackagedIcObjectProbe(
        exists=True,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        header_shape_invalid_reason=_packaged_ic_header_shape_invalid_reason(content),
    )


def _packaged_ic_header_shape_invalid_reason(content: bytes) -> str:
    """Return why the probed IC header is malformed, or "" when it is well formed.

    Shape only -- the mesh element count cannot be cross-checked here because a
    packaged IC object is probed alone, without the package's ``.sp.mesh`` in hand
    (a named limit; the mesh cross-check lives on the registration and provision
    gates, which do have the whole package).  Undecodable bytes count as malformed
    rather than unreadable: the probe completed, the payload is simply not a SHUD
    header.
    """

    try:
        header = content.split(b"\n", 1)[0].decode("utf-8")
    except UnicodeDecodeError:
        return "IC header line is not UTF-8 text"
    shape = _state_qc.cfg_ic_header_shape(header.split())
    return "" if shape.valid else (shape.reason or "IC header shape is invalid")


def packaged_initial_condition_signal(
    scheduler: ProductionScheduler,
    candidate: _scheduler.SchedulerCandidate,
) -> _generation.PackagedIcSignal | None:
    """Return the #1164 packaged-IC qualification signal for ``candidate``.

    ``None`` means "no published package-manifest reference" — the legacy
    carve-out that keeps ``cold_new_model`` intact.  Every other outcome is a
    signal: a manifest that is referenced but unreachable / malformed is
    ``UNREADABLE`` (fail closed), never "no IC".

    The manifest read is unconditional; ONE package object is additionally
    probed only for the inventory-less direct-grid variant shape (D1 tier (b)),
    which is the only shape whose qualification cannot be decided from published
    metadata.  Callers reach this function on the first-cycle branch only, so
    the warm path still performs zero object IO.
    """
    resource_profile = getattr(candidate, "resource_profile", None) or {}
    manifest_uri = resource_profile.get("manifest_uri")
    if manifest_uri in (None, ""):
        return None
    reader = _package_manifest_reader(scheduler)
    if reader is None:
        return _generation.PackagedIcSignal(
            status=_generation.PACKAGED_IC_UNREADABLE,
            detail="package_manifest_reader_unavailable",
        )
    try:
        content = reader.read_bytes_limited(
            str(manifest_uri), max_bytes=MAX_PACKAGE_MANIFEST_BYTES
        )
    except Exception:
        # Bounded reader surface (object store errors, unsafe path, oversize,
        # unsupported reference shape): the manifest is referenced but we cannot
        # read it, which fails closed rather than degrading to "no IC".  The
        # "no manifest reference at all" case never reaches here — it returned
        # ``None`` above — so absence and undecidability stay distinct.
        return _generation.PackagedIcSignal(
            status=_generation.PACKAGED_IC_UNREADABLE,
            detail="package_manifest_read_failed",
        )
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return _generation.PackagedIcSignal(
            status=_generation.PACKAGED_IC_UNREADABLE,
            detail="package_manifest_malformed_json",
        )
    return _generation.classify_packaged_initial_condition(
        payload,
        resource_profile=resource_profile,
        canonical_object_probe=lambda object_uri: _canonical_packaged_ic_probe(reader, object_uri),
    )


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
    # #1164: the packaged-IC qualification signal is only meaningful on the
    # first-cycle branch, so the manifest read is scoped to candidates with no
    # history in ANY generation.  Warm / cutover candidates therefore incur no
    # additional object IO and cannot be blocked by a package-manifest read.
    packaged_signal = (
        packaged_initial_condition_signal(scheduler, candidate)
        if not signal.exists_any_generation
        else None
    )
    return _generation.evaluate_transition_decision(
        model_id=candidate.model_id,
        package_checksum=package_checksum,
        source_id=candidate.source_id,
        candidate_cycle_time_utc=candidate_time,
        required_lead_hours=required_lead_hours,
        history=signal,
        declaration=declaration,
        packaged_initial_condition=packaged_signal,
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

    if transition.decision == _generation.TransitionDecision.PACKAGED_IC_BOOTSTRAP:
        # #1164: admitted first cycle that MUST consume the calibrated IC
        # shipped in its model package.  ``packaged_ic_checksum`` is the
        # carrier that reaches the run manifest and, from there, the runtime's
        # consume-or-raise verification.
        return _scheduler._evidence_safe(
            {
                "status": "ready",
                "ready": True,
                "reason": None,
                "mode": PACKAGED_IC_BOOTSTRAP_MODE,
                "model_id": candidate.model_id,
                "source_id": candidate.source_id,
                "generation": transition.generation,
                "cold_start_reason": None,
                "packaged_ic_checksum": transition.packaged_ic_checksum,
                "registry_cutover_transition": transition_evidence,
            }
        )
    if (
        transition.decision
        == _generation.TransitionDecision.BLOCK_FIRST_CYCLE_INITIAL_STATE_UNDECIDED
    ):
        # #1164 fail-closed: the package's calibrated IC is unqualified or its
        # manifest is unreadable.  Nothing is submitted — a first cycle never
        # silently zeroes the model state.
        return _scheduler._evidence_safe(
            {
                "status": "blocked",
                "ready": False,
                "reason": transition.typed_reason,
                "mode": "db_free_first_cycle_initial_state_undecided",
                "model_id": candidate.model_id,
                "source_id": candidate.source_id,
                "generation": transition.generation,
                "registry_cutover_transition": transition_evidence,
                "failure": {
                    "classifier": "first_cycle_initial_state_undecided",
                    "reason_code": (transition.typed_reason or "").upper(),
                    "dependency": "model_package_initial_condition",
                    "retryable": False,
                    "permanent": False,
                },
            }
        )
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
    # NOTE: both warm_continue and block_predecessor_pending reach here.
    # In warm_continue, current-generation history exists by definition —
    # the exact predecessor was just observed by the generation-scoped
    # history signal.  If ``strict_warm_start_evidence`` then says the
    # exact match is missing, it means the object failed verification
    # (checksum / usable_flag / lineage) — we fall through to the same
    # block-with-prior-checkpoint reason as before so the public reason
    # string stays stable.
    if (
        not bool(history.get("history_exists"))
        and transition.decision == _generation.TransitionDecision.WARM_CONTINUE
    ):
        # #1150: cold-seed passthrough for warm_continue ONLY.  This probe
        # counts strictly-earlier entries while the matrix history signal
        # counts any usable current-generation entry, so the two can
        # disagree; warm_continue has no block to overturn, every other
        # decision (block_predecessor_pending included) falls through to
        # the blocked evidence below.  Positive predicate = fail CLOSED.
        return None
    producer_cycle_time = _scheduler._ensure_utc(candidate.cycle_time_utc) - _scheduler.timedelta(
        hours=required_lead_hours
    )
    # Issue #1152: split the two operator populations that reach this single
    # typed reason.  §8.6 steps back exactly ONE level per pass, so the gap
    # self-heals only when the emitted predecessor would itself be ADMITTED —
    # which is decided by the predecessor's own gate, not by a timestamp.  So
    # run that very verification here: the same provider call the predecessor
    # will make, at ``producer_cycle_time`` (= T − required_lead_hours) with
    # this candidate's package checksum and lead hours.  ``ready=True`` covers
    # identity, generation/lineage, ``usable_flag`` and state-object
    # availability/content in one shot; anything short of that (no earlier
    # history, a ≥2-cycle hole, a wrong-generation entry sitting exactly at the
    # slot, an index entry whose object is gone) is a fixpoint — the emitted
    # predecessor re-evaluates to a block every pass and the successor defers
    # forever, so only an operator publishing the missing state can close it.
    #
    # Neither ``history_exists`` nor ``latest_usable_state.valid_time`` is a
    # sound discriminator: ``usable_state_history_evidence`` is generation- and
    # object-blind (``state_manager.py`` :1297-1317), so both read "self-heal"
    # on geometries the predecessor's gate rejects.  Not wrapped in try/except
    # on purpose: this is the same provider call already made unprotected at
    # the top of this function, and swallowing a raise here would be exactly
    # the false reassurance this signal exists to prevent.  Additive fields
    # only: the gate decision and the ``failure`` block below are unchanged.
    self_heal_probe = scheduler._db_free_state_index_provider().strict_warm_start_evidence(
        model_id=candidate.model_id,
        source_id=candidate.source_id,
        valid_time=producer_cycle_time,
        model_package_version=candidate.model_package_uri,
        model_package_checksum=checksum_str,
        required_lead_hours=required_lead_hours,
    )
    self_heal_expected = bool(self_heal_probe.get("ready"))
    operator_signal: dict[str, Any] = {
        "self_heal_expected": self_heal_expected,
        "operator_action_required": not self_heal_expected,
        # Compact probe receipt: operators must be able to see WHY self-heal
        # was ruled out without re-running the gate.
        "self_heal_probe": {
            "ready": self_heal_expected,
            "reason": self_heal_probe.get("reason"),
        },
    }
    if not self_heal_expected:
        operator_signal["operator_action"] = "backfill_predecessor_state"
        operator_signal["runbook"] = "docs/runbooks/scheduler-dbfree-typed-reasons.md"
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
            **operator_signal,
            "failure": {
                "classifier": "file_state_snapshot_index_unavailable",
                "reason_code": "STATE_SNAPSHOT_INDEX_PRIOR_CHECKPOINT_MISSING_AFTER_HISTORY",
                "dependency": "file_state_snapshot_index",
                "retryable": True,
                "permanent": False,
            },
        }
    )
