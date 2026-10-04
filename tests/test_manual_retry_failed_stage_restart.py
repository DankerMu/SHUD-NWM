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
from services.orchestrator import scheduler_candidates as scheduler_candidates_module
from services.orchestrator.file_orchestration_journal import FileJournalRetryService, FileOrchestrationJournalRepository
from services.orchestrator.retry import RetryConfig
from services.orchestrator.scheduler_state_failure import _forcing_input_failure
from services.orchestrator.scheduler_state_types import CandidateStateDecision
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
_FORCING_INPUT = "manual_retry_forcing_input_failure"
_UPGRADED = "strict_warm_start_retry_run_manifest_mismatch"
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

    evidenced = [
        replace(candidate, state_evidence=dict(decision.evidence))
        for candidate, decision in zip(candidates, decisions, strict=True)
    ]
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
def test_single_model_state_save_qc_marker_restarts_only_state_save_qc(tmp_path: Path, error_code: str | None) -> None:
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


# #2670: ``FORCING_CHECKSUM_READ_FAILED`` is what the runtime reports for a direct-grid
# package whose declared member (``.tsd.forc``, a station CSV) is missing or unreadable.
_FORCING_INPUT_CODES = [
    "FORCING_PACKAGE_CHECKSUM_MISMATCH",
    "FORCING_FILE_NOT_STAGED",
    "SHUD_FORCING_CSV_MISSING",
    "FORCING_CHECKSUM_READ_FAILED",
]


@pytest.mark.parametrize("error_code", _FORCING_INPUT_CODES)
def test_forcing_input_forecast_failure_marker_regenerates_forcing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    """A forecast failure the runtime raised for a bad forcing package keeps the full chain.

    The witness (sidecar + package manifest) exists, so the witness guard would pass and
    a ``forecast`` restart would re-stage the SAME corrupt package; only the full chain
    regenerates it (``workers/forcing_producer/producer.py`` atomic package writes).
    """

    (candidate,) = candidates = [_candidate(0)]
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = _Runtime(
        fail_stage="forecast",
        array_results_by_stage={"forecast": [["failed"], ["succeeded"]]},
        task_error_codes={("forecast", 0): error_code},
    )
    _run_pass(tmp_path, repository, client, _cohort_basins(candidates), max_retries=0)
    _plant_forcing_witness(tmp_path, monkeypatch, candidate)
    _mark(repository, _cohort_run_id("forecast", candidates))

    (decision,) = _decisions(repository, candidates)
    assert (decision.action, decision.reason) == ("retry", "manual_retry_requested")
    assert decision.evidence["failure"]["stage"] == "forecast"
    for key in ("restart_stage", "restart_from_stage", _ADDED, _DROPPED):
        assert key not in decision.evidence, key
    assert decision.evidence[_FORCING_INPUT] is True
    basins, (manifest,) = _pass_two_basins(candidates, [decision])
    assert "restart_stage" not in manifest
    before = len(client.submissions)
    _run_pass(tmp_path, repository, client, basins, max_retries=0)

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


def test_manifest_matching_candidate_construction_keeps_the_state_save_qc_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The added ``state_save_qc`` restart survives the REAL ``_build_candidates``.

    Strict lane, but the run's own ``runs/<run_id>/input/manifest.json`` records the
    initial state the strict lineage selects, so the post-decision upgrade leaves the
    decision alone (``_terminal_decision_run_manifest_matches_strict_warm_start``) and the
    built candidate -- and the chain manifest built from it -- restart at ``state_save_qc``.
    (Warm start not required is no separate lane here: with the ``state_save_qc`` terminal
    the incomplete pipeline still takes the strict evidence, ``scheduler_core`` ~850.)
    The first pass reads the selected state from the real strict evidence, never by hand.
    """

    from tests.test_production_scheduler import _budget_pass, _seed_budget_journal

    root, scheduler = _seed_budget_journal(monkeypatch, tmp_path, _strict_rows("STATE_SAVE_QC_TASK_FAILED"))
    _mark(FileOrchestrationJournalRepository(root), "fcst_gfs_2026052100_model_a")
    _selected, (mismatched,), _blocked, _skipped = _budget_pass(scheduler())
    assert mismatched.state_evidence["reason"] == "strict_warm_start_retry_run_manifest_mismatch"
    selected_state = mismatched.state_evidence["strict_warm_start"]["candidate_state"]
    run_manifest = Path(os.environ["OBJECT_STORE_ROOT"]) / "runs" / mismatched.run_id / "input" / "manifest.json"
    run_manifest.parent.mkdir(parents=True)
    run_manifest.write_text(json.dumps({"initial_state": selected_state}), encoding="utf-8")

    _selected, candidates, blocked, skipped = _budget_pass(scheduler())

    assert (blocked, skipped) == ([], [])
    (candidate,) = candidates
    evidence = candidate.state_evidence
    assert (evidence["decision"], evidence["reason"]) == ("manual_retry", "manual_retry_requested")
    assert evidence[_ADDED] is True
    assert evidence["run_manifest_initial_state"]["state_id"] == selected_state["state_id"]
    assert scheduler_module._candidate_restart_stage(candidate) == "state_save_qc"
    manifest = manifest_module._candidate_basin_manifest(candidate, output_uri=_OUTPUT_URI)
    assert manifest["restart_stage"] == "state_save_qc"
    assert manifest["durable_shud_output_reused"] is True


# --- 4b. the strict lane keeps a forcing-input full-chain manual retry (#2670) --------------


def _strict_forecast_failure_rows(error_code: str) -> list[dict[str, Any]]:
    """The candidate's forecast failed for good with ``error_code``; nothing ran after it."""

    (forecast, _state_save_qc) = _strict_rows(error_code)
    return [{**forecast, "status": "permanently_failed", "error_code": error_code}]


def _strict_forecast_failure_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, error_code: str) -> Any:
    """Production shape of #2670: strict lane, the run manifest never reached the object store.

    The runtime rejects the forcing package before ``_persist_manifest``, so
    ``runs/<run_id>/input/manifest.json`` exists in the workspace only, and the workspace
    root is not the object-store root.  The forcing witness IS present (the package
    exists, its content is bad), so nothing but the decision itself keeps the full chain.
    """

    from tests.test_production_scheduler import _budget_pass, _seed_budget_journal

    root, scheduler = _seed_budget_journal(monkeypatch, tmp_path, _strict_forecast_failure_rows(error_code))
    assert os.environ["NHMS_REQUIRE_FORECAST_WARM_START"] == "true"
    object_root = Path(os.environ["OBJECT_STORE_ROOT"])
    assert Path(os.environ["WORKSPACE_ROOT"]) != object_root
    assert not (object_root / "runs" / "fcst_gfs_2026052100_model_a" / "input" / "manifest.json").exists()
    assert list(object_root.rglob("forcing_version_record.json")), "the forcing witness must be present"
    repository = FileOrchestrationJournalRepository(root)
    _mark(repository, "fcst_gfs_2026052100_model_a")
    _selected, candidates, blocked, skipped = _budget_pass(scheduler())
    assert (blocked, skipped) == ([], [])
    (candidate,) = candidates
    return repository, candidate


def _strict_next_pass_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository: FileOrchestrationJournalRepository, candidate: Any
) -> tuple[dict[str, Any], list[str]]:
    """The chain pass the scheduler would run for this built candidate: (manifest, submitted stages).

    The basin is the REAL candidate manifest under the scheduler's own cohort run id.  The
    copyback root is unset because the Slurm double writes no run tree to copy back; that
    step runs after the forecast and decides nothing about which stages are submitted.
    """

    from tests.test_orchestration_chain import _orchestrator

    stage = scheduler_module._candidate_restart_stage(candidate)
    run_id = scheduler_module._candidate_execution_cohort_run_id_for_candidate(
        "gfs", candidate.cycle_time_utc, scheduler_module._candidate_restart_cohort_key(stage), candidate
    )
    manifest = manifest_module._candidate_basin_manifest(
        candidate, output_uri=f"s3://nhms/runs/{candidate.run_id}/output/", orchestration_run_id=run_id
    )
    monkeypatch.delenv("NHMS_OBJECT_STORE_COPYBACK_ROOT")
    client = _Runtime()
    orchestrator = _orchestrator(
        tmp_path,
        repository,
        client,
        terminal_stage="state_save_qc",
        retry_service=FileJournalRetryService(repository, RetryConfig(max_retries=0, backoff_schedule=[0])),
    )
    orchestrator.orchestrate_cycle("gfs", "2026052100", [dict(manifest)])
    return manifest, _new_stages(client, 0)


@pytest.mark.parametrize("error_code", _FORCING_INPUT_CODES)
def test_strict_lane_keeps_a_forcing_input_manual_retry_full_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    """The strict upgrade must not turn the forcing-input full chain into a ``forecast`` restart.

    Pre-#2670 the upgrade saw no run manifest, rewrote the stage-less retry to
    ``strict_warm_start_retry_run_manifest_mismatch`` / ``forecast``, the witness passed
    (the package exists) and the same bad package was re-staged.
    """

    repository, candidate = _strict_forecast_failure_pass(tmp_path, monkeypatch, error_code=error_code)

    evidence = candidate.state_evidence
    assert (evidence["decision"], evidence["reason"]) == ("manual_retry", "manual_retry_requested")
    assert evidence["reason"] != _UPGRADED
    assert evidence["failure"]["stage"] == "forecast"
    assert evidence[_FORCING_INPUT] is True
    for key in ("restart_stage", "restart_from_stage", _ADDED, _DROPPED):
        assert key not in evidence, key
    assert scheduler_module._candidate_restart_stage(candidate) is None

    manifest, stages = _strict_next_pass_stages(tmp_path, monkeypatch, repository, candidate)
    assert "restart_stage" not in manifest
    assert stages[:3] == ["convert", "forcing", "forecast"]


def test_strict_lane_non_forcing_input_forecast_failure_keeps_its_forecast_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin: a forecast failure that is not a forcing-input failure is not rescued (#2670).

    Its #2600 ``forecast`` restart passes the upgrade and the witness as before, carries
    no forcing-input marker, and the next pass starts at ``forecast``.
    """

    repository, candidate = _strict_forecast_failure_pass(tmp_path, monkeypatch, error_code="SHUD_FAILED")

    evidence = candidate.state_evidence
    assert (evidence["decision"], evidence["reason"]) == ("manual_retry", "manual_retry_requested")
    assert evidence["restart_stage"] == "forecast"
    assert evidence["native_shud_resubmitted"] is True
    assert evidence[_ADDED] is True
    assert _FORCING_INPUT not in evidence

    manifest, stages = _strict_next_pass_stages(tmp_path, monkeypatch, repository, candidate)
    assert manifest["restart_stage"] == "forecast"
    assert stages[0] == "forecast"
    assert "convert" not in stages and "forcing" not in stages


def test_strict_lane_stage_added_manual_retry_carries_no_forcing_input_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin: the #2600 added ``state_save_qc`` restart is still upgraded to ``forecast`` (#2670)."""

    candidates, blocked = _strict_pass(tmp_path, monkeypatch, error_code="STATE_SAVE_QC_TASK_FAILED", witness=True)

    assert blocked == []
    (candidate,) = candidates
    assert candidate.state_evidence["reason"] == _UPGRADED
    assert candidate.state_evidence["restart_stage"] == "forecast"
    assert _FORCING_INPUT not in candidate.state_evidence


_UPGRADED_DECISION = CandidateStateDecision("retry", _UPGRADED, {"restart_stage": "forecast"})


@pytest.mark.parametrize(
    "pre_upgrade",
    [
        None,
        CandidateStateDecision("retry", "manual_retry_requested", {}),
        CandidateStateDecision("retry", "manual_retry_requested", {_FORCING_INPUT: "true"}),
        CandidateStateDecision("retry", "manual_retry_requested", {_FORCING_INPUT: True, "restart_stage": "forecast"}),
        CandidateStateDecision(
            "retry", "manual_retry_requested", {_FORCING_INPUT: True, "restart_from_stage": "download"}
        ),
        CandidateStateDecision("blocked", "permanent_failure_guard", {_FORCING_INPUT: True}),
    ],
    ids=["none", "no_marker", "marker_not_true", "restart_stage", "restart_from_stage", "not_a_retry"],
)
def test_forcing_input_strict_recovery_leaves_every_other_decision_to_the_upgrade(pre_upgrade: Any) -> None:
    """Only a stage-less ``retry`` carrying the marker is restored (#2670 review focus 3)."""

    recovered = scheduler_candidates_module._manual_retry_forcing_input_strict_recovery(pre_upgrade, _UPGRADED_DECISION)

    assert recovered is _UPGRADED_DECISION


def test_forcing_input_strict_recovery_restores_the_marked_stage_less_retry() -> None:
    pre_upgrade = CandidateStateDecision("retry", "manual_retry_requested", {_FORCING_INPUT: True})

    recovered = scheduler_candidates_module._manual_retry_forcing_input_strict_recovery(pre_upgrade, _UPGRADED_DECISION)

    assert recovered is pre_upgrade


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        ("FORCING_CHECKSUM_READ_FAILED", True),
        ("DIRECT_GRID_FORCING_CSV_TOO_LARGE", True),
        ("DIRECT_GRID_TSD_FORC_TOO_LARGE", False),
        ("DIRECT_GRID_TSD_FORC_TOO_MANY_LINES", False),
        ("DIRECT_GRID_TSD_FORC_LINE_TOO_LONG", False),
        ("SHUD_FAILED", False),
    ],
)
def test_forcing_input_matcher_covers_what_its_docstring_names(error_code: str, expected: bool) -> None:
    """The missing-member code matches; the real ``.tsd.forc`` size limits do not (#2670)."""

    assert _forcing_input_failure({"error_code": error_code}) is expected


@pytest.mark.parametrize("strict", [False, True], ids=["none_lane", "strict_lane"])
def test_automatic_lane_never_retries_a_forcing_input_forecast_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    """Audit pin (#2670): without a marker the failure is permanent on both lanes.

    No forcing-input code is transient (``retry.TRANSIENT_ERROR_CODES``), so the ladder
    stops at the permanent-failure guard before ``retry_failed_candidate`` could pick its
    ``forecast`` restart: the automatic lane emits no retry for the upgrade to rewrite.
    """

    error_code = "FORCING_PACKAGE_CHECKSUM_MISMATCH"
    if strict:
        from tests.test_production_scheduler import _budget_pass, _seed_budget_journal

        _root, scheduler = _seed_budget_journal(monkeypatch, tmp_path, _strict_forecast_failure_rows(error_code))
        _selected, candidates, blocked, _skipped = _budget_pass(scheduler())
        assert candidates == []
        (item,) = blocked
        evidence = item.state_evidence
    else:
        candidates = [_candidate(0)]
        repository = FileOrchestrationJournalRepository(tmp_path / "journal")
        client = _Runtime(
            fail_stage="forecast",
            array_results_by_stage={"forecast": [["failed"]]},
            task_error_codes={("forecast", 0): error_code},
        )
        _run_pass(tmp_path, repository, client, _cohort_basins(candidates), max_retries=0)
        _plant_forcing_witness(tmp_path, monkeypatch, candidates[0])
        (decision,) = _decisions(repository, candidates)
        assert decision.action == "blocked"
        evidence = decision.evidence

    assert evidence["reason"] == "permanent_failure_guard"
    assert evidence["failure"]["reason_code"] == error_code
    assert evidence["retry_policy"]["automatic_retry_allowed"] is False
    assert evidence["retry_policy"]["manual_retry_required"] is True
    assert "restart_stage" not in evidence


# --- 5. after the restart: a second failure and an in-flight restart ------------------------


def _restart_pass_two(
    tmp_path: Path, member_count: int, *, error_code: str | None
) -> tuple[Any, Any, list[Any], list[Any]]:
    repository, client, candidates, master = _state_save_failure_pass(tmp_path, member_count, error_code=error_code)
    _mark(repository, master)
    decisions = _decisions(repository, candidates)
    assert {decision.evidence.get("restart_stage") for decision in decisions} == {"state_save_qc"}
    basins, _manifests = _pass_two_basins(candidates, decisions)
    return repository, client, candidates, basins


@pytest.mark.parametrize(
    ("member_count", "error_code"),
    [(1, None), (1, "STATE_SAVE_QC_TASK_FAILED"), (3, None)],
    ids=["single_transient", "single_non_transient", "cohort_transient"],
)
def test_restarted_state_save_qc_that_fails_again_is_not_a_second_manual_retry(
    tmp_path: Path, member_count: int, error_code: str | None
) -> None:
    """The consumed marker does not re-arm: the new failure meets the ordinary guards."""

    repository, client, candidates, basins = _restart_pass_two(tmp_path, member_count, error_code=error_code)
    client.failures_before_success_by_stage["state_save_qc"] = 99
    client.array_results_by_stage["state_save_qc"] = [["failed"] * member_count]
    if error_code:
        client.task_error_codes[("state_save_qc", 1)] = error_code
    before = len(client.submissions)
    _run_pass(tmp_path, repository, client, basins, max_retries=0)
    assert _new_stages(client, before) == ["state_save_qc"]

    for decision in _decisions(repository, candidates):
        assert (decision.action, decision.reason) == ("blocked", "permanent_failure_guard")
        assert decision.evidence["retry_policy"]["manual_retry_required"] is True
        assert decision.evidence["retry_policy"].get("manual_retry_marker") is not True
        for key in ("restart_stage", _ADDED):
            assert key not in decision.evidence, key


class _PassInterrupted(BaseException):
    """Ends the pass while the restart row is running: the next pass reads that journal."""


def test_running_model_less_restart_row_makes_the_next_pass_skip_active(tmp_path: Path) -> None:
    repository, client, candidates, basins = _restart_pass_two(tmp_path, 3, error_code=None)
    poll = client.get_job_status

    def interrupt_while_running(job_id: str) -> dict[str, Any]:
        status = poll(job_id)
        if status["stage"] == "state_save_qc" and client.poll_counts[job_id] >= 2:
            raise _PassInterrupted
        return status

    client.get_job_status = interrupt_while_running
    before = len(client.submissions)
    with pytest.raises(_PassInterrupted):
        _run_pass(tmp_path, repository, client, basins, max_retries=0)
    assert _new_stages(client, before) == ["state_save_qc"]
    running = [row for row in repository.query_pipeline_jobs_by_cycle(_CYCLE_ID) if row.get("status") == "running"]
    assert [(row.get("stage"), row.get("model_id")) for row in running] == [("state_save_qc", None)]

    for decision in _decisions(repository, candidates):
        assert (decision.action, decision.reason) == ("skip", "active_slurm_job")
        assert decision.evidence["decision"] == "skip_active"
        assert decision.evidence["replacement_submitted"] is False
