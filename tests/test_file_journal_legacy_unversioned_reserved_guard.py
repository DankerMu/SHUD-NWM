"""#2674: no journal writer persists held shape (c).

Shape (c) is a ``reserved`` forecast cohort master (``stage``, or ``job_type``
when ``stage`` is empty, is a forecast alias) with non-empty ``cohort_members``
and no current accepted-submit contract marker.  Restart reconcile can only
report it ``legacy_unversioned_read_only`` and the operator bind refuses it, so
every writer refuses it with ``file_journal_legacy_unversioned_reserved_forecast_master``
and writes zero bytes (E1-E7), legitimate writes are unchanged (E9), the only
production minter of forecast cohort reservations stamps the contract (E10),
and a pre-existing row keeps its fail-closed handling (E11).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    FORECAST_COHORT_STAGE_ALIASES,
    accepted_submit_contract_is_current,
    accepted_submit_row_kind,
    canonical_forecast_cohort_members,
    forecast_cohort_digest,
)
from services.orchestrator.file_orchestration_journal import (
    FileJournalRetryService,
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.file_orchestration_migration import import_historical_scheduler_state
from tests.file_journal_legacy_seed_helpers import seed_pre_existing_legacy_row

REFUSAL = "file_journal_legacy_unversioned_reserved_forecast_master"
CYCLE_TIME = datetime(2026, 7, 12, tzinfo=UTC)
CYCLE_ID = "gfs_2026071200"
RUN_ID = "cycle_gfs_2026071200_forecast_legacy"
JOB_ID = "job_cycle_gfs_2026071200_forecast_legacy_forecast"
KEY = "cycle_gfs_2026071200_forecast_legacy:forecast"


def _members(count: int = 2) -> list[dict[str, Any]]:
    return [
        {
            "array_task_id": index,
            "candidate_id": f"GFS:2026-07-12T00:00:00Z:model_{index}:forecast_gfs_deterministic",
            "run_id": f"fcst_gfs_2026071200_model_{index}",
            "model_id": f"model_{index}",
            "basin_id": f"basin_{index}",
            "scenario_id": "forecast_gfs_deterministic",
            "restart_stage": "forecast",
        }
        for index in range(count)
    ]


def _legacy_master(**overrides: Any) -> dict[str, Any]:
    """A marker-free forecast cohort master as a pre-contract writer shaped it: shape (c) by default."""

    row: dict[str, Any] = {
        "job_id": JOB_ID,
        "run_id": RUN_ID,
        "cycle_id": CYCLE_ID,
        "job_type": "run_shud_forecast_array",
        "stage": "forecast",
        "model_id": None,
        "status": "reserved",
        "idempotency_key": KEY,
        "slurm_comment": f"nhms_idem:{KEY}",
        "restart_stage": "forecast",
        "cohort_members": _members(),
        "submission_attempt": 1,
        "submission_attempt_started_at": CYCLE_TIME,
        "created_at": CYCLE_TIME,
        "updated_at": CYCLE_TIME,
    }
    row.update(overrides)
    return row


def _benign_row(job_id: str = "job_cycle_gfs_2026071200_download") -> dict[str, Any]:
    """A settled same-cycle row: creates the root and the cycle log a refused write must leave alone."""

    return {
        "job_id": job_id,
        "run_id": "cycle_gfs_2026071200",
        "cycle_id": CYCLE_ID,
        "job_type": "download_source_cycle",
        "stage": "download",
        "status": "succeeded",
        "created_at": CYCLE_TIME,
        "updated_at": CYCLE_TIME,
    }


def _tree(root: Path) -> dict[str, bytes | None]:
    """Every file (with its bytes) and directory under ``root``, including ``reconcile-inventory/``."""

    return {
        str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    }


def _repository(tmp_path: Path) -> FileOrchestrationJournalRepository:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.append_historical_pipeline_job(_benign_row())
    return repository


def _refused(repository: FileOrchestrationJournalRepository, write: Any, *args: Any) -> None:
    before = _tree(repository.root)
    with pytest.raises(FileOrchestrationJournalError) as error:
        write(*args)
    assert (error.value.reason, error.value.field) == (REFUSAL, "accepted_submit_contract_version")
    assert _tree(repository.root) == before


# --- the predicate -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, True),
        *[({"stage": alias}, True) for alias in sorted(FORECAST_COHORT_STAGE_ALIASES)],
        ({"stage": None, "job_type": "forecast"}, True),
        ({"stage": "", "job_type": "run_shud_forecast"}, True),
        ({"accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION}, False),
        ({"status": "pending"}, False),
        ({"status": "reservation_lost"}, False),
        ({"status": "submitted"}, False),
        ({"cohort_members": []}, False),
        ({"cohort_members": None}, False),
        ({"stage": "convert", "job_type": "convert_canonical"}, False),
        ({"stage": "forcing", "job_type": "produce_forcing_array"}, False),
        # ``stage`` wins over ``job_type`` exactly as in reconcile's rule.
        ({"stage": "parse", "job_type": "run_shud_forecast_array"}, False),
    ],
)
def test_the_predicate_is_exactly_shape_c(overrides: dict[str, Any], expected: bool) -> None:
    predicate = journal_module._is_legacy_unversioned_reserved_forecast_master
    assert predicate(_legacy_master(**overrides)) is expected


# --- E1-E6: every journal writer refuses (c) with zero bytes ----------------------------------------


def test_e1_reserve_refuses_shape_c_and_publishes_no_anchor(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _refused(repository, repository.reserve_pipeline_job, _legacy_master())
    assert not (repository.root / "reconcile-inventory" / f"{JOB_ID}.json").exists()
    assert repository.get_pipeline_job(JOB_ID) is None


def test_e2_upsert_refuses_moving_a_legacy_forecast_master_to_reserved(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.append_historical_pipeline_job(_legacy_master(status="pending", idempotency_key=None))
    before = repository.get_pipeline_job(JOB_ID)
    _refused(
        repository,
        repository.upsert_pipeline_job,
        {
            "job_id": JOB_ID,
            "run_id": RUN_ID,
            "cycle_id": CYCLE_ID,
            "job_type": "run_shud_forecast_array",
            "status": "reserved",
            "idempotency_key": KEY,
        },
    )
    assert repository.get_pipeline_job(JOB_ID) == before


def test_e3_historical_append_refuses_shape_c(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _refused(repository, repository.append_historical_pipeline_job, _legacy_master())
    assert repository.get_pipeline_job(JOB_ID) is None


def _dead_legacy_forecast_master(tmp_path: Path) -> tuple[FileOrchestrationJournalRepository, bytes]:
    """A pre-existing (c) row released by the legacy branch of the retry permit; returns its held anchor."""

    repository = _repository(tmp_path)
    seed_pre_existing_legacy_row(repository.reserve_pipeline_job, _legacy_master())
    anchor_bytes = (repository.root / "reconcile-inventory" / f"{JOB_ID}.json").read_bytes()
    # A bypass batch append, and legal: the released row is not reserved.
    assert repository.permit_pipeline_job_retry(JOB_ID) == 1
    dead = repository.get_pipeline_job(JOB_ID)
    assert (dead["status"], dead["slurm_job_id"]) == ("reservation_lost", None)
    return repository, anchor_bytes


def test_e4_reclaim_of_a_dead_legacy_forecast_master_is_refused_and_publishes_no_anchor(tmp_path: Path) -> None:
    repository, _anchor_bytes = _dead_legacy_forecast_master(tmp_path)
    dead = repository.get_pipeline_job(JOB_ID)
    assert not (repository.root / "reconcile-inventory" / f"{JOB_ID}.json").exists()

    _refused(repository, repository.reclaim_pipeline_job_reservation, _legacy_master())

    assert not (repository.root / "reconcile-inventory" / f"{JOB_ID}.json").exists()
    assert repository.get_pipeline_job(JOB_ID) == dead


def test_e4_reclaim_of_a_dead_legacy_forecast_master_leaves_a_stale_anchor_byte_identical(tmp_path: Path) -> None:
    repository, anchor_bytes = _dead_legacy_forecast_master(tmp_path)
    dead = repository.get_pipeline_job(JOB_ID)
    # The stale anchor a contained direct-projection fault leaves behind after a
    # batch release (the Phase 6h shape) stays byte-identical.  Outcome check
    # only: a rewrite would reproduce these same bytes, so the ordering against
    # the anchor sync is pinned by the side-effect placement test below.
    anchor = repository.root / "reconcile-inventory" / f"{JOB_ID}.json"
    anchor.write_bytes(anchor_bytes)

    _refused(repository, repository.reclaim_pipeline_job_reservation, _legacy_master())

    assert anchor.read_bytes() == anchor_bytes
    assert repository.get_pipeline_job(JOB_ID) == dead


def test_e4_a_member_less_reclaim_of_a_dead_legacy_forecast_master_is_refused(tmp_path: Path) -> None:
    """Refused on the EXISTING row's shape: a request without ``cohort_members``
    would otherwise backfill ``[]``, clear the members and commit a ``reserved``
    member-less unversioned forecast master (then routed to the generic lane)."""

    repository, _anchor_bytes = _dead_legacy_forecast_master(tmp_path)
    dead = repository.get_pipeline_job(JOB_ID)
    assert dead["cohort_members"] == _members()

    _refused(repository, repository.reclaim_pipeline_job_reservation, _legacy_master(cohort_members=None))

    assert repository.get_pipeline_job(JOB_ID) == dead


def test_e4_a_versioned_reclaim_over_a_dead_legacy_forecast_master_still_returns_none(tmp_path: Path) -> None:
    """The only production forecast reclaim request is versioned; over a legacy
    row it must keep losing quietly (``None``) at the versioned gates, which sit
    before the existing-row shape refusal -- it must not start raising."""

    repository, _anchor_bytes = _dead_legacy_forecast_master(tmp_path)
    dead = repository.get_pipeline_job(JOB_ID)
    # The scheduler's stamped request for the same job id and key: canonical
    # members (the versioned evidence boundary validates them) and a digest.
    request = _legacy_master(
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        cohort_members=list(
            canonical_forecast_cohort_members(
                source_id="gfs",
                cycle_time=CYCLE_TIME,
                basins=[{"model_id": member["model_id"], "basin_id": member["basin_id"]} for member in _members()],
            )
        ),
        slurm_ownership_required=False,
        expected_slurm_user=None,
        expected_slurm_account=None,
    )
    request["cohort_digest"] = forecast_cohort_digest({"source_id": "gfs", **request})
    # What ``reserve_candidate`` adds from the exact accepted-submit read.
    request["expected_submission_attempt"] = dead["submission_attempt"]
    request["expected_submission_attempt_started_at"] = dead["submission_attempt_started_at"]
    request_row = repository._pipeline_job_row(request)
    assert accepted_submit_contract_is_current(request_row)
    assert accepted_submit_row_kind(request_row) == "master"
    before = _tree(repository.root)

    assert repository.reclaim_pipeline_job_reservation(request) is None

    assert _tree(repository.root) == before
    assert repository.get_pipeline_job(JOB_ID) == dead


def test_e4_the_scheduler_reservation_over_a_dead_legacy_forecast_master_is_not_created(tmp_path: Path) -> None:
    """``_reserve_cycle_stage`` -> ``reserve_candidate`` for the very cycle run
    whose job id and key the legacy row holds: the stamped insert loses, the
    versioned reclaim returns ``None``, so the pass reports ``created=False``
    (no sbatch) and writes nothing."""

    repository, _anchor_bytes = _dead_legacy_forecast_master(tmp_path)
    dead = repository.get_pipeline_job(JOB_ID)
    before = _tree(repository.root)

    reservation, job_id = _reserve_cycle_stage(
        repository, stage="forecast", job_type="run_shud_forecast_array", canonical="forecast", run_id=RUN_ID
    )

    assert job_id == JOB_ID
    assert (reservation.job_id, reservation.status, reservation.created) == (JOB_ID, "reservation_lost", False)
    assert _tree(repository.root) == before
    assert repository.get_pipeline_job(JOB_ID) == dead


def test_e5_non_versioned_reclaim_of_a_legacy_masters_auto_retry_clone_is_refused(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.append_historical_pipeline_job(
        _legacy_master(status="failed", slurm_job_id="4242", error_code="SLURM_NODE_FAIL")
    )
    clone = FileJournalRetryService(repository).schedule_auto_retry(repository.get_pipeline_job(JOB_ID))
    stored = repository.get_pipeline_job(clone.job_id)
    # The legacy branch of the auto retry: a pending, keyless forecast clone
    # without a contract marker.  The unmatched non-versioned reclaim branch
    # takes it over and backfills ``cohort_members`` from the request, so the
    # reclaimed row would be (c).
    assert (stored["status"], stored["idempotency_key"], stored["slurm_job_id"]) == ("pending", None, None)
    assert (stored["stage"], stored.get("accepted_submit_contract_version")) == ("forecast", None)
    # Member-less, so the reclaim's existing-row check passes; the write-entry
    # check is what refuses the backfilled row.
    assert stored["cohort_members"] == []

    request = _legacy_master(job_id=clone.job_id, idempotency_key=f"{KEY}:retry_1", slurm_comment=None)
    _refused(repository, repository.reclaim_pipeline_job_reservation, request)
    assert repository.get_pipeline_job(clone.job_id) == stored


@pytest.mark.parametrize("writer", ["reserve_pipeline_job", "append_historical_pipeline_job"])
def test_e6_an_empty_stage_falls_back_to_the_forecast_job_type(tmp_path: Path, writer: str) -> None:
    repository = _repository(tmp_path)
    _refused(repository, getattr(repository, writer), _legacy_master(stage=None, job_type="forecast"))
    assert repository.get_pipeline_job(JOB_ID) is None


def _placement_upsert(repository: FileOrchestrationJournalRepository) -> Any:
    repository.append_historical_pipeline_job(_legacy_master(status="pending", idempotency_key=None))
    request = {
        "job_id": JOB_ID,
        "run_id": RUN_ID,
        "cycle_id": CYCLE_ID,
        "job_type": "run_shud_forecast_array",
        "status": "reserved",
        "idempotency_key": KEY,
    }
    return lambda: repository.upsert_pipeline_job(request)


def _placement_historical_append(repository: FileOrchestrationJournalRepository) -> Any:
    return lambda: repository.append_historical_pipeline_job(_legacy_master())


def _placement_clone_reclaim(repository: FileOrchestrationJournalRepository) -> Any:
    repository.append_historical_pipeline_job(
        _legacy_master(status="failed", slurm_job_id="4242", error_code="SLURM_NODE_FAIL")
    )
    clone = FileJournalRetryService(repository).schedule_auto_retry(repository.get_pipeline_job(JOB_ID))
    request = _legacy_master(job_id=clone.job_id, idempotency_key=f"{KEY}:retry_1", slurm_comment=None)
    return lambda: repository.reclaim_pipeline_job_reservation(request)


def _placement_in_place_rewrite(repository: FileOrchestrationJournalRepository) -> Any:
    seed_pre_existing_legacy_row(repository.reserve_pipeline_job, _legacy_master())
    return lambda: repository.record_pipeline_job_reconciliation(
        JOB_ID, submit_outcome="submit_result_ambiguous", reconciliation_decision="absence_deferred"
    )


@pytest.mark.parametrize(
    "arrange",
    [_placement_upsert, _placement_historical_append, _placement_clone_reclaim, _placement_in_place_rewrite],
    ids=["upsert", "historical_append", "memberless_clone_reclaim", "in_place_rewrite"],
)
def test_the_refusal_runs_before_any_side_effect_of_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arrange: Any
) -> None:
    """Entry placement in ``_write_pipeline_job_unlocked``: the conflict check,
    the sequence allocation and the reconcile-inventory anchor sync never run
    for a refused write (the append funnels alone would refuse only after them).

    Every case is refused by the write-entry check alone.  The auto-retry
    clone is stored member-less, so the reclaim's existing-row shape check
    passes and the entry check refuses the row the reclaim backfilled; the
    existing-row check is pinned by the E4 member-less request instead."""

    repository = _repository(tmp_path)
    write = arrange(repository)
    for name in (
        "_pipeline_job_conflicts_unlocked",
        "_next_sequence_unlocked",
        "_sync_reconcile_inventory_for_row_unlocked",
    ):
        monkeypatch.setattr(
            repository,
            name,
            lambda *_args, _name=name, **_kwargs: pytest.fail(f"{_name} ran before the #2674 refusal"),
        )
    _refused(repository, write)


def test_the_batch_append_funnel_refuses_a_shape_c_record(tmp_path: Path) -> None:
    """Backstop for the appenders that bypass ``_write_pipeline_job_unlocked``."""

    repository = _repository(tmp_path)
    row = repository._pipeline_job_row(_legacy_master())
    record = journal_module._journal_record_for_write(
        "pipeline_job", row, source_id="gfs", cycle_time=CYCLE_TIME, model_id=None, sequence=99
    )
    for append in (
        lambda: repository._append_journal_record_unlocked(source_id="gfs", cycle_time=CYCLE_TIME, record=record),
        lambda: repository._append_journal_records_unlocked(
            source_id="gfs", cycle_time=CYCLE_TIME, records=[record]
        ),
    ):
        _refused(repository, append)


# --- E7: the historical import fails closed before the root --------------------------------------


def _snapshot(*jobs: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "forecast_cycles": [
            {"cycle_id": CYCLE_ID, "source_id": "gfs", "cycle_time": CYCLE_TIME, "status": "forecast_running"}
        ],
        "hydro_runs": [
            {
                "run_id": "fcst_gfs_2026071200_model_0",
                "run_type": "forecast",
                "scenario_id": "forecast_gfs_deterministic",
                "model_id": "model_0",
                "source_id": "gfs",
                "cycle_time": CYCLE_TIME,
                "start_time": CYCLE_TIME,
                "end_time": CYCLE_TIME,
                "status": "succeeded",
            }
        ],
        "pipeline_jobs": [_benign_row(), *jobs],
        "pipeline_events": [],
    }


def test_e7_import_with_a_shape_c_job_creates_no_journal_root(tmp_path: Path) -> None:
    journal_root = tmp_path / "journal"
    with pytest.raises(FileOrchestrationJournalError) as error:
        import_historical_scheduler_state(
            journal_root=journal_root,
            cutoff_time=CYCLE_TIME + timedelta(hours=1),
            **_snapshot(_legacy_master()),
        )
    assert (error.value.reason, error.value.field) == (REFUSAL, "accepted_submit_contract_version")
    assert not journal_root.exists()


def test_e7_import_with_a_shape_c_job_writes_zero_bytes_into_an_existing_root(tmp_path: Path) -> None:
    """A NEW cycle (another ``cycle_time``, new run and job ids): without the
    pre-scan the import would append its cycle, run and settled job before the
    writer refused the (c) row, so the tree would change."""

    journal_root = tmp_path / "journal"
    import_historical_scheduler_state(
        journal_root=journal_root, cutoff_time=CYCLE_TIME + timedelta(hours=12), **_snapshot()
    )
    before = _tree(journal_root)
    cycle_time = CYCLE_TIME + timedelta(hours=6)
    cycle_id = "gfs_2026071206"
    run_id = "cycle_gfs_2026071206_forecast_legacy"
    key = f"{run_id}:forecast"
    snapshot = {
        "forecast_cycles": [
            {"cycle_id": cycle_id, "source_id": "gfs", "cycle_time": cycle_time, "status": "forecast_running"}
        ],
        "hydro_runs": [
            {
                "run_id": "fcst_gfs_2026071206_model_0",
                "run_type": "forecast",
                "scenario_id": "forecast_gfs_deterministic",
                "model_id": "model_0",
                "source_id": "gfs",
                "cycle_time": cycle_time,
                "start_time": cycle_time,
                "end_time": cycle_time,
                "status": "succeeded",
            }
        ],
        "pipeline_jobs": [
            {
                **_benign_row("job_cycle_gfs_2026071206_download"),
                "run_id": "cycle_gfs_2026071206",
                "cycle_id": cycle_id,
                "created_at": cycle_time,
                "updated_at": cycle_time,
            },
            _legacy_master(
                job_id=f"job_{run_id}_forecast",
                run_id=run_id,
                cycle_id=cycle_id,
                idempotency_key=key,
                slurm_comment=f"nhms_idem:{key}",
                stage=None,
                job_type="forecast",
                submission_attempt_started_at=cycle_time,
                created_at=cycle_time,
                updated_at=cycle_time,
            ),
        ],
        "pipeline_events": [],
    }
    with pytest.raises(FileOrchestrationJournalError) as error:
        import_historical_scheduler_state(
            journal_root=journal_root, cutoff_time=CYCLE_TIME + timedelta(hours=12), **snapshot
        )
    assert (error.value.reason, error.value.field) == (REFUSAL, "accepted_submit_contract_version")
    assert _tree(journal_root) == before


def test_e7_import_is_unchanged_for_a_legacy_forecast_master_in_another_status(tmp_path: Path) -> None:
    receipt = import_historical_scheduler_state(
        journal_root=tmp_path / "journal",
        cutoff_time=CYCLE_TIME + timedelta(hours=1),
        **_snapshot(_legacy_master(status="succeeded", slurm_job_id="4242")),
    )
    assert receipt["imported_row_counts"]["pipeline_jobs"] == 2
    stored = FileOrchestrationJournalRepository(tmp_path / "journal").get_pipeline_job(JOB_ID)
    assert (stored["status"], stored["cohort_members"]) == ("succeeded", _members())


# --- E9: legitimate writes are unchanged ---------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "pending", "idempotency_key": None},
        {"status": "submitted", "slurm_job_id": "4242"},
        {"cohort_members": []},
        {"stage": "convert", "job_type": "convert_canonical"},
        {"stage": "download", "job_type": "download_source_cycle", "cohort_members": None},
        # A forcing-stage legacy reserved row with members (#2675's lane, not (c)).
        {"stage": "forcing", "job_type": "produce_forcing_array"},
    ],
)
@pytest.mark.parametrize("writer", ["reserve_pipeline_job", "upsert_pipeline_job", "append_historical_pipeline_job"])
def test_e9_a_non_shape_c_legacy_row_is_written_as_before(
    tmp_path: Path, writer: str, overrides: dict[str, Any]
) -> None:
    repository = _repository(tmp_path)
    record = _legacy_master(**overrides)
    assert getattr(repository, writer)(record) is not None
    stored = repository.get_pipeline_job(JOB_ID)
    assert (stored["status"], stored["stage"], stored.get("accepted_submit_contract_version")) == (
        record["status"],
        record["stage"],
        None,
    )


def test_e9_a_versioned_forecast_master_is_reserved_as_before(tmp_path: Path) -> None:
    from tests.gateway_reconcile_helpers import _versioned_master_reservation_record

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _versioned_master_reservation_record(created_at=CYCLE_TIME, member_count=2)
    assert repository.reserve_pipeline_job(record) is not None
    stored = repository.get_accepted_submit_pipeline_job(record["job_id"])
    assert (stored["status"], stored["accepted_submit_contract_version"]) == (
        "reserved",
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
    )


def test_e9_a_pre_existing_shape_c_row_may_still_leave_reserved(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    seed_pre_existing_legacy_row(repository.reserve_pipeline_job, _legacy_master())
    _previous, written = repository.update_pipeline_job_status(JOB_ID, "reservation_lost")
    assert written["status"] == "reservation_lost"
    assert repository.get_pipeline_job(JOB_ID)["status"] == "reservation_lost"


def test_e9_a_pre_existing_shape_c_row_cannot_be_rewritten_in_place(tmp_path: Path) -> None:
    """Readable is not writable: a write that keeps the row reserved re-persists (c)."""

    repository = _repository(tmp_path)
    seed_pre_existing_legacy_row(repository.reserve_pipeline_job, _legacy_master())
    before = repository.get_pipeline_job(JOB_ID)
    _refused(
        repository,
        lambda: repository.record_pipeline_job_reconciliation(
            JOB_ID, submit_outcome="submit_result_ambiguous", reconciliation_decision="absence_deferred"
        ),
    )
    assert repository.get_pipeline_job(JOB_ID) == before


# --- E10: the only production minter stamps the contract ------------------------------------------


def _reserve_cycle_stage(
    repository: FileOrchestrationJournalRepository,
    *,
    stage: str,
    job_type: str,
    canonical: str,
    run_id: str = "cycle_gfs_2026071200",
    basins: int = 2,
) -> tuple[Any, str]:
    """Drive the production reservation minter with a real journal (#2674 caller pin).

    ``canonical`` is the stage name the chain derives the job id and key from
    (every forecast alias canonicalizes to ``forecast``); ``run_id`` is the
    cycle run the job id and key are derived from, exactly as the chain does.
    """

    from services.orchestrator.chain import ForecastOrchestrator
    from services.orchestrator.chain_types import StageDefinition

    orchestrator = object.__new__(ForecastOrchestrator)
    orchestrator.repository = repository
    orchestrator.config = SimpleNamespace(reconcile_slurm_user="scheduler", reconcile_slurm_account="account")
    active = [{"model_id": f"model_{index}", "basin_id": f"basin_{index}"} for index in range(basins)]
    context = SimpleNamespace(
        source_id="gfs",
        cycle_time=CYCLE_TIME,
        cycle_id=CYCLE_ID,
        run_id=run_id,
        all_basins=active,
        active_basins=active,
        retry_attempt=None,
        inherited_retry_attempt_floors=None,
    )
    stage_definition = StageDefinition(
        stage=stage,
        job_type=job_type,
        template_name="unused.sbatch",
        success_cycle_status="unused",
        failure_cycle_status="unused",
        is_array=True,
    )
    job_id = f"job_{run_id}_{canonical}"
    return orchestrator._reserve_cycle_stage(stage_definition, context, job_id, f"{run_id}:{canonical}"), job_id


def _reserve_through_the_cycle_stage(
    tmp_path: Path, *, stage: str, job_type: str, canonical: str
) -> tuple[FileOrchestrationJournalRepository, str]:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    reservation, job_id = _reserve_cycle_stage(repository, stage=stage, job_type=job_type, canonical=canonical)
    assert reservation is not None and reservation.created
    return repository, job_id


@pytest.mark.parametrize(
    ("stage", "job_type"),
    [
        *[(alias, "run_shud_forecast_array") for alias in sorted(FORECAST_COHORT_STAGE_ALIASES)],
        ("", "run_shud_forecast_array"),
    ],
)
def test_e10_reserve_cycle_stage_stamps_every_forecast_cohort_reservation(
    tmp_path: Path, stage: str, job_type: str
) -> None:
    repository, job_id = _reserve_through_the_cycle_stage(
        tmp_path, stage=stage, job_type=job_type, canonical="forecast"
    )
    stored = repository.get_accepted_submit_pipeline_job(job_id)
    assert stored["status"] == "reserved"
    assert stored["cohort_members"]
    assert stored["accepted_submit_contract_version"] == ACCEPTED_SUBMIT_CONTRACT_VERSION


def test_e9_the_downstream_member_recording_reservation_is_unchanged(tmp_path: Path) -> None:
    """#2603 B4: a model-less convert cohort row is reserved, member-bearing and marker-free by design."""

    repository, job_id = _reserve_through_the_cycle_stage(
        tmp_path, stage="convert", job_type="convert_canonical", canonical="convert"
    )
    stored = repository.get_pipeline_job(job_id)
    assert (stored["status"], stored["stage"]) == ("reserved", "convert")
    assert stored["cohort_members"]
    assert stored.get("accepted_submit_contract_version") is None


# --- E11: a pre-existing (c) row keeps its fail-closed handling ----------------------------------


def test_e11_a_pre_existing_shape_c_row_stays_read_only_and_unbindable(tmp_path: Path) -> None:
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs
    from tests.orchestrator_bind_reserved_job_helpers import QUERY_END, bind_kwargs

    repository = _repository(tmp_path)
    seed_pre_existing_legacy_row(repository.reserve_pipeline_job, _legacy_master())
    row = repository.get_pipeline_job(JOB_ID)

    (outcome,) = reconcile_reserved_unbound_jobs(
        repository, comment_query=lambda _key, **_kw: pytest.fail("never queried"), now=lambda: QUERY_END
    )
    assert outcome.action == "legacy_unversioned_read_only"
    assert repository.get_pipeline_job(JOB_ID) == row
    # Taken after the pass: its one-time reconcile-inventory migration marker
    # is the only byte it writes.
    before = _tree(repository.root)
    result = repository.bind_operator_verified_reserved_job(
        JOB_ID,
        **bind_kwargs(
            {
                "submission_attempt": row["submission_attempt"],
                "submission_attempt_started_at": row["submission_attempt_started_at"],
            }
        ),
    )
    assert (result.refusal, result.receipt) == ("legacy_unversioned_unsupported", None)
    assert _tree(repository.root) == before
