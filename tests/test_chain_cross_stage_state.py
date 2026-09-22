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


# --- #1845: forcing resume matches the cohort's exact model set ---------------
#
# Unscoped path: a direct ``orchestrate_cycle`` whose basins carry no
# ``orchestration_run_id`` shares the run id ``cycle_gfs_<stamp>`` with every
# other unscoped cohort of the cycle (production always stamps a run id, so this
# is latent hardening; see tasks.md 4.0). FileJournal lane: forcing rows carry
# complete ``cohort_members`` as #2447 writes them. Every round stops after
# forcing, so model-blind downstream stages (a non-goal) never enter the result.

_SHARED_FORCING = f"job_cycle_gfs_{_CYCLE}_forcing"


def _cohort_basins(models: tuple[int, ...], *, orchestration_run_id: str | None = None) -> list[dict[str, Any]]:
    basins = []
    for index in models:
        basin = _basins(index + 1)[index]
        basin.update(
            {
                "run_id": f"fcst_gfs_{_CYCLE}_model_{index}",
                "candidate_id": f"gfs:2026-05-01T00:00:00Z:model_{index}:forecast_gfs_deterministic",
                "restart_stage": "forcing",
                "state_evidence": {"restart_stage": "forcing"},
                "model_package_uri": f"s3://nhms/models/model_{index}.tar",
                "model_package_checksum": f"sha256:model-{index}",
            }
        )
        if orchestration_run_id is not None:
            basin["orchestration_run_id"] = orchestration_run_id
        basins.append(basin)
    return basins


def _forcing_round(
    tmp_path: Path,
    label: str,
    repository: Any,
    client: FakeCycleSlurmClient,
    models: tuple[int, ...],
    *,
    orchestration_run_id: str | None = None,
    retry_service: Any | None = None,
) -> Any:
    basins = _cohort_basins(models, orchestration_run_id=orchestration_run_id)
    orchestrator = _orchestrator(
        tmp_path / label, repository, client, terminal_stage="forcing", retry_service=retry_service
    )
    return orchestrator.orchestrate_cycle("gfs", _CYCLE, basins)


def _member_models(row: dict[str, Any]) -> set[str]:
    return {member["model_id"] for member in row["cohort_members"]}


def _task_models(submission: dict[str, Any]) -> list[str]:
    return [task["model_id"] for task in submission["tasks"]]


def _sibling_forcing_row(
    tmp_path: Path, repository: Any, models: tuple[int, ...], *, run_id: str | None = None, status: str | None = None
) -> dict[str, Any]:
    """A sibling cohort's forcing row, written by a real forcing round (#2447 identity)."""

    from services.orchestrator.forcing_submit_identity import forcing_member_identity_is_complete

    client = FakeCycleSlurmClient()
    result = _forcing_round(tmp_path, "sibling", repository, client, models, orchestration_run_id=run_id)
    assert result.status == "succeeded"
    row = _journal_master(repository, f"job_{run_id}_forcing" if run_id else _SHARED_FORCING)
    assert forcing_member_identity_is_complete(row)
    assert _member_models(row) == {f"model_{index}" for index in models}
    if status is not None:
        # Only the lifecycle status is moved (the sibling is still in flight);
        # every identity field stays exactly as the forcing round wrote it.
        repository.upsert_pipeline_job({**row, "status": status, "finished_at": None})
        row = _journal_master(repository, row["job_id"])
    return row


@pytest.mark.parametrize(
    "sibling_models",
    [pytest.param((2, 3), id="disjoint-4.1"), pytest.param((1, 2), id="overlapping-terminal-4.4b")],
)
def test_sibling_model_set_terminal_forcing_row_under_shared_base_id_is_not_resumed(
    tmp_path: Path, sibling_models: tuple[int, ...]
) -> None:
    """4.1 / 4.4b: another model set's terminal succeeded forcing row holds the bare id.

    The current cohort {model_0, model_1} really submits forcing under the next
    free ``_retry_1`` (no ``skipped_duplicate_submission``) and the sibling row is
    neither resumed nor touched. 4.4b (members overlap but differ, i.e. the
    cohort changed) is the accepted consequence: forcing is recomputed.
    """

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    sibling = _sibling_forcing_row(tmp_path, repository, sibling_models)
    assert chain_runtime_utils._candidate_scoped_cycle_execution(_cohort_basins((0, 1))) is False
    client = FakeCycleSlurmClient()
    client.next_job = 3000

    result = _forcing_round(tmp_path, "current", repository, client, (0, 1))

    forcing = _stage_result(result, "forcing")
    assert forcing.pipeline_job_id == f"{_SHARED_FORCING}_retry_1"
    assert forcing.status == "succeeded"
    assert result.status == "succeeded"
    assert "skipped_duplicate_submission" not in {stage.status for stage in result.stages}
    assert [_task_models(submission) for submission in client.submissions] == [["model_0", "model_1"]]
    assert forcing.slurm_job_id == "3001"
    assert _member_models(_journal_master(repository, f"{_SHARED_FORCING}_retry_1")) == {"model_0", "model_1"}
    assert _journal_master(repository, _SHARED_FORCING) == sibling


def test_disjoint_in_flight_sibling_forcing_row_is_not_polled_as_ours(tmp_path: Path) -> None:
    """4.1b: a disjoint sibling cohort's forcing is still running under its own run id.

    The #2543 admission gate already lets the disjoint cohort in; the forcing
    stage must then submit for the current cohort under the bare id instead of
    adopting (polling) the sibling's in-flight array.
    """

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.forcing_submit_identity import is_unresolved_forcing_attempt

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    sibling_run_id = f"cycle_gfs_{_CYCLE}_forcing_cohort_sibling"
    sibling = _sibling_forcing_row(tmp_path, repository, (2,), run_id=sibling_run_id, status="running")
    assert is_unresolved_forcing_attempt(sibling)
    client = FakeCycleSlurmClient()
    client.next_job = 3000

    result = _forcing_round(tmp_path, "current", repository, client, (0, 1))

    forcing = _stage_result(result, "forcing")
    assert forcing.pipeline_job_id == _SHARED_FORCING
    assert forcing.status == "succeeded"
    assert forcing.slurm_job_id == "3001"
    assert result.status == "succeeded"
    assert [_task_models(submission) for submission in client.submissions] == [["model_0", "model_1"]]
    assert set(client.poll_counts) == {"3001"}
    assert _journal_master(repository, sibling["job_id"]) == sibling


def test_own_model_set_terminal_forcing_row_is_resumed_verbatim(tmp_path: Path) -> None:
    """4.2 must-preserve: equal member set (any basin order) -> resume, no new submission."""

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = FakeCycleSlurmClient()
    first = _forcing_round(tmp_path, "round-1", repository, client, (0, 1))
    own = _journal_master(repository, _SHARED_FORCING)
    assert first.status == "succeeded"

    second = _forcing_round(tmp_path, "round-2", repository, client, (1, 0))

    forcing = _stage_result(second, "forcing")
    assert forcing.pipeline_job_id == _SHARED_FORCING
    assert forcing.status == "succeeded"
    assert forcing.slurm_job_id == own["slurm_job_id"]
    assert second.status == "succeeded"
    assert len(client.submissions) == 1
    assert [row["job_id"] for row in repository.query_pipeline_jobs_by_cycle(_CYCLE_ID)] == [_SHARED_FORCING]


def test_db_legacy_forcing_row_without_member_identity_keeps_model_blind_resume(tmp_path: Path) -> None:
    """4.3 lane limitation: DB-legacy rows never carry ``cohort_members`` (only the
    FileJournal lane writes them), so D2 cannot tell whose forcing it is and the
    stage-name match resumes it -- even though it ran for another model set.
    """

    from tests.test_orchestration_chain import StoreBackedCycleRepository, _pipeline_store

    store = _pipeline_store()
    repository = StoreBackedCycleRepository(store)
    store.create_job(
        job_id=_SHARED_FORCING,
        run_id=f"cycle_gfs_{_CYCLE}",
        cycle_id=_CYCLE_ID,
        job_type="produce_forcing_array",
        slurm_job_id="4001",
        model_id=None,
        stage="forcing",
        status="succeeded",
        idempotency_key=f"cycle_gfs_{_CYCLE}:forcing",
    )
    client = FakeCycleSlurmClient()
    client.jobs["4001"] = {
        "job_id": "4001",
        "run_id": f"cycle_gfs_{_CYCLE}",
        "model_id": None,
        "stage": "forcing",
        "status": "succeeded",
        "submitted_at": "2026-05-01T00:01:00Z",
        "payload": {"tasks": [{"model_id": "model_9"}, {"model_id": "model_8"}]},
        "stage_attempt": 0,
    }

    result = _forcing_round(tmp_path, "legacy", repository, client, (0, 1))

    forcing = _stage_result(result, "forcing")
    assert forcing.pipeline_job_id == _SHARED_FORCING
    assert forcing.slurm_job_id == "4001"
    assert client.submissions == []


def test_overlapping_unresolved_sibling_forcing_row_still_blocks(tmp_path: Path) -> None:
    """4.4: members intersect the current cohort and are still in flight.

    Public seam: the admission gate refuses the cohort (the shared model is
    active), so nothing is submitted. Identity seam: were the chain reached,
    ``find_existing_stage_job`` still hands back the overlapping row through the
    blocker branch instead of letting D2's exclusion open a fresh submission.
    """

    from services.orchestrator.chain import CycleOrchestrationContext, OrchestratorError
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    sibling_run_id = f"cycle_gfs_{_CYCLE}_forcing_cohort_overlap"
    sibling = _sibling_forcing_row(tmp_path, repository, (1, 2), run_id=sibling_run_id, status="running")
    client = FakeCycleSlurmClient()

    with pytest.raises(OrchestratorError) as refused:
        _forcing_round(tmp_path, "current", repository, client, (0, 1))
    assert refused.value.error_code == "PIPELINE_ALREADY_ACTIVE"
    assert client.submissions == []

    orchestrator = _orchestrator(tmp_path / "seam", repository, client, terminal_stage="forcing")
    cycle_time = datetime(2026, 5, 1, tzinfo=UTC)
    basins = orchestrator._normalize_cycle_basins(_cohort_basins((0, 1)), "gfs", cycle_time)
    context = CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=cycle_time,
        cycle_id=_CYCLE_ID,
        run_id=f"cycle_gfs_{_CYCLE}",
        all_basins=basins,
        active_basins=list(basins),
        restart_stage="forcing",
    )
    forcing_stage = next(stage for stage in orchestrator.stages if stage.stage == "forcing")
    jobs = orchestrator._query_pipeline_jobs_for_cycle_context(context)

    existing = orchestrator._find_existing_stage_job(jobs, forcing_stage, context=context)

    assert existing is not None
    assert existing["job_id"] == sibling["job_id"]
    assert existing["status"] == "running"


@pytest.mark.parametrize("nested_outcome", ["succeeded", "failed"])
def test_reentry_after_nested_partial_retry_mints_past_the_excluded_subset_row(
    tmp_path: Path, nested_outcome: str
) -> None:
    """D2 x same-run nested partial retry: the excluded subset row still owns its id.

    Pass 1: forcing for {model_0, model_1} ends ``partially_failed`` (model_1
    failed) and the nested ``_forcing_retry_1`` records the SUBSET {model_1} as
    its ``cohort_members`` under the same run id. Pass 2 (markerless, no convert
    refresh) excludes that subset row (D2), resumes the bare master and re-enters
    the nested retry: the new id is derived from the stage-selection snapshot --
    which still holds ``_retry_1`` -- so it is ``_retry_2``, never a colliding
    ``_retry_1`` that reservation would turn into ``skipped_duplicate_submission``.
    The pending subset {model_1} is recomputed in pass 2 even when ``_retry_1``
    already succeeded for it: the bare master's own aggregation is the
    stage's truth (accepted design outcome). ``_retry_1`` counts against the
    budget exactly as it did when selection still picked it, so pass 2 runs
    with one more retry allowed (a re-run granted budget); under the pass-1
    budget the stage declines durably instead of colliding.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileJournalRetryService,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.retry import RetryConfig

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    retry_service = FileJournalRetryService(repository, RetryConfig(max_retries=1, backoff_schedule=[0]))
    client = FakeCycleSlurmClient(
        array_results_by_stage={"forcing": [["succeeded", "failed"], [nested_outcome], ["succeeded"]]}
    )
    first = _forcing_round(tmp_path, "round-1", repository, client, (0, 1), retry_service=retry_service)
    assert _stage_result(first, "forcing").pipeline_job_id.startswith(_SHARED_FORCING)
    nested = _journal_master(repository, f"{_SHARED_FORCING}_retry_1")
    assert _member_models(nested) == {"model_1"}
    assert nested["status"] in ({"succeeded"} if nested_outcome == "succeeded" else {"failed", "permanently_failed"})
    assert _journal_master(repository, _SHARED_FORCING)["status"] == "partially_failed"
    assert [_task_models(submission) for submission in client.submissions] == [["model_0", "model_1"], ["model_1"]]

    retry_service = FileJournalRetryService(repository, RetryConfig(max_retries=2, backoff_schedule=[0]))
    second = _forcing_round(tmp_path, "round-2", repository, client, (0, 1), retry_service=retry_service)

    forcing = _stage_result(second, "forcing")
    assert "skipped_duplicate_submission" not in {stage.status for stage in second.stages}
    assert forcing.pipeline_job_id == f"{_SHARED_FORCING}_retry_2"
    assert forcing.status == "succeeded"
    assert second.status == "succeeded"
    assert [_task_models(submission) for submission in client.submissions][2:] == [["model_1"]]
    assert _member_models(_journal_master(repository, f"{_SHARED_FORCING}_retry_2")) == {"model_1"}
    # The pass-1 subset row is left exactly as its own attempt finished it.
    assert _journal_master(repository, f"{_SHARED_FORCING}_retry_1") == nested


@pytest.mark.parametrize("nested_outcome", ["succeeded", "failed"])
def test_reentry_after_nested_partial_retry_with_spent_budget_declines_durably(
    tmp_path: Path, nested_outcome: str
) -> None:
    """Same residue, budget already spent by ``_retry_1``: pass 2 neither collides
    with the excluded subset row (``AUTO_RETRY_JOB_CONFLICT`` pre-fix) nor submits;
    it lands the durable permanent-failure decline on the resumed bare master.
    Identical whether ``_retry_1`` succeeded or failed for its subset: the bare
    master's own aggregation is the stage's truth, and the budget ``_retry_1``
    charged is spent either way.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileJournalRetryService,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.retry import RetryConfig

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    retry_service = FileJournalRetryService(repository, RetryConfig(max_retries=1, backoff_schedule=[0]))
    client = FakeCycleSlurmClient(array_results_by_stage={"forcing": [["succeeded", "failed"], [nested_outcome]]})
    _forcing_round(tmp_path, "round-1", repository, client, (0, 1), retry_service=retry_service)
    nested = _journal_master(repository, f"{_SHARED_FORCING}_retry_1")
    assert nested["status"] == ("succeeded" if nested_outcome == "succeeded" else "permanently_failed")

    second = _forcing_round(tmp_path, "round-2", repository, client, (0, 1), retry_service=retry_service)

    forcing = _stage_result(second, "forcing")
    assert forcing.pipeline_job_id == _SHARED_FORCING
    assert forcing.status == "partially_failed"
    assert len(client.submissions) == 2
    assert _journal_master(repository, _SHARED_FORCING)["status"] == "permanently_failed"
    assert _journal_master(repository, f"{_SHARED_FORCING}_retry_1") == nested
    assert f"{_SHARED_FORCING}_retry_2" not in {
        row["job_id"] for row in repository.query_pipeline_jobs_by_cycle(_CYCLE_ID)
    }


@pytest.mark.parametrize(
    "array_results",
    [pytest.param(["succeeded", "failed"], id="nested-partial"), pytest.param(["failed", "failed"], id="top-level")],
)
def test_concurrent_retry_reservation_after_fresh_submit_does_not_advance_the_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, array_results: list[str]
) -> None:
    """job-retry-mechanism "Automatic public-cycle retry identity uses the selected
    stage snapshot": between this pass's failed fresh forcing submit and its
    post-submit re-query, a concurrent pass reserves ``_retry_1``. That later read
    must not push this pass to ``_retry_2`` (a second gateway submission); it
    derives ``_retry_1`` from its own attempt and meets the concurrent owner.
    ``max_retries=2`` so a floor-advanced ``_retry_2`` would be within budget.
    """

    from services.orchestrator.chain_forecast_orchestrator_cycle import ForecastOrchestratorCycleMixin
    from services.orchestrator.file_orchestration_journal import (
        FileJournalRetryService,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.retry import RetryConfig, RetryError

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    retry_service = FileJournalRetryService(repository, RetryConfig(max_retries=2, backoff_schedule=[0]))
    client = FakeCycleSlurmClient(array_results_by_stage={"forcing": [array_results]})
    concurrent_id = f"{_SHARED_FORCING}_retry_1"
    original_query = ForecastOrchestratorCycleMixin._query_pipeline_jobs_for_cycle_context

    def query_with_concurrent_reservation(self: Any, context: Any) -> Any:
        rows = {row["job_id"]: row for row in repository.query_pipeline_jobs_by_cycle(_CYCLE_ID)}
        master = rows.get(_SHARED_FORCING)
        if master is not None and master.get("finished_at") and concurrent_id not in rows:
            repository.upsert_pipeline_job(
                {
                    **master,
                    "job_id": concurrent_id,
                    "status": "running",
                    "slurm_job_id": "9999",
                    "finished_at": None,
                    "error_code": None,
                    "error_message": None,
                }
            )
        return original_query(self, context)

    monkeypatch.setattr(
        ForecastOrchestratorCycleMixin, "_query_pipeline_jobs_for_cycle_context", query_with_concurrent_reservation
    )
    with pytest.raises(RetryError) as conflict:
        _forcing_round(tmp_path, "race", repository, client, (0, 1), retry_service=retry_service)

    # The existing exact-key conflict (the concurrent owner's live ``_retry_1``
    # cannot be reset), never a fresh ``_retry_2`` identity.
    assert conflict.value.code == "AUTO_RETRY_JOB_CONFLICT"
    assert conflict.value.details["retry_job_id"] == concurrent_id
    assert len(client.submissions) == 1
    job_ids = {row["job_id"] for row in repository.query_pipeline_jobs_by_cycle(_CYCLE_ID)}
    assert job_ids == {_SHARED_FORCING, concurrent_id}
    assert _journal_master(repository, concurrent_id)["status"] == "running"
