"""#2542: a mixed-floor cohort retry charges each member its own attempt.

The cohort master is minted once above ``max(floors)`` (#2404), so charging every
member the master's attempt billed a low-floor member for retries it never
consumed.  The master now records each floored member's floor at reservation
(``retry_attempt_floors``, first-write frozen), and reconcile charges each member
``min(eff, floor + 1 + (eff - (max(floors) + 1)))`` for a listed member, or the
shared ``eff`` for an unlisted member (a forecast restart without a budget floor,
which may already carry forecast history), when the list is empty, or when ``eff``
is not above the largest floor.

Seams: the budget scenarios run the REAL scheduler candidate construction over a
REAL file journal, handed to the REAL ``orchestrate_cycle`` / reservation path
(``tests/test_retry_mint_floor.py`` helpers); the row-contract scenarios drive the
REAL journal reserve / commit / accepted-submit projection API with the
production master payload (``tests.gateway_reconcile_helpers``).  Only Slurm is
faked.  The expected charges come from the issue's acceptance geometry.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

#: The master field this change adds (independent literal: it is the durable contract).
RETRY_ATTEMPT_FLOORS_FIELD = "retry_attempt_floors"
_RETRY_DECISION = "retry_strict_warm_start_terminal_init_state_mismatch"
_BLOCKED_DECISION = "blocked_strict_warm_start_init_state_mismatch"
#: The strict-lane retry of a member whose last forecast attempt FAILED.
_FAILED_RETRY_DECISION = "retry_strict_warm_start_retry_run_manifest_mismatch"


def _spent_full_chain_rows(model_id: str, suffixes: tuple[str, ...], first_slurm: int) -> list[dict[str, Any]]:
    """``model_id`` spent ``len(suffixes) - 1`` charged attempts under its own ``_full_<model>`` prefix."""

    from tests.test_production_scheduler import _budget_full_chain_master_row

    return [
        {
            **_budget_full_chain_master_row(suffix, slurm_job_id=str(first_slurm + offset)),
            "job_id": f"job_cycle_gfs_2026052100_full_{model_id}_forecast{suffix}",
            "run_id": f"cycle_gfs_2026052100_full_{model_id}",
        }
        for offset, suffix in enumerate(suffixes)
    ]


def _member_charges(root: Path, before: set[str]) -> dict[str, list[int]]:
    """Each model's reconciled per-model forecast rows written since ``before``, in id order."""

    from tests.test_retry_mint_floor import _forecast_rows

    charges: dict[str, list[int]] = {}
    for row in sorted(_forecast_rows(root), key=lambda item: str(item["job_id"])):
        if str(row["job_id"]) in before or row.get("model_id") in (None, ""):
            continue
        charges.setdefault(str(row["model_id"]), []).append(int(row["retry_count"]))
    return charges


def _master(root: Path, job_id: str) -> dict[str, Any]:
    from tests.test_retry_mint_floor import _forecast_rows

    (row,) = [row for row in _forecast_rows(root) if row["job_id"] == job_id]
    return row


# ---------------------------------------------------------------------------
# Budget geometry through the real scheduler + chain
# ---------------------------------------------------------------------------


def test_limit_three_members_at_floor_zero_and_two_are_charged_one_and_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec scenario: limit 3, A floor 0, B floor 2 -> A's next attempt is 1, B's is 3."""

    from tests.test_retry_mint_floor import (
        _cohort_run_id,
        _forecast_rows,
        _new_forecast_ids,
        _pass_decisions,
        _rerun,
        _seed_cohort_budget_journal,
    )

    models = ("model_a", "model_b")
    root, scheduler = _seed_cohort_budget_journal(
        monkeypatch,
        tmp_path,
        models,
        _spent_full_chain_rows("model_b", ("", "_retry_1", "_retry_1_retry_2"), 500),
        retry_limit=3,
    )
    built = scheduler(models)
    candidates, decisions = _pass_decisions(built)
    assert decisions == {
        "model_a": (_RETRY_DECISION, {"stage": "forecast", "attempt": 0}),
        "model_b": (_RETRY_DECISION, {"stage": "forecast", "attempt": 2}),
    }
    before = {str(row["job_id"]) for row in _forecast_rows(root)}
    _basins, result = _rerun(tmp_path, monkeypatch, root, built, candidates)
    assert result.status == "succeeded"
    master_id = f"job_{_cohort_run_id(models)}_forecast_retry_3"
    assert [new for new in _new_forecast_ids(root, before) if new.startswith("job_cycle_")] == [master_id]
    assert _master(root, master_id)[RETRY_ATTEMPT_FLOORS_FIELD] == [
        {"model_id": "model_a", "attempt": 0},
        {"model_id": "model_b", "attempt": 2},
    ]
    assert _member_charges(root, before) == {"model_a": [1], "model_b": [3]}

    _candidates, after = _pass_decisions(scheduler(models))
    assert after == {
        "model_a": (_RETRY_DECISION, {"stage": "forecast", "attempt": 1}),
        "model_b": (_BLOCKED_DECISION, 3),
    }


def test_inline_auto_retry_of_a_mixed_floor_cohort_advances_every_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_retry_2`` fails and the same call mints ``_retry_3``: A is charged 1 then 2, B 2 then 3.

    The added ``eff - (max(floors) + 1)`` term is what carries the master's own
    inline attempt to every member: each member's charge moves with each of its
    submissions, and no member is charged more than the shared attempt.  Wired as
    production wires it: scheduler ``retry_limit`` 3 == the inline service's
    ``max_retries`` (``scheduler_state_types.DEFAULT_RETRY_LIMIT`` /
    ``SlurmGatewaySettings.max_retries``), so no member's total submissions exceed
    the limit: next pass A (charged 2) retries at floor 2, B (charged 3) is blocked.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileJournalRetryService,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.retry import RetryConfig
    from tests.test_orchestration_chain import FakeCycleSlurmClient
    from tests.test_retry_mint_floor import (
        _cohort_run_id,
        _forecast_rows,
        _new_forecast_ids,
        _pass_decisions,
        _rerun,
        _seed_cohort_budget_journal,
    )

    models = ("model_a", "model_b")
    root, scheduler = _seed_cohort_budget_journal(
        monkeypatch, tmp_path, models, _spent_full_chain_rows("model_b", ("", "_retry_1"), 600), retry_limit=3
    )
    built = scheduler(models)
    candidates, decisions = _pass_decisions(built)
    assert decisions == {
        "model_a": (_RETRY_DECISION, {"stage": "forecast", "attempt": 0}),
        "model_b": (_RETRY_DECISION, {"stage": "forecast", "attempt": 1}),
    }
    client = FakeCycleSlurmClient(fail_stage="forecast", array_results_by_stage={"forecast": ["failed", "failed"]})
    before = {str(row["job_id"]) for row in _forecast_rows(root)}
    _basins, result = _rerun(
        tmp_path,
        monkeypatch,
        root,
        built,
        candidates,
        slurm_client=client,
        retry_service=FileJournalRetryService(
            FileOrchestrationJournalRepository(root), RetryConfig(max_retries=3, backoff_schedule=[0])
        ),
    )

    assert result.status == "failed"
    base = f"job_{_cohort_run_id(models)}_forecast"
    masters = [new for new in _new_forecast_ids(root, before) if new.startswith("job_cycle_")]
    assert masters == [f"{base}_retry_2", f"{base}_retry_3"]
    assert sum(1 for item in client.submissions if item.get("stage") == "forecast") == 2
    # Both masters recorded the same floors (the same basins reserved them).
    for master_id in masters:
        assert _master(root, master_id)[RETRY_ATTEMPT_FLOORS_FIELD] == [
            {"model_id": "model_a", "attempt": 0},
            {"model_id": "model_b", "attempt": 1},
        ]
    charges = _member_charges(root, before)
    assert {model_id: sorted(values) for model_id, values in charges.items()} == {
        "model_a": [1, 2],
        "model_b": [2, 3],
    }

    later, after = _pass_decisions(scheduler(models))
    assert after == {
        "model_a": (_FAILED_RETRY_DECISION, {"stage": "forecast", "attempt": 2}),
        "model_b": ("permanent_failure", 3),
    }
    assert [candidate.model_id for candidate in later] == ["model_a"]


def test_partial_nested_retry_keeps_the_low_floor_members_own_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec scenario (review round 1): limit 3, A floor 0, B floor 2, ``_retry_3`` then only A fails.

    The partial-array retry narrows the active basins to A before reserving
    ``_retry_4``; the nested master still records the floors of the master it
    retries, so A is charged 2 (not 4) and keeps a retry next pass.

    Deviation from production wiring (reported): the inline service declines at
    ``retry_count >= max_retries`` (``retry.classify_failure``), so with the
    scheduler's limit 3 and ``max_retries=3`` ``_retry_3`` is never retried in the
    same call.  ``max_retries=4`` reaches the geometry the spec names.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileJournalRetryService,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.retry import RetryConfig
    from tests.test_orchestration_chain import FakeCycleSlurmClient
    from tests.test_retry_mint_floor import (
        _cohort_run_id,
        _forecast_rows,
        _new_forecast_ids,
        _pass_decisions,
        _rerun,
        _seed_cohort_budget_journal,
    )

    models = ("model_a", "model_b")
    root, scheduler = _seed_cohort_budget_journal(
        monkeypatch,
        tmp_path,
        models,
        _spent_full_chain_rows("model_b", ("", "_retry_1", "_retry_1_retry_2"), 500),
        retry_limit=3,
    )
    built = scheduler(models)
    candidates, _decisions = _pass_decisions(built)
    # ``_retry_3``: A fails, B succeeds; the nested ``_retry_4`` (A only) fails again.
    client = FakeCycleSlurmClient(array_results_by_stage={"forecast": [["failed", "succeeded"], ["failed"]]})
    before = {str(row["job_id"]) for row in _forecast_rows(root)}
    _basins, result = _rerun(
        tmp_path,
        monkeypatch,
        root,
        built,
        candidates,
        slurm_client=client,
        retry_service=FileJournalRetryService(
            FileOrchestrationJournalRepository(root), RetryConfig(max_retries=4, backoff_schedule=[0])
        ),
    )

    # A partially failed cohort terminal: B's task succeeded, A's did not.
    assert result.status == "forcing_ready_partial"
    forecast_tasks = [
        [task["model_id"] for task in item["tasks"]] for item in client.submissions if item.get("stage") == "forecast"
    ]
    assert forecast_tasks == [["model_a", "model_b"], ["model_a"]]
    base = f"job_{_cohort_run_id(models)}_forecast"
    masters = [new for new in _new_forecast_ids(root, before) if new.startswith("job_cycle_")]
    assert masters == [f"{base}_retry_3", f"{base}_retry_4"]
    for master_id in masters:
        assert _master(root, master_id)[RETRY_ATTEMPT_FLOORS_FIELD] == [
            {"model_id": "model_a", "attempt": 0},
            {"model_id": "model_b", "attempt": 2},
        ]
    assert _member_charges(root, before) == {"model_a": [1, 2], "model_b": [3]}

    _candidates, after = _pass_decisions(scheduler(models))
    assert after == {
        "model_a": (_FAILED_RETRY_DECISION, {"stage": "forecast", "attempt": 2}),
        "model_b": (_BLOCKED_DECISION, 3),
    }


def test_failure_lane_mixed_floor_cohort_stays_within_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed-floor ``retry_failed_candidate`` cohort: B at the limit stops, A keeps one retry.

    Retry limit 2.  A spent nothing, B spent attempt 1 under ``fcst_..._model_b``.
    The failing cohort mints ``_retry_2``; the inline service stops at its own
    limit (one forecast submission).  Next pass B is ``permanent_failure``, while A,
    charged 1, still has a budgeted retry.
    """

    from tests.test_retry_mint_floor import (
        _cohort_run_id,
        _failing_rerun,
        _forecast_rows,
        _new_forecast_ids,
        _pass_decisions,
        _seed_cohort_budget_journal,
        _spent_failure_row,
    )

    models = ("model_a", "model_b")
    root, scheduler = _seed_cohort_budget_journal(monkeypatch, tmp_path, models, [_spent_failure_row("model_b")])
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "false")
    # model_a has no failed row of its own, so it needs its own retry reason: a
    # failed forecast attempt 0 row under the same prefix (charge 0 -> floor 0).
    from tests.test_production_scheduler import _record_budget_attempt

    _record_budget_attempt(
        root,
        {**_spent_failure_row("model_a"), "job_id": "job_fcst_gfs_2026052100_model_a_forecast"},
    )

    built = scheduler(models)
    candidates, decisions = _pass_decisions(built)
    assert decisions == {
        "model_a": ("retry_failed", {"stage": "forecast", "attempt": 0}),
        "model_b": ("retry_failed", {"stage": "forecast", "attempt": 1}),
    }
    before = {str(row["job_id"]) for row in _forecast_rows(root)}
    submitted = _failing_rerun(tmp_path, monkeypatch, root, built, candidates)
    minted = [new for new in _new_forecast_ids(root, before) if new.startswith("job_cycle_")]
    assert (submitted, minted) == (1, [f"job_{_cohort_run_id(models)}_forecast_retry_2"])
    assert _member_charges(root, before) == {"model_a": [1], "model_b": [2]}

    later, decisions = _pass_decisions(scheduler(models))
    assert decisions["model_b"][0] == "permanent_failure"
    assert decisions["model_a"] == ("retry_failed", {"stage": "forecast", "attempt": 1})
    assert [candidate.model_id for candidate in later] == ["model_a"]


def test_cohort_without_a_floored_member_keeps_the_shared_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-deploy cohort whose decisions carry no floor: empty list, shared ``eff`` (pin)."""

    from tests.test_retry_mint_floor import (
        _cohort_run_id,
        _forecast_rows,
        _new_forecast_ids,
        _pass_decisions,
        _rerun,
        _seed_cohort_budget_journal,
    )

    models = ("model_a", "model_b")
    root, scheduler = _seed_cohort_budget_journal(monkeypatch, tmp_path, models)
    built = scheduler(models)
    candidates, _decisions = _pass_decisions(built)
    for candidate in candidates:
        candidate.state_evidence.pop("retry_attempt_floor")
    before = {str(row["job_id"]) for row in _forecast_rows(root)}
    _basins, result = _rerun(tmp_path, monkeypatch, root, built, candidates)
    assert result.status == "succeeded"
    master_id = f"job_{_cohort_run_id(models)}_forecast"
    assert [new for new in _new_forecast_ids(root, before) if new.startswith("job_cycle_")] == [master_id]
    assert _master(root, master_id)[RETRY_ATTEMPT_FLOORS_FIELD] == []
    assert _member_charges(root, before) == {"model_a": [0], "model_b": [0]}


def test_unlisted_member_with_more_history_is_charged_the_shared_attempt_and_keeps_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unlisted member (no floor) is charged the master's ``eff``; its older, higher charge still reads.

    model_b was charged 2 under an older row and rides this cohort without a
    floor; model_a's floor 0 mints ``_retry_1`` (eff 1).  model_b is charged the
    shared 1, and the budget's max over all rows still reads 2 next pass.
    """

    from tests.test_retry_mint_floor import (
        _forecast_rows,
        _pass_decisions,
        _rerun,
        _seed_cohort_budget_journal,
    )

    older_b = {
        "job_id": "job_fcst_gfs_2026052100_model_b_forecast_reconciled_307_0",
        "run_id": "fcst_gfs_2026052100_model_b",
        "cycle_id": "gfs_2026052100",
        "model_id": "model_b",
        "candidate_id": "gfs:2026-05-21T00:00:00Z:model_b:forecast_gfs_deterministic",
        "stage": "forecast",
        "job_type": "run_shud_forecast_array",
        "status": "succeeded",
        "slurm_job_id": "307_0",
        "retry_count": 2,
    }
    models = ("model_a", "model_b")
    root, scheduler = _seed_cohort_budget_journal(monkeypatch, tmp_path, models, [older_b], retry_limit=4)
    built = scheduler(models)
    candidates, decisions = _pass_decisions(built)
    assert decisions["model_b"] == (_RETRY_DECISION, {"stage": "forecast", "attempt": 2})
    # model_b rides this cohort WITHOUT a floor (an unlisted member).
    (b_candidate,) = [candidate for candidate in candidates if candidate.model_id == "model_b"]
    b_candidate.state_evidence.pop("retry_attempt_floor")
    before = {str(row["job_id"]) for row in _forecast_rows(root)}
    _basins, result = _rerun(tmp_path, monkeypatch, root, built, candidates)
    assert result.status == "succeeded"
    assert _member_charges(root, before) == {"model_a": [1], "model_b": [1]}

    _candidates, after = _pass_decisions(scheduler(models))
    assert after["model_b"] == (_RETRY_DECISION, {"stage": "forecast", "attempt": 2})


def test_unlisted_member_below_the_masters_attempt_is_charged_the_shared_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec scenario: a member without a recorded floor keeps the shared charge (review round 1).

    Retry limit 2.  X (model_a) spent attempt 1 and joins as a forecast restart
    WITHOUT a floor (the manifest-missing / quarantine / missing-output shape);
    Y (model_b) floor 1 mints ``_retry_2`` (eff 2).  X is charged 2 and is blocked
    at the limit next pass.  Charging it base 0 would leave its budget read at 1
    and buy it one more forecast submission.
    """

    from tests.test_retry_mint_floor import (
        _cohort_run_id,
        _forecast_rows,
        _new_forecast_ids,
        _pass_decisions,
        _rerun,
        _seed_cohort_budget_journal,
    )

    models = ("model_a", "model_b")
    root, scheduler = _seed_cohort_budget_journal(
        monkeypatch,
        tmp_path,
        models,
        _spent_full_chain_rows("model_a", ("", "_retry_1"), 500)
        + _spent_full_chain_rows("model_b", ("", "_retry_1"), 600),
    )
    built = scheduler(models)
    candidates, decisions = _pass_decisions(built)
    assert decisions == {
        "model_a": (_RETRY_DECISION, {"stage": "forecast", "attempt": 1}),
        "model_b": (_RETRY_DECISION, {"stage": "forecast", "attempt": 1}),
    }
    (x_candidate,) = [candidate for candidate in candidates if candidate.model_id == "model_a"]
    x_candidate.state_evidence.pop("retry_attempt_floor")
    before = {str(row["job_id"]) for row in _forecast_rows(root)}
    _basins, result = _rerun(tmp_path, monkeypatch, root, built, candidates)
    assert result.status == "succeeded"
    master_id = f"job_{_cohort_run_id(models)}_forecast_retry_2"
    assert [new for new in _new_forecast_ids(root, before) if new.startswith("job_cycle_")] == [master_id]
    assert _master(root, master_id)[RETRY_ATTEMPT_FLOORS_FIELD] == [{"model_id": "model_b", "attempt": 1}]
    assert _member_charges(root, before) == {"model_a": [2], "model_b": [2]}

    later, after = _pass_decisions(scheduler(models))
    assert after == {"model_a": (_BLOCKED_DECISION, 2), "model_b": (_BLOCKED_DECISION, 2)}
    assert later == []


def test_one_member_cohort_after_an_occupied_id_skip_keeps_the_masters_attempt(tmp_path: Path) -> None:
    """Pin (single-model, #2404 must-preserve): the one member's floor is the max, so it is charged ``eff``.

    Public ``orchestrate_cycle`` on a real file journal.  The run's own history is
    one terminal bare row, the evidence floor is 1, and ``_retry_2`` is already
    held by a terminal legacy row, so the minter skips to ``_retry_3``; the member
    is charged 3 -- exactly the pre-change charge.
    """

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from tests.test_orchestration_chain import FakeCycleSlurmClient, _orchestrator

    run_id = "cycle_gfs_2026050100_forecast_model_0"
    base = f"job_{run_id}_forecast"
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    occupied = ((base, run_id, "6051"), (f"{base}_retry_2", "cycle_gfs_2026050100_forecast_legacy", "6053"))
    for job_id, row_run_id, slurm_job_id in occupied:
        repository.upsert_pipeline_job(
            {
                "job_id": job_id,
                "run_id": row_run_id,
                "cycle_id": "gfs_2026050100",
                "source_id": "gfs",
                "job_type": "run_shud_forecast_array",
                "slurm_job_id": slurm_job_id,
                "stage": "forecast",
                "status": "failed",
                "error_code": "SLURM_JOB_FAILED",
            }
        )
    basin = {
        "model_id": "model_0",
        "basin_id": "basin_0",
        "basin_version_id": "basin_v0",
        "river_network_version_id": "river_v0",
        "run_id": "fcst_gfs_2026050100_model_0",
        "candidate_id": "gfs:2026-05-01T00:00:00Z:model_0:forecast_gfs_deterministic",
        "orchestration_run_id": run_id,
        "restart_stage": "forecast",
        "model_package_uri": "s3://nhms/models/model_0.tar",
        "model_package_checksum": "sha256:model-0",
        "init_state_id": "state_gfs_model_0_2026050100_gfs_2026043012_f012",
        "init_state_uri": "s3://nhms/states/gfs/model_0/2026050100/state.cfg.ic",
        "init_state_checksum": "sha256:state-0",
        "init_state_valid_time": "2026-05-01T00:00:00Z",
        "state_evidence": {
            "decision": _RETRY_DECISION,
            "restart_stage": "forecast",
            "retry_attempt_floor": {"stage": "forecast", "attempt": 1},
        },
    }
    orchestrator = _orchestrator(tmp_path, repository, FakeCycleSlurmClient(), terminal_stage="forecast")

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    forecast = result.stages[-1]
    assert (forecast.stage, forecast.pipeline_job_id, forecast.status) == ("forecast", f"{base}_retry_3", "succeeded")
    rows = FileOrchestrationJournalRepository(repository.root).query_pipeline_jobs_by_cycle("gfs_2026050100")
    (member,) = [row for row in rows if "_forecast_reconciled_" in str(row["job_id"])]
    assert (member["model_id"], member["retry_count"]) == ("model_0", 3)


# ---------------------------------------------------------------------------
# Row contract through the real journal API (production master payload)
# ---------------------------------------------------------------------------


def _master_record(*, job_suffix: str = "", floors: Any = None) -> dict[str, Any]:
    from services.orchestrator.accepted_submit_identity import forecast_cohort_digest
    from services.orchestrator.reservation import slurm_comment_for
    from tests.gateway_reconcile_helpers import _versioned_master_reservation_record

    record = _versioned_master_reservation_record(member_count=2)
    if job_suffix:
        # The production retry identity (``forecast_cohort_identity_is_valid``).
        record["job_id"] = f"{record['job_id']}{job_suffix}"
        record["idempotency_key"] = f"{record['idempotency_key']}:{job_suffix.lstrip('_')}"
        record["slurm_comment"] = slurm_comment_for(record["idempotency_key"])
    if floors is not None:
        record[RETRY_ATTEMPT_FLOORS_FIELD] = floors
    record["cohort_digest"] = forecast_cohort_digest(record)
    return record


def _projected_charges(tmp_path: Path, record: Mapping[str, Any], *, outcome: str = "succeeded") -> dict[str, int]:
    """Reserve -> accepted commit -> real accepted-submit projection; each member's ``retry_count``."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    assert repository.reserve_pipeline_job(dict(record)) is not None
    commit = repository.commit_pipeline_job_submit_attempt(
        str(record["idempotency_key"]),
        pipeline_job_id=str(record["job_id"]),
        expected_submission_attempt=1,
        slurm_job_id="7100",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    assert commit.committed
    repository.project_forecast_cohort_tasks(
        str(record["job_id"]),
        master_slurm_job_id="7100",
        projections=[
            {
                **{key: member[key] for key in ("candidate_id", "run_id", "model_id", "array_task_id")},
                "array_task_outcome": outcome,
                "task_slurm_job_id": f"7100_{member['array_task_id']}",
                "restart_stage": "forecast",
                "native_shud_resubmitted": False,
                "error_code": None if outcome == "succeeded" else "NODE_FAILURE",
            }
            for member in record["cohort_members"]
        ],
        complete=True,
        master_status=outcome,
        master_error_code=None if outcome == "succeeded" else "NODE_FAILURE",
        reconciliation_decision="matched_bound",
    )
    return {
        str(row["model_id"]): int(row["retry_count"])
        for row in repository.query_pipeline_jobs_by_cycle(str(record["cycle_id"]))
        if "_forecast_reconciled_7100_" in str(row["job_id"])
    }


def test_legacy_master_without_floors_keeps_the_shared_charge(tmp_path: Path) -> None:
    """Pin: a master written before #2542 (no field) charges every member its effective attempt."""

    record = _master_record(job_suffix="_retry_2")
    assert RETRY_ATTEMPT_FLOORS_FIELD not in record

    assert _projected_charges(tmp_path, record) == {"model_0": 2, "model_1": 2}


def test_master_with_floors_charges_each_member_its_own_attempt(tmp_path: Path) -> None:
    record = _master_record(
        job_suffix="_retry_2",
        floors=[{"model_id": "model_1", "attempt": 1}, {"model_id": "model_0", "attempt": 0}],
    )

    assert _projected_charges(tmp_path, record) == {"model_0": 1, "model_1": 2}


def test_master_whose_attempt_is_not_above_its_largest_floor_keeps_the_shared_charge(tmp_path: Path) -> None:
    """Pin: an id reused without the floored minter (eff 2 <= max floor 5) falls back to ``eff``."""

    record = _master_record(
        job_suffix="_retry_2",
        floors=[{"model_id": "model_0", "attempt": 5}, {"model_id": "model_1", "attempt": 0}],
    )

    assert _projected_charges(tmp_path, record) == {"model_0": 2, "model_1": 2}


def test_floors_round_trip_and_are_first_write_frozen(tmp_path: Path) -> None:
    """The closed constructor keeps the list; an ordinary upsert may not rewrite it; reclaim keeps it."""

    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    floors = [{"model_id": "model_0", "attempt": 0}, {"model_id": "model_1", "attempt": 1}]
    record = _master_record(job_suffix="_retry_2", floors=floors)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    assert repository.reserve_pipeline_job(dict(record)) is not None
    reopened = FileOrchestrationJournalRepository(repository.root)
    durable = reopened.get_accepted_submit_pipeline_job(str(record["job_id"]))
    assert durable[RETRY_ATTEMPT_FLOORS_FIELD] == floors

    identity = {key: durable[key] for key in ("job_id", "run_id", "cycle_id", "job_type", "stage", "model_id")}
    # An equivalent replay (same floors, another order) is the same durable value, not a rewrite.
    repository.upsert_pipeline_job({**identity, RETRY_ATTEMPT_FLOORS_FIELD: list(reversed(floors))})
    assert repository.get_accepted_submit_pipeline_job(str(record["job_id"]))[RETRY_ATTEMPT_FLOORS_FIELD] == floors
    with pytest.raises(FileOrchestrationJournalError) as forged:
        repository.upsert_pipeline_job(
            {**identity, RETRY_ATTEMPT_FLOORS_FIELD: [{"model_id": "model_0", "attempt": 9}]}
        )
    assert (forged.value.reason, forged.value.field) == (
        "file_journal_evidence_invariant_invalid",
        RETRY_ATTEMPT_FLOORS_FIELD,
    )

    # Ambiguous submit -> absence permit -> reclaim with a DIFFERENT floor list on the request.
    repository.transition_pipeline_job_submit_evidence(
        str(record["job_id"]),
        AcceptedSubmitTransition.timeout(),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    )
    held = repository.get_accepted_submit_pipeline_job(str(record["job_id"]))
    assert repository.permit_pipeline_job_retry(
        str(record["job_id"]),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_submission_attempt_started_at=held["submission_attempt_started_at"],
    ) == 1
    released = repository.get_accepted_submit_pipeline_job(str(record["job_id"]))
    reclaimed = repository.reclaim_pipeline_job_reservation(
        {
            **record,
            "status": "reserved",
            "expected_submission_attempt": 1,
            "expected_submission_attempt_started_at": released["submission_attempt_started_at"],
            "submission_attempt": 2,
            RETRY_ATTEMPT_FLOORS_FIELD: [{"model_id": "model_0", "attempt": 7}],
        }
    )
    assert reclaimed is not None
    assert reclaimed["submission_attempt"] == 2
    assert repository.get_accepted_submit_pipeline_job(str(record["job_id"]))[RETRY_ATTEMPT_FLOORS_FIELD] == floors


def test_manual_retry_clone_does_not_carry_floors(tmp_path: Path) -> None:
    """The manual-retry pending clone is a fresh row: stale floors never cross the clone boundary."""

    from services.orchestrator.file_orchestration_journal import (
        FileJournalRetryService,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.retry import RetryConfig

    floors = [{"model_id": "model_0", "attempt": 0}, {"model_id": "model_1", "attempt": 1}]
    record = _master_record(job_suffix="_retry_2", floors=floors)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    _projected_charges(tmp_path, record, outcome="failed")
    assert repository.get_pipeline_job(str(record["job_id"]))["status"] == "failed"
    assert repository.get_accepted_submit_pipeline_job(str(record["job_id"]))[RETRY_ATTEMPT_FLOORS_FIELD] == floors

    service = FileJournalRetryService(repository, RetryConfig(max_retries=5, backoff_schedule=[0]))
    # The clone boundary itself (the public entry adds only the gateway submit on top).
    pending = service._create_pending_manual_retry_job(str(record["run_id"]))

    clone = pending.private_snapshot
    assert clone["previous_job_id"] == record["job_id"]
    assert clone["manual_retry_marker"] is True
    assert clone[RETRY_ATTEMPT_FLOORS_FIELD] == []
    durable = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(str(clone["job_id"]))
    assert durable[RETRY_ATTEMPT_FLOORS_FIELD] == []


# ---------------------------------------------------------------------------
# The charge rule and the normaliser (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("floors", "model_id", "eff", "expected"),
    [
        (None, "a", 3, 3),  # absent: shared
        ([], "a", 3, 3),  # empty: shared
        ([{"model_id": "a", "attempt": 0}, {"model_id": "b", "attempt": 1}], "a", 2, 1),
        ([{"model_id": "a", "attempt": 0}, {"model_id": "b", "attempt": 1}], "b", 2, 2),
        ([{"model_id": "a", "attempt": 0}, {"model_id": "b", "attempt": 1}], "a", 3, 2),  # inline retry
        ([{"model_id": "a", "attempt": 0}, {"model_id": "b", "attempt": 1}], "b", 3, 3),
        ([{"model_id": "b", "attempt": 1}], "c", 2, 2),  # unlisted: shared (fail-closed)
        ([{"model_id": "b", "attempt": 1}], "c", 4, 4),  # unlisted + two master skips: shared
        ([{"model_id": "a", "attempt": 1}], "a", 5, 5),  # one-member cohort: always eff
        ([{"model_id": "a", "attempt": 5}], "a", 2, 2),  # eff <= max floor: shared
    ],
)
def test_member_charge_rule(floors: Any, model_id: str, eff: int, expected: int) -> None:
    from services.orchestrator.retry_identity import member_charged_retry_attempt

    assert member_charged_retry_attempt(floors, model_id, eff) == expected


def test_floor_normaliser_is_bounded_sorted_and_deduplicated() -> None:
    from services.orchestrator.accepted_submit_cohort import MAX_FORECAST_COHORT_MEMBERS
    from services.orchestrator.retry_identity import normalize_retry_attempt_floors

    assert normalize_retry_attempt_floors(None) == []
    assert normalize_retry_attempt_floors("model_a") == []
    assert normalize_retry_attempt_floors(
        [
            {"model_id": "b", "attempt": 1},
            {"model_id": " a ", "attempt": 0},
            {"model_id": "b", "attempt": 3},
            {"model_id": "c", "attempt": -1},
            {"model_id": "d", "attempt": True},
            {"model_id": "", "attempt": 2},
            {"attempt": 2},
            "e",
        ]
    ) == [{"model_id": "a", "attempt": 0}, {"model_id": "b", "attempt": 3}]
    many = [{"model_id": f"m{index:04d}", "attempt": 0} for index in range(MAX_FORECAST_COHORT_MEMBERS + 5)]
    assert len(normalize_retry_attempt_floors(many)) == MAX_FORECAST_COHORT_MEMBERS
