"""Cross-stage state residue in the forecast chain (batch C: #2416, #2394, #2393, #1845).

Governing invariant (``openspec/changes/chain-cross-stage-state-residue``):
every stage decision in one ``orchestrate_cycle`` call is derived from that
stage's own rows, that model's own identity, and the manifest's top-level
fields -- never from another stage's reservation, another model's row, or a
marker the manifest deliberately stripped.

Seams: ``ForecastOrchestrator.orchestrate_cycle`` (public), the scheduler's
``_candidate_basin_manifest`` -> chain ``_restart_stage_from_basins`` hop, and
``_candidate_scoped_cycle_execution`` (the restart stage's second consumer).
Only Slurm is faked.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import chain_runtime_utils
from services.orchestrator import scheduler_candidate_manifest as manifest_module
from tests.test_orchestration_chain import (
    FakeCycleRepository,
    FakeCycleSlurmClient,
    _basins,
    _orchestrator,
)

_OUTPUT_URI = "s3://nhms/runs/out.nc"
_FRESH_FULL_CHAIN = {"required": True, "mode": "full_chain"}


def _manifest(state_evidence: dict[str, Any], **extra: Any) -> dict[str, Any]:
    # Function-local on purpose: a module-scope import would grow the frozen
    # direct-importer anchor of test_production_scheduler.py (test_select_ci_tests).
    from tests.test_production_scheduler import _scheduler_candidate_fixture

    candidate = replace(_scheduler_candidate_fixture(), state_evidence=state_evidence)
    return manifest_module._candidate_basin_manifest(candidate, output_uri=_OUTPUT_URI, **extra)


def _fresh_full_chain_manifest_with_residual_marker() -> dict[str, Any]:
    return _manifest({"restart_stage": "forecast", "fresh_ingestion": dict(_FRESH_FULL_CHAIN)})


# --- #2416: the manifest's top-level field is the single restart source ------


def test_fresh_full_chain_manifest_residual_marker_does_not_reach_the_chain() -> None:
    """#2416 table A reversed: the stripped marker is not read back from evidence."""

    manifest = _fresh_full_chain_manifest_with_residual_marker()

    assert "restart_stage" not in manifest
    # The evidence still travels verbatim (audit); only its restart marker is ignored.
    assert manifest["state_evidence"]["restart_stage"] == "forecast"
    assert chain_runtime_utils._restart_stage_from_basins([manifest]) is None


def test_full_cohort_member_with_residual_marker_does_not_skip_convert_or_forcing(tmp_path: Path) -> None:
    """#2416 table B reversed: a ``(0,"full")`` cohort starts at convert for every member."""

    fresh_manifest = _fresh_full_chain_manifest_with_residual_marker()
    basins = _basins(3)
    # Member 0 carries exactly what the manifest builder hands the chain for a
    # fresh full-chain candidate: evidence with the residual marker and no
    # top-level ``restart_stage``. Members 1 and 2 are markerless.
    basins[0]["state_evidence"] = fresh_manifest["state_evidence"]
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, FakeCycleRepository(), client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", basins)

    assert result.status == "complete"
    assert client.submissions[0]["stage"] == "convert"
    submitted_stages = [submission["stage"] for submission in client.submissions]
    assert submitted_stages[:3] == ["convert", "forcing", "forecast"]


@pytest.mark.parametrize("evidence_key", ["restart_stage", "restart_from_stage"])
def test_ordinary_restart_candidate_keeps_its_restart_through_the_manifest(
    tmp_path: Path, evidence_key: str
) -> None:
    """Must-preserve (3.3): a non-fresh candidate's restart reaches the chain via the top-level key."""

    manifest = _manifest({"decision": "retry_failed", evidence_key: "forecast"})

    assert manifest["restart_stage"] == "forecast"
    assert chain_runtime_utils._restart_stage_from_basins([manifest]) == "forecast"

    basin = _basins(1)[0]
    basin["restart_stage"] = manifest["restart_stage"]
    basin["state_evidence"] = manifest["state_evidence"]
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, FakeCycleRepository(), client)

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", [basin])

    assert result.status == "complete"
    submitted_stages = [submission["stage"] for submission in client.submissions]
    assert submitted_stages[0] == "forecast"
    assert "convert" not in submitted_stages
    assert "forcing" not in submitted_stages


def test_manifest_restart_stage_keeps_raw_value_and_chain_canonicalizes_it() -> None:
    """The manifest writes the raw evidence value; the chain canonicalizes on read."""

    manifest = _manifest({"decision": "retry_failed", "restart_from_stage": "parse_output"})

    assert manifest["restart_stage"] == "parse_output"
    assert chain_runtime_utils._restart_stage_from_basins([manifest]) == "parse"


def test_raw_repair_downstream_emitter_marker_stays_off_the_manifest_top_level() -> None:
    """Census pin (3.4): ``retry_downstream_after_raw_repair`` writes only
    ``restart_from_stage: "download"`` (a retired stage) and is NOT fresh full-chain.

    Its top-level ``restart_stage`` must stay absent: raw readers of that key
    (``chain_array_accounting`` forecast projections, ``reconcile``) would
    otherwise project ``"download"``, which the accepted-submit evidence
    validator rejects (``ACCEPTED_RESTART_STAGES``). The chain starts at stage 0
    for it either way.
    """

    manifest = _manifest(
        {
            "decision": "retry_failed",
            "reason": "retry_downstream_after_raw_repair",
            "restart_stage": None,
            "restart_from_stage": "download",
            "fresh_ingestion": {"required": False, "mode": "reuse_repaired_raw_then_full_chain"},
        }
    )

    assert "restart_stage" not in manifest
    assert chain_runtime_utils._restart_stage_from_basins([manifest]) is None


def test_restart_stage_from_basins_takes_earliest_marker_and_ignores_markerless_members() -> None:
    """3.6 ``min`` semantics: earliest claim among marker-carrying members."""

    basins = [{"restart_stage": "parse"}, {}, {"restart_stage": "forecast"}]

    assert chain_runtime_utils._restart_stage_from_basins(basins) == "forecast"


# --- #2416 3.5: the second consumer, _candidate_scoped_cycle_execution --------


def test_candidate_scope_of_single_fresh_full_chain_basin_without_run_id_no_longer_narrows() -> None:
    """3.5(a) FLIPS True -> False: the stripped marker no longer narrows scope.

    Accepted: without the marker the basin is exactly a markerless fresh
    candidate, which was never candidate-scoped. Production basins always carry
    ``orchestration_run_id`` (3.5(c)), so production scope is unchanged.
    """

    manifest = _fresh_full_chain_manifest_with_residual_marker()
    assert "orchestration_run_id" not in manifest

    assert chain_runtime_utils._candidate_scoped_cycle_execution([manifest]) is False


def test_candidate_scope_of_single_ordinary_restart_basin_is_unchanged() -> None:
    """3.5(b) does not flip: the top-level key keeps the single basin scoped."""

    manifest = _manifest({"decision": "retry_failed", "restart_from_stage": "forecast"})
    assert "orchestration_run_id" not in manifest

    assert chain_runtime_utils._candidate_scoped_cycle_execution([manifest]) is True


def test_candidate_scope_with_orchestration_run_id_is_unchanged() -> None:
    """3.5(c) does not flip: a run-id-stamped basin is scoped regardless of restart."""

    fresh = _fresh_full_chain_manifest_with_residual_marker()
    fresh["orchestration_run_id"] = "cycle_gfs_2026052106_full_cohort_abc"
    plain = _manifest({})
    plain["orchestration_run_id"] = "cycle_gfs_2026052106_full_cohort_abc"

    assert chain_runtime_utils._candidate_scoped_cycle_execution([fresh]) is True
    assert chain_runtime_utils._candidate_scoped_cycle_execution([fresh, plain]) is True


# --- #2393: a stage's reservation attempt never leaks into the next stage -----
#
# ``context.retry_attempt`` is the invocation's claim (operator/API field or an
# active manual marker; markerless -> ``None``). A stage's reservation writes its
# own attempt back for the in-stage consumers (manifests, ambiguity release);
# the stage loop restores the claim before the next stage is entered.

_CYCLE = "2026050100"
_CYCLE_ID = "gfs_2026050100"
_COHORT_RUN_ID = f"cycle_gfs_{_CYCLE}_cohort_fixture"


def _stage_result(result: Any, stage: str) -> Any:
    return next(item for item in result.stages if item.stage == stage)


def _submitted_stages(client: FakeCycleSlurmClient) -> list[str]:
    return [submission["stage"] for submission in client.submissions]


def _journal_basins(*, restart_stage: str, count: int = 1, **evidence: Any) -> list[dict[str, Any]]:
    basins = _basins(count)
    for index, basin in enumerate(basins):
        basin.update(
            {
                "run_id": f"fcst_gfs_{_CYCLE}_model_{index}",
                "candidate_id": f"gfs:2026-05-01T00:00:00Z:model_{index}:forecast_gfs_deterministic",
                "orchestration_run_id": _COHORT_RUN_ID,
                "restart_stage": restart_stage,
                "state_evidence": {"restart_stage": restart_stage, **evidence},
                "model_package_uri": f"s3://nhms/models/model_{index}.tar",
                "model_package_checksum": f"sha256:model-{index}",
                "init_state_id": f"state_gfs_model_{index}_2026050100_gfs_2026043012_f012",
                "init_state_uri": f"s3://nhms/states/gfs/model_{index}/2026050100/state.cfg.ic",
                "init_state_checksum": f"sha256:state-{index}",
                "init_state_valid_time": "2026-05-01T00:00:00Z",
            }
        )
    return basins


def _journal_master(repository: Any, job_id: str) -> dict[str, Any]:
    return next(row for row in repository.query_pipeline_jobs_by_cycle(_CYCLE_ID) if row["job_id"] == job_id)


def test_legacy_lane_downstream_parse_derives_its_own_attempt_after_upstream_reservation(tmp_path: Path) -> None:
    """2.1 (#2393 two-round recipe, DB-legacy lane): parse never targets forecast's attempt.

    Round 1 stacks forecast to ``_retry_1_retry_2_retry_3`` and leaves parse
    terminal at ``_parse_retry_1_retry_2_retry_3``. Round 2 (same markerless
    ``retry_missing_forecast_output`` evidence): forecast reserves a fresh
    attempt (legacy lane -> 1), and parse must mint from ITS rows (last suffix
    3 -> ``_retry_4``) instead of aiming at the occupied ``_parse_retry_1``.
    """

    from services.orchestrator.retry import RetryConfig, RetryService
    from tests.test_orchestration_chain import StoreBackedCycleRepository, _marker_claim_basins, _pipeline_store

    store = _pipeline_store()
    repository = StoreBackedCycleRepository(store)
    retry_service = RetryService(store, RetryConfig(max_retries=3, backoff_schedule=[0]))

    def _round(label: str, client: FakeCycleSlurmClient) -> Any:
        orchestrator = _orchestrator(tmp_path / label, repository, client, retry_service=retry_service)
        basins = _marker_claim_basins(decision="retry_missing_forecast_output", claim=None)
        return orchestrator.orchestrate_cycle("gfs", _CYCLE, basins)

    first = _round(
        "round-1",
        FakeCycleSlurmClient(
            array_results_by_stage={"forecast": [["failed"]] * 3 + [["succeeded"]], "parse": [["failed"]]}
        ),
    )
    base = "job_cycle_gfs_2026050100_forecast_model_0"
    assert first.status == "failed"
    assert f"{base}_parse_retry_1" in repository.jobs
    assert repository.jobs[f"{base}_parse_retry_1_retry_2_retry_3"]["status"] in {"failed", "permanently_failed"}

    client = FakeCycleSlurmClient()
    second = _round("round-2", client)

    assert _stage_result(second, "forecast").pipeline_job_id == f"{base}_forecast_retry_4"
    parse = _stage_result(second, "parse")
    assert parse.pipeline_job_id == f"{base}_parse_retry_4"
    assert parse.status == "succeeded"
    assert "skipped_duplicate_submission" not in {stage.status for stage in second.stages}
    assert second.status == "complete"
    assert _submitted_stages(client)[:2] == ["forecast", "parse"]
    assert repository.jobs[f"{base}_parse_retry_4"]["idempotency_key"].endswith(":parse:retry_4")


def test_file_journal_downstream_reservation_attempt_comes_from_its_own_rows(tmp_path: Path) -> None:
    """2.2 (FileJournal lane): forcing reserves attempt 2; forecast must not inherit it.

    The FileJournal cohort reservation computes ``max(claim or 1, own suffix+1)``.
    Round 2 restarts at forcing under the whitelisted ``retry_repair_missing_forcing``:
    forcing mints ``_forcing_retry_1`` (reservation attempt 2). Forecast's own rows
    hold only the bare master, so its identity is ``_forecast_retry_1`` with
    reservation attempt 2 -- not ``_forecast_retry_2`` / attempt 3 inherited from
    forcing. Parse likewise mints from its own rows.

    2.4 must-preserve (in-stage): the forecast runtime manifest this pass staged
    (``chain_manifests`` ``submission_attempt``, read after the reservation
    writeback) carries the forecast reservation's own attempt.
    """

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    first = _orchestrator(tmp_path / "round-1", repository, FakeCycleSlurmClient()).orchestrate_cycle(
        "gfs", _CYCLE, _journal_basins(restart_stage="forcing", count=2)
    )
    assert first.status == "complete"

    client = FakeCycleSlurmClient()
    client.next_job = 3000
    second = _orchestrator(tmp_path / "round-2", repository, client).orchestrate_cycle(
        "gfs",
        _CYCLE,
        _journal_basins(restart_stage="forcing", count=2, decision="retry_repair_missing_forcing"),
    )

    base = f"job_{_COHORT_RUN_ID}"
    assert second.status == "complete"
    assert _stage_result(second, "forcing").pipeline_job_id == f"{base}_forcing_retry_1"
    assert _journal_master(repository, f"{base}_forcing_retry_1")["submission_attempt"] == 2
    forecast_id = f"{base}_forecast_retry_1"
    assert _stage_result(second, "forecast").pipeline_job_id == forecast_id
    forecast_master = _journal_master(repository, forecast_id)
    assert forecast_master["submission_attempt"] == 2
    assert _stage_result(second, "parse").pipeline_job_id == f"{base}_parse_retry_1"
    assert _submitted_stages(client)[:3] == ["forcing", "forecast", "parse"]
    for index in range(2):
        staged = tmp_path / "round-2" / "workspace" / "runs" / f"fcst_gfs_{_CYCLE}_model_{index}" / "input"
        manifest = json.loads((staged / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["submission_attempt"] == forecast_master["submission_attempt"]


def test_confirmed_budget_reentry_state_save_qc_takes_its_own_attempt_and_submits(tmp_path: Path) -> None:
    """2.3 (#2393 comment): a confirmed strict warm-start budget re-entry must progress downstream.

    The re-entry reruns forecast (reservation attempt 2, stamping the budget
    re-entry provenance, which consumes the confirmation). ``state_save_qc``
    already holds a terminal ``_retry_2`` row from an earlier stage retry; it must
    mint its own next attempt (``_retry_3``) and really submit, not aim at the
    occupied ``_retry_2`` and end ``skipped_duplicate_submission`` with the
    operator's confirmation already spent.
    """

    from services.orchestrator.chain_stages import COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")

    def _orchestrate(label: str, client: FakeCycleSlurmClient, **evidence: Any) -> Any:
        orchestrator = _orchestrator(
            tmp_path / label, repository, client, terminal_stage=COMPUTE_STATE_SAVE_QC_TERMINAL_STAGE
        )
        return orchestrator.orchestrate_cycle("gfs", _CYCLE, _journal_basins(restart_stage="forecast", **evidence))

    assert _orchestrate("round-1", FakeCycleSlurmClient()).status == "succeeded"
    base = f"job_{_COHORT_RUN_ID}_state_save_qc"
    bare = _journal_master(repository, base)
    repository.upsert_pipeline_job(
        {
            "job_id": f"{base}_retry_2",
            "run_id": bare["run_id"],
            "cycle_id": _CYCLE_ID,
            "source_id": "gfs",
            "model_id": bare.get("model_id"),
            "stage": "state_save_qc",
            "job_type": bare["job_type"],
            "status": "failed",
            "retry_count": 0,
            "slurm_job_id": "2900",
            "idempotency_key": f"{_COHORT_RUN_ID}:state_save_qc:retry_2",
        }
    )

    client = FakeCycleSlurmClient()
    client.next_job = 3000
    confirmation = {
        "request_id": "r",
        "operator": "ops",
        "reason": "state index repaired",
        "pin": 0,
        "decision": "blocked_strict_warm_start_init_state_mismatch",
    }
    second = _orchestrate(
        "round-2",
        client,
        decision="retry_strict_warm_start_terminal_init_state_mismatch",
        operator_reentry_confirmation=confirmation,
    )

    forecast_id = f"job_{_COHORT_RUN_ID}_forecast_retry_1"
    assert _stage_result(second, "forecast").pipeline_job_id == forecast_id
    assert _journal_master(repository, forecast_id)["submission_attempt"] == 2
    state_save_qc = _stage_result(second, "state_save_qc")
    assert state_save_qc.pipeline_job_id == f"{base}_retry_3"
    assert state_save_qc.status == "succeeded"
    assert "skipped_duplicate_submission" not in {stage.status for stage in second.stages}
    assert second.status == "succeeded"
    assert _submitted_stages(client) == ["forecast", "state_save_qc"]
    assert _journal_master(repository, f"{base}_retry_2")["status"] == "failed"
    cycle_time = datetime(2026, 5, 1, tzinfo=UTC)
    assert repository.budget_reentry_count(source_id="gfs", cycle_time=cycle_time, model_id="model_0") == 1


def test_markerless_non_whitelisted_downstream_terminal_row_resumes_after_upstream_reservation(
    tmp_path: Path,
) -> None:
    """2.5 sibling pin: the ``retry_attempt is None`` gate is not opened by upstream residue.

    Markerless ``retry_transient_failure`` (outside the forced-resubmit set):
    forecast has no row, so it submits and reserves (legacy lane -> attempt 1).
    Parse's terminal failed row must be RESUMED, exactly as for a markerless
    candidate (#1201 markerless equivalence), not resubmitted as ``_parse_retry_1``.
    """

    from tests.test_orchestration_chain import StoreBackedCycleRepository, _marker_claim_basins, _pipeline_store

    store = _pipeline_store()
    repository = StoreBackedCycleRepository(store)
    run_id = "cycle_gfs_2026050100_forecast_model_0"
    parse_id = f"job_{run_id}_parse"
    parse_row = store.create_job(
        job_id=parse_id,
        run_id=run_id,
        cycle_id=_CYCLE_ID,
        job_type="parse_output_array",
        slurm_job_id="6060",
        model_id="model_0",
        stage="parse",
        status="failed",
        idempotency_key=f"{run_id}:parse",
    )
    parse_row.error_code = "SLURM_JOB_FAILED"
    store.session.add(parse_row)
    store.session.commit()
    client = FakeCycleSlurmClient()

    result = _orchestrator(tmp_path, repository, client).orchestrate_cycle(
        "gfs", _CYCLE, _marker_claim_basins(decision="retry_transient_failure", claim=None)
    )

    assert _stage_result(result, "forecast").pipeline_job_id == f"job_{run_id}_forecast"
    parse = _stage_result(result, "parse")
    assert parse.pipeline_job_id == parse_id
    assert parse.status == "failed"
    assert _submitted_stages(client) == ["forecast"]
    assert f"{parse_id}_retry_1" not in repository.jobs
    assert result.status == "failed"
