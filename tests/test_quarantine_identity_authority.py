"""#2397: §8.7 identity authority prefers the newer of the completed hydro_run and the master.

Every journal here is written through the REAL lifecycle -- the real scheduler
pass, the real forecast orchestrator + reservation path (``orchestrate_cycle``,
which calls ``create_hydro_run_from_basin``) and the real reconcile terminal
writes; only Slurm and the object store are faked.  No hydro_run row is
hand-written.

A same-run_id corrective rerun cannot rewrite the already-succeeded hydro_run
row (retriable-only write path), so its recorded identity stays the first run's
token.  The accessor therefore compares the hydro row's ``created_at`` against
the ``created_at`` (accepted-submit time) of the newest accepted-submit master:
both are set once and never refreshed by ``update_hydro_run_status`` or any
other post-hoc rewrite, which only touch ``updated_at``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import scheduler_candidates as scheduler_candidates_module
from services.orchestrator import scheduler_discovery as scheduler_discovery_module
from services.orchestrator.file_orchestration_journal import (
    FileOrchestrationJournalRepository,
    accepted_submit_row_kind,
)
from services.orchestrator.scheduler_state_types import CandidateStateDecision

QUARANTINE_DECISION = "retry_journal_predecessor_identity_mismatch"
RUN_ID = "fcst_gfs_2026052100_model_a"


def _stale() -> str:
    from tests.test_operator_reentry_confirmation import stale_token

    return stale_token()


def _correct() -> str:
    """The lineage a converged rerun records: cadence 0/6/12/18 means a 6h lead."""
    from tests.test_operator_reentry_confirmation import stale_token

    return stale_token(lead_hours=6)


def _cycle() -> Any:
    from tests.test_operator_reentry_confirmation import BREAKER_CYCLE, _dt

    return _dt(BREAKER_CYCLE)


def _query() -> dict[str, Any]:
    return {"source_id": "gfs", "cycle_time": _cycle(), "model_id": "model_a"}


def _identity(root: Path) -> dict[str, Any] | None:
    return FileOrchestrationJournalRepository(root).completed_pipeline_init_state_identity(**_query())


def _hydro(root: Path) -> dict[str, Any]:
    rows = FileOrchestrationJournalRepository(root)._cycle_rows(
        source_id="gfs", cycle_time=_cycle(), model_id="model_a"
    )
    assert rows.hydro_run is not None
    return dict(rows.hydro_run)


def _masters(root: Path) -> dict[str, dict[str, Any]]:
    rows = FileOrchestrationJournalRepository(root)._cycle_rows(
        source_id="gfs", cycle_time=_cycle(), model_id="model_a"
    )
    return {
        job_id: dict(job)
        for job_id, job in rows.pipeline_jobs.items()
        if job.get("stage") == "forecast" and accepted_submit_row_kind(job) == "master"
    }


def _pass(tmp_path: Path, root: Path) -> tuple[Any, Any]:
    from tests.test_operator_reentry_confirmation import breaker_scheduler
    from tests.test_production_scheduler import FakeProductionOrchestrator

    orchestrator = FakeProductionOrchestrator()
    return breaker_scheduler(tmp_path, root, orchestrator).run_once(), orchestrator


def _decisions(result: Any) -> list[tuple[str, str | None]]:
    return [
        (key, (item.get("state_evidence") or {}).get("decision"))
        for key in ("candidates", "blocked_candidates", "skipped_candidates")
        for item in result.evidence.get(key) or []
    ]


def _quarantine_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: str,
    slurm_client: Any | None = None,
) -> tuple[Path, str]:
    """Run 1 records the stale X; the §8.7 quarantine rerun (same run_id) records ``token``."""
    from tests.test_operator_reentry_confirmation import real_rerun, seed_breaker_journal

    root = seed_breaker_journal(tmp_path, monkeypatch, breaker_engaged=False)
    # Ordinary single run: the completed hydro row keeps priority, shape unchanged.
    assert _identity(root) == {"init_state_id": _stale()}
    result, orchestrator = _pass(tmp_path, root)
    assert _decisions(result) == [("candidates", QUARANTINE_DECISION)]
    (call,) = orchestrator.calls
    basins = [dict(basin) for basin in call["basins"]]
    assert {basin["run_id"] for basin in basins} == {RUN_ID}
    status = real_rerun(
        tmp_path, root, basins, recorded_tokens={"model_a": token}, slurm_client=slurm_client
    ).status
    return root, status


def _harness(tmp_path: Path, root: Path) -> tuple[Any, Any, Any, Any, Any]:
    from tests.test_file_orchestration_journal import _discovery_and_quarantine_harness

    return _discovery_and_quarantine_harness(
        tmp_path / "harness",
        active_repository=FileOrchestrationJournalRepository(root),
        cycle_time=_cycle(),
        stamp="2026052100",
        base_lead=6,
    )


def _stale_tokens(tmp_path: Path, root: Path) -> tuple[str, str] | None:
    discovery, candidate, _source_cycle, discovery_context, _construction = _harness(tmp_path, root)
    return scheduler_discovery_module._journal_predecessor_identity_stale_tokens(
        discovery_context, discovery, candidate, horizon={}
    )


def _quarantine(tmp_path: Path, root: Path) -> CandidateStateDecision | None:
    _discovery, candidate, source_cycle, _discovery_context, construction = _harness(tmp_path, root)
    return scheduler_candidates_module._journal_predecessor_identity_quarantine(
        construction,
        candidate,
        source_cycle,
        CandidateStateDecision(
            "skip",
            "terminal_completed_cycle",
            {"terminal_source": "pipeline_job", "terminal_status": "succeeded"},
        ),
    )


# ---------------------------------------------------------------------------
# 2.1 / 2.2 -- a same-run_id corrective rerun converges
# ---------------------------------------------------------------------------


def test_same_run_id_corrective_rerun_resolves_to_the_rerun_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    x, y = _stale(), _correct()
    assert x != y
    root, status = _quarantine_rerun(tmp_path, monkeypatch, token=y)
    assert status == "complete"

    # Write path stays retriable-only: the succeeded hydro row still records X.
    hydro = _hydro(root)
    assert hydro["run_id"] == RUN_ID
    assert hydro["status"] == "succeeded"
    assert hydro["init_state_id"] == x
    identity = _identity(root)
    assert identity is not None and identity["init_state_id"] == y, identity
    assert identity["model_id"] == "model_a"
    assert FileOrchestrationJournalRepository(root).completed_pipeline_init_state_id(**_query()) == y


def test_comparison_keys_survive_update_hydro_run_status_and_later_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    y = _correct()
    root, status = _quarantine_rerun(tmp_path, monkeypatch, token=y)
    assert status == "complete"
    hydro = _hydro(root)
    masters = _masters(root)
    rerun_master = max(masters.values(), key=lambda job: job["created_at"])
    assert rerun_master["journal_predecessor_quarantine_rerun_model_ids"] == ["model_a"]
    assert rerun_master["created_at"] > hydro["created_at"]

    # The real post-hoc hydro writer refreshes updated_at (and would out-date the
    # rerun master on that key) but never created_at.
    FileOrchestrationJournalRepository(root).update_hydro_run_status(RUN_ID, "succeeded")
    refreshed = _hydro(root)
    assert refreshed["updated_at"] > rerun_master["updated_at"]
    assert refreshed["created_at"] == hydro["created_at"]
    assert _identity(root)["init_state_id"] == y  # type: ignore[index]

    # A further real scheduler pass writes nothing that moves either key.
    result, orchestrator = _pass(tmp_path, root)
    assert orchestrator.calls == []
    assert _hydro(root)["created_at"] == hydro["created_at"]
    assert {job_id: job["created_at"] for job_id, job in _masters(root).items()} == {
        job_id: job["created_at"] for job_id, job in masters.items()
    }
    assert _identity(root)["init_state_id"] == y  # type: ignore[index]


def test_failed_rerun_does_not_override_the_completed_hydro_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_scheduler_terminal_recency import _FORECAST_FAILURE, _wallclock_slurm_client

    root, status = _quarantine_rerun(
        tmp_path, monkeypatch, token=_correct(), slurm_client=_wallclock_slurm_client(**_FORECAST_FAILURE)
    )
    assert status == "failed"
    # Only terminal-success masters with a self-bound identity compete (IS-01);
    # the failed rerun does not qualify and no earlier qualifying success is
    # newer than the completed hydro row, so the hydro row keeps priority.
    assert _identity(root) == {"init_state_id": _stale()}


def test_later_failed_rerun_does_not_hide_the_newest_converged_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IS-01: qualify first, then take the newest -- a later failure must not unseat Y with X."""
    from tests.test_operator_reentry_confirmation import real_rerun, seed_breaker_journal
    from tests.test_scheduler_terminal_recency import _FORECAST_FAILURE, _wallclock_slurm_client

    x, y = _stale(), _correct()
    root = seed_breaker_journal(tmp_path, monkeypatch, breaker_engaged=False)
    result, orchestrator = _pass(tmp_path, root)
    assert _decisions(result) == [("candidates", QUARANTINE_DECISION)]
    (call,) = orchestrator.calls
    basins = [dict(basin) for basin in call["basins"]]
    assert real_rerun(tmp_path, root, basins, recorded_tokens={"model_a": y}).status == "complete"
    assert _identity(root)["init_state_id"] == y  # type: ignore[index]

    failed = real_rerun(
        tmp_path,
        root,
        basins,
        recorded_tokens={"model_a": y},
        slurm_client=_wallclock_slurm_client(**_FORECAST_FAILURE),
    )
    assert failed.status == "failed"
    masters = sorted(_masters(root).values(), key=lambda job: job["created_at"])
    assert masters[-1]["status"] != "succeeded"
    identity = _identity(root)
    assert identity is not None and identity["init_state_id"] == y, (identity, x)


def test_sibling_model_later_submission_does_not_hide_the_converged_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cohort masters are model-less: only submissions carrying THIS model compete."""
    from tests.test_operator_reentry_confirmation import (
        breaker_scheduler,
        real_rerun,
        seed_breaker_journal,
        stale_token,
    )
    from tests.test_production_scheduler import FakeProductionOrchestrator

    models = ("model_a", "model_b")
    correct = {model_id: stale_token(model_id, lead_hours=6) for model_id in models}
    root = seed_breaker_journal(tmp_path, monkeypatch, model_ids=models, breaker_engaged=False)
    orchestrator = FakeProductionOrchestrator()
    breaker_scheduler(tmp_path, root, orchestrator, model_ids=models).run_once()
    (call,) = orchestrator.calls
    basins = [dict(basin) for basin in call["basins"]]
    assert sorted(basin["model_id"] for basin in basins) == list(models)
    assert real_rerun(tmp_path, root, basins, recorded_tokens=correct).status == "complete"
    assert _identity(root)["init_state_id"] == correct["model_a"]  # type: ignore[index]

    # A later submission for model_b alone (newest master of the cycle).
    model_b_only = [basin for basin in basins if basin["model_id"] == "model_b"]
    assert real_rerun(tmp_path, root, model_b_only, recorded_tokens=correct).status == "complete"
    newest = max(_masters(root).values(), key=lambda job: job["created_at"])
    assert [entry["model_id"] for entry in newest["init_state_identities"]] == ["model_b"]

    assert _identity(root)["init_state_id"] == correct["model_a"]  # type: ignore[index]
    assert _quarantine(tmp_path, root) is None


# ---------------------------------------------------------------------------
# 2.3 -- quarantine convergence and the breaker
# ---------------------------------------------------------------------------


def test_converged_rerun_clears_the_quarantine_without_engaging_the_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_operator_reentry_confirmation import _breaker_candidate_decisions

    x, y = _stale(), _correct()
    root, status = _quarantine_rerun(tmp_path, monkeypatch, token=y)
    assert status == "complete"
    repository = FileOrchestrationJournalRepository(root)

    assert _quarantine(tmp_path, root) is None
    # Lineage: exactly one completed stamped quarantine rerun, and it recorded Y.
    assert repository.quarantine_rerun_count(**_query()) == 1
    assert repository.completed_pipeline_init_state_id_occurrences(**_query(), init_state_id=y) == 1
    assert repository.completed_pipeline_init_state_id_occurrences(**_query(), init_state_id=x) == 0

    candidates, blocked = _breaker_candidate_decisions(tmp_path, root)
    assert (candidates, blocked) == ([], [])

    result, orchestrator = _pass(tmp_path, root)
    assert orchestrator.calls == []
    decisions = [decision for _key, decision in _decisions(result)]
    assert QUARANTINE_DECISION not in decisions
    assert "blocked_journal_predecessor_identity_quarantine" not in decisions
    assert result.evidence["counts"]["submitted_count"] == 0


def test_rerun_recording_the_stale_token_still_engages_the_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_operator_reentry_confirmation import (
        BREAKER_DECISION,
        _breaker_candidate_decisions,
        seed_breaker_journal,
    )

    x = _stale()
    root = seed_breaker_journal(tmp_path, monkeypatch, breaker_engaged=True)
    assert _identity(root)["init_state_id"] == x  # type: ignore[index]
    assert FileOrchestrationJournalRepository(root).completed_pipeline_init_state_id_occurrences(
        **_query(), init_state_id=x
    ) == 1

    quarantine = _quarantine(tmp_path, root)
    assert quarantine is not None
    assert quarantine.action == "blocked"
    assert quarantine.reason == "journal_predecessor_identity_quarantine_breaker_engaged"

    # Candidate side through ``_build_candidates`` (run_once releases the slot of
    # a breaker-engaged cycle before candidate construction).
    candidates, blocked = _breaker_candidate_decisions(tmp_path, root)
    assert candidates == []
    (entry,) = blocked
    assert entry.state_evidence["decision"] == BREAKER_DECISION
    assert entry.state_evidence["journal_predecessor_identity"]["occurrences"] == 1


# ---------------------------------------------------------------------------
# 2.4 -- legacy shape and the discovery-side §8.7 scoring
# ---------------------------------------------------------------------------


def test_legacy_hydro_only_identity_is_unchanged(tmp_path: Path) -> None:
    """No accepted-submit master at all: the completed hydro row's identity decides."""
    from tests.test_production_scheduler import _dt

    cycle_time = _dt("2026-06-28T00:00:00Z")
    recorded = "state_gfs_model_a_2026062800_gfs_2026062718_f006"
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    run = repository.create_hydro_run_from_basin(
        {"source_id": "gfs"},
        {
            "run_id": "fcst_gfs_2026062800_model_a",
            "run_type": "forecast",
            "scenario_id": "scenario_a",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "model": {"model_id": "model_a", "basin_version_id": "basin_version_a"},
            "initial_state": {"state_id": recorded, "quality": "fresh"},
            "outputs": {"run_manifest_uri": "s3://nhms/manifests/run.json"},
        },
    )
    repository.update_hydro_run_status(run["run_id"], "succeeded", slurm_job_id="3001")

    identity = FileOrchestrationJournalRepository(tmp_path / "journal").completed_pipeline_init_state_identity(
        source_id="gfs", cycle_time=cycle_time, model_id="model_a"
    )

    assert identity == {"init_state_id": recorded}


def test_discovery_scoring_reads_the_converged_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_operator_reentry_confirmation import seed_breaker_journal

    x, y = _stale(), _correct()
    before = seed_breaker_journal(tmp_path / "before", monkeypatch, breaker_engaged=False)
    assert _stale_tokens(tmp_path / "before", before) == (x, y)

    root, status = _quarantine_rerun(tmp_path / "after", monkeypatch, token=y)
    assert status == "complete"
    assert _stale_tokens(tmp_path / "after", root) is None
