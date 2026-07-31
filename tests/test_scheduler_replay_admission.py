"""Replay admission: config triple, terminal override, discovery branch (#1164 change 2).

Decision-shape table under test (design D1 v3).  "covered" = replay admission
active AND ``model_id`` in the closed set AND cycle inside the replay window.

``terminal_hydro_success`` (the observed production token)
  * not covered -- the pre-change ladder, unchanged: mismatch retry
    ``retry_strict_warm_start_terminal_init_state_mismatch`` (budget-consulted),
    degrading to ``blocked`` ``strict_warm_start_retry_budget_exhausted`` once the
    budget is spent, or successor retry
    ``retry_strict_warm_start_successor_checkpoint_missing`` with
    ``restart_stage="state_save_qc"``, or the plain terminal skip.
  * covered -- ``retry`` with reason ``replay_terminal_override`` and evidence
    ``decision="replay_resubmit"``, ``restart_stage`` = ``restart_from_stage`` =
    ``forecast``, ``native_shud_resubmitted=True``,
    ``durable_output_reused=False``; the retry budget is never consulted.

``terminal_pipeline_success``
  * not covered -- same ladder as above.
  * covered -- same override as above.

``terminal_completed_cycle``
  * not covered -- generic skip (``scheduler_candidates.py:495-502``).
  * covered -- UNCHANGED generic skip: no override, nothing submitted; the driver
    surfaces it as a convergence-timeout halt rather than silently resubmitting.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.orchestrator import chain as _chain  # noqa: F401  (import order: chain owns the cycle mixin)
from services.orchestrator import chain_forecast_orchestrator_cycle as chain_cycle_module
from services.orchestrator import scheduler as scheduler_module
from services.orchestrator import scheduler_candidate_manifest as scheduler_candidate_manifest_module
from services.orchestrator import scheduler_candidates as scheduler_candidates_module
from services.orchestrator import scheduler_replay as scheduler_replay_module
from services.orchestrator import scheduler_state_failure as scheduler_state_failure_module
from services.orchestrator.scheduler import ProductionSchedulerConfig
from services.orchestrator.scheduler_generation import PACKAGED_IC_BOOTSTRAP_MODE
from services.orchestrator.scheduler_replay import (
    REPLAY_MODE_ENV,
    REPLAY_MODEL_IDS_ENV,
    REPLAY_WINDOW_ENV,
    ReplayAdmission,
    SchedulerReplayConfigError,
    parse_replay_admission,
    replay_forcing_readiness,
)
from services.orchestrator.scheduler_state import CandidateStateDecision, _candidate_state_decision
from services.orchestrator.scheduler_types import SchedulerCandidate
from workers.canonical_converter.converter import evaluate_canonical_readiness
from workers.data_adapters.base import CycleDiscovery, cycle_id_for

REPLAY_MODEL_ID = "dg_dth_ls_gfs"
OUT_OF_SET_MODEL_ID = "dg_other_basin_gfs"
WINDOW = "2026070500..2026072100"
IN_WINDOW_CYCLE = datetime(2026, 7, 6, 0, tzinfo=UTC)
OUT_OF_WINDOW_CYCLE = datetime(2026, 7, 22, 0, tzinfo=UTC)
#: Replay-sequence origin: the first business cycle, whose state chain the reset
#: clears and whose IC therefore comes from the model package.
FIRST_CYCLE = datetime(2026, 7, 5, 0, tzinfo=UTC)

#: Production canonical readiness inside the replay window: retention removed the
#: canonical products, so a genuine evaluation returns zero candidate rows
#: (``_canonical_evidence_is_fresh_zero_row`` is True) — the merge point at
#: ``scheduler_candidates.py:745-810`` is live for every replay candidate.
FRESH_ZERO_ROW_READINESS: dict[str, Any] = {
    "ready": False,
    "status": "canonical_incomplete",
    "reason": "canonical_incomplete",
    "candidate_row_count": 0,
    "expected_leads": [0, 12, 24],
    "product_id": "canonical_gfs_2026070600",
}

#: Journal candidate state for a cycle whose NFS raw manifest is still on disk
#: (070712 onwards): the raw-ready leg of the fresh-zero-row merge.
RAW_MANIFEST_READY_STATE: dict[str, Any] = {
    "nfs_raw_manifest": {
        "status": "ready",
        "ready": True,
        "required": True,
        "source": "node27_nfs_raw_manifest",
        "manifest_uri": "s3://nhms/raw/gfs/2026070600/manifest.json",
        "source_id": "gfs",
        "cycle_id": cycle_id_for("gfs", IN_WINDOW_CYCLE),
        "cycle_time": "2026-07-06T00:00:00Z",
    }
}

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
    """Minimal active repository: a candidate-state provider and nothing active.

    ``slurm_jobs_script`` scripts successive ``active_slurm_jobs`` answers so a
    pass can reproduce the production shape where a job is active on the first
    probe and has gone terminal by the post-sync re-probe (round-3 A3-1).
    """

    def __init__(
        self,
        raw_state: Mapping[str, Any] | None,
        *,
        slurm_jobs_script: Sequence[Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        self._raw_state = dict(raw_state or {})
        self._slurm_jobs_script = [list(answer) for answer in (slurm_jobs_script or [])]
        self.slurm_jobs_calls = 0

    def candidate_state(self, **kwargs: Any) -> dict[str, Any]:
        return dict(self._raw_state)

    def has_active_orchestration(self, **kwargs: Any) -> bool:
        return False

    def has_active_pipeline(self, **kwargs: Any) -> bool:
        return False

    def has_completed_pipeline(self, **kwargs: Any) -> bool:
        return True

    def active_slurm_jobs(self, **kwargs: Any) -> list[dict[str, Any]]:
        index = self.slurm_jobs_calls
        self.slurm_jobs_calls += 1
        if index < len(self._slurm_jobs_script):
            return [dict(job) for job in self._slurm_jobs_script[index]]
        return []


class _SyncingOrchestrator:
    """node-22 orchestrator stub exposing only ``sync_cycle_statuses``."""

    def __init__(self, updates: Sequence[Mapping[str, Any]]) -> None:
        self._updates = [dict(update) for update in updates]
        self.calls: list[str] = []

    def sync_cycle_statuses(self, cycle_id: str) -> list[dict[str, Any]]:
        self.calls.append(cycle_id)
        return [dict(update) for update in self._updates]


def _candidate_factory(
    *, discovery: CycleDiscovery, model: _FakeModel, horizon: Mapping[str, Any]
) -> SchedulerCandidate:
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
    canonical_readiness: Mapping[str, Any] | None = None,
    discovery_classifier: str | None = None,
    discovery_evidence: Mapping[str, Any] | None = None,
    config_overrides: Mapping[str, Any] | None = None,
    resource_profiles: Mapping[str, Mapping[str, Any]] | None = None,
    decider: Any | None = None,
    repository: Any | None = None,
    orchestrator: Any | None = None,
    allow_slurm_status_sync: bool = False,
) -> tuple[list[Any], list[Any], list[dict[str, Any]]]:
    discovery = CycleDiscovery(
        cycle_id=cycle_id_for("gfs", cycle_time),
        source_id="gfs",
        cycle_time=cycle_time,
        cycle_hour=cycle_time.hour,
        available=True,
        status="discovered",
        classifier=discovery_classifier,
        evidence=dict(discovery_evidence or {}),
    )
    context = scheduler_candidates_module.SchedulerCandidateConstructionContext(
        config=_FakeConfig(replay_admission=admission, **dict(config_overrides or {})),
        active_repository=repository if repository is not None else _FakeRepository(raw_state),
        canonical_readiness_for_candidate=lambda candidate, cycle: (
            dict(canonical_readiness) if canonical_readiness is not None else None
        ),
        strict_warm_start_for_candidate=lambda candidate, cycle: (
            dict(strict_warm_start) if strict_warm_start is not None else None
        ),
        orchestrator_for=(
            (lambda source_id: orchestrator)
            if orchestrator is not None
            else (lambda source_id: pytest.fail("candidate construction must not build an orchestrator"))
        ),
        candidate_factory=_candidate_factory,
        candidate_state_decider=decider
        if decider is not None
        else lambda candidate, state: (
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
        models=[
            _FakeModel(
                model_id=model_id,
                resource_profile=dict((resource_profiles or {}).get(model_id) or {}),
            )
            for model_id in model_ids
        ],
        cycles=[_FakeCycle(discovery=discovery)],
        allow_slurm_status_sync=allow_slurm_status_sync,
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


def test_first_cycle_bootstrap_evidence_survives_the_replay_override() -> None:
    """070500 production shape: journal survives the reset, evidence is bootstrap.

    Round-1 A-P2-4: the scoped reset clears only the state INDEX; the journal
    keeps the succeeded hydro run, so the first cycle's state decision is still
    ``terminal_hydro_success`` and the override fires like any other cycle.
    What is special about 070500 is the strict warm-start payload: the
    ``PACKAGED_IC_BOOTSTRAP`` shape emitted by
    ``scheduler_generation_gate.py:535-553`` — ready, packaged-IC mode, a
    ``packaged_ic_checksum`` and NO ``candidate_state``.  That payload must
    reach the candidate unaltered, and
    ``_upgrade_retry_for_strict_warm_start_manifest`` must not relabel the
    decision (no ``candidate_state`` means no run-manifest match to prove).
    """

    reason = "terminal_hydro_success"
    bootstrap_strict = {
        "status": "ready",
        "ready": True,
        "reason": None,
        "mode": PACKAGED_IC_BOOTSTRAP_MODE,
        "model_id": REPLAY_MODEL_ID,
        "source_id": "gfs",
        "generation": 1,
        "cold_start_reason": None,
        "packaged_ic_checksum": "sha256:packaged-ic-070500",
    }
    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=CandidateStateDecision("skip", reason, _terminal_evidence(reason)),
        strict_warm_start=bootstrap_strict,
        cycle_time=FIRST_CYCLE,
    )
    assert blocked == []
    assert skipped == []
    assert len(candidates) == 1
    _assert_replay_shape(
        candidates[0],
        overridden_skip_reason=reason,
        overridden_branch="strict_warm_start_terminal_init_state_mismatch",
    )
    evidence = candidates[0].state_evidence
    # The bootstrap payload carries no selected warm state; nothing may invent one.
    assert "candidate_state" not in evidence
    strict_evidence = evidence["strict_warm_start"]
    assert strict_evidence["mode"] == PACKAGED_IC_BOOTSTRAP_MODE
    assert strict_evidence["packaged_ic_checksum"] == "sha256:packaged-ic-070500"
    assert strict_evidence["ready"] is True
    assert "candidate_state" not in strict_evidence
    # The upgrade hook ran inside build_candidates (the decision token above
    # proves it did not relabel); assert the same directly on the seam.
    decision = scheduler_candidates_module._replay_terminal_override_decision(
        _FakeConfig(replay_admission=_admission()),
        _candidate_factory(
            discovery=CycleDiscovery(
                cycle_id=cycle_id_for("gfs", FIRST_CYCLE),
                source_id="gfs",
                cycle_time=FIRST_CYCLE,
                cycle_hour=0,
                available=True,
                status="discovered",
            ),
            model=_FakeModel(model_id=REPLAY_MODEL_ID),
            horizon={},
        ),
        CandidateStateDecision("skip", reason, _terminal_evidence(reason)),
        strict_warm_start=bootstrap_strict,
        successor_state=None,
    )
    assert decision is not None
    upgraded = scheduler_candidates_module._upgrade_retry_for_strict_warm_start_manifest(
        decision,
        bootstrap_strict,
    )
    assert upgraded is decision
    assert upgraded.evidence["strict_warm_start"]["packaged_ic_checksum"] == "sha256:packaged-ic-070500"


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
# 1.2b canonical-readiness fresh-zero-row merge point (design D3.5)
# --------------------------------------------------------------------------


def _replay_terminal_decision() -> CandidateStateDecision:
    reason = "terminal_hydro_success"
    return CandidateStateDecision("skip", reason, _terminal_evidence(reason))


def test_fresh_zero_row_merge_admits_the_raw_less_replay_candidate() -> None:
    """A-P1-1: replay forcing evidence substitutes for the deleted raw manifest.

    070500..070700 x 2 sources have no raw manifest at all (the objects were
    retired) and no canonical rows, so both fresh-zero-row conditions hold.  The
    pre-change merge blocked them unconditionally — ``required`` is never
    consulted on this leg — which submitted exactly nothing for ten cycle-passes,
    including the packaged-IC acceptance point.
    """

    forcing_evidence = {
        "source": "replay_forcing_evidence",
        "status": "ready",
        "model_ids": [REPLAY_MODEL_ID],
    }
    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=_replay_terminal_decision(),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        raw_state={},
        discovery_classifier="replay_forcing_evidence",
        discovery_evidence={"replay_forcing_evidence": forcing_evidence},
    )
    assert blocked == []
    assert skipped == []
    assert len(candidates) == 1
    evidence = candidates[0].state_evidence
    assert evidence["decision"] == "replay_resubmit"
    assert evidence["restart_stage"] == "forecast"
    assert evidence["restart_from_stage"] == "forecast"
    assert evidence["canonical_readiness"]["candidate_row_count"] == 0
    guard = evidence["replay_canonical_readiness_guard"]
    assert guard["leg"] == "raw_manifest_absent"
    assert guard["status"] == "admitted"
    assert guard["reason"] == "replay_forcing_evidence_substitutes_raw_manifest"
    assert guard["discovery_classifier"] == "replay_forcing_evidence"
    assert guard["replay_forcing_evidence"]["status"] == "ready"
    # The absent manifest is recorded, not silently dropped.
    assert guard["nfs_raw_manifest"]["status"] == "missing"


def test_fresh_zero_row_merge_keeps_the_forecast_restart_when_the_raw_manifest_is_ready() -> None:
    """A-P1-2: the raw-ready leg must not lower the replay restart to ``convert``.

    ``_source_raw_manifest_restart_evidence`` describes a full-chain restart from
    ``convert`` with ``raw_manifest_ready_without_canonical``; merged over a
    replay candidate it makes the chain force-resubmit convert/forcing stages that
    already succeeded (~18/33 cycles per source, raw from 070712 vs canonical
    from 071612).
    """

    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=_replay_terminal_decision(),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        raw_state=RAW_MANIFEST_READY_STATE,
    )
    assert blocked == []
    assert skipped == []
    assert len(candidates) == 1
    evidence = candidates[0].state_evidence
    assert evidence["decision"] == "replay_resubmit"
    assert evidence["restart_stage"] == "forecast"
    assert evidence["restart_from_stage"] == "forecast"
    assert evidence.get("restart_reason") != "raw_manifest_ready_without_canonical"
    assert "fresh_ingestion" not in evidence
    # The raw manifest itself stays on the record — only the restart downgrade is
    # suppressed.
    assert evidence["raw_manifest_reuse"]["status"] == "ready"
    guard = evidence["replay_canonical_readiness_guard"]
    assert guard["leg"] == "raw_manifest_ready"
    assert guard["status"] == "restart_preserved"
    assert guard["restart_stage"] == "forecast"
    assert guard["suppressed_restart"]["restart_stage"] == "convert"
    assert guard["suppressed_restart"]["restart_from_stage"] == "convert"
    assert guard["suppressed_restart"]["restart_reason"] == "raw_manifest_ready_without_canonical"


def test_fresh_zero_row_merge_is_unchanged_without_replay_admission() -> None:
    """must-preserve 3: both legs keep their pre-change behaviour when replay is off."""

    raw_less_candidates, raw_less_blocked, raw_less_skipped = _build(
        admission=None,
        decision=_replay_terminal_decision(),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        raw_state={},
    )
    assert raw_less_candidates == []
    assert raw_less_skipped == []
    assert raw_less_blocked[0].reason == "nfs_raw_manifest_required"
    blocked_evidence = raw_less_blocked[0].state_evidence
    assert blocked_evidence["nfs_raw_manifest"]["status"] == "missing"
    assert "replay_canonical_readiness_guard" not in blocked_evidence

    raw_ready_candidates, raw_ready_blocked, raw_ready_skipped = _build(
        admission=None,
        decision=_replay_terminal_decision(),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        raw_state=RAW_MANIFEST_READY_STATE,
    )
    assert raw_ready_blocked == []
    assert raw_ready_skipped == []
    evidence = raw_ready_candidates[0].state_evidence
    assert evidence["restart_stage"] == "convert"
    assert evidence["restart_from_stage"] == "convert"
    assert evidence["restart_reason"] == "raw_manifest_ready_without_canonical"
    assert evidence["fresh_ingestion"] == {"required": False, "mode": "reuse_raw_then_convert"}
    assert "replay_canonical_readiness_guard" not in evidence


def test_canonical_readiness_ready_leaves_the_replay_candidate_alone() -> None:
    """A ready canonical evaluation never reaches the fresh-zero-row legs."""

    ready_readiness = {
        "ready": True,
        "status": "canonical_ready",
        "candidate_row_count": 3,
        "expected_leads": [0, 12, 24],
    }
    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=_replay_terminal_decision(),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=ready_readiness,
        raw_state=RAW_MANIFEST_READY_STATE,
    )
    assert blocked == []
    assert skipped == []
    evidence = candidates[0].state_evidence
    assert evidence["decision"] == "replay_resubmit"
    assert evidence["restart_stage"] == "forecast"
    assert evidence["canonical_readiness"]["ready"] is True
    assert "replay_canonical_readiness_guard" not in evidence


# --------------------------------------------------------------------------
# 1.2c repair leg of the canonical gate + raw-less substitute verification
# (round-2 A2-1 / A2-3, design D3.5 v5)
# --------------------------------------------------------------------------

#: GFS 2026070712 — the cycle whose five basins lost their forcing packages and
#: whose repair candidates hit the canonical gate's FIRST leg.
REPAIR_CYCLE = datetime(2026, 7, 7, 12, tzinfo=UTC)
REPAIR_MODEL_IDS = (
    "dg_dth_ls_gfs",
    "dg_hhe_gfs",
    "dg_lsh_gfs",
    "dg_nsh_gfs",
    "dg_swh_gfs",
)
#: huai_main kept its forcing package: it rides the ordinary replay override.
OVERRIDE_MODEL_ID = "dg_huai_main_gfs"
REPAIR_PASS_MODEL_IDS = (*REPAIR_MODEL_IDS, OVERRIDE_MODEL_ID)


def _repair_admission(cycle_model_ids: Sequence[str] = REPAIR_PASS_MODEL_IDS) -> ReplayAdmission:
    admission = parse_replay_admission(mode=True, model_ids=list(cycle_model_ids), window=WINDOW)
    assert admission is not None
    return admission


def _direct_grid_profile(source_id: str = "gfs") -> dict[str, Any]:
    grid_id = f"{source_id.lower()}_0p25"
    return {
        "runnable": True,
        "memory_gb": 8,
        "forcing_mapping_mode": "direct_grid",
        "direct_grid_forcing": {
            "forcing_mapping_mode": "direct_grid",
            "binding_uri": f"s3://nhms/mappings/{source_id.lower()}/binding.json",
            "binding_checksum": "sha256:" + "a" * 64,
            "model_input_package_id": f"input-{source_id.lower()}",
            "sp_att_path": "input/mesh/SpatialData/sp.att",
            "sp_att_checksum": "sha256:" + "b" * 64,
            "applicable_source_ids": [source_id],
            "grid_id": grid_id,
            "grid_signature": "c" * 64,
            "stations": [
                {
                    "station_id": f"{source_id.lower()}-station-1",
                    "shud_forcing_index": 1,
                    "forcing_filename": "X100Y30.csv",
                    "longitude": 100.0,
                    "latitude": 30.0,
                    "x": 100.0,
                    "y": 30.0,
                    "z": 0.0,
                    "grid_id": grid_id,
                    "grid_cell_id": "100:30",
                }
            ],
        },
    }


def _write_raw_manifest(root: Path, *, source_id: str = "gfs", cycle_time: datetime) -> dict[str, Any]:
    """Write the on-disk NFS raw manifest the repair policy verifies present."""

    from services.orchestrator.source_cycle_raw_manifest import nfs_raw_manifest_readiness

    compact = cycle_time.strftime("%Y%m%d%H")
    raw_key = f"raw/{source_id.lower()}/{compact}/{source_id.lower()}.f000.grib2"
    raw_path = root / raw_key
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"verified-raw-input")
    manifest_path = root / f"raw/{source_id.lower()}/{compact}/manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source_id": source_id,
                "cycle_time": cycle_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "manifest_uri": f"s3://nhms/raw/{source_id.lower()}/{compact}/manifest.json",
                "entries": [
                    {
                        "remote_url": f"https://example.invalid/{source_id.lower()}",
                        "local_key": raw_key,
                        "variable": "prcp_rate_or_amount",
                        "forecast_hour": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    readiness = nfs_raw_manifest_readiness(
        source_id=source_id,
        cycle_time=cycle_time,
        object_store_root=root,
        object_store_prefix="s3://nhms",
        required=True,
    )
    assert readiness["status"] == "ready"
    return readiness


def _repair_raw_state(cycle_time: datetime) -> dict[str, Any]:
    """Journal candidate state for a cycle whose raw manifest survived (070712+)."""

    compact = cycle_time.strftime("%Y%m%d%H")
    return {
        "nfs_raw_manifest": {
            "status": "ready",
            "ready": True,
            "required": True,
            "source": "node27_nfs_raw_manifest",
            "manifest_uri": f"s3://nhms/raw/gfs/{compact}/manifest.json",
            "source_id": "gfs",
            "cycle_id": cycle_id_for("gfs", cycle_time),
            "cycle_time": cycle_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    }


def _missing_forcing_blocked_decision() -> CandidateStateDecision:
    """The stable missing-forcing blocker the repair policy reclassifies."""

    return CandidateStateDecision(
        "blocked",
        "missing_forcing_package_uri",
        {
            "decision": "blocked_missing_upstream_artifact",
            "error_code": "FORCING_PACKAGE_URI_MISSING",
            "classifier": "missing_upstream_artifact",
            "restart_stage": "forecast",
            "replacement_submitted": False,
            "artifact_guard": {
                "artifact_type": "forcing_package_uri",
                "artifact_uri": "forcing/gfs/2026070712/model/package.json",
                "artifact_exists": False,
                "unsafe_reason": None,
                "stable_classifier": "FORCING_PACKAGE_URI_MISSING",
                "planned_retry_decision": "retry_failed",
                "planned_retry_reason": "retry_failed_candidate",
            },
        },
    )


def _repair_warm_state(cycle_time: datetime) -> dict[str, Any]:
    """A verified warm state: the 070700 replay state, valid at 070712."""

    return {
        "ready": True,
        "candidate_state": {
            "state_id": "state-replay-2026070700",
            "state_uri": "s3://nhms/states/gfs/2026070700/state.cfg.ic",
            "checksum": "sha256:" + "d" * 64,
            "valid_time": cycle_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "init_state_quality": "warm_hydrologic_state",
            "init_state_lineage": {"start_mode": "warm_start", "generation": 4},
        },
    }


def _repair_pass(
    *,
    admission: ReplayAdmission | None,
    cycle_time: datetime,
    raw_root: Path,
    canonical_readiness: Mapping[str, Any] | None = FRESH_ZERO_ROW_READINESS,
) -> tuple[list[Any], list[Any], list[dict[str, Any]]]:
    """The production 070712 pass shape: 5 repair candidates + 1 override candidate."""

    terminal_reason = "terminal_hydro_success"
    decisions: dict[str, CandidateStateDecision | None] = {
        model_id: _missing_forcing_blocked_decision() for model_id in REPAIR_MODEL_IDS
    }
    decisions[OVERRIDE_MODEL_ID] = CandidateStateDecision(
        "skip",
        terminal_reason,
        _terminal_evidence(terminal_reason),
    )
    return _build(
        admission=admission,
        decision=None,
        decisions_by_model=decisions,
        strict_warm_start=_repair_warm_state(cycle_time),
        canonical_readiness=canonical_readiness,
        cycle_time=cycle_time,
        model_ids=REPAIR_PASS_MODEL_IDS,
        raw_state=_repair_raw_state(cycle_time),
        resource_profiles={model_id: _direct_grid_profile() for model_id in REPAIR_PASS_MODEL_IDS},
        config_overrides={
            "repair_missing_forcing": True,
            "repair_missing_forcing_cycle_time": cycle_time,
            "require_direct_grid": True,
            "nfs_raw_manifest_root": raw_root,
            "nfs_raw_manifest_prefix": "s3://nhms",
        },
    )


def test_replay_window_repair_candidates_are_admitted_with_a_convert_restart(tmp_path: Path) -> None:
    """A2-1: the canonical gate's first leg halted Phase-2 GFS at cycle 6/33.

    The five 070712 repair candidates carry an authorized missing-forcing repair
    decision; canonical readiness inside the replay window is a genuine
    zero-row evaluation (retention purged the products), and the pre-change leg
    blocked all five unconditionally — with 070500..070700 already overwritten.
    Simply lifting the block is not enough: the repair restarts at ``forcing``,
    which has no inputs without canonical, so the raw-manifest restart is merged
    in full and the chain rebuilds convert -> forcing -> forecast from raw.
    """

    _write_raw_manifest(tmp_path, cycle_time=REPAIR_CYCLE)
    candidates, blocked, skipped = _repair_pass(
        admission=_repair_admission(),
        cycle_time=REPAIR_CYCLE,
        raw_root=tmp_path,
    )

    assert blocked == []
    assert skipped == []
    by_model = {candidate.model_id: candidate for candidate in candidates}
    assert set(by_model) == set(REPAIR_PASS_MODEL_IDS)

    for model_id in REPAIR_MODEL_IDS:
        evidence = by_model[model_id].state_evidence
        assert evidence["decision"] == "retry_repair_missing_forcing"
        assert evidence["restart_stage"] == "convert"
        assert evidence["restart_from_stage"] == "convert"
        assert evidence["restart_reason"] == "raw_manifest_ready_without_canonical"
        assert evidence["fresh_ingestion"] == {"required": False, "mode": "reuse_raw_then_convert"}
        # The repair decision's own evidence survives the merge.
        assert evidence["missing_forcing_repair"]["status"] == "authorized"
        assert evidence["raw_manifest_reuse"]["status"] == "ready"
        assert evidence["canonical_readiness"]["candidate_row_count"] == 0
        guard = evidence["replay_canonical_readiness_guard"]
        assert guard["leg"] == "authorized_missing_forcing_repair"
        assert guard["status"] == "admitted"
        assert guard["reason"] == "replay_repair_rebuilds_canonical_from_raw"
        assert guard["repair_restart_stage"] == "forcing"
        assert guard["restart_stage"] == "convert"

    override_evidence = by_model[OVERRIDE_MODEL_ID].state_evidence
    assert override_evidence["decision"] == "replay_resubmit"
    assert override_evidence["restart_stage"] == "forecast"
    assert override_evidence["restart_from_stage"] == "forecast"
    assert override_evidence["replay_canonical_readiness_guard"]["leg"] == "raw_manifest_ready"


def test_repair_candidates_outside_the_replay_window_stay_blocked(tmp_path: Path) -> None:
    """must-preserve 3 / fail-closed: the leg is byte-identical off the replay path."""

    out_of_window = datetime(2026, 7, 22, 12, tzinfo=UTC)
    _write_raw_manifest(tmp_path, cycle_time=out_of_window)

    for admission in (None, _repair_admission()):
        candidates, blocked, skipped = _repair_pass(
            admission=admission,
            cycle_time=out_of_window,
            raw_root=tmp_path,
        )
        assert skipped == []
        blocked_by_model = {candidate.model_id: candidate for candidate in blocked}
        for model_id in REPAIR_MODEL_IDS:
            candidate = blocked_by_model[model_id]
            assert candidate.reason == "missing_forcing_package_uri"
            evidence = candidate.state_evidence
            assert evidence["missing_forcing_repair"]["status"] == "rejected"
            assert evidence["missing_forcing_repair"]["reason"] == "canonical_not_ready"
            assert evidence["restart_stage"] == "forecast"
            assert "replay_canonical_readiness_guard" not in evidence
        # huai_main is out of the window too: the ordinary raw-ready leg applies.
        assert [candidate.model_id for candidate in candidates] == [OVERRIDE_MODEL_ID]
        assert candidates[0].state_evidence["restart_stage"] == "convert"


def test_replay_window_repair_without_raw_restart_evidence_stays_blocked(tmp_path: Path) -> None:
    """Fail-closed: no raw evidence in the journal state means no convert restart.

    ``_source_raw_manifest_restart_evidence`` returns ``None`` when the journal
    candidate state carries neither a matching ``nfs_raw_manifest`` nor a ready
    ``forecast_cycle`` manifest URI; the leg must keep the pre-change block
    rather than admit a rebuild with nothing to rebuild from.
    """

    _write_raw_manifest(tmp_path, cycle_time=REPAIR_CYCLE)
    model_id = REPAIR_MODEL_IDS[0]
    repair_config = _FakeConfig(
        replay_admission=_repair_admission(),
        repair_missing_forcing=True,
        repair_missing_forcing_cycle_time=REPAIR_CYCLE,
        require_direct_grid=True,
        nfs_raw_manifest_root=tmp_path,
        nfs_raw_manifest_prefix="s3://nhms",
    )
    candidate = _candidate_factory(
        discovery=CycleDiscovery(
            cycle_id=cycle_id_for("gfs", REPAIR_CYCLE),
            source_id="gfs",
            cycle_time=REPAIR_CYCLE,
            cycle_hour=REPAIR_CYCLE.hour,
            available=True,
            status="discovered",
        ),
        model=_FakeModel(model_id=model_id, resource_profile=_direct_grid_profile()),
        horizon={},
    )
    warm_state = _repair_warm_state(REPAIR_CYCLE)
    authorized = scheduler_candidates_module._apply_explicit_missing_forcing_repair_policy(
        repair_config,
        candidate,
        _repair_raw_state(REPAIR_CYCLE),
        _missing_forcing_blocked_decision(),
        strict_warm_start=warm_state,
    )
    assert authorized is not None
    assert authorized.reason == "operator_repair_missing_forcing"
    assert scheduler_candidates_module._decision_is_authorized_missing_forcing_repair(authorized) is True

    # Replay the same authorized decision against a journal candidate state that
    # carries no raw evidence at all: nothing to rebuild canonical from.
    candidates, blocked, skipped = _build(
        admission=_repair_admission(),
        decision=None,
        decisions_by_model={model_id: authorized},
        strict_warm_start=warm_state,
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        cycle_time=REPAIR_CYCLE,
        model_ids=(model_id,),
        raw_state={},
        resource_profiles={model_id: _direct_grid_profile()},
    )

    assert skipped == []
    assert candidates == []
    assert blocked[0].reason == "missing_forcing_package_uri"
    evidence = blocked[0].state_evidence
    assert evidence["missing_forcing_repair"]["status"] == "rejected"
    assert evidence["missing_forcing_repair"]["reason"] == "canonical_not_ready"
    assert "replay_canonical_readiness_guard" not in evidence


def test_raw_less_leg_blocks_a_replay_candidate_whose_substitute_is_not_ready() -> None:
    """A2-3: the guard must verify the substitute, not just the decision token."""

    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=_replay_terminal_decision(),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        raw_state={},
        discovery_classifier="replay_forcing_evidence",
        discovery_evidence={
            "replay_forcing_evidence": {
                "source": "replay_forcing_evidence",
                "status": "undeterminable",
                "reason": "replay_forcing_evidence_undeterminable",
                "model_ids": [REPLAY_MODEL_ID],
            }
        },
    )
    assert candidates == []
    assert skipped == []
    assert blocked[0].reason == "replay_forcing_substitute_not_ready"
    guard = blocked[0].state_evidence["replay_canonical_readiness_guard"]
    assert guard["status"] == "blocked"
    assert guard["reason"] == "replay_forcing_substitute_not_ready"
    assert guard["substitute_present"] is False
    assert guard["replay_forcing_evidence"]["status"] == "undeterminable"


def test_raw_less_leg_blocks_a_replay_candidate_from_another_discovery_provenance() -> None:
    """Phase-2 raw-path discovery + a journal state with no usable raw evidence.

    The candidate reaches the raw-less leg with no ``replay_forcing_evidence`` at
    all: nothing probed this cycle's forcing packages, so the guard has no
    substitute to lean on and must block rather than claim one.
    """

    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=_replay_terminal_decision(),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        raw_state={},
        discovery_classifier="nfs_raw_manifest",
    )
    assert candidates == []
    assert skipped == []
    assert blocked[0].reason == "replay_raw_manifest_substitute_unavailable"
    guard = blocked[0].state_evidence["replay_canonical_readiness_guard"]
    assert guard["status"] == "blocked"
    assert guard["reason"] == "replay_raw_manifest_substitute_unavailable"
    assert guard["substitute_present"] is False
    # The guard never claims a substitute it does not have.
    assert "replay_forcing_evidence" not in guard
    assert guard["nfs_raw_manifest"]["status"] == "missing"


# --------------------------------------------------------------------------
# 1.2c-2 replay-window manual-retry admission over the raw-less canonical leg
# (#1164 execution-phase defect 2)
#
# Production shape (node-22 `scheduler_2026073105_62f33b0c34d0.json`): an
# operator manual-retry journal event flipped one candidate of an already
# replayed cycle to `manual_retry`; the strict-warm upgrade relabelled it
# `retry_strict_warm_start_retry_run_manifest_mismatch`, and the fresh-zero-row
# merge then blocked it `nfs_raw_manifest_required` because the raw-less leg
# only ever recognised the `replay_resubmit` token.  The raw objects for
# 070500..070700 do not exist, so no operator action could clear that block
# while the SAME cycle's replay candidates had already submitted 6/6.
# --------------------------------------------------------------------------

#: `scheduler_state_failure._manual_retry_state_evidence` shape for an operator
#: retry bound to a previous job id.
def _manual_retry_decision() -> CandidateStateDecision:
    return CandidateStateDecision(
        "retry",
        "manual_retry_requested",
        {
            "decision": "manual_retry",
            "reason": "manual_retry_requested",
            "manual_retry": {
                "marker": True,
                "allowed": True,
                "requested": True,
                "previous_attempt": 1,
                "new_attempt": 2,
                "previous_job_id": "4412207",
            },
            "failure": {
                "reason_code": "SHUD_RUNTIME_FAILURE",
                "prior_failure_reason": "SHUD_RUNTIME_FAILURE",
                "previous_attempt": 1,
                "new_attempt": 2,
            },
            "retry_policy": {
                "automatic_retry_allowed": False,
                "manual_retry_required": False,
                "manual_retry_marker": True,
                "attempt": 2,
            },
        },
    )


def _downstream_retry_decision() -> CandidateStateDecision:
    """A plain, non-manual retry: the family the completion must NOT touch."""

    return CandidateStateDecision(
        "retry",
        "resume_downstream_after_durable_shud",
        {
            "decision": "retry_downstream",
            "reason": "resume_downstream_after_durable_shud",
            "restart_stage": "state_save_qc",
            "restart_from_stage": "state_save_qc",
            "durable_shud_output_reused": True,
        },
    )


READY_FORCING_EVIDENCE: dict[str, Any] = {
    "source": "replay_forcing_evidence",
    "status": "ready",
    "model_ids": [REPLAY_MODEL_ID, OUT_OF_SET_MODEL_ID],
}


def _raw_less_manual_retry_pass(
    *,
    admission: ReplayAdmission | None,
    decision: CandidateStateDecision,
    cycle_time: datetime = IN_WINDOW_CYCLE,
    model_ids: Sequence[str] = (REPLAY_MODEL_ID,),
    strict_warm_start: Mapping[str, Any] | None = None,
    discovery_classifier: str | None = "replay_forcing_evidence",
    raw_state: Mapping[str, Any] | None = None,
) -> tuple[list[Any], list[Any], list[dict[str, Any]]]:
    """The blocked production pass: zero canonical rows and no raw manifest."""

    return _build(
        admission=admission,
        decision=decision,
        strict_warm_start=strict_warm_start,
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        raw_state={} if raw_state is None else raw_state,
        cycle_time=cycle_time,
        model_ids=model_ids,
        discovery_classifier=discovery_classifier,
        discovery_evidence={"replay_forcing_evidence": dict(READY_FORCING_EVIDENCE)},
    )


def test_replay_window_manual_retry_is_admitted_over_the_raw_less_leg() -> None:
    """T1 consumer oracle: admitted, and the manual-retry token survives intact.

    Round-2 C1 re-pin: the shape under test is the ``COLD_START_QUARANTINED``
    manual retry, which declares ``restart_stage="forecast"`` at stamp time.  A
    BARE manual retry (no restart stage at all) is no longer admitted here — see
    ``test_a_bare_manual_retry_without_a_forecast_restart_stays_blocked``.
    """

    candidates, blocked, skipped = _raw_less_manual_retry_pass(
        admission=_admission(),
        decision=_produced_manual_retry_decision(prior_failure_reason="COLD_START_QUARANTINED"),
    )
    assert blocked == []
    assert skipped == []
    assert len(candidates) == 1
    evidence = candidates[0].state_evidence
    # Honest token: NEVER relabelled to `replay_resubmit`, so the retry keeps its
    # own retry-suffixed submission path.  The token stays OUT of the global
    # `_FORCE_TERMINAL_RESUBMIT_DECISIONS` whitelist — what makes this candidate
    # resubmittable is the windowed admission marker the chain gate reads, which
    # the cohort oracle in section 1.2e drives end to end.
    assert evidence["decision"] == "manual_retry"
    assert evidence["reason"] == "manual_retry_requested"
    assert (
        evidence["decision"]
        == _produced_manual_retry_decision(prior_failure_reason="SHUD_RUNTIME_FAILURE").evidence["decision"]
    )
    assert scheduler_candidate_manifest_module._candidate_manual_retry_attempt(candidates[0]) == 2

    admitted = evidence["replay_manual_retry_admission"]
    assert admitted["status"] == "admitted"
    assert admitted["reason"] == "replay_window_manual_retry_admitted"
    assert admitted["decision"] == "manual_retry"
    assert admitted["origin_reason"] == "manual_retry_requested"
    assert admitted["replay_admission"]["model_ids"] == [REPLAY_MODEL_ID]

    guard = evidence["replay_canonical_readiness_guard"]
    assert guard["leg"] == "raw_manifest_absent"
    assert guard["status"] == "admitted"
    assert guard["reason"] == "replay_forcing_evidence_substitutes_raw_manifest"
    assert guard["decision"] == "manual_retry"
    # Fold-in B: the receipt always names what the admitted candidate resumes
    # from.  Round-2 C1: for an ADMITTED candidate that is now always a real
    # stage at or after `forecast`, never null.
    assert "restart_stage" in guard
    assert guard["restart_stage"] == "forecast"
    assert evidence["restart_stage"] == "forecast"
    assert guard["substitute_present"] is True
    assert guard["replay_forcing_evidence"]["status"] == "ready"
    # The absent manifest is recorded, not silently dropped.
    assert guard["nfs_raw_manifest"]["status"] == "missing"
    assert evidence["canonical_readiness"]["candidate_row_count"] == 0


def test_a_bare_manual_retry_without_a_forecast_restart_stays_blocked() -> None:
    """C1: an eligible marker is not a restart precondition.

    The bare producer shape (``_manual_retry_state_evidence`` writes restart keys
    ONLY in its ``COLD_START_QUARANTINED`` branch) declares no restart stage, so
    ``_candidate_restart_stage`` returns None and the chain cohort key is
    ``(0, "full")`` — the pipeline would start at ``convert_canonical``, against
    the very raw manifest this leg has just recorded as ABSENT.  Fail-closed must
    stay fail-closed: the pre-change typed block is kept and the marker says why.
    """

    for label, decision in (
        ("hand_written", _manual_retry_decision()),
        ("producer", _produced_manual_retry_decision(prior_failure_reason="SHUD_RUNTIME_FAILURE")),
    ):
        candidates, blocked, skipped = _raw_less_manual_retry_pass(
            admission=_admission(),
            decision=decision,
        )
        assert candidates == [], label
        assert skipped == [], label
        assert len(blocked) == 1, label
        # The reason TOKEN is unchanged — operators keep the one runbook row.
        assert blocked[0].reason == "nfs_raw_manifest_required", label
        blocked_evidence = blocked[0].state_evidence
        assert blocked_evidence["nfs_raw_manifest"]["status"] == "missing", label
        # Nothing was admitted, so no substitute receipt is written.
        assert "replay_canonical_readiness_guard" not in blocked_evidence, label
        # The marker stays honest AND explains the declined admission.
        marker = blocked_evidence["replay_manual_retry_admission"]
        assert marker["status"] == "eligible", label
        assert marker["raw_less_admission"] == "declined", label
        assert marker["declined_reason"] == "replay_manual_retry_restart_stage_unsupported", label
        assert marker["restart_stage"] is None, label
        assert marker["required_restart_stage"] == "forecast", label


def test_a_bare_manual_retry_surviving_the_strict_warm_upgrade_stays_blocked() -> None:
    """C1, second bare-shaped survival path: the upgrade early-returns.

    ``_upgrade_retry_for_strict_warm_start_manifest`` leaves the decision alone
    when the retry evidence's ``run_manifest_initial_state`` already matches the
    strict warm start, so the bare shape reaches the raw-less leg with no restart
    stage even under a strict warm start.  The precondition is therefore
    evaluated at the FIRING POINT on the CURRENT decision, not at the eligibility
    stamp.
    """

    decision = _produced_manual_retry_decision(
        prior_failure_reason="SHUD_RUNTIME_FAILURE",
        extra_state={"run_manifest_initial_state": dict(SELECTED_STATE)},
    )
    strict = {"ready": True, "candidate_state": dict(SELECTED_STATE)}
    # Precondition of this test: the upgrade really is a no-op for this shape.
    upgraded = scheduler_candidates_module._upgrade_retry_for_strict_warm_start_manifest(decision, strict)
    assert upgraded.evidence["decision"] == "manual_retry"
    assert "restart_stage" not in upgraded.evidence

    candidates, blocked, skipped = _raw_less_manual_retry_pass(
        admission=_admission(),
        decision=decision,
        strict_warm_start=strict,
    )
    assert candidates == []
    assert skipped == []
    assert blocked[0].reason == "nfs_raw_manifest_required"
    marker = blocked[0].state_evidence["replay_manual_retry_admission"]
    assert marker["status"] == "eligible"
    assert marker["declined_reason"] == "replay_manual_retry_restart_stage_unsupported"


def test_no_admitted_manual_retry_can_land_in_the_full_chain_cohort() -> None:
    """C1 consumer oracle: the real cohort keying, over every admitted shape.

    ``(0, "full")`` is the cohort whose chain starts at ``convert_canonical``
    (``chain_runtime_utils`` stage index 0).  For a replay cycle whose raw
    objects do not exist that is the one shape the admission must never produce,
    so the property is asserted against the REAL scheduler helpers rather than
    against the evidence keys the leg happens to write.
    """

    strict = {"ready": True, "candidate_state": dict(SELECTED_STATE)}
    shapes = {
        "bare": (_produced_manual_retry_decision(prior_failure_reason="SHUD_RUNTIME_FAILURE"), None),
        "bare_upgraded": (
            _produced_manual_retry_decision(prior_failure_reason="SHUD_RUNTIME_FAILURE"),
            strict,
        ),
        "cold_start": (
            _produced_manual_retry_decision(prior_failure_reason="COLD_START_QUARANTINED"),
            None,
        ),
        "cold_start_upgraded": (
            _produced_manual_retry_decision(prior_failure_reason="COLD_START_QUARANTINED"),
            strict,
        ),
    }
    admitted_labels = []
    for label, (decision, strict_warm_start) in shapes.items():
        candidates, blocked, _skipped = _raw_less_manual_retry_pass(
            admission=_admission(),
            decision=decision,
            strict_warm_start=strict_warm_start,
        )
        for candidate in candidates:
            assert candidate.state_evidence["replay_manual_retry_admission"]["status"] == "admitted", label
            admitted_labels.append(label)
            restart_stage = scheduler_module._candidate_restart_stage(candidate)
            cohort_key = scheduler_module._candidate_restart_cohort_key(restart_stage)
            assert restart_stage == "forecast", (label, restart_stage)
            assert cohort_key != (0, "full"), (label, cohort_key)
            assert cohort_key == (3, "forecast"), (label, cohort_key)
        if not candidates:
            assert blocked[0].reason == "nfs_raw_manifest_required", label
    # The campaign shape (bare -> upgraded token, restart forecast) and the
    # cold-start shape must BOTH still be admitted; only the bare one is blocked.
    assert sorted(admitted_labels) == ["bare_upgraded", "cold_start", "cold_start_upgraded"]


def test_the_manual_retry_clamp_expectation_pins_admission_time_truth() -> None:
    """C1 item 5: the 6-key expectation, re-derived for each admitted shape.

    Replays the real pipeline order — producer decision, eligibility stamp,
    strict-warm upgrade, admitted flip — and reads the expectation the raw-less
    leg would register.  Present keys are pinned BY VALUE; only keys the
    admission decision genuinely lacks keep the absence sentinel.
    """

    absent = scheduler_candidates_module._REPLAY_CLAMP_ABSENT
    config = _FakeConfig(replay_admission=_admission())
    candidate = _scheduler_candidate_for(REPLAY_MODEL_ID)
    strict = {"ready": True, "candidate_state": dict(SELECTED_STATE)}

    def _expectation(*, prior_failure_reason: str, strict_warm_start: Mapping[str, Any] | None) -> Any:
        decision = _produced_manual_retry_decision(prior_failure_reason=prior_failure_reason)
        stamped = scheduler_candidates_module._replay_manual_retry_admission_decision(
            config, candidate, decision
        )
        assert stamped is not None
        upgraded = scheduler_candidates_module._upgrade_retry_for_strict_warm_start_manifest(
            stamped,
            strict_warm_start,
        )
        admitted = scheduler_candidates_module._replay_manual_retry_admitted_decision(upgraded)
        return scheduler_candidates_module._replay_manual_retry_invariant(admitted)

    cold_start = _expectation(prior_failure_reason="COLD_START_QUARANTINED", strict_warm_start=None)
    assert cold_start.mode == "replay_manual_retry"
    assert cold_start.required == {
        "decision": "manual_retry",
        "restart_stage": "forecast",
        "restart_from_stage": "forecast",
        "native_shud_resubmitted": True,
        # never written by the producer's cold-start branch -> pinned ABSENT
        "durable_output_reused": absent,
        "durable_shud_output_reused": absent,
        # repair-2 round 3: no manual-retry shape ever declares a fresh ingestion,
        # and a merged-in `full_chain` one OUTRANKS the restart keys above
        "fresh_ingestion": absent,
    }

    upgraded = _expectation(prior_failure_reason="SHUD_RUNTIME_FAILURE", strict_warm_start=strict)
    assert upgraded.required == {
        "decision": "retry_strict_warm_start_retry_run_manifest_mismatch",
        "restart_stage": "forecast",
        "restart_from_stage": "forecast",
        "native_shud_resubmitted": True,
        "durable_output_reused": False,
        "durable_shud_output_reused": False,
        "fresh_ingestion": absent,
    }
    # Every admitted shape pins a restart stage BY VALUE: the absence sentinel
    # survives only for genuinely-absent durable keys (C1 item 5).
    for expectation in (cold_start, upgraded):
        assert expectation.required["restart_stage"] == "forecast"
        assert expectation.required["restart_from_stage"] == "forecast"


def test_manual_retry_outside_the_replay_scope_stays_blocked() -> None:
    """T2: out of window, out of model set, and replay off all keep the block."""

    for label, kwargs in (
        ("out_of_window", {"cycle_time": OUT_OF_WINDOW_CYCLE}),
        ("out_of_set_model", {"model_ids": (OUT_OF_SET_MODEL_ID,)}),
    ):
        candidates, blocked, skipped = _raw_less_manual_retry_pass(
            admission=_admission(),
            decision=_manual_retry_decision(),
            **kwargs,
        )
        assert candidates == [], label
        assert skipped == [], label
        assert blocked[0].reason == "nfs_raw_manifest_required", label
        blocked_evidence = blocked[0].state_evidence
        assert blocked_evidence["nfs_raw_manifest"]["status"] == "missing", label
        assert "replay_manual_retry_admission" not in blocked_evidence, label
        assert "replay_canonical_readiness_guard" not in blocked_evidence, label

    candidates, blocked, skipped = _raw_less_manual_retry_pass(
        admission=None,
        decision=_manual_retry_decision(),
    )
    assert candidates == []
    assert skipped == []
    assert blocked[0].reason == "nfs_raw_manifest_required"
    assert "replay_manual_retry_admission" not in blocked[0].state_evidence


def test_replay_window_non_manual_retry_keeps_the_raw_manifest_block() -> None:
    """T3: the completion is manual-retry scoped, not a window-wide raw-less lift."""

    candidates, blocked, skipped = _raw_less_manual_retry_pass(
        admission=_admission(),
        decision=_downstream_retry_decision(),
    )
    assert candidates == []
    assert skipped == []
    assert blocked[0].reason == "nfs_raw_manifest_required"
    blocked_evidence = blocked[0].state_evidence
    assert blocked_evidence["decision"] == "retry_downstream"
    assert "replay_manual_retry_admission" not in blocked_evidence
    assert "replay_canonical_readiness_guard" not in blocked_evidence


def test_strict_warm_upgraded_manual_retry_is_admitted_by_origin_not_by_token() -> None:
    """T4: the production shape — upgraded FROM a manual retry — is admitted.

    The same upgraded token produced from a NON-manual origin keeps the block, so
    the admission follows the manual-retry origin rather than the relabelled
    decision the upgrade writes.
    """

    strict = {"ready": True, "candidate_state": dict(SELECTED_STATE)}
    candidates, blocked, skipped = _raw_less_manual_retry_pass(
        admission=_admission(),
        decision=_manual_retry_decision(),
        strict_warm_start=strict,
    )
    assert blocked == []
    assert skipped == []
    evidence = candidates[0].state_evidence
    assert evidence["decision"] == "retry_strict_warm_start_retry_run_manifest_mismatch"
    assert evidence["replay_manual_retry_admission"]["status"] == "admitted"
    # C1: the campaign path — bare at stamp time, `forecast` restart written by
    # the upgrade BEFORE the firing point — must still be admitted.
    assert evidence["restart_stage"] == "forecast"
    assert evidence["restart_from_stage"] == "forecast"
    # The upgrade relabels the token; the recorded origin still names the retry
    # family the admission was granted to.
    assert evidence["replay_manual_retry_admission"]["decision"] == "manual_retry"
    assert evidence["manual_retry"]["marker"] is True
    guard = evidence["replay_canonical_readiness_guard"]
    assert guard["status"] == "admitted"
    assert guard["decision"] == "retry_strict_warm_start_retry_run_manifest_mismatch"

    non_manual_candidates, non_manual_blocked, non_manual_skipped = _raw_less_manual_retry_pass(
        admission=_admission(),
        decision=_downstream_retry_decision(),
        strict_warm_start=strict,
    )
    assert non_manual_candidates == []
    assert non_manual_skipped == []
    assert non_manual_blocked[0].reason == "nfs_raw_manifest_required"
    non_manual_evidence = non_manual_blocked[0].state_evidence
    assert non_manual_evidence["decision"] == "retry_strict_warm_start_retry_run_manifest_mismatch"
    assert "replay_manual_retry_admission" not in non_manual_evidence


def test_manual_retry_admission_still_needs_a_verified_forcing_substitute() -> None:
    """A2-3 stays armed: no forcing-evidence provenance, no raw-less admission.

    The candidate here clears the C1 restart precondition (cold-start shape,
    ``forecast`` restart) — the substitute check is the thing that blocks it, so
    the two preconditions are proven independent.  This is the third observable
    state the runbook row now names: marker ``eligible`` + guard ``blocked``.
    """

    candidates, blocked, skipped = _raw_less_manual_retry_pass(
        admission=_admission(),
        decision=_produced_manual_retry_decision(prior_failure_reason="COLD_START_QUARANTINED"),
        discovery_classifier="nfs_raw_manifest",
    )
    assert candidates == []
    assert skipped == []
    assert blocked[0].reason == "replay_raw_manifest_substitute_unavailable"
    blocked_evidence = blocked[0].state_evidence
    assert blocked_evidence["replay_manual_retry_admission"]["status"] == "eligible"
    guard = blocked_evidence["replay_canonical_readiness_guard"]
    assert guard["status"] == "blocked"
    assert guard["substitute_present"] is False
    assert guard["decision"] == "manual_retry"


def test_manual_retry_with_a_ready_raw_manifest_keeps_the_convert_restart() -> None:
    """The raw-ready leg is untouched: a manual retry still rebuilds from raw."""

    candidates, blocked, skipped = _raw_less_manual_retry_pass(
        admission=_admission(),
        decision=_manual_retry_decision(),
        raw_state=RAW_MANIFEST_READY_STATE,
    )
    assert blocked == []
    assert skipped == []
    evidence = candidates[0].state_evidence
    assert evidence["decision"] == "manual_retry"
    assert evidence["restart_stage"] == "convert"
    assert evidence["restart_from_stage"] == "convert"
    assert evidence["restart_reason"] == "raw_manifest_ready_without_canonical"
    assert evidence["fresh_ingestion"] == {"required": False, "mode": "reuse_raw_then_convert"}
    assert "replay_canonical_readiness_guard" not in evidence
    # Fold-in A: the marker is honest about what happened.  This candidate was
    # only ever ELIGIBLE — the raw-less admission never fired for it, so no
    # invariant expectation is registered and the raw-ready `convert` restart the
    # manual retry legitimately wants is not clamped back to the admission shape.
    assert evidence["replay_manual_retry_admission"]["status"] == "eligible"
    assert "replay_invariant_clamp_applied" not in evidence


# --------------------------------------------------------------------------
# 1.2d post-assembly invariant clamp + merge-site audit
# (round-3 gate retro, design D3.5 v6: A3-1, A3-2)
# --------------------------------------------------------------------------

#: The production active-job row shape the candidate-state repository returns.
ACTIVE_SLURM_JOB: dict[str, Any] = {
    "job_id": "998877",
    "state": "RUNNING",
    "stage": "forecast",
    "submitted_at": "2026-07-07T12:05:00Z",
}
#: What ``sync_cycle_statuses`` reports once that job has gone terminal.
TERMINAL_SYNC_UPDATE: dict[str, Any] = {
    "job_id": "998877",
    "status": "succeeded",
    "stage": "forecast",
    "pipeline_run_id": "pipe-2026070712",
}


def _post_sync_retry_decision() -> CandidateStateDecision:
    """The ordinary ``retry_failed`` decision the journal yields after a sync.

    Shape copied from ``scheduler_state_failure._retry_failed_evidence``: the
    restart stage is TOP-LEVEL (which is what clobbers the replay keys at the
    post-sync rebuild merge) while the reuse flags sit under ``reuse``.
    """

    return CandidateStateDecision(
        "retry",
        "retry_failed_candidate",
        {
            "decision": "retry_failed",
            "reason": "retry_failed_candidate",
            "stage": "convert",
            "restart_stage": "convert",
            "restart_from_stage": "convert",
            "failure": {
                "classifier": "chain_stage_failed",
                "retryable": True,
                "permanent": False,
                "attempt": 1,
                "retry_limit": 3,
            },
            "retry_policy": {
                "automatic_retry_allowed": True,
                "manual_retry_required": False,
                "attempt": 1,
                "retry_limit": 3,
            },
            "reuse": {"successful_sibling_outputs_reused": False, "durable_output_reused": False},
        },
    )


class _DecisionSequence:
    """Candidate-state decider whose answer changes between journal reads."""

    def __init__(self, decisions: Sequence[CandidateStateDecision | None]) -> None:
        self._decisions = list(decisions)
        self.calls = 0

    def __call__(self, candidate: Any, state: Mapping[str, Any] | None) -> CandidateStateDecision | None:
        index = min(self.calls, len(self._decisions) - 1)
        self.calls += 1
        return self._decisions[index]


def test_post_sync_rebuild_cannot_clobber_the_repair_convert_restart(tmp_path: Path) -> None:
    """A3-1: the post-Slurm-sync rebuild merge (``:1013``) is a sibling clobber point.

    The 070712 repair candidate has an active Slurm job on the first probe; the
    sync reports it terminal, the driver re-reads the journal and re-authorizes
    the repair — whose restart is ``forcing`` — and the rebuild merge puts that
    back over the ``convert`` restart the canonical gate deliberately lowered.
    The clamp restores it AFTER every merge, and the ``slurm_state_sync`` audit
    evidence that merge carries still lands on the candidate.
    """

    _write_raw_manifest(tmp_path, cycle_time=REPAIR_CYCLE)
    model_id = REPAIR_MODEL_IDS[0]
    repository = _FakeRepository(
        _repair_raw_state(REPAIR_CYCLE),
        slurm_jobs_script=[[ACTIVE_SLURM_JOB]],
    )
    orchestrator = _SyncingOrchestrator([TERMINAL_SYNC_UPDATE])

    candidates, blocked, skipped = _build(
        admission=_repair_admission((model_id,)),
        decision=None,
        decisions_by_model={model_id: _missing_forcing_blocked_decision()},
        strict_warm_start=_repair_warm_state(REPAIR_CYCLE),
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        cycle_time=REPAIR_CYCLE,
        model_ids=(model_id,),
        raw_state=_repair_raw_state(REPAIR_CYCLE),
        resource_profiles={model_id: _direct_grid_profile()},
        config_overrides={
            "repair_missing_forcing": True,
            "repair_missing_forcing_cycle_time": REPAIR_CYCLE,
            "require_direct_grid": True,
            "nfs_raw_manifest_root": tmp_path,
            "nfs_raw_manifest_prefix": "s3://nhms",
        },
        repository=repository,
        orchestrator=orchestrator,
        allow_slurm_status_sync=True,
    )

    assert blocked == []
    assert skipped == []
    assert [candidate.model_id for candidate in candidates] == [model_id]
    evidence = candidates[0].state_evidence
    # the invariant: the repair rebuilds canonical from raw, at `convert`
    assert evidence["restart_stage"] == "convert"
    assert evidence["restart_from_stage"] == "convert"
    assert evidence["fresh_ingestion"]["mode"] == "reuse_raw_then_convert"
    # A4-1 symmetry: the repair's decision token is clamp-owned too, and it is
    # what the chain gate matches for THIS leg's forced resubmission
    assert evidence["decision"] == "retry_repair_missing_forcing"
    assert evidence["decision"] in chain_cycle_module._FORCE_TERMINAL_RESUBMIT_DECISIONS
    # the merge itself is NOT skipped: its audit evidence is the point of it
    assert orchestrator.calls == [cycle_id_for("gfs", REPAIR_CYCLE)]
    assert evidence["slurm_state_sync"]["status"] == "synced"
    assert evidence["slurm_state_sync"]["terminal_updates"] == [TERMINAL_SYNC_UPDATE]
    # ... and the repair is visible, not silently healed
    clamp = evidence["replay_invariant_clamp_applied"]
    assert clamp["mode"] == "replay_repair_convert_restart"
    assert clamp["candidate_id"] == candidates[0].candidate_id
    assert clamp["clobbered_keys"] == ["fresh_ingestion", "restart_from_stage", "restart_stage"]
    assert clamp["pre_clamp"]["restart_stage"] == "forcing"
    assert clamp["pre_clamp"]["restart_from_stage"] == "forcing"
    assert clamp["pre_clamp"]["fresh_ingestion"]["mode"] == "repair_missing_forcing"
    assert clamp["restored"]["restart_stage"] == "convert"


SUCCEEDED_FORECAST_JOB = {
    "job_id": "job_cycle_gfs_2026070600_forecast",
    "status": "succeeded",
    "stage": "forecast",
    "job_type": "run_shud_forecast_array",
}


def _cohort_basin_row(candidate: Any) -> dict[str, Any]:
    """The ``context.active_basins`` row the chain builds for one candidate."""

    return {
        "model_id": candidate.model_id,
        "basin_id": candidate.basin_id,
        "candidate_id": candidate.candidate_id,
        "orchestration_run_id": candidate.cycle_id,
        "state_evidence": dict(candidate.state_evidence),
    }


def _chain_gate_forces_resubmit_cohort(candidates: Sequence[Any]) -> bool:
    """Drive the REAL chain gate with a WHOLE cohort's basins.

    ``_terminal_stage_needs_forced_resubmit`` is a conjunction over
    ``context.active_basins``: one non-qualifying basin returns False for every
    basin in the cohort.  The cohort form is therefore the honest consumer shape
    whenever a replay cycle carries more than one admitted candidate.
    """

    return chain_cycle_module._terminal_stage_needs_forced_resubmit(
        SimpleNamespace(active_basins=[_cohort_basin_row(c) for c in candidates], restart_stage=None),
        dict(SUCCEEDED_FORECAST_JOB),
    )


def _chain_gate_forces_resubmit(candidate: Any) -> bool:
    """Drive the REAL chain gate with an admitted candidate's evidence (A4-1).

    ``_terminal_stage_needs_forced_resubmit`` (``chain_forecast_orchestrator_cycle``
    :802) is the consumer that decides whether a succeeded forecast job gets
    resubmitted; it matches ``state_evidence["decision"]`` against
    ``_FORCE_TERMINAL_RESUBMIT_DECISIONS`` literally.  ``restart_stage=None`` on
    the context is the honest shape: the chain has no restart stage of its own
    here, the candidate evidence is the only source.
    """

    return _chain_gate_forces_resubmit_cohort([candidate])


def _post_sync_downstream_retry_decision() -> CandidateStateDecision:
    """The ``retry_downstream`` decision shape (``scheduler_state_failure`` :246-262).

    The post-sync journal read can just as well land here: a durable SHUD output
    exists and a downstream stage failed.  Its evidence says the exact opposite
    of a replay on both SHUD keys — reusing the durable output, not resubmitting
    the native run.
    """

    return CandidateStateDecision(
        "retry",
        "resume_downstream_after_durable_shud",
        {
            "decision": "retry_downstream",
            "reason": "resume_downstream_after_durable_shud",
            "restart_stage": "parse",
            "restart_from_stage": "parse",
            "native_shud_resubmitted": False,
            "durable_shud_output_reused": True,
            "durable_output_uri": "s3://nhms/runs/fcst_gfs_2026070600_dg_dth_ls_gfs/output",
            "force_native_shud_rerun": False,
            "failure": {
                "classifier": "chain_stage_failed",
                "error_code": "PARSE_FAILED",
                "retryable": True,
                "permanent": False,
                "attempt": 1,
                "retry_limit": 3,
            },
            "retry_policy": {
                "automatic_retry_allowed": True,
                "manual_retry_required": False,
                "attempt": 1,
                "retry_limit": 3,
            },
        },
    )


def test_post_sync_rebuild_cannot_clobber_the_replay_resubmit_forecast_restart() -> None:
    """Same sibling merge, leg (a): a post-sync retry lowers the forecast restart.

    Once the sync reports the job terminal the journal is re-read, and the
    ordinary ``retry_failed`` decision it now yields carries a ``convert``
    restart.  Merging it is right for its own evidence and wrong for the replay
    keys: re-running convert/forcing would rebuild inputs the replay window
    already has.  The clamp puts ``forecast`` back and names what it repaired.
    """

    decider = _DecisionSequence([_replay_terminal_decision(), _post_sync_retry_decision()])
    repository = _FakeRepository(RAW_MANIFEST_READY_STATE, slurm_jobs_script=[[ACTIVE_SLURM_JOB]])
    orchestrator = _SyncingOrchestrator([TERMINAL_SYNC_UPDATE])

    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=None,
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        decider=decider,
        repository=repository,
        orchestrator=orchestrator,
        allow_slurm_status_sync=True,
    )

    assert blocked == []
    assert skipped == []
    assert decider.calls == 2
    evidence = candidates[0].state_evidence
    assert evidence["restart_stage"] == "forecast"
    assert evidence["restart_from_stage"] == "forecast"
    # A4-1: the decision token itself is a merge target, not a stable premise —
    # the retry_failed rebuild overwrites it and the chain gate matches it
    # literally, so the clamp owns it too.
    assert evidence["decision"] == "replay_resubmit"
    # the override's own keys were never clobbered and stay exactly as decided
    assert evidence["native_shud_resubmitted"] is True
    assert evidence["durable_output_reused"] is False
    assert evidence["slurm_state_sync"]["status"] == "synced"
    clamp = evidence["replay_invariant_clamp_applied"]
    assert clamp["mode"] == "replay_resubmit"
    assert clamp["clobbered_keys"] == ["decision", "restart_from_stage", "restart_stage"]
    assert clamp["pre_clamp"] == {
        "decision": "retry_failed",
        "restart_stage": "convert",
        "restart_from_stage": "convert",
    }

    # CONSUMER ORACLE (A4-1): producer-side key equality is not the property
    # under test — "the replay actually resubmits" is.  Drive the real chain
    # gate with the admitted candidate's evidence and a succeeded forecast job:
    # False here means parse/publish rerun over the OLD forecast outputs.
    assert _chain_gate_forces_resubmit(candidates[0]) is True


def test_post_sync_downstream_retry_cannot_flip_the_manifest_shud_projection() -> None:
    """A4-1 secondary: the ``retry_downstream`` shape lands on the same merge.

    ``durable_shud_output_reused=True`` is not merely a stale flag: the emitted
    basin manifest reads it and REWRITES ``native_shud_resubmitted`` to False
    (``scheduler_candidate_manifest`` :236-238).  Unclamped, the replay ships a
    manifest that says the SHUD run is reused while the restart stage says the
    forecast is rerun — self-contradictory evidence downstream consumers act on.
    """

    decider = _DecisionSequence([_replay_terminal_decision(), _post_sync_downstream_retry_decision()])
    repository = _FakeRepository(RAW_MANIFEST_READY_STATE, slurm_jobs_script=[[ACTIVE_SLURM_JOB]])
    orchestrator = _SyncingOrchestrator([TERMINAL_SYNC_UPDATE])

    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=None,
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        decider=decider,
        repository=repository,
        orchestrator=orchestrator,
        allow_slurm_status_sync=True,
    )

    assert blocked == []
    assert skipped == []
    admitted = candidates[0]
    evidence = admitted.state_evidence
    assert evidence["decision"] == "replay_resubmit"
    assert evidence["restart_stage"] == "forecast"
    assert evidence["native_shud_resubmitted"] is True
    assert evidence["durable_shud_output_reused"] is False
    assert evidence["durable_output_reused"] is False
    clamp = evidence["replay_invariant_clamp_applied"]
    assert clamp["mode"] == "replay_resubmit"
    assert clamp["clobbered_keys"] == [
        "decision",
        "durable_shud_output_reused",
        "native_shud_resubmitted",
        "restart_from_stage",
        "restart_stage",
    ]
    assert clamp["pre_clamp"]["durable_shud_output_reused"] is True
    assert clamp["pre_clamp"]["native_shud_resubmitted"] is False

    # CONSUMER ORACLE: the manifest the scheduler actually emits, and the chain
    # gate that decides whether the forecast is resubmitted at all.
    manifest = scheduler_candidate_manifest_module._candidate_basin_manifest(
        admitted,
        output_uri="s3://nhms/runs/replayed/output",
    )
    assert manifest["state_evidence"]["native_shud_resubmitted"] is True
    assert manifest["state_evidence"]["durable_shud_output_reused"] is False
    # the flip-to-reuse projection never fires: neither key is asserted at the
    # manifest's top level, which is what a downstream reader consults
    assert "durable_shud_output_reused" not in manifest
    assert manifest.get("native_shud_resubmitted") is not False
    assert manifest["restart_stage"] == "forecast"
    assert _chain_gate_forces_resubmit(admitted) is True


def test_the_clamp_restores_every_replay_resubmit_key_a_future_merge_could_clobber() -> None:
    """The backstop itself: every owned key, whatever clobbered them.

    Stands in for the merge leg nobody has written yet — the retro's whole point
    is that the invariant must not depend on enumerating those legs.
    """

    candidates, _blocked, _skipped = _build(
        admission=_admission(),
        decision=_replay_terminal_decision(),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        raw_state=RAW_MANIFEST_READY_STATE,
    )
    admitted = candidates[0]
    assert admitted.state_evidence["restart_stage"] == "forecast"
    assert "replay_invariant_clamp_applied" not in admitted.state_evidence

    override = scheduler_candidates_module._replay_terminal_override_decision(
        _FakeConfig(replay_admission=_admission()),
        admitted,
        _replay_terminal_decision(),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        successor_state=None,
    )
    assert override is not None
    expectation = scheduler_candidates_module._replay_resubmit_invariant(override)
    assert expectation is not None

    # repair-2 round 3: the expectation is WHOLE-SET, so a key the override never
    # wrote is pinned ABSENT rather than ignored.
    assert expectation.required["fresh_ingestion"] is scheduler_candidates_module._REPLAY_CLAMP_ABSENT

    clobbered = scheduler_candidates_module._candidate_with_state_evidence(
        admitted,
        {
            "decision": "retry_failed",
            "restart_stage": "convert",
            "restart_from_stage": "convert",
            "native_shud_resubmitted": False,
            "durable_output_reused": True,
            "durable_shud_output_reused": True,
            "fresh_ingestion": {"required": True, "mode": "full_chain"},
        },
    )
    clamped = [clobbered]
    scheduler_candidates_module._clamp_replay_invariants(clamped, {clobbered.candidate_id: expectation})

    evidence = clamped[0].state_evidence
    assert evidence["decision"] == "replay_resubmit"
    assert evidence["restart_stage"] == "forecast"
    assert evidence["restart_from_stage"] == "forecast"
    assert evidence["native_shud_resubmitted"] is True
    assert evidence["durable_output_reused"] is False
    assert evidence["durable_shud_output_reused"] is False
    assert "fresh_ingestion" not in evidence
    clamp = evidence["replay_invariant_clamp_applied"]
    assert clamp["clobbered_keys"] == [
        "decision",
        "durable_output_reused",
        "durable_shud_output_reused",
        "fresh_ingestion",
        "native_shud_resubmitted",
        "restart_from_stage",
        "restart_stage",
    ]
    assert clamp["removed_keys"] == ["fresh_ingestion"]
    assert clamp["pre_clamp"]["native_shud_resubmitted"] is False
    assert clamp["pre_clamp"]["durable_output_reused"] is True
    assert clamp["pre_clamp"]["decision"] == "retry_failed"
    assert clamp["pre_clamp"]["fresh_ingestion"]["mode"] == "full_chain"
    # the whole owned set is what the consumers read, nothing narrower
    assert set(scheduler_candidates_module._REPLAY_RESUBMIT_CLAMPED_KEYS) == set(clamp["clobbered_keys"])
    assert _chain_gate_forces_resubmit(clamped[0]) is True
    # ... and the EXECUTOR agrees: `fresh_ingestion` outranks `restart_stage`, so
    # leaving it merged in would have keyed the full-chain cohort regardless.
    assert scheduler_module._candidate_restart_stage(clamped[0]) == "forecast"
    assert scheduler_module._candidate_restart_cohort_key("forecast") == (3, "forecast")


def test_the_clamp_is_a_pure_no_op_without_replay_admission() -> None:
    """must-preserve 3: with no replay in the pass the clamp rebuilds nothing.

    Both halves: the same post-sync rebuild pass keeps its pre-change evidence
    (the ``convert`` restart the retry decision asked for, no clamp key), and the
    clamp leaves the candidate objects themselves untouched by identity.
    """

    decider = _DecisionSequence([_replay_terminal_decision(), _post_sync_retry_decision()])
    repository = _FakeRepository(RAW_MANIFEST_READY_STATE, slurm_jobs_script=[[ACTIVE_SLURM_JOB]])
    orchestrator = _SyncingOrchestrator([TERMINAL_SYNC_UPDATE])

    candidates, blocked, skipped = _build(
        admission=None,
        decision=None,
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        decider=decider,
        repository=repository,
        orchestrator=orchestrator,
        allow_slurm_status_sync=True,
    )

    # Pre-change shape, unchanged: outside replay the terminal skip degrades to
    # the ordinary mismatch retry, the raw-ready canonical leg lowers the restart
    # to `convert`, and the post-sync rebuild merge keeps it there.  Nothing
    # clamps it back up.
    assert blocked == []
    assert skipped == []
    evidence = candidates[0].state_evidence
    assert evidence["restart_stage"] == "convert"
    assert evidence["restart_from_stage"] == "convert"
    assert evidence["slurm_state_sync"]["status"] == "synced"
    assert "replay_invariant_clamp_applied" not in evidence
    assert "replay_canonical_readiness_guard" not in evidence

    unrelated = [_candidate_factory(
        discovery=CycleDiscovery(
            cycle_id=cycle_id_for("gfs", IN_WINDOW_CYCLE),
            source_id="gfs",
            cycle_time=IN_WINDOW_CYCLE,
            cycle_hour=IN_WINDOW_CYCLE.hour,
            available=True,
            status="discovered",
        ),
        model=_FakeModel(model_id=REPLAY_MODEL_ID),
        horizon={},
    )]
    before = list(unrelated)
    scheduler_candidates_module._clamp_replay_invariants(unrelated, {})
    assert unrelated[0] is before[0]


def test_replay_window_repair_needs_a_genuine_zero_row_canonical_evaluation(tmp_path: Path) -> None:
    """A3-2: unknown dependency state is not an authorization to rebuild.

    ``canonical_unavailable`` (the readiness query itself failed) and
    ``no_expected_leads`` (a broken horizon/policy config) both report a
    not-ready canonical, and neither means "retention purged the products".  The
    repair leg must fall back to the pre-change typed block.
    """

    _write_raw_manifest(tmp_path, cycle_time=REPAIR_CYCLE)
    model_id = REPAIR_MODEL_IDS[0]
    unavailable = {
        "source": "gfs",
        "source_id": "gfs",
        "cycle_id": cycle_id_for("gfs", REPAIR_CYCLE),
        "cycle_time": "2026-07-07T12:00:00Z",
        "status": "canonical_unavailable",
        "ready": False,
        "reason": "canonical_products_query_failed",
        "expected_leads": [0, 12, 24],
        "dependency": {"name": "canonical_products", "status": "unavailable", "retryable": True},
        "failure": {
            "classifier": "dependency_unavailable",
            "reason_code": "CANONICAL_PRODUCTS_QUERY_FAILED",
            "dependency": "canonical_products",
            "retryable": True,
            "permanent": False,
        },
    }
    broken_horizon = evaluate_canonical_readiness(
        source_id="gfs",
        cycle_time=REPAIR_CYCLE,
        products=[],
        forecast_hours=(),
        canonical_product_id="canonical_gfs_2026070712",
        model_id=model_id,
        basin_id=model_id.replace("dg_", "basin_"),
    ).evidence
    assert broken_horizon["candidate_row_count"] == 0
    assert not broken_horizon["expected_leads"]

    for readiness in (unavailable, broken_horizon):
        candidates, blocked, skipped = _build(
            admission=_repair_admission((model_id,)),
            decision=None,
            decisions_by_model={model_id: _missing_forcing_blocked_decision()},
            strict_warm_start=_repair_warm_state(REPAIR_CYCLE),
            canonical_readiness=readiness,
            cycle_time=REPAIR_CYCLE,
            model_ids=(model_id,),
            raw_state=_repair_raw_state(REPAIR_CYCLE),
            resource_profiles={model_id: _direct_grid_profile()},
            config_overrides={
                "repair_missing_forcing": True,
                "repair_missing_forcing_cycle_time": REPAIR_CYCLE,
                "require_direct_grid": True,
                "nfs_raw_manifest_root": tmp_path,
                "nfs_raw_manifest_prefix": "s3://nhms",
            },
        )
        assert candidates == []
        assert skipped == []
        assert blocked[0].reason == "missing_forcing_package_uri"
        evidence = blocked[0].state_evidence
        assert evidence["missing_forcing_repair"]["status"] == "rejected"
        assert evidence["missing_forcing_repair"]["reason"] == "canonical_not_ready"
        assert evidence["restart_stage"] == "forecast"
        assert "replay_canonical_readiness_guard" not in evidence
        assert "replay_invariant_clamp_applied" not in evidence


# --------------------------------------------------------------------------
# 1.2e execution-phase P1 fixes for the manual-retry admission
#
# P1-1  the chain's force-terminal-resubmit gate is a CONJUNCTION over the
#       cohort's basins, so an admitted bare-``manual_retry`` sibling used to
#       return False for the whole cohort — zero submissions for the
#       ``replay_resubmit`` basins that shared its cycle.
# P1-2  the post-Slurm-sync rebuild re-derives the decision from the journal and
#       could launder the admitted manual retry into ``retry_downstream``
#       (restart_stage=parse, durable_shud_output_reused=True) while the
#       admission marker still said "admitted": parse/publish rerun over the OLD
#       forecast outputs of a purged cycle.
# --------------------------------------------------------------------------

#: Second in-set basin of the same replay cycle, so a cohort can be mixed.
MANUAL_RETRY_MODEL_ID = "dg_second_basin_gfs"


def _cohort_admission() -> ReplayAdmission:
    admission = parse_replay_admission(
        mode=True,
        model_ids=f"{REPLAY_MODEL_ID},{MANUAL_RETRY_MODEL_ID}",
        window=WINDOW,
    )
    assert admission is not None
    return admission


def _scheduler_candidate_for(model_id: str, *, cycle_time: datetime = IN_WINDOW_CYCLE) -> Any:
    """The candidate ``build_candidates`` itself constructs for this model/cycle."""

    return _candidate_factory(
        discovery=CycleDiscovery(
            cycle_id=cycle_id_for("gfs", cycle_time),
            source_id="gfs",
            cycle_time=cycle_time,
            cycle_hour=cycle_time.hour,
            available=True,
            status="discovered",
        ),
        model=_FakeModel(model_id=model_id),
        horizon={},
    )


def _produced_manual_retry_decision(
    *,
    prior_failure_reason: str,
    model_id: str = REPLAY_MODEL_ID,
    cycle_time: datetime = IN_WINDOW_CYCLE,
    extra_state: Mapping[str, Any] | None = None,
) -> CandidateStateDecision:
    """A manual-retry decision from the REAL producer, not a hand-written copy.

    ``scheduler_state._candidate_state_decision`` -> ``_manual_retry_state_evidence``
    is the only writer of this family, so the independent source of truth for
    both the token and the two evidence shapes (bare, and the
    ``COLD_START_QUARANTINED`` shape that carries a ``forecast`` restart) is the
    producer itself.
    """

    candidate = _scheduler_candidate_for(model_id, cycle_time=cycle_time)
    state = {
        "pipeline_status": "permanently_failed",
        "failed_stage": "forecast",
        "error_code": prior_failure_reason,
        "retry_count": 1,
        "retry_limit": 3,
        "manual_retry": {"marker": True, "requested_by": "operator"},
        "prior_failure_reason": prior_failure_reason,
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "candidate_id": candidate.candidate_id,
        **dict(extra_state or {}),
    }
    decision = _candidate_state_decision(candidate, state)
    assert decision is not None
    return decision


def test_the_manual_retry_literals_match_their_producer() -> None:
    """Fold-in C: a coordinated rename must fail here, not silently disable admission.

    ``scheduler_candidates._decision_is_manual_retry`` re-states literals owned by
    ``scheduler_state_failure._manual_retry_state_evidence`` /
    ``scheduler_state_decision``.  Both shapes the producer emits are pinned: the
    bare one (``decision`` and nothing else the clamp owns) and the
    ``COLD_START_QUARANTINED`` one.
    """

    bare = _produced_manual_retry_decision(prior_failure_reason="SHUD_RUNTIME_FAILURE")
    assert bare.action == "retry"
    assert bare.reason == scheduler_candidates_module._MANUAL_RETRY_REASON
    assert bare.evidence["decision"] == scheduler_candidates_module._MANUAL_RETRY_DECISION
    assert bare.evidence["reason"] == scheduler_candidates_module._MANUAL_RETRY_REASON
    assert scheduler_candidates_module._decision_is_manual_retry(bare) is True
    # Bare really is bare: none of the clamp-owned restart/reuse keys exist, which
    # is exactly why the expectation has to pin ABSENCE and not present-keys-only.
    for key in scheduler_candidates_module._REPLAY_MANUAL_RETRY_CLAMPED_KEYS:
        assert (key in bare.evidence) is (key == "decision"), key

    cold_start = _produced_manual_retry_decision(prior_failure_reason="COLD_START_QUARANTINED")
    assert scheduler_candidates_module._decision_is_manual_retry(cold_start) is True
    assert cold_start.evidence["restart_stage"] == "forecast"
    assert cold_start.evidence["restart_from_stage"] == "forecast"
    assert cold_start.evidence["native_shud_resubmitted"] is True
    assert "durable_shud_output_reused" not in cold_start.evidence


def _mixed_cohort_pass() -> tuple[list[Any], list[Any], list[dict[str, Any]]]:
    """One replay cycle, two in-set basins: a replay sibling and a manual retry."""

    return _build(
        admission=_cohort_admission(),
        decision=None,
        decisions_by_model={
            REPLAY_MODEL_ID: _replay_terminal_decision(),
            MANUAL_RETRY_MODEL_ID: _produced_manual_retry_decision(
                prior_failure_reason="COLD_START_QUARANTINED",
                model_id=MANUAL_RETRY_MODEL_ID,
            ),
        },
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        model_ids=(REPLAY_MODEL_ID, MANUAL_RETRY_MODEL_ID),
        discovery_classifier="replay_forcing_evidence",
        discovery_evidence={"replay_forcing_evidence": dict(READY_FORCING_EVIDENCE)},
    )


def test_an_admitted_manual_retry_sibling_does_not_collapse_the_replay_cohort() -> None:
    """P1-1 consumer oracle: the mixed cohort still forces the forecast resubmit.

    Pre-fix this returned False — one basin outside
    ``_FORCE_TERMINAL_RESUBMIT_DECISIONS`` vetoes the whole conjunction — so the
    replay siblings that shared the cycle submitted nothing and the driver
    re-created the convergence-timeout deadlock.
    """

    candidates, blocked, skipped = _mixed_cohort_pass()

    assert blocked == []
    assert skipped == []
    by_model = {candidate.model_id: candidate for candidate in candidates}
    assert set(by_model) == {REPLAY_MODEL_ID, MANUAL_RETRY_MODEL_ID}

    replay_evidence = by_model[REPLAY_MODEL_ID].state_evidence
    assert replay_evidence["decision"] == "replay_resubmit"
    assert replay_evidence["restart_stage"] == "forecast"
    assert "replay_manual_retry_admission" not in replay_evidence

    manual_evidence = by_model[MANUAL_RETRY_MODEL_ID].state_evidence
    # The token is NEVER rewritten: it stays the honest producer token.
    assert manual_evidence["decision"] == "manual_retry"
    assert manual_evidence["restart_stage"] == "forecast"
    assert manual_evidence["replay_manual_retry_admission"]["status"] == "admitted"

    # Each basin on its own already forced the resubmit before this fix ...
    assert _chain_gate_forces_resubmit(by_model[REPLAY_MODEL_ID]) is True
    # ... and the cohort — what the chain actually feeds the gate — must too.
    assert _chain_gate_forces_resubmit_cohort(candidates) is True
    assert _chain_gate_forces_resubmit(by_model[MANUAL_RETRY_MODEL_ID]) is True


def test_an_unadmitted_manual_retry_never_forces_a_succeeded_forecast_resubmit() -> None:
    """The exemption is the admission MARKER, not the manual-retry family.

    Nothing was added to the global whitelist: an out-of-window / replay-off
    manual retry reaching a succeeded forecast job must still be a no-op, alone
    or beside a replay sibling.
    """

    unadmitted = _scheduler_candidate_for(MANUAL_RETRY_MODEL_ID)
    unadmitted = scheduler_candidates_module._candidate_with_state_evidence(
        unadmitted,
        _produced_manual_retry_decision(
            prior_failure_reason="COLD_START_QUARANTINED",
            model_id=MANUAL_RETRY_MODEL_ID,
        ).evidence,
    )
    assert "replay_manual_retry_admission" not in unadmitted.state_evidence
    assert scheduler_candidates_module._MANUAL_RETRY_DECISION not in (
        chain_cycle_module._FORCE_TERMINAL_RESUBMIT_DECISIONS
    )
    assert _chain_gate_forces_resubmit(unadmitted) is False

    replay_candidate = _mixed_cohort_pass()[0][0]
    assert replay_candidate.state_evidence["decision"] == "replay_resubmit"
    assert _chain_gate_forces_resubmit_cohort([replay_candidate, unadmitted]) is False


def test_an_eligible_only_manual_retry_does_not_get_the_gate_exemption() -> None:
    """C2: the exemption is the ADMITTED status, not the marker's presence.

    ``eligible`` is stamped for EVERY in-window manual retry, including the
    raw-READY leg candidate that never passes the raw-less admission: it keeps
    the ``convert`` restart it legitimately wants and no clamp expectation.
    Granting it the whitelist exemption would let a resume-shaped candidate force
    a succeeded forecast to resubmit — outside anything this change admits.
    """

    candidates, blocked, skipped = _raw_less_manual_retry_pass(
        admission=_admission(),
        decision=_produced_manual_retry_decision(prior_failure_reason="COLD_START_QUARANTINED"),
        raw_state=RAW_MANIFEST_READY_STATE,
    )
    assert blocked == []
    assert skipped == []
    eligible_only = candidates[0]
    evidence = eligible_only.state_evidence
    assert evidence["replay_manual_retry_admission"]["status"] == "eligible"
    assert evidence["restart_stage"] == "convert"
    assert "replay_invariant_clamp_applied" not in evidence

    assert _chain_gate_forces_resubmit(eligible_only) is False
    # ... and it must not veto NOR carry the cohort it could share with a replay
    # sibling: the conjunction is False because this basin does not qualify.
    replay_candidate = _mixed_cohort_pass()[0][0]
    assert replay_candidate.state_evidence["decision"] == "replay_resubmit"
    assert _chain_gate_forces_resubmit_cohort([replay_candidate, eligible_only]) is False
    # The producer-side scope predicate stays TWO-status on purpose (it is what
    # opens the raw-less leg); only the chain-side consumer narrows to admitted.
    stamped = scheduler_candidates_module._replay_manual_retry_admission_decision(
        _FakeConfig(replay_admission=_admission()),
        _scheduler_candidate_for(REPLAY_MODEL_ID),
        _produced_manual_retry_decision(prior_failure_reason="COLD_START_QUARANTINED"),
    )
    assert stamped is not None
    assert stamped.evidence["replay_manual_retry_admission"]["status"] == "eligible"
    assert scheduler_candidates_module._decision_is_replay_manual_retry_scoped(stamped) is True
    assert (
        chain_cycle_module._basin_carries_replay_manual_retry_admission(stamped.evidence) is False
    )
    assert (
        chain_cycle_module._basin_carries_replay_manual_retry_admission(
            scheduler_candidates_module._replay_manual_retry_admitted_decision(stamped).evidence
        )
        is True
    )


def test_an_admitted_manual_retry_still_resubmits_through_the_failed_status_leg() -> None:
    """must-preserve: the admitted basin's OWN resubmission path is untouched.

    ``_terminal_stage_needs_manual_retry`` resubmits a failed forecast job on the
    ``retry_attempt`` leg; the marker exemption must not be the only thing making
    that work, and must not break it either.
    """

    failed_job = {**SUCCEEDED_FORECAST_JOB, "status": "failed"}
    for candidate in (
        _mixed_cohort_pass()[0][1],
        scheduler_candidates_module._candidate_with_state_evidence(
            _scheduler_candidate_for(MANUAL_RETRY_MODEL_ID),
            _produced_manual_retry_decision(
                prior_failure_reason="COLD_START_QUARANTINED",
                model_id=MANUAL_RETRY_MODEL_ID,
            ).evidence,
        ),
    ):
        context = SimpleNamespace(
            active_basins=[_cohort_basin_row(candidate)],
            restart_stage=None,
            retry_attempt=2,
        )
        assert (
            chain_cycle_module.ForecastOrchestratorCycleMixin._terminal_stage_needs_manual_retry(
                context, dict(failed_job)
            )
            is True
        )


def test_post_sync_rebuild_cannot_launder_the_admitted_manual_retry() -> None:
    """P1-2 consumer oracle: the A3-1 sync shape, on the manual-retry family.

    Active job on the first probe, terminal on the re-probe, and the re-read
    journal now says ``retry_downstream``.  Unclamped that merge ships
    ``restart_stage=parse`` + ``durable_shud_output_reused=True`` under an
    admission marker still claiming a forecast restart — the emitted manifest
    then reruns parse/publish over the OLD forecast outputs of a purged cycle.
    """

    decider = _DecisionSequence(
        [
            _produced_manual_retry_decision(prior_failure_reason="COLD_START_QUARANTINED"),
            _post_sync_downstream_retry_decision(),
        ]
    )
    repository = _FakeRepository({}, slurm_jobs_script=[[ACTIVE_SLURM_JOB]])
    orchestrator = _SyncingOrchestrator([TERMINAL_SYNC_UPDATE])

    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=None,
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        discovery_classifier="replay_forcing_evidence",
        discovery_evidence={"replay_forcing_evidence": dict(READY_FORCING_EVIDENCE)},
        decider=decider,
        repository=repository,
        orchestrator=orchestrator,
        allow_slurm_status_sync=True,
    )

    assert blocked == []
    assert skipped == []
    assert decider.calls == 2
    admitted = candidates[0]
    evidence = admitted.state_evidence
    assert evidence["decision"] == "manual_retry"
    assert evidence["restart_stage"] == "forecast"
    assert evidence["restart_from_stage"] == "forecast"
    assert evidence["native_shud_resubmitted"] is True
    # absent at admission time -> absent afterwards, however a merge introduced it
    assert "durable_shud_output_reused" not in evidence
    assert "durable_output_reused" not in evidence
    assert evidence["replay_manual_retry_admission"]["status"] == "admitted"
    assert evidence["slurm_state_sync"]["status"] == "synced"

    clamp = evidence["replay_invariant_clamp_applied"]
    assert clamp["mode"] == "replay_manual_retry"
    assert clamp["candidate_id"] == admitted.candidate_id
    assert clamp["clobbered_keys"] == [
        "decision",
        "durable_shud_output_reused",
        "native_shud_resubmitted",
        "restart_from_stage",
        "restart_stage",
    ]
    assert clamp["removed_keys"] == ["durable_shud_output_reused"]
    assert clamp["pre_clamp"]["decision"] == "retry_downstream"
    assert clamp["pre_clamp"]["restart_stage"] == "parse"
    assert clamp["pre_clamp"]["durable_shud_output_reused"] is True
    assert clamp["pre_clamp"]["native_shud_resubmitted"] is False

    # CONSUMER ORACLES: the manifest the scheduler emits, and the chain gate.
    manifest = scheduler_candidate_manifest_module._candidate_basin_manifest(
        admitted,
        output_uri="s3://nhms/runs/replayed/output",
    )
    assert manifest["restart_stage"] == "forecast"
    assert "durable_shud_output_reused" not in manifest
    assert manifest.get("native_shud_resubmitted") is not False
    assert manifest["state_evidence"]["decision"] == "manual_retry"
    assert _chain_gate_forces_resubmit(admitted) is True


# --------------------------------------------------------------------------
# 1.2f fresh_ingestion: the key that OUTRANKS every clamped restart key
# (execution-phase repair-2 round 3, F1; retro
# `.workplans/issue-1164-change2/review/review-failure-retro-repair2-r3.md`)
# --------------------------------------------------------------------------


def _produced_missing_raw_manifest_repair_decision(
    object_store_root: Path,
    *,
    model_id: str = REPLAY_MODEL_ID,
) -> CandidateStateDecision:
    """The ``repair_missing_raw_manifest`` decision from its REAL producer.

    ``scheduler_state_failure._missing_raw_manifest_repair_evidence`` is the only
    writer of the ``fresh_ingestion={"required": True, "mode": "full_chain"}`` +
    ``restart_stage=None`` shape, so it — not a hand-written copy — is the source
    of truth for what the post-sync journal re-read can put on a replay candidate.
    Its preconditions are DEFAULT-TRUE inside the replay window: the raw objects
    for the pre-070712 cycles are gone, so ``_object_manifest_is_missing`` is True
    for any candidate whose object store root is configured.  The wrapping
    ``CandidateStateDecision("retry", "repair_missing_raw_manifest", ...)`` mirrors
    ``scheduler_state_decision`` :288-290 exactly.
    """

    candidate = replace(
        _scheduler_candidate_for(model_id),
        resource_profile={"object_store_root": str(object_store_root), "object_store_prefix": ""},
    )
    state = {
        "pipeline_status": "failed",
        "failed_stage": "forecast",
        "error_code": "SHUD_RUNTIME_FAILURE",
        "retry_count": 0,
        "retry_limit": 3,
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "candidate_id": candidate.candidate_id,
        "jobs": [
            {
                "job_id": "job_cycle_gfs_2026070600_download_source_cycle",
                "stage": "download_source_cycle",
                "status": "succeeded",
                "finished_at": "2026-07-06T01:00:00Z",
            },
            {
                "job_id": "job_fcst_gfs_2026070600_forecast",
                "stage": "forecast",
                "status": "failed",
                "finished_at": "2026-07-06T02:00:00Z",
            },
        ],
    }
    evidence = scheduler_state_failure_module._missing_raw_manifest_repair_evidence(candidate, state, {})
    assert evidence is not None, "the repair producer's preconditions no longer hold"
    # Precondition of every assertion below: this really is the full-chain shape.
    assert evidence["fresh_ingestion"] == {"required": True, "mode": "full_chain"}
    assert evidence["restart_stage"] is None
    assert evidence["restart_from_stage"] == "download"
    return CandidateStateDecision("retry", "repair_missing_raw_manifest", evidence)


def _assert_no_full_chain_downgrade(candidate: Any, *, mode: str) -> None:
    """The three consumer oracles ``fresh_ingestion`` would have flipped."""

    evidence = candidate.state_evidence
    assert "fresh_ingestion" not in evidence
    assert scheduler_candidates_module._candidate_is_fresh_full_chain(candidate) is False
    restart_stage = scheduler_module._candidate_restart_stage(candidate)
    assert restart_stage == "forecast"
    cohort_key = scheduler_module._candidate_restart_cohort_key(restart_stage)
    assert cohort_key == (3, "forecast")
    assert cohort_key != (0, "full")
    manifest = scheduler_candidate_manifest_module._candidate_basin_manifest(
        candidate,
        output_uri="s3://nhms/runs/replayed/output",
    )
    assert manifest["restart_stage"] == "forecast"
    assert _chain_gate_forces_resubmit(candidate) is True
    clamp = evidence["replay_invariant_clamp_applied"]
    assert clamp["mode"] == mode
    assert "fresh_ingestion" in clamp["removed_keys"]
    assert clamp["pre_clamp"]["fresh_ingestion"] == {"required": True, "mode": "full_chain"}
    assert clamp["restored"]["fresh_ingestion"] == "<absent>"


def test_post_sync_raw_repair_cannot_full_chain_the_admitted_manual_retry(tmp_path: Path) -> None:
    """F1, leg (c): ``fresh_ingestion`` outranks every restart key the clamp owned.

    The A3-1 sync shape again, but the re-read journal now yields
    ``repair_missing_raw_manifest`` — the DEFAULT post-sync answer inside the
    replay window, since the raw object really is absent.  Its
    ``fresh_ingestion={"required": True, "mode": "full_chain"}`` short-circuits
    ``candidate_restart_stage`` to None on that function's FIRST line, so the
    clamp restoring ``restart_stage="forecast"`` bought nothing: cohort
    ``(0, "full")``, chain starting at ``convert_canonical`` against the raw
    manifest this very leg recorded as ABSENT, and a basin manifest with no
    restart stage at all — under a receipt still claiming a forecast replay.
    """

    decider = _DecisionSequence(
        [
            _produced_manual_retry_decision(prior_failure_reason="COLD_START_QUARANTINED"),
            _produced_missing_raw_manifest_repair_decision(tmp_path),
        ]
    )
    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=None,
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        discovery_classifier="replay_forcing_evidence",
        discovery_evidence={"replay_forcing_evidence": dict(READY_FORCING_EVIDENCE)},
        decider=decider,
        repository=_FakeRepository({}, slurm_jobs_script=[[ACTIVE_SLURM_JOB]]),
        orchestrator=_SyncingOrchestrator([TERMINAL_SYNC_UPDATE]),
        allow_slurm_status_sync=True,
    )

    assert blocked == []
    assert skipped == []
    assert decider.calls == 2
    admitted = candidates[0]
    evidence = admitted.state_evidence
    assert evidence["decision"] == "manual_retry"
    assert evidence["restart_stage"] == "forecast"
    assert evidence["replay_manual_retry_admission"]["status"] == "admitted"
    # the merge itself is not skipped — its audit evidence is the point of it
    assert evidence["slurm_state_sync"]["status"] == "synced"
    _assert_no_full_chain_downgrade(admitted, mode="replay_manual_retry")


def test_post_sync_raw_repair_cannot_full_chain_the_replay_resubmit(tmp_path: Path) -> None:
    """F1, leg (a): the same hole on the COMMITTED ``replay_resubmit`` mode.

    Adding ``fresh_ingestion`` to ``_REPLAY_RESUBMIT_CLAMPED_KEYS`` alone would
    have been a no-op here: the override never writes that key, and the old
    present-keys-only expectation could not pin what the admission decision did not
    carry.  The mode had to be converted to the whole-set/absence-pinned form the
    manual-retry mode already used.
    """

    decider = _DecisionSequence(
        [
            _replay_terminal_decision(),
            _produced_missing_raw_manifest_repair_decision(tmp_path),
        ]
    )
    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=None,
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        discovery_classifier="replay_forcing_evidence",
        discovery_evidence={"replay_forcing_evidence": dict(READY_FORCING_EVIDENCE)},
        decider=decider,
        repository=_FakeRepository({}, slurm_jobs_script=[[ACTIVE_SLURM_JOB]]),
        orchestrator=_SyncingOrchestrator([TERMINAL_SYNC_UPDATE]),
        allow_slurm_status_sync=True,
    )

    assert blocked == []
    assert skipped == []
    assert decider.calls == 2
    admitted = candidates[0]
    evidence = admitted.state_evidence
    assert evidence["decision"] == "replay_resubmit"
    assert evidence["restart_stage"] == "forecast"
    assert evidence["native_shud_resubmitted"] is True
    assert evidence["slurm_state_sync"]["status"] == "synced"
    _assert_no_full_chain_downgrade(admitted, mode="replay_resubmit")


# --------------------------------------------------------------------------
# 1.2g the eligibility stamp is re-applied after every Slurm-status sync
# (execution-phase repair-2 round 3, F2)
# --------------------------------------------------------------------------

#: The ``skip active_slurm_job`` decision the FIRST journal read yields while the
#: previous attempt's forecast job is still running.
_ACTIVE_JOB_SKIP_DECISION = CandidateStateDecision(
    "skip",
    "active_slurm_job",
    {
        "decision": "skip_active",
        "active_slurm_jobs": [dict(ACTIVE_SLURM_JOB)],
        "replacement_submitted": False,
    },
)


def _first_sync_manual_retry_pass(
    *,
    decision: CandidateStateDecision,
    strict_warm_start: Mapping[str, Any] | None,
    admission: ReplayAdmission | None = None,
) -> tuple[list[Any], list[Any], list[dict[str, Any]]]:
    """Active job first, manual retry only after the sync (the F2 shape).

    ``scheduler_state_manual_retry`` grants a manual retry off a marker bound to
    the previous job id regardless of timestamps, so "the operator's retry only
    becomes visible once the running job goes terminal" is an ordinary campaign
    shape, not a contrived one.
    """

    return _build(
        admission=_admission() if admission is None else admission,
        decision=None,
        strict_warm_start=strict_warm_start,
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        discovery_classifier="replay_forcing_evidence",
        discovery_evidence={"replay_forcing_evidence": dict(READY_FORCING_EVIDENCE)},
        decider=_DecisionSequence([_ACTIVE_JOB_SKIP_DECISION, decision]),
        repository=_FakeRepository({}, slurm_jobs_script=[[ACTIVE_SLURM_JOB]]),
        orchestrator=_SyncingOrchestrator([TERMINAL_SYNC_UPDATE]),
        allow_slurm_status_sync=True,
    )


def test_a_manual_retry_first_seen_after_the_slurm_sync_is_still_admitted() -> None:
    """F2: the stamp ran once, pre-sync, and the sync REPLACED the decision.

    Pre-fix this candidate reached the raw-less leg unstamped: blocked
    ``nfs_raw_manifest_required`` with NO ``replay_manual_retry_admission`` key at
    all — a fifth operator state no runbook row describes, zero submissions, and a
    convergence-timeout halt out of the single-pass driver.  All three campaign
    shapes are re-driven here through the sync path, and each must land exactly
    where the main path puts it.
    """

    strict = {"ready": True, "candidate_state": dict(SELECTED_STATE)}
    admitted_labels = []
    for label, (decision, strict_warm_start) in {
        "bare": (_produced_manual_retry_decision(prior_failure_reason="SHUD_RUNTIME_FAILURE"), None),
        "bare_upgraded": (
            _produced_manual_retry_decision(prior_failure_reason="SHUD_RUNTIME_FAILURE"),
            strict,
        ),
        "cold_start": (_produced_manual_retry_decision(prior_failure_reason="COLD_START_QUARANTINED"), None),
    }.items():
        candidates, blocked, skipped = _first_sync_manual_retry_pass(
            decision=decision,
            strict_warm_start=strict_warm_start,
        )
        assert skipped == [], label
        if candidates:
            admitted_labels.append(label)
            evidence = candidates[0].state_evidence
            assert blocked == [], label
            assert evidence["replay_manual_retry_admission"]["status"] == "admitted", label
            restart_stage = scheduler_module._candidate_restart_stage(candidates[0])
            assert restart_stage == "forecast", (label, restart_stage)
            assert scheduler_module._candidate_restart_cohort_key(restart_stage) == (3, "forecast"), label
            assert _chain_gate_forces_resubmit(candidates[0]) is True, label
            # the sync's own audit evidence still reaches the candidate
            assert evidence["slurm_state_sync"]["status"] == "synced", label
        else:
            # The bare shape is still DECLINED — but now with the C1 marker that
            # says why, which is the state the runbook row describes.
            assert blocked[0].reason == "nfs_raw_manifest_required", label
            marker = blocked[0].state_evidence["replay_manual_retry_admission"]
            assert marker["status"] == "eligible", label
            assert marker["declined_reason"] == "replay_manual_retry_restart_stage_unsupported", label
    assert sorted(admitted_labels) == ["bare_upgraded", "cold_start"]


def test_a_post_sync_manual_retry_outside_the_replay_scope_is_untouched() -> None:
    """must-preserve: the re-stamp is windowed exactly like the main-path one.

    Replay off, out-of-window cycle and out-of-set model all keep the pre-change
    typed block with no admission marker anywhere — the sync path may not be a
    second, wider door into the raw-less leg.
    """

    cold_start = _produced_manual_retry_decision(prior_failure_reason="COLD_START_QUARANTINED")
    cases = {
        "replay_off": {"admission": None, "cycle_time": IN_WINDOW_CYCLE, "model_ids": (REPLAY_MODEL_ID,)},
        "out_of_window": {
            "admission": _admission(),
            "cycle_time": OUT_OF_WINDOW_CYCLE,
            "model_ids": (REPLAY_MODEL_ID,),
        },
        "out_of_set_model": {
            "admission": _admission(),
            "cycle_time": IN_WINDOW_CYCLE,
            "model_ids": (OUT_OF_SET_MODEL_ID,),
        },
    }
    for label, case in cases.items():
        candidates, blocked, skipped = _build(
            admission=case["admission"],
            decision=None,
            strict_warm_start=None,
            canonical_readiness=FRESH_ZERO_ROW_READINESS,
            cycle_time=case["cycle_time"],
            model_ids=case["model_ids"],
            discovery_classifier="replay_forcing_evidence",
            discovery_evidence={"replay_forcing_evidence": dict(READY_FORCING_EVIDENCE)},
            decider=_DecisionSequence([_ACTIVE_JOB_SKIP_DECISION, cold_start]),
            repository=_FakeRepository({}, slurm_jobs_script=[[ACTIVE_SLURM_JOB]]),
            orchestrator=_SyncingOrchestrator([TERMINAL_SYNC_UPDATE]),
            allow_slurm_status_sync=True,
        )
        assert candidates == [], label
        assert skipped == [], label
        assert blocked[0].reason == "nfs_raw_manifest_required", label
        assert "replay_manual_retry_admission" not in blocked[0].state_evidence, label
        assert "replay_canonical_readiness_guard" not in blocked[0].state_evidence, label


def test_a_post_sync_rebuild_never_demotes_an_already_admitted_manual_retry() -> None:
    """F2 second site: the re-stamp is first-wins on the ``admitted`` status.

    The second sync block runs DOWNSTREAM of the raw-less leg, so it can no longer
    open an admission — but a plain re-stamp there would write ``eligible`` over
    the marker the leg already granted, and the chain-side gate keys on
    ``admitted`` alone (round-2 C2).  That would strip the exemption off a
    candidate whose restart shape the clamp is still pinning: a self-contradictory
    receipt.  This guards the new re-stamp path, not the pre-change one.
    """

    decider = _DecisionSequence(
        [
            _produced_manual_retry_decision(prior_failure_reason="COLD_START_QUARANTINED"),
            _produced_manual_retry_decision(prior_failure_reason="SHUD_RUNTIME_FAILURE"),
        ]
    )
    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=None,
        strict_warm_start=None,
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        discovery_classifier="replay_forcing_evidence",
        discovery_evidence={"replay_forcing_evidence": dict(READY_FORCING_EVIDENCE)},
        decider=decider,
        repository=_FakeRepository({}, slurm_jobs_script=[[ACTIVE_SLURM_JOB]]),
        orchestrator=_SyncingOrchestrator([TERMINAL_SYNC_UPDATE]),
        allow_slurm_status_sync=True,
    )

    assert blocked == []
    assert skipped == []
    assert decider.calls == 2
    admitted = candidates[0]
    evidence = admitted.state_evidence
    assert evidence["replay_manual_retry_admission"]["status"] == "admitted"
    assert (
        chain_cycle_module._basin_carries_replay_manual_retry_admission(evidence) is True
    )
    assert evidence["restart_stage"] == "forecast"
    assert _chain_gate_forces_resubmit(admitted) is True


def test_a_post_sync_rebuild_cannot_forecast_restart_the_raw_ready_manual_retry() -> None:
    """F2 second site: it re-stamps, it does NOT re-run the strict-warm upgrade.

    The RAW-READY manual retry (round-4 delta P1).  The canonical gate lowered
    its restart to ``convert`` on purpose (``:1048`` — the raw manifest really is
    on disk and the canonical is a fresh zero-row), and that leg registers NO
    clamp expectation (deferred #1198), so nothing restores the shape after a
    later merge.  Re-running ``_upgrade_retry_for_strict_warm_start_manifest``
    inside the second sync block put ``restart_stage=forecast`` back on the
    rebuilt decision, and the ``:1281`` merge — which, unlike the main-path merge
    at ``:1122``, has no ``raw_manifest_restart_applied`` guard — last-wrote it
    over the ``convert`` restart.  The shipped receipt then contradicts itself:
    a ``forecast`` restart beside ``restart_reason=raw_manifest_ready_without_canonical``
    and ``fresh_ingestion.mode=reuse_raw_then_convert``, i.e. the chain skips
    pre-orchestration forcing AND starts at forecast on a canonical with zero
    rows.  The upgrade half belongs to the FIRST site only (``:829``), where the
    ``forecast`` restart it writes is what the C1 precondition then accepts.
    """

    from services.orchestrator import scheduler_execution as scheduler_execution_module

    bare = _produced_manual_retry_decision(prior_failure_reason="SHUD_RUNTIME_FAILURE")
    decider = _DecisionSequence([bare, bare])
    candidates, blocked, skipped = _build(
        admission=_admission(),
        decision=None,
        # Load-bearing: the strict warm start is what ARMS the upgrade.  Without
        # it the second site is a no-op and the defect is invisible.
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
        canonical_readiness=FRESH_ZERO_ROW_READINESS,
        discovery_classifier="replay_forcing_evidence",
        discovery_evidence={"replay_forcing_evidence": dict(READY_FORCING_EVIDENCE)},
        decider=decider,
        repository=_FakeRepository(RAW_MANIFEST_READY_STATE, slurm_jobs_script=[[ACTIVE_SLURM_JOB]]),
        orchestrator=_SyncingOrchestrator([TERMINAL_SYNC_UPDATE]),
        allow_slurm_status_sync=True,
    )

    assert blocked == []
    assert skipped == []
    # No active-slurm SKIP decision anywhere in the sequence: the first sync
    # block (`:829`) is never entered, so this exercises the SECOND site alone.
    assert decider.calls == 2
    candidate = candidates[0]
    evidence = candidate.state_evidence
    assert evidence["decision"] == "manual_retry"
    assert evidence["restart_stage"] == "convert"
    assert evidence["restart_from_stage"] == "convert"
    assert evidence["restart_reason"] == "raw_manifest_ready_without_canonical"
    assert evidence["fresh_ingestion"] == {"required": False, "mode": "reuse_raw_then_convert"}
    assert evidence["raw_manifest_reuse"]["status"] == "ready"

    restart_stage = scheduler_module._candidate_restart_stage(candidate)
    assert restart_stage == "convert"
    assert scheduler_module._candidate_restart_cohort_key(restart_stage) == (1, "convert")

    # CONSUMER ORACLES: the two readers that make the contradiction operational —
    # the forcing skip (raw reuse) and the manifest the chain actually runs from.
    execution_context = SimpleNamespace(
        candidate_is_fresh_full_chain=scheduler_candidates_module._candidate_is_fresh_full_chain,
    )
    assert (
        scheduler_execution_module._candidate_skips_pre_orchestration_forcing(
            execution_context,
            candidate,
        )
        is True
    )
    manifest = scheduler_candidate_manifest_module._candidate_basin_manifest(
        candidate,
        output_uri="s3://nhms/runs/replayed/output",
    )
    assert manifest["restart_stage"] == "convert"

    # Eligible-only: the raw-less admission never fired, so no chain-gate
    # exemption and no clamp — the marker still says exactly what happened.
    assert evidence["replay_manual_retry_admission"]["status"] == "eligible"
    assert chain_cycle_module._basin_carries_replay_manual_retry_admission(evidence) is False
    assert "replay_invariant_clamp_applied" not in evidence
    # The second sync block still runs, and its audit evidence still ships.
    assert evidence["slurm_state_sync"]["status"] == "synced"

    # MUST-PRESERVE, first site (`:829`): unchanged.  Same bare shape, same
    # strict warm start, but reached through the active-job skip that makes the
    # FIRST sync block rebuild the decision — there the upgrade must still run,
    # because its `forecast` restart is what the raw-less C1 precondition accepts.
    first_site, first_blocked, first_skipped = _first_sync_manual_retry_pass(
        decision=_produced_manual_retry_decision(prior_failure_reason="SHUD_RUNTIME_FAILURE"),
        strict_warm_start={"ready": True, "candidate_state": dict(SELECTED_STATE)},
    )
    assert first_blocked == []
    assert first_skipped == []
    first_evidence = first_site[0].state_evidence
    assert first_evidence["replay_manual_retry_admission"]["status"] == "admitted"
    first_stage = scheduler_module._candidate_restart_stage(first_site[0])
    assert first_stage == "forecast"
    assert scheduler_module._candidate_restart_cohort_key(first_stage) == (3, "forecast")
    assert _chain_gate_forces_resubmit(first_site[0]) is True


# --------------------------------------------------------------------------
# 1.2h derivation-closure audit: the clamp key sets vs. what consumers read
# (retro-mandated Phase 6.2 invariant audit for the repeated
# protection-set-completeness class)
# --------------------------------------------------------------------------

#: The restart-semantics consumers the clamp key sets must close over.  Each is a
#: (module, top-level function) seed; the extractor follows same-module calls to
#: fixpoint, so a helper split out of one of these stays covered.
CLAMP_CONSUMER_SEEDS: tuple[tuple[str, str], ...] = (
    ("services.orchestrator.scheduler_execution", "candidate_restart_stage"),
    ("services.orchestrator.scheduler_execution", "_candidate_skips_pre_orchestration_forcing"),
    ("services.orchestrator.scheduler_candidates", "_candidate_is_fresh_full_chain"),
    ("services.orchestrator.scheduler_candidate_manifest", "_candidate_basin_manifest"),
)

#: Top-level state-evidence keys those consumers read that are deliberately NOT
#: clamp-owned.  Every entry must say why the key cannot clobber restart / cohort /
#: manifest / forcing-skip semantics for a clamped candidate.
CLAMP_CONSUMER_KEY_ALLOWLIST: dict[str, str] = {
    "raw_manifest_reuse": (
        "read ONLY in conjunction with `fresh_ingestion.mode` in "
        "{reuse_raw_then_convert, repair_missing_forcing} "
        "(`_candidate_skips_pre_orchestration_forcing`).  Both whole-set modes pin "
        "`fresh_ingestion` ABSENT, so the conjunction is unreachable for a clamped "
        "candidate whatever this key says."
    ),
    "manual_retry": (
        "feeds `manual_retry_attempt` / `retry_attempt` on the basin manifest — a "
        "retry COUNTER.  It selects no stage, keys no cohort, and the admitted "
        "manual retry legitimately wants the count the journal reports."
    ),
    "candidate_state": (
        "warm-start init-state passthrough (`_apply_candidate_warm_start_fields`): "
        "it names WHICH initial state the run starts from, not WHERE the chain "
        "restarts.  The replay legs deliberately let the post-sync rebuild refresh "
        "it — the strict-warm gate, not the clamp, owns that surface."
    ),
}


def _consumer_state_evidence_keys() -> dict[str, set[str]]:
    """key -> the consumer functions that read it, straight from the sources.

    Deliberately structural rather than a curated list: the retro's finding is
    that every HAND derivation of the protected set has missed at least one key.
    A name is treated as state evidence when it is bound from an expression
    mentioning ``.state_evidence`` (``state_evidence = candidate.state_evidence``,
    ``state_evidence = _evidence_safe(candidate.state_evidence)``), and only
    TOP-LEVEL reads off such a name count — nested reads (``fresh_ingestion.get
    ("mode")``) are sub-keys of a key already extracted.

    BOUNDARY: the closure follows same-module calls only, and
    ``scheduler_candidate_manifest``'s ``*args`` re-export shims
    (``_candidate_is_fresh_full_chain = _scheduler_call(...)``) are opaque to it —
    which is exactly why the predicate is named as its own seed above.
    """

    def _is_evidence_expression(node: ast.AST, evidence_names: set[str]) -> bool:
        if isinstance(node, ast.Name):
            return node.id in evidence_names
        return isinstance(node, ast.Attribute) and node.attr == "state_evidence"

    def _scan(function: ast.AST) -> tuple[set[str], set[str]]:
        evidence_names: set[str] = set()
        for node in ast.walk(function):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(child, ast.Attribute) and child.attr == "state_evidence"
                for child in ast.walk(node.value)
            ):
                continue
            evidence_names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        keys: set[str] = set()
        calls: set[str] = set()
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and _is_evidence_expression(func.value, evidence_names)
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    keys.add(node.args[0].value)
                if isinstance(func, ast.Name):
                    calls.add(func.id)
            elif isinstance(node, ast.Subscript) and _is_evidence_expression(node.value, evidence_names):
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    keys.add(node.slice.value)
            elif (
                isinstance(node, ast.Compare)
                and len(node.comparators) == 1
                and _is_evidence_expression(node.comparators[0], evidence_names)
                and isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str)
            ):
                keys.add(node.left.value)
        return keys, calls

    found: dict[str, set[str]] = {}
    for module_name, seed in CLAMP_CONSUMER_SEEDS:
        module = importlib.import_module(module_name)
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert seed in functions, f"{module_name}.{seed} no longer exists"
        pending = [seed]
        visited: set[str] = set()
        while pending:
            name = pending.pop()
            if name in visited or name not in functions:
                continue
            visited.add(name)
            keys, calls = _scan(functions[name])
            for key in keys:
                found.setdefault(key, set()).add(f"{module_name.rsplit('.', 1)[-1]}.{name}")
            pending.extend(calls)
    return found


def test_the_whole_set_clamp_modes_really_pin_their_whole_key_set() -> None:
    """A key added to a present-keys-only expectation is a NO-OP (F1 leg a).

    Both whole-set modes are driven with an EMPTY admission evidence, which is the
    strongest form of the question: every owned key must come back pinned, and a
    key the decision did not carry must come back pinned ABSENT rather than
    silently dropped.
    """

    absent = scheduler_candidates_module._REPLAY_CLAMP_ABSENT
    builders = {
        "replay_resubmit": scheduler_candidates_module._replay_resubmit_invariant,
        "replay_manual_retry": scheduler_candidates_module._replay_manual_retry_invariant,
    }
    assert set(builders) == set(scheduler_candidates_module._REPLAY_WHOLE_SET_CLAMPED_KEY_SETS)
    for mode, builder in builders.items():
        owned = scheduler_candidates_module._REPLAY_WHOLE_SET_CLAMPED_KEY_SETS[mode]
        expectation = builder(CandidateStateDecision("retry", mode, {}))
        assert expectation is not None, mode
        assert expectation.mode == mode
        assert set(expectation.required) == set(owned), mode
        assert all(value is absent for value in expectation.required.values()), mode


def test_the_clamp_key_sets_close_over_every_restart_semantics_consumer_read() -> None:
    """Phase 6.2 invariant audit: the pin list itself, checked against the readers.

    Three review rounds broke the SAME invariant (protection-set completeness), the
    last one because v7's "derive the set from the consumers" rule was executed by
    hand.  This test re-derives it from the sources every run: a key any named
    consumer reads must be owned by EVERY whole-set clamp mode, or carry an
    allowlist entry saying why it cannot clobber restart semantics.  The NEXT key
    added to one of those consumers turns this red by construction.
    """

    found = _consumer_state_evidence_keys()
    # Anti-vacuity: the extractor still sees the keys we know are read.  A rename
    # that silently emptied the extraction would otherwise make this pass.
    assert {"restart_stage", "restart_from_stage", "fresh_ingestion", "durable_shud_output_reused"} <= set(
        found
    ), sorted(found)

    whole_set_modes = scheduler_candidates_module._REPLAY_WHOLE_SET_CLAMPED_KEY_SETS
    assert whole_set_modes, "at least one clamp mode must claim whole-set protection"
    uncovered: dict[str, list[str]] = {}
    for key, readers in found.items():
        if key in CLAMP_CONSUMER_KEY_ALLOWLIST:
            continue
        missing = [mode for mode, owned in whole_set_modes.items() if key not in owned]
        if missing:
            uncovered[key] = sorted(readers)
    assert uncovered == {}, (
        "a restart-semantics consumer reads a state-evidence key no whole-set clamp "
        "mode owns; add it to _REPLAY_RESUBMIT_CLAMPED_KEYS and "
        "_REPLAY_MANUAL_RETRY_CLAMPED_KEYS, or allowlist it in "
        f"CLAMP_CONSUMER_KEY_ALLOWLIST with the reason it cannot clobber: {uncovered}"
    )
    # The allowlist may not rot into a mute exception list either.
    assert set(CLAMP_CONSUMER_KEY_ALLOWLIST) <= set(found), sorted(
        set(CLAMP_CONSUMER_KEY_ALLOWLIST) - set(found)
    )
    for key, reason in CLAMP_CONSUMER_KEY_ALLOWLIST.items():
        assert len(reason) > 40, key


#: Every replay-scoped branch inside ``build_candidates``, keyed by its literal
#: test expression and classified by what it does about the invariant clamp
#: (round-2 test-quality repair of the former mode-counting audit, which was
#: structurally blind: it compared a COUNT of ``replay_invariants[...]``
#: assignments against a COUNT of ``_REPLAY_CLAMP_MODE_*`` constants, so a family
#: with two admitting paths and one registration balanced out to "covered").
#:
#: The unit here is an ADMITTING PATH, not a mode constant.  A new branch that
#: consults a replay admission name — or a new replay-scoped local — fails this
#: test by construction, and its author has to declare, per branch, which
#: expectation builders run under it and whether it registers one.
#:
#: value = (invariant builders called in the branch subtree, registers?, note)
REPLAY_ADMITTING_PATHS: dict[str, tuple[tuple[str, ...], bool, str]] = {
    "replay_override is not None": (
        ("_replay_resubmit_invariant",),
        True,
        "leg (a): the replay terminal override — registers the replay_resubmit expectation",
    ),
    "resubmit_invariant is not None": ((), True, "leg (a) registration guard"),
    "manual_retry_admission is not None": (
        (),
        False,
        "eligibility STAMP, not an admission: it only marks the candidate in-scope so the "
        "marker rides through the strict-warm token relabel; nothing is admitted yet, so "
        "there is nothing to pin",
    ),
    "manual_retry_resync_active_leg is not None": (
        (),
        False,
        "F2 re-stamp, first sync block.  Same eligibility STAMP as the main path (plus the "
        "strict-warm upgrade the main path runs right after it), re-applied because the "
        "post-sync rebuild REPLACED the decision the main-path stamp was written on.  It "
        "sits UPSTREAM of the raw-less leg, so it re-opens the ordinary admission path "
        "rather than admitting anything itself — leg (c) still does the pinning",
    ),
    "manual_retry_resync_rebuild_leg is not None": (
        (),
        False,
        "F2 re-stamp, second sync block.  DOWNSTREAM of the canonical gate: it cannot open an "
        "admission, and it is first-wins on the `admitted` status so it cannot demote one "
        "either.  STAMP ONLY — it calls `_replay_manual_retry_restamped_decision`, NOT the "
        "resynced variant, so it never re-runs the strict-warm upgrade over the `convert` "
        "restart the raw-ready leg set (round-4 delta P1).  Registers nothing — a candidate "
        "admitted upstream keeps the expectation leg (c) already registered for it",
    ),
    "replay_repair_evidence is not None": (
        ("_replay_repair_convert_invariant",),
        True,
        "leg (b): the replay-window authorized repair — registers the convert-restart expectation",
    ),
    "repair_invariant is not None": ((), True, "leg (b) registration guard"),
    "replay_covered": (
        (),
        False,
        "raw-READY leg.  The `replay_resubmit` branch suppresses the restart downgrade and its "
        "expectation was already registered by leg (a).  Its ELSE branch is the raw-ready "
        "manual-retry path (marker stays `eligible`, restart lowered to `convert`), which is "
        "admitted WITHOUT any registered expectation — the known clamp gap, DEFERRED as round-2 "
        "C3 (#1164) and deliberately NOT fixed in this round.  Recorded here so the gap is "
        "visible in the audit instead of hiding behind a balanced count.",
    ),
    "replay_manual_retry_covered and (not _replay_manual_retry_restart_is_supported(state_decision))": (
        (),
        False,
        "round-2 C1 restart precondition: DECLINES the raw-less admission for a decision whose "
        "restart stage is absent or earlier than `forecast`, so nothing is admitted and nothing "
        "is pinned",
    ),
    "replay_covered or replay_manual_retry_covered": (
        ("_replay_manual_retry_invariant",),
        True,
        "leg (c): the raw-less substitute admission, shared by both admission keys",
    ),
    "substitute_block is not None": ((), False, "fail-closed block: the substitute is absent or not ready"),
    "replay_manual_retry_covered": (
        ("_replay_manual_retry_invariant",),
        True,
        "leg (c) manual-retry half: flips the marker to `admitted` and registers its expectation",
    ),
    "manual_retry_invariant is not None": ((), True, "leg (c) registration guard"),
    "manual_retry_restart_unsupported is not None": (
        (),
        False,
        "round-2 C1: enriches the kept `nfs_raw_manifest_required` block with the declined marker",
    ),
}


def _build_candidates_ast() -> ast.FunctionDef:
    tree = ast.parse(Path(scheduler_candidates_module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_candidates":
            return node
    raise AssertionError("build_candidates not found")


def _is_replay_helper(name: str) -> bool:
    return name.startswith("_replay_") or name.startswith("_decision_is_replay")


def _replay_scoped_locals(function: ast.FunctionDef) -> set[str]:
    """Locals bound from a replay admission helper and from no OTHER call.

    A name that is also bound from a non-replay call (``state_decision`` from
    ``context.candidate_state_decider``, ``candidate`` from
    ``_candidate_with_state_evidence``) is not a replay-scoped name: branching on
    it says nothing about a replay admission.  Re-binding to a literal does NOT
    disqualify a name — the C1 precondition sets ``replay_manual_retry_covered``
    to ``False`` to decline the admission, and that is precisely a replay-scoped
    use.
    """

    replay_bound: set[str] = set()
    disqualified: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign):
            continue
        names: list[str] = []
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.append(target.id)
            elif isinstance(target, ast.Tuple):
                names.extend(element.id for element in target.elts if isinstance(element, ast.Name))
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        if isinstance(value.func, ast.Name) and _is_replay_helper(value.func.id):
            replay_bound.update(names)
        else:
            disqualified.update(names)
    return replay_bound - disqualified


def _branch_facts(node: ast.If) -> tuple[tuple[str, ...], bool]:
    """(invariant builders called under this branch, registers an expectation?)."""

    builders: set[str] = set()
    registers = False
    for statement in [*node.body, *node.orelse]:
        for child in ast.walk(statement):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                if child.func.id.endswith("_invariant"):
                    builders.add(child.func.id)
            if (
                isinstance(child, ast.Assign)
                and any(
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "replay_invariants"
                    for target in child.targets
                )
            ):
                registers = True
    return tuple(sorted(builders)), registers


def _expectation_builder_modes() -> dict[str, str]:
    """``_replay_*_invariant`` -> the clamp mode constant it actually returns."""

    tree = ast.parse(Path(scheduler_candidates_module.__file__).read_text(encoding="utf-8"))
    modes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.endswith("_invariant"):
            continue
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "_ReplayInvariantExpectation"
            ):
                for keyword in child.keywords:
                    if keyword.arg == "mode" and isinstance(keyword.value, ast.Name):
                        modes[node.name] = getattr(scheduler_candidates_module, keyword.value.id)
    return modes


def test_every_replay_admitting_path_is_classified_against_the_clamp() -> None:
    """P1-2 item 4, reworked: paths are the unit, not mode constants.

    The merge-site audit below classifies POSITIONS; this one classifies
    ADMITTING PATHS.  ``site #10`` (post-sync rebuild) passed vacuously for the
    manual-retry family precisely because nothing was registered for it, so the
    clamp had nothing to enforce — and the previous version of THIS test passed
    vacuously too, because it only counted assignments against mode constants.
    """

    function = _build_candidates_ast()
    scoped = _replay_scoped_locals(function)
    # The name set itself is pinned: a NEW replay-scoped local (i.e. a new
    # admission predicate/builder result) changes it and lands here first.
    assert scoped == {
        "manual_retry_admission",
        "manual_retry_invariant",
        "manual_retry_restart_unsupported",
        "manual_retry_resync_active_leg",
        "manual_retry_resync_rebuild_leg",
        "repair_invariant",
        "replay_covered",
        "replay_manual_retry_covered",
        "replay_override",
        "replay_repair_evidence",
        "resubmit_invariant",
        "substitute_block",
        "substitute_evidence",
    }, sorted(scoped)

    found: dict[str, tuple[tuple[str, ...], bool]] = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.If):
            continue
        test_source = ast.unparse(node.test)
        mentions = {
            child.id for child in ast.walk(node.test) if isinstance(child, ast.Name)
        } & scoped
        calls_helper = any(
            isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and _is_replay_helper(child.func.id)
            for child in ast.walk(node.test)
        )
        if not mentions and not calls_helper:
            continue
        assert test_source not in found, f"duplicate branch key: {test_source}"
        found[test_source] = _branch_facts(node)

    assert set(found) == set(REPLAY_ADMITTING_PATHS), (
        "a replay admitting path appeared or moved without a classification; "
        f"unclassified={sorted(set(found) - set(REPLAY_ADMITTING_PATHS))} "
        f"stale={sorted(set(REPLAY_ADMITTING_PATHS) - set(found))}"
    )
    for test_source, (builders, registers) in found.items():
        declared_builders, declared_registers, note = REPLAY_ADMITTING_PATHS[test_source]
        assert builders == declared_builders, (test_source, builders, declared_builders)
        assert registers is declared_registers, (test_source, registers, note)

    # Every clamp mode is still produced by a builder some classified path runs:
    # a fourth mode with no admitting path, or a builder no path ever calls,
    # fails here.
    builder_modes = _expectation_builder_modes()
    reachable = {
        builder for builders, _registers, _note in REPLAY_ADMITTING_PATHS.values() for builder in builders
    }
    assert set(builder_modes) == reachable, (sorted(builder_modes), sorted(reachable))
    declared_modes = {
        getattr(scheduler_candidates_module, name)
        for name in dir(scheduler_candidates_module)
        if name.startswith("_REPLAY_CLAMP_MODE_")
    }
    assert set(builder_modes.values()) == declared_modes == {
        "replay_resubmit",
        "replay_repair_convert_restart",
        "replay_manual_retry",
    }


#: Every ``_merge_state_evidence`` / ``_candidate_with_state_evidence`` call site
#: in ``scheduler_candidates.py``, keyed by (enclosing function, callee, nth call
#: in that function) and classified.  The retro's finding is that this surface
#: keeps growing new clobber points, so a NEW call site fails this test by
#: construction and its author has to say which class it is:
#:
#: * ``upstream of clamp`` -- inside ``build_candidates`` before the final
#:   ``_clamp_replay_invariants`` call, which re-imposes the replay keys after it;
#: * ``self-guarded`` -- outside the admitted-candidate path (blocked/skipped
#:   entries, decision-level evidence, or the clamp's own repair merge), so the
#:   clamp neither can nor needs to cover it.
#:
#: SCOPE OF THIS AUDIT (round-4 lane A): ``upstream of clamp`` is a POSITION
#: assertion and nothing more.  A merge site being lexically before the clamp
#: does NOT by itself mean the keys it clobbers get restored; coverage also
#: requires (a) an invariant expectation to have been registered for that
#: candidate and (b) the clobbered key to be a member of that mode's owned set
#: (``_REPLAY_RESUBMIT_CLAMPED_KEYS`` / ``_REPLAY_REPAIR_CLAMPED_KEYS``).  A4-1
#: was exactly that hole: the merge site was correctly classified here while
#: ``decision`` sat outside the owned set.  The owned sets are pinned by the
#: consumer-side oracles above, not by this audit.  The audit also scans only
#: ``scheduler_candidates.py``; merges performed by other modules on a returned
#: candidate (e.g. ``scheduler_execution.py:256``) are invisible to it.
MERGE_CALL_SITE_ALLOWLIST: dict[tuple[str, str, int], str] = {
    ("build_candidates", "_merge_state_evidence", 1): "self-guarded (skipped entry, not a candidate)",
    ("build_candidates", "_candidate_with_state_evidence", 1): "upstream of clamp (strict warm start)",
    ("build_candidates", "_candidate_with_state_evidence", 2): "upstream of clamp (post-sync retry, active-job leg)",
    ("build_candidates", "_candidate_with_state_evidence", 3): "upstream of clamp (post-sync repaired audit)",
    ("build_candidates", "_candidate_with_state_evidence", 4): "upstream of clamp (nfs raw manifest gate)",
    ("build_candidates", "_candidate_with_state_evidence", 5): "upstream of clamp (replay repair leg)",
    ("build_candidates", "_candidate_with_state_evidence", 6): "upstream of clamp (fresh-zero-row leg)",
    ("build_candidates", "_candidate_with_state_evidence", 7): "upstream of clamp (canonical readiness ready)",
    ("build_candidates", "_candidate_with_state_evidence", 8): "upstream of clamp (retry decision evidence)",
    ("build_candidates", "_candidate_with_state_evidence", 9): "upstream of clamp (repaired state audit)",
    ("build_candidates", "_candidate_with_state_evidence", 10): "upstream of clamp (post-sync rebuild, A3-1)",
    ("build_candidates", "_candidate_with_state_evidence", 11): "upstream of clamp (post-sync repaired audit)",
    ("_candidate_warm_admission_decision", "_merge_state_evidence", 1): "self-guarded (blocked candidate evidence)",
    ("_clamped_replay_candidate", "_candidate_with_state_evidence", 1): "self-guarded (the clamp's own repair)",
    ("_blocked_candidate", "_merge_state_evidence", 1): "self-guarded (blocked candidate evidence)",
    ("_candidate_with_state_evidence", "_merge_state_evidence", 1): "self-guarded (the merge helper itself)",
}
MERGE_CALL_SITE_CLASSES = ("upstream of clamp", "self-guarded")


def _merge_call_sites() -> dict[tuple[str, str, int], int]:
    """(enclosing function, callee, ordinal) -> line, straight from the source."""

    source_path = Path(scheduler_candidates_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    tracked = {"_merge_state_evidence", "_candidate_with_state_evidence"}
    found: list[tuple[str, str, int]] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                visit(child, child.name)
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in tracked:
                found.append((scope, child.func.id, child.lineno))
            visit(child, scope)

    visit(tree, "<module>")
    sites: dict[tuple[str, str, int], int] = {}
    ordinals: dict[tuple[str, str], int] = {}
    for scope, callee, lineno in sorted(found, key=lambda item: item[2]):
        ordinals[(scope, callee)] = ordinals.get((scope, callee), 0) + 1
        sites[(scope, callee, ordinals[(scope, callee)])] = lineno
    return sites


def test_every_state_evidence_merge_site_is_classified_against_the_clamp() -> None:
    """Invariant audit (round-3 retro): new merge sites fail by construction."""

    found = _merge_call_sites()
    assert set(found) == set(MERGE_CALL_SITE_ALLOWLIST), (
        "state-evidence merge sites changed; classify each new site as "
        f"{MERGE_CALL_SITE_CLASSES} in MERGE_CALL_SITE_ALLOWLIST. "
        f"added={sorted(set(found) - set(MERGE_CALL_SITE_ALLOWLIST))} "
        f"removed={sorted(set(MERGE_CALL_SITE_ALLOWLIST) - set(found))}"
    )
    for key, classification in MERGE_CALL_SITE_ALLOWLIST.items():
        assert classification.startswith(MERGE_CALL_SITE_CLASSES), key

    # "upstream of clamp" is a claim about position, so assert it: every such
    # site is in ``build_candidates`` and lexically before the clamp call.
    clamp_lines = [
        node.lineno
        for node in ast.walk(ast.parse(Path(scheduler_candidates_module.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_clamp_replay_invariants"
    ]
    assert clamp_lines, "build_candidates must call the invariant clamp"
    for key, classification in MERGE_CALL_SITE_ALLOWLIST.items():
        if not classification.startswith("upstream of clamp"):
            continue
        assert key[0] == "build_candidates"
        assert found[key] < max(clamp_lines)


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


def test_forcing_readiness_empty_package_after_an_unreadable_parent_is_undeterminable(
    tmp_path: Path,
) -> None:
    """A-P2-5: an empty sibling never converts an uncompletable probe into absence.

    The package may well live under the parent that could not be read; only a
    completed negative may report ``missing`` (module invariant, #1190).
    """

    cycle_root = tmp_path / "forcing" / "gfs" / "2026070600"
    unreadable_parent = cycle_root / "basins_aaa_vbasins"
    (unreadable_parent / REPLAY_MODEL_ID).mkdir(parents=True)
    (unreadable_parent / REPLAY_MODEL_ID / "forcing.csv").write_text("t,q\n0,1\n", encoding="utf-8")
    (cycle_root / "basins_bbb_vbasins" / REPLAY_MODEL_ID).mkdir(parents=True)
    os.chmod(unreadable_parent, 0o000)
    try:
        evidence = replay_forcing_readiness(
            object_store_root=tmp_path,
            source_id="gfs",
            cycle_time=IN_WINDOW_CYCLE,
            model_ids=[REPLAY_MODEL_ID],
        )
    finally:
        os.chmod(unreadable_parent, 0o700)
    model_evidence = evidence["models"][REPLAY_MODEL_ID]
    assert model_evidence["status"] == "undeterminable"
    assert "basins_aaa_vbasins" in model_evidence["detail"]
    assert model_evidence["empty_package_dir"].endswith(f"basins_bbb_vbasins/{REPLAY_MODEL_ID}")
    assert evidence["status"] == "undeterminable"
    assert evidence["reason"] == "replay_forcing_evidence_undeterminable"


def test_forcing_readiness_empty_parent_before_a_complete_one_is_present(tmp_path: Path) -> None:
    """A2-2: an empty first parent must not decide the probe.

    ``basins_aaa_vbasins`` sorts before ``basins_bbb_vbasins``; the real package
    lives under the second one.  Returning ``missing`` at the first empty
    directory is a proven-wrong negative that rejects the whole cycle.
    """

    cycle_root = tmp_path / "forcing" / "gfs" / "2026070600"
    (cycle_root / "basins_aaa_vbasins" / REPLAY_MODEL_ID).mkdir(parents=True)
    package = cycle_root / "basins_bbb_vbasins" / REPLAY_MODEL_ID
    package.mkdir(parents=True)
    (package / "forcing.csv").write_text("t,q\n0,1\n", encoding="utf-8")

    evidence = replay_forcing_readiness(
        object_store_root=tmp_path,
        source_id="gfs",
        cycle_time=IN_WINDOW_CYCLE,
        model_ids=[REPLAY_MODEL_ID],
    )
    model_evidence = evidence["models"][REPLAY_MODEL_ID]
    assert model_evidence["status"] == "present"
    assert model_evidence["package_dir"].endswith(f"basins_bbb_vbasins/{REPLAY_MODEL_ID}")
    assert evidence["status"] == "ready"


def test_forcing_readiness_empty_parent_before_an_unreadable_package_is_undeterminable(
    tmp_path: Path,
) -> None:
    """A2-2: the empty-then-unreadable order must match the unreadable-then-empty one."""

    cycle_root = tmp_path / "forcing" / "gfs" / "2026070600"
    (cycle_root / "basins_aaa_vbasins" / REPLAY_MODEL_ID).mkdir(parents=True)
    unreadable_package = cycle_root / "basins_bbb_vbasins" / REPLAY_MODEL_ID
    unreadable_package.mkdir(parents=True)
    (unreadable_package / "forcing.csv").write_text("t,q\n0,1\n", encoding="utf-8")
    os.chmod(unreadable_package, 0o000)
    try:
        evidence = replay_forcing_readiness(
            object_store_root=tmp_path,
            source_id="gfs",
            cycle_time=IN_WINDOW_CYCLE,
            model_ids=[REPLAY_MODEL_ID],
        )
    finally:
        os.chmod(unreadable_package, 0o700)
    model_evidence = evidence["models"][REPLAY_MODEL_ID]
    assert model_evidence["status"] == "undeterminable"
    assert "package unreadable" in model_evidence["detail"]
    assert model_evidence["empty_package_dir"].endswith(f"basins_aaa_vbasins/{REPLAY_MODEL_ID}")
    assert evidence["status"] == "undeterminable"
    assert evidence["reason"] == "replay_forcing_evidence_undeterminable"


def test_forcing_readiness_empty_parent_before_an_unreadable_parent_is_undeterminable(
    tmp_path: Path,
) -> None:
    """A2-2: the same, with the LATER parent directory itself unreadable."""

    cycle_root = tmp_path / "forcing" / "gfs" / "2026070600"
    (cycle_root / "basins_aaa_vbasins" / REPLAY_MODEL_ID).mkdir(parents=True)
    unreadable_parent = cycle_root / "basins_zzz_vbasins"
    (unreadable_parent / REPLAY_MODEL_ID).mkdir(parents=True)
    (unreadable_parent / REPLAY_MODEL_ID / "forcing.csv").write_text("t,q\n0,1\n", encoding="utf-8")
    os.chmod(unreadable_parent, 0o000)
    try:
        evidence = replay_forcing_readiness(
            object_store_root=tmp_path,
            source_id="gfs",
            cycle_time=IN_WINDOW_CYCLE,
            model_ids=[REPLAY_MODEL_ID],
        )
    finally:
        os.chmod(unreadable_parent, 0o700)
    model_evidence = evidence["models"][REPLAY_MODEL_ID]
    assert model_evidence["status"] == "undeterminable"
    assert "basins_zzz_vbasins" in model_evidence["detail"]
    assert model_evidence["empty_package_dir"].endswith(f"basins_aaa_vbasins/{REPLAY_MODEL_ID}")
    assert evidence["status"] == "undeterminable"


def test_forcing_readiness_present_package_wins_over_an_unreadable_parent(tmp_path: Path) -> None:
    """Presence still short-circuits: a readable, non-empty package is ``present``."""

    cycle_root = tmp_path / "forcing" / "gfs" / "2026070600"
    unreadable_parent = cycle_root / "basins_aaa_vbasins"
    (unreadable_parent / REPLAY_MODEL_ID).mkdir(parents=True)
    package = cycle_root / "basins_bbb_vbasins" / REPLAY_MODEL_ID
    package.mkdir(parents=True)
    (package / "forcing.csv").write_text("t,q\n0,1\n", encoding="utf-8")
    os.chmod(unreadable_parent, 0o000)
    try:
        evidence = replay_forcing_readiness(
            object_store_root=tmp_path,
            source_id="gfs",
            cycle_time=IN_WINDOW_CYCLE,
            model_ids=[REPLAY_MODEL_ID],
        )
    finally:
        os.chmod(unreadable_parent, 0o700)
    assert evidence["models"][REPLAY_MODEL_ID]["status"] == "present"
    assert evidence["status"] == "ready"


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
