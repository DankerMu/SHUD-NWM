"""Claimant exclusivity (#1850 Phase 6c/6d): incarnation occupancy and the flat-scan hot path.

Partition of ``tests/test_gateway_reconcile_claimant_exclusivity.py`` (#3177):
pre-outcome lane parity, accounting-incarnation occupancy, the true two-normal
race, the valid-JSON invalid anchor, and the normal hot path that skips the
settled flat scan. The shared helpers are in
``tests/gateway_reconcile_claimant_exclusivity_helpers.py``.
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
    _reserve_second_master,
)
from tests.gateway_reconcile_helpers import _file_cohort_repository
from tests.test_real_slurm_gateway import _pinned_local_timezone

# --- #1850 Phase 6c (final correctness/oracle): pre-outcome lane parity,
# accounting-incarnation occupancy, public vocabulary, true two-normal race,
# and valid-JSON invalid anchor.

@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_incarnation_scan_terminal_sibling_same_submit_blocks_recycle(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 2: a TERMINAL sibling whose durable submitted_at equals the
    candidate's Submit instant is the exact accounting incarnation and must
    block a wrong bind, even though its row is settled."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        # IFS settles terminally holding 72001 with submitted_at = 01:00.
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

        # GFS reserved at the anchor, candidate Submit instant is the SAME
        # 01:00 incarnation, inside the GFS window.
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
        assert outcome.action == "identity_mismatch_blocked"
        assert outcome.match_count == 1
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_incarnation_scan_terminal_sibling_different_submit_permits_recycle(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 2: a TERMINAL sibling whose durable submitted_at differs from the
    candidate's Submit instant is a recycled id and must NOT block."""

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

        # GFS reserved at the anchor, candidate Submit instant is a DIFFERENT
        # incarnation (01:30), inside the GFS window.
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
        assert outcome.action == "bound"
        assert outcome.matched_slurm_job_id == "72001"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["slurm_job_id"] == "72001"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72001"
        assert reopened.get_pipeline_job(ifs_job_id)["status"] == "succeeded"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_active_same_id_no_submit_blocks_even_without_submitted_at(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 2: an ACTIVE master with the same id blocks even when its durable
    submitted_at is unavailable (fail closed)."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
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
        # Normal commit WITHOUT submitted_at (submitted_at falls back to now):
        # an active owner with no durable submit instant.
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert bound.committed

        gfs_anchor = anchor + timedelta(hours=1)
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=gfs_anchor,
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
        assert outcome.action == "identity_mismatch_blocked"
        assert outcome.match_count == 1
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_two_normal_writers_race_same_id_concurrently(
    tmp_path: Any,
) -> None:
    """Fix 4: TRUE two-normal-writer concurrency race with two repository
    instances on one root and a Barrier across distinct source/cycle locks.

    Exactly one normal commit applies/idempotently owns the id; the loser
    returns the internal ``active_slurm_id_occupied`` occupancy outcome; no
    deadlock. Repeated for stability."""

    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        _reserve_second_master(
            repository,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        gfs_key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        barrier = Barrier(2, timeout=30)

        def gfs_normal() -> str:
            contender = FileOrchestrationJournalRepository(repository.root)
            barrier.wait(timeout=30)
            return contender.commit_pipeline_job_submit_attempt(
                gfs_key,
                pipeline_job_id=gfs_job_id,
                expected_submission_attempt=1,
                slurm_job_id="72001",
                submitted_at=anchor + timedelta(minutes=30),
                transition=AcceptedSubmitTransition.accepted(status="submitted"),
            ).outcome

        def ifs_normal() -> str:
            contender = FileOrchestrationJournalRepository(repository.root)
            barrier.wait(timeout=30)
            return contender.commit_pipeline_job_submit_attempt(
                ifs_key,
                pipeline_job_id=ifs_job_id,
                expected_submission_attempt=1,
                slurm_job_id="72001",
                submitted_at=anchor + timedelta(minutes=40),
                transition=AcceptedSubmitTransition.accepted(status="submitted"),
            ).outcome

        outcomes = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(gfs_normal), pool.submit(ifs_normal)]
            outcomes = [future.result(timeout=60) for future in futures]

        applied = [o for o in outcomes if o in {"applied", "idempotent"}]
        assert len(applied) == 1, outcomes
        loser = [o for o in outcomes if o not in {"applied", "idempotent"}]
        assert loser and loser[0] in {"active_slurm_id_occupied", "identity_mismatch_blocked"}, outcomes

        owners = []
        reopened = FileOrchestrationJournalRepository(repository.root)
        for job_id in (gfs_job_id, ifs_job_id):
            row = reopened.get_pipeline_job(job_id)
            if row is not None and row.get("slurm_job_id") == "72001":
                owners.append(job_id)
        assert len(owners) == 1, owners


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_valid_json_invalid_anchor_schema_fails_closed(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 5: a VALID-JSON but invalid anchor (wrong schema_version) must bite
    the ``_validated_reconcile_inventory_anchor`` validation path and fail the
    commit closed — not the decode path."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        _reserve_second_master(
            repository,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        assert len(repository.query_reserved_unbound_jobs()) == 2
        # Valid JSON, invalid schema_version.
        anchor_path = (
            repository.root
            / "reconcile-inventory"
            / "job_cycle_ifs_2026071200_forecast_fixture_forecast.json"
        )
        anchor_path.write_text(
            '{"schema_version": "wrong.version", "job_id":'
            ' "job_cycle_ifs_2026071200_forecast_fixture_forecast",'
            ' "row_kind": "current_master", "source_id": "IFS",'
            ' "cycle_time": "2026-07-12T00:00:00Z"}',
            encoding="utf-8",
        )

        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        gfs_key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        reopened = FileOrchestrationJournalRepository(repository.root)
        # Drive the typed commit directly so the occupancy SCAN's own anchor
        # validation is the path under test (the reconcile iterator would fail
        # first). The invalid authority must never be read as free.
        with pytest.raises(FileOrchestrationJournalError):
            reopened.commit_pipeline_job_submit_attempt(
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
        persisted = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None


# --- #1850 Phase 6d (final small fixes): normal hot path skips settled scan,
# flat direct is candidate-only with canonical confirmation, and all writes
# that CREATE/CHANGE a bound owner or CREATE/RECLAIM a reserved claimant
# serialize under the global inventory lock (batch terminal/rejection/operator
# transitions may bypass: they never introduce an owner/claimant, and stale
# anchors are adjudicated canonically).

@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_normal_fresh_id_commit_never_touches_flat_direct_scan(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix A: a fresh normal typed commit (no name-window source) must NOT scan
    the flat settled master surface. The flat incarnation scan runs only for
    the fallback producer, which is the only lane that carries a candidate
    Submit and needs recycled-id disambiguation."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        gfs_key = "cycle_gfs_2026071200_forecast_fixture:forecast"

        real_paths = repository._iter_reconcile_direct_pipeline_job_paths
        calls = []

        def forbidden_paths(*args: Any, **kwargs: Any):
            calls.append("called")
            raise AssertionError("normal commit must not scan the flat direct surface")

        monkeypatch.setattr(repository, "_iter_reconcile_direct_pipeline_job_paths", forbidden_paths)

        result = repository.commit_pipeline_job_submit_attempt(
            gfs_key,
            pipeline_job_id=gfs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )

        assert result.committed
        assert calls == []
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72001"
        del real_paths


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_fallback_flat_scan_still_considers_terminal_incarnation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix A + E: the fallback lane DOES call the flat direct scan (it needs the
    terminal incarnation check), unlike the normal lane.

    Fix E boundedness: the grouped canonical replay reads each distinct
    (source, cycle) journal at most once even when several flat candidates
    share the same cycle, so the fallback scan stays bounded by
    ``max_files``-bounded flat identities — never an unbounded tree walk.
    """

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        real_paths = repository._iter_reconcile_direct_pipeline_job_paths
        calls = []

        def tracking_paths(*args: Any, **kwargs: Any):
            calls.append("called")
            return real_paths(*args, **kwargs)

        monkeypatch.setattr(repository, "_iter_reconcile_direct_pipeline_job_paths", tracking_paths)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "bound"
        assert calls, "fallback bind must consult the flat direct scan"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["slurm_job_id"] == "72001"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_stale_direct_same_id_but_canonical_authority_different_does_not_block(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix B: a stale flat direct projection never decides occupancy. The
    canonical cycle authority does. Two genuine scenarios:

    1. Stale direct with a DIFFERENT canonical authority: the IFS master binds
       + settles holding 72001 @ 01:00 (journal lineage intact); its flat
       direct file is forged to claim 72002 @ 01:30. The lenient filter
       decodes the stale direct, but canonical confirmation says 72002 is
       FREE (the real authority holds 72001) — the fallback must NOT be
       falsely blocked.
    2. Invalid canonical authority: the same valid direct claiming 72001 @
       01:00, but its cycle journal is corrupted into a segment gap. The
       canonical authority replay fails — the occupancy scan must fail closed
       rather than read vacancy from a broken authority.
    """

    import json

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        # Real IFS master: bind + settle 72001 @ 01:00 with a real journal
        # lineage.
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
        real_direct = repository._direct_pipeline_job_record(ifs_job_id)
        assert real_direct["slurm_job_id"] == "72001"

        # --- Scenario 1: stale direct claims a DIFFERENT id than canonical. ---
        stale = json.loads(
            (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").read_text(
                encoding="utf-8"
            )
        )
        payload = stale.get("payload", stale)
        payload["slurm_job_id"] = "72002"
        # The forged row must stay self-consistent under the closed-world bound
        # identity (Fix B): a matched_bound row's matched id must equal the id
        # it claims to own. The point of the scenario is that the stale direct
        # CLAIMS a different id than the canonical authority -- not that it
        # contradicts itself.
        payload["matched_slurm_job_id"] = "72002"
        payload["submitted_at"] = "2026-07-12T01:30:00Z"
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").write_text(
            json.dumps(stale), encoding="utf-8"
        )
        # Sanity: the forged direct still decodes as a same-id master.
        assert (
            repository._validated_direct_pipeline_job_record(
                repository._read_optional_json(
                    repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
                ),
                expected_job_id=ifs_job_id,
            )["slurm_job_id"]
            == "72002"
        )

        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72002", submit="2026-07-12T01:30:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "bound", (
            "stale direct claiming 72002 must NOT block: canonical authority"
            " holds 72001"
        )
        assert outcome.matched_slurm_job_id == "72002"
        assert repository.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72002"

        # --- Scenario 2: decoded same-id candidate whose canonical authority
        # is INVALID fails closed. A separate root where the flat direct
        # claims 72001 @ 01:00, but its cycle journal is corrupted into a
        # segment gap. The canonical authority replay raises
        # ``file_journal_segment_gap`` — the occupancy scan must fail closed
        # rather than read vacancy from a broken authority.
        repository2 = _file_cohort_repository(
            tmp_path / "gfs2",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        valid_direct = json.loads(
            (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").read_text(
                encoding="utf-8"
            )
        )
        # Restore the true 72001 identity (scenario 1 changed it to 72002),
        # then corrupt the canonical authority's journal into a segment gap:
        # segment-1 present with segment-0 removed. The flat direct stays a
        # valid decoded same-id candidate, but its canonical cycle authority
        # is broken — the flat scan's grouped replay raises
        # file_journal_segment_gap and the commit fails closed.
        payload2 = valid_direct.get("payload", valid_direct)
        payload2["slurm_job_id"] = "72001"
        payload2["submitted_at"] = "2026-07-12T01:00:00Z"
        payload2["matched_slurm_job_id"] = "72001"
        (repository2.root / "pipeline-jobs" / f"{ifs_job_id}.json").write_text(
            json.dumps(valid_direct), encoding="utf-8"
        )
        ifs_journal_dir = repository2.root / "journal" / "IFS"
        ifs_journal_dir.mkdir(parents=True, exist_ok=True)
        segment_zero = ifs_journal_dir / "2026071200.jsonl"
        segment_zero.write_text("", encoding="utf-8")
        segment_zero.unlink()
        (ifs_journal_dir / "2026071200.1.jsonl").write_text("", encoding="utf-8")
        # Sanity: the direct still decodes as a current-master 72001.
        assert (
            repository2._validated_direct_pipeline_job_record(
                repository2._read_optional_json(
                    repository2.root / "pipeline-jobs" / f"{ifs_job_id}.json"
                ),
                expected_job_id=ifs_job_id,
            )["slurm_job_id"]
            == "72001"
        )

        gfs2_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        gfs2_key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        reopened2 = FileOrchestrationJournalRepository(repository2.root)
        # Drive the typed commit directly so the occupancy SCAN itself (the
        # flat direct canonical confirmation) is the path under test — the
        # reconcile iterator's migration pre-scan would fail first and mask
        # the scan.
        with pytest.raises(FileOrchestrationJournalError):
            reopened2.commit_pipeline_job_submit_attempt(
                gfs2_key,
                pipeline_job_id=gfs2_job_id,
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
        persisted2 = FileOrchestrationJournalRepository(
            repository2.root
        ).get_pipeline_job(gfs2_job_id)
        assert persisted2["status"] == "reserved"
        assert persisted2["slurm_job_id"] is None
