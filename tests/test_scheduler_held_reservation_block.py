"""A held reservation blocks the candidate-state retry decision (#2666).

Production shape (node-22, 2026-09-27, IFS 0925 12Z): the forecast array submit of
a convert cohort timed out at the gateway while Slurm had accepted it (57553). The
journal row stayed ``reserved`` / ``submit_result_ambiguous`` / ``slurm_job_id``
null; forcing had succeeded and every member's ``hydro_run`` was ``created``. In
the next pass restart reconcile bound only the sibling source, and the planner
judged every member ``retry_after_completed_stage``: it nulled the ``created``
placeholder, ``candidate_state_scoped_retry_detector`` exempted the retry from the
journal's ``has_active_pipeline`` gate, and a second forecast array (57637) ran
the same runs in the same run directories.

The requirement: any row of the candidate's provider-filtered state that is
``reserved`` with no real Slurm binding makes the decision ``skip`` /
``active_duplicate_pipeline`` (``active_status: reserved``), before every
supersession / resume / terminal / failure / manual-retry branch, whatever the
row's stage, its ``cohort_membership`` (``member`` / ``unwitnessed`` /
``incomplete``) and its reconcile reason class. ``non_member`` sibling rows,
bound rows and released rows keep their pre-change decisions.

Seams: the real ``orchestrate_cycle`` stage loop writes the journal through a real
``FileOrchestrationJournalRepository`` (only the Slurm runtime is a double), then
the real ``candidate_state`` read feeds the scheduler's ``_candidate_state_decision``
and the planner's ``ProductionScheduler._build_candidates`` / ``_execute_candidates``.
Candidate and cohort run ids come from the scheduler's own factories.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import scheduler as scheduler_module
from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    AcceptedSubmitTransition,
)
from services.orchestrator.chain_stages import COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE
from services.orchestrator.file_orchestration_journal import (
    FileJournalRetryService,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.scheduler_state_rows import _job_state_evidence
from services.orchestrator.scheduler_state_types import FAILED_PIPELINE_STATUSES
from tests.test_orchestration_chain import FakeCycleSlurmClient, _orchestrator

# ``tests.test_production_scheduler`` helpers are imported function-locally, as the
# other scheduler suites do: the selector pins that suite's module-scope importers.

_CYCLE = "2026050100"
_CYCLE_ID = f"gfs_{_CYCLE}"
_CYCLE_TIME = datetime(2026, 5, 1, tzinfo=UTC)
_NEXT_PASS = datetime(2026, 5, 1, 6, tzinfo=UTC)
_HELD = ("skip", "active_duplicate_pipeline")


def _projection_keys() -> frozenset[str]:
    """Keys ``_job_state_evidence`` may emit: the bounded projection of one held row."""

    names: set[str] = set()
    for const in _job_state_evidence.__code__.co_consts:
        names.update(item for item in (const if isinstance(const, tuple) else (const,)) if isinstance(item, str))
    return frozenset(_job_state_evidence({name: "x" for name in names}))


_PROJECTION_KEYS = _projection_keys()


class _PassDied(BaseException):
    """The scheduler process died after reserving a stage row, before binding it."""


class _Runtime(FakeCycleSlurmClient):
    """Slurm runtime double; the submit of ``hold_stage`` never returns an answer.

    ``forecast`` times out at the gateway boundary (the production 57553 shape:
    classified ambiguous, row held ``reserved`` / ``submit_result_ambiguous``).
    Any other stage dies with the pass after the row was reserved (``reserved``,
    unbound): the downstream masters are not accepted-submit rows, so a timeout
    there is recorded as a submission failure instead of a held reservation.
    """

    def __init__(self, hold_stage: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.hold_stage = hold_stage

    def submit_job_array(self, job_type: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("stage_name") == self.hold_stage:
            from services.orchestrator.chain_types import OrchestratorError

            if self.hold_stage == "forecast":
                raise OrchestratorError("SLURM_TIMEOUT", "gateway read timed out")
            raise _PassDied(self.hold_stage)
        return super().submit_job_array(job_type, **kwargs)


@pytest.fixture(autouse=True)
def _planner_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The compat regime: no strict warm-start resolution gates the planner, so
    # the candidate-state decision is the gate under test.
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")
    monkeypatch.delenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", raising=False)


def _raw_models(indexes: Sequence[int]) -> list[dict[str, Any]]:
    from tests.test_production_scheduler import _model

    return [_model(f"model_{index}", f"basin_{index}") for index in indexes]


def _discovery() -> Any:
    return scheduler_module.CycleDiscovery(
        cycle_id=_CYCLE_ID,
        source_id="gfs",
        cycle_time=_CYCLE_TIME,
        cycle_hour=0,
        available=True,
        status="discovered",
    )


def _candidates(raw_models: Sequence[dict[str, Any]]) -> list[Any]:
    return [
        scheduler_module._candidate_for(
            discovery=_discovery(),
            model=scheduler_module._coerce_registered_model(model),
            horizon={},
        )
        for model in raw_models
    ]


def _cohort_run_id(candidates: Sequence[Any], restart_stage: str = "convert") -> str:
    """The scheduler's own execution-cohort run id (never hand-written)."""

    key = scheduler_module._candidate_restart_cohort_key(restart_stage)
    return scheduler_module._candidate_execution_cohort_run_id("gfs", _CYCLE_TIME, key, list(candidates))


def _cohort_basins(candidates: Sequence[Any]) -> list[dict[str, Any]]:
    run_id = _cohort_run_id(candidates)
    return [
        {
            "model_id": candidate.model_id,
            "basin_id": candidate.basin_id,
            "basin_version_id": candidate.basin_version_id,
            "river_network_version_id": candidate.river_network_version_id,
            "run_id": candidate.run_id,
            "candidate_id": candidate.candidate_id,
            "orchestration_run_id": run_id,
            "model_package_uri": candidate.model_package_uri,
            "model_package_checksum": f"sha256:{candidate.model_id}",
        }
        for candidate in candidates
    ]


def _first_pass(
    tmp_path: Path,
    repository: FileOrchestrationJournalRepository,
    candidates: Sequence[Any],
    *,
    hold_stage: str | None,
    terminal_stage: str | None = None,
) -> Any:
    """Run one real cycle pass for the cohort; the ``hold_stage`` submit never answers."""

    client = _Runtime(hold_stage)
    orchestrator = _orchestrator(tmp_path / "first-pass", repository, client, terminal_stage=terminal_stage)
    try:
        return orchestrator.orchestrate_cycle("gfs", _CYCLE, _cohort_basins(candidates))
    except _PassDied:
        return None


def _forcing_witness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, candidates: Sequence[Any]) -> None:
    """The producer's forcing sidecar per member, as on node-22 (forcing succeeded)."""

    from tests.test_production_scheduler import _seed_producer_forcing_sidecar

    root = tmp_path / "forcing-object-store"
    root.mkdir()
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(root))
    for candidate in candidates:
        _seed_producer_forcing_sidecar(root, candidate=candidate)


def _state(repository: FileOrchestrationJournalRepository, candidate: Any, **kwargs: Any) -> dict[str, Any]:
    state = FileOrchestrationJournalRepository(repository.root).candidate_state(
        source_id="gfs",
        cycle_time=_CYCLE_TIME,
        model_id=candidate.model_id,
        run_id=candidate.run_id,
        forcing_version_id=candidate.forcing_version_id,
        candidate_id=candidate.candidate_id,
        **kwargs,
    )
    assert state is not None
    return state


def _decision(repository: FileOrchestrationJournalRepository, candidate: Any) -> Any:
    decision = scheduler_module._candidate_state_decision(candidate, _state(repository, candidate))
    assert decision is not None
    return decision


def _scheduler(
    tmp_path: Path,
    repository: FileOrchestrationJournalRepository,
    raw_models: Sequence[dict[str, Any]],
    *,
    orchestrator: Any | None = None,
    **config: Any,
) -> Any:
    from tests.test_production_scheduler import FakeRegistry, ProductionScheduler, _config

    return ProductionScheduler(
        _config(tmp_path / "planner", now=_NEXT_PASS, **config),
        registry=FakeRegistry(list(raw_models)),
        adapters={},
        active_repository=FileOrchestrationJournalRepository(repository.root),
        orchestrator_factory=(
            (lambda _source_id: orchestrator)
            if orchestrator is not None
            else (lambda _source_id: pytest.fail("the planner pass must not build an orchestrator"))
        ),
    )


def _plan(scheduler: Any, raw_models: Sequence[dict[str, Any]]) -> tuple[Any, Any, Any]:
    candidates, blocked, skipped, _duplicates, _sync = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(model) for model in raw_models],
        cycles=[scheduler_module.SchedulerSourceCycle(discovery=_discovery(), horizon={})],
    )
    return candidates, blocked, skipped


def _rows(repository: FileOrchestrationJournalRepository, stage: str | None = None) -> list[dict[str, Any]]:
    return [
        row
        for row in FileOrchestrationJournalRepository(repository.root).query_pipeline_jobs_by_cycle(_CYCLE_ID)
        if stage is None or row.get("stage") == stage
    ]


def _master(repository: FileOrchestrationJournalRepository, stage: str) -> dict[str, Any]:
    (row,) = [row for row in _rows(repository, stage) if row.get("model_id") in (None, "")]
    return row


def _assert_held_decision(decision: Any, *, stage: str) -> None:
    assert (decision.action, decision.reason) == _HELD
    evidence = decision.evidence
    assert evidence["decision"] == "skip_active"
    assert evidence["active_status"] == "reserved"
    assert evidence["replacement_submitted"] is False
    held = evidence["held_reservations"]
    assert [(row["stage"], row["status"], row.get("slurm_job_id")) for row in held] == [(stage, "reserved", None)]
    assert all(set(row) <= _PROJECTION_KEYS for row in held)


def _production_forecast_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    members: int = 2,
    *,
    terminal_stage: str | None = None,
) -> tuple[FileOrchestrationJournalRepository, list[dict[str, Any]], list[Any]]:
    raw_models = _raw_models(range(members))
    candidates = _candidates(raw_models)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    result = _first_pass(tmp_path, repository, candidates, hold_stage="forecast", terminal_stage=terminal_stage)
    assert [(stage.stage, stage.status) for stage in result.stages] == [
        ("convert", "succeeded"),
        ("forcing", "succeeded"),
        ("forecast", "submit_result_ambiguous"),
    ]
    _forcing_witness(monkeypatch, tmp_path, candidates)
    return repository, raw_models, candidates


def _assert_production_rows(repository: FileOrchestrationJournalRepository, candidates: Sequence[Any]) -> None:
    held = _master(repository, "forecast")
    assert (held["status"], held["submit_outcome"], held["slurm_job_id"]) == (
        "reserved",
        "submit_result_ambiguous",
        None,
    )
    assert sorted(member["model_id"] for member in held["cohort_members"]) == sorted(
        candidate.model_id for candidate in candidates
    )
    assert _master(repository, "forcing")["status"] == "succeeded"
    assert not [row for row in _rows(repository) if row.get("status") in FAILED_PIPELINE_STATUSES]
    for candidate in candidates:
        assert repository._hydro_run_for(candidate.run_id)["status"] == "created"


# --- 1. the production double submission ------------------------------------------------


def test_held_ambiguous_forecast_submit_blocks_every_member_through_the_planner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository, raw_models, candidates = _production_forecast_shape(monkeypatch, tmp_path)
    _assert_production_rows(repository, candidates)

    for candidate in candidates:
        state = _state(repository, candidate)
        # The held master is visible only through the provider-filtered state:
        # the journal annotated it ``member`` (forecast is outside the cohort
        # attribution stage gate, so that gate cannot be what finds it).
        assert [
            (row["stage"], row["status"], row.get("slurm_job_id"), row.get("cohort_membership"))
            for row in state["pipeline_jobs"]
            if row.get("status") == "reserved"
        ] == [("forecast", "reserved", None, "member")]
        decision = _decision(repository, candidate)
        assert decision.evidence.get("decision") != "retry_after_completed_stage"
        _assert_held_decision(decision, stage="forecast")

    selected, blocked, skipped = _plan(_scheduler(tmp_path, repository, raw_models), raw_models)

    assert selected == []
    assert blocked == []
    assert sorted(row["candidate_id"] for row in skipped) == sorted(c.candidate_id for c in candidates)
    for row in skipped:
        assert row["reason"] == "active_duplicate_pipeline"
        assert row["state_evidence"]["active_status"] == "reserved"
        assert row["state_evidence"]["held_reservations"][0]["job_id"] == _master(repository, "forecast")["job_id"]


def test_gateway_timeout_then_planner_pass_submits_no_second_forecast(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Chain-level: pass 1 times out on the forecast submit; pass 2 plans and dispatches."""

    repository, raw_models, _candidates_ = _production_forecast_shape(monkeypatch, tmp_path)
    forecast_masters_before = [row["job_id"] for row in _rows(repository, "forecast")]
    runtime = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path / "second-pass", FileOrchestrationJournalRepository(repository.root), runtime)
    scheduler = _scheduler(tmp_path, repository, raw_models, orchestrator=orchestrator, dry_run=False)

    selected, _blocked, _skipped = _plan(scheduler, raw_models)
    scheduler._execute_candidates(selected)

    assert runtime.submissions == []
    assert [row["job_id"] for row in _rows(repository, "forecast")] == forecast_masters_before
    assert _master(repository, "forecast")["status"] == "reserved"


# --- 2. other held stages / terminal branches --------------------------------------------


def test_held_state_save_qc_master_blocks_the_completed_stage_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gfs 12Z first-half shape: forecast succeeded, the state_save_qc master is held."""

    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE)
    raw_models = _raw_models(range(2))
    candidates = _candidates(raw_models)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    _first_pass(
        tmp_path,
        repository,
        candidates,
        hold_stage="state_save_qc",
        terminal_stage=COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE,
    )
    assert _master(repository, "forecast")["status"] == "succeeded"
    held = _master(repository, "state_save_qc")
    assert (held["status"], held["slurm_job_id"]) == ("reserved", None)

    for candidate in candidates:
        decision = _decision(repository, candidate)
        assert decision.reason != "resume_after_completed_stage"
        _assert_held_decision(decision, stage="state_save_qc")


def test_terminal_hydro_success_with_a_held_row_is_in_flight_not_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Terminal hydro truth + a held parse master: skip-held, cycle ``gap``, frontier in-flight."""

    raw_models = _raw_models(range(2))
    candidates = _candidates(raw_models)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    _first_pass(tmp_path, repository, candidates, hold_stage="parse")
    assert {repository._hydro_run_for(c.run_id)["status"] for c in candidates} == {"succeeded"}
    assert (_master(repository, "parse")["status"], _master(repository, "parse")["slurm_job_id"]) == (
        "reserved",
        None,
    )

    for candidate in candidates:
        decision = _decision(repository, candidate)
        assert decision.reason != "terminal_hydro_success"
        _assert_held_decision(decision, stage="parse")

    scheduler = _scheduler(tmp_path, repository, raw_models, lookback_hours=0, cycle_lag_hours=0)
    monkeypatch.setattr(scheduler, "_strict_warm_start_for_candidate", lambda _candidate, _cycle: None)
    monkeypatch.setattr(
        scheduler,
        "_successor_warm_start_state_for_candidate",
        lambda _candidate, _cycle: {"ready": True, "status": "ready"},
    )
    models = [scheduler_module._coerce_registered_model(model) for model in raw_models]
    assert scheduler._cycle_completion_status(_discovery(), models, horizon={}) == "gap"

    _selected, _blocked, skipped = _plan(scheduler, raw_models)
    assert {row["reason"] for row in skipped} == {"active_duplicate_pipeline"}
    from services.orchestrator import scheduler_runtime

    bound, source = scheduler_runtime._retention_active_lower_bound(scheduler, _NEXT_PASS, skipped_candidates=skipped)
    assert (bound, source) == (_CYCLE_TIME, "skipped_in_flight")


def _pre_b4_downstream_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write downstream cohort rows as pre-#2603 code did: no recorded members."""

    from services.orchestrator import chain_forecast_orchestrator_cycle

    monkeypatch.setattr(chain_forecast_orchestrator_cycle, "_MEMBER_RECORDING_DOWNSTREAM_STAGES", frozenset())


@pytest.mark.parametrize("membership", ["unwitnessed", "incomplete"])
def test_unwitnessed_or_incomplete_held_row_still_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, membership: str
) -> None:
    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE)
    if membership == "unwitnessed":
        _pre_b4_downstream_rows(monkeypatch)
    raw_models = _raw_models(range(3))
    candidates = _candidates(raw_models)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    _first_pass(
        tmp_path,
        repository,
        candidates,
        hold_stage="state_save_qc",
        terminal_stage=COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE,
    )
    held = _master(repository, "state_save_qc")
    if membership == "incomplete":
        # A truncated membership record (one member lost its model id): the
        # journal cannot prove who the row ran for (#2603 B2b).
        members = [dict(member) for member in held["cohort_members"]]
        members[-1]["model_id"] = ""
        repository.upsert_pipeline_job({**held, "cohort_members": members})

    for candidate in candidates:
        (row,) = [row for row in _state(repository, candidate)["pipeline_jobs"] if row.get("status") == "reserved"]
        assert row["cohort_membership"] == membership
        _assert_held_decision(_decision(repository, candidate), stage="state_save_qc")


# --- 3. fail closed whatever reconcile said, and against every override -----------------


def _record_reconcile_outcome(repository: FileOrchestrationJournalRepository, outcome: str) -> None:
    """Write the durable tuple restart reconcile leaves on a held master for ``outcome``.

    ``query_unavailable`` writes nothing (the row stays as the timeout left it);
    ``comment_accounting_unproven`` and ``ambiguous_fallback_match`` both write the
    held ``accounting_unavailable`` / ``comment_accounting_unproven`` tuple
    (``reconcile._record_file_reconciliation``), and ``multiple_matches_blocked``
    the exact-comment ambiguity decision.
    """

    if outcome == "query_unavailable":
        return
    held = _master(repository, "forecast")
    if outcome == "multiple_matches_blocked":
        transition = AcceptedSubmitTransition.accounting(
            "multiple_matches_blocked", submit_outcome="submit_result_ambiguous"
        )
    else:
        transition = AcceptedSubmitTransition.accounting(
            "accounting_unavailable",
            submit_outcome="submit_result_ambiguous",
            reconciliation_reason_class="comment_accounting_unproven",
        )
    result = repository.transition_pipeline_job_submit_evidence(
        held["job_id"],
        transition,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=int(held["submission_attempt"]),
        expected_statuses=("reserved",),
        require_unbound=True,
    )
    assert result.wrote


@pytest.mark.parametrize(
    "outcome",
    ["query_unavailable", "comment_accounting_unproven", "ambiguous_fallback_match", "multiple_matches_blocked"],
)
def test_reconcile_reason_class_does_not_release_the_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str
) -> None:
    repository, _raw_models_, candidates = _production_forecast_shape(monkeypatch, tmp_path)
    _record_reconcile_outcome(repository, outcome)
    held = _master(repository, "forecast")
    assert (held["status"], held["slurm_job_id"]) == ("reserved", None)

    for candidate in candidates:
        _assert_held_decision(_decision(repository, candidate), stage="forecast")


def test_held_row_plus_a_failure_signal_is_explicitly_held(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The 0925 00Z shape: a failed state_save_qc attempt sits in the cycle beside the held
    forecast. Before #2666 the skip was incidental (the failure suppressed the resume and the
    ``created`` placeholder answered); now it names the held reservation."""

    repository, _raw_models_, candidates = _production_forecast_shape(monkeypatch, tmp_path)
    cohort_run_id = _cohort_run_id(candidates)
    repository.upsert_pipeline_job(
        {
            "job_id": f"job_{cohort_run_id}_state_save_qc_earlier",
            "run_id": cohort_run_id,
            "cycle_id": _CYCLE_ID,
            "job_type": "save_state_snapshot_array",
            "stage": "state_save_qc",
            "model_id": None,
            "status": "submission_failed",
            "error_code": "STATE_SAVE_SUBMIT_AMBIGUOUS",
            "retry_count": 0,
            "cohort_members": _master(repository, "forecast")["cohort_members"],
            "created_at": "2026-05-01T00:00:00Z",
        }
    )

    for candidate in candidates:
        _assert_held_decision(_decision(repository, candidate), stage="forecast")


def test_manual_retry_marker_does_not_override_a_held_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Operator exit misuse: a forecast attempt failed, the operator marked the cohort for
    manual retry, the re-dispatch timed out at the gateway (held), and the operator marks the
    run again. The marker must not turn the held attempt into another submission."""

    raw_models = _raw_models(range(2))
    candidates = _candidates(raw_models)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    failing = FakeCycleSlurmClient(fail_stage="forecast", array_results_by_stage={"forecast": ["failed", "failed"]})
    _orchestrator(tmp_path / "failed-pass", repository, failing).orchestrate_cycle(
        "gfs", _CYCLE, _cohort_basins(candidates)
    )
    _forcing_witness(monkeypatch, tmp_path, candidates)
    retry_service = FileJournalRetryService(repository)
    convert_cohort = _cohort_run_id(candidates)
    retry_service.record_manual_repair(convert_cohort, trusted_internal=True)
    assert {_decision(repository, c).reason for c in candidates} == {"manual_retry_requested"}

    forecast_cohort = _cohort_run_id(candidates, "forecast")
    redispatch = _cohort_basins(candidates)
    for basin in redispatch:
        basin.update(
            {
                "orchestration_run_id": forecast_cohort,
                "restart_stage": "forecast",
                "state_evidence": {"restart_stage": "forecast"},
            }
        )
    redispatcher = _orchestrator(tmp_path / "redispatch", repository, _Runtime("forecast"))
    redispatcher.orchestrate_cycle("gfs", _CYCLE, redispatch)
    marker = retry_service.record_manual_repair(convert_cohort, trusted_internal=True)
    assert (marker.status, marker.manual_retry_marker) == ("manual_repair_requested", True)
    held = next(row for row in _rows(repository, "forecast") if row["run_id"] == forecast_cohort)
    assert (held["status"], held["submit_outcome"], held["slurm_job_id"]) == (
        "reserved",
        "submit_result_ambiguous",
        None,
    )

    for candidate in candidates:
        decision = _decision(repository, candidate)
        _assert_held_decision(decision, stage="forecast")
        assert decision.evidence["held_reservations"][0]["job_id"] == held["job_id"]


def test_live_manual_retry_marker_does_not_override_a_held_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A marker the decision DOES read as requested still loses to a held reservation.

    The journal refuses a manual repair on the production forecast shape (no failure to
    repair) and the ``created`` hydro placeholder is an active blocker no marker overrides,
    so the live marker is laid onto the real held state_save_qc state as the top-level
    ``manual_retry`` field ``_manual_retry_markers`` reads, stamped after every blocker.
    """

    from services.orchestrator.scheduler_state_manual_retry import _manual_retry_requested

    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE)
    candidates = _candidates(_raw_models(range(2)))
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    _first_pass(
        tmp_path,
        repository,
        candidates,
        hold_stage="state_save_qc",
        terminal_stage=COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE,
    )

    for candidate in candidates:
        marked = {
            **_state(repository, candidate),
            "manual_retry": {"requested": True, "requested_at": "2030-01-01T00:00:00Z"},
        }
        assert _manual_retry_requested(marked) is True
        decision = scheduler_module._candidate_state_decision(candidate, marked)
        assert decision is not None
        assert decision.reason != "manual_retry_requested"
        _assert_held_decision(decision, stage="state_save_qc")


# --- 4. legacy decisions are unchanged ---------------------------------------------------


def test_non_member_sibling_cohort_reservation_does_not_block(tmp_path: Path) -> None:
    """A sibling execution cohort's held forecast is not the candidate's (#2543)."""

    raw_models = _raw_models(range(4))
    own, sibling = _candidates(raw_models[:2]), _candidates(raw_models[2:])
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    _first_pass(tmp_path / "own", repository, own, hold_stage=None)
    baseline = [(_decision(repository, c).action, _decision(repository, c).reason) for c in own]

    _first_pass(tmp_path / "sibling", repository, sibling, hold_stage="forecast")
    sibling_master = next(
        row for row in _rows(repository, "forecast") if row["run_id"] == _cohort_run_id(sibling)
    )
    assert (sibling_master["status"], sibling_master["slurm_job_id"]) == ("reserved", None)

    for candidate, before in zip(own, baseline, strict=True):
        assert not [row for row in _state(repository, candidate)["pipeline_jobs"] if row.get("status") == "reserved"]
        decision = _decision(repository, candidate)
        assert (decision.action, decision.reason) == before
        assert "held_reservations" not in decision.evidence
    for candidate in sibling:
        assert (_decision(repository, candidate).action, _decision(repository, candidate).reason) == _HELD


def test_bound_and_succeeded_reservation_resumes_after_the_completed_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After restart reconcile binds the held master and accounting projects COMPLETED,
    the block no longer applies: members resume state_save_qc (gfs 12Z shape)."""

    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE)
    repository, _raw_models_, candidates = _production_forecast_shape(
        monkeypatch, tmp_path, terminal_stage=COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE
    )
    held = _master(repository, "forecast")
    committed = repository.commit_pipeline_job_submit_attempt(
        str(held["idempotency_key"]),
        pipeline_job_id=str(held["job_id"]),
        expected_submission_attempt=int(held["submission_attempt"]),
        slurm_job_id="57553",
        transition=AcceptedSubmitTransition.accounting(
            "matched_bound", submit_outcome="accepted", matched_slurm_job_id="57553", status="submitted"
        ),
    )
    assert committed.committed

    tasks = tuple(
        SacctRecord(f"57553_{index}", "COMPLETED", "nhms_forecast", exit_code="0:0", array_task_id=index)
        for index in range(len(candidates))
    )
    master = SacctRecord(
        slurm_job_id="57553",
        raw_state="COMPLETED",
        job_name="nhms_forecast",
        exit_code="0:0",
        array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
        array_task_records=tasks,
    )
    inflight = reconcile_inflight_jobs(
        repository, sacct_query=lambda job_id: master if str(job_id) == "57553" else None
    )
    assert [(outcome.job_id, outcome.status) for outcome in inflight] == [(held["job_id"], "succeeded")]

    for candidate in candidates:
        assert repository._hydro_run_for(candidate.run_id)["status"] == "succeeded"
        decision = _decision(repository, candidate)
        assert (decision.action, decision.reason) == ("retry", "resume_after_completed_stage")
        assert decision.evidence["restart_stage"] == "state_save_qc"
        assert decision.evidence["native_shud_resubmitted"] is False


# Captured on the pre-#2666 source (9ed2fb6a5) with this exact fixture: the released
# reservation is ``reservation_lost`` (terminal, code-less), so the decision never
# saw a held row; the forcing-complete resume is the pre-existing decision.
_RELEASED_PRE_CHANGE_FACE = ("retry", "resume_after_completed_stage", "forecast")


def test_released_reservation_keeps_the_pre_change_decision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repository, _raw_models_, candidates = _production_forecast_shape(monkeypatch, tmp_path)
    held = _master(repository, "forecast")
    released = repository.release_identity_blocked_reservation(
        str(held["job_id"]),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=int(held["submission_attempt"]),
        expected_submission_attempt_started_at=datetime.fromisoformat(
            str(held["submission_attempt_started_at"]).replace("Z", "+00:00")
        ),
        expected_status="reserved",
        identity_blocked_streak=3,
    )
    assert released == 1
    row = _master(repository, "forecast")
    assert (row["status"], row["reconciliation_decision"]) == ("reservation_lost", "identity_mismatch_released")

    for candidate in candidates:
        decision = _decision(repository, candidate)
        assert (decision.action, decision.reason, decision.evidence.get("restart_stage")) == _RELEASED_PRE_CHANGE_FACE
        assert "held_reservations" not in decision.evidence


def test_reserved_row_with_a_real_slurm_binding_takes_the_active_job_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``reserved`` row that already carries a real Slurm id is not a held reservation.

    The typed accepted-submit commit never leaves a bound row ``reserved`` (the journal
    refuses that on a forecast master), so this is the legacy direct-write shape on a
    downstream master. It stays on the journal's active-job path, and the decision is
    the pre-change one.
    """

    raw_models = _raw_models(range(2))
    candidates = _candidates(raw_models)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    _first_pass(tmp_path, repository, candidates, hold_stage="parse")
    _forcing_witness(monkeypatch, tmp_path, candidates)
    repository.upsert_pipeline_job({**_master(repository, "parse"), "slurm_job_id": "57553"})
    bound = _master(repository, "parse")
    assert (bound["status"], bound["slurm_job_id"]) == ("reserved", "57553")

    for candidate in candidates:
        active = repository.active_slurm_jobs(source_id="gfs", cycle_time=_CYCLE_TIME, model_id=candidate.model_id)
        assert [row["job_id"] for row in active] == [bound["job_id"]]
        decision = _decision(repository, candidate)
        assert (decision.action, decision.reason) == ("skip", "terminal_hydro_success")
        assert "held_reservations" not in decision.evidence

    selected, blocked, skipped = _plan(_scheduler(tmp_path, repository, raw_models), raw_models)
    assert (selected, blocked) == ([], [])
    assert {row["reason"] for row in skipped} == {"active_slurm_job"}


# --- 5. evidence stays readable and bounded ----------------------------------------------


def test_held_reservations_are_bounded_by_the_candidate_state_job_limit() -> None:
    from services.orchestrator.scheduler_state_rows import _state_held_reservations

    rows = [
        {"job_id": f"job_{index}", "stage": "forecast", "status": "reserved", "slurm_job_id": None, "extra": "x"}
        for index in range(5)
    ]

    held = _state_held_reservations({"pipeline_jobs": rows, "job_limit": 2})

    assert [row["job_id"] for row in held] == ["job_0", "job_1"]
    assert all(set(row) <= _PROJECTION_KEYS for row in held)
    # ``local`` is not a real Slurm binding; a real id or an array task id is.
    local = _state_held_reservations({"pipeline_jobs": [{**rows[0], "slurm_job_id": "local"}]})
    assert [row["job_id"] for row in local] == ["job_0"]
    assert _state_held_reservations({"pipeline_jobs": [{**rows[0], "slurm_job_id": "57553"}]}) == []
    assert _state_held_reservations({"pipeline_jobs": [{**rows[0], "array_task_id": 3}]}) == []
    assert _state_held_reservations({"pipeline_jobs": [{**rows[0], "status": "reservation_lost"}]}) == []


def test_held_skip_reason_survives_pre_write_size_pressure_compaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.orchestrator.scheduler_evidence_payload import (
        _bounded_candidate_summary_rows,
        _compact_admissible_pass_payload,
    )

    repository, raw_models, candidates = _production_forecast_shape(monkeypatch, tmp_path)
    _selected, _blocked, skipped = _plan(
        _scheduler(tmp_path, repository, raw_models, candidate_state_job_limit=1), raw_models
    )
    assert len(skipped) == len(candidates)
    for row in skipped:
        held = row["state_evidence"]["held_reservations"]
        assert len(held) == 1
        assert all(set(entry) <= _PROJECTION_KEYS for entry in held)

    terminal = {**skipped[0], "candidate_id": "other", "reason": "terminal_hydro_success"}
    compacted = _compact_admissible_pass_payload({"status": "submitted", "skipped_candidates": [*skipped, terminal]})

    assert compacted is not None
    assert compacted["evidence_compaction"]["reason"] == "pre_write_size_pressure"
    assert [row["reason"] for row in compacted["skipped_candidates"]] == [
        *["active_duplicate_pipeline"] * len(candidates),
        "terminal_hydro_success",
    ]
    assert [row["reason"] for row in _bounded_candidate_summary_rows(compacted["skipped_candidates"])] == [
        *["active_duplicate_pipeline"] * len(candidates),
        "terminal_hydro_success",
    ]
