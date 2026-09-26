"""A model-less cohort row is attributed to exactly its recorded members (#2603).

Production shape: since #2543 a restart-compatible cohort of up to 47 models runs as one
execution cohort ``cycle_<src>_<stamp>_<stage>_cohort_<12hex>`` whose downstream master rows
carry no ``model_id``.  Their SUCCESS was credited cycle-wide, their FAILURE to nobody: a
permanently failed ``state_save_qc`` left every member auto-retrying with ``attempt 0``
past ``permanent_failure_guard``, while a single-model cohort (whose master names its model)
took the guard.  The requirement: classify every model-less cycle-scope row by the cycle's
recorded ``cohort_members`` before the candidate state is compacted -- ``member`` rows are the
candidate's own on both sides, ``non_member`` rows affect neither, a permanently failed
``incomplete`` row blocks as ``cohort_membership_unprovable``, and ``unwitnessed`` rows keep
their pre-change meaning -- and have downstream cohort rows record their members (B4).

Seams: the real ``orchestrate_cycle`` stage loop against a real
``FileOrchestrationJournalRepository``, then the real ``candidate_state`` read and the
scheduler's ``_candidate_state_decision``; cohort run ids come from the scheduler's own
``_candidate_execution_cohort_run_id``.  Only the Slurm runtime is a double.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import scheduler as scheduler_module
from services.orchestrator.accepted_submit_identity import accepted_submit_row_kind
from services.orchestrator.chain_stages import COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE
from services.orchestrator.file_orchestration_journal import (
    FileJournalRetryService,
    FileOrchestrationJournalRepository,
    _job_needs_restart_reconcile,
    _reconcile_inventory_row_kind,
)
from services.orchestrator.retry import RetryConfig
from tests.test_orchestration_chain import FakeCycleRepository, FakeCycleSlurmClient, _basins, _orchestrator

_CYCLE = "2026050100"
_CYCLE_ID = f"gfs_{_CYCLE}"
_CYCLE_TIME = datetime(2026, 5, 1, tzinfo=UTC)
_UNPROVABLE = "cohort_membership_unprovable"


@pytest.fixture(autouse=True)
def _state_save_qc_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE)


def _candidate(index: int) -> Any:
    model_id = f"model_{index}"
    return scheduler_module.SchedulerCandidate(
        candidate_id=f"gfs:2026-05-01T00:00:00Z:{model_id}:forecast_gfs_deterministic",
        source_id="gfs",
        cycle_id=_CYCLE_ID,
        cycle_time_utc=_CYCLE_TIME,
        model_id=model_id,
        basin_id=f"basin_{index}",
        basin_version_id=f"basin_v{index}",
        river_network_version_id=f"river_v{index}",
        segment_count=3,
        output_segment_count=3,
        model_package_uri=f"s3://nhms/models/{model_id}.tar",
        resource_profile={},
        display_capabilities={},
        horizon={},
        scenario_id="forecast_gfs_deterministic",
        run_id=f"fcst_gfs_{_CYCLE}_{model_id}",
        forcing_version_id=f"forc_gfs_{_CYCLE}_{model_id}",
        status="selected",
    )


def _cohort_run_id(restart_stage: str, candidates: Sequence[Any]) -> str:
    """The scheduler's own execution-cohort run id (never hand-written)."""

    cohort_key = scheduler_module._candidate_restart_cohort_key(restart_stage)
    if len(candidates) == 1:
        return scheduler_module._candidate_execution_cohort_run_id_for_candidate(
            "gfs", _CYCLE_TIME, cohort_key, candidates[0]
        )
    return scheduler_module._candidate_execution_cohort_run_id("gfs", _CYCLE_TIME, cohort_key, list(candidates))


def _cohort_basins(
    candidates: Sequence[Any],
    *,
    restart_stage: str = "forecast",
    evidence: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    run_id = _cohort_run_id(restart_stage, candidates)
    basins = []
    for candidate in candidates:
        index = int(candidate.model_id.rsplit("_", 1)[1])
        basin = _basins(index + 1)[index]
        basin.update(
            {
                "run_id": candidate.run_id,
                "candidate_id": candidate.candidate_id,
                "orchestration_run_id": run_id,
                "restart_stage": restart_stage,
                "state_evidence": dict((evidence or {}).get(candidate.model_id) or {"restart_stage": restart_stage}),
                "model_package_uri": candidate.model_package_uri,
                "model_package_checksum": f"sha256:{candidate.model_id}",
                "init_state_id": f"state_gfs_{candidate.model_id}_{_CYCLE}_gfs_2026043012_f012",
                "init_state_uri": f"s3://nhms/states/gfs/{candidate.model_id}/{_CYCLE}/state.cfg.ic",
                "init_state_checksum": f"sha256:state-{candidate.model_id}",
                "init_state_valid_time": "2026-05-01T00:00:00Z",
            }
        )
        basins.append(basin)
    return basins


class _Runtime(FakeCycleSlurmClient):
    """The Slurm runtime double, plus per-attempt task error codes for one stage."""

    def __init__(self, *, task_error_codes: dict[tuple[str, int], str] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.task_error_codes = task_error_codes or {}

    def get_array_task_results(self, job_id: str) -> list[dict[str, Any]]:
        job = self.jobs[job_id]
        code = self.task_error_codes.get((job["stage"], job["stage_attempt"]))
        results = super().get_array_task_results(job_id)
        if code is not None:
            for result in results:
                if result["status"] != "succeeded":
                    result["error_code"] = code
        return results

    def stage_submissions(self, stage: str) -> list[list[str]]:
        return [
            [task["model_id"] for task in payload.get("tasks") or []]
            for payload in self.submissions
            if payload.get("stage") == stage
        ]


def _run_pass(
    tmp_path: Path,
    repository: FileOrchestrationJournalRepository,
    client: FakeCycleSlurmClient,
    basins: list[dict[str, Any]],
    *,
    max_retries: int | None = 1,
) -> Any:
    retry_service = (
        None
        if max_retries is None
        else FileJournalRetryService(repository, RetryConfig(max_retries=max_retries, backoff_schedule=[0]))
    )
    orchestrator = _orchestrator(
        tmp_path,
        repository,
        client,
        terminal_stage=COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE,
        retry_service=retry_service,
    )
    return orchestrator.orchestrate_cycle("gfs", _CYCLE, basins)


def _state(repository: FileOrchestrationJournalRepository, candidate: Any, *, retry_limit: int) -> dict[str, Any]:
    state = FileOrchestrationJournalRepository(repository.root).candidate_state(
        source_id="gfs",
        cycle_time=_CYCLE_TIME,
        model_id=candidate.model_id,
        run_id=candidate.run_id,
        forcing_version_id=candidate.forcing_version_id,
        candidate_id=candidate.candidate_id,
        retry_limit=retry_limit,
    )
    assert state is not None
    return state


def _decision(repository: FileOrchestrationJournalRepository, candidate: Any, *, retry_limit: int = 1) -> Any:
    state = _state(repository, candidate, retry_limit=retry_limit)
    decision = scheduler_module._candidate_state_decision(candidate, state)
    assert decision is not None
    return decision


def _face(decision: Any) -> tuple[str, str, Any]:
    return decision.action, decision.reason, decision.evidence.get("retry_policy")


def _state_save_rows(repository: FileOrchestrationJournalRepository) -> dict[str, dict[str, Any]]:
    return {
        row["job_id"]: row
        for row in FileOrchestrationJournalRepository(repository.root).query_pipeline_jobs_by_cycle(_CYCLE_ID)
        if row.get("stage") == "state_save_qc"
    }


# --- 1. the production failure: a 3-member cohort's state_save_qc exhausts its retries ------


def test_permanently_failed_multi_member_state_save_qc_blocks_every_member(tmp_path: Path) -> None:
    candidates = [_candidate(index) for index in range(3)]
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = _Runtime(fail_stage="state_save_qc", array_results_by_stage={"state_save_qc": ["failed"] * 3})

    result = _run_pass(tmp_path, repository, client, _cohort_basins(candidates), max_retries=1)

    assert [(stage.stage, stage.status) for stage in result.stages] == [
        ("forecast", "succeeded"),
        ("state_save_qc", "failed"),
    ]
    run_id = _cohort_run_id("forecast", candidates)
    exhausted = _state_save_rows(repository)[f"job_{run_id}_state_save_qc_retry_1"]
    assert (exhausted["model_id"], exhausted["status"], exhausted["retry_count"]) == (None, "permanently_failed", 1)
    for candidate in candidates:
        decision = _decision(repository, candidate, retry_limit=1)
        assert (decision.action, decision.reason) == ("blocked", "permanent_failure_guard")
        assert decision.evidence["failure"]["stage"] == "state_save_qc"
        assert decision.evidence["failure"]["limit_exhausted"] is True
        assert decision.evidence["retry_policy"]["automatic_retry_allowed"] is False
        assert decision.evidence["retry_policy"]["attempt"] == exhausted["retry_count"]


# --- 2. transient failures consume the budget, exactly like a single-model cohort ----------


def test_transient_cohort_failures_exhaust_the_aligned_retry_budget(tmp_path: Path) -> None:
    candidates = [_candidate(index) for index in range(3)]
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = _Runtime(fail_stage="state_save_qc", array_results_by_stage={"state_save_qc": ["failed"] * 3})

    _run_pass(tmp_path, repository, client, _cohort_basins(candidates), max_retries=2)

    rows = sorted(_state_save_rows(repository).values(), key=lambda row: int(row["retry_count"]))
    assert [(row["model_id"], row["retry_count"]) for row in rows] == [(None, 0), (None, 1), (None, 2)]
    assert rows[-1]["status"] == "permanently_failed"
    assert client.stage_submissions("state_save_qc") == [[c.model_id for c in candidates]] * 3
    for candidate in candidates:
        decision = _decision(repository, candidate, retry_limit=2)
        assert (decision.action, decision.reason) == ("blocked", "permanent_failure_guard")
        assert decision.evidence["retry_policy"]["attempt"] == 2
        assert decision.evidence["retry_policy"]["automatic_retry_allowed"] is False


def _two_pass_trajectory(tmp_path: Path, member_count: int) -> list[list[tuple[str, str, Any]]]:
    """Two scheduler passes of a transiently failing state_save_qc, decision after each."""

    candidates = [_candidate(index) for index in range(member_count)]
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = _Runtime(
        fail_stage="state_save_qc", array_results_by_stage={"state_save_qc": ["failed"] * member_count}
    )
    trajectory = []
    restart_stage, evidence = "forecast", None
    for _pass in range(2):
        _run_pass(
            tmp_path,
            repository,
            client,
            _cohort_basins(candidates, restart_stage=restart_stage, evidence=evidence),
            max_retries=1,
        )
        decisions = {candidate.model_id: _decision(repository, candidate, retry_limit=3) for candidate in candidates}
        trajectory.append([_face(decision) for decision in decisions.values()])
        first = decisions[candidates[0].model_id]
        assert first.action == "retry"
        restart_stage = str(first.evidence["restart_stage"])
        evidence = {model_id: dict(decision.evidence) for model_id, decision in decisions.items()}
    return trajectory


def test_transient_multi_member_cohort_takes_the_single_model_retry_lane(tmp_path: Path) -> None:
    single = _two_pass_trajectory(tmp_path / "single", 1)
    multi = _two_pass_trajectory(tmp_path / "multi", 3)

    # The policy no longer depends on the member count: every member, every pass, carries the
    # single-model cohort's decision, with the attempt the cohort row's retry_count charged.
    for single_pass, multi_pass in zip(single, multi, strict=True):
        assert multi_pass == single_pass * 3
        assert single_pass[0][1] == "resume_downstream_after_durable_shud"
        assert single_pass[0][2]["attempt"] == 1


# --- 3. a strict-subset state_save_qc restart cohort is witnessed by its own rows -----------


@pytest.mark.parametrize("outcome", ["succeeded", "permanently_failed"])
def test_strict_subset_restart_cohort_is_attributed_to_exactly_its_members(tmp_path: Path, outcome: str) -> None:
    candidates = [_candidate(index) for index in range(3)]
    subset = candidates[:2]
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    pass_two = ["succeeded"] * 2 if outcome == "succeeded" else ["failed"] * 2
    client = _Runtime(
        failures_before_success_by_stage={"state_save_qc": 2 if outcome == "succeeded" else 99},
        array_results_by_stage={"state_save_qc": [["failed"] * 3, ["failed"] * 3, pass_two]},
        # A non-transient task code: the restart cohort's failure is permanent at once.
        task_error_codes={("state_save_qc", 2): "STATE_SAVE_QC_TASK_FAILED"},
    )
    _run_pass(tmp_path, repository, client, _cohort_basins(candidates), max_retries=1)
    before = {candidate.model_id: _decision(repository, candidate, retry_limit=3) for candidate in candidates}
    assert {decision.reason for decision in before.values()} == {"resume_downstream_after_durable_shud"}

    # Pass 2 restarts state_save_qc for two of the three members only.
    evidence = {model_id: dict(decision.evidence) for model_id, decision in before.items()}
    _run_pass(
        tmp_path,
        repository,
        client,
        _cohort_basins(subset, restart_stage="state_save_qc", evidence=evidence),
        max_retries=0,
    )

    subset_run_id = _cohort_run_id("state_save_qc", subset)
    assert subset_run_id.split("_cohort_")[0].endswith("_state_save_qc")
    subset_row = _state_save_rows(repository)[f"job_{subset_run_id}_state_save_qc"]
    assert subset_row["status"] == outcome
    assert [member["model_id"] for member in subset_row["cohort_members"]] == [c.model_id for c in subset]
    for candidate in subset:
        decision = _decision(repository, candidate, retry_limit=3)
        if outcome == "succeeded":
            # Credited, so the next pass submits nothing for it.
            assert decision.action == "skip"
            assert decision.reason in {"terminal_pipeline_success", "terminal_hydro_success"}
        else:
            assert (decision.action, decision.reason) == ("blocked", "permanent_failure_guard")
            assert decision.evidence["failure"]["stage"] == "state_save_qc"
    # The member the restart cohort left out neither gains its success nor inherits its failure.
    left_out = candidates[2]
    assert _face(_decision(repository, left_out, retry_limit=3)) == _face(before[left_out.model_id])


# --- 4. sibling execution cohorts of one cycle (#2543) -----------------------------------


def test_sibling_cohorts_affect_only_their_own_members(tmp_path: Path) -> None:
    failing = [_candidate(0), _candidate(1)]
    succeeding = [_candidate(2), _candidate(3)]
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = _Runtime(
        failures_before_success_by_stage={"state_save_qc": 1},
        array_results_by_stage={"state_save_qc": [["failed"] * 2, ["succeeded"] * 2]},
        task_error_codes={("state_save_qc", 0): "STATE_SAVE_QC_TASK_FAILED"},
    )

    _run_pass(tmp_path, repository, client, _cohort_basins(failing), max_retries=0)
    alone = {candidate.model_id: _face(_decision(repository, candidate)) for candidate in failing}
    _run_pass(tmp_path, repository, client, _cohort_basins(succeeding), max_retries=0)

    for candidate in failing:
        decision = _decision(repository, candidate)
        # The sibling's success is not this cohort's completion.
        assert (decision.action, decision.reason) == ("blocked", "permanent_failure_guard")
        assert _face(decision) == alone[candidate.model_id]
    for candidate in succeeding:
        decision = _decision(repository, candidate)
        # The sibling's permanent failure is not this cohort's failure.
        assert decision.action == "skip"


# --- 5. partial forcing narrows the downstream rows' membership ---------------------------


def test_partial_forcing_attributes_downstream_rows_only_to_surviving_members(tmp_path: Path) -> None:
    candidates = [_candidate(index) for index in range(3)]
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = _Runtime(
        fail_stage="state_save_qc",
        array_results_by_stage={"forcing": ["succeeded", "succeeded", "failed"], "state_save_qc": ["failed"] * 2},
    )

    basins = _cohort_basins(candidates, restart_stage="forcing")
    result = _run_pass(tmp_path, repository, client, basins, max_retries=None)

    assert [(stage.stage, stage.status) for stage in result.stages] == [
        ("forcing", "partially_failed"),
        ("forecast", "succeeded"),
        ("state_save_qc", "failed"),
    ]
    assert client.stage_submissions("forecast") == [["model_0", "model_1"]]

    dropped = candidates[2]
    # The member forcing lost is judged by its own forcing failure, not by the narrowed
    # downstream rows; the survivors own the failed state_save_qc row.
    decision = _decision(repository, dropped)
    assert decision.reason == "retry_failed_candidate"
    assert decision.evidence["failure"]["stage"] == "forcing"
    for survivor in candidates[:2]:
        decision = _decision(repository, survivor)
        assert (decision.reason, decision.evidence["restart_stage"]) == (
            "resume_downstream_after_durable_shud",
            "state_save_qc",
        )

    def cohort_rows(candidate: Any) -> dict[str, str]:
        return {
            str(job["stage"]): str(job.get("cohort_membership"))
            for job in _state(repository, candidate, retry_limit=1)["pipeline_jobs"]
            if job.get("model_id") in (None, "")
        }

    for survivor in candidates[:2]:
        assert cohort_rows(survivor) == {"forcing": "member", "forecast": "member", "state_save_qc": "member"}
    # Only the forcing row (whose own list still names it) is the dropped member's.
    assert cohort_rows(dropped) == {"forcing": "member"}


# --- 6. incomplete recorded membership fails closed ---------------------------------------


def _seed_historical_cohort(
    tmp_path: Path, *, members: list[dict[str, Any]] | None, own_success_for: str | None = None
) -> tuple[FileOrchestrationJournalRepository, list[Any], str]:
    """A journal whose members' forecasts succeeded and whose cohort state_save_qc row failed.

    The cohort row is written through the ordinary ``upsert_pipeline_job`` a pre-#2603 (or
    corrupted) journal holds: ``members=None`` records no membership at all (unwitnessed).
    """

    candidates = [_candidate(index) for index in range(3)]
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=_CYCLE_TIME)
    for index, candidate in enumerate(candidates):
        repository.upsert_pipeline_job(
            {
                "job_id": f"job_{candidate.run_id}_forecast",
                "run_id": candidate.run_id,
                "cycle_id": _CYCLE_ID,
                "job_type": "run_shud_forecast",
                "stage": "forecast",
                "model_id": candidate.model_id,
                "status": "succeeded",
                "slurm_job_id": str(900 + index),
                "retry_count": 0,
                "finished_at": "2026-05-01T01:00:00Z",
            }
        )
    run_id = _cohort_run_id("state_save_qc", candidates)
    row: dict[str, Any] = {
        "job_id": f"job_{run_id}_state_save_qc",
        "run_id": run_id,
        "cycle_id": _CYCLE_ID,
        "job_type": "save_state_snapshot_array",
        "stage": "state_save_qc",
        "model_id": None,
        "status": "permanently_failed",
        "slurm_job_id": "950",
        "retry_count": 1,
        "error_code": "NODE_FAILURE",
        "finished_at": "2026-05-01T02:00:00Z",
    }
    if members is not None:
        row["cohort_members"] = members
    repository.upsert_pipeline_job(row)
    if own_success_for is not None:
        owner = next(candidate for candidate in candidates if candidate.model_id == own_success_for)
        repository.upsert_pipeline_job(
            {
                "job_id": f"job_{owner.run_id}_state_save_qc",
                "run_id": owner.run_id,
                "cycle_id": _CYCLE_ID,
                "job_type": "save_state_snapshot",
                "stage": "state_save_qc",
                "model_id": owner.model_id,
                "status": "succeeded",
                "slurm_job_id": "960",
                "retry_count": 0,
                "finished_at": "2026-05-01T03:00:00Z",
            }
        )
    return repository, candidates, run_id


def _members_with_blank_model(candidates: Sequence[Any]) -> list[dict[str, Any]]:
    members = [
        {
            "array_task_id": index,
            "candidate_id": candidate.candidate_id,
            "run_id": candidate.run_id,
            "model_id": candidate.model_id,
            "basin_id": candidate.basin_id,
            "scenario_id": "forecast_gfs_deterministic",
            "restart_stage": "state_save_qc",
        }
        for index, candidate in enumerate(candidates)
    ]
    members[-1]["model_id"] = ""
    return members


def test_incomplete_membership_with_a_permanent_failure_blocks_fail_closed(tmp_path: Path) -> None:
    repository, candidates, run_id = _seed_historical_cohort(
        tmp_path, members=_members_with_blank_model([_candidate(index) for index in range(3)])
    )

    for candidate in candidates:
        decision = _decision(repository, candidate)
        assert (decision.action, decision.reason) == ("blocked", _UNPROVABLE)
        assert decision.evidence["cohort_run_id"] == run_id
        assert decision.evidence["retry_policy"]["automatic_retry_allowed"] is False


def test_own_terminal_success_outranks_an_unprovable_cohort_failure(tmp_path: Path) -> None:
    repository, candidates, _run_id = _seed_historical_cohort(
        tmp_path,
        members=_members_with_blank_model([_candidate(index) for index in range(3)]),
        own_success_for="model_0",
    )

    assert _decision(repository, candidates[0]).action == "skip"
    assert [_decision(repository, candidate).reason for candidate in candidates[1:]] == [_UNPROVABLE, _UNPROVABLE]


def test_manual_retry_marker_clears_the_unprovable_block(tmp_path: Path) -> None:
    repository, candidates, run_id = _seed_historical_cohort(
        tmp_path, members=_members_with_blank_model([_candidate(index) for index in range(3)])
    )

    FileJournalRetryService(repository).record_manual_repair(run_id, trusted_internal=True)

    for candidate in candidates:
        decision = _decision(repository, candidate)
        assert decision.reason != _UNPROVABLE
        assert decision.action == "retry"


def test_manual_retry_marker_clears_a_member_cohort_permanent_failure(tmp_path: Path) -> None:
    candidates = [_candidate(index) for index in range(3)]
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = _Runtime(fail_stage="state_save_qc", array_results_by_stage={"state_save_qc": ["failed"] * 3})
    _run_pass(tmp_path, repository, client, _cohort_basins(candidates), max_retries=1)
    assert {_decision(repository, candidate).reason for candidate in candidates} == {"permanent_failure_guard"}

    run_id = _cohort_run_id("forecast", candidates)
    FileJournalRetryService(repository).record_manual_repair(run_id, trusted_internal=True)

    for candidate in candidates:
        decision = _decision(repository, candidate)
        assert (decision.action, decision.reason) == ("retry", "manual_retry_requested")


# --- 7. unwitnessed historical rows keep their pre-change decisions -----------------------

# Captured on origin/master (d40fd6bc3) with this exact fixture: a model-less cohort row that
# records no membership is not attributed to any candidate, so each one resumes state_save_qc
# from its own completed forecast.
_UNWITNESSED_ORIGIN_MASTER_FACE = (
    "retry",
    "resume_after_completed_stage",
    {"automatic_retry_allowed": True, "manual_retry_required": False, "attempt": 0, "retry_limit": 1},
)


def test_unwitnessed_historical_cohort_row_keeps_the_origin_master_decision(tmp_path: Path) -> None:
    repository, candidates, _run_id = _seed_historical_cohort(tmp_path, members=None)

    for candidate in candidates:
        decision = _decision(repository, candidate)
        assert _face(decision) == _UNWITNESSED_ORIGIN_MASTER_FACE
        assert decision.evidence["restart_stage"] == "state_save_qc"
        assert all(
            job.get("cohort_membership") == "unwitnessed"
            for job in _state(repository, candidate, retry_limit=1)["pipeline_jobs"]
            if job.get("model_id") in (None, "")
        )


# --- 8. B4: the downstream cohort row's member record keeps the plain row contract -------


def test_downstream_cohort_rows_record_members_without_becoming_accepted_submit_masters(tmp_path: Path) -> None:
    candidates = [_candidate(index) for index in range(3)]
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = _Runtime(fail_stage="state_save_qc", array_results_by_stage={"state_save_qc": ["failed"] * 3})
    _run_pass(tmp_path, repository, client, _cohort_basins(candidates), max_retries=1)

    run_id = _cohort_run_id("forecast", candidates)
    rows = _state_save_rows(repository)
    for job_id in (f"job_{run_id}_state_save_qc", f"job_{run_id}_state_save_qc_retry_1"):
        row = rows[job_id]
        assert [member["model_id"] for member in row["cohort_members"]] == ["model_0", "model_1", "model_2"]
        assert {member["restart_stage"] for member in row["cohort_members"]} == {"state_save_qc"}
        # Members only: none of the accepted-submit master markers B4 could have added.
        for marker in ("accepted_submit_contract_version", "cohort_digest", "expected_slurm_user", "slurm_comment"):
            assert row.get(marker) in (None, ""), marker
        assert accepted_submit_row_kind(row) is None
        assert _reconcile_inventory_row_kind(row) == "legacy"
        without_members = {key: value for key, value in row.items() if key != "cohort_members"}
        for status in ("running", "reserved", "succeeded"):
            assert _job_needs_restart_reconcile({**row, "status": status}) == _job_needs_restart_reconcile(
                {**without_members, "status": status}
            )
    # An ordinary status upsert keeps the recorded members (upsert merge).
    base = rows[f"job_{run_id}_state_save_qc"]
    repository.upsert_pipeline_job(
        {
            **{key: base[key] for key in ("job_id", "run_id", "cycle_id", "job_type", "stage", "model_id")},
            "status": "permanently_failed",
            "error_code": "NODE_FAILURE",
        }
    )
    merged = _state_save_rows(repository)[f"job_{run_id}_state_save_qc"]
    assert [member["model_id"] for member in merged["cohort_members"]] == ["model_0", "model_1", "model_2"]
    # Compaction still strips the member list from every candidate state.
    for candidate in candidates:
        jobs = _state(repository, candidate, retry_limit=1)["pipeline_jobs"]
        assert not any(job.get("cohort_members") for job in jobs)


def test_downstream_member_record_is_limited_to_accepted_submit_repositories(tmp_path: Path) -> None:
    candidates = [_candidate(index) for index in range(3)]
    repository = FakeCycleRepository()
    assert not getattr(repository, "supports_accepted_submit_reconcile", False)
    _run_pass(tmp_path, repository, FakeCycleSlurmClient(), _cohort_basins(candidates), max_retries=None)

    state_save = [job for job in repository.jobs.values() if job.get("stage") == "state_save_qc"]
    assert state_save
    assert all(job.get("cohort_members") in (None, [], ()) for job in state_save)


def test_member_record_of_a_47_member_cohort_row_is_bounded(tmp_path: Path) -> None:
    from services.orchestrator.accepted_submit_identity import canonical_forecast_cohort_members

    basins = [
        {"model_id": f"dg_{index:032x}", "basin_id": f"basins_{index:03d}", "task_id": index} for index in range(47)
    ]
    members = [
        {**member, "restart_stage": "state_save_qc"}
        for member in canonical_forecast_cohort_members(source_id="IFS", cycle_time=_CYCLE_TIME, basins=basins)
    ]
    size = len(json.dumps(members, separators=(",", ":")))
    # ~340 bytes per production-shaped member: one row carries about 16 KB, and compaction
    # keeps it out of the 47 candidate states entirely.
    assert 14_000 < size < 20_000
