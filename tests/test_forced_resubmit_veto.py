"""#1199: bounded mixed-cohort forced-resubmit veto evidence.

The verdict is the exact pre-change all-basin conjunction.  A mixed cohort
(>=1 qualifying basin and >=1 non-qualifying basin) returns False AND records
exactly the FIRST non-qualifying basin in stable active-basin order as one
invocation-local fixed-shape `terminal_stage_forced_resubmit_veto` record.
All-qualifying returns True with no record; zero-qualifying returns False
with no record.  Later stage checks never overwrite the first record.

The evaluator/evidence implementation is owned by
``services.orchestrator.chain_forced_resubmit`` (#1562 structural split).  The
legacy ``chain_forecast_orchestrator_cycle`` ``_terminal_stage_needs_forced_
resubmit`` / ``_FORCE_TERMINAL_RESUBMIT_DECISIONS`` / ``_canonical_stage``
surfaces remain as direct aliases to the same objects; the compatibility tests
at the bottom of this file pin that they stay faithful.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

_FORCED_RESUBMIT_TERMINAL_JOB = {
    "job_id": "job_cycle_gfs_2026062800_forecast",
    "status": "succeeded",
    "stage": "forecast",
    "job_type": "run_shud_forecast_array",
}


def _forced_resubmit_basin(
    model_id: str,
    *,
    decision: str = "retry_terminal_run_manifest_missing",
    restart_stage: str = "forecast",
    **extra: Any,
) -> dict[str, Any]:
    basin = {
        "model_id": model_id,
        "basin_id": f"basin_{model_id}",
        "candidate_id": f"gfs:2026-06-28T00:00:00Z:{model_id}:forecast_gfs_deterministic",
        "run_id": f"fcst_gfs_2026062800_{model_id}",
        "state_evidence": {
            "decision": decision,
            "restart_stage": restart_stage,
            "restart_from_stage": restart_stage,
        },
    }
    basin.update(extra)
    return basin


def _forced_resubmit_context(
    active_basins: list[dict[str, Any]],
    *,
    restart_stage: str | None = "forecast",
    cycle_id: str = "gfs_2026062800",
    run_id: str = "cycle_gfs_2026062800",
) -> SimpleNamespace:
    return SimpleNamespace(
        active_basins=active_basins,
        restart_stage=restart_stage,
        cycle_id=cycle_id,
        run_id=run_id,
        forced_resubmit_veto=None,
    )


def test_forced_resubmit_all_eligible_returns_true_without_record() -> None:
    from services.orchestrator.chain_forced_resubmit import (
        _terminal_stage_needs_forced_resubmit,
    )

    context = _forced_resubmit_context(
        [_forced_resubmit_basin("model_a"), _forced_resubmit_basin("model_b")]
    )
    assert _terminal_stage_needs_forced_resubmit(context, _FORCED_RESUBMIT_TERMINAL_JOB) is True
    assert context.forced_resubmit_veto is None


def test_forced_resubmit_mixed_cohort_returns_false_with_exact_first_record() -> None:
    from services.orchestrator.chain_forced_resubmit import (
        FORCED_RESUBMIT_VETO_REASON,
        FORCED_RESUBMIT_VETO_SCHEMA,
        _terminal_stage_needs_forced_resubmit,
    )

    context = _forced_resubmit_context(
        [
            _forced_resubmit_basin("model_a"),
            _forced_resubmit_basin("model_b", decision="skip_terminal"),
        ]
    )
    assert _terminal_stage_needs_forced_resubmit(context, _FORCED_RESUBMIT_TERMINAL_JOB) is False

    record = context.forced_resubmit_veto
    assert record is not None
    # Fixed-shape key name, schema token, and reason token.
    assert set(record) == {
        "schema",
        "reason",
        "cycle_id",
        "pipeline_run_id",
        "terminal_job_id",
        "canonical_job_stage",
        "cohort_size",
        "qualifying_request_count",
        "first_veto_candidate_id",
        "first_veto_model_id",
        "first_veto_basin_id",
        "veto_decision",
        "canonical_restart_stage",
        "veto_cause",
    }
    assert record["schema"] == FORCED_RESUBMIT_VETO_SCHEMA
    assert record["reason"] == FORCED_RESUBMIT_VETO_REASON
    assert record["cycle_id"] == "gfs_2026062800"
    assert record["pipeline_run_id"] == "cycle_gfs_2026062800"
    assert record["terminal_job_id"] == "job_cycle_gfs_2026062800_forecast"
    assert record["canonical_job_stage"] == "forecast"
    assert record["cohort_size"] == 2
    assert record["qualifying_request_count"] == 1
    # The first vetoing basin is model_b (stable active-basin order).
    assert record["first_veto_model_id"] == "model_b"
    assert record["first_veto_basin_id"] == "basin_model_b"
    assert record["first_veto_candidate_id"] == (
        "gfs:2026-06-28T00:00:00Z:model_b:forecast_gfs_deterministic"
    )
    assert record["veto_decision"] == "skip_terminal"
    # The basin carries restart_stage="forecast", so the decisive veto evidence
    # reports the canonical restart stage alongside the veto decision even
    # though the decision check is the failing clause (#1199).
    assert record["canonical_restart_stage"] == "forecast"
    assert record["veto_cause"] == "decision_not_in_whitelist"


def test_forced_resubmit_multiple_vetoes_keep_first_record() -> None:
    from services.orchestrator.chain_forced_resubmit import (
        _terminal_stage_needs_forced_resubmit,
    )

    context = _forced_resubmit_context(
        [
            _forced_resubmit_basin("model_a", decision="skip_terminal"),
            _forced_resubmit_basin("model_b", decision="skip_terminal"),
            _forced_resubmit_basin("model_c"),
        ]
    )
    assert _terminal_stage_needs_forced_resubmit(context, _FORCED_RESUBMIT_TERMINAL_JOB) is False
    first = dict(context.forced_resubmit_veto)
    assert first["first_veto_model_id"] == "model_a"
    assert first["cohort_size"] == 3
    assert first["qualifying_request_count"] == 1

    # A later stage check over the same invocation must NOT overwrite the record.
    assert _terminal_stage_needs_forced_resubmit(context, _FORCED_RESUBMIT_TERMINAL_JOB) is False
    assert context.forced_resubmit_veto == first


def test_forced_resubmit_zero_eligible_returns_false_without_record() -> None:
    from services.orchestrator.chain_forced_resubmit import (
        _terminal_stage_needs_forced_resubmit,
    )

    context = _forced_resubmit_context(
        [
            _forced_resubmit_basin("model_a", decision="skip_terminal"),
            _forced_resubmit_basin("model_b", decision="skip_terminal"),
        ]
    )
    assert _terminal_stage_needs_forced_resubmit(context, _FORCED_RESUBMIT_TERMINAL_JOB) is False
    # No qualifying basin means no mixed-cohort incident to report.
    assert context.forced_resubmit_veto is None


def test_forced_resubmit_restart_stage_order_veto_is_typed() -> None:
    from services.orchestrator.chain_forced_resubmit import (
        _terminal_stage_needs_forced_resubmit,
    )

    # Whitelisted decision but the canonical restart stage is LATER than the
    # terminal job stage — the pre-change stage-order rule still vetoes, and
    # the single record reports the stage-order cause with the restart value.
    context = _forced_resubmit_context(
        [
            _forced_resubmit_basin("model_a"),
            _forced_resubmit_basin(
                "model_b",
                decision="retry_terminal_run_manifest_missing",
                restart_stage="parse",
            ),
        ]
    )
    assert _terminal_stage_needs_forced_resubmit(context, _FORCED_RESUBMIT_TERMINAL_JOB) is False

    record = context.forced_resubmit_veto
    assert record["first_veto_model_id"] == "model_b"
    assert record["veto_decision"] == "retry_terminal_run_manifest_missing"
    assert record["canonical_restart_stage"] == "parse"
    assert record["veto_cause"] == "stage_before_restart_stage"


def test_forced_resubmit_missing_state_evidence_veto_is_typed() -> None:
    from services.orchestrator.chain_forced_resubmit import (
        _terminal_stage_needs_forced_resubmit,
    )

    context = _forced_resubmit_context(
        [
            _forced_resubmit_basin("model_a"),
            _forced_resubmit_basin("model_b", state_evidence="not-a-mapping"),
        ]
    )
    assert _terminal_stage_needs_forced_resubmit(context, _FORCED_RESUBMIT_TERMINAL_JOB) is False

    record = context.forced_resubmit_veto
    assert record["first_veto_model_id"] == "model_b"
    assert record["veto_cause"] == "state_evidence_missing"
    assert record["veto_decision"] is None
    assert record["canonical_restart_stage"] is None


def test_forced_resubmit_archived_marker_shape_stays_non_qualifying() -> None:
    """#1199 compatibility: no branch-only replay admission on master.

    ``replay_manual_retry_admission`` existed only on the never-merged archived
    branch commit ``2094d480``; current master has no such eligibility contract.
    The archived shape nests the marker under ``state_evidence``; a marker
    with a NON-whitelisted decision (``manual_retry``) must stay
    non-qualifying — the marker never creates forced-resubmit eligibility.

    This is a PRESERVE/CONTROL test: it documents and pins the absent contract
    by asserting only the unchanged boolean verdict.  It is expected to pass
    both before and after this change; the mixed-cohort veto receipt itself is
    covered by the dedicated mixed-cohort tests.
    """
    from services.orchestrator.chain_forced_resubmit import (
        _terminal_stage_needs_forced_resubmit,
    )

    def marker_basin() -> dict[str, Any]:
        basin = _forced_resubmit_basin("model_b", decision="manual_retry")
        basin["state_evidence"]["replay_manual_retry_admission"] = {"status": "admitted"}
        return basin

    # The control must use the HISTORICAL nested shape: the marker lives under
    # ``state_evidence`` (as the archived branch emitted it), never at the
    # basin top level.  Pin the nesting so future helper drift cannot silently
    # weaken the control into a shape master never saw.
    nested = marker_basin()
    assert isinstance(nested["state_evidence"], dict)
    assert nested["state_evidence"]["replay_manual_retry_admission"] == {"status": "admitted"}
    assert "replay_manual_retry_admission" not in nested

    context = _forced_resubmit_context([_forced_resubmit_basin("model_a"), marker_basin()])
    assert _terminal_stage_needs_forced_resubmit(context, _FORCED_RESUBMIT_TERMINAL_JOB) is False
    # The same marker shape must not qualify on its own either (solo cohort).
    solo = _forced_resubmit_context([marker_basin()])
    assert _terminal_stage_needs_forced_resubmit(solo, _FORCED_RESUBMIT_TERMINAL_JOB) is False


def test_forced_resubmit_veto_keeps_canonical_restart_stage_for_non_whitelisted_decision() -> None:
    """#1199: the decisive veto evidence carries BOTH the veto decision and the
    canonical restart stage.

    The primary mixed-cohort shape carries a non-whitelisted decision plus
    ``restart_stage="forecast"``.  The boolean predicate and clause precedence
    are unchanged (the basin stays non-qualifying with cause
    ``decision_not_in_whitelist``), but the canonical restart stage is computed
    from the same source precedence even when the decision check is the failing
    clause, so the record reports it instead of ``None``.
    """
    from services.orchestrator.chain_forced_resubmit import (
        _terminal_stage_needs_forced_resubmit,
    )

    context = _forced_resubmit_context(
        [
            _forced_resubmit_basin("model_a"),
            _forced_resubmit_basin("model_b", decision="skip_terminal", restart_stage="forecast"),
        ]
    )
    assert _terminal_stage_needs_forced_resubmit(context, _FORCED_RESUBMIT_TERMINAL_JOB) is False

    record = context.forced_resubmit_veto
    assert record is not None
    assert record["veto_decision"] == "skip_terminal"
    assert record["canonical_restart_stage"] == "forecast"
    assert record["veto_cause"] == "decision_not_in_whitelist"
    assert record["first_veto_model_id"] == "model_b"


def test_forced_resubmit_veto_non_whitelisted_decision_without_restart_stage_stays_none() -> None:
    """#1199: when no restart source exists the canonical restart stage stays
    ``None`` even under a non-whitelisted decision.

    The canonical-stage computation is additive evidence, not a new admission:
    with no ``restart_stage`` / ``restart_from_stage`` and no context fallback,
    the record still reports the decision cause with a ``None`` restart stage.
    """
    from services.orchestrator.chain_forced_resubmit import (
        _terminal_stage_needs_forced_resubmit,
    )

    context = _forced_resubmit_context(
        [
            _forced_resubmit_basin("model_a"),
            _forced_resubmit_basin(
                "model_b",
                decision="skip_terminal",
                restart_stage=None,
                restart_from_stage=None,
            ),
        ],
        restart_stage=None,
    )
    assert _terminal_stage_needs_forced_resubmit(context, _FORCED_RESUBMIT_TERMINAL_JOB) is False

    record = context.forced_resubmit_veto
    assert record is not None
    assert record["veto_decision"] == "skip_terminal"
    assert record["canonical_restart_stage"] is None
    assert record["veto_cause"] == "decision_not_in_whitelist"


def test_forced_resubmit_veto_context_fallback_restart_stage_reported_for_non_whitelisted() -> None:
    """#1199: the canonical restart stage falls back to the context value even
    when the decision check is the failing clause.

    A basin with a non-whitelisted decision and no per-basin restart source but
    a context ``restart_stage`` reports that canonical stage in the evidence.
    """
    from services.orchestrator.chain_forced_resubmit import (
        _terminal_stage_needs_forced_resubmit,
    )

    context = _forced_resubmit_context(
        [
            _forced_resubmit_basin("model_a"),
            _forced_resubmit_basin(
                "model_b",
                decision="skip_terminal",
                restart_stage=None,
                restart_from_stage=None,
            ),
        ],
        restart_stage="forecast",
    )
    assert _terminal_stage_needs_forced_resubmit(context, _FORCED_RESUBMIT_TERMINAL_JOB) is False

    record = context.forced_resubmit_veto
    assert record is not None
    assert record["veto_decision"] == "skip_terminal"
    assert record["canonical_restart_stage"] == "forecast"
    assert record["veto_cause"] == "decision_not_in_whitelist"


def test_forced_resubmit_veto_record_text_fields_are_bounded() -> None:
    """#1199: every textual record field is capped so the fixed-shape retained
    block stays bounded even for over-limit identities/decisions.

    An over-limit non-whitelisted decision and an over-limit candidate identity
    still return ``False`` with cause ``decision_not_in_whitelist``, and every
    textual field lands within ``FORCED_RESUBMIT_VETO_TEXT_FIELD_MAX`` — exact
    normal strings unchanged, only over-limit values truncated.
    """
    from services.orchestrator.chain_forced_resubmit import (
        FORCED_RESUBMIT_VETO_TEXT_FIELD_MAX,
        _terminal_stage_needs_forced_resubmit,
    )

    over_limit_decision = "skip_terminal_" + "x" * FORCED_RESUBMIT_VETO_TEXT_FIELD_MAX
    over_limit_candidate = "candidate_" + "y" * FORCED_RESUBMIT_VETO_TEXT_FIELD_MAX
    veto_basin = _forced_resubmit_basin(
        "model_b", decision=over_limit_decision, restart_stage="forecast"
    )
    veto_basin["candidate_id"] = over_limit_candidate
    veto_basin["run_id"] = over_limit_candidate
    context = _forced_resubmit_context([_forced_resubmit_basin("model_a"), veto_basin])
    assert _terminal_stage_needs_forced_resubmit(context, _FORCED_RESUBMIT_TERMINAL_JOB) is False

    record = context.forced_resubmit_veto
    assert record is not None
    assert record["veto_cause"] == "decision_not_in_whitelist"
    assert record["veto_decision"] != over_limit_decision
    assert record["first_veto_candidate_id"] != over_limit_candidate
    for key in (
        "cycle_id",
        "pipeline_run_id",
        "terminal_job_id",
        "canonical_job_stage",
        "first_veto_candidate_id",
        "first_veto_model_id",
        "first_veto_basin_id",
        "veto_decision",
        "canonical_restart_stage",
        "veto_cause",
    ):
        value = record[key]
        if value is None:
            continue
        assert isinstance(value, str)
        assert len(value) <= FORCED_RESUBMIT_VETO_TEXT_FIELD_MAX


def test_forced_resubmit_veto_unhashable_decision_never_crashes_and_stays_non_qualifying() -> None:
    """#1199: a mapping/list decision does not crash evidence collection and
    never enters the record as nested data.

    The malformed shape is outside the production input domain; the boolean
    verdict stays ``False``/no admission, the veto cause is still
    ``decision_not_in_whitelist``, and the record carries only the stable
    bounded placeholder, never the raw mapping.
    """
    from services.orchestrator.chain_forced_resubmit import (
        FORCED_RESUBMIT_VETO_TEXT_FIELD_MAX,
        _terminal_stage_needs_forced_resubmit,
    )

    mapping_decision_basin = _forced_resubmit_basin(
        "model_b", decision={"skip_terminal": True}, restart_stage="forecast"
    )
    context = _forced_resubmit_context(
        [_forced_resubmit_basin("model_a"), mapping_decision_basin]
    )
    assert _terminal_stage_needs_forced_resubmit(context, _FORCED_RESUBMIT_TERMINAL_JOB) is False
    record = context.forced_resubmit_veto
    assert record is not None
    assert record["veto_cause"] == "decision_not_in_whitelist"
    assert not isinstance(record["veto_decision"], dict)
    assert isinstance(record["veto_decision"], str)
    assert len(record["veto_decision"]) <= FORCED_RESUBMIT_VETO_TEXT_FIELD_MAX

    list_decision_basin = _forced_resubmit_basin(
        "model_b", decision=["skip_terminal"], restart_stage="forecast"
    )
    context2 = _forced_resubmit_context([_forced_resubmit_basin("model_a"), list_decision_basin])
    assert _terminal_stage_needs_forced_resubmit(context2, _FORCED_RESUBMIT_TERMINAL_JOB) is False
    record2 = context2.forced_resubmit_veto
    assert record2 is not None
    assert record2["veto_cause"] == "decision_not_in_whitelist"
    assert not isinstance(record2["veto_decision"], list)
    assert isinstance(record2["veto_decision"], str)
    assert len(record2["veto_decision"]) <= FORCED_RESUBMIT_VETO_TEXT_FIELD_MAX


# ---------------------------------------------------------------------------
# Compatibility: the legacy ``chain_forecast_orchestrator_cycle`` surfaces stay
# faithful to the owner (direct aliases, one source of truth).
# ---------------------------------------------------------------------------


def test_cycle_gate_surface_aliases_the_owner_faithfully() -> None:
    # ``chain`` must import first: the cycle module is only importable through
    # the composed facade (pre-existing import ordering; the historical warm-
    # start tests import it the same way, always after ``chain``).
    import services.orchestrator.chain as _chain_module  # noqa: F401
    import services.orchestrator.chain_forced_resubmit as owner
    import services.orchestrator.chain_forecast_orchestrator_cycle as cycle

    assert cycle._terminal_stage_needs_forced_resubmit is owner._terminal_stage_needs_forced_resubmit
    assert cycle._FORCE_TERMINAL_RESUBMIT_DECISIONS is owner._FORCE_TERMINAL_RESUBMIT_DECISIONS
    assert cycle._canonical_stage is owner._canonical_stage
    assert cycle._STAGE_ORDER is owner._STAGE_ORDER
    assert cycle.FORCED_RESUBMIT_VETO_SCHEMA is owner.FORCED_RESUBMIT_VETO_SCHEMA
    assert cycle.FORCED_RESUBMIT_VETO_REASON is owner.FORCED_RESUBMIT_VETO_REASON
    assert cycle.FORCED_RESUBMIT_VETO_TEXT_FIELD_MAX == owner.FORCED_RESUBMIT_VETO_TEXT_FIELD_MAX
    assert (
        cycle.FORCED_RESUBMIT_VETO_CAUSE_STATE_EVIDENCE_MISSING
        == owner.FORCED_RESUBMIT_VETO_CAUSE_STATE_EVIDENCE_MISSING
    )
    assert (
        cycle.FORCED_RESUBMIT_VETO_CAUSE_DECISION_NOT_IN_WHITELIST
        == owner.FORCED_RESUBMIT_VETO_CAUSE_DECISION_NOT_IN_WHITELIST
    )
    assert (
        cycle.FORCED_RESUBMIT_VETO_CAUSE_RESTART_STAGE_UNAVAILABLE
        == owner.FORCED_RESUBMIT_VETO_CAUSE_RESTART_STAGE_UNAVAILABLE
    )
    assert (
        cycle.FORCED_RESUBMIT_VETO_CAUSE_STAGE_BEFORE_RESTART
        == owner.FORCED_RESUBMIT_VETO_CAUSE_STAGE_BEFORE_RESTART
    )


def test_cycle_gate_surface_returns_identical_verdicts_and_records() -> None:
    """Both the owner gate and the legacy cycle alias produce identical
    verdicts AND identical veto records over the same mixed cohort."""
    import services.orchestrator.chain as _chain_module  # noqa: F401
    import services.orchestrator.chain_forced_resubmit as owner
    import services.orchestrator.chain_forecast_orchestrator_cycle as cycle

    context = _forced_resubmit_context(
        [
            _forced_resubmit_basin("model_a"),
            _forced_resubmit_basin("model_b", decision="skip_terminal"),
        ]
    )
    owner_verdict = owner._terminal_stage_needs_forced_resubmit(
        context, _FORCED_RESUBMIT_TERMINAL_JOB
    )
    owner_record = dict(context.forced_resubmit_veto)

    legacy_context = _forced_resubmit_context(
        [
            _forced_resubmit_basin("model_a"),
            _forced_resubmit_basin("model_b", decision="skip_terminal"),
        ]
    )
    legacy_verdict = cycle._terminal_stage_needs_forced_resubmit(
        legacy_context, _FORCED_RESUBMIT_TERMINAL_JOB
    )
    legacy_record = dict(legacy_context.forced_resubmit_veto)

    assert owner_verdict is legacy_verdict
    assert owner_record == legacy_record


def test_terminal_job_statuses_keeps_pre_change_mutable_set_semantics() -> None:
    """#1562 split compat: the canonical ``TERMINAL_JOB_STATUSES`` object must
    remain a plain mutable ``set`` (as it was pre-split in ``chain``) with the
    exact pre-change members, and every re-export / consumer alias must be the
    same object -- never a copy, and never a ``frozenset``."""
    import services.orchestrator.chain as _chain_module  # noqa: F401
    import services.orchestrator.chain_forced_resubmit as owner
    import services.orchestrator.chain_forecast_execution as forecast_execution
    from services.orchestrator import chain_types

    expected = {
        "succeeded",
        "partially_failed",
        "failed",
        "cancelled",
        "submission_failed",
        "reservation_lost",
        "permanently_failed",
    }

    canonical = chain_types.TERMINAL_JOB_STATUSES
    assert canonical == expected
    assert type(canonical) is set
    # Identity: the canonical owner export is the very object ``chain`` and the
    # light-owner consumer aliases expose -- no copies anywhere.
    assert _chain_module.TERMINAL_JOB_STATUSES is canonical
    assert owner.TERMINAL_JOB_STATUSES is canonical
    assert forecast_execution.TERMINAL_JOB_STATUSES is canonical
    # Pre-change semantics: the canonical object stays mutable.
    assert not isinstance(canonical, frozenset)


def test_forced_resubmit_restart_stage_unavailable_cause_is_exact() -> None:
    """#1199: a whitelisted decision with no restart source reports the exact
    ``restart_stage_unavailable`` cause.

    The basin's decision IS in the whitelist but ``restart_stage``,
    ``restart_from_stage``, and the context fallback are all absent, so the
    canonical restart stage is ``None`` and the qualification clause that
    fails is the restart-stage availability check — not the decision check.
    The single record carries the exact whitelist decision token and a
    ``None`` canonical restart stage alongside the cause.
    """
    from services.orchestrator.chain_forced_resubmit import (
        FORCED_RESUBMIT_VETO_CAUSE_RESTART_STAGE_UNAVAILABLE,
        _terminal_stage_needs_forced_resubmit,
    )

    context = _forced_resubmit_context(
        [
            _forced_resubmit_basin("model_a"),
            _forced_resubmit_basin(
                "model_b",
                decision="retry_terminal_run_manifest_missing",
                restart_stage=None,
                restart_from_stage=None,
            ),
        ],
        restart_stage=None,
    )
    assert _terminal_stage_needs_forced_resubmit(context, _FORCED_RESUBMIT_TERMINAL_JOB) is False

    record = context.forced_resubmit_veto
    assert record is not None
    assert record["veto_cause"] == FORCED_RESUBMIT_VETO_CAUSE_RESTART_STAGE_UNAVAILABLE
    assert record["veto_cause"] == "restart_stage_unavailable"
    # The exact whitelist token is reported verbatim as the veto decision.
    assert record["veto_decision"] == "retry_terminal_run_manifest_missing"
    assert record["canonical_restart_stage"] is None
    # Veto candidate/model binding is exact (stable active-basin order).
    assert record["first_veto_model_id"] == "model_b"
    assert record["first_veto_basin_id"] == "basin_model_b"
    assert record["first_veto_candidate_id"] == (
        "gfs:2026-06-28T00:00:00Z:model_b:forecast_gfs_deterministic"
    )
    assert record["cohort_size"] == 2
    assert record["qualifying_request_count"] == 1


# ---------------------------------------------------------------------------
# #1199 Round 1: the REAL public cycle path — gate-produced veto record reaches
# the returned ``PipelineResult.candidate_outcomes`` over a mixed cohort whose
# terminal forecast job is resumed (no replacement Slurm submission).
# ---------------------------------------------------------------------------


def _veto_cycle_basin(
    model_id: str,
    *,
    decision: str,
    restart_stage: str | None = "forecast",
) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "basin_id": f"basin_{model_id}",
        "basin_version_id": "bv",
        "river_network_version_id": "rn",
        "model_package_uri": f"s3://nhms/models/{model_id}/v1/package/",
        "run_id": f"fcst_gfs_2026062800_{model_id}",
        "candidate_id": f"gfs:2026-06-28T00:00:00Z:{model_id}:forecast_gfs_deterministic",
        "state_evidence": {
            "decision": decision,
            "restart_stage": restart_stage,
            "restart_from_stage": restart_stage,
        },
    }


def test_real_cycle_resume_mixed_cohort_produces_veto_receipt_in_candidate_outcomes(
    tmp_path: Path,
) -> None:
    """#1199 Round 1: the REAL public ``ForecastOrchestrator.orchestrate_cycle``
    resume path carries the gate-produced veto record into the returned
    ``PipelineResult.candidate_outcomes``.

    This is the missing end-to-end composition proof: a terminal succeeded
    forecast stage job exists; the cohort mixes one whitelisted forced-resubmit
    basin with one non-whitelisted veto basin.  ``_run_cycle_chain`` drives the
    real ``_terminal_stage_needs_manual_retry`` -> real
    ``_terminal_stage_needs_forced_resubmit`` gate: the mixed verdict is False,
    so the terminal job is RESUMED (no replacement submission for the forecast
    stage — the fake client records zero extra forecast submissions), and the
    final ``PipelineResult.candidate_outcomes`` attaches the fixed-shape veto
    receipt exactly to the vetoing candidate, omitting it from the qualifying
    sibling.
    """
    # ``tests.test_orchestration_chain`` imports the ``chain`` facade at module
    # top, so the cycle module is already importable when this body runs.
    from services.orchestrator.chain_forced_resubmit import (
        FORCED_RESUBMIT_VETO_REASON,
        FORCED_RESUBMIT_VETO_SCHEMA,
    )
    from tests.test_orchestration_chain import (
        FakeCycleRepository,
        FakeCycleSlurmClient,
        _dt,
        _fmt,
        _orchestrator,
    )

    cycle_id = "gfs_2026062800"
    run_id = "cycle_gfs_2026062800"
    forecast_job_id = "job_cycle_gfs_2026062800_forecast"

    repository = FakeCycleRepository()
    repository.jobs[forecast_job_id] = {
        "job_id": forecast_job_id,
        "run_id": run_id,
        "cycle_id": cycle_id,
        "job_type": "run_shud_forecast_array",
        "slurm_job_id": "3001",
        "model_id": None,
        "status": "succeeded",
        "stage": "forecast",
        "submitted_at": _fmt(_dt("2026-06-28T00:00:00Z")),
        "started_at": None,
        "finished_at": None,
        "exit_code": 0,
        "error_code": None,
        "error_message": None,
        "log_uri": None,
    }

    client = FakeCycleSlurmClient(array_results_by_stage={"forecast": ["succeeded", "succeeded"]})
    client.jobs["3001"] = {
        "job_id": "3001",
        "run_id": run_id,
        "model_id": "model_0",
        "stage": "forecast",
        "status": "succeeded",
        "submitted_at": _fmt(_dt("2026-06-28T00:00:00Z")),
        "payload": {"tasks": [{}, {}]},
    }

    basins = [
        _veto_cycle_basin("model_0", decision="retry_terminal_run_manifest_missing"),
        _veto_cycle_basin("model_1", decision="skip_terminal"),
    ]
    for basin in basins:
        repository.hydro_runs[str(basin["run_id"])] = {
            "run_id": str(basin["run_id"]),
            "status": "submitted",
        }

    orchestrator = _orchestrator(tmp_path, repository, client)
    result = orchestrator.orchestrate_cycle("gfs", "2026062800", basins)

    # Pre-change decision semantics: the terminal job is resumed, no replacement
    # submission for this stage.  Only the downstream stages (parse/state_save_qc/
    # publish) submit.
    assert result.status == "complete"
    assert [submission["stage"] for submission in client.submissions] == [
        "parse",
        "state_save_qc",
        "publish",
    ]

    # The returned candidate outcomes expose the real gate receipt on exactly
    # the vetoing candidate; the qualifying sibling omits it.
    by_model = {outcome["model_id"]: outcome for outcome in result.candidate_outcomes}
    assert set(by_model) == {"model_0", "model_1"}
    veto = by_model["model_1"].get("terminal_stage_forced_resubmit_veto")
    assert veto is not None
    assert veto["schema"] == FORCED_RESUBMIT_VETO_SCHEMA
    assert veto["reason"] == FORCED_RESUBMIT_VETO_REASON
    assert veto["cycle_id"] == cycle_id
    assert veto["pipeline_run_id"] == run_id
    assert veto["terminal_job_id"] == forecast_job_id
    assert veto["canonical_job_stage"] == "forecast"
    assert veto["cohort_size"] == 2
    assert veto["qualifying_request_count"] == 1
    assert veto["first_veto_model_id"] == "model_1"
    assert veto["first_veto_basin_id"] == "basin_model_1"
    assert veto["veto_decision"] == "skip_terminal"
    assert veto["canonical_restart_stage"] == "forecast"
    assert veto["veto_cause"] == "decision_not_in_whitelist"
    assert "terminal_stage_forced_resubmit_veto" not in by_model["model_0"]
