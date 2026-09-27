"""A manual-retry marker restarts a candidate at its failed stage (#2600).

Production incident (node-22, 2026-09-23): ``basins_hlj`` IFS 2026092212 failed only its
``state_save_qc``; the operator marked the model-less cohort master
``cycle_ifs_2026092212_convert_dg_...`` (``record_manual_repair``) and the next pass reran
convert -> forcing -> forecast -> state_save_qc (19 minutes for a 9-second stage).

Requirement (``openspec/changes/retry-blocked-reads-failed-stage-restart``,
production-scheduler-orchestration): the marker restarts at a failed ``parse`` /
``state_save_qc`` / ``publish`` when the candidate's own forecast output is provably
durable, at a failed ``forecast`` only with the forcing witness, and otherwise reruns the
full chain; a restart stage it adds that a guard refuses is dropped (full chain), never
turned into a blocker.

Seams: the real ``orchestrate_cycle`` stage loop over a real
``FileOrchestrationJournalRepository`` (``tests/test_cohort_membership_attribution.py``
harness), the real ``candidate_state`` read and ``_candidate_state_decision``, the real
``_candidate_basin_manifest`` (whose top-level ``restart_stage`` is the chain's only
restart source), and the fake Slurm runtime's submitted stage sequence.  The strict
warm-start lane runs the REAL scheduler candidate construction
(``tests/test_production_scheduler._seed_budget_journal``).  Only Slurm is a double.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore
from services.orchestrator import scheduler as scheduler_module
from services.orchestrator import scheduler_candidate_manifest as manifest_module
from services.orchestrator.file_orchestration_journal import FileJournalRetryService, FileOrchestrationJournalRepository
from tests.test_cohort_membership_attribution import (
    _CYCLE,
    _CYCLE_ID,
    _candidate,
    _cohort_basins,
    _cohort_run_id,
    _decision,
    _members_with_blank_model,
    _run_pass,
    _Runtime,
    _state_save_qc_terminal,  # noqa: F401 -- autouse: the production terminal stage
)

_OUTPUT_URI = "s3://nhms/runs/out.nc"
_ADDED = "manual_retry_restart_stage_added"
_DROPPED = "manual_retry_restart_stage_dropped"
_RESTART_KEYS = ("restart_stage", "restart_from_stage", "durable_shud_output_reused", _ADDED, _DROPPED)


def _mark(repository: FileOrchestrationJournalRepository, run_id: str) -> None:
    """The operator channel of the incident: ``node22_manual_retry_failed_runs.py --execute``."""

    FileJournalRetryService(repository).record_manual_repair(run_id, trusted_internal=True)


def _decisions(repository: FileOrchestrationJournalRepository, candidates: list[Any]) -> list[Any]:
    return [_decision(repository, candidate) for candidate in candidates]


def _pass_two_basins(candidates: list[Any], decisions: list[Any]) -> tuple[list[dict[str, Any]], list[Any]]:
    """What the scheduler hands the chain for these decisions.

    The restart cohort comes from the scheduler's own restart-stage reader, and every
    member's top-level ``restart_stage`` / attempt fields come from the REAL manifest
    builder -- never hand-written -- so a later rewrite of the decision would show here.
    """

    evidenced = [replace(candidate, state_evidence=dict(decision.evidence)) for candidate, decision in zip(
        candidates, decisions, strict=True
    )]
    stages = {scheduler_module._candidate_restart_stage(candidate) for candidate in evidenced}
    assert len(stages) == 1, f"the members must form ONE restart cohort: {stages}"
    (stage,) = stages
    run_id = _cohort_run_id(stage, evidenced)
    manifests = [
        manifest_module._candidate_basin_manifest(candidate, output_uri=_OUTPUT_URI, orchestration_run_id=run_id)
        for candidate in evidenced
    ]
    basins = _cohort_basins(candidates, restart_stage=stage or "forecast")
    for basin, manifest in zip(basins, manifests, strict=True):
        basin.pop("restart_stage")
        basin["orchestration_run_id"] = run_id
        basin["state_evidence"] = manifest["state_evidence"]
        for key in ("restart_stage", "manual_retry_attempt", "retry_attempt", "durable_shud_output_reused"):
            if key in manifest:
                basin[key] = manifest[key]
    return basins, manifests


def _new_stages(client: Any, before: int) -> list[str]:
    return [str(submission.get("stage")) for submission in client.submissions[before:]]


def _state_save_failure_pass(tmp_path: Path, member_count: int, *, error_code: str | None = None) -> Any:
    """Pass 1: every member's forecast succeeds, the cohort's state_save_qc fails for good.

    ``error_code=None``: a transient failure exhausts ``max_retries=1`` (``permanently_failed``
    after two attempts); a non-transient task code is ``permanently_failed`` at once.  The
    third state_save_qc attempt (pass 2) succeeds.
    """

    candidates = [_candidate(index) for index in range(member_count)]
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = _Runtime(
        failures_before_success_by_stage={"state_save_qc": 1 if error_code else 2},
        array_results_by_stage={
            "state_save_qc": (
                [["failed"] * member_count, ["succeeded"] * member_count]
                if error_code
                else [["failed"] * member_count, ["failed"] * member_count, ["succeeded"] * member_count]
            )
        },
        task_error_codes={("state_save_qc", 0): error_code} if error_code else None,
    )
    _run_pass(tmp_path, repository, client, _cohort_basins(candidates), max_retries=1)
    master = _cohort_run_id("forecast", candidates)
    failed = [
        row
        for row in repository.query_pipeline_jobs_by_run(master)
        if row.get("stage") == "state_save_qc" and row.get("status") == "permanently_failed"
    ]
    assert failed, "pass 1 must leave the cohort's state_save_qc permanently failed"
    return repository, client, candidates, master


# --- 1. the incident: state_save_qc-only failure -------------------------------------------


@pytest.mark.parametrize(
    "error_code",
    [None, "STATE_SAVE_QC_TASK_FAILED"],
    ids=["transient_retries_exhausted", "non_transient_permanent"],
)
def test_single_model_state_save_qc_marker_restarts_only_state_save_qc(
    tmp_path: Path, error_code: str | None
) -> None:
    repository, client, candidates, master = _state_save_failure_pass(tmp_path, 1, error_code=error_code)
    # The failure is permanent either way: the marker is the operator's authority.
    (blocked,) = _decisions(repository, candidates)
    assert (blocked.action, blocked.reason) == ("blocked", "permanent_failure_guard")
    _mark(repository, master)

    (decision,) = _decisions(repository, candidates)
    assert (decision.action, decision.reason) == ("retry", "manual_retry_requested")
    assert decision.evidence["restart_stage"] == "state_save_qc"
    assert decision.evidence["native_shud_resubmitted"] is False
    assert decision.evidence["durable_shud_output_reused"] is True
    # Attempt accounting is the marker's, unchanged (pinned against origin/master 626f53985).
    previous = 1 if error_code is None else 0
    assert decision.evidence["manual_retry"]["previous_attempt"] == previous
    assert decision.evidence["manual_retry"]["new_attempt"] == previous + 1
    assert decision.evidence["retry_policy"]["attempt"] == previous + 1

    basins, (manifest,) = _pass_two_basins(candidates, [decision])
    assert manifest["restart_stage"] == "state_save_qc"
    assert manifest["manual_retry_attempt"] == previous + 1
    before = len(client.submissions)
    result = _run_pass(tmp_path, repository, client, basins, max_retries=0)

    assert _new_stages(client, before) == ["state_save_qc"]
    assert [(stage.stage, stage.status) for stage in result.stages] == [("state_save_qc", "succeeded")]
    assert _decision(repository, candidates[0]).action == "skip"


def test_marker_on_the_model_less_cohort_master_restarts_one_state_save_qc_cohort(tmp_path: Path) -> None:
    """The incident shape at cohort size: no member's forecast is recomputed."""

    repository, client, candidates, master = _state_save_failure_pass(tmp_path, 3)
    assert master.startswith("cycle_gfs_") and "_cohort_" in master
    assert all(row.get("model_id") in (None, "") for row in repository.query_pipeline_jobs_by_run(master))
    _mark(repository, master)

    decisions = _decisions(repository, candidates)
    assert {(decision.reason, decision.evidence.get("restart_stage")) for decision in decisions} == {
        ("manual_retry_requested", "state_save_qc")
    }
    basins, manifests = _pass_two_basins(candidates, decisions)
    assert [manifest["restart_stage"] for manifest in manifests] == ["state_save_qc"] * 3
    assert len({basin["orchestration_run_id"] for basin in basins}) == 1
    forecasts_before = client.stage_submissions("forecast")
    before = len(client.submissions)
    _run_pass(tmp_path, repository, client, basins, max_retries=0)

    assert _new_stages(client, before) == ["state_save_qc"]
    assert client.stage_submissions("state_save_qc")[-1] == [candidate.model_id for candidate in candidates]
    assert client.stage_submissions("forecast") == forecasts_before
    assert {_decision(repository, candidate).action for candidate in candidates} == {"skip"}


# --- 2. forecast failure: only with the forcing witness ------------------------------------


def _plant_forcing_witness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate: Any) -> None:
    """The producer's sidecar + package manifest for this candidate, in the chain's own store."""

    object_root = tmp_path / "object-store"
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    store = LocalObjectStore(object_root, "s3://nhms")
    package_dir = f"forcing/gfs/{_CYCLE}/{candidate.basin_version_id}/{candidate.model_id}"
    store.write_bytes_atomic(f"{package_dir}/forcing_package.json", b'{"schema_version": "nhms.forcing_package.v1"}')
    store.write_bytes_atomic(
        f"{package_dir}/forcing_version_record.json",
        json.dumps(
            {"forcing_package_uri": f"s3://nhms/{package_dir}/", "forcing_version_id": candidate.forcing_version_id}
        ).encode("utf-8"),
    )


@pytest.mark.parametrize("witness", [True, False], ids=["witness_found", "witness_missing"])
def test_forecast_failure_marker_restarts_at_forecast_only_with_the_forcing_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, witness: bool
) -> None:
    (candidate,) = candidates = [_candidate(0)]
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = _Runtime(fail_stage="forecast", array_results_by_stage={"forecast": [["failed"], ["succeeded"]]})
    _run_pass(tmp_path, repository, client, _cohort_basins(candidates), max_retries=0)
    if witness:
        _plant_forcing_witness(tmp_path, monkeypatch, candidate)
    else:
        monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "object-store"))
    _mark(repository, _cohort_run_id("forecast", candidates))

    (decision,) = _decisions(repository, candidates)
    # Never a blocker: the marker's decision is a retry either way.
    assert (decision.action, decision.reason) == ("retry", "manual_retry_requested")
    basins, (manifest,) = _pass_two_basins(candidates, [decision])
    before = len(client.submissions)
    _run_pass(tmp_path, repository, client, basins, max_retries=0)

    if witness:
        assert decision.evidence["restart_stage"] == "forecast"
        assert decision.evidence["native_shud_resubmitted"] is True
        assert decision.evidence["forcing_provenance"]["source"] == "object_store_sidecar"
        assert manifest["restart_stage"] == "forecast"
        assert _new_stages(client, before)[0] == "forecast"
        assert "convert" not in _new_stages(client, before)
    else:
        for key in ("restart_stage", "restart_from_stage", _ADDED):
            assert key not in decision.evidence, key
        assert decision.evidence[_DROPPED] == {"restart_stage": "forecast", "reason": "forcing_version_row_absent"}
        assert "restart_stage" not in manifest
        assert _new_stages(client, before)[:3] == ["convert", "forcing", "forecast"]


# --- 3. fail-closed pins: no provable own output, convert/forcing, cold start ---------------


def _seed_failed_downstream(
    tmp_path: Path, *, forecast_rows: list[dict[str, Any]], failed_row: dict[str, Any]
) -> tuple[FileOrchestrationJournalRepository, Any]:
    candidate = _candidate(0)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=candidate.cycle_time_utc)
    for row in [*forecast_rows, failed_row]:
        repository.upsert_pipeline_job({"cycle_id": _CYCLE_ID, "retry_count": 0, **row})
    return repository, candidate


def _assert_full_chain(decision: Any) -> None:
    assert (decision.action, decision.reason) == ("retry", "manual_retry_requested")
    for key in _RESTART_KEYS:
        assert key not in decision.evidence, key
    candidate_manifest = manifest_module._candidate_basin_manifest(
        replace(_candidate(0), state_evidence=dict(decision.evidence)), output_uri=_OUTPUT_URI
    )
    assert "restart_stage" not in candidate_manifest


_STATE_SAVE_FAILED = {
    "job_id": "job_cycle_gfs_2026050100_forecast_cohort_000000000000_state_save_qc",
    "run_id": "cycle_gfs_2026050100_forecast_cohort_000000000000",
    "job_type": "save_state_snapshot_array",
    "stage": "state_save_qc",
    "model_id": None,
    "status": "permanently_failed",
    "slurm_job_id": "950",
    "error_code": "STATE_SAVE_QC_TASK_FAILED",
    "finished_at": "2026-05-01T02:00:00Z",
}


_OWN_STATE_SAVE_FAILED = {
    **_STATE_SAVE_FAILED,
    "job_id": "job_fcst_gfs_2026050100_model_0_state_save_qc",
    "run_id": "fcst_gfs_2026050100_model_0",
    "job_type": "save_state_snapshot",
    "model_id": "model_0",
}


def test_only_an_incomplete_membership_forecast_success_runs_the_full_chain(tmp_path: Path) -> None:
    """Pin: the only forecast success is a cohort row whose recorded membership is ``incomplete``.

    The failure is the candidate's own (its per-model state_save_qc row), so the marker's
    decision is reached; the forecast success it would restart from cannot be attributed.
    """

    members = _members_with_blank_model([_candidate(index) for index in range(3)])
    forecast_master = {
        "job_id": "job_cycle_gfs_2026050100_forecast_cohort_000000000000_forecast",
        "run_id": _STATE_SAVE_FAILED["run_id"],
        "job_type": "run_shud_forecast_array",
        "stage": "forecast",
        "model_id": None,
        "status": "succeeded",
        "slurm_job_id": "940",
        "finished_at": "2026-05-01T01:00:00Z",
        "cohort_members": members,
    }
    repository, candidate = _seed_failed_downstream(
        tmp_path, forecast_rows=[forecast_master], failed_row=_OWN_STATE_SAVE_FAILED
    )
    _mark(repository, "fcst_gfs_2026050100_model_0")

    state = FileOrchestrationJournalRepository(repository.root).candidate_state(
        source_id="gfs",
        cycle_time=candidate.cycle_time_utc,
        model_id=candidate.model_id,
        run_id=candidate.run_id,
        forcing_version_id=candidate.forcing_version_id,
        candidate_id=candidate.candidate_id,
        retry_limit=1,
    )
    assert state is not None
    assert {job.get("cohort_membership") for job in state["pipeline_jobs"] if job.get("stage") == "forecast"} == {
        "incomplete"
    }
    _assert_full_chain(_decision(repository, candidate))


def test_downstream_failure_without_own_forecast_output_runs_the_full_chain(tmp_path: Path) -> None:
    """Pin: a failed state_save_qc naming the model, but no forecast success and no hydro success."""

    repository, candidate = _seed_failed_downstream(tmp_path, forecast_rows=[], failed_row=_OWN_STATE_SAVE_FAILED)
    _mark(repository, "fcst_gfs_2026050100_model_0")

    _assert_full_chain(_decision(repository, candidate))


@pytest.mark.parametrize("stage", ["convert", "forcing"])
def test_convert_or_forcing_failure_marker_is_unchanged(tmp_path: Path, stage: str) -> None:
    """Pin: an upstream failure keeps the full chain, exactly as before."""

    failed = {
        **_STATE_SAVE_FAILED,
        "job_id": f"job_fcst_gfs_2026050100_model_0_{stage}",
        "run_id": "fcst_gfs_2026050100_model_0",
        "job_type": "convert_canonical" if stage == "convert" else "produce_forcing_array",
        "stage": stage,
        "model_id": "model_0",
        "error_code": "SLURM_JOB_FAILED",
    }
    repository, candidate = _seed_failed_downstream(tmp_path, forecast_rows=[], failed_row=failed)
    _mark(repository, "fcst_gfs_2026050100_model_0")

    decision = _decision(repository, candidate)
    assert decision.evidence["failure"]["stage"] == stage
    _assert_full_chain(decision)


def test_cold_start_quarantined_marker_keeps_its_forced_forecast_restart(tmp_path: Path) -> None:
    """Pin: the quarantine's forced native forecast rerun, with no added-stage marker."""

    own_forecast = {
        "job_id": "job_fcst_gfs_2026050100_model_0_forecast",
        "run_id": "fcst_gfs_2026050100_model_0",
        "job_type": "run_shud_forecast",
        "stage": "forecast",
        "model_id": "model_0",
        "status": "succeeded",
        "slurm_job_id": "900",
        "finished_at": "2026-05-01T01:00:00Z",
    }
    failed = {**_OWN_STATE_SAVE_FAILED, "error_code": "COLD_START_QUARANTINED"}
    repository, candidate = _seed_failed_downstream(tmp_path, forecast_rows=[own_forecast], failed_row=failed)
    _mark(repository, "fcst_gfs_2026050100_model_0")

    decision = _decision(repository, candidate)
    assert (decision.action, decision.reason) == ("retry", "manual_retry_requested")
    assert decision.evidence["restart_stage"] == "forecast"
    assert decision.evidence["native_shud_resubmitted"] is True
    assert decision.evidence["force_native_shud_rerun"] is True
    assert _ADDED not in decision.evidence
    assert _DROPPED not in decision.evidence


# --- 4. the strict warm-start lane (real scheduler candidate construction) ------------------


def _strict_rows(error_code: str) -> list[dict[str, Any]]:
    base = {"run_id": "fcst_gfs_2026052100_model_a", "cycle_id": "gfs_2026052100", "model_id": "model_a"}
    return [
        {
            **base,
            "job_id": "job_fcst_gfs_2026052100_model_a_forecast",
            "stage": "forecast",
            "job_type": "run_shud_forecast_array",
            "status": "succeeded",
            "slurm_job_id": "700",
            "retry_count": 0,
            "finished_at": "2026-05-21T01:00:00Z",
        },
        {
            **base,
            "job_id": "job_fcst_gfs_2026052100_model_a_state_save_qc",
            "stage": "state_save_qc",
            "job_type": "save_state_snapshot",
            "status": "permanently_failed",
            "slurm_job_id": "701",
            "retry_count": 0,
            "error_code": error_code,
            "finished_at": "2026-05-21T02:00:00Z",
        },
    ]


def _strict_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, error_code: str, witness: bool) -> Any:
    from tests.test_production_scheduler import _budget_pass, _seed_budget_journal

    root, scheduler = _seed_budget_journal(monkeypatch, tmp_path, _strict_rows(error_code))
    assert os.environ["NHMS_REQUIRE_FORECAST_WARM_START"] == "true"
    if not witness:
        for path in Path(os.environ["OBJECT_STORE_ROOT"]).rglob("forcing_*.json"):
            path.unlink()
    _mark(FileOrchestrationJournalRepository(root), "fcst_gfs_2026052100_model_a")
    _selected, candidates, blocked, _skipped = _budget_pass(scheduler())
    return candidates, blocked


@pytest.mark.parametrize("witness", [True, False], ids=["witness_found", "witness_missing"])
def test_strict_lane_never_blocks_a_manual_restart_stage_it_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, witness: bool
) -> None:
    """The strict upgrade rewrites the added ``state_save_qc`` restart to ``forecast``.

    With the witness that forecast restart stands; without it the witness consultation
    would block, so the manual retry falls back to the full chain instead.
    ``witness_found`` is a pin: origin reaches the same forecast restart by upgrading the
    full-chain manual retry; ``witness_missing`` is the behavior change (origin: blocked).
    """

    candidates, blocked = _strict_pass(tmp_path, monkeypatch, error_code="STATE_SAVE_QC_TASK_FAILED", witness=witness)

    assert blocked == []
    (candidate,) = candidates
    evidence = candidate.state_evidence
    if witness:
        assert evidence["reason"] == "strict_warm_start_retry_run_manifest_mismatch"
        assert evidence["restart_stage"] == "forecast"
        assert evidence["manual_retry"]["marker"] is True
    else:
        assert (evidence["decision"], evidence["reason"]) == ("manual_retry", "manual_retry_requested")
        assert "restart_stage" not in evidence
        assert evidence[_DROPPED] == {"restart_stage": "state_save_qc", "reason": "forcing_version_row_absent"}
        assert scheduler_module._candidate_restart_stage(candidate) is None


def test_strict_lane_cold_start_manual_retry_keeps_its_blocker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin: the pre-existing forced ``forecast`` of a cold-start quarantine is not rescued."""

    candidates, blocked = _strict_pass(tmp_path, monkeypatch, error_code="COLD_START_QUARANTINED", witness=False)

    assert candidates == []
    (item,) = blocked
    assert item.state_evidence["reason"] == "forcing_version_row_absent"
    assert _DROPPED not in item.state_evidence
