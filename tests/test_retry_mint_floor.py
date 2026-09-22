"""#2404: a cycle-stage retry mints an attempt past the one the budget already charged.

The strict warm-start budget reads the candidate's stage-scoped attempt across
every run_id prefix (``_state_retry_attempt`` over the candidate-authoritative
rows), while the chain minted retry suffixes from the CURRENT run_id prefix only.
A prefix switch (``..._full_<model>`` -> ``..._forecast_<model>``, or a cohort
digest change) restarted the suffix and the budget stopped advancing.

Seam: the REAL scheduler candidate construction over a REAL file journal, handed
to the REAL forecast orchestrator (public ``orchestrate_cycle``) and reservation
path; only Slurm is faked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

_CYCLE_ID = "gfs_2026052100"
_FULL_RUN_ID = "cycle_gfs_2026052100_full_model_a"
_FORECAST_BASE = "job_cycle_gfs_2026052100_forecast_model_a_forecast"
_RETRY_DECISION = "retry_strict_warm_start_terminal_init_state_mismatch"
_BLOCKED_DECISION = "blocked_strict_warm_start_init_state_mismatch"


def _full_chain_row(job_suffix: str, *, slurm_job_id: str) -> dict[str, Any]:
    from tests.test_production_scheduler import _budget_full_chain_master_row

    return _budget_full_chain_master_row(job_suffix, slurm_job_id=slurm_job_id)


def _forecast_rows(root: Path) -> list[dict[str, Any]]:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    return [
        dict(row)
        for row in FileOrchestrationJournalRepository(root).query_pipeline_jobs_by_cycle(_CYCLE_ID)
        if row.get("stage") == "forecast"
    ]


def _rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    scheduler: Any,
    candidates: list[Any],
    **rerun_kwargs: Any,
) -> tuple[list[dict[str, Any]], Any]:
    """Hand ``candidates`` to the REAL orchestrator + reservation path; return (handoff basins, result).

    The handoff basins are the scheduler's own (``_execute_candidates_async``
    builds them through ``candidate_basin_manifest``), captured by a fake
    orchestrator and replayed through ``real_rerun``, which re-records each
    model's lead-12 token so the strict CONFLICT persists pass after pass.
    """

    from tests.test_operator_reentry_confirmation import real_rerun
    from tests.test_production_scheduler import FakeProductionOrchestrator

    capture = FakeProductionOrchestrator()
    scheduler.orchestrator_factory = lambda _source_id: capture
    scheduler._execute_candidates_async(candidates)
    (call,) = capture.calls
    basins = [dict(basin) for basin in call["basins"]]
    with monkeypatch.context() as patch:
        patch.delenv("NHMS_OBJECT_STORE_COPYBACK_ROOT")
        result = real_rerun(tmp_path, root, basins, terminal_stage="forecast", **rerun_kwargs)
    return basins, result


def _pass_decisions(scheduler: Any) -> tuple[list[Any], dict[str, tuple[str, Any]]]:
    """One scheduler pass: (retry candidates, model -> (decision, floor or blocked attempt))."""

    from tests.test_production_scheduler import _budget_pass

    _selected, candidates, blocked, _skipped = _budget_pass(scheduler)
    decisions: dict[str, tuple[str, Any]] = {}
    for item in candidates:
        decisions[item.model_id] = (item.state_evidence["decision"], item.state_evidence.get("retry_attempt_floor"))
    for item in blocked:
        decisions[item.model_id] = (item.state_evidence["decision"], item.state_evidence["retry_policy"]["attempt"])
    return candidates, decisions


def _new_forecast_ids(root: Path, before: set[str]) -> list[str]:
    return sorted({str(row["job_id"]) for row in _forecast_rows(root)} - before)


#: Top-level keys the db-free cycle chain already writes outside
#: ``run_manifest.schema.json`` (root ``additionalProperties: false``) before
#: #2404 -- a pre-existing gap, pinned so this change provably adds none.
_PRE_EXISTING_UNDECLARED_MANIFEST_KEYS = {
    "candidate_id",
    "display",
    "forecast_horizon_hours",
    "object_store_prefix",
    "object_store_root",
    "quality_states",
    "residual_blockers",
    "submission_attempt",
    "workspace_dir",
}


def _assert_run_manifests_carry_no_floor(tmp_path: Path) -> int:
    """No emitted run manifest gains a top-level key or carries the floor anywhere."""

    import json

    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "run_manifest.schema.json").read_text())
    assert schema["additionalProperties"] is False
    manifests = sorted((tmp_path / "rerun-workspaces").glob("*/object-store/runs/*/input/manifest.json"))
    for path in manifests:
        text = path.read_text(encoding="utf-8")
        assert set(json.loads(text)) - set(schema["properties"]) == _PRE_EXISTING_UNDECLARED_MANIFEST_KEYS
        assert "retry_attempt_floor" not in text
    return len(manifests)


def test_prefix_switch_does_not_reset_the_minted_attempt_and_the_budget_blocks_on_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3.1/3.2: attempt M=1 spent under ``_full_model_a``; retry_limit 2.

    The one remaining budgeted retry mints under ``_forecast_model_a`` with a
    suffix > M, and the very next pass blocks: exactly limit - M submissions.
    Pre-fix the rerun minted bare, then ``_retry_1``, then ``_retry_2`` -- two
    extra full SHUD forecasts before the budget noticed.
    """

    from tests.test_production_scheduler import _BUDGET_RETRY_LIMIT, _seed_budget_journal

    spent = [_full_chain_row("", slurm_job_id="100"), _full_chain_row("_retry_1", slurm_job_id="101")]
    root, scheduler = _seed_budget_journal(monkeypatch, tmp_path, spent)

    minted: list[str] = []
    trace: list[tuple[str, Any]] = []
    for _ in range(5):
        built = scheduler()
        candidates, decisions = _pass_decisions(built)
        trace.append(decisions["model_a"])
        if not candidates:
            break
        before = {str(row["job_id"]) for row in _forecast_rows(root)}
        basins, result = _rerun(tmp_path, monkeypatch, root, built, candidates)
        assert result.status == "succeeded"
        # Evidence-only carriage: nothing on the handoff can pin
        # ``context.retry_attempt`` (#1201 / #2393).
        from services.orchestrator.chain_runtime_utils import _retry_attempt_from_basins

        assert _retry_attempt_from_basins(basins) is None
        assert all("retry_attempt" not in basin and "manual_retry_attempt" not in basin for basin in basins)
        minted.extend(new for new in _new_forecast_ids(root, before) if new.startswith("job_cycle_"))

    assert minted == [f"{_FORECAST_BASE}_retry_2"]
    assert trace == [
        (_RETRY_DECISION, {"stage": "forecast", "attempt": 1}),
        (_BLOCKED_DECISION, _BUDGET_RETRY_LIMIT),
    ]
    assert _assert_run_manifests_carry_no_floor(tmp_path) == 1


# ---------------------------------------------------------------------------
# Cohort geometry (D4): ``..._forecast_cohort_<digest>`` run_ids
# ---------------------------------------------------------------------------


def _seed_cohort_budget_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    model_ids: tuple[str, ...],
    jobs: list[dict[str, Any]] | None = None,
) -> tuple[Path, Any]:
    """``_seed_budget_journal`` for several models: every model is a strict CONFLICT candidate.

    Returns ``(root, scheduler_factory)``; the factory takes the registry's model
    ids, so a pass can run a different cohort membership (a digest change).
    """

    import json
    import os

    from packages.common.object_store import LocalObjectStore
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from tests.test_production_scheduler import (
        _BUDGET_CYCLE,
        _BUDGET_RETRY_LIMIT,
        FakeRegistry,
        ProductionScheduler,
        _config,
        _dt,
        _model,
        _record_budget_attempt,
        _set_db_free_scheduler_env,
        _write_db_free_state_index_fixture,
    )
    from tests.test_scheduler_backfill import (
        _DB_FREE_PACKAGE_CHECKSUM,
        _db_free_state_index_entry,
        _gfs_adapter,
        _init_state_id_for,
        _seed_completed_journal_cycle,
    )

    root = tmp_path / "journal"
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path / "db-free-local-root")
    monkeypatch.setenv("NHMS_REQUIRE_FORECAST_WARM_START", "true")
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    # Distinct package checksums: two active models sharing one are excluded as
    # a duplicate model identity.
    checksums = {model_id: "sha256:" + format(index + 10, "x") * 64 for index, model_id in enumerate(model_ids)}
    entries = [
        {
            **_db_free_state_index_entry(
                roots, valid_time=_dt(valid), producer_cycle_time=_dt(producer), model_id=model_id
            ),
            "model_package_checksum": checksums[model_id],
        }
        for model_id in model_ids
        for valid, producer in ((_BUDGET_CYCLE, "2026-05-20T18:00:00Z"), ("2026-05-21T06:00:00Z", _BUDGET_CYCLE))
    ]
    _write_db_free_state_index_fixture(
        roots,
        paths,
        cycle_time=_dt(_BUDGET_CYCLE),
        package_checksum=_DB_FREE_PACKAGE_CHECKSUM,
        generated_at=_dt("2026-05-21T06:00:00Z"),
        entries=entries,
    )
    store = LocalObjectStore(Path(os.environ["OBJECT_STORE_ROOT"]), "s3://nhms")
    for model_id in model_ids:
        basin_id = f"basin_{model_id.removeprefix('model_')}"
        package_dir = f"forcing/gfs/2026052100/{basin_id}_v1/{model_id}"
        store.write_bytes_atomic(
            f"{package_dir}/forcing_package.json", b'{"schema_version": "nhms.forcing_package.v1"}'
        )
        store.write_bytes_atomic(
            f"{package_dir}/forcing_version_record.json",
            json.dumps(
                {
                    "forcing_package_uri": f"s3://nhms/{package_dir}/",
                    "forcing_version_id": f"forc_gfs_2026052100_{model_id}",
                }
            ).encode("utf-8"),
        )
        latest_path = _seed_completed_journal_cycle(
            root,
            cycle_time=_BUDGET_CYCLE,
            init_state_id=_init_state_id_for(_BUDGET_CYCLE, lead_hours=12, model_id=model_id),
            model_id=model_id,
            jobs=[],
        )
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        package_uri = f"s3://nhms/{package_dir}/forcing_package.json"
        latest["forcing_version"].update(
            {
                "forcing_version_id": f"forc_gfs_2026052100_{model_id}",
                "forcing_package_uri": package_uri,
                "forcing_package_manifest_uri": package_uri,
            }
        )
        latest_path.write_text(json.dumps(latest), encoding="utf-8")
    for job in jobs or []:
        _record_budget_attempt(root, job)

    def _profile(model_id: str) -> dict[str, Any]:
        return {
            "runnable": True,
            "memory_gb": 8,
            "display_capabilities": {"tiles": True},
            "package_checksum": checksums[model_id],
        }

    def _scheduler(members: tuple[str, ...] = model_ids) -> ProductionScheduler:
        return ProductionScheduler(
            _config(
                tmp_path,
                now=_dt("2026-05-21T06:00:00Z"),
                backfill_enabled=True,
                max_cycles_per_source=1,
                lookback_hours=12,
                retry_limit=_BUDGET_RETRY_LIMIT,
            ),
            registry=FakeRegistry(
                [
                    _model(model_id, f"basin_{model_id.removeprefix('model_')}", resource_profile=_profile(model_id))
                    for model_id in members
                ]
            ),
            adapters={"gfs": _gfs_adapter([_BUDGET_CYCLE])},
            active_repository=FileOrchestrationJournalRepository(root),
            orchestrator_factory=lambda _source_id: pytest.fail("candidate construction must not build orchestrator"),
        )

    return root, _scheduler


def test_cohort_digest_changes_do_not_escape_the_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """3.3 (D4): ``..._forecast_cohort_<digest>`` run_ids, with the digest changing between passes.

    Pre-fix evidence (this test against the pre-change source): every pass stayed
    ``retry`` and re-ran the whole cohort -- six cohort submissions over six passes
    at retry_limit 2 and never a block -- because the model-less cohort master
    proves no candidate authority (#1586) and the per-model reconciled rows carried
    no attempt.  Now each member's charged attempt is its master's, and every
    member stops at exactly ``retry_limit`` submissions.
    """

    from tests.test_production_scheduler import _BUDGET_RETRY_LIMIT

    models = ("model_a", "model_b", "model_c")
    root, scheduler = _seed_cohort_budget_journal(monkeypatch, tmp_path, models)
    memberships = [models, ("model_a", "model_b"), models, ("model_a", "model_c"), models]
    trace: list[dict[str, tuple[str, Any]]] = []
    minted: list[str] = []
    submissions = {model_id: 0 for model_id in models}
    for members in memberships:
        built = scheduler(members)
        candidates, decisions = _pass_decisions(built)
        trace.append(decisions)
        if not candidates:
            continue
        before = {str(row["job_id"]) for row in _forecast_rows(root)}
        _basins, result = _rerun(tmp_path, monkeypatch, root, built, candidates)
        assert result.status == "succeeded"
        minted.extend(new for new in _new_forecast_ids(root, before) if new.startswith("job_cycle_"))
        for candidate in candidates:
            submissions[candidate.model_id] += 1

    def _floor(attempt: int) -> tuple[str, dict[str, Any]]:
        return (_RETRY_DECISION, {"stage": "forecast", "attempt": attempt})

    blocked = (_BLOCKED_DECISION, _BUDGET_RETRY_LIMIT)
    assert trace == [
        {"model_a": _floor(0), "model_b": _floor(0), "model_c": _floor(0)},
        {"model_a": _floor(1), "model_b": _floor(1)},
        {"model_a": blocked, "model_b": blocked, "model_c": _floor(1)},
        {"model_a": blocked, "model_c": blocked},
        {"model_a": blocked, "model_b": blocked, "model_c": blocked},
    ]
    abc = _cohort_run_id(("model_a", "model_b", "model_c"))
    ab = _cohort_run_id(("model_a", "model_b"))
    assert abc != ab
    assert minted == [
        f"job_{abc}_forecast_retry_1",
        f"job_{ab}_forecast_retry_2",
        "job_cycle_gfs_2026052100_forecast_model_c_forecast_retry_2",
    ]
    assert submissions == {model_id: _BUDGET_RETRY_LIMIT for model_id in models}


def _cohort_run_id(model_ids: tuple[str, ...]) -> str:
    """Independent recomputation of the scheduler's cohort run_id (``scheduler_execution``)."""

    import hashlib

    identity = "\0".join(
        sorted(f"{model_id}\0gfs:2026-05-21T00:00:00Z:{model_id}:forecast_gfs_deterministic" for model_id in model_ids)
    )
    return f"cycle_gfs_2026052100_forecast_cohort_{hashlib.sha256(identity.encode()).hexdigest()[:12]}"


def test_sibling_model_retry_rows_do_not_raise_this_models_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.4 (#1845 still open): the floor is model-scoped, including cohort geometry.

    ``model_b`` has spent attempts under its own single-model prefix, and a
    sibling cohort it alone belonged to reached ``_retry_7``.  ``model_a`` has
    spent nothing; its floor stays 0 and its retry mints at ``_retry_1``.
    """

    from tests.test_production_scheduler import _budget_full_chain_master_row

    sibling_full = [
        {
            **_budget_full_chain_master_row(suffix, slurm_job_id=slurm),
            "job_id": f"job_cycle_gfs_2026052100_full_model_b_forecast{suffix}",
            "run_id": "cycle_gfs_2026052100_full_model_b",
        }
        for suffix, slurm in (("", "300"), ("_retry_1", "301"), ("_retry_1_retry_2", "302"))
    ]
    sibling_cohort = {
        **_budget_full_chain_master_row("", slurm_job_id="307"),
        "job_id": "job_cycle_gfs_2026052100_forecast_cohort_0123456789ab_forecast_retry_7",
        "run_id": "cycle_gfs_2026052100_forecast_cohort_0123456789ab",
    }
    sibling_member = {
        "job_id": "job_fcst_gfs_2026052100_model_b_forecast_reconciled_307_0",
        "run_id": "fcst_gfs_2026052100_model_b",
        "cycle_id": _CYCLE_ID,
        "model_id": "model_b",
        "candidate_id": "gfs:2026-05-21T00:00:00Z:model_b:forecast_gfs_deterministic",
        "stage": "forecast",
        "job_type": "run_shud_forecast_array",
        "status": "succeeded",
        "slurm_job_id": "307_0",
        "retry_count": 7,
    }
    root, scheduler = _seed_cohort_budget_journal(
        monkeypatch, tmp_path, ("model_a", "model_b"), [*sibling_full, sibling_cohort, sibling_member]
    )

    built = scheduler(("model_a",))
    candidates, decisions = _pass_decisions(built)
    assert decisions == {"model_a": (_RETRY_DECISION, {"stage": "forecast", "attempt": 0})}
    before = {str(row["job_id"]) for row in _forecast_rows(root)}
    _basins, result = _rerun(tmp_path, monkeypatch, root, built, candidates)
    assert result.status == "succeeded"
    assert [new for new in _new_forecast_ids(root, before) if new.startswith("job_cycle_")] == [
        f"{_FORECAST_BASE}_retry_1"
    ]
    # The sibling's own budget read is untouched by model_a's retry.
    _candidates, sibling = _pass_decisions(scheduler(("model_b",)))
    assert sibling == {"model_b": (_BLOCKED_DECISION, 7)}


def test_inline_auto_retry_stacks_on_the_floored_id_and_never_under_mints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.3 auto-retry minter audit: the in-invocation retry derives from the id just submitted.

    Production wires a ``FileJournalRetryService`` into the chain.  Its current
    master path (``_next_current_master_retry_identity``) and the chain's
    ``_schedule_cycle_stage_retry`` both take the FAILED row's own suffix + 1 on
    the same base, so after a floored ``_retry_2`` fails the next attempt is
    ``_retry_3`` -- never a restart under another prefix.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileJournalRetryService,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.retry import RetryConfig
    from tests.test_orchestration_chain import FakeCycleSlurmClient
    from tests.test_production_scheduler import _seed_budget_journal

    spent = [_full_chain_row("", slurm_job_id="100"), _full_chain_row("_retry_1", slurm_job_id="101")]
    root, scheduler = _seed_budget_journal(monkeypatch, tmp_path, spent)
    built = scheduler()
    candidates, _decisions = _pass_decisions(built)
    client = FakeCycleSlurmClient(fail_stage="forecast", array_results_by_stage={"forecast": ["failed"]})
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
    masters = [new for new in _new_forecast_ids(root, before) if new.startswith("job_cycle_")]
    assert masters == [f"{_FORECAST_BASE}_retry_2", f"{_FORECAST_BASE}_retry_3"]


def test_floor_derived_id_already_in_the_journal_keeps_the_minter_advancing(tmp_path: Path) -> None:
    """3.5 / #1201: a floor-derived id that is already occupied is skipped, never pinned.

    Public ``orchestrate_cycle``.  The run's own history is one terminal bare
    row, so the prefix-scoped mint is ``_retry_1``; the evidence floor (1) raises
    it to ``_retry_2`` -- which a terminal, Slurm-bound legacy row outside this
    run's query already holds.  The minter moves on to ``_retry_3`` and the
    stage really submits (no ``skipped_duplicate_submission`` wedge).
    """

    from tests.test_orchestration_chain import (
        FakeCycleSlurmClient,
        StoreBackedCycleRepository,
        _basins,
        _orchestrator,
        _pipeline_store,
    )

    run_id = "cycle_gfs_2026050100_forecast_model_0"
    base = f"job_{run_id}_forecast"
    store = _pipeline_store()
    for job_id, row_run_id, slurm_job_id in ((base, run_id, "6051"), (f"{base}_retry_2", "legacy_run", "6053")):
        job = store.create_job(
            job_id=job_id,
            run_id=row_run_id,
            cycle_id="gfs_2026050100",
            job_type="run_shud_forecast_array",
            slurm_job_id=slurm_job_id,
            model_id="model_0",
            stage="forecast",
            status="failed",
            idempotency_key=f"{row_run_id}:{job_id}",
        )
        job.error_code = "SLURM_JOB_FAILED"
        store.session.add(job)
    store.session.commit()
    repository = StoreBackedCycleRepository(store)
    client = FakeCycleSlurmClient()
    basins = _basins(1)
    basins[0].update(
        {
            "orchestration_run_id": run_id,
            "restart_stage": "forecast",
            "state_evidence": {
                "decision": _RETRY_DECISION,
                "restart_stage": "forecast",
                "retry_attempt_floor": {"stage": "forecast", "attempt": 1},
            },
        }
    )

    result = _orchestrator(tmp_path, repository, client).orchestrate_cycle("gfs", "2026050100", basins)

    forecast = result.stages[0]
    assert (forecast.stage, forecast.pipeline_job_id, forecast.status) == ("forecast", f"{base}_retry_3", "succeeded")
    assert "skipped_duplicate_submission" not in {stage.status for stage in result.stages}
    occupied = store.get_job(f"{base}_retry_2")
    assert (occupied.slurm_job_id, occupied.status) == ("6053", "failed")


def test_mixed_floor_cohort_mints_past_every_member_and_charges_the_shared_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cohort shares ONE master id, so it mints past its highest member floor.

    ``model_b`` spent attempt 1 under its own ``_full_model_b`` prefix,
    ``model_a`` nothing.  The cohort mints ``_retry_2`` (b's floor + 1), and each
    member's reconciled row is charged that shared attempt -- ``model_a`` jumps
    0 -> 2 on one submission.  Intended, conservative over-charge: the budget
    can only block earlier, never admit a submission past ``retry_limit``.
    """

    from tests.test_production_scheduler import _BUDGET_RETRY_LIMIT, _budget_full_chain_master_row

    spent_b = [
        {
            **_budget_full_chain_master_row(suffix, slurm_job_id=slurm),
            "job_id": f"job_cycle_gfs_2026052100_full_model_b_forecast{suffix}",
            "run_id": "cycle_gfs_2026052100_full_model_b",
        }
        for suffix, slurm in (("", "400"), ("_retry_1", "401"))
    ]
    models = ("model_a", "model_b")
    root, scheduler = _seed_cohort_budget_journal(monkeypatch, tmp_path, models, spent_b)

    built = scheduler(models)
    candidates, decisions = _pass_decisions(built)
    assert decisions == {
        "model_a": (_RETRY_DECISION, {"stage": "forecast", "attempt": 0}),
        "model_b": (_RETRY_DECISION, {"stage": "forecast", "attempt": 1}),
    }
    before = {str(row["job_id"]) for row in _forecast_rows(root)}
    _basins, result = _rerun(tmp_path, monkeypatch, root, built, candidates)
    assert result.status == "succeeded"
    assert [new for new in _new_forecast_ids(root, before) if new.startswith("job_cycle_")] == [
        f"job_{_cohort_run_id(models)}_forecast_retry_2"
    ]

    _candidates, after = _pass_decisions(scheduler(models))
    blocked = (_BLOCKED_DECISION, _BUDGET_RETRY_LIMIT)
    assert after == {"model_a": blocked, "model_b": blocked}
