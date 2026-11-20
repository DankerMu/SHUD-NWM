"""Completion scoring decides without the per-event evidence copy (#3223, E2).

Seam: ``ProductionScheduler._cycle_completion_status`` through the production
binding ``ProductionScheduler._discovery_context()``, whose decider slot reads
``scheduler._candidate_state_decision`` at call time.  A spy installed there
records the decision completion scoring receives.  The setups are the existing
completion truth-table ones (``tests/test_production_scheduler.py``), with one
pipeline event added to every candidate state so the per-event copy has
something to build.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from services.orchestrator import scheduler as scheduler_module
from services.orchestrator import scheduler_generation as scheduler_generation_module
from services.orchestrator import scheduler_lineage as _scheduler_lineage
from services.orchestrator.scheduler import CycleDiscovery, ProductionScheduler
from tests.test_production_scheduler import (
    _ABSENCE_TOLERANCE_SELECTED_STATE,
    _ABSENT_INIT_STATE_HYDRO_RUN,
    _CONFLICTING_INIT_STATE_HYDRO_RUN,
    _MATCHING_INIT_STATE_HYDRO_RUN,
    _NOT_READY_STRICT_EVIDENCE,
    FakeCandidateStateRepository,
    FakeRegistry,
    _absence_tolerance_scheduler,
    _AlwaysReadyCanonicalReadinessProvider,
    _config,
    _dt,
    _model,
)

# A benign, already-finished pipeline event: it must not move any verdict below
# (each case asserts its truth-table verdict), it only gives the per-event
# evidence copy something to copy.
_PUBLISH_SUCCEEDED_EVENT = {
    "event_id": 7,
    "event_type": "stage_succeeded",
    "stage": "publish",
    "status": "succeeded",
    "created_at": "2026-07-20T05:00:00Z",
    "details": {"note": "publish finished"},
}


class _EventCarryingStateRepository(FakeCandidateStateRepository):
    """The truth-table candidate state plus one pipeline event."""

    def __init__(self, state: dict[str, object]) -> None:
        super().__init__({**state, "pipeline_events": [dict(_PUBLISH_SUCCEEDED_EVENT)]})


@dataclass
class _DecisionSpy:
    """Records every decision the completion slot hands back."""

    full_evidence: bool
    delegate: Callable[..., object]
    calls: list[tuple[dict[str, object], object, object, object]] = field(default_factory=list)

    def __call__(self, candidate: object, state: object, **kwargs: object) -> object:
        # ``full_evidence`` is the oracle arm: it drops the opt-out so the same
        # ladder builds its full evidence for the same inputs.
        forwarded = {} if self.full_evidence else dict(kwargs)
        decision = self.delegate(candidate, state, **forwarded)
        self.calls.append((dict(kwargs), candidate, state, decision))
        return decision


def _score(
    monkeypatch: pytest.MonkeyPatch,
    build: Callable[[], tuple[ProductionScheduler, CycleDiscovery, list[object]]],
    *,
    full_evidence: bool,
) -> tuple[str, _DecisionSpy]:
    spy = _DecisionSpy(full_evidence=full_evidence, delegate=scheduler_module._candidate_state_decision)
    scheduler, discovery, models = build()
    with monkeypatch.context() as patch:
        patch.setattr(scheduler_module, "_candidate_state_decision", spy)
        verdict = scheduler._cycle_completion_status(discovery, models, horizon={})
    return verdict, spy


def _assert_scored_without_event_copy(spy: _DecisionSpy) -> None:
    assert spy.calls, "completion scoring never consulted the decider: the check would be vacuous"
    for kwargs, candidate, state, decision in spy.calls:
        assert kwargs == {"event_evidence": False}
        assert decision is not None
        assert "pipeline_events" not in decision.evidence
        # positive control: the default on the very same inputs builds the copy
        full = scheduler_module._candidate_state_decision(candidate, state)
        assert full is not None
        assert len(full.evidence["pipeline_events"]) == 1
        assert (decision.action, decision.reason) == (full.action, full.reason)
        assert decision.evidence == {k: v for k, v in full.evidence.items() if k != "pipeline_events"}


def _site_444(monkeypatch: pytest.MonkeyPatch, scheduler: ProductionScheduler) -> ProductionScheduler:
    """No strict / successor verdict for any model: scoring reaches the second decider call."""

    monkeypatch.setattr(scheduler, "_strict_warm_start_for_candidate", lambda _candidate, _cycle: None)
    monkeypatch.setattr(scheduler, "_successor_warm_start_state_for_candidate", lambda _candidate, _cycle: None)
    return scheduler


def _truth_table_builder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    second_call_site: bool = False,
    **kwargs: object,
) -> Callable[[], tuple[ProductionScheduler, CycleDiscovery, list[object]]]:
    def build() -> tuple[ProductionScheduler, CycleDiscovery, list[object]]:
        # ``_absence_tolerance_scheduler`` (tests/test_production_scheduler.py)
        # takes no mode kwarg: the env selects DB-free explicitly.
        monkeypatch.setenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", "true")
        scheduler, discovery, models = _absence_tolerance_scheduler(
            monkeypatch,
            tmp_path,
            repository_factory=_EventCarryingStateRepository,
            **kwargs,
        )
        if second_call_site:
            _site_444(monkeypatch, scheduler)
        return scheduler, discovery, models

    return build


_PACKAGED_IC_CHECKSUM = "a" * 64


def _packaged_ic_kwargs(strict_checksum: str) -> dict[str, object]:
    return {
        "hydro_run": {
            **_ABSENT_INIT_STATE_HYDRO_RUN,
            "quality": "packaged_calibrated_state",
            "init_state_id": None,
        },
        "successor_state": None,
        "strict_evidence": {
            "status": "ready",
            "ready": True,
            "mode": scheduler_generation_module.PACKAGED_IC_BOOTSTRAP_MODE,
            "packaged_ic_checksum": strict_checksum,
        },
        "run_manifest_initial_state": {
            "quality": "packaged_calibrated_state",
            "state_id": None,
            "packaged_ic_checksum": _PACKAGED_IC_CHECKSUM,
        },
    }


@pytest.mark.parametrize(
    ("case_kwargs", "expected_verdict"),
    [
        pytest.param(
            {"hydro_run": _MATCHING_INIT_STATE_HYDRO_RUN, "successor_state": None},
            "complete",
            id="terminal_success_matching_init_state",
        ),
        pytest.param(
            {"hydro_run": _CONFLICTING_INIT_STATE_HYDRO_RUN, "successor_state": {"ready": True}},
            "gap",
            id="init_state_conflict",
        ),
        pytest.param(
            {"hydro_run": _ABSENT_INIT_STATE_HYDRO_RUN, "successor_state": {"ready": True}},
            "complete",
            id="init_state_absent_successor_ready",
        ),
        pytest.param(
            {"hydro_run": _ABSENT_INIT_STATE_HYDRO_RUN, "successor_state": None},
            "gap",
            id="init_state_absent_no_successor_verdict",
        ),
        pytest.param(
            {
                "hydro_run": _ABSENT_INIT_STATE_HYDRO_RUN,
                "successor_state": {"ready": True},
                "strict_evidence": _NOT_READY_STRICT_EVIDENCE,
            },
            "complete",
            id="init_state_unverifiable_successor_ready",
        ),
        pytest.param(
            {
                "hydro_run": _ABSENT_INIT_STATE_HYDRO_RUN,
                "successor_state": None,
                "strict_evidence": _NOT_READY_STRICT_EVIDENCE,
            },
            "gap",
            id="init_state_unverifiable_no_successor_verdict",
        ),
        pytest.param(_packaged_ic_kwargs(_PACKAGED_IC_CHECKSUM), "complete", id="packaged_ic_checksum_match"),
        pytest.param(_packaged_ic_kwargs("b" * 64), "gap", id="packaged_ic_checksum_mismatch"),
        pytest.param(
            {"hydro_run": _MATCHING_INIT_STATE_HYDRO_RUN, "successor_state": None, "second_call_site": True},
            "complete",
            id="terminal_success_without_strict_or_successor_verdict",
        ),
        pytest.param(
            {"hydro_run": _MATCHING_INIT_STATE_HYDRO_RUN, "successor_state": None, "hydro_status": "failed"},
            "gap",
            id="non_terminal_candidate",
        ),
    ],
)
def test_completion_scoring_receives_no_event_copy_and_keeps_the_full_evidence_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case_kwargs: dict[str, object],
    expected_verdict: str,
) -> None:
    build = _truth_table_builder(monkeypatch, tmp_path, **case_kwargs)

    verdict, spy = _score(monkeypatch, build, full_evidence=False)
    full_verdict, full_spy = _score(monkeypatch, build, full_evidence=True)

    _assert_scored_without_event_copy(spy)
    assert full_spy.calls and all("pipeline_events" in call[3].evidence for call in full_spy.calls)
    assert verdict == full_verdict == expected_verdict


# --- a recalibrated model scoped out of a pre-cutover cycle by its lineage (#1735) ---


class _PerModelStateRepository(_EventCarryingStateRepository):
    """model_a's own run failed; every other model's own run is published."""

    def candidate_state(self, **kwargs: object) -> dict[str, object]:
        state = super().candidate_state(**kwargs)
        run_id = kwargs["run_id"]
        if kwargs["model_id"] == "model_a":
            return {**state, "hydro_status": "failed", "hydro_run": {"run_id": run_id, "status": "failed"}}
        hydro_run = {
            **_MATCHING_INIT_STATE_HYDRO_RUN,
            "run_id": run_id,
            "output_uri": f"s3://nhms/runs/{run_id}/output/",
        }
        return {**state, "hydro_run": hydro_run}


def _lineage_builder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    scoped_out: bool,
) -> Callable[[], tuple[ProductionScheduler, CycleDiscovery, list[object]]]:
    def build() -> tuple[ProductionScheduler, CycleDiscovery, list[object]]:
        models = [_model("model_a", "basin_a"), _model("model_b", "basin_b")]
        scheduler = ProductionScheduler(
            _config(tmp_path, db_free=True, now=_dt("2026-07-20T12:00:00Z")),
            registry=FakeRegistry(models),
            adapters={},
            active_repository=_PerModelStateRepository({"hydro_status": "published"}),
            canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        )
        monkeypatch.setattr(
            scheduler,
            "_strict_warm_start_for_candidate",
            lambda _candidate, _cycle: {
                "status": "ready",
                "ready": True,
                "candidate_state": dict(_ABSENCE_TOLERANCE_SELECTED_STATE),
            },
        )
        monkeypatch.setattr(
            scheduler, "_successor_warm_start_state_for_candidate", lambda _candidate, _cycle: {"ready": True}
        )
        # model_a was recalibrated: its cutover ``t*`` is after this cycle.
        scheduler._lineage_cutover_cache[("model_a", "gfs")] = (
            _scheduler_lineage.LineageCutover(
                model_id="model_a",
                source_id="gfs",
                predecessor_model_id="model_a_legacy",
                cutover_time=_dt("2026-07-20T06:00:00Z"),
            )
            if scoped_out
            else None
        )
        scheduler._lineage_cutover_cache[("model_b", "gfs")] = None
        discovery = CycleDiscovery(
            cycle_id="gfs_2026072000",
            source_id="gfs",
            cycle_time=_dt("2026-07-20T00:00:00Z"),
            cycle_hour=0,
            available=True,
            status="discovered",
        )
        return scheduler, discovery, [scheduler_module._coerce_registered_model(model) for model in models]

    return build


@pytest.mark.parametrize(("scoped_out", "expected_verdict"), [(True, "complete"), (False, "gap")])
def test_recalibrated_model_scoped_out_by_lineage_keeps_its_verdict_without_the_event_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scoped_out: bool,
    expected_verdict: str,
) -> None:
    build = _lineage_builder(monkeypatch, tmp_path, scoped_out=scoped_out)

    verdict, spy = _score(monkeypatch, build, full_evidence=False)
    full_verdict, _full_spy = _score(monkeypatch, build, full_evidence=True)

    _assert_scored_without_event_copy(spy)
    assert verdict == full_verdict == expected_verdict
    decided = [call[1].model_id for call in spy.calls]
    # scoped out: model_a is never scored; in scope its failed row is the gap
    assert decided == (["model_b"] if scoped_out else ["model_a"])


def test_submitting_path_keeps_the_event_copy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The submit-path context binds the same decider and keeps the default."""

    # ``_absence_tolerance_scheduler`` takes no mode kwarg: the env selects DB-free explicitly.
    monkeypatch.setenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", "true")
    scheduler, discovery, models = _absence_tolerance_scheduler(
        monkeypatch,
        tmp_path,
        hydro_run=dict(_MATCHING_INIT_STATE_HYDRO_RUN),
        successor_state=None,
        repository_factory=_EventCarryingStateRepository,
    )
    context = scheduler._candidate_construction_context()
    candidate = scheduler_module._candidate_for(discovery=discovery, model=models[0], horizon={})
    state = scheduler.active_repository.candidate_state(
        source_id=candidate.source_id,
        cycle_time=candidate.cycle_time_utc,
        model_id=candidate.model_id,
        run_id=candidate.run_id,
        forcing_version_id=candidate.forcing_version_id,
        candidate_id=candidate.candidate_id,
    )
    decision = context.candidate_state_decider(candidate, state)

    assert decision is not None and decision.reason == "terminal_hydro_success"
    assert decision.evidence["pipeline_events"][0]["event_type"] == "stage_succeeded"
