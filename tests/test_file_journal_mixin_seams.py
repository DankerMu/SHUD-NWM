"""Behavioural seams of the file-journal repository mixins (#3350).

Tests patch names on ``services.orchestrator.file_orchestration_journal``.  A
repository-mixin method that moved into a ``file_journal_*`` sibling only sees
such a patch when it reads the name off the facade module at call time.  The
cluster map (``tests/fixtures/file_journal_cluster_map.json``) records, per
facade-resident patched name, every mixin that reads it; each such
``(patched name, mixin cluster)`` pair gets one driver here, on a real
``FileOrchestrationJournalRepository`` under ``tmp_path``.

Oracle.  "The stub was called" proves nothing for a mixin: every mixin write
also goes through the facade's append path and ``_journal_record_for_write``,
which read ``_utcnow`` themselves, and ``file_journal_paths`` and facade code
call the IO names too.  So each patched name is replaced by a recorder that
notes the module of its caller (``sys._getframe(1).f_globals["__name__"]``)
and delegates to the real callable, and the test asserts that the mixin's own
module is among the recorded callers.  A driver seeds first and returns its
act; the recorders are installed only around the act, so seeding calls never
count.  Each act also asserts that it took the path it is meant to drive, so a
silent refusal cannot pass for a seam read.

A driver pins the reads its act executes, not every read in the mixin: one
method that bypasses the facade while another still reads through it is the
origin rule's job (``origin_bypass_problems`` in
``tests/test_file_journal_cluster_map.py``), which covers every import-bound
name statically.  For ``_stat_signature``, which the facade defines itself and
which therefore has no origin, these drivers are the only guard.

The recorders patch only names already in the map's ``patched`` table, with
literal attribute names, so the map's patched-name scanner stays exact.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import services.orchestrator.file_orchestration_journal as file_orchestration_journal
from packages.common.contracts.time_normalization import utc_now
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

MAP_PATH = Path(__file__).resolve().parent / "fixtures" / "file_journal_cluster_map.json"
_MIXIN_UNIT_RE = re.compile(r"_FileJournal\w+Mixin")
CYCLE_TIME = datetime(2026, 6, 28, tzinfo=UTC)
MASTER_JOB_ID = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
# The on-disk name of the facade's private `_RECONCILE_INVENTORY_MIGRATION_MARKER`.
MIGRATION_MARKER = "reconcile-inventory-migration-v1.json"

Act = Callable[[], None]


def _install_recorders(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Replace the five driven patched names with caller-recording proxies; return ``name -> caller modules``."""

    callers: dict[str, list[str]] = {}

    def recorder(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        seen = callers.setdefault(name, [])

        def proxy(*args: Any, **kwargs: Any) -> Any:
            seen.append(sys._getframe(1).f_globals["__name__"])
            return real(*args, **kwargs)

        return proxy

    # The facade binds `utc_now as _utcnow`; delegating to the origin keeps `_utcnow` a patch-only name
    # outside the siblings (tests/test_file_orchestration_journal_facade.py), and the cluster map's
    # origin table pins that binding.
    monkeypatch.setattr(file_orchestration_journal, "_utcnow", recorder("_utcnow", utc_now))
    monkeypatch.setattr(
        file_orchestration_journal,
        "stat_no_follow",
        recorder("stat_no_follow", file_orchestration_journal.stat_no_follow),
    )
    monkeypatch.setattr(
        file_orchestration_journal,
        "unlink_no_follow",
        recorder("unlink_no_follow", file_orchestration_journal.unlink_no_follow),
    )
    monkeypatch.setattr(
        file_orchestration_journal,
        "list_directory_no_follow_limited",
        recorder("list_directory_no_follow_limited", file_orchestration_journal.list_directory_no_follow_limited),
    )
    monkeypatch.setattr(
        file_orchestration_journal,
        "_stat_signature",
        recorder("_stat_signature", file_orchestration_journal._stat_signature),
    )
    return callers


def _repository(tmp_path: Path) -> FileOrchestrationJournalRepository:
    return FileOrchestrationJournalRepository(tmp_path / "journal")


def _versioned_master_record() -> dict[str, Any]:
    from tests.gateway_reconcile_helpers import _versioned_master_reservation_record

    return _versioned_master_reservation_record(member_count=2)


def _attempt_kwargs(row: dict[str, Any]) -> dict[str, Any]:
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    return {
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
        "expected_submission_attempt": int(row["submission_attempt"]),
        "expected_submission_attempt_started_at": row["submission_attempt_started_at"],
    }


# ---------------------------------------------------------------------------
# Drivers: seed under tmp_path, return the act
# ---------------------------------------------------------------------------


def _events_update_forecast_cycle_status(tmp_path: Path) -> Act:
    repository = _repository(tmp_path)

    def act() -> None:
        row = repository.update_forecast_cycle_status(source_id="gfs", cycle_time=CYCLE_TIME, status="discovered")
        assert row is not None and row["status"] == "discovered"

    return act


def _release_recover_released_reservation(tmp_path: Path) -> Act:
    from tests.gateway_reconcile_helpers import _file_cohort_repository

    repository = _file_cohort_repository(tmp_path, member_count=2, with_runtime_rows=False)
    kwargs = _attempt_kwargs(repository.get_accepted_submit_pipeline_job(MASTER_JOB_ID))
    assert repository.release_identity_blocked_reservation(MASTER_JOB_ID, identity_blocked_streak=1, **kwargs) == 1

    def act() -> None:
        recovered = repository.recover_released_identity_blocked_reservation(MASTER_JOB_ID, **kwargs)
        assert recovered is not None
        assert recovered[file_orchestration_journal.OPERATOR_RECOVERY_ATTESTATION_FIELD] not in (None, "")

    return act


def _reclaim_dead_reservation(tmp_path: Path) -> Act:
    repository = _repository(tmp_path)
    record = {
        "job_id": "job_cycle_gfs_2026062800_forecast",
        "run_id": "fcst_gfs_2026062800_model_a",
        "cycle_id": "gfs_2026062800",
        "job_type": "run_shud_forecast_array",
        "model_id": "model_a",
        "status": "reserved",
        "stage": "forecast",
        "idempotency_key": "gfs:gfs_2026062800:basin_a:forecast",
        "candidate_id": "candidate_a",
    }
    assert repository.reserve_pipeline_job(dict(record)) is not None
    repository.update_pipeline_job_status(record["job_id"], "submission_failed", error_code="SBATCH_REJECTED")

    def act() -> None:
        reclaimed = repository.reclaim_pipeline_job_reservation(dict(record))
        assert reclaimed is not None and reclaimed["status"] == "reserved"

    return act


def _operator_demote_held_reservation(tmp_path: Path) -> Act:
    from tests.orchestrator_demote_reserved_job_helpers import JOB_ID, _demote_kwargs, _held_cohort_repository

    repository = _held_cohort_repository(tmp_path)
    kwargs = _demote_kwargs(repository.get_accepted_submit_pipeline_job(JOB_ID))

    def act() -> None:
        assert repository.demote_operator_verified_reserved_job(JOB_ID, **kwargs) is not None
        assert repository.get_accepted_submit_pipeline_job(JOB_ID)["status"] == "reservation_lost"

    return act


def _cohort_defer_mismatched_master(tmp_path: Path) -> Act:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

    repository = _repository(tmp_path)
    record = _versioned_master_record()
    assert repository.reserve_pipeline_job(dict(record)) is not None
    assert repository.commit_pipeline_job_submit_attempt(
        str(record["idempotency_key"]),
        pipeline_job_id=str(record["job_id"]),
        expected_submission_attempt=1,
        slurm_job_id="7100",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    ).committed

    def act() -> None:
        # A terminal master id that does not match the durable accepted submit defers the projection.
        result = repository.project_forecast_cohort_tasks(
            str(record["job_id"]),
            master_slurm_job_id="7199",
            projections=[],
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert result == {"total": 1, "pipeline_status": 1, "pipeline_event": 0}

    return act


def _submit_transition_evidence(tmp_path: Path) -> Act:
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )

    repository = _repository(tmp_path)
    record = _versioned_master_record()
    assert repository.reserve_pipeline_job(dict(record)) is not None

    def act() -> None:
        result = repository.transition_pipeline_job_submit_evidence(
            record["job_id"],
            AcceptedSubmitTransition.timeout(),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_statuses=("reserved",),
            require_unbound=True,
        )
        assert result.wrote

    return act


def _retention_discovery_on_an_empty_root(tmp_path: Path) -> Act:
    repository = _repository(tmp_path)

    def act() -> None:
        assert repository.discover_retention_cycles().cycles == ()

    return act


def _reconcile_inventory_migration_marker(tmp_path: Path) -> Act:
    repository = _repository(tmp_path)

    def act() -> None:
        assert list(repository._iter_reconcile_pipeline_job_records()) == []
        marker = json.loads((repository.root / MIGRATION_MARKER).read_text(encoding="utf-8"))
        assert marker["completed_at"]

    return act


def _reconcile_inventory_direct_job_scan(tmp_path: Path) -> Act:
    repository = _repository(tmp_path)

    def act() -> None:
        assert list(repository._iter_reconcile_direct_pipeline_job_records()) == []

    return act


def _reconcile_inventory_migration_residue_removal(tmp_path: Path) -> Act:
    repository = _repository(tmp_path)
    repository.root.mkdir(parents=True)
    residue = repository.root / f".{MIGRATION_MARKER}.{'0' * 32}.tmp"
    residue.write_text("residue", encoding="utf-8")

    def act() -> None:
        assert list(repository._iter_reconcile_pipeline_job_records()) == []
        assert not residue.exists()

    return act


def _reconcile_inventory_anchor_listing(tmp_path: Path) -> Act:
    repository = _repository(tmp_path)

    def act() -> None:
        assert list(repository._iter_reconcile_inventory_records()) == []

    return act


def _reconcile_inventory_canonical_job(tmp_path: Path) -> Act:
    from tests.gateway_reconcile_helpers import _file_cohort_repository

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    # The first pass runs the one-time migration; the act is a steady-state pass.
    assert [row["job_id"] for row in repository._iter_reconcile_pipeline_job_records()] == [MASTER_JOB_ID]

    def act() -> None:
        assert [row["job_id"] for row in repository._iter_reconcile_pipeline_job_records()] == [MASTER_JOB_ID]

    return act


PAIR_DRIVERS: dict[tuple[str, str], Callable[[Path], Act]] = {
    ("_utcnow", "events"): _events_update_forecast_cycle_status,
    ("_utcnow", "release"): _release_recover_released_reservation,
    ("_utcnow", "reclaim"): _reclaim_dead_reservation,
    ("_utcnow", "operator"): _operator_demote_held_reservation,
    ("_utcnow", "cohort"): _cohort_defer_mismatched_master,
    ("_utcnow", "submit"): _submit_transition_evidence,
    ("_utcnow", "reconcile_inventory"): _reconcile_inventory_migration_marker,
    ("stat_no_follow", "retention"): _retention_discovery_on_an_empty_root,
    ("stat_no_follow", "reconcile_inventory"): _reconcile_inventory_direct_job_scan,
    ("unlink_no_follow", "reconcile_inventory"): _reconcile_inventory_migration_residue_removal,
    ("list_directory_no_follow_limited", "reconcile_inventory"): _reconcile_inventory_anchor_listing,
    ("_stat_signature", "reconcile_inventory"): _reconcile_inventory_canonical_job,
}


def _mixin_pairs_from_the_map() -> set[tuple[str, str]]:
    """Every ``(patched name, caller cluster)`` whose recorded caller units include a ``_FileJournal...Mixin``."""

    patched = json.loads(MAP_PATH.read_text(encoding="utf-8"))["patched"]
    return {
        (name, cluster)
        for name, entry in patched.items()
        for cluster, units in entry["callers"].items()
        if any(_MIXIN_UNIT_RE.fullmatch(unit) for unit in units)
    }


def test_every_mixin_caller_pair_of_the_map_has_exactly_one_driver() -> None:
    derived = _mixin_pairs_from_the_map()
    missing = sorted(derived - set(PAIR_DRIVERS))
    stale = sorted(set(PAIR_DRIVERS) - derived)
    assert missing == [], f"map pairs (patched name, mixin cluster) without a seam driver: {missing}"
    assert stale == [], f"seam drivers for pairs the map no longer records: {stale}"


@pytest.mark.parametrize(
    ("name", "cluster"),
    sorted(PAIR_DRIVERS),
    ids=[f"{name}-{cluster}" for name, cluster in sorted(PAIR_DRIVERS)],
)
def test_mixin_reads_the_patched_name_off_the_facade(
    name: str, cluster: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    act = PAIR_DRIVERS[(name, cluster)](tmp_path)
    callers = _install_recorders(monkeypatch)

    act()

    module = f"services.orchestrator.file_journal_{cluster}"
    assert module in callers[name], (
        f"{module} did not read `{name}` off the facade at call time; "
        f"the recorded callers are {sorted(set(callers[name]))}"
    )
