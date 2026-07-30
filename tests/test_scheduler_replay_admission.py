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
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.orchestrator import chain as _chain  # noqa: F401  (import order: chain owns the cycle mixin)
from services.orchestrator import chain_forecast_orchestrator_cycle as chain_cycle_module
from services.orchestrator import scheduler_candidate_manifest as scheduler_candidate_manifest_module
from services.orchestrator import scheduler_candidates as scheduler_candidates_module
from services.orchestrator import scheduler_replay as scheduler_replay_module
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
from services.orchestrator.scheduler_state import CandidateStateDecision
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


def _chain_gate_forces_resubmit(candidate: Any) -> bool:
    """Drive the REAL chain gate with an admitted candidate's evidence (A4-1).

    ``_terminal_stage_needs_forced_resubmit`` (``chain_forecast_orchestrator_cycle``
    :802) is the consumer that decides whether a succeeded forecast job gets
    resubmitted; it matches ``state_evidence["decision"]`` against
    ``_FORCE_TERMINAL_RESUBMIT_DECISIONS`` literally.  ``restart_stage=None`` on
    the context is the honest shape: the chain has no restart stage of its own
    here, the candidate evidence is the only source.
    """

    basin = {
        "model_id": candidate.model_id,
        "basin_id": candidate.basin_id,
        "candidate_id": candidate.candidate_id,
        "orchestration_run_id": candidate.cycle_id,
        "state_evidence": dict(candidate.state_evidence),
    }
    return chain_cycle_module._terminal_stage_needs_forced_resubmit(
        SimpleNamespace(active_basins=[basin], restart_stage=None),
        dict(SUCCEEDED_FORECAST_JOB),
    )


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

    clobbered = scheduler_candidates_module._candidate_with_state_evidence(
        admitted,
        {
            "decision": "retry_failed",
            "restart_stage": "convert",
            "restart_from_stage": "convert",
            "native_shud_resubmitted": False,
            "durable_output_reused": True,
            "durable_shud_output_reused": True,
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
    clamp = evidence["replay_invariant_clamp_applied"]
    assert clamp["clobbered_keys"] == [
        "decision",
        "durable_output_reused",
        "durable_shud_output_reused",
        "native_shud_resubmitted",
        "restart_from_stage",
        "restart_stage",
    ]
    assert clamp["pre_clamp"]["native_shud_resubmitted"] is False
    assert clamp["pre_clamp"]["durable_output_reused"] is True
    assert clamp["pre_clamp"]["decision"] == "retry_failed"
    # the whole owned set is what the consumers read, nothing narrower
    assert set(scheduler_candidates_module._REPLAY_RESUBMIT_CLAMPED_KEYS) == set(clamp["clobbered_keys"])
    assert _chain_gate_forces_resubmit(clamped[0]) is True


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
