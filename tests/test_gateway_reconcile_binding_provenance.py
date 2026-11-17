"""#1850 Round 2 invariant closure: attempt-scoped binding provenance.

The governing invariant: one durable claimant may bind one uniquely owned
Slurm accounting incarnation, and every later transition/locator must preserve
the canonical bind provenance needed to identify that incarnation; derived
payload state may neither manufacture vacancy nor rewrite provenance;
uncertainty is fail-closed.

These tests cover the three verified FIX_NOW defects on the pre-fix head:

- cand-r2-state-01: a complete terminal ``matched_bound`` projection must not
  replace a name-window fallback bind's provenance with the exact-comment
  factory default.
- cand-r2-state-02: the gateway/commit ``submitted_at`` (T1) is never canonical
  accounting Submit evidence; a settled same-id sibling without canonical
  provenance blocks fail-closed even when its legacy ``submitted_at`` differs
  from the candidate Submit (including microsecond-only differences).
- cand-r2-state-03: source/cycle discovery comes unconditionally from the safe
  master filename, so a validator-accepted wrong-kind payload planted under a
  settled master filename can never suppress canonical replay.

Plus the transition/validation surface: binding provenance is immutable for one
bound attempt, cleared by clean reservation/reclaim, and present on the
scheduler attempt evidence projection.

This module keeps the projection and settled-sibling provenance cases and the
wrong-kind payload. #3177 moved the other lanes to
``tests/test_gateway_reconcile_binding_provenance_normalizer.py``,
``tests/test_gateway_reconcile_binding_provenance_replay.py`` and
``tests/test_gateway_reconcile_binding_provenance_legacy.py``; the shared field
names and helpers are in ``tests/gateway_reconcile_binding_provenance_helpers.py``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION, AcceptedSubmitTransition
from tests.gateway_reconcile_binding_provenance_helpers import (
    _ANCHOR,
    _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
    _SLURM_BINDING_SOURCE_FIELD,
    _fallback_querier,
    _fallback_row,
    _incomplete_projection,
    _reserve_gfs_sibling,
    _single_projection,
)
from tests.gateway_reconcile_helpers import _commit_name_window_fallback_bind, _file_cohort_repository
from tests.test_real_slurm_gateway import _pinned_local_timezone

# ---------------------------------------------------------------------------
# cand-r2-state-01: terminal projection preserves fallback bind provenance
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_terminal_projection_preserves_fallback_binding_provenance(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real fallback bind -> complete projection preserves immutable provenance.

    Before the fix this was red: ``project_forecast_cohort_tasks`` called
    ``AcceptedSubmitTransition.accounting()`` with the exact-comment factory
    default, replaced the full tuple, and the durable/public row falsely
    claimed exact-comment recovery.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed
        assert bind.wrote
        bound_row = repository.get_pipeline_job(job_id)
        assert bound_row[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert bound_row[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"
        assert bound_row["reconciliation_source"] == "slurm_name_window_unique"
        assert bound_row["reconciliation_decision"] == "matched_bound"

        projections, complete = _single_projection(repository, job_id)
        assert complete
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )

        terminal = repository.get_pipeline_job(job_id)
        assert terminal["status"] == "succeeded"
        assert terminal["reconciliation_decision"] == "matched_bound"
        assert terminal["matched_slurm_job_id"] == "72001"
        # Immutable binding provenance survives the terminal projection.
        assert terminal[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert terminal[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"
        # The current matched-bound source is restored to name-window, never the
        # exact-comment factory default.
        assert terminal["reconciliation_source"] == "slurm_name_window_unique"

        # Reopened journal agrees.
        reopened = FileOrchestrationJournalRepository(repository.root)
        reopened_terminal = reopened.get_pipeline_job(job_id)
        assert reopened_terminal[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert reopened_terminal["reconciliation_source"] == "slurm_name_window_unique"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_defer_then_complete_projection_restores_fallback_source(
    tmp_path: Any,
) -> None:
    """Fallback bind -> incomplete coverage defer -> later complete projection.

    The defer reasserts the exact-comment held tuple (accounting_unavailable,
    no matched id) but must retain the immutable binding provenance; the later
    complete matched-bound projection restores the name-window current source
    and the matched id from binding provenance.
    """

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed

        # Incomplete coverage: defer parks the row, clears matched id, keeps the
        # current tuple exact-comment sourced.
        projections, complete = _incomplete_projection(repository, job_id)
        assert not complete
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=False,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        deferred = repository.get_pipeline_job(job_id)
        assert deferred["status"] == "reconcile_unverified"
        assert deferred["reconciliation_decision"] == "accounting_unavailable"
        assert deferred["reconciliation_reason_class"] == "coverage_incomplete"
        assert deferred["matched_slurm_job_id"] is None
        # Binding provenance survives the defer.
        assert deferred[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert deferred[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"

        # Later complete projection restores source and matched id.
        full, full_complete = _single_projection(repository, job_id)
        assert full_complete
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=full,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        terminal = repository.get_pipeline_job(job_id)
        assert terminal["status"] == "succeeded"
        assert terminal["reconciliation_decision"] == "matched_bound"
        assert terminal["matched_slurm_job_id"] == "72001"
        assert terminal["reconciliation_source"] == "slurm_name_window_unique"
        assert terminal[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert terminal[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_terminal_projection_exact_comment_sibling_stays_exact_comment(
    tmp_path: Any,
) -> None:
    """An exact-comment/gateway sibling projected terminally stays exact-comment.

    The source-restoration helper must not rewrite a normal/exact-comment bind
    into name-window; only fallback-bound rows restore name-window.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bind = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert bind.committed
        bound_row = repository.get_pipeline_job(job_id)
        assert bound_row[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"
        assert bound_row[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None

        projections, _complete = _single_projection(repository, job_id)
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        terminal = repository.get_pipeline_job(job_id)
        assert terminal["status"] == "succeeded"
        assert terminal["reconciliation_source"] == "slurm_exact_comment"
        assert terminal[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"
        assert terminal[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id)["reconciliation_source"] == "slurm_exact_comment"


# ---------------------------------------------------------------------------
# cand-r2-state-02: gateway/commit submitted_at is never incarnation proof
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_gateway_bound_settled_sibling_blocks_fallback_even_with_different_legacy_submit(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal gateway bind with legacy submitted_at=T1, no canonical accounting
    Submit -> settle -> sibling fallback same numeric id with sacct T0 != T1:
    held identity_mismatch_blocked/1, #1564 tuple and streak zero, no double owner.

    Before the fix the mixed timestamp T0 != T1 was interpreted as recycle and
    double-bound the same job.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        # Gateway acceptance time T1 = 01:00:00.123456 (microseconds, distinct
        # from the whole-second sacct T0 the fallback later sees).
        gateway_t1 = _ANCHOR + timedelta(hours=1) + timedelta(microseconds=123456)
        bind = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=gateway_t1,
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert bind.committed
        bound_row = repository.get_pipeline_job(job_id)
        assert bound_row[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"
        assert bound_row[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None
        assert bound_row["submitted_at"] == "2026-07-12T01:00:00.123456Z"

        projections, _complete = _single_projection(repository, job_id)
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(job_id)["status"] == "succeeded"

        # GFS fallback sees the same numeric id with sacct Submit T0 (whole
        # second, no microseconds) -- different from the gateway T1.
        gfs_job_id = _reserve_gfs_sibling(repository, created_at=_ANCHOR + timedelta(hours=1))
        query_end = _ANCHOR + timedelta(hours=2)
        query = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T01:00:00"),
            query_end=query_end,
        )
        outcome = reconcile_reserved_unbound_jobs(
            repository, comment_query=query, now=lambda: query_end
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked"
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0
        # The settled gateway-bound IFS owner keeps its bind; no double owner.
        ifs = repository.get_pipeline_job(job_id)
        assert ifs["slurm_job_id"] == "72001"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        assert reopened.get_pipeline_job(job_id)["slurm_job_id"] == "72001"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_exact_comment_bound_settled_sibling_without_canonical_submit_blocks_fallback(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exact-comment matched bind without canonical Submit -> settle -> sibling
    fallback same id at a different candidate Submit: also fail-closed."""

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        # Exact-comment recovery bind: matched_bound with exact-comment source,
        # legacy submitted_at T1, NO canonical accounting Submit.
        bind = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_exact_comment",
            ),
        )
        assert bind.committed
        bound_row = repository.get_pipeline_job(job_id)
        assert bound_row[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_exact_comment"
        assert bound_row[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None

        projections, _complete = _single_projection(repository, job_id)
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(job_id)["status"] == "succeeded"

        gfs_job_id = _reserve_gfs_sibling(repository, created_at=_ANCHOR + timedelta(hours=1))
        query_end = _ANCHOR + timedelta(hours=2)
        query = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T02:00:00"),
            query_end=query_end,
        )
        outcome = reconcile_reserved_unbound_jobs(
            repository, comment_query=query, now=lambda: query_end
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked"
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0
        # Settled exact-comment owner keeps its bind.
        assert repository.get_pipeline_job(job_id)["slurm_job_id"] == "72001"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id)["slurm_job_id"] == "72001"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_settled_fallback_same_canonical_submit_blocks_and_different_permits_recycle(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallback-bound settled row canonical Submit T0: same id T0 blocks;
    same id canonical T2 permits genuine recycle. Legacy submitted_at is
    irrelevant in both."""

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        # Settle IFS at canonical 01:00 with a fallback bind.
        repository = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed
        projections, _complete = _single_projection(repository, job_id)
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(job_id)["status"] == "succeeded"

        # Same canonical incarnation 01:00: blocks.
        gfs_job_id = _reserve_gfs_sibling(repository, created_at=_ANCHOR + timedelta(hours=1))
        query_end = _ANCHOR + timedelta(hours=2)
        query = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T01:00:00"),
            query_end=query_end,
        )
        outcome = reconcile_reserved_unbound_jobs(
            repository, comment_query=query, now=lambda: query_end
        )[0]
        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked"
        assert outcome.match_count == 1
        assert repository.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None

        # Different canonical incarnation 02:00: genuine recycle permits bind.
        repository2 = _file_cohort_repository(
            tmp_path / "ifs2",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        bind2 = _commit_name_window_fallback_bind(
            repository2, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind2.committed
        projections2, _ = _single_projection(repository2, job_id)
        repository2.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections2,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        gfs_job_id2 = _reserve_gfs_sibling(repository2, created_at=_ANCHOR + timedelta(hours=1))
        query2 = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T02:00:00"),
            query_end=query_end,
        )
        outcome2 = reconcile_reserved_unbound_jobs(
            repository2, comment_query=query2, now=lambda: query_end
        )[0]
        assert outcome2.job_id == gfs_job_id2
        assert outcome2.action == "bound"
        assert outcome2.matched_slurm_job_id == "72001"
        assert outcome2.reconciliation_source == "slurm_name_window_unique"
        persisted2 = repository2.get_pipeline_job(gfs_job_id2)
        assert persisted2["slurm_job_id"] == "72001"
        assert persisted2[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T02:00:00Z"
        reopened2 = FileOrchestrationJournalRepository(repository2.root)
        assert reopened2.get_pipeline_job(gfs_job_id2)["slurm_job_id"] == "72001"
        assert reopened2.get_pipeline_job(job_id)["slurm_job_id"] == "72001"


# ---------------------------------------------------------------------------
# cand-r2-state-03: safe filename owns cycle discovery
# ---------------------------------------------------------------------------


def _plant_wrong_kind_payload(repository: Any, job_id: str, *, legacy: bool) -> None:
    """Plant a validator-accepted wrong-kind payload under the master filename."""
    from services.orchestrator.file_orchestration_journal import (
        FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
    )

    if legacy:
        payload = {
            "job_id": job_id,
            "run_id": "cycle_ifs_2026071200_forecast_fixture",
            "cycle_id": "ifs_2026071200",
            "job_type": "run_shud_forecast_array",
            "model_id": None,
            "status": "succeeded",
            "stage": "forecast",
            "candidate_id": None,
            "submitted_at": "2026-07-12T01:00:00Z",
        }
    else:
        # A valid accepted-submit CANDIDATE row shape under the master filename.
        payload = {
            "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
            "job_id": job_id,
            "run_id": "cycle_ifs_2026071200_forecast_fixture",
            "cycle_id": "ifs_2026071200",
            "job_type": "run_shud_forecast_array",
            "model_id": "model_0",
            "array_task_id": 0,
            "candidate_id": "IFS:2026-07-12T00:00:00Z:model_0:default",
            "status": "succeeded",
            "stage": "forecast",
            "submit_outcome": "accepted",
            "restart_stage": "forecast",
            "native_shud_resubmitted": False,
        }
    record = {
        "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
        "record_type": "pipeline_job",
        "source_id": "IFS",
        "cycle_time": "2026-07-12T00:00:00Z",
        "job_id": job_id,
        "payload": payload,
    }
    (repository.root / "pipeline-jobs" / f"{job_id}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    # The planted payload must be genuinely validator-accepted (never a
    # monkeypatched validator).
    direct = repository._validated_direct_pipeline_job_record(
        repository._read_optional_json(repository.root / "pipeline-jobs" / f"{job_id}.json"),
        expected_job_id=job_id,
    )
    assert direct["job_id"] == job_id


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
@pytest.mark.parametrize("legacy", [True, False], ids=["legacy_payload", "candidate_payload"])
def test_valid_wrong_kind_payload_under_master_filename_cannot_hide_canonical_owner(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    """A validator-accepted legacy/candidate payload planted under a settled
    master filename still triggers canonical replay and blocks the same
    incarnation. Before the fix the decoded wrong-kind payload early-continued
    before retaining the cycle, hiding the canonical owner."""

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed
        projections, _complete = _single_projection(repository, job_id)
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(job_id)["status"] == "succeeded"
        assert len(repository.query_reserved_unbound_jobs()) == 0

        # Plant a validator-accepted wrong-kind payload over the master direct.
        _plant_wrong_kind_payload(repository, job_id, legacy=legacy)

        gfs_job_id = _reserve_gfs_sibling(repository, created_at=_ANCHOR + timedelta(hours=1))
        query_end = _ANCHOR + timedelta(hours=2)
        query = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T01:00:00"),
            query_end=query_end,
        )
        outcome = reconcile_reserved_unbound_jobs(
            repository, comment_query=query, now=lambda: query_end
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked", (
            "a valid wrong-kind payload under the master filename must not hide"
            " the canonical settled owner"
        )
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0
        # Canonical owner unchanged and still authoritative.
        assert repository.get_pipeline_job(job_id)["slurm_job_id"] == "72001"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        assert reopened.get_pipeline_job(job_id)["status"] == "succeeded"
        assert reopened.get_pipeline_job(job_id)["slurm_job_id"] == "72001"
