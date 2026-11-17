"""Claimant exclusivity (#1850 Phase 6e/6f): canonical cycle authority over stale flat state.

Partition of ``tests/test_gateway_reconcile_claimant_exclusivity.py`` (#3177):
a stale or damaged flat direct projection can neither hide nor fake the
same-incarnation owner, the two-repository race, and the fallback flat scan
that never walks the cycle tree. The shared helpers are in
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


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_two_repository_race_commits_cannot_read_stale_canonical_state(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix C: a sibling terminal transition and a fallback commit race on one
    root; the fallback commit cannot observe a stale direct/canonical state and
    must not deadlock. Lock order stays cycle -> inventory."""

    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from services.orchestrator import reconcile as reconcile_module
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

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
        ifs_job_id, ifs_key = _reserve_second_master(
            repository,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        # IFS binds 72001 first (exclusive, no sibling claimant yet) at the
        # SAME 01:00 incarnation the fallback candidate will carry, so a
        # settled IFS still blocks the fallback (same accounting incarnation).
        from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

        bind = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert bind.committed
        # Complete the inventory migration so later backfills don't race.
        assert len(repository.query_reserved_unbound_jobs()) == 1
        # Now add GFS as a sibling reserved claimant AFTER IFS bound.
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        barrier = Barrier(2, timeout=30)

        def terminal_ifs() -> str:
            contender = FileOrchestrationJournalRepository(repository.root)
            barrier.wait(timeout=30)
            # IFS settles terminally while the GFS fallback races.
            projections, complete = _file_cohort_single_projection(contender, ifs_job_id)
            contender.project_forecast_cohort_tasks(
                ifs_job_id,
                master_slurm_job_id="72001",
                projections=projections,
                complete=True,
                master_status="succeeded",
                master_error_code=None,
                reconciliation_decision="matched_bound",
            )
            return str(contender.get_pipeline_job(ifs_job_id)["status"])

        def fallback_gfs() -> str:
            contender = FileOrchestrationJournalRepository(repository.root)
            barrier.wait(timeout=30)
            return reconcile_module.reconcile_reserved_unbound_jobs(
                contender,
                comment_query=query,
                now=lambda: query_end,
            )[0].action

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(terminal_ifs), pool.submit(fallback_gfs)]
            outcomes = [future.result(timeout=60) for future in futures]

        assert outcomes[0] == "succeeded"
        # The fallback never binds the same-incarnation id: either the active
        # owner blocks it (identity_mismatch_blocked) or the terminal
        # same-incarnation blocks it. It must never be ``bound``.
        assert outcomes[1] in {"identity_mismatch_blocked", "ambiguous_fallback_match"}, outcomes
        # GFS stays unbound; IFS owns/settles the id.
        reopened = FileOrchestrationJournalRepository(repository.root)
        gfs_row = reopened.get_pipeline_job(gfs_job_id)
        ifs_row = reopened.get_pipeline_job(ifs_job_id)
        assert ifs_row["status"] == "succeeded"
        assert ifs_row["slurm_job_id"] == "72001"
        assert gfs_row["status"] == "reserved" and gfs_row["slurm_job_id"] is None


# --- #1850 Phase 6e (invariant closure): canonical cycle authority decides
# BOTH false-positive AND false-negative occupancy — a stale flat direct
# projection may neither fabricate nor hide a same-incarnation owner.


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_stale_direct_cannot_hide_the_same_incarnation_owner(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix E: a stale flat direct projection cannot HIDE a canonical owner.

    The IFS master binds + settles ``72001`` at the canonical submitted_at
    ``2026-07-12T01:00:00Z`` so its reconcile-inventory anchor is pruned, then
    its flat direct projection is tampered to claim ``72002``. The canonical
    cycle journal still authoritatively owns ``72001 @ 01:00`` — the exact
    accounting incarnation the comment-less fallback returns. The fallback
    MUST be blocked (``identity_mismatch_blocked``, count 1) and GFS MUST
    stay reserved/unbound under the #1564 held tuple with streak zero.

    Mirror of ``test_stale_direct_same_id_but_canonical_authority_different_does_not_block``:
    that test proves a stale direct cannot FABRICATE an owner; this test
    proves it cannot HIDE one either. Both directions are the same authority
    invariant: canonical cycle authority, not the flat projection.
    """

    import json

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        # Real IFS master: bind + settle 72001 @ 01:00 with a real journal
        # lineage; the reconcile-inventory anchor is pruned on the settle.
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

        # Stale the flat direct projection: it now claims 72002 @ 01:30 while
        # the canonical cycle journal still authoritatively owns 72001 @ 01:00.
        stale = json.loads(
            (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").read_text(
                encoding="utf-8"
            )
        )
        payload = stale.get("payload", stale)
        payload["slurm_job_id"] = "72002"
        payload["matched_slurm_job_id"] = "72002"
        payload["submitted_at"] = "2026-07-12T01:30:00Z"
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").write_text(
            json.dumps(stale), encoding="utf-8"
        )
        # Sanity: the stale direct still decodes as a valid current-master row
        # (only its projected id differs from the canonical authority).
        assert (
            repository._validated_direct_pipeline_job_record(
                repository._read_optional_json(
                    repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
                ),
                expected_job_id=ifs_job_id,
            )["slurm_job_id"]
            == "72002"
        )
        # Sanity: the canonical cycle journal still authoritatively owns
        # 72001 @ 01:00 (the reconciled direct projection is only stale).
        canonical = repository._canonical_reconcile_job_unlocked(
            ifs_job_id,
            source_id="IFS",
            cycle_time=anchor,
        )
        assert canonical["slurm_job_id"] == "72001"
        assert canonical["submitted_at"] == "2026-07-12T01:00:00Z"
        assert canonical["status"] == "succeeded"

        # GFS reserves the SAME 01:00 accounting incarnation the fallback
        # query returns (unique candidate 72001 @ 01:00). The canonical owner
        # must block the bind even though the flat projection says 72002.
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
            "the canonical cycle authority still owns 72001 @ 01:00; a stale"
            " flat direct claiming 72002 must NOT let GFS reuse that exact"
            " accounting incarnation"
        )
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0
        # Reopening the journal re-reads the same result: GFS unbound, the
        # settled IFS keeps its canonical ownership of 72001.
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        assert reopened.get_pipeline_job(gfs_job_id)["status"] == "reserved"
        ifs_reopened = reopened.get_pipeline_job(ifs_job_id)
        assert ifs_reopened["status"] == "succeeded"
        assert ifs_reopened["slurm_job_id"] == "72001"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_stale_direct_hidden_owner_blocks_via_public_reconcile_and_exact_commit(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix E: the hidden-owner block is a COMMIT-level occupancy refusal, not a
    reconcile-iteration artifact.

    The same hidden-owner state driven through the typed
    ``commit_pipeline_job_submit_attempt`` with a name-window unique source
    must refuse the bind (``active_slurm_id_occupied``) — the fallback
    public action (``identity_mismatch_blocked``) is a projection of that
    commit refusal, so both entry points agree and no entry point can bind
    the exact incarnation the stale projection tried to hide.
    """

    import json

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

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

        # Stale the flat direct projection to claim 72002 @ 01:30.
        stale = json.loads(
            (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").read_text(
                encoding="utf-8"
            )
        )
        payload = stale.get("payload", stale)
        payload["slurm_job_id"] = "72002"
        payload["matched_slurm_job_id"] = "72002"
        payload["submitted_at"] = "2026-07-12T01:30:00Z"
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").write_text(
            json.dumps(stale), encoding="utf-8"
        )

        # GFS reserved, then a name-window-unique commit for the SAME 01:00
        # incarnation the canonical cycle authority owns.
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        commit_result = repository.commit_pipeline_job_submit_attempt(
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
        assert commit_result.outcome == "active_slurm_id_occupied"
        persisted = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(
            gfs_job_id
        )
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_fallback_flat_scan_never_walks_by_cycle_tree(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix E boundedness: the flat scan enumerates only the flat current-master
    surface (``max_files``-bounded) and NEVER descends the by-cycle candidate
    tree, which is where unbounded growth lives. The grouped canonical replay
    keys off the flat identities alone."""

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
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
        flat_calls = []

        def tracking_paths(*args: Any, **kwargs: Any):
            flat_calls.append("called")
            return real_paths(*args, **kwargs)

        monkeypatch.setattr(repository, "_iter_reconcile_direct_pipeline_job_paths", tracking_paths)

        # The by-cycle candidate tree must never be walked: the fallback scan
        # enumerates ONLY the flat current-master surface, and a by-cycle walk
        # would be an unbounded tree walk (candidate history lives there).
        from services.orchestrator import file_orchestration_journal as journal_module

        by_cycle_calls = []
        real_walker = journal_module._iter_regular_json_files

        def forbidden_by_cycle(*args: Any, **kwargs: Any):
            if args and "by-cycle" in str(args[0]):
                by_cycle_calls.append("called")
                raise AssertionError("fallback flat scan must not walk the by-cycle tree")
            return real_walker(*args, **kwargs)

        monkeypatch.setattr(journal_module, "_iter_regular_json_files", forbidden_by_cycle)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "bound"
        assert flat_calls, "fallback bind must consult the flat direct surface"
        assert by_cycle_calls == [], "fallback flat scan must never walk the by-cycle tree"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72001"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_exact_comment_bind_hot_path_never_scans_flat_history(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix E hot path: an exact-comment (owned_match) bind carries no candidate
    Submit and must never run the flat settled-history scan — only the
    name-window fallback producer may pay that cost."""

    from services.orchestrator.reconcile import (
        CommentAccountingResult,
        SacctRecord,
        reconcile_reserved_unbound_jobs,
    )

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

        # Warm the one-time reconcile-inventory migration BEFORE the assertion
        # patch goes live: the migration's own fingerprint pass legitimately
        # walks the flat surface once, independent of any bind.
        assert len(repository.query_reserved_unbound_jobs()) == 1

        real_paths = repository._iter_reconcile_direct_pipeline_job_paths
        calls = []

        def forbidden_paths(*args: Any, **kwargs: Any):
            calls.append("called")
            raise AssertionError("exact-comment bind must not scan the flat direct surface")

        monkeypatch.setattr(repository, "_iter_reconcile_direct_pipeline_job_paths", forbidden_paths)

        # A comment-storing exact-comment owner match: one row with the exact
        # idempotency comment and the reservation's user/account.
        comment = "nhms_idem:cycle_gfs_2026071200_forecast_fixture:forecast"
        query = lambda key, **kwargs: CommentAccountingResult(  # noqa: E731
            (
                SacctRecord(
                    slurm_job_id="72001",
                    raw_state="RUNNING",
                    job_name="nhms_forecast",
                    exit_code="0:0",
                    comment=comment,
                    user="scheduler",
                    account="account",
                ),
            ),
            scope="global",
            coverage_start=anchor,
            coverage_end=query_end,
            coverage_complete=True,
        )

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "bound"
        assert calls == [], "exact-comment bind must not scan the flat direct surface"
        assert repository.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72001"
        del real_paths


# --- #1850 Phase 6f (invariant closure II): canonical cycle authority is
# reachable even when the flat direct projection is DAMAGED (undecodable /
# fails direct-record validation). The fallback scan derives the eligible
# (source, cycle) identity from the safe filename/job-id alone, so a malformed
# projection can neither fabricate nor HIDE a canonical owner. Replayed
# canonical rows are filtered to current accepted-submit forecast MASTERS
# before occupancy adjudication; candidate/legacy rows are never owners.


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_damaged_flat_projection_cannot_hide_the_same_incarnation_owner(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix F: a damaged flat projection (valid JSON ``{}`` that fails direct
    validation) cannot HIDE the canonical owner.

    The IFS master binds + settles ``72001`` at ``2026-07-12T01:00:00Z`` so its
    reconcile-inventory anchor is pruned, then its flat direct file is replaced
    with ``{}`` (undecodable as a pipeline-job record). The canonical cycle
    journal still authoritatively owns ``72001 @ 01:00``. The fallback must be
    blocked (``identity_mismatch_blocked``, count 1) and GFS must stay
    reserved/unbound under the #1564 held tuple with streak zero.

    The eligible (source, cycle) identity must come from the SAFE FILENAME
    alone — payload decoding must not gate canonical replay, otherwise a
    malformed projection hides the very owner the fallback must respect.
    """

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
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
        # Complete the reconcile-inventory migration so a reopen does NOT
        # backfill and restore the damaged direct.
        assert len(repository.query_reserved_unbound_jobs()) == 0

        # Damage the flat direct projection: valid JSON that fails direct
        # pipeline-job record validation.
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").write_text(
            "{}", encoding="utf-8"
        )
        # Sanity: the damaged direct can no longer be decoded as a
        # pipeline-job record.
        try:
            repository._validated_direct_pipeline_job_record(
                repository._read_optional_json(
                    repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
                ),
                expected_job_id=ifs_job_id,
            )
            raise AssertionError("damaged direct must fail pipeline-job validation")
        except FileOrchestrationJournalError:
            pass
        # Sanity: the canonical cycle journal still authoritatively owns
        # 72001 @ 01:00 independent of the damaged projection.
        from services.orchestrator import file_orchestration_journal as journal_module

        journal_rows = journal_module._CycleRows()
        for record in repository._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            repository._apply_journal_record(
                journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical = journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical["slurm_job_id"] == "72001"
        assert canonical["submitted_at"] == "2026-07-12T01:00:00Z"
        assert canonical["status"] == "succeeded"

        # GFS reserves the SAME 01:00 accounting incarnation the fallback
        # query returns (unique candidate 72001 @ 01:00). The canonical owner
        # must block the bind even though the flat projection is damaged.
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
            "the canonical cycle journal still owns 72001 @ 01:00; a damaged"
            " flat projection must NOT let GFS reuse that exact accounting"
            " incarnation"
        )
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0
        # Reopening the journal re-reads the same result: GFS unbound, and the
        # canonical cycle authority still owns the settled IFS 72001 @ 01:00
        # (the damaged direct makes the public projection read-blocked, but
        # the canonical authority replay is authoritative and unchanged).
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        assert reopened.get_pipeline_job(gfs_job_id)["status"] == "reserved"
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
def test_damaged_flat_projection_fails_closed_when_no_valid_cycle_authority(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix F: a master-looking flat filename whose cycle has no valid lineage
    AND whose direct cannot be decoded/resolved fails the fallback closed.

    A damaged direct whose filename parses as an accepted-submit master
    identity, with no cycle journal to fall back on, cannot be read as
    "vacant": the typed commit raises ``FileOrchestrationJournalError`` and
    the public reconcile surface stays fail-closed (no bind, GFS stays
    reserved). Unrelated malformed filenames (non accepted-submit surface)
    never enter this authority path."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
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
        gfs_key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        # Complete the reconcile-inventory migration so a reopen does NOT
        # backfill and restore/remove the orphan.
        assert len(repository.query_reserved_unbound_jobs()) == 1

        # A master-looking flat file with NO valid cycle lineage and NO
        # decodable direct authority.
        orphan_job_id = "job_cycle_era5_2026071200_forecast_fixture_forecast"
        (repository.root / "pipeline-jobs" / f"{orphan_job_id}.json").write_text(
            "{}", encoding="utf-8"
        )

        # Typed commit surface: the occupancy scan's own flat authority
        # resolution must fail the commit closed (the reconcile iterator's
        # migration pre-scan would otherwise mask the scan).
        reopened = FileOrchestrationJournalRepository(repository.root)
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
        persisted = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(
            gfs_job_id
        )
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None

        # Public reconcile surface: the same orphan fails closed (quarantine
        # outcome, no bind, GFS stays reserved/unbound).
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)
        outcomes = reconcile_reserved_unbound_jobs(
            reopened,
            comment_query=query,
            now=lambda: query_end,
        )
        assert outcomes, "reconcile must produce an outcome"
        assert outcomes[0].job_id == gfs_job_id
        assert outcomes[0].action == "journal_quarantined"
        # Any fail-closed journal error is acceptable; the invariant is that
        # the damaged authority is NEVER read as vacancy.
        assert outcomes[0].quarantine_reason is not None
        persisted2 = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(
            gfs_job_id
        )
        assert persisted2["status"] == "reserved"
        assert persisted2["slurm_job_id"] is None
