"""Requirement pins for the one-shot manual-retry marker on DB-free forecast runs.

The requirement: a run whose failure the classifier calls permanent (``ARTIFACT_NOT_FOUND``
is the motivating case) never restarts on its own once the cause is repaired, so an operator
must be able to mark exactly ONE named run for one manual retry -- and nothing else.  "Nothing
else" is the whole risk: the forecast stage carries a cohort-master row covering every model in
the cycle, and a marker aimed at that row would restart the entire cohort.  The tool must
therefore resolve the per-run row, preview before it writes, report its refusals instead of
raising them, and stay per-run when several run ids are handed to one invocation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.node22_manual_retry_failed_runs import main
from services.orchestrator.file_orchestration_journal import (
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
    _blocked_query_job,
)
from tests.test_file_orchestration_journal import _dt, _journal_tree_bytes, _source_job

_CYCLE_TIME = _dt("2026-08-23T00:00:00Z")

# The two shapes of the motivating node-22 incident, verbatim in geometry: the per-run row the
# marker must target, and the cohort master covering the whole cycle that it must not.
_PER_RUN_MODEL_ID = "dg_abc"
_PER_RUN_ID = "fcst_gfs_2026082300_dg_abc"
_PER_RUN_JOB_ID = "job_fcst_gfs_2026082300_dg_abc_forecast_reconciled_34817_6"
_COHORT_RUN_ID = "cycle_gfs_2026082300"
_COHORT_JOB_ID = "job_cycle_gfs_2026082300_forecast_cohort_abc123_forecast"
_ACTIVE_RUN_ID = "fcst_gfs_2026082300_dg_zzz"
_ACTIVE_JOB_ID = "job_fcst_gfs_2026082300_dg_zzz_forecast_reconciled_34817_7"
_UNSEEDED_RUN_ID = "fcst_gfs_2026082300_dg_absent"


def _failed_per_run_job() -> dict[str, Any]:
    """The reconciled per-model row, permanently failed on a missing artifact."""

    job = _source_job(
        _CYCLE_TIME,
        source_id="gfs",
        job_id=_PER_RUN_JOB_ID,
        model_id=_PER_RUN_MODEL_ID,
    )
    job.update(
        {
            "status": "permanently_failed",
            "error_code": "ARTIFACT_NOT_FOUND",
            "slurm_job_id": "34817_6",
            "array_task_id": 6,
            "retry_count": 3,
        }
    )
    return job


def _failed_cohort_master_job() -> dict[str, Any]:
    """The cycle-wide forecast master, failed on the same cause.

    Failed as well, deliberately: with both rows in the same terminal shape a resolver that
    scoped by cycle instead of by run would have two rows to choose from, so naming the per-run
    one is evidence of run scoping rather than of the cohort row simply being absent.
    """

    job = _source_job(
        _CYCLE_TIME,
        source_id="gfs",
        job_id=_COHORT_JOB_ID,
        model_id=_PER_RUN_MODEL_ID,
    )
    job.update(
        {
            "run_id": _COHORT_RUN_ID,
            "model_id": None,
            "status": "permanently_failed",
            "error_code": "ARTIFACT_NOT_FOUND",
            "slurm_job_id": "34817",
            "idempotency_key": "cycle_gfs_2026082300:forecast",
            "retry_count": 3,
        }
    )
    return job


def _active_per_run_job() -> dict[str, Any]:
    """A second model in the same cycle, still queued -- not markable."""

    return _source_job(
        _CYCLE_TIME,
        source_id="gfs",
        job_id=_ACTIVE_JOB_ID,
        model_id="dg_zzz",
    )


def _journal(tmp_path: Path, jobs: list[dict[str, Any]]) -> Path:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=_CYCLE_TIME)
    for job in jobs:
        repository.upsert_pipeline_job(job)
    for job in jobs:
        # A vacuous pin is worse than none: assert the seeding actually landed both shapes
        # before any assertion claims the tool chose between them.
        assert repository.get_pipeline_job(str(job["job_id"])) is not None
    return root


def _invoke(root: Path, tmp_path: Path, *args: str) -> tuple[int, dict[str, Any]]:
    receipt_path = tmp_path / "receipt.json"
    exit_code = main(
        [
            "--journal-root",
            str(root),
            "--reason",
            "forcing backfilled under the new model id",
            "--requested-by",
            "operator",
            "--output",
            str(receipt_path),
            *args,
        ]
    )
    return exit_code, json.loads(receipt_path.read_text(encoding="utf-8"))


def _manual_retry_marker_events(root: Path) -> list[dict[str, Any]]:
    """Every durable manual-retry marker in the journal, read off the record stream."""

    events: list[dict[str, Any]] = []
    for path in sorted((root / "journal").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("record_type") != "pipeline_event":
                continue
            payload = record.get("payload") or {}
            if (payload.get("details") or {}).get("manual_retry_marker") is True:
                events.append(payload)
    return events


def test_preview_targets_the_per_run_row_not_the_cohort_master(tmp_path: Path) -> None:
    """The highest-value pin: a marker on the cohort master would restart every model."""

    root = _journal(tmp_path, [_failed_per_run_job(), _failed_cohort_master_job()])

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", _PER_RUN_ID)

    assert exit_code == 0
    preview = receipt["runs"][0]["preview"]
    assert preview["decision"] == "would_mark"
    assert preview["job_id"] == _PER_RUN_JOB_ID
    assert preview["job_id"] != _COHORT_JOB_ID
    assert preview["stage"] == "forecast"
    assert preview["status"] == "permanently_failed"
    assert preview["error_code"] == "ARTIFACT_NOT_FOUND"


def test_default_invocation_previews_without_writing_anything(tmp_path: Path) -> None:
    """No ``--execute`` means no bytes: the operator sees the target before it is touched."""

    root = _journal(tmp_path, [_failed_per_run_job(), _failed_cohort_master_job()])
    before = _journal_tree_bytes(root)

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", _PER_RUN_ID)

    assert exit_code == 0
    assert receipt["executed"] is False
    assert receipt["runs"][0]["outcome"] == "preview_only"
    assert receipt["outcome_counts"] == {"preview_only": 1}
    assert _journal_tree_bytes(root) == before
    assert _manual_retry_marker_events(root) == []


def test_active_run_is_reported_as_run_active_not_raised(tmp_path: Path) -> None:
    """A run still in flight is refused, and the refusal comes back in the receipt."""

    root = _journal(tmp_path, [_active_per_run_job()])

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", _ACTIVE_RUN_ID, "--execute")

    # Under ``--execute`` the operator asked for a marker and did not get one, so this is a
    # refusal in the receipt AND in the exit code -- a preview-time refusal must not be
    # indistinguishable from a successful preview run.
    assert exit_code == 1
    entry = receipt["runs"][0]
    assert entry["outcome"] == "refused"
    assert entry["preview"]["decision"] == "refused"
    assert entry["preview"]["reason"] == "run_active"
    assert entry["preview"]["job_id"] == _ACTIVE_JOB_ID
    assert _manual_retry_marker_events(root) == []


def test_run_without_a_failed_job_is_reported_as_no_retryable_failed_job(tmp_path: Path) -> None:
    """Nothing eligible in the journal is a reported refusal, not a crash."""

    root = _journal(tmp_path, [_failed_per_run_job()])

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", _UNSEEDED_RUN_ID, "--execute")

    assert exit_code == 1
    entry = receipt["runs"][0]
    assert entry["outcome"] == "refused"
    assert entry["preview"]["decision"] == "refused"
    assert entry["preview"]["reason"] == "no_retryable_failed_job"
    assert _manual_retry_marker_events(root) == []


def test_execute_marks_exactly_the_named_run_past_an_ineligible_sibling(tmp_path: Path) -> None:
    """Per-run, never a sweep: the ineligible run is listed FIRST and must not abort the rest.

    One marker lands, on the reconciled per-run row.  The cohort master and the active sibling
    stay unmarked -- marking the master here would be the cohort-wide restart this tool exists
    to avoid.
    """

    root = _journal(
        tmp_path,
        [_failed_per_run_job(), _failed_cohort_master_job(), _active_per_run_job()],
    )

    exit_code, receipt = _invoke(
        root,
        tmp_path,
        "--run-id",
        _ACTIVE_RUN_ID,
        "--run-id",
        _PER_RUN_ID,
        "--execute",
    )

    # Non-zero because one run was refused, and yet the eligible run WAS marked: the refusal
    # is per-run and must not abort the rest of the invocation.
    assert exit_code == 1
    assert receipt["executed"] is True
    assert receipt["outcome_counts"] == {"refused": 1, "marked": 1}

    refused, marked = receipt["runs"]
    assert refused["run_id"] == _ACTIVE_RUN_ID
    assert refused["preview"]["reason"] == "run_active"
    assert marked["run_id"] == _PER_RUN_ID
    assert marked["outcome"] == "marked"
    assert marked["marker"]["job_id"] == _PER_RUN_JOB_ID
    assert marked["marker"]["status"] == "manual_repair_requested"
    assert marked["marker"]["retry_count"] == 4

    events = _manual_retry_marker_events(root)
    assert len(events) == 1
    assert events[0]["entity_id"] == _PER_RUN_JOB_ID
    assert events[0]["status_to"] == "manual_repair_requested"
    assert events[0]["details"]["prior_failure_reason"] == "ARTIFACT_NOT_FOUND"
    assert events[0]["details"]["failure"]["permanent"] is False
    assert events[0]["details"]["requested_by"] == "operator"


# ---------------------------------------------------------------------------------------------
# #2584: the cohort-master hint on a refused hydro run id.
#
# Production shape (2026-09-23, basins_hlj IFS 12Z): the hydro run ``fcst_ifs_2026092212_<m>``
# succeeded, while the ``state_save_qc`` row of its single-model cohort master
# ``cycle_ifs_2026092212_convert_<m>`` was permanently failed on a gateway 502.  Previewing the
# hydro run id is refused (``no_retryable_failed_job``) -- correctly, the failed row is not the
# hydro run's -- and the preview must now NAME the cohort master to mark, and what marking it
# costs.  ``cohort_members`` sits only on forcing/forecast rows, as in production.
# ---------------------------------------------------------------------------------------------

_IFS_CYCLE_TIME = _dt("2026-09-22T12:00:00Z")
_HLJ_MODEL_ID = "dg_8a34ed2ba8f8dd22f2716405569628a9"
_HLJ_RUN_ID = f"fcst_ifs_2026092212_{_HLJ_MODEL_ID}"
_HLJ_COHORT_RUN_ID = f"cycle_ifs_2026092212_convert_{_HLJ_MODEL_ID}"
_HLJ_STATE_SAVE_JOB_ID = f"job_{_HLJ_COHORT_RUN_ID}_state_save_qc"
_MULTI_COHORT_RUN_ID = "cycle_ifs_2026092212_convert_cohort_a25183db6af8"
_OTHER_MODEL_IDS = ("dg_1b0c7e25d0b94a7c9d3a4a8b1c2d3e4f", "dg_2c1d8f36e1ca5b8dae4b5b9c2d3e4f50")
_STAGE_JOB_TYPES = {
    "convert": "convert_canonical",
    "forcing": "produce_forcing_array",
    "forecast": "run_shud_forecast_array",
    "state_save_qc": "save_state_snapshot_array",
}
_STAGE_MINUTES = {"convert": 5, "forcing": 20, "forecast": 60, "state_save_qc": 150}


def _members(model_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    """``cohort_members`` as a production forcing/forecast row records them, one per array task."""

    return [
        {
            "array_task_id": index,
            "candidate_id": f"IFS:2026-09-22T12:00:00Z:{model_id}:forecast_ifs_deterministic",
            "run_id": f"fcst_ifs_2026092212_{model_id}",
            "model_id": model_id,
            "basin_id": f"basin_{index}",
            "scenario_id": "forecast_ifs_deterministic",
            "restart_stage": "forecast",
        }
        for index, model_id in enumerate(model_ids)
    ]


def _stamp(minutes: int) -> str:
    # The production cycle's execution window: convert started 2026-09-23T02:00Z, the
    # state_save_qc submit failed at 04:30:39Z (12:30:39 CST).
    hours, mins = divmod(minutes, 60)
    return f"2026-09-23T{2 + hours:02d}:{mins:02d}:39Z"


def _cohort_row(
    run_id: str,
    stage: str,
    *,
    status: str,
    slurm_job_id: str | None,
    model_id: str | None = None,
    members: list[dict[str, Any]] | None = None,
    error_code: str | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    minutes = _STAGE_MINUTES[stage] + offset
    row: dict[str, Any] = {
        "job_id": f"job_{run_id}_{stage}",
        "run_id": run_id,
        "cycle_id": "ifs_2026092212",
        "source_id": "IFS",
        "job_type": _STAGE_JOB_TYPES[stage],
        "stage": stage,
        "model_id": model_id,
        "status": status,
        "slurm_job_id": slurm_job_id,
        "retry_count": 0,
        "created_at": _stamp(minutes),
        "submitted_at": _stamp(minutes),
        "finished_at": _stamp(minutes + 1),
        "updated_at": _stamp(minutes + 1),
    }
    if members is not None:
        row["cohort_members"] = members
    if error_code is not None:
        row.update(
            {
                "error_code": error_code,
                "error_message": "Slurm Gateway returned HTTP 502.",
                "slurm_job_id": None,
                "exit_code": None,
            }
        )
    elif status == "succeeded":
        row["exit_code"] = 0
    return row


def _single_model_cohort(*, state_save_status: str) -> list[dict[str, Any]]:
    """The HLJ cohort master: model on convert/forcing/state_save_qc, members on the forecast row."""

    failed = state_save_status != "succeeded"
    return [
        _cohort_row(_HLJ_COHORT_RUN_ID, "convert", status="succeeded", slurm_job_id="54817", model_id=_HLJ_MODEL_ID),
        _cohort_row(_HLJ_COHORT_RUN_ID, "forcing", status="succeeded", slurm_job_id="54822", model_id=_HLJ_MODEL_ID),
        _cohort_row(
            _HLJ_COHORT_RUN_ID,
            "forecast",
            status="succeeded",
            slurm_job_id="54871",
            members=_members((_HLJ_MODEL_ID,)),
        ),
        _cohort_row(
            _HLJ_COHORT_RUN_ID,
            "state_save_qc",
            status=state_save_status,
            slurm_job_id=None if failed else "54918",
            model_id=_HLJ_MODEL_ID,
            error_code="SLURM_PARSE_ERROR" if failed else None,
        ),
    ]


def _multi_member_cohort(
    run_id: str,
    model_ids: tuple[str, ...],
    *,
    state_save_status: str,
    members: list[dict[str, Any]] | None = None,
    offset: int = 1,
) -> list[dict[str, Any]]:
    """A model-less multi-member cohort; its membership is only on forcing and forecast."""

    recorded = members if members is not None else _members(model_ids)
    failed = state_save_status != "succeeded"
    return [
        _cohort_row(run_id, "convert", status="succeeded", slurm_job_id="54819", offset=offset),
        _cohort_row(run_id, "forcing", status="succeeded", slurm_job_id="54823", members=recorded, offset=offset),
        _cohort_row(run_id, "forecast", status="succeeded", slurm_job_id="54922", members=recorded, offset=offset),
        _cohort_row(
            run_id,
            "state_save_qc",
            status=state_save_status,
            slurm_job_id=None if failed else "55016",
            error_code="SLURM_PARSE_ERROR" if failed else None,
            offset=offset,
        ),
    ]


def _hlj_hydro_run_row() -> dict[str, Any]:
    return {
        "job_id": f"job_{_HLJ_RUN_ID}_forecast_reconciled_54871_0",
        "run_id": _HLJ_RUN_ID,
        "cycle_id": "ifs_2026092212",
        "source_id": "IFS",
        "job_type": "run_shud_forecast_array",
        "stage": "forecast",
        "model_id": _HLJ_MODEL_ID,
        "status": "succeeded",
        "slurm_job_id": "54871_0",
        "array_task_id": 0,
        "exit_code": 0,
        "retry_count": 0,
        "created_at": _stamp(60),
        "submitted_at": _stamp(60),
        "finished_at": _stamp(140),
        "updated_at": _stamp(140),
    }


def _ifs_journal(tmp_path: Path, jobs: list[dict[str, Any]]) -> Path:
    """IFS 12Z journal whose HLJ hydro run SUCCEEDED -- the selector refuses its id."""

    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    repository.ensure_forecast_cycle(source_id="IFS", cycle_time=_IFS_CYCLE_TIME)
    repository.create_hydro_run_from_basin(
        {"source_id": "IFS"},
        {
            "run_id": _HLJ_RUN_ID,
            "run_type": "forecast",
            "scenario_id": "forecast_ifs_deterministic",
            "source_id": "IFS",
            "cycle_time": _IFS_CYCLE_TIME.isoformat(),
            "start_time": _IFS_CYCLE_TIME.isoformat(),
            "end_time": "2026-09-29T12:00:00+00:00",
            "model": {"model_id": _HLJ_MODEL_ID, "basin_version_id": "basins_hlj_vbasins"},
            "forcing": {"forcing_version_id": f"forc_ifs_2026092212_{_HLJ_MODEL_ID}"},
            "outputs": {"run_manifest_uri": "s3://nhms/manifests/run.json"},
        },
    )
    repository.update_hydro_run_status(_HLJ_RUN_ID, "succeeded", slurm_job_id="54871_0")
    for job in jobs:
        repository.upsert_pipeline_job(job)
    for job in jobs:
        stored = repository.get_pipeline_job(str(job["job_id"]))
        assert stored is not None
        assert stored["status"] == job["status"]
        if "cohort_members" in job:
            # Seeding must keep the membership, or the multi-member pins would be vacuous.
            assert [member["model_id"] for member in stored["cohort_members"]] == [
                member["model_id"] for member in job["cohort_members"]
            ]
    assert (repository._hydro_run_for(_HLJ_RUN_ID) or {}).get("status") == "succeeded"
    return root


def test_refused_hydro_run_lists_its_failed_single_model_cohort_master(tmp_path: Path) -> None:
    """The HLJ incident: the preview names the cohort master to mark, and its cost."""

    root = _ifs_journal(
        tmp_path,
        [
            *_single_model_cohort(state_save_status="permanently_failed"),
            _hlj_hydro_run_row(),
            *_multi_member_cohort(
                _MULTI_COHORT_RUN_ID, (_OTHER_MODEL_IDS[0], _OTHER_MODEL_IDS[1]), state_save_status="succeeded"
            ),
        ],
    )
    before = _journal_tree_bytes(root)

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", _HLJ_RUN_ID)

    assert exit_code == 0
    assert receipt["outcome_counts"] == {"preview_only": 1}
    preview = receipt["runs"][0]["preview"]
    assert preview["decision"] == "refused"
    assert preview["reason"] == "no_retryable_failed_job"
    assert preview["cohort_candidates"] == [
        {
            "run_id": "cycle_ifs_2026092212_convert_dg_8a34ed2ba8f8dd22f2716405569628a9",
            "job_id": "job_cycle_ifs_2026092212_convert_dg_8a34ed2ba8f8dd22f2716405569628a9_state_save_qc",
            "stage": "state_save_qc",
            "status": "permanently_failed",
            "error_code": "SLURM_PARSE_ERROR",
            "member_count": 1,
        }
    ]
    assert "whole cohort from convert" in preview["warning"].lower()
    assert "cohort_candidates_error" not in preview
    # The hint is a read: not one journal byte moves.
    assert _journal_tree_bytes(root) == before

    # The id the hint names is the one the tool can act on.
    exit_code, receipt = _invoke(root, tmp_path, "--run-id", preview["cohort_candidates"][0]["run_id"])

    assert exit_code == 0
    cohort_preview = receipt["runs"][0]["preview"]
    assert cohort_preview["decision"] == "would_mark"
    assert cohort_preview["stage"] == "state_save_qc"
    assert cohort_preview["job_id"] == _HLJ_STATE_SAVE_JOB_ID
    assert _journal_tree_bytes(root) == before


def test_refused_hydro_run_under_execute_still_refuses_and_marks_nothing(tmp_path: Path) -> None:
    """The hint never substitutes the id: --execute on the hydro run id writes no marker."""

    root = _ifs_journal(
        tmp_path, [*_single_model_cohort(state_save_status="permanently_failed"), _hlj_hydro_run_row()]
    )

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", _HLJ_RUN_ID, "--execute")

    assert exit_code == 1
    entry = receipt["runs"][0]
    assert entry["outcome"] == "refused"
    assert entry["error"] == "no_retryable_failed_job"
    assert [candidate["run_id"] for candidate in entry["preview"]["cohort_candidates"]] == [_HLJ_COHORT_RUN_ID]
    assert _manual_retry_marker_events(root) == []


def test_no_failed_cohort_covering_the_model_lists_no_candidates(tmp_path: Path) -> None:
    """A succeeded own cohort and a failed cohort of OTHER models: nothing to name."""

    root = _ifs_journal(
        tmp_path,
        [
            *_single_model_cohort(state_save_status="succeeded"),
            _hlj_hydro_run_row(),
            *_multi_member_cohort(
                _MULTI_COHORT_RUN_ID,
                (_OTHER_MODEL_IDS[0], _OTHER_MODEL_IDS[1]),
                state_save_status="permanently_failed",
            ),
        ],
    )

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", _HLJ_RUN_ID)

    assert exit_code == 0
    preview = receipt["runs"][0]["preview"]
    assert preview["decision"] == "refused"
    assert preview["reason"] == "no_retryable_failed_job"
    assert preview["cohort_candidates"] == []
    assert "warning" not in preview
    assert "cohort_candidates_error" not in preview


def test_unparseable_run_id_gets_the_plain_refusal(tmp_path: Path) -> None:
    root = _ifs_journal(
        tmp_path, [*_single_model_cohort(state_save_status="permanently_failed"), _hlj_hydro_run_row()]
    )

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", "fcst_ifs_20260922_dg_8a34")

    assert exit_code == 0
    assert receipt["runs"][0]["preview"] == {"decision": "refused", "reason": "no_retryable_failed_job"}


def test_failed_multi_member_cohort_is_listed_only_when_its_membership_is_provable(tmp_path: Path) -> None:
    """Membership comes from forcing/forecast ``cohort_members``, never from the qc row.

    Two failed model-less cohorts both record the HLJ model.  One records a complete member
    list of three models -- listed, ``member_count`` 3.  The other records a member with a
    blank ``model_id`` -- membership unprovable, so it is not listed.
    """

    covering = (_HLJ_MODEL_ID, *_OTHER_MODEL_IDS)
    incomplete_members = _members((_HLJ_MODEL_ID, _OTHER_MODEL_IDS[0]))
    incomplete_members[1] = {**incomplete_members[1], "model_id": ""}
    incomplete_run_id = "cycle_ifs_2026092212_convert_cohort_0c9d6e1f2a3b"
    root = _ifs_journal(
        tmp_path,
        [
            *_single_model_cohort(state_save_status="succeeded"),
            _hlj_hydro_run_row(),
            *_multi_member_cohort(_MULTI_COHORT_RUN_ID, covering, state_save_status="permanently_failed"),
            *_multi_member_cohort(
                incomplete_run_id,
                (),
                members=incomplete_members,
                state_save_status="permanently_failed",
                offset=2,
            ),
        ],
    )
    qc_row = FileOrchestrationJournalRepository(root).get_pipeline_job(f"job_{_MULTI_COHORT_RUN_ID}_state_save_qc")
    assert qc_row is not None
    assert qc_row.get("model_id") in (None, "")
    assert not qc_row.get("cohort_members")

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", _HLJ_RUN_ID)

    assert exit_code == 0
    preview = receipt["runs"][0]["preview"]
    assert preview["reason"] == "no_retryable_failed_job"
    assert preview["cohort_candidates"] == [
        {
            "run_id": "cycle_ifs_2026092212_convert_cohort_a25183db6af8",
            "job_id": "job_cycle_ifs_2026092212_convert_cohort_a25183db6af8_state_save_qc",
            "stage": "state_save_qc",
            "status": "permanently_failed",
            "error_code": "SLURM_PARSE_ERROR",
            "member_count": 3,
        }
    ]
    assert "warning" in preview


def test_would_mark_and_run_active_previews_keep_their_exact_fields(tmp_path: Path) -> None:
    root = _journal(tmp_path, [_failed_per_run_job(), _failed_cohort_master_job(), _active_per_run_job()])

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", _PER_RUN_ID, "--run-id", _ACTIVE_RUN_ID)

    assert exit_code == 0
    would_mark, run_active = (entry["preview"] for entry in receipt["runs"])
    assert would_mark == {
        "decision": "would_mark",
        "job_id": _PER_RUN_JOB_ID,
        "stage": "forecast",
        "status": "permanently_failed",
        "error_code": "ARTIFACT_NOT_FOUND",
        "retry_count": 3,
    }
    assert run_active == {"decision": "refused", "reason": "run_active", "job_id": _ACTIVE_JOB_ID}


@pytest.mark.parametrize(
    ("cycle_answer", "expected_error"),
    [
        (
            "blocked",
            {
                "reason": "journal_read_blocked",
                "journal_reason": "file_journal_record_invalid",
                "journal_field": "payload",
            },
        ),
        (
            "raises",
            {"reason": "cohort_candidates_query_failed", "error": "OSError: stale NFS file handle"},
        ),
    ],
)
def test_unreadable_cycle_reports_a_hint_error_and_keeps_the_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cycle_answer: str,
    expected_error: dict[str, str],
) -> None:
    """A blocked cycle read is not "no candidates": it says so, and changes nothing else."""

    root = _ifs_journal(
        tmp_path, [*_single_model_cohort(state_save_status="permanently_failed"), _hlj_hydro_run_row()]
    )
    seen_cycle_ids: list[str] = []

    def _unreadable_cycle(self: FileOrchestrationJournalRepository, cycle_id: str) -> list[dict[str, Any]]:
        seen_cycle_ids.append(cycle_id)
        if cycle_answer == "raises":
            raise OSError("stale NFS file handle")
        return [
            _blocked_query_job(
                FileOrchestrationJournalError("file_journal_record_invalid", field="payload"),
                cycle_id=cycle_id,
            )
        ]

    monkeypatch.setattr(FileOrchestrationJournalRepository, "query_pipeline_jobs_by_cycle", _unreadable_cycle)

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", _HLJ_RUN_ID)

    assert seen_cycle_ids == ["ifs_2026092212"]
    assert exit_code == 0
    assert receipt["outcome_counts"] == {"preview_only": 1}
    preview = receipt["runs"][0]["preview"]
    assert preview == {
        "decision": "refused",
        "reason": "no_retryable_failed_job",
        "cohort_candidates_error": expected_error,
    }
