"""The candidate-state ladder decides the same with and without the event evidence copy (#3223, E1).

For one state per ladder exit -- every state carrying at least one pipeline
event -- ``_candidate_state_decision(..., event_evidence=False)`` must return the
same ``action`` and ``reason`` as the default, and its evidence must equal the
default evidence with only the ``pipeline_events`` key removed.  The states are
the ones the existing scheduler tests use to reach each exit (the source test is
named on every case); where that state has no event, one already-finished
event is added and the case still asserts the exit it reaches.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from services.orchestrator import scheduler as scheduler_module
from tests.test_production_scheduler import (
    _RECORDED_FORCING_PACKAGE_URI,
    FakeCandidateStateRepository,
    _convert_only_completed_stage_state,
    _forecast_failure_state,
    _production_identity_fixture,
    _scheduler_candidate_fixture,
    _seed_recorded_forcing_packages,
)

_RUN_ID = "fcst_gfs_2026052106_model_a"

# Added only to states whose source test carries no event.
_FINISHED_EVENT = {
    "event_id": 1,
    "event_type": "status_change",
    "entity_id": "job_convert_done",
    "status_to": "succeeded",
    "created_at": "2026-05-21T05:00:00Z",
    "details": {"stage": "convert"},
}

_MANUAL_RETRY_MARKER = {
    "event_id": 9,
    "event_type": "retry",
    "run_id": _RUN_ID,
    "model_id": "model_a",
    "source": "gfs",
    "cycle_time": "2026-05-21T06:00:00Z",
    "entity_id": "job_manual_retry",
    "created_at": "2026-05-21T06:30:00Z",
    "details": {
        "trigger": "manual",
        "manual_retry_marker": True,
        "retry_count": 4,
        "previous_job_id": "job_old_failed",
    },
}


def _identity_mismatch_state(candidate: object) -> dict[str, object]:
    # test_scheduler_candidate_state_list_identity_mismatch_blocks_before_reuse[pipeline_events-...identity]
    return {
        "pipeline_events": [
            {
                "event_id": 7,
                "entity_id": "job_fcst_gfs_2026052106_model_a_forecast",
                "event_type": "status_change",
                "status_to": "running",
                "details": {
                    "identity": {**_production_identity_fixture(), "basin_id": "basin_other"},
                    "stage": "forecast",
                    "pipeline_event_id": "event_7",
                },
            }
        ],
        "pipeline_status": "running",
    }


def _active_job_state(status: str, slurm_job_id: str | None) -> Callable[[object], dict[str, object]]:
    # test_newer_manual_retry_marker_does_not_override_active_truth[state0 / state1]
    def build(_candidate: object) -> dict[str, object]:
        job = {
            "job_id": f"job_{status}",
            "run_id": _RUN_ID,
            "status": status,
            "stage": "forecast",
            "updated_at": "2026-05-21T06:20:00Z",
        }
        if slurm_job_id is not None:
            job["slurm_job_id"] = slurm_job_id
        return {"pipeline_status": status, "pipeline_jobs": [job], "pipeline_events": [dict(_MANUAL_RETRY_MARKER)]}

    return build


def _completed_cycle_state(candidate: object) -> dict[str, object]:
    # test at tests/test_production_scheduler.py ``decision.reason == "terminal_completed_cycle"``
    # (same completed forecast cycle + copyback geometry, on the gfs candidate fixture)
    return {
        "source_id": "gfs",
        "cycle_time": "2026-05-21T06:00:00Z",
        "model_id": candidate.model_id,
        "basin_id": candidate.basin_id,
        "candidate_id": candidate.candidate_id,
        "forecast_cycle": {
            "cycle_id": candidate.cycle_id,
            "source_id": "gfs",
            "cycle_time": "2026-05-21T06:00:00Z",
            "status": "complete",
        },
        "copyback_evidence": {
            "stage": "copyback",
            "status": "succeeded",
            "source_id": "gfs",
            "cycle_id": candidate.cycle_id,
            "model_id": candidate.model_id,
            "run_id": candidate.run_id,
            "candidate_id": candidate.candidate_id,
        },
        "pipeline_events": [dict(_FINISHED_EVENT)],
    }


def _forecast_failure_with_event(candidate: object) -> dict[str, object]:
    # test_*_forcing_version_row_absent: ``_forecast_failure_state`` on an empty object store
    return _forecast_failure_state(candidate, pipeline_events=[dict(_FINISHED_EVENT)])


def _cohort_unprovable_state(_candidate: object) -> dict[str, object]:
    # tests/test_scheduler_evidence_decidability.py: the ``incomplete`` cohort row
    return {
        "pipeline_jobs": [
            {
                "job_id": "job_cycle_gfs_2026052106_state_save_qc_cohort_0123456789ab_state_save_qc",
                "run_id": "cycle_gfs_2026052106_state_save_qc_cohort_0123456789ab",
                "stage": "state_save_qc",
                "model_id": None,
                "status": "permanently_failed",
                "error_code": "NODE_FAILURE",
                "cohort_membership": "incomplete",
            }
        ],
        "pipeline_events": [dict(_FINISHED_EVENT)],
    }


def _convert_only_with_event(candidate: object) -> dict[str, object]:
    # test_convert_only_strict_ready_resume_preserves_forcing_through_upgrade_and_guard
    return {**_convert_only_completed_stage_state(candidate), "pipeline_events": [dict(_FINISHED_EVENT)]}


def _manual_retry_after_terminal_state(_candidate: object) -> dict[str, object]:
    # test_newer_manual_retry_after_terminal_truth_allows_candidate_and_preserves_attempts
    return {
        "pipeline_status": "permanently_failed",
        "failed_stage": "forecast",
        "error_code": "INVALID_MANIFEST",
        "retry_count": 3,
        "pipeline_jobs": [
            {
                "job_id": "job_latest",
                "run_id": _RUN_ID,
                "status": "permanently_failed",
                "stage": "forecast",
                "retry_count": 3,
                "error_code": "INVALID_MANIFEST",
                "updated_at": "2026-05-21T06:20:00Z",
            }
        ],
        "pipeline_events": [
            {
                **_MANUAL_RETRY_MARKER,
                "event_id": 5,
                "entity_id": "job_retry",
                "details": {
                    "trigger": "manual",
                    "manual_retry_marker": True,
                    "retry_count": 4,
                    "previous_job_id": "job_latest",
                    "prior_failure_reason": "INVALID_MANIFEST",
                },
            }
        ],
    }


def _nested_failed_task_state(candidate: object) -> dict[str, object]:
    # test_nested_failed_task_identity_remains_available_when_failure_state_is_candidate_scoped
    return {
        "pipeline_status": "failed",
        "failed_stage": "forecast",
        "error_code": "NODE_FAILURE",
        "forcing_package_uri": _RECORDED_FORCING_PACKAGE_URI,
        "retry_count": 1,
        "pipeline_events": [
            {
                "event_id": 103,
                "event_type": "status_change",
                "status_to": "failed",
                "created_at": "2026-05-21T06:30:00Z",
                "details": {
                    "stage": "forecast",
                    "task_results": [
                        {
                            "run_id": candidate.run_id,
                            "task_id": 2,
                            "array_task_id": 2,
                            "original_task_id": 12,
                            "status": "failed",
                            "error_code": "NODE_FAILURE",
                            "slurm_job_id": "slurm_task_2",
                        }
                    ],
                },
            }
        ],
    }


def _stale_marker_state(latest_status: str) -> Callable[[object], dict[str, object]]:
    # test_stale_manual_retry_marker_does_not_override_newer_blocking_truth[permanently_failed / cancelled]
    def build(_candidate: object) -> dict[str, object]:
        failed = latest_status == "permanently_failed"
        return {
            "pipeline_status": latest_status,
            "failed_stage": "forecast" if failed else None,
            "error_code": "INVALID_MANIFEST" if failed else None,
            "forcing_package_uri": _RECORDED_FORCING_PACKAGE_URI,
            "retry_count": 3,
            "pipeline_jobs": [
                {
                    "job_id": "job_latest",
                    "run_id": _RUN_ID,
                    "status": latest_status,
                    "stage": "forecast",
                    "retry_count": 3,
                    "error_code": "INVALID_MANIFEST" if failed else None,
                    "slurm_job_id": None,
                    "submitted_at": "2026-05-21T06:20:00Z",
                    "updated_at": "2026-05-21T06:21:00Z",
                }
            ],
            "pipeline_events": [
                {
                    "event_id": 1,
                    "event_type": "retry",
                    "created_at": "2026-05-21T06:10:00Z",
                    "details": {
                        "trigger": "manual",
                        "manual_retry_marker": True,
                        "retry_count": 3,
                        "previous_job_id": "job_old_failed",
                        "prior_failure_reason": "NODE_FAILURE",
                    },
                },
                {
                    "event_id": 2,
                    "event_type": "status_change",
                    "entity_id": "job_latest",
                    "status_to": latest_status,
                    "created_at": "2026-05-21T06:21:00Z",
                    "details": {"stage": "forecast", "error_code": "INVALID_MANIFEST" if failed else None},
                },
            ],
        }

    return build


def _durable_hydro_over_failed_parse_state(_candidate: object) -> dict[str, object]:
    # the ``terminal_hydro_success`` over a stale failed parse row test
    return {
        "hydro_status": "published",
        "hydro_run": {
            "run_id": _RUN_ID,
            "status": "published",
            "output_uri": f"s3://nhms/runs/{_RUN_ID}/output/",
            "updated_at": "2026-05-21T07:00:00Z",
        },
        "run_manifest_initial_state": {
            "quality": "fresh",
            "state_id": "state_gfs_model_a_2026052106_gfs_2026052100_f006",
        },
        "pipeline_status": "failed",
        "failed_stage": "parse",
        "error_code": "FAILED_PARSE",
        "pipeline_jobs": [
            {
                "job_id": "job_parse_old",
                "run_id": _RUN_ID,
                "status": "failed",
                "stage": "parse",
                "error_code": "FAILED_PARSE",
                "updated_at": "2026-05-21T06:00:00Z",
            }
        ],
        "pipeline_events": [dict(_FINISHED_EVENT)],
    }


def _published_pipeline_state(_candidate: object) -> dict[str, object]:
    # test_terminal_pipeline_success_is_not_overridden_by_manual_retry_marker[2026-05-21T06:00:00Z]
    return {
        "pipeline_status": "published",
        "run_manifest_initial_state": {
            "quality": "fresh",
            "state_id": "state_gfs_model_a_2026052106_gfs_2026052100_f006",
        },
        "pipeline_jobs": [
            {
                "job_id": "job_failed",
                "run_id": _RUN_ID,
                "model_id": "model_a",
                "status": "failed",
                "stage": "parse",
                "error_code": "FAILED_PARSE",
                "updated_at": "2026-05-21T05:50:00Z",
            },
            {
                "job_id": "job_publish_success",
                "run_id": _RUN_ID,
                "model_id": "model_a",
                "status": "published",
                "stage": "publish",
                "updated_at": "2026-05-21T06:30:00Z",
            },
        ],
        "pipeline_events": [
            {
                "event_id": 10,
                "event_type": "retry",
                "created_at": "2026-05-21T06:00:00Z",
                "details": {
                    "trigger": "manual",
                    "manual_retry_marker": True,
                    "retry_count": 3,
                    "previous_job_id": "job_failed",
                },
            }
        ],
    }


# (state builder, object store seeding, expected action, expected reason)
#   object store: "empty" = configured but holding nothing, "recorded" = holds
#   ``_RECORDED_FORCING_PACKAGE_URI``.
_LADDER_EXITS = [
    pytest.param(_identity_mismatch_state, None, "blocked", "production_identity_mismatch", id="identity_mismatch"),
    pytest.param(_active_job_state("running", "999"), None, "skip", "active_slurm_job", id="active_slurm_job"),
    pytest.param(_active_job_state("queued", None), None, "skip", "active_duplicate_pipeline", id="active_duplicate"),
    pytest.param(_completed_cycle_state, None, "skip", "terminal_completed_cycle", id="terminal_completed_cycle"),
    pytest.param(
        _forecast_failure_with_event, "empty", "blocked", "forcing_version_row_absent", id="missing_upstream_artifact"
    ),
    pytest.param(_cohort_unprovable_state, None, "blocked", "cohort_membership_unprovable", id="cohort_unprovable"),
    pytest.param(_convert_only_with_event, "empty", "retry", "resume_after_completed_stage", id="resume_completed"),
    pytest.param(
        _manual_retry_after_terminal_state, "recorded", "retry", "manual_retry_requested", id="manual_retry_requested"
    ),
    pytest.param(_nested_failed_task_state, "recorded", "retry", "retry_failed_candidate", id="retry_failed"),
    pytest.param(
        _stale_marker_state("permanently_failed"), "recorded", "blocked", "permanent_failure_guard", id="permanent"
    ),
    pytest.param(
        _stale_marker_state("cancelled"),
        "recorded",
        "blocked",
        "manual_retry_required_after_cancelled",
        id="cancelled",
    ),
    pytest.param(
        _durable_hydro_over_failed_parse_state, None, "skip", "terminal_hydro_success", id="terminal_hydro_success"
    ),
    pytest.param(_published_pipeline_state, None, "skip", "terminal_pipeline_success", id="terminal_pipeline"),
]


def _candidate_state(candidate: object, state: dict[str, object]) -> dict[str, object]:
    """The state the scheduler reads for ``candidate`` (the fake journal repository's shape)."""

    return FakeCandidateStateRepository(state).candidate_state(
        source_id=candidate.source_id,
        cycle_time=candidate.cycle_time_utc,
        model_id=candidate.model_id,
        run_id=candidate.run_id,
        forcing_version_id=candidate.forcing_version_id,
        candidate_id=candidate.candidate_id,
    )


def _seed_object_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str | None) -> None:
    if kind == "empty":
        root = tmp_path / "object-store"
        root.mkdir()
        monkeypatch.setenv("OBJECT_STORE_ROOT", str(root))
    elif kind == "recorded":
        _seed_recorded_forcing_packages(monkeypatch, tmp_path / "object-store", _RECORDED_FORCING_PACKAGE_URI)


def _assert_parity(candidate: object, state: dict[str, object]) -> tuple[object, object]:
    full = scheduler_module._candidate_state_decision(candidate, state)
    lean = scheduler_module._candidate_state_decision(candidate, state, event_evidence=False)

    assert full is not None and lean is not None
    assert full.evidence["pipeline_events"], "the state must carry at least one pipeline event"
    assert "pipeline_events" not in lean.evidence
    assert (lean.action, lean.reason) == (full.action, full.reason)
    assert lean.evidence == {key: value for key, value in full.evidence.items() if key != "pipeline_events"}
    return full, lean


@pytest.mark.parametrize(("build_state", "object_store", "expected_action", "expected_reason"), _LADDER_EXITS)
def test_every_ladder_exit_decides_the_same_without_the_event_evidence_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    build_state: Callable[[object], dict[str, object]],
    object_store: str | None,
    expected_action: str,
    expected_reason: str,
) -> None:
    _seed_object_store(monkeypatch, tmp_path, object_store)
    candidate = _scheduler_candidate_fixture()
    state = _candidate_state(candidate, build_state(candidate))

    full, _lean = _assert_parity(candidate, state)

    assert (full.action, full.reason) == (expected_action, expected_reason)


def test_legacy_non_authoritative_rows_decide_the_same_without_the_event_evidence_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed_object_store(monkeypatch, tmp_path, "recorded")
    candidate = _scheduler_candidate_fixture()
    state = _candidate_state(candidate, _nested_failed_task_state(candidate))

    full, lean = _assert_parity(candidate, state)

    validation = full.evidence["production_identity_validation"]
    assert "pipeline_events[0]" in validation["legacy_non_authoritative"]
    assert lean.evidence["production_identity_validation"] == validation


def test_the_default_evidence_is_unchanged_key_for_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The default keeps ``pipeline_events`` in its place, between the jobs and the hydro run."""

    _seed_object_store(monkeypatch, tmp_path, "recorded")
    candidate = _scheduler_candidate_fixture()
    state = _candidate_state(candidate, _nested_failed_task_state(candidate))

    evidence = scheduler_module._candidate_state_evidence(candidate, state)
    lean = scheduler_module._candidate_state_evidence(candidate, state, event_evidence=False)

    keys = list(evidence)
    assert keys[: keys.index("pipeline_events") + 2] == [
        "candidate_identity",
        "production_identity_validation",
        "pipeline_jobs",
        "pipeline_events",
        "hydro_run",
    ]
    assert evidence["pipeline_events"][0]["event_id"] == 103
    assert list(lean) == [key for key in keys if key != "pipeline_events"]
