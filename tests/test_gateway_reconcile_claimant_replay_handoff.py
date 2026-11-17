"""Claimant exclusivity (#1850 Phase 6g-6i): replayed rows, stale anchors and migration handoff.

Partition of ``tests/test_gateway_reconcile_claimant_exclusivity.py`` (#3177):
replayed candidate rows never occupy a master id, a stale reconcile-inventory
anchor, the public full-reconcile path when the master direct failed, the
anchor-prune and handoff races, and the marker-absent first migration. The
shared helpers are in ``tests/gateway_reconcile_claimant_exclusivity_helpers.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.gateway_reconcile_claimant_exclusivity_helpers import (
    _fallback_querier,
    _fallback_row,
    _file_cohort_single_projection,
    _reserve_gfs_master_on,
)
from tests.gateway_reconcile_helpers import _file_cohort_repository
from tests.test_real_slurm_gateway import _pinned_local_timezone


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_replayed_candidate_rows_never_occupy_a_master_id(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix F: only current accepted-submit forecast MASTERS are owners.

    The cycle journal of a settled master also contains accepted-submit task
    CANDIDATE rows (``job_fcst_...``) that carry array-shaped slurm ids. A
    candidate row must never count as an owner of the master id, even when it
    shares the numeric prefix. The scan filters replayed canonical rows to
    current forecast masters before occupancy adjudication."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            slurm_accounting_submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        repository.project_forecast_cohort_tasks(
            ifs_job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(ifs_job_id)["status"] == "succeeded"
        assert len(repository.query_reserved_unbound_jobs()) == 0

        # The settled journal now contains a task candidate row with an
        # array-shaped id. Tamper the flat direct so the master projection is
        # hidden (replaced with {}), forcing the scan to replay the cycle
        # journal where both master and candidate rows live.
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").write_text(
            "{}", encoding="utf-8"
        )
        # Sanity: the cycle journal replay contains BOTH the settled master
        # and the task candidate.
        from services.orchestrator import file_orchestration_journal as journal_module

        journal_records = repository._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        )
        rows = journal_module._CycleRows()
        for record in journal_records:
            repository._apply_journal_record(
                rows, record, source_id="IFS", cycle_time=anchor
            )
        replayed_ids = set(rows.pipeline_jobs)
        assert ifs_job_id in replayed_ids
        candidate_ids = [jid for jid in replayed_ids if jid.startswith("job_fcst_")]
        assert candidate_ids, "settled master journal must contain task candidates"
        candidate = rows.pipeline_jobs[candidate_ids[0]]
        assert str(candidate.get("slurm_job_id") or "").startswith("72001")

        # GFS recycles the freed master id at a DIFFERENT incarnation (01:30):
        # the master row is settled at 01:00 so it does not block, and the
        # candidate row must not block either. The bind succeeds.
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:30:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "bound", (
            "a task candidate carrying a shared numeric prefix is not a master"
            " owner; only current forecast masters occupy a Slurm id"
        )
        assert outcome.matched_slurm_job_id == "72001"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["slurm_job_id"] == "72001"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72001"
        from services.orchestrator import file_orchestration_journal as journal_module

        reopened_journal_rows = journal_module._CycleRows()
        for record in reopened._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            reopened._apply_journal_record(
                reopened_journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = reopened_journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"


# --- #1850 Phase 6g (invariant closure III): a stale reconcile-inventory
# anchor whose canonical row has settled (batch terminal projection committed
# the journal but the direct projection failed) must STILL block a fallback
# bind of the exact same accounting incarnation. The anchor is adjudicated by
# canonical submitted_at BEFORE settled/quiescent rows are skipped — only for
# the name-window fallback producer that carries a candidate Submit.


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_stale_anchor_terminal_canonical_blocks_same_incarnation_fallback_bind(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix G: a stale reconcile-inventory anchor over a SETTLED canonical row
    blocks the exact same accounting incarnation.

    The IFS master binds + settles ``72001@01:00`` through the REAL batch
    projection path with an injected terminal-master direct failure: the batch
    journal append commits canonical ``succeeded/72001@01:00``, then the master
    direct write raises, leaving the reconcile-inventory anchor stale (still
    listing the row as current) and the master direct absent. A later GFS
    fallback commit for candidate ``72001@01:00`` MUST refuse
    (``active_slurm_id_occupied`` internally; ``identity_mismatch_blocked`` /
    count 1 on the public reconcile surface) — the stale anchor's canonical
    authority is the exact same accounting incarnation, so the fallback cannot
    reuse it even though the canonical row is settled.
    """

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            slurm_accounting_submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed

        # Remove the derived master direct projection (models the supported
        # crash boundary: journal-authority commit followed by direct
        # projection failure).
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").unlink()

        # Real batch terminal projection with the terminal master direct write
        # injected to fail AFTER the batch journal append has committed.
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        real_direct = repository._write_pipeline_job_direct_unlocked

        def failing_direct(row: Any, record: Any) -> None:
            job_id = str((row or {}).get("job_id") or "")
            if job_id == ifs_job_id:
                raise OSError("simulated terminal master direct failure")
            return real_direct(row, record)

        repository._write_pipeline_job_direct_unlocked = failing_direct
        try:
            repository.project_forecast_cohort_tasks(
                ifs_job_id,
                master_slurm_job_id="72001",
                projections=projections,
                complete=True,
                master_status="succeeded",
                master_error_code=None,
                reconciliation_decision="matched_bound",
            )
        except Exception:
            # The batch journal append committed before the terminal direct
            # write raised; the projection surface fails (lock release wraps
            # the original fault). The durable state below is the invariant.
            pass
        finally:
            repository._write_pipeline_job_direct_unlocked = real_direct

        # Invariant state: canonical journal committed, master direct absent,
        # stale reconcile-inventory anchor still present.
        from services.orchestrator import file_orchestration_journal as journal_module

        journal_rows = journal_module._CycleRows()
        for record in repository._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            repository._apply_journal_record(
                journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"
        assert canonical_ifs["submitted_at"] == "2026-07-12T01:00:00Z"
        assert not (
            repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
        ).exists(), "master direct must be absent after the injected failure"
        anchor_path = (
            repository.root
            / "reconcile-inventory"
            / f"{ifs_job_id}.json"
        )
        assert anchor_path.exists(), "stale reconcile-inventory anchor must remain"

        # Reserve GFS on a FRESH repository WITHOUT iterating the inventory
        # first (that would prune the stale anchor — the crash window is the
        # reserve-then-commit sequence).
        fresh = FileOrchestrationJournalRepository(repository.root)
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            fresh,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        # The stale IFS anchor must still be present at commit time.
        entry_names = fresh._reconcile_inventory_entry_names_unlocked()
        assert f"{ifs_job_id}.json" in entry_names

        # Typed name-window-unique commit for the SAME 01:00 incarnation.
        gfs_key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        commit_result = fresh.commit_pipeline_job_submit_attempt(
            gfs_key,
            pipeline_job_id=gfs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            slurm_accounting_submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert not commit_result.committed
        assert commit_result.outcome == "active_slurm_id_occupied", commit_result.outcome
        persisted = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(
            gfs_job_id
        )
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None

        # The public reconcile surface must ALSO refuse: its own inventory
        # iteration would prune the settled IFS anchor, but the Phase 6h handoff
        # restores the missing derived master direct from canonical authority
        # BEFORE pruning, so the bounded fallback flat scan still discovers the
        # canonical cycle and blocks the same incarnation. GFS stays held and
        # unbound; same (id, canonical Submit) never binds regardless of settled
        # status or a stale/missing derived projection.

        # Reopened canonical IFS remains the sole settled owner; GFS held.
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["status"] == "reserved"
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        reopened_journal_rows = journal_module._CycleRows()
        for record in reopened._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            reopened._apply_journal_record(
                reopened_journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = reopened_journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"
        assert canonical_ifs["submitted_at"] == "2026-07-12T01:00:00Z"


# --- #1850 Phase 6h (invariant closure IV): the PUBLIC full-reconcile path
# must also refuse a fallback bind of the exact same accounting incarnation
# when a batch terminal projection committed the canonical journal but the
# derived master direct failed. The inventory-iteration prune of a stale
# anchor must first restore the missing derived direct from canonical
# authority (the flat scan's bounded locator), so the fallback discovers the
# canonical cycle and blocks. Same (id, canonical Submit) never binds
# regardless of settled status or a stale/missing derived projection.


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_public_reconcile_refuses_same_incarnation_when_master_direct_failed(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix H: the REAL public reconcile refuses the same accounting incarnation.

    Full sequence: IFS bound ``72001@01:00``, migration completed, derived
    master direct removed, real ``project_forecast_cohort_tasks`` terminal
    projection with injected terminal-master direct failure (canonical journal
    ``succeeded/72001@01:00``, stale anchor remains, direct absent), GFS
    reserved, then REAL ``reconcile_reserved_unbound_jobs`` with unique
    comment-less fallback ``72001@01:00``. The public outcome MUST be
    ``identity_mismatch_blocked`` / count 1, GFS held reserved/unbound under
    the #1564 tuple with streak zero, and reopened canonical IFS remains the
    sole settled owner.
    """

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            slurm_accounting_submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        # Complete the reconcile-inventory migration (production journal state:
        # the marker exists before the crash window opens).
        assert len(repository.query_reserved_unbound_jobs()) == 0
        assert (
            repository.root / "reconcile-inventory-migration-v1.json"
        ).exists()

        # Remove the derived master direct, then run the REAL batch terminal
        # projection with an injected terminal-master direct failure.
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").unlink()
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        real_direct = repository._write_pipeline_job_direct_unlocked

        def failing_direct(row: Any, record: Any) -> None:
            job_id = str((row or {}).get("job_id") or "")
            if job_id == ifs_job_id:
                raise OSError("simulated terminal master direct failure")
            return real_direct(row, record)

        repository._write_pipeline_job_direct_unlocked = failing_direct
        try:
            repository.project_forecast_cohort_tasks(
                ifs_job_id,
                master_slurm_job_id="72001",
                projections=projections,
                complete=True,
                master_status="succeeded",
                master_error_code=None,
                reconciliation_decision="matched_bound",
            )
        except Exception:
            pass
        finally:
            repository._write_pipeline_job_direct_unlocked = real_direct

        # Crash state: canonical journal succeeded/72001@01:00, stale anchor
        # present, master direct absent.
        from services.orchestrator import file_orchestration_journal as journal_module

        journal_rows = journal_module._CycleRows()
        for record in repository._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            repository._apply_journal_record(
                journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"
        assert canonical_ifs["submitted_at"] == "2026-07-12T01:00:00Z"
        assert (
            repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
        ).exists(), "stale reconcile-inventory anchor must remain"
        assert not (
            repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
        ).exists(), "master direct must be absent after the injected failure"

        # Reserve GFS then run REAL public reconcile.
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)
        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked", (
            "the public reconcile must refuse the exact same accounting"
            " incarnation; the handoff restores the missing direct before"
            " pruning the stale anchor, so the fallback cannot read vacancy"
        )
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0

        # Reopened canonical IFS remains the sole settled owner; GFS held.
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["status"] == "reserved"
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        reopened_journal_rows = journal_module._CycleRows()
        for record in reopened._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            reopened._apply_journal_record(
                reopened_journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = reopened_journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"
        assert canonical_ifs["submitted_at"] == "2026-07-12T01:00:00Z"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_anchor_not_pruned_when_derived_direct_restore_fails(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix H fail-closed: when the derived direct restoration raises, the stale
    inventory anchor is KEPT (never pruned first), so a fallback cannot read
    vacancy from a missing locator."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            slurm_accounting_submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        assert len(repository.query_reserved_unbound_jobs()) == 0
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").unlink()
        real_direct = repository._write_pipeline_job_direct_unlocked

        def failing_direct(row: Any, record: Any) -> None:
            job_id = str((row or {}).get("job_id") or "")
            if job_id == ifs_job_id:
                raise OSError("simulated terminal master direct failure")
            return real_direct(row, record)

        repository._write_pipeline_job_direct_unlocked = failing_direct
        try:
            repository.project_forecast_cohort_tasks(
                ifs_job_id,
                master_slurm_job_id="72001",
                projections=projections,
                complete=True,
                master_status="succeeded",
                master_error_code=None,
                reconciliation_decision="matched_bound",
            )
        except Exception:
            pass
        finally:
            repository._write_pipeline_job_direct_unlocked = real_direct

        # Now make the RESTORE itself fail: the inventory iteration must keep
        # the anchor (fail closed) rather than prune first.
        fresh = FileOrchestrationJournalRepository(repository.root)
        assert (
            repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
        ).exists()
        real_write = fresh._write_pipeline_job_direct_unlocked

        def failing_restore(row: Any, record: Any) -> None:
            job_id = str((row or {}).get("job_id") or "")
            if job_id == ifs_job_id:
                raise OSError("simulated restore failure")
            return real_write(row, record)

        fresh._write_pipeline_job_direct_unlocked = failing_restore
        try:
            list(fresh._iter_reconcile_inventory_records())
        except Exception as error:
            # The restore fault surfaces through the cycle-lock release wrap;
            # the invariant is that the anchor is preserved.
            assert not isinstance(error, AssertionError)
        finally:
            fresh._write_pipeline_job_direct_unlocked = real_write

        # Anchor must NOT have been pruned; direct must still be absent.
        assert (
            repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
        ).exists(), "fail closed: stale anchor must be kept when restore raises"
        assert not (
            repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
        ).exists(), "direct must stay absent after a failed restore"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_handoff_race_with_fallback_commit_never_binds_same_incarnation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix H interleaving: while the stale-anchor handoff (restore missing
    derived direct, then prune anchor) runs under the inventory lock, a
    competing fallback commit cannot observe BOTH locator surfaces absent and
    must never bind the same accounting incarnation."""

    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            slurm_accounting_submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        assert len(repository.query_reserved_unbound_jobs()) == 0
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").unlink()
        real_direct = repository._write_pipeline_job_direct_unlocked

        def failing_direct(row: Any, record: Any) -> None:
            job_id = str((row or {}).get("job_id") or "")
            if job_id == ifs_job_id:
                raise OSError("simulated terminal master direct failure")
            return real_direct(row, record)

        repository._write_pipeline_job_direct_unlocked = failing_direct
        try:
            repository.project_forecast_cohort_tasks(
                ifs_job_id,
                master_slurm_job_id="72001",
                projections=projections,
                complete=True,
                master_status="succeeded",
                master_error_code=None,
                reconciliation_decision="matched_bound",
            )
        except Exception:
            pass
        finally:
            repository._write_pipeline_job_direct_unlocked = real_direct

        # GFS reserved on the same root; the stale IFS anchor is still present.
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        assert (
            repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
        ).exists()

        barrier = Barrier(2, timeout=30)

        def handoff_iter() -> str:
            contender = FileOrchestrationJournalRepository(repository.root)
            barrier.wait(timeout=30)
            # The inventory iteration prunes the stale anchor AFTER restoring
            # the missing direct (handoff).
            rows = list(contender._iter_reconcile_inventory_records())
            return f"iter:{len(rows)}"

        def fallback_commit() -> str:
            contender = FileOrchestrationJournalRepository(repository.root)
            barrier.wait(timeout=30)
            commit_result = contender.commit_pipeline_job_submit_attempt(
                "cycle_gfs_2026071200_forecast_fixture:forecast",
                pipeline_job_id=gfs_job_id,
                expected_submission_attempt=1,
                slurm_job_id="72001",
                submitted_at=anchor + timedelta(hours=1),
                slurm_accounting_submitted_at=anchor + timedelta(hours=1),
                transition=AcceptedSubmitTransition.accounting(
                    "matched_bound",
                    submit_outcome="accepted",
                    matched_slurm_job_id="72001",
                    status="submitted",
                    reconciliation_source="slurm_name_window_unique",
                ),
            )
            return str(commit_result.outcome)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(handoff_iter), pool.submit(fallback_commit)]
            results = [future.result(timeout=60) for future in futures]

        # The fallback commit must NEVER bind the same accounting incarnation:
        # either the stale anchor's Phase 6g guard blocks it
        # (``active_slurm_id_occupied``) or the restored direct's flat scan
        # blocks it (``active_slurm_id_occupied``). ``applied`` is forbidden.
        assert results[1] == "active_slurm_id_occupied", results
        persisted = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(
            gfs_job_id
        )
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None


# --- #1850 Phase 6i (invariant closure V): the marker-absent FIRST-migration
# variant of the wrong bind. When the reconcile-inventory migration marker is
# absent, the one-time backfill must NOT prune the only locator (a stale
# anchor over a settled current master with a missing derived direct). The
# migration lane preserves a handoff anchor (authority fingerprint is
# anchor-exclusive), the marker completes, and the steady-state iterator then
# restores the direct and prunes the anchor. Migration state cannot alter the
# accounting-incarnation invariant.


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_marker_absent_first_migration_public_reconcile_refuses_same_incarnation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix I: the REAL public reconcile with the migration marker ABSENT refuses
    the same accounting incarnation.

    Full sequence: IFS bound ``72001@01:00`` (marker absent), derived master
    direct removed, real ``project_forecast_cohort_tasks`` terminal projection
    with injected terminal-master direct failure (canonical journal
    ``succeeded/72001@01:00``, stale anchor present, direct absent), GFS
    reserved, then REAL ``reconcile_reserved_unbound_jobs`` with unique
    comment-less fallback ``72001@01:00``. The first run performs the one-time
    migration (marker absent): the backfill must preserve the handoff anchor
    (not prune the only locator), the marker completes, and the steady-state
    handoff restores the direct and prunes the anchor. The public outcome MUST
    be ``identity_mismatch_blocked`` / count 1, GFS held reserved/unbound with
    streak zero, and reopened canonical IFS remains the sole settled owner.
    """

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            slurm_accounting_submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        # The migration marker must be ABSENT (first migration).
        assert not (
            repository.root / "reconcile-inventory-migration-v1.json"
        ).exists()

        # Remove the derived master direct, then run the REAL batch terminal
        # projection with an injected terminal-master direct failure.
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").unlink()
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        real_direct = repository._write_pipeline_job_direct_unlocked

        def failing_direct(row: Any, record: Any) -> None:
            job_id = str((row or {}).get("job_id") or "")
            if job_id == ifs_job_id:
                raise OSError("simulated terminal master direct failure")
            return real_direct(row, record)

        repository._write_pipeline_job_direct_unlocked = failing_direct
        try:
            repository.project_forecast_cohort_tasks(
                ifs_job_id,
                master_slurm_job_id="72001",
                projections=projections,
                complete=True,
                master_status="succeeded",
                master_error_code=None,
                reconciliation_decision="matched_bound",
            )
        except Exception:
            pass
        finally:
            repository._write_pipeline_job_direct_unlocked = real_direct

        # Crash state: canonical journal succeeded/72001@01:00, stale anchor
        # present, master direct absent, marker absent.
        from services.orchestrator import file_orchestration_journal as journal_module

        journal_rows = journal_module._CycleRows()
        for record in repository._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            repository._apply_journal_record(
                journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"
        assert canonical_ifs["submitted_at"] == "2026-07-12T01:00:00Z"
        assert (
            repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
        ).exists(), "stale reconcile-inventory anchor must remain"
        assert not (
            repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
        ).exists(), "master direct must be absent after the injected failure"

        # Reserve GFS then run REAL public reconcile (marker absent -> the
        # one-time migration runs first).
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)
        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked", (
            "the marker-absent first migration must NOT let the fallback bind"
            " the same accounting incarnation: the migration lane preserves the"
            " handoff anchor, the marker completes, and the steady-state handoff"
            " restores the direct before pruning"
        )
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0

        # Migration completed; final state has the durable flat locator
        # restored and the handoff anchor pruned.
        assert (
            repository.root / "reconcile-inventory-migration-v1.json"
        ).exists(), "migration marker must complete"
        assert (
            repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
        ).exists(), "derived flat direct must be restored by the handoff"
        assert not (
            repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
        ).exists(), "handoff anchor must be pruned after the direct is restored"

        # Reopened canonical IFS remains the sole settled owner; GFS held.
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["status"] == "reserved"
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        reopened_journal_rows = journal_module._CycleRows()
        for record in reopened._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            reopened._apply_journal_record(
                reopened_journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = reopened_journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"
        assert canonical_ifs["submitted_at"] == "2026-07-12T01:00:00Z"
