"""#2546: the model-less cohort ``convert`` row records its members.

Production shape (#2543/#2544): one cycle's restart-compatible candidates are split by
gateway resource profile into a multi-member default execution cohort (model-less rows
under ``cycle_<src>_<stamp>_full_cohort_<12hex>``) and one-member override units
(``cycle_<src>_<stamp>_full_<model>``, whose rows name their model).  The file journal's
``has_active_pipeline`` excludes a sibling cohort's model-less row only by that row's
recorded ``cohort_members``; before #2546 the convert row recorded none, so while the
default cohort's convert ran, the override unit's conflict check raised
``PIPELINE_ALREADY_ACTIVE``.

Seams: the real ``orchestrate_cycle`` stage loop (two orchestrators, one per execution
unit, as the scheduler runs them) against one real ``FileOrchestrationJournalRepository``
root; cohort run ids come from the scheduler's own ``_candidate_execution_cohort_run_id``
helpers.  Only the Slurm runtime is a double.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import scheduler as scheduler_module
from services.orchestrator.accepted_submit_identity import accepted_submit_row_kind
from services.orchestrator.file_orchestration_journal import (
    FileOrchestrationJournalRepository,
    _reconcile_inventory_row_kind,
)
from services.orchestrator.forcing_submit_identity import forcing_member_identity_is_complete
from services.orchestrator.scheduler_state_types import ACTIVE_PIPELINE_STATUSES
from tests.test_orchestration_chain import FakeCycleSlurmClient, _orchestrator

_CYCLE = "2026050100"
_CYCLE_ID = f"gfs_{_CYCLE}"
_CYCLE_TIME = datetime(2026, 5, 1, tzinfo=UTC)
_FULL = scheduler_module._candidate_restart_cohort_key(None)


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


_DEFAULT = [_candidate(index) for index in range(3)]
_OVERRIDE = _candidate(3)


def _unit_run_id(candidates: Sequence[Any]) -> str:
    """The scheduler's own execution-unit run id (never hand-written)."""

    if len(candidates) == 1:
        return scheduler_module._candidate_execution_cohort_run_id_for_candidate(
            "gfs", _CYCLE_TIME, _FULL, candidates[0]
        )
    return scheduler_module._candidate_execution_cohort_run_id("gfs", _CYCLE_TIME, _FULL, list(candidates))


def _unit_basins(candidates: Sequence[Any]) -> list[dict[str, Any]]:
    run_id = _unit_run_id(candidates)
    basins = []
    for candidate in candidates:
        index = int(candidate.model_id.rsplit("_", 1)[1])
        basins.append(
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
                "init_state_id": f"state_gfs_{candidate.model_id}_{_CYCLE}_gfs_2026043012_f012",
                "init_state_uri": f"s3://nhms/states/gfs/{candidate.model_id}/{_CYCLE}/state.cfg.ic",
                "init_state_checksum": f"sha256:state-{index}",
                "init_state_valid_time": "2026-05-01T00:00:00Z",
            }
        )
    return basins


class _RuntimeWithHook(FakeCycleSlurmClient):
    """Slurm double that runs ``hook`` once, while ``stage`` is RUNNING on this client."""

    def __init__(self, *, stage: str, hook: Callable[[], Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.hook_stage = stage
        self.hook = hook
        self.hook_result: Any = None

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        # Function-local: ``chain_types`` is stop-rule owned in scripts/select_ci_tests.py.
        from services.orchestrator.chain_types import OrchestratorError

        status = super().get_job_status(job_id)
        if self.hook is not None and status["stage"] == self.hook_stage and status["status"] == "running":
            hook, self.hook = self.hook, None
            try:
                self.hook_result = ("returned", hook())
            except OrchestratorError as error:
                self.hook_result = ("raised", error.error_code)
            except BaseException as error:  # surfaced by the test, never swallowed by the poll loop
                import traceback
                self.hook_result = ("crashed", "".join(traceback.format_exception(error)))
        return status


def _run_unit(root: Path, workspace: Path, client: Any, candidates: Sequence[Any]) -> Any:
    orchestrator = _orchestrator(workspace, FileOrchestrationJournalRepository(root), client, terminal_stage="forecast")
    return orchestrator.orchestrate_cycle("gfs", _CYCLE, _unit_basins(candidates))


def _convert_rows(root: Path) -> dict[str, dict[str, Any]]:
    return {
        str(row["job_id"]): dict(row)
        for row in FileOrchestrationJournalRepository(root).query_pipeline_jobs_by_cycle(_CYCLE_ID)
        if row.get("stage") == "convert"
    }


def _while_default_convert_runs(tmp_path: Path, nested: Callable[[Path], Any]) -> tuple[Any, Any]:
    """Run the default cohort; while its convert row is active, run ``nested(root)``."""

    root = tmp_path / "journal"

    def _hook() -> Any:
        (active,) = [row for row in _convert_rows(root).values() if row["run_id"] == _unit_run_id(_DEFAULT)]
        assert active["status"] in ACTIVE_PIPELINE_STATUSES
        assert active["model_id"] in (None, "")
        return nested(root)

    client = _RuntimeWithHook(stage="convert", hook=_hook)
    result = _run_unit(root, tmp_path / "default", client, _DEFAULT)
    assert client.hook is None, "the nested unit never ran"
    return result, client.hook_result


def test_override_unit_is_not_blocked_by_the_default_cohorts_active_convert_row(tmp_path: Path) -> None:
    def _override(root: Path) -> Any:
        return _run_unit(root, tmp_path / "override", FakeCycleSlurmClient(), [_OVERRIDE])

    default_result, (outcome, override_result) = _while_default_convert_runs(tmp_path, _override)

    assert outcome == "returned", override_result
    assert override_result.status == "succeeded"
    assert default_result.status == "succeeded"
    rows = _convert_rows(tmp_path / "journal")
    default_convert = rows[f"job_{_unit_run_id(_DEFAULT)}_convert"]
    assert [member["model_id"] for member in default_convert["cohort_members"]] == ["model_0", "model_1", "model_2"]
    assert {member["restart_stage"] for member in default_convert["cohort_members"]} == {"convert"}
    override_convert = rows[f"job_{_unit_run_id([_OVERRIDE])}_convert"]
    assert override_convert["model_id"] == "model_3"


@pytest.mark.parametrize("duplicate", ["same_cohort", "member_override"])
def test_a_true_duplicate_is_still_refused(tmp_path: Path, duplicate: str) -> None:
    """Pin: a unit sharing a member with the active default cohort still conflicts."""

    candidates = _DEFAULT if duplicate == "same_cohort" else [_DEFAULT[1]]

    def _duplicate(root: Path) -> Any:
        return _run_unit(root, tmp_path / "duplicate", FakeCycleSlurmClient(), candidates)

    _default_result, hook_result = _while_default_convert_runs(tmp_path, _duplicate)

    assert hook_result == ("raised", "PIPELINE_ALREADY_ACTIVE")


def test_a_convert_row_with_members_is_not_accepted_submit_identity(tmp_path: Path) -> None:
    """The member list rides a plain row: no master marker, no forcing member identity."""

    root = tmp_path / "journal"
    _run_unit(root, tmp_path / "default", FakeCycleSlurmClient(), _DEFAULT)

    row = _convert_rows(root)[f"job_{_unit_run_id(_DEFAULT)}_convert"]
    assert [member["model_id"] for member in row["cohort_members"]] == ["model_0", "model_1", "model_2"]
    for marker in ("accepted_submit_contract_version", "cohort_digest", "expected_slurm_user", "slurm_comment"):
        assert row.get(marker) in (None, ""), marker
    assert accepted_submit_row_kind(row) is None
    assert _reconcile_inventory_row_kind(row) == "legacy"
    assert not forcing_member_identity_is_complete(row)


def _decision(root: Path, candidate: Any) -> Any:
    state = FileOrchestrationJournalRepository(root).candidate_state(
        source_id="gfs",
        cycle_time=_CYCLE_TIME,
        model_id=candidate.model_id,
        run_id=candidate.run_id,
        forcing_version_id=candidate.forcing_version_id,
        candidate_id=candidate.candidate_id,
        retry_limit=1,
    )
    assert state is not None
    return scheduler_module._candidate_state_decision(candidate, state), state


def test_a_sibling_cohorts_convert_failure_does_not_reach_the_override_candidate(tmp_path: Path) -> None:
    """The failed default-cohort convert row is ``non_member`` for the override and is dropped.

    On origin it was ``unwitnessed`` (no list) and stayed in the override's candidate
    state; the decision itself (``skip`` on its own terminal success) is unchanged.
    """

    root = tmp_path / "journal"
    failed = _run_unit(root, tmp_path / "default", FakeCycleSlurmClient(fail_stage="convert"), _DEFAULT)
    assert [(stage.stage, stage.status) for stage in failed.stages] == [("convert", "failed")]
    override = _run_unit(root, tmp_path / "override", FakeCycleSlurmClient(), [_OVERRIDE])
    assert override.status == "succeeded"

    decision, state = _decision(root, _OVERRIDE)

    assert decision.action == "skip"
    classes = {
        str(job["job_id"]): job.get("cohort_membership")
        for job in state["pipeline_jobs"]
        if job.get("model_id") in (None, "")
    }
    assert f"job_{_unit_run_id(_DEFAULT)}_convert" not in classes
    # ``convert`` is not a cohort-member-attributed stage, so for the default cohort's own
    # members a ``member`` convert row keeps its origin (unattributed) meaning: no state
    # decision, exactly as captured on origin/master (9facc5b94) with this fixture.
    for member in _DEFAULT:
        member_decision, member_state = _decision(root, member)
        assert member_decision is None
        assert {
            str(job["job_id"]): job.get("cohort_membership")
            for job in member_state["pipeline_jobs"]
            if job.get("model_id") in (None, "")
        } == {f"job_{_unit_run_id(_DEFAULT)}_convert": "member"}
