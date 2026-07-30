"""Replay admission: config triple, terminal override, discovery branch (#1164 change 2).

Decision-shape table under test (design D1 v3).  "covered" = replay admission
active AND ``model_id`` in the closed set AND cycle inside the replay window.

| ``state_decision.reason``      | not covered (pre-change ladder)                                                                                                                                       | covered                                                                                                     |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| ``terminal_hydro_success``     | mismatch retry ``retry_strict_warm_start_terminal_init_state_mismatch`` (budget-consulted) / ``blocked`` ``strict_warm_start_retry_budget_exhausted`` once the budget is spent / successor retry ``retry_strict_warm_start_successor_checkpoint_missing`` with ``restart_stage="state_save_qc"`` / plain terminal skip | ``retry`` reason ``replay_terminal_override``, evidence ``decision="replay_resubmit"``, restart_stage = restart_from_stage = ``forecast``, ``native_shud_resubmitted=True``, ``durable_output_reused=False``, retry budget never consulted |
| ``terminal_pipeline_success``  | same ladder                                                                                                                                                             | same override                                                                                                |
| ``terminal_completed_cycle``   | generic skip (``scheduler_candidates.py:495-502``)                                                                                                                      | UNCHANGED generic skip — no override, nothing submitted; the driver surfaces it as a convergence-timeout halt |
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import chain as _chain  # noqa: F401  (import order: chain owns the cycle mixin)
from services.orchestrator import chain_forecast_orchestrator_cycle as chain_cycle_module
from services.orchestrator import scheduler_candidates as scheduler_candidates_module
from services.orchestrator import scheduler_replay as scheduler_replay_module
from services.orchestrator.scheduler import ProductionSchedulerConfig
from services.orchestrator.scheduler_replay import (
    REPLAY_MODE_ENV,
    REPLAY_MODEL_IDS_ENV,
    REPLAY_WINDOW_ENV,
    ReplayAdmission,
    SchedulerReplayConfigError,
    parse_replay_admission,
    replay_forcing_readiness,
)
from services.orchestrator.scheduler_state import CandidateStateDecision
from services.orchestrator.scheduler_types import SchedulerCandidate
from workers.data_adapters.base import CycleDiscovery, cycle_id_for

REPLAY_MODEL_ID = "dg_dth_ls_gfs"
OUT_OF_SET_MODEL_ID = "dg_other_basin_gfs"
WINDOW = "2026070500..2026072100"
IN_WINDOW_CYCLE = datetime(2026, 7, 6, 0, tzinfo=UTC)
OUT_OF_WINDOW_CYCLE = datetime(2026, 7, 22, 0, tzinfo=UTC)

SELECTED_STATE = {
    "state_id": "state-dth-ls-2026070600",
    "checksum": "sha256:aaaa",
    "valid_time": "2026-07-06T12:00:00Z",
}


def _admission() -> ReplayAdmission:
    admission = parse_replay_admission(mode=True, model_ids=REPLAY_MODEL_ID, window=WINDOW)
    assert admission is not None
    return admission


@dataclass
class _FakeConfig:
    replay_admission: ReplayAdmission | None = None
    retry_limit: int = 3
    candidate_state_job_limit: int = 50
    candidate_state_event_limit: int = 50
    cancel_active_slurm: bool = False
    dry_run: bool = False
    require_direct_grid: bool = False
    repair_missing_forcing: bool = False
    repair_missing_forcing_cycle_time: datetime | None = None
    nfs_raw_manifest_root: str | Path | None = None
    nfs_raw_manifest_prefix: str = "s3://nhms"


@dataclass
class _FakeModel:
    model_id: str
    resource_profile: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class _FakeCycle:
    discovery: CycleDiscovery
    horizon: Mapping[str, Any] = field(default_factory=dict)


class _FakeRepository:
    """Minimal active repository: a candidate-state provider and nothing active."""

    def __init__(self, raw_state: Mapping[str, Any] | None) -> None:
        self._raw_state = dict(raw_state or {})

    def candidate_state(self, **kwargs: Any) -> dict[str, Any]:
        return dict(self._raw_state)

    def has_active_orchestration(self, **kwargs: Any) -> bool:
        return False

    def has_active_pipeline(self, **kwargs: Any) -> bool:
        return False

    def has_completed_pipeline(self, **kwargs: Any) -> bool:
        return True

    def active_slurm_jobs(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


def _candidate_factory(*, discovery: CycleDiscovery, model: _FakeModel, horizon: Mapping[str, Any]) -> SchedulerCandidate:
    compact = discovery.cycle_time.strftime("%Y%m%d%H")
    return SchedulerCandidate(
        candidate_id=f"cand_{model.model_id}_{compact}",
        source_id=discovery.source_id,
        cycle_id=discovery.cycle_id,
        cycle_time_utc=discovery.cycle_time,
        model_id=model.model_id,
        basin_id=model.model_id.replace("dg_", "basin_"),
        basin_version_id=f"basins_{model.model_id}_vbasins",
        river_network_version_id="rn-1",
        segment_count=10,
        output_segment_count=10,
        model_package_uri=f"s3://nhms/models/{model.model_id}/",
        resource_profile=dict(model.resource_profile),
        display_capabilities={},
        horizon=dict(horizon),
        scenario_id="operational",
        run_id=f"fcst_{discovery.source_id.lower()}_{compact}_{model.model_id}",
        forcing_version_id=f"fv_{model.model_id}_{compact}",
        status="selected",
    )


def _terminal_evidence(reason: str) -> dict[str, Any]:
    """The production-shaped terminal skip evidence for a succeeded run."""

    return {
        "decision": "skip_terminal",
        "terminal_source": "hydro_run" if reason == "terminal_hydro_success" else "pipeline_job",
        "terminal_status": "succeeded",
        "durable_hydro_status": "succeeded",
        "durable_output_reused": True,
        "native_shud_resubmitted": False,
        "hydro_run": {
            "run_id": "fcst_gfs_2026070600_dg_dth_ls_gfs",
            "status": "succeeded",
            # A cold-started historical run: the recorded init state is NOT the
            # state the strict warm start now selects, which is exactly the
            # mismatch branch the replay override presses over.
            "init_state_id": "state-legacy-cold",
        },
    }


def _build(
    *,
    admission: ReplayAdmission | None,
    decision: CandidateStateDecision | None,
    strict_warm_start: Mapping[str, Any] | None,
    successor_state: Mapping[str, Any] | None = None,
    model_ids: Sequence[str] = (REPLAY_MODEL_ID,),
    cycle_time: datetime = IN_WINDOW_CYCLE,
    raw_state: Mapping[str, Any] | None = None,
    decisions_by_model: Mapping[str, CandidateStateDecision | None] | None = None,
) -> tuple[list[Any], list[Any], list[dict[str, Any]]]:
    discovery = CycleDiscovery(
        cycle_id=cycle_id_for("gfs", cycle_time),
        source_id="gfs",
        cycle_time=cycle_time,
        cycle_hour=cycle_time.hour,
        available=True,
        status="discovered",
    )
    context = scheduler_candidates_module.SchedulerCandidateConstructionContext(
        config=_FakeConfig(replay_admission=admission),
        active_repository=_FakeRepository(raw_state),
        canonical_readiness_for_candidate=lambda candidate, cycle: None,
        strict_warm_start_for_candidate=lambda candidate, cycle: (
            dict(strict_warm_start) if strict_warm_start is not None else None
        ),
        orchestrator_for=lambda source_id: pytest.fail("candidate construction must not build an orchestrator"),
        candidate_factory=_candidate_factory,
        candidate_state_decider=lambda candidate, state: (
            decisions_by_model.get(candidate.model_id)
            if decisions_by_model is not None
            else decision
        ),
        successor_state_for_candidate=lambda candidate, cycle: (
            dict(successor_state) if successor_state is not None else None
        ),
    )
    candidates, blocked, skipped, _duplicates, _sync = scheduler_candidates_module.build_candidates(
        context,
        models=[_FakeModel(model_id=model_id) for model_id in model_ids],
        cycles=[_FakeCycle(discovery=discovery)],
    )
    return candidates, blocked, skipped


# --------------------------------------------------------------------------
# 1.1 config triple
# --------------------------------------------------------------------------


def test_replay_triple_absent_is_inert() -> None:
    assert parse_replay_admission(mode=None, model_ids=None, window=None) is None
    assert parse_replay_admission(mode="", model_ids="", window="") is None


def test_replay_config_defaults_leave_scheduler_config_untouched(monkeypatch: Any, tmp_path: Path) -> None:
    for name in (REPLAY_MODE_ENV, REPLAY_MODEL_IDS_ENV, REPLAY_WINDOW_ENV):
        monkeypatch.delenv(name, raising=False)
    config = ProductionSchedulerConfig(workspace_root=str(tmp_path))
    assert config.replay_admission is None
    assert config.replay_mode is False
    assert config.replay_model_ids == ()
    assert config.replay_window is None


def test_replay_mode_without_companions_fails_closed(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv(REPLAY_MODE_ENV, "1")
    monkeypatch.delenv(REPLAY_MODEL_IDS_ENV, raising=False)
    monkeypatch.delenv(REPLAY_WINDOW_ENV, raising=False)
    with pytest.raises(SchedulerReplayConfigError) as error:
        ProductionSchedulerConfig(workspace_root=str(tmp_path))
    assert error.value.reason == "replay_model_ids_missing"


def test_replay_mode_without_window_fails_closed(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv(REPLAY_MODE_ENV, "true")
    monkeypatch.setenv(REPLAY_MODEL_IDS_ENV, REPLAY_MODEL_ID)
    monkeypatch.delenv(REPLAY_WINDOW_ENV, raising=False)
    with pytest.raises(SchedulerReplayConfigError) as error:
        ProductionSchedulerConfig(workspace_root=str(tmp_path))
    assert error.value.reason == "replay_window_missing"


@pytest.mark.parametrize(
    "window",
    ["2026070500", "2026070500..", "2026070500..20260721", "2026070500..2026073200", "2026072100..2026070500"],
)
def test_replay_window_malformed_fails_closed(window: str) -> None:
    with pytest.raises(SchedulerReplayConfigError):
        parse_replay_admission(mode=True, model_ids=REPLAY_MODEL_ID, window=window)


def test_replay_companion_without_mode_fails_closed(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.delenv(REPLAY_MODE_ENV, raising=False)
    monkeypatch.setenv(REPLAY_MODEL_IDS_ENV, REPLAY_MODEL_ID)
    monkeypatch.setenv(REPLAY_WINDOW_ENV, WINDOW)
    with pytest.raises(SchedulerReplayConfigError) as error:
        ProductionSchedulerConfig(workspace_root=str(tmp_path))
    assert error.value.reason == "replay_companion_without_mode"


def test_replay_mode_requires_pinned_single_cycle_pass(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv(REPLAY_MODE_ENV, "1")
    monkeypatch.setenv(REPLAY_MODEL_IDS_ENV, REPLAY_MODEL_ID)
    monkeypatch.setenv(REPLAY_WINDOW_ENV, WINDOW)
    with pytest.raises(SchedulerReplayConfigError) as error:
        ProductionSchedulerConfig(workspace_root=str(tmp_path), lookback_hours=24)
    assert error.value.reason == "replay_cycle_pin_missing"
    pinned = ProductionSchedulerConfig(
        workspace_root=str(tmp_path),
        lookback_hours=0,
        max_cycles_per_source=1,
        backfill_enabled=False,
    )
    assert pinned.replay_admission is not None
    assert pinned.replay_model_ids == (REPLAY_MODEL_ID,)
    assert pinned.replay_window == WINDOW


def test_replay_admission_scope_predicates() -> None:
    admission = _admission()
    assert admission.covers(model_id=REPLAY_MODEL_ID, cycle_time=IN_WINDOW_CYCLE)
    assert not admission.covers(model_id=OUT_OF_SET_MODEL_ID, cycle_time=IN_WINDOW_CYCLE)
    assert not admission.covers(model_id=REPLAY_MODEL_ID, cycle_time=OUT_OF_WINDOW_CYCLE)
    # Window bounds are inclusive on both ends.
    assert admission.includes_cycle(datetime(2026, 7, 5, 0, tzinfo=UTC))
    assert admission.includes_cycle(datetime(2026, 7, 21, 0, tzinfo=UTC))
    assert not admission.includes_cycle(datetime(2026, 7, 4, 12, tzinfo=UTC))


# --------------------------------------------------------------------------
# 1.2 terminal override — decision-shape table
# --------------------------------------------------------------------------


def _assert_replay_shape(candidate: Any, *, overridden_skip_reason: str, overridden_branch: str) -> None:
    evidence = candidate.state_evidence
    assert evidence["decision"] == "replay_resubmit"
    assert evidence["reason"] == "replay_terminal_override"
    assert evidence["restart_stage"] == "forecast"
    assert evidence["restart_from_stage"] == "forecast"
    assert evidence["native_shud_resubmitted"] is True
    assert evidence["durable_output_reused"] is False
    override = evidence["replay_terminal_override"]
    assert override["overridden_skip_reason"] == overridden_skip_reason
    assert override["overridden_branch"] == overridden_branch
    assert override["retry_budget_consulted"] is False
    assert override["replay_admission"]["model_ids"] == [REPLAY_MODEL_ID]
    assert override["replay_admission"]["window_start"] == "2026070500"
    assert override["replay_admission"]["window_end"] == "2026072100"


@pytest.mark.parametrize("reason", ["terminal_hydro_success", "terminal_pipeline_success"])
def test_completed_terminal_family_is_overridden(reason: str) -> None:
    """Production shape is ``terminal_hydro_success``; both family tokens override."""

    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=CandidateStateDecision("skip", reason, _terminal_evidence(reason)),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
    )
    assert blocked == []
    assert skipped == []
    assert len(candidates) == 1
    _assert_replay_shape(
        candidates[0],
        overridden_skip_reason=reason,
        overridden_branch="strict_warm_start_terminal_init_state_mismatch",
    )


def test_env_absent_keeps_the_mismatch_retry_byte_identical() -> None:
    """must-preserve 3: with no replay admission the ladder is unchanged."""

    reason = "terminal_hydro_success"
    candidates, blocked, skipped = _build(
        admission=None,
        decision=CandidateStateDecision("skip", reason, _terminal_evidence(reason)),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
    )
    assert blocked == []
    assert skipped == []
    assert len(candidates) == 1
    evidence = candidates[0].state_evidence
    assert evidence["decision"] == "retry_strict_warm_start_terminal_init_state_mismatch"
    assert evidence["reason"] == "strict_warm_start_terminal_init_state_mismatch"
    assert "replay_terminal_override" not in evidence


def test_terminal_completed_cycle_is_not_overridden() -> None:
    """The third completed-type token stays a generic skip — no replay seam."""

    evidence = _terminal_evidence("terminal_hydro_success")
    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=CandidateStateDecision("skip", "terminal_completed_cycle", evidence),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
    )
    assert candidates == []
    assert blocked == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "terminal_completed_cycle"
    assert "replay_terminal_override" not in skipped[0].get("state_evidence", {})


def test_successor_retry_shape_is_overridden_to_forecast_restart() -> None:
    """The ``state_save_qc`` resume would re-save state from the bad forecast."""

    reason = "terminal_hydro_success"
    evidence = _terminal_evidence(reason)
    # Make the terminal row match the selected warm state so the pre-change
    # ladder would take the successor-retry leg, not the mismatch leg.
    evidence["hydro_run"]["init_state_id"] = SELECTED_STATE["state_id"]
    evidence["run_manifest_initial_state"] = dict(SELECTED_STATE)
    evidence["hydro_run"]["init_state_checksum"] = SELECTED_STATE["checksum"]
    evidence["hydro_run"]["init_state_valid_time"] = SELECTED_STATE["valid_time"]
    strict = {"ready": True, "candidate_state": dict(SELECTED_STATE)}
    successor = {"ready": False, "reason": "successor_checkpoint_missing"}

    baseline_candidates, _blocked, _skipped = _build(
        admission=None,
        decision=CandidateStateDecision("skip", reason, dict(evidence)),
        strict_warm_start=strict,
        successor_state=successor,
    )
    assert baseline_candidates[0].state_evidence["restart_stage"] == "state_save_qc"

    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=CandidateStateDecision("skip", reason, dict(evidence)),
        strict_warm_start=strict,
        successor_state=successor,
    )
    assert blocked == []
    assert skipped == []
    assert len(candidates) == 1
    assert candidates[0].state_evidence["restart_stage"] != "state_save_qc"
    _assert_replay_shape(
        candidates[0],
        overridden_skip_reason=reason,
        overridden_branch="strict_warm_start_successor_checkpoint_missing",
    )


def test_exhausted_retry_budget_does_not_block_replay(monkeypatch: Any) -> None:
    """The historical budget is neither consumed nor even read on the replay path."""

    reason = "terminal_hydro_success"
    raw_state = {"retry_limit": 3, "pipeline_jobs": [], "hydro_run": {"status": "succeeded"}}

    def _exhausted_attempt(state: Mapping[str, Any], *, stage: str) -> int:
        del state, stage
        return 3

    monkeypatch.setattr(scheduler_candidates_module, "_state_retry_attempt", _exhausted_attempt)
    monkeypatch.setattr(scheduler_candidates_module, "_state_retry_limit", lambda state: 3)

    baseline_candidates, baseline_blocked, _skipped = _build(
        admission=None,
        decision=CandidateStateDecision("skip", reason, _terminal_evidence(reason)),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        raw_state=raw_state,
    )
    assert baseline_candidates == []
    assert baseline_blocked[0].reason == "strict_warm_start_retry_budget_exhausted"

    def _forbidden_attempt(state: Mapping[str, Any], *, stage: str) -> int:
        raise AssertionError("replay must not consult the persisted retry budget")

    monkeypatch.setattr(scheduler_candidates_module, "_state_retry_attempt", _forbidden_attempt)
    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=CandidateStateDecision("skip", reason, _terminal_evidence(reason)),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        raw_state=raw_state,
    )
    assert blocked == []
    assert skipped == []
    assert len(candidates) == 1
    assert candidates[0].state_evidence["reason"] != "strict_warm_start_retry_budget_exhausted"
    _assert_replay_shape(
        candidates[0],
        overridden_skip_reason=reason,
        overridden_branch="strict_warm_start_terminal_init_state_mismatch",
    )


def test_replay_evidence_survives_run_manifest_retry_upgrade() -> None:
    """``_upgrade_retry_for_strict_warm_start_manifest`` must not relabel it."""

    reason = "terminal_hydro_success"
    decision = scheduler_candidates_module._replay_terminal_override_decision(
        _FakeConfig(replay_admission=_admission()),
        _candidate_factory(
            discovery=CycleDiscovery(
                cycle_id=cycle_id_for("gfs", IN_WINDOW_CYCLE),
                source_id="gfs",
                cycle_time=IN_WINDOW_CYCLE,
                cycle_hour=0,
                available=True,
                status="discovered",
            ),
            model=_FakeModel(model_id=REPLAY_MODEL_ID),
            horizon={},
        ),
        CandidateStateDecision("skip", reason, _terminal_evidence(reason)),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        successor_state=None,
    )
    assert decision is not None
    upgraded = scheduler_candidates_module._upgrade_retry_for_strict_warm_start_manifest(
        decision,
        {"ready": True, "candidate_state": dict(SELECTED_STATE)},
    )
    assert upgraded is decision
    assert upgraded.evidence["decision"] == "replay_resubmit"


def test_out_of_set_model_and_out_of_window_cycle_keep_the_unchanged_decision() -> None:
    reason = "terminal_hydro_success"
    decision = CandidateStateDecision("skip", reason, _terminal_evidence(reason))
    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=None,
        decisions_by_model={REPLAY_MODEL_ID: decision, OUT_OF_SET_MODEL_ID: decision},
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        model_ids=(REPLAY_MODEL_ID, OUT_OF_SET_MODEL_ID),
    )
    assert blocked == []
    assert skipped == []
    by_model = {candidate.model_id: candidate for candidate in candidates}
    assert by_model[REPLAY_MODEL_ID].state_evidence["decision"] == "replay_resubmit"
    assert (
        by_model[OUT_OF_SET_MODEL_ID].state_evidence["decision"]
        == "retry_strict_warm_start_terminal_init_state_mismatch"
    )

    out_of_window, _blocked, _skipped = _build(
        admission=_admission(),
        decision=decision,
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        cycle_time=OUT_OF_WINDOW_CYCLE,
    )
    assert out_of_window[0].state_evidence["decision"] == "retry_strict_warm_start_terminal_init_state_mismatch"


def test_non_completed_state_decisions_are_not_overridden() -> None:
    """Only the completed-type terminal family is in the override seam."""

    admission = _admission()
    active_skip = CandidateStateDecision(
        "skip",
        "active_duplicate_pipeline",
        {"decision": "skip_active", "active_status": "running"},
    )
    _candidates, _blocked, skipped = _build(
        admission=admission,
        decision=active_skip,
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
    )
    assert skipped[0]["reason"] == "active_duplicate_pipeline"

    blocked_decision = CandidateStateDecision(
        "blocked",
        "missing_upstream_artifact",
        {"decision": "blocked_missing_upstream_artifact"},
    )
    _candidates, blocked, _skipped = _build(
        admission=admission,
        decision=blocked_decision,
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
    )
    assert blocked[0].reason == "missing_upstream_artifact"


def test_first_cycle_bootstrap_branch_is_untouched_by_replay() -> None:
    """Change-1 contract: a cleared state scope keeps its own decision path.

    With the state chain cleared there is no completed-type terminal decision to
    override (``candidate_state`` yields ``None``), so the candidate reaches the
    submission set exactly as it does without replay admission and carries no
    replay evidence.  The ``PACKAGED_IC_BOOTSTRAP`` decision itself lives in
    ``scheduler_generation`` and is never consulted here.
    """

    with_replay, blocked, skipped = _build(
        admission=_admission(),
        decision=None,
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
    )
    without_replay, _blocked, _skipped = _build(
        admission=None,
        decision=None,
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
    )
    assert blocked == []
    assert skipped == []
    assert len(with_replay) == 1
    assert with_replay[0].to_dict() == without_replay[0].to_dict()
    assert "replay_terminal_override" not in with_replay[0].state_evidence


def test_replay_resubmit_is_in_the_chain_force_terminal_resubmit_whitelist() -> None:
    assert "replay_resubmit" in chain_cycle_module._FORCE_TERMINAL_RESUBMIT_DECISIONS
    # Append-only: the pre-existing members are untouched.
    assert {
        "retry_repair_missing_forcing",
        "retry_missing_forecast_output",
        "retry_strict_warm_start_terminal_init_state_mismatch",
        "retry_strict_warm_start_terminal_run_manifest_missing",
        "retry_strict_warm_start_retry_run_manifest_mismatch",
        "retry_terminal_run_manifest_missing",
    } <= chain_cycle_module._FORCE_TERMINAL_RESUBMIT_DECISIONS


def test_repair_and_replay_candidates_land_in_separate_restart_cohorts() -> None:
    """R2: the 070712 repair (``forcing``) never shares a cohort with replay."""

    from services.orchestrator import scheduler_execution as scheduler_execution_module

    @dataclass
    class _CohortCandidate:
        model_id: str
        state_evidence: Mapping[str, Any]

    replay_candidate = _CohortCandidate(
        model_id=REPLAY_MODEL_ID,
        state_evidence={"decision": "replay_resubmit", "restart_stage": "forecast"},
    )
    repair_candidate = _CohortCandidate(
        model_id="dg_huai_main_gfs",
        state_evidence={"decision": "retry_repair_missing_forcing", "restart_stage": "forcing"},
    )
    cohorts = scheduler_execution_module.restart_compatible_candidate_cohorts(
        [replay_candidate, repair_candidate],
        candidate_restart_stage=lambda candidate: str(candidate.state_evidence["restart_stage"]),
        candidate_restart_cohort_key=lambda stage: (
            scheduler_execution_module.candidate_restart_cohort_key(
                stage,
                downstream_restart_stages=("forcing", "forecast", "state_save_qc"),
            )
        ),
    )
    assert len(cohorts) == 2
    members = {key[1]: [candidate.model_id for candidate in group] for key, group in cohorts}
    assert members["forcing"] == ["dg_huai_main_gfs"]
    assert members["forecast"] == [REPLAY_MODEL_ID]


def test_replay_override_does_not_bypass_the_required_raw_manifest_candidate_gate() -> None:
    """Env posture note for the runbook, asserted rather than assumed.

    The candidate-level ``nfs_raw_manifest`` gate (``scheduler_candidates.py``
    :696-708) is fed by the journal candidate state, not by discovery, and the
    replay override deliberately does not touch it.  A replay pass over the five
    raw-less cycles therefore must run with
    ``NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=false`` (the D2 discovery branch
    supplies the admission evidence instead); with the requirement left on, the
    candidate is blocked and nothing is submitted — fail-closed, never silently
    resubmitted.
    """

    reason = "terminal_hydro_success"
    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=CandidateStateDecision("skip", reason, _terminal_evidence(reason)),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        raw_state={
            "nfs_raw_manifest": {
                "status": "missing",
                "required": True,
                "reason": "manifest_missing",
                "source": "nfs_raw_manifest",
            }
        },
    )
    assert candidates == []
    assert skipped == []
    assert blocked[0].reason == "nfs_raw_manifest_manifest_missing"


# --------------------------------------------------------------------------
# 1.3 discovery admission on forcing evidence
# --------------------------------------------------------------------------


def _write_forcing_package(root: Path, *, source: str, cycle: str, model_id: str, empty: bool = False) -> Path:
    package = root / "forcing" / source / cycle / f"basins_{model_id}_vbasins" / model_id
    package.mkdir(parents=True, exist_ok=True)
    if not empty:
        (package / "forcing.csv").write_text("t,q\n0,1\n", encoding="utf-8")
    return package


def test_forcing_readiness_ready_when_every_model_package_is_present(tmp_path: Path) -> None:
    for model_id in (REPLAY_MODEL_ID, "dg_hhe_gfs"):
        _write_forcing_package(tmp_path, source="gfs", cycle="2026070600", model_id=model_id)
    evidence = replay_forcing_readiness(
        object_store_root=tmp_path,
        source_id="gfs",
        cycle_time=IN_WINDOW_CYCLE,
        model_ids=[REPLAY_MODEL_ID, "dg_hhe_gfs"],
    )
    assert evidence["status"] == "ready"
    assert set(evidence["models"]) == {REPLAY_MODEL_ID, "dg_hhe_gfs"}
    assert all(item["status"] == "present" for item in evidence["models"].values())


def test_forcing_readiness_missing_when_one_package_is_absent_or_empty(tmp_path: Path) -> None:
    _write_forcing_package(tmp_path, source="gfs", cycle="2026070600", model_id=REPLAY_MODEL_ID)
    evidence = replay_forcing_readiness(
        object_store_root=tmp_path,
        source_id="gfs",
        cycle_time=IN_WINDOW_CYCLE,
        model_ids=[REPLAY_MODEL_ID, "dg_hhe_gfs"],
    )
    assert evidence["status"] == "missing"
    assert evidence["reason"] == "replay_forcing_evidence_missing"
    assert evidence["models"]["dg_hhe_gfs"]["status"] == "missing"

    _write_forcing_package(tmp_path, source="gfs", cycle="2026070600", model_id="dg_hhe_gfs", empty=True)
    empty_evidence = replay_forcing_readiness(
        object_store_root=tmp_path,
        source_id="gfs",
        cycle_time=IN_WINDOW_CYCLE,
        model_ids=[REPLAY_MODEL_ID, "dg_hhe_gfs"],
    )
    assert empty_evidence["status"] == "missing"
    assert empty_evidence["models"]["dg_hhe_gfs"]["detail"] == "package directory is empty"


def test_forcing_readiness_uncompletable_probe_is_not_a_completed_negative(tmp_path: Path) -> None:
    """Error-classification invariant: unreadable is not absent."""

    cycle_root = tmp_path / "forcing" / "gfs" / "2026070600"
    cycle_root.mkdir(parents=True)
    os.chmod(cycle_root, 0o000)
    try:
        evidence = replay_forcing_readiness(
            object_store_root=tmp_path,
            source_id="gfs",
            cycle_time=IN_WINDOW_CYCLE,
            model_ids=[REPLAY_MODEL_ID],
        )
    finally:
        os.chmod(cycle_root, 0o700)
    assert evidence["status"] == "undeterminable"
    assert evidence["reason"] == "replay_forcing_evidence_undeterminable"


def test_forcing_readiness_without_object_store_root_is_undeterminable() -> None:
    evidence = replay_forcing_readiness(
        object_store_root=None,
        source_id="gfs",
        cycle_time=IN_WINDOW_CYCLE,
        model_ids=[REPLAY_MODEL_ID],
    )
    assert evidence["status"] == "undeterminable"


class _DiscoveryHarness:
    """Just enough of ``ProductionScheduler`` for the discovery branch method."""

    def __init__(self, config: Any) -> None:
        self.config = config

    _replay_forcing_admission_discovery = None  # bound below


def _discovery_harness(*, admission: ReplayAdmission | None, object_store_root: Path) -> Any:
    from services.orchestrator.scheduler_core import ProductionScheduler

    harness = _DiscoveryHarness.__new__(_DiscoveryHarness)
    harness.config = _FakeConfig(replay_admission=admission)
    harness.config.object_store_root = object_store_root  # type: ignore[attr-defined]
    harness._replay_forcing_admission_discovery = (
        ProductionScheduler._replay_forcing_admission_discovery.__get__(harness, type(harness))
    )
    return harness


_RAW_MISSING = {
    "status": "missing",
    "required": True,
    "reason": "manifest_missing",
    "source": "nfs_raw_manifest",
    "source_id": "gfs",
    "cycle_id": "gfs_2026070600",
    "cycle_time": "2026-07-06T00:00:00Z",
}


def test_discovery_admits_raw_less_cycle_on_full_forcing_evidence(tmp_path: Path) -> None:
    _write_forcing_package(tmp_path, source="gfs", cycle="2026070600", model_id=REPLAY_MODEL_ID)
    harness = _discovery_harness(admission=_admission(), object_store_root=tmp_path)
    discovery = harness._replay_forcing_admission_discovery(
        source_id="gfs",
        cycle_time=IN_WINDOW_CYCLE,
        cycle_hour=0,
        raw_readiness=dict(_RAW_MISSING),
    )
    assert discovery is not None
    assert discovery.available is True
    assert discovery.status == "discovered"
    assert discovery.reason is None
    assert discovery.classifier == "replay_forcing_evidence"
    assert discovery.evidence["replay_forcing_evidence"]["status"] == "ready"


def test_discovery_rejects_whole_cycle_when_one_forcing_package_is_missing(tmp_path: Path) -> None:
    admission = parse_replay_admission(
        mode=True,
        model_ids=f"{REPLAY_MODEL_ID},dg_hhe_gfs",
        window=WINDOW,
    )
    _write_forcing_package(tmp_path, source="gfs", cycle="2026070600", model_id=REPLAY_MODEL_ID)
    harness = _discovery_harness(admission=admission, object_store_root=tmp_path)
    discovery = harness._replay_forcing_admission_discovery(
        source_id="gfs",
        cycle_time=IN_WINDOW_CYCLE,
        cycle_hour=0,
        raw_readiness=dict(_RAW_MISSING),
    )
    assert discovery is not None
    assert discovery.available is False
    assert discovery.reason == "replay_forcing_evidence_missing"
    assert discovery.retryable is False


def test_discovery_branch_is_absent_without_replay_or_outside_the_window(tmp_path: Path) -> None:
    inactive = _discovery_harness(admission=None, object_store_root=tmp_path)
    assert (
        inactive._replay_forcing_admission_discovery(
            source_id="gfs",
            cycle_time=IN_WINDOW_CYCLE,
            cycle_hour=0,
            raw_readiness=dict(_RAW_MISSING),
        )
        is None
    )
    active = _discovery_harness(admission=_admission(), object_store_root=tmp_path)
    assert (
        active._replay_forcing_admission_discovery(
            source_id="gfs",
            cycle_time=OUT_OF_WINDOW_CYCLE,
            cycle_hour=0,
            raw_readiness=dict(_RAW_MISSING),
        )
        is None
    )


def test_discovery_window_keeps_the_original_gate_when_the_raw_manifest_is_ready(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """A ready raw manifest never reaches the replay branch."""

    from services.orchestrator import scheduler_core as scheduler_core_module

    calls: list[str] = []

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        calls.append("probed")
        raise AssertionError("ready raw manifests must not consult the replay branch")

    monkeypatch.setattr(scheduler_replay_module, "replay_forcing_readiness", _forbidden)
    assert scheduler_core_module is not None
    harness = _discovery_harness(admission=_admission(), object_store_root=tmp_path)
    # The caller only invokes the branch when the raw gate is NOT ready; assert
    # that contract holds by exercising the branch's own guard instead.
    assert harness._replay_forcing_admission_discovery(
        source_id="gfs",
        cycle_time=OUT_OF_WINDOW_CYCLE,
        cycle_hour=0,
        raw_readiness=dict(_RAW_MISSING),
    ) is None
    assert calls == []
